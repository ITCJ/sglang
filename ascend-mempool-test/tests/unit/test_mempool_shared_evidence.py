"""Reject sharing claims that lack the required ownership and hardware receipts."""

import copy
import unittest

import verify_mempool_shared
from test_mempool_router import registration
from test_sharing_pool import GroupSDK

from ascend_mempool.config import MempoolConfig
from ascend_mempool.manager import MempoolKVManager
from ascend_mempool.sharing_plan import HostTopology, MempoolNumaConfig
from ascend_mempool_pd.mempool_router_protocol import RouterConfig


def shared_registration(testcase):
    config = RouterConfig("p", "d", "/model", prefix_pages=40, prefill_shared=True)
    layout = MempoolConfig(
        prefill_capacity=8192, decode_capacity=128, page_size=128
    ).make_mla_layout(
        num_layers=78,
        kv_lora_rank=512,
        qk_rope_head_dim=64,
        prefix_pages=40,
        indexer_layers=21,
        index_head_dim=128,
    )
    registrations = {role: registration(role) for role in ("prefill", "decode")}
    for side, role in enumerate(("prefill", "decode")):
        plan = MempoolNumaConfig(False, "0,1,2,3,4,5,6,7").plan(
            hosts=(HostTopology(role, tuple(range(16)), "0-7"),),
            tp_size=16,
            role=role,
            instance_id="shared",
            kv_identity="model",
            layout=layout,
            prefill_shared=True,
            prefill_groups=config.prefill_groups,
        )
        for row in registrations[role]["ranks"]:
            session = plan.session(row["rank"])
            manager = MempoolKVManager.create(
                session.layout,
                side,
                session=session,
                bm_module=GroupSDK(session.participant),
            )
            testcase.addCleanup(manager.close, drain=lambda: None)
            manager.join(0.1)
            row.update(
                replicas=config.replicas(role),
                replica_locations=manager.replica_registration(),
                local_dram_bytes=manager.contribution,
                numa_placement_verified=False,
                shared_probe=dict(
                    status="passed",
                    reader=row["rank"],
                    writers=list(session.members),
                    copy="unidex_copy",
                    source="shared_prompt",
                    bytes=2304,
                ),
            )
    return config, registrations


class TestSharedEvidence(unittest.TestCase):
    def test_shared_registration_rejects_private_aliases_and_missing_peer_reads(self):
        config, registrations = shared_registration(self)
        result = verify_mempool_shared.check_registration(registrations, config)
        self.assertEqual(result["main_replicas"], 8)
        self.assertFalse(result["physical_placement_verified"])
        self.assertEqual(
            sum(n["allocated_bytes"] for n in result["per_numa"].values()), 120 * 2**30
        )
        for region in ("alive_index", "prefix_main", "prefix_index"):
            bad = copy.deepcopy(registrations)
            bad["prefill"]["ranks"][1]["replica_locations"][region]["members"] = [0, 1]
            with self.assertRaisesRegex(ValueError, "owner/member"):
                verify_mempool_shared.check_registration(bad, config)
        bad = copy.deepcopy(registrations)
        bad["decode"]["ranks"][1]["shared_probe"]["writers"] = [1]
        with self.assertRaisesRegex(ValueError, "cross-writer"):
            verify_mempool_shared.check_registration(bad, config)

    def test_session_startup_checker_selects_the_shared_registration_contract(self):
        import verify_mempool_session

        config, registrations = shared_registration(self)
        with self.assertRaisesRegex(ValueError, "share=0"):
            verify_mempool_session.check_registration(registrations)
        verify_mempool_session.check_registration(registrations, config=config)
        registrations["prefill"]["ranks"][0]["replicas"]["alive_main"] = 16
        with self.assertRaisesRegex(ValueError, "replica counts"):
            verify_mempool_session.check_registration(registrations, config=config)
