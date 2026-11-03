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


def shared_registration(
    testcase, decode_shared=False, index_shared=False, prefix_shared=False
):
    config = RouterConfig(
        "p",
        "d",
        "/model",
        prefix_pages=40,
        prefill_shared=True,
        decode_shared=decode_shared,
        prefill_index_shared=index_shared,
        prefix_shared=prefix_shared,
    )
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
            decode_shared=decode_shared,
            prefill_index_shared=index_shared,
            prefix_shared=prefix_shared,
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
                storage_order="page_first",
                layout_signature=layout.signature(),
                replicas=config.replicas(role),
                replica_locations=manager.replica_registration(),
                local_dram_bytes=manager.contribution,
                numa_placement_verified=False,
            )
            if prefix_shared and side == 0:
                row.update(
                    prefix_region=session.identity, prefix_policy=config.prefix_policy()
                )
    return config, registrations


class TestSharedEvidence(unittest.TestCase):
    def test_page_first_registration_rejects_layout_signature_and_segment_drift(self):
        config, registrations = shared_registration(self, True, True, True)
        result = verify_mempool_shared.check_page_first_registration(
            registrations, config
        )
        self.assertEqual(result["page_bytes"]["prefill/alive_main"], 11501568)
        self.assertEqual(result["page_bytes"]["prefill/prefix_index"], 688128)
        bad = copy.deepcopy(registrations)
        bad["decode"]["ranks"][3]["layout_signature"]["layout"]["prompt"][
            "storage_order"
        ] = "layer_first"
        with self.assertRaisesRegex(ValueError, "signature"):
            verify_mempool_shared.check_page_first_registration(bad, config)
        bad = copy.deepcopy(registrations)
        bad["prefill"]["ranks"][1]["replica_locations"]["prefix_main"][
            "physical_layout"
        ]["slots"] = 40
        with self.assertRaisesRegex(ValueError, "dimensions"):
            verify_mempool_shared.check_page_first_registration(bad, config)

    def test_single_prefix_registration_checks_segments_and_zero_followers(self):
        config, registrations = shared_registration(self, True, True, True)
        result = verify_mempool_shared.check_registration(registrations, config)
        self.assertEqual(result["prefix_replicas"], 1)
        self.assertEqual(
            sum(n["allocated_bytes"] for n in result["per_numa"].values()), 96 * 2**30
        )
        for field, value in (("owner", 3), ("bytes", 1), ("numa_node", 7)):
            bad = copy.deepcopy(registrations)
            bad["prefill"]["ranks"][1]["replica_locations"]["prefix_main"]["segments"][
                1
            ][field] = value
            with self.assertRaisesRegex(ValueError, "segment"):
                verify_mempool_shared.check_registration(bad, config)

    def test_single_index_registration_rejects_a_second_owner_or_wrong_numa(self):
        config, registrations = shared_registration(
            self, decode_shared=True, index_shared=True
        )
        result = verify_mempool_shared.check_registration(registrations, config)
        self.assertEqual(result["index_replicas"], 1)
        self.assertEqual(
            sum(n["allocated_bytes"] for n in result["per_numa"].values()), 105 * 2**30
        )
        for key, value in (
            ("owner", 2),
            ("numa_node", 1),
            ("members", [0, 1]),
            ("owner_participant", 2),
        ):
            bad = copy.deepcopy(registrations)
            bad["prefill"]["ranks"][2]["replica_locations"]["alive_index"][key] = value
            with self.assertRaisesRegex(ValueError, "owner/member"):
                verify_mempool_shared.check_registration(bad, config)

    def test_decode_residence_is_not_inferred_from_prefill_residence(self):
        config, registrations = shared_registration(self, decode_shared=True)
        for row in registrations["prefill"]["ranks"]:
            row["numa_placement_verified"] = True
        report = verify_mempool_shared.check_registration(registrations, config)
        self.assertFalse(report["physical_placement_verified"])
        self.assertEqual(
            sum(n["allocated_bytes"] for n in report["decode_per_numa"].values()),
            8 * 2**30,
        )

    def test_decode_registration_and_receipts_reject_unproven_sharing(self):
        from verify_mempool_decode import check_completion, check_order

        config, registrations = shared_registration(self, decode_shared=True)
        report = verify_mempool_shared.check_registration(registrations, config)
        self.assertEqual(report["decode_main_replicas"], 8)
        self.assertEqual(
            sum(r["local_dram_bytes"] for r in registrations["decode"]["ranks"]),
            8 * 2**30,
        )
        data = dict(
            forwards=3,
            replay_forwards=3,
            layers=78,
            shared_decode=dict(
                assignment="features",
                member=1,
                members=2,
                verified=True,
                checked_rows=156,
                written_bytes=134784,
                current_rows=234,
                ordering="compute_stream_before_post_attention_tp_collective",
            ),
        )
        # Historical report checker only: current registration has no probes.
        for row in registrations["decode"]["ranks"]:
            row["shared_probe"] = dict(
                decode_order=dict(
                    status="passed",
                    engine="graph_replay",
                    negative_without_wait=True,
                    positive_with_wait=True,
                )
            )
        check_order(registrations)
        self.assertEqual(check_completion(data, 1, config)["steps"], 3)
        for key in ("checked_rows", "written_bytes"):
            bad = copy.deepcopy(data)
            bad["shared_decode"][key] -= 1
            with self.assertRaisesRegex(ValueError, "coverage"):
                check_completion(bad, 1, config)
        registrations["decode"]["ranks"][1]["shared_probe"]["decode_order"][
            "negative_without_wait"
        ] = False
        with self.assertRaisesRegex(ValueError, "Graph ordering"):
            check_order(registrations)

    def test_shared_registration_rejects_private_aliases(self):
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

    def test_session_startup_checker_selects_the_shared_registration_contract(self):
        import verify_mempool_session

        config, registrations = shared_registration(self)
        with self.assertRaisesRegex(ValueError, "share=0"):
            verify_mempool_session.check_registration(registrations)
        verify_mempool_session.check_registration(registrations, config=config)
        registrations["prefill"]["ranks"][0]["replicas"]["alive_main"] = 16
        with self.assertRaisesRegex(ValueError, "replica counts"):
            verify_mempool_session.check_registration(registrations, config=config)
