"""Check BM rank-pair startup at the MemFabric SDK boundary."""

import os
import unittest
from unittest.mock import patch

from test_pool import FakeBM

from ascend_mempool.layout import KVLayout, PoolLayout
from ascend_mempool.pool import MempoolKVManager

NUMA_NODE_ENV = "SGLANG_NPU_MEMPOOL_LOCAL_NUMA_NODE"


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


class StartupClock:
    """Advance startup deadlines without sleeping through model loading."""

    def __init__(self):
        """Start the simulated D worker at time zero."""
        self.now = 0.0

    def monotonic(self):
        """Return elapsed startup time in seconds."""
        return self.now

    def sleep(self, seconds):
        """Advance time by the requested polling interval."""
        self.now += seconds


class TestPairStartup(unittest.TestCase):
    """Observe per-pair session identity and cleanup through the public manager."""

    def setUp(self):
        """Use a compact two-rank layout with the production 16-slot topology."""
        layer = KVLayout(layers=1, slots=16, tokens=8, heads=1, dim=8)
        self.layout = PoolLayout(layer, layer)
        connection_patch = patch("socket.create_connection")
        self.connect = connection_patch.start()
        self.addCleanup(connection_patch.stop)
        environment_patch = patch.dict(os.environ)
        environment_patch.start()
        self.addCleanup(environment_patch.stop)
        os.environ.pop(NUMA_NODE_ENV, None)
        os.environ.pop("SGLANG_NPU_MEMPOOL_LOCAL_NUMA_NODE_COUNT", None)
        topology_patch = patch("pathlib.Path.read_text", return_value="0-7\n")
        self.read_topology = topology_patch.start()
        self.addCleanup(topology_patch.stop)

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

    def test_decode_waits_for_slow_prefill_before_entering_bm(self):
        """A P store appearing after MF's 60 retries must still allow D startup."""
        sdk = FakePairBM()
        clock = StartupClock()
        connection = self.connect.return_value
        initialize = sdk.initialize

        def connect_when_prefill_loaded(address, timeout):
            """Refuse D until P finishes its longer model load at 90 seconds."""
            self.assertEqual(address, ("10.120.72.31", 18576))
            self.assertGreater(timeout, 0)
            self.assertLessEqual(timeout, 1.0)
            self.assertEqual(sdk.init_calls, [])
            if clock.now < 90:
                raise ConnectionRefusedError("P is still loading weights")
            return connection

        def initialize_after_listen(*args):
            """Model MF failing if entered before the P store is listening."""
            if clock.now < 90:
                return -1
            connection.__exit__.assert_called_once()
            return initialize(*args)

        self.connect.side_effect = connect_when_prefill_loaded
        with (
            patch("ascend_mempool.manager.time", clock),
            patch.object(sdk, "initialize", side_effect=initialize_after_listen),
        ):
            manager = self.start(sdk, role="decode", tp_rank=3, timeout=600)
            self.addCleanup(manager.close, drain=lambda: None)
        self.assertGreaterEqual(clock.now, 90)
        self.assertLess(clock.now, 600)
        self.assertEqual(len(sdk.init_calls), 1)
        self.assertEqual(sdk.init_calls[0][3].init_timeout, 600)

    def test_decode_store_wait_expires_without_initializing_bm(self):
        """An absent P store times out without allocating or retaining BM state."""
        sdk = FakePairBM()
        clock = StartupClock()
        self.connect.side_effect = ConnectionRefusedError("P is not listening")
        with (
            patch("ascend_mempool.manager.time", clock),
            self.assertRaisesRegex(TimeoutError, "P BM store.*18573.*1.25s"),
        ):
            manager = self.start(sdk, role="decode", timeout=1.25)
            self.addCleanup(manager.close, drain=lambda: None)
        self.assertAlmostEqual(clock.now, 1.25)
        self.assertEqual(sdk.init_calls, [])
        self.assertEqual(sdk.uninit_calls, 0)
        self.assertIsNone(sdk.handle)

    def test_decode_connect_timeout_obeys_remaining_wait_budget(self):
        """An unresponsive address cannot make each probe restart the deadline."""
        sdk = FakePairBM()
        clock = StartupClock()

        def connect_times_out(address, timeout):
            """Consume the socket timeout as a dropped connection attempt would."""
            self.assertLessEqual(clock.now + timeout, 0.25)
            clock.sleep(timeout)
            raise TimeoutError("TCP connect timed out")

        self.connect.side_effect = connect_times_out
        with (
            patch("ascend_mempool.manager.time", clock),
            self.assertRaisesRegex(TimeoutError, "P BM store.*0.25s"),
        ):
            manager = self.start(sdk, role="decode", timeout=0.25)
            self.addCleanup(manager.close, drain=lambda: None)
        self.assertAlmostEqual(clock.now, 0.25)
        self.connect.assert_called_once()
        self.assertEqual(sdk.init_calls, [])

    def test_prefill_starts_its_store_without_waiting_for_it(self):
        """P must enter BM directly so D has a store to connect to."""
        sdk = FakePairBM()
        manager = self.start(sdk)
        self.addCleanup(manager.close, drain=lambda: None)
        self.connect.assert_not_called()
        self.assertEqual(len(sdk.init_calls), 1)

    def test_decode_does_not_retry_sdk_errors_after_store_is_reachable(self):
        """Do not turn configuration or device initialization errors into waits."""
        sdk = FakePairBM(init_status=-7)
        with self.assertRaisesRegex(RuntimeError, "BM initialize failed.*-7"):
            self.start(sdk, role="decode")
        self.connect.assert_called_once()
        self.assertEqual(len(sdk.init_calls), 1)
        self.assertEqual(sdk.uninit_calls, 0)

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
                    self.assertEqual(sdk.handle.options["flags"], 0)
                    self.assertGreater(
                        manager.view(1 - expected_rank, 0).device_base, 0
                    )
                    manager.close(drain=lambda: None)
                    self.assertTrue(sdk.handle.destroyed)
                    self.assertEqual(sdk.uninit_calls, 1)

    def test_numa_binding_cycles_selected_nodes_on_each_side(self):
        """Use the ordered list and TP rank, independent of the NPU ID and BM rank."""
        for nodes, online, expected_flags in (
            ("0,2,4,6", "0-7\n", [128, 130, 132, 134] * 4),
            (" 6, 0 ,4 ", "0,2,4,6\n", [134, 128, 132] * 5 + [134]),
            ("3", "0-3,6-7\n", [131] * 16),
            ("126", "0-7,126\n", [254] * 16),
        ):
            os.environ[NUMA_NODE_ENV] = nodes
            self.read_topology.return_value = online
            for tp_rank, flags in enumerate(expected_flags):
                for role, bm_rank in (("prefill", 0), ("decode", 1)):
                    with self.subTest(nodes=nodes, tp_rank=tp_rank, role=role):
                        sdk = FakePairBM()
                        manager = self.start(
                            sdk, tp_rank=tp_rank, role=role, device_id=5
                        )
                        try:
                            self.assertEqual(sdk.handle.options["flags"], flags)
                            self.assertEqual(manager.rank, bm_rank)
                            self.assertEqual(sdk.init_calls[0][2], 5)
                            self.assertEqual(
                                sdk.handle.options["local_dram_size"],
                                self.layout.contribution_bytes(bm_rank),
                            )
                        finally:
                            manager.close(drain=lambda: None)

    def test_invalid_numa_list_warns_and_allocates_with_default_flags(self):
        """A bad list falls back as a whole before attempting a native allocation."""
        for nodes, online in (
            ("", "0-7"),
            (" ", "0-7"),
            ("-1", "0-7"),
            ("127", "0-127"),
            ("128", "0-128"),
            ("1.5", "0-7"),
            ("eight", "0-7"),
            ("0,", "0-7"),
            ("0,,2", "0-7"),
            ("0,0,2", "0-7"),
            ("0-6", "0-7"),
            ("0,2,4,8", "0-7"),
            ("0,1", "0,2,4,6"),
        ):
            with self.subTest(nodes=nodes, online=online):
                os.environ[NUMA_NODE_ENV] = nodes
                self.read_topology.return_value = online
                sdk = FakePairBM()
                with (
                    self.assertLogs("ascend_mempool.manager", level="WARNING") as logs,
                    patch.object(sdk, "create2", wraps=sdk.create2) as create,
                ):
                    manager = self.start(sdk, tp_rank=0)
                    try:
                        create.assert_called_once()
                        self.assertEqual(create.call_args.kwargs["flags"], 0)
                        self.assertEqual(sdk.handle.options["flags"], 0)
                    finally:
                        manager.close(drain=lambda: None)
                self.assertIn(NUMA_NODE_ENV, logs.output[0])
                self.assertIn("falling back to default BM allocation", logs.output[0])
                self.assertEqual(sdk.uninit_calls, 1)

    def test_nonexistent_node_warning_identifies_requested_and_available_nodes(self):
        """Report the offending node even when this TP rank would select a valid one."""
        os.environ[NUMA_NODE_ENV] = "0,8"
        sdk = FakePairBM()
        with self.assertLogs("ascend_mempool.manager", level="WARNING") as logs:
            manager = self.start(sdk, tp_rank=0)
            try:
                self.assertEqual(sdk.handle.options["flags"], 0)
            finally:
                manager.close(drain=lambda: None)
        self.assertIn("[8]", logs.output[0])
        self.assertIn("online nodes: 0-7", logs.output[0])

    def test_unavailable_or_invalid_topology_warns_and_uses_default(self):
        """Do not guess valid node IDs if local sysfs cannot be read or parsed."""
        os.environ[NUMA_NODE_ENV] = "0,2"
        for failure in (
            FileNotFoundError("missing sysfs"),
            PermissionError("sysfs"),
            "",
            "0-",
            "3-1",
            "0,,2",
        ):
            with self.subTest(failure=failure):
                self.read_topology.side_effect = (
                    failure if isinstance(failure, OSError) else None
                )
                self.read_topology.return_value = failure
                sdk = FakePairBM()
                with self.assertLogs("ascend_mempool.manager", level="WARNING"):
                    manager = self.start(sdk)
                    try:
                        self.assertEqual(sdk.handle.options["flags"], 0)
                    finally:
                        manager.close(drain=lambda: None)

    def test_unset_and_removed_count_use_default_without_reading_topology(self):
        """The removed COUNT variable must not silently reactivate explicit binding."""
        for old_count in (None, "8"):
            with self.subTest(old_count=old_count):
                if old_count is not None:
                    os.environ["SGLANG_NPU_MEMPOOL_LOCAL_NUMA_NODE_COUNT"] = old_count
                sdk = FakeBM(1)
                manager = MempoolKVManager.create(self.layout, 1, bm_module=sdk)
                try:
                    self.assertEqual(sdk.handle.options["flags"], 0)
                    self.read_topology.assert_not_called()
                finally:
                    manager.close(drain=lambda: None)

    def test_direct_create_requires_tp_rank_for_numa_binding(self):
        """Never substitute P/D BM rank for an absent or invalid TP rank."""
        os.environ[NUMA_NODE_ENV] = "0,2,4,6"
        for tp_rank in (None, -1, True, 1.5):
            with self.subTest(tp_rank=tp_rank):
                sdk = FakeBM(1)
                with self.assertRaisesRegex(ValueError, "requires.*tp_rank"):
                    MempoolKVManager.create(
                        self.layout, 1, bm_module=sdk, tp_rank=tp_rank
                    )
                self.assertIsNone(sdk.handle)

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
