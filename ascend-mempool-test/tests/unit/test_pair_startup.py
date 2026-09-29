"""Check BM rank-pair startup at the MemFabric SDK boundary."""

import unittest

from test_pool import FakeBM

from ascend_mempool.layout import KVLayout, PoolLayout
from ascend_mempool.pool import MempoolKVManager


class FakeBMConfig:
    """Record the BM rank and NIC selected for one worker process."""

    def set_nic(self, nic_url):
        """Store the NIC URL passed to the native BM config."""
        self.nic_url = nic_url


class FakePairBM(FakeBM):
    """Emulate process-wide BM initialization and pool creation."""

    BmConfig = FakeBMConfig

    def __init__(self, *, init_status=0, mapped=True, create_handle=True):
        """Allow startup failure at the init, create, or mapping boundary."""
        super().__init__(rank=0)
        self.init_status = init_status
        self.mapped = mapped
        self.create_handle = create_handle
        self.init_calls = []
        self.uninit_calls = 0
        self.initialized = False
        self.reported_rank = None

    def initialize(self, store_url, world_size, device_id, config):
        """Record exactly which two-rank BM session the worker joins."""
        self.init_calls.append((store_url, world_size, device_id, config))
        self.rank = config.rank_id
        self.initialized = self.init_status == 0
        return self.init_status

    def bm_rank_id(self):
        """Report the BM rank assigned during initialization."""
        if not self.initialized:
            return 0
        return self.rank if self.reported_rank is None else self.reported_rank

    def uninitialize(self):
        """Record release of the process-wide BM context."""
        self.uninit_calls += 1
        self.initialized = False

    def create2(self, **options):
        """Create a handle whose peer mapping may remain unavailable."""
        if not self.create_handle:
            return None
        handle = super().create2(**options)
        handle.mapping_available = self.mapped
        return handle


class TestPairStartup(unittest.TestCase):
    """Observe per-pair session identity and cleanup through the public manager."""

    def setUp(self):
        """Use a compact two-rank layout with the production 16-slot topology."""
        layer = KVLayout(layers=1, slots=16, tokens=8, heads=1, dim=8)
        self.layout = PoolLayout(layer, layer)

    def start(self, sdk, **overrides):
        """Start one worker with valid defaults and selected overrides."""
        arguments = dict(
            layout=self.layout,
            tp_rank=0,
            role="prefill",
            store_host="10.120.72.31",
            base_port=18573,
            device_id=0,
            nic_url="tcp://10.120.72.31:24670",
            timeout=0.01,
            bm_module=sdk,
        )
        arguments.update(overrides)
        return MempoolKVManager.initialize_rank_pair(**arguments)

    def test_each_pair_uses_its_own_port_and_local_bm_ranks(self):
        """P_i and D_i share a store, while adjacent pairs use distinct stores."""
        for tp_rank in range(16):
            for role, expected_rank in (("prefill", 0), ("decode", 1)):
                with self.subTest(tp_rank=tp_rank, role=role):
                    sdk = FakePairBM()
                    manager = self.start(
                        sdk,
                        tp_rank=tp_rank,
                        role=role,
                        device_id=tp_rank,
                    )
                    url, world_size, device_id, config = sdk.init_calls[0]
                    self.assertEqual(url, f"tcp://10.120.72.31:{18573 + tp_rank}")
                    self.assertEqual((world_size, device_id), (2, tp_rank))
                    self.assertEqual(config.rank_id, expected_rank)
                    self.assertFalse(config.auto_ranking)
                    self.assertEqual(config.start_store, role == "prefill")
                    self.assertEqual(config.nic_url, "tcp://10.120.72.31:24670")
                    self.assertEqual(config.init_timeout, 1)
                    self.assertEqual(manager.rank, expected_rank)
                    self.assertGreater(
                        manager.view(1 - expected_rank, 0).device_base, 0
                    )
                    manager.close(drain=lambda: None)
                    self.assertTrue(sdk.handle.destroyed)
                    self.assertEqual(sdk.uninit_calls, 1)

    def test_invalid_identity_and_network_settings_fail_before_bm_initialization(self):
        """Reject roles, ranks, ports, and addresses that cannot identify a pair."""
        for override in (
            {"role": "local"},
            {"tp_rank": -1},
            {"tp_rank": 16},
            {"tp_rank": True},
            {"base_port": 65521},
            {"base_port": 0},
            {"store_host": ""},
            {"device_id": -1},
            {"nic_url": ""},
            {"timeout": 0},
            {"pool_id": 256},
        ):
            with self.subTest(override=override):
                sdk = FakePairBM()
                with self.assertRaises(ValueError):
                    self.start(sdk, **override)
                self.assertEqual(sdk.init_calls, [])

    def test_gate_pool_ids_are_accepted_by_service_startup(self):
        """Allow independently verified BM IDs below the TransferEngine range."""
        for pool_id in (0, 64, 101, 102, 255):
            with self.subTest(pool_id=pool_id):
                sdk = FakePairBM()
                manager = self.start(sdk, pool_id=pool_id)
                manager.close(drain=lambda: None)

    def test_failed_startup_releases_only_resources_it_initialized(self):
        """Do not retain BM state after init, create, or join errors."""
        for sdk, error, uninit in (
            (FakePairBM(init_status=-7), RuntimeError, 0),
            (FakePairBM(create_handle=False), RuntimeError, 1),
            (FakePairBM(mapped=False), TimeoutError, 1),
        ):
            with self.subTest(error=error, sdk=sdk):
                with self.assertRaises(error):
                    self.start(sdk)
                self.assertEqual(sdk.uninit_calls, uninit)
                if sdk.handle is not None:
                    self.assertTrue(sdk.handle.destroyed)

    def test_rejects_a_second_manager_pair_and_wrong_initialized_rank(self):
        """Keep one active pair per worker and verify BM's assigned rank."""
        first = FakePairBM()
        manager = self.start(first)
        second = FakePairBM()
        with self.assertRaisesRegex(RuntimeError, "already owns"):
            self.start(second)
        self.assertEqual(second.init_calls, [])
        self.assertEqual(second.uninit_calls, 0)
        manager.close(drain=lambda: None)

        wrong_rank = FakePairBM()
        wrong_rank.reported_rank = 1
        with self.assertRaisesRegex(RuntimeError, "unexpected rank"):
            self.start(wrong_rank)
        self.assertEqual(wrong_rank.uninit_calls, 1)
        self.assertIsNone(wrong_rank.handle)

    def test_failed_drain_keeps_owned_bm_context_alive(self):
        """Never uninitialize BM while captured work might still use its mapping."""
        sdk = FakePairBM()
        manager = self.start(sdk)

        def pending_work():
            """Represent NPU work that has not drained."""
            raise RuntimeError("pending NPU reads")

        with self.assertRaisesRegex(RuntimeError, "pending NPU reads"):
            manager.close(drain=pending_work)
        self.assertEqual(sdk.uninit_calls, 0)
        self.assertFalse(sdk.handle.destroyed)
        self.assertGreater(manager.view(0, 0).device_base, 0)
        manager.close(drain=lambda: None)
        manager.close(drain=lambda: None)
        self.assertEqual(sdk.uninit_calls, 1)


if __name__ == "__main__":
    unittest.main()
