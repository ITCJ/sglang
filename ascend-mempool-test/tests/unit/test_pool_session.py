"""Serve through planned BM sessions, observing the external SDK boundary."""

import unittest
from unittest.mock import patch

from test_pair_startup import FakePairBM
from test_sharing_pool import GroupHandle, GroupSDK

from ascend_mempool.layout import KVLayout, PoolLayout
from ascend_mempool.manager import MempoolKVManager
from ascend_mempool.sharing_plan import HostTopology, MempoolNumaConfig


class TestPoolSession(unittest.TestCase):
    def test_prefill_group_shares_main_but_keeps_index_prefix_and_decode_private(self):
        layout = PoolLayout(
            KVLayout(2, 2, 128, 1, 8),
            KVLayout(2, 2, 16, 1, 8),
            alignment_bytes=64,
            prompt_index=KVLayout(1, 2, 128, 1, 4),
            prefix_main=KVLayout(2, 3, 128, 1, 8),
            prefix_index=KVLayout(1, 3, 128, 1, 4),
        )
        pools = []
        for side, role in enumerate(("prefill", "decode")):
            plan = MempoolNumaConfig(False, "0").plan(
                hosts=(HostTopology(role, (0, 1), "0"),),
                tp_size=2,
                role=role,
                instance_id="p-shared",
                kv_identity="model",
                layout=layout,
                prefill_shared=True,
            )
            for rank in (0, 1):
                session = plan.session(rank)
                sdk = GroupSDK(session.participant)
                pool = MempoolKVManager.create(
                    session.layout, side, session=session, bm_module=sdk
                )
                self.addCleanup(pool.close, drain=lambda: None)
                pool.join(0.1)
                pools.append(pool)
        self.assertEqual([p.contribution for p in pools], [25728, 17536, 1152, 1152])
        self.assertEqual([p.view(0, 0).gva_base for p in pools], [0x1000000000] * 4)
        self.assertNotEqual(pools[2].view(1, 0).gva_base, pools[3].view(1, 0).gva_base)
        for region in ("alive_index", "prefix_main", "prefix_index"):
            self.assertNotEqual(
                pools[0].view(0, 0, region=region).gva_base,
                pools[1].view(0, 0, region=region).gva_base,
            )
        self.assertEqual(
            pools[1].replica_registration()["alive_main"]["members"], [0, 1]
        )
        self.assertEqual(pools[1].replica_registration()["alive_index"]["members"], [1])
        self.assertEqual(pools[3].replica_registration()["alive_main"]["members"], [1])

    def test_group_follower_uses_owner_mappings_with_zero_contribution(self):
        session = self.plan(role="decode", share=True).session(3)
        sdk = FakePairBM()

        def create(**options):
            sdk.handle = GroupHandle(sdk.rank, options)
            return sdk.handle

        sdk.create2 = create
        with patch("socket.create_connection"):
            pool = MempoolKVManager.initialize_session(
                session=session,
                store_host="host-p",
                nic_host="host-d",
                device_id=3,
                bm_module=sdk,
                timeout=0.1,
            )
        self.addCleanup(pool.close, drain=lambda: None)
        self.assertEqual(sdk.init_calls[-1][1], 4)
        self.assertEqual(sdk.init_calls[-1][3].rank_id, 3)
        self.assertEqual(sdk.handle.options["local_dram_size"], 0)
        self.assertEqual(pool.view(1, 0).gva_base, 0x1080000000)
        self.assertEqual(pool.replica_registration()["alive_main"]["members"], [2, 3])
        self.assertEqual(pool.replica_registration()["alive_main"]["owner"], 2)

    def plan(self, role="prefill", share=False):
        layer = KVLayout(layers=2, slots=16, tokens=128, heads=1, dim=8)
        return MempoolNumaConfig(share, "0,1").plan(
            hosts=(HostTopology("host-p", tuple(range(4)), "0-3"),),
            tp_size=4,
            role=role,
            instance_id="test-session",
            kv_identity="test-model",
            layout=PoolLayout(layer, layer),
            base_port=19000,
            nic_port=25670,
        )

    def test_private_session_uses_planned_ports_and_can_restart_after_drain(self):
        session = self.plan().session(3)
        sdk = FakePairBM()
        for host in ("host-p", "::1"):
            manager = MempoolKVManager.initialize_session(
                session=session,
                store_host="host-p",
                nic_host=host,
                device_id=3,
                bm_module=sdk,
                timeout=0.1,
            )
            self.addCleanup(manager.close, drain=lambda: None)
            view = manager.view(1, 0)
            self.assertEqual(view.device_base, 0x7000000000)
            self.assertEqual(sdk.init_calls[-1][:3], ("tcp://host-p:19003", 2, 3))
            self.assertEqual(
                sdk.init_calls[-1][3].nic_url,
                f"tcp://{'[::1]' if host == '::1' else host}:25676",
            )
            self.assertEqual(sdk.handle.options["flags"], 0x81)
            with self.assertRaisesRegex(RuntimeError, "still reading"):
                manager.close(
                    drain=lambda: (_ for _ in ()).throw(RuntimeError("still reading"))
                )
            self.assertEqual(view.device_base, 0x7000000000)
            manager.close(drain=lambda: None)
            self.assertTrue(sdk.handle.destroyed)
            self.assertFalse(sdk.initialized)
            with self.assertRaisesRegex(RuntimeError, "closed"):
                _ = view.device_base


if __name__ == "__main__":
    unittest.main()
