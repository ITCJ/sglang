"""Serve through planned BM sessions, observing the external SDK boundary."""

import unittest
from unittest.mock import patch

from test_pair_startup import FakePairBM
from test_sharing_pool import GroupHandle, GroupSDK

from ascend_mempool.layout import KVLayout, PoolLayout
from ascend_mempool.manager import MempoolKVManager
from ascend_mempool.sharing_plan import HostTopology, MempoolNumaConfig


class TestPoolSession(unittest.TestCase):
    def test_shared_prefix_segments_have_one_owner_and_segment_layer_stride(self):
        layout = PoolLayout(
            KVLayout(2, 2, 128, 1, 8, page_size=128),
            KVLayout(2, 2, 128, 1, 8, page_size=128),
            alignment_bytes=64,
            prompt_index=KVLayout(1, 2, 128, 1, 4, page_size=128),
            prefix_main=KVLayout(2, 40, 128, 1, 8, page_size=128),
            prefix_index=KVLayout(1, 40, 128, 1, 4, page_size=128),
        )
        plans, pools = [], []
        for side, role in enumerate(("prefill", "decode")):
            plan = MempoolNumaConfig(False, "0,1,2,3,4,5,6,7").plan(
                hosts=(HostTopology(role, tuple(range(16)), "0-7"),),
                tp_size=16,
                role=role,
                instance_id="single-prefix",
                kv_identity="model",
                layout=layout,
                prefill_shared=True,
                decode_shared=True,
                prefill_index_shared=True,
                prefix_shared=True,
            )
            plans.append(plan)
            for rank in range(16):
                session = plan.session(rank)
                pool = MempoolKVManager.create(
                    session.layout,
                    side,
                    session=session,
                    bm_module=GroupSDK(session.participant),
                )
                self.addCleanup(pool.close, drain=lambda: None)
                pool.join(0.1)
                pools.append(pool)
                if rank % 2:
                    self.assertEqual(pool.contribution, 0)
        plans[0].validate_peer(plans[1])
        for side in (0, 1):
            self.assertEqual(len({p.probe_gva(side) for p in pools}), 16)
            self.assertEqual(len({p.view(side, 0).gva_base for p in pools}), 8)
        session = pools[0].session
        for region, total in (("prefix_main", 163840), ("prefix_index", 40960)):
            physical = [
                g
                for i in range(32)
                for n, (_, g) in session.regions(i).items()
                if n == region
            ]
            self.assertEqual(len(physical), 8)
            self.assertEqual(sum(g.total_bytes for g in physical), total)
            for segment in range(8):
                views = [p.view(0, 0, region=region, segment=segment) for p in pools]
                self.assertEqual(len({v.gva_base for v in views}), 1)
                self.assertEqual(views[0].layout.slots, 5)
                if region == "prefix_main":
                    next_layer = pools[0].view(0, 1, region=region, segment=segment)
                    self.assertEqual(next_layer.gva_base - views[0].gva_base, 2048)
            registration = pools[1].replica_registration()[region]
            self.assertEqual(registration["replica"], 0)
            self.assertEqual(len(registration["segments"]), 8)
            self.assertEqual(registration["mapping_members"], list(range(16)))

    def test_single_index_spans_prefill_ranks_without_merging_main_replicas(self):
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
            plan = MempoolNumaConfig(
                False, "0,1,2,3,4,5,6,7" if side == 0 else "7,6,5,4,3,2,1,0"
            ).plan(
                hosts=(HostTopology(role, tuple(range(16)), "0-7"),),
                tp_size=16,
                role=role,
                instance_id="single-index",
                kv_identity="model",
                layout=layout,
                prefill_shared=True,
                decode_shared=True,
                prefill_index_shared=True,
            )
            for rank in range(16):
                session = plan.session(rank)
                pool = MempoolKVManager.create(
                    session.layout,
                    side,
                    session=session,
                    bm_module=GroupSDK(session.participant),
                )
                self.addCleanup(pool.close, drain=lambda: None)
                pool.join(0.1)
                pools.append(pool)
        for side in (0, 1):
            self.assertEqual(len({p.view(side, 0).gva_base for p in pools}), 8)
        for region, count in (
            ("alive_index", 1),
            ("prefix_main", 16),
            ("prefix_index", 16),
        ):
            self.assertEqual(
                len({p.view(0, 0, region=region).gva_base for p in pools}), count
            )
        for rank, pool in enumerate(pools[:16]):
            registration = pool.replica_registration()
            self.assertEqual(registration["alive_index"]["owner"], 0)
            self.assertEqual(registration["alive_index"]["numa_node"], 0)
            self.assertEqual(registration["alive_index"]["members"], list(range(16)))
            self.assertEqual(registration["alive_main"]["owner"], rank // 2 * 2)
        self.assertEqual(len({p.probe_gva(1) for p in pools[:16]}), 16)
        self.assertEqual(
            sum("alive_index" in pools[0].session.regions(i) for i in range(32)), 1
        )

    def test_decode_shared_owner_allocates_once_with_private_or_shared_prefill(self):
        layer = KVLayout(2, 2, 16, 1, 8)
        layout = PoolLayout(layer, layer, alignment_bytes=64)
        for prefill_shared in (False, True):
            pools = []
            for side, role in enumerate(("prefill", "decode")):
                plan = MempoolNumaConfig(False, "0").plan(
                    hosts=(HostTopology(role, (0, 1), "0"),),
                    tp_size=2,
                    role=role,
                    instance_id="d-shared",
                    kv_identity="model",
                    layout=layout,
                    prefill_shared=prefill_shared,
                    decode_shared=True,
                )
                for rank in (0, 1):
                    session = plan.session(rank)
                    pool = MempoolKVManager.create(
                        session.layout,
                        side,
                        session=session,
                        bm_module=GroupSDK(session.participant),
                    )
                    self.addCleanup(pool.close, drain=lambda: None)
                    pool.join(0.1)
                    pools.append(pool)
            self.assertEqual([p.contribution for p in pools[2:]], [1152, 0])
            self.assertEqual(pools[2].view(1, 0).gva_base, pools[3].view(1, 0).gva_base)
            self.assertEqual(
                pools[0].view(0, 0).gva_base == pools[1].view(0, 0).gva_base,
                prefill_shared,
            )
            self.assertNotEqual(pools[2].probe_gva(1), pools[3].probe_gva(1))
            self.assertNotEqual(pools[0].probe_gva(0), pools[1].probe_gva(0))
            self.assertEqual(
                pools[3].replica_registration()["alive_main"]["members"], [0, 1]
            )
            self.assertEqual(pools[3].replica_registration()["alive_main"]["owner"], 0)

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
