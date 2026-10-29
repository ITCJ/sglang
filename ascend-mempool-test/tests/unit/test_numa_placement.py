"""Observe placement through the pool and planner's public evidence interfaces."""

import unittest
from unittest.mock import patch

from test_pool import FakeBM

from ascend_mempool.layout import KVLayout, PoolLayout
from ascend_mempool.manager import MempoolKVManager
from ascend_mempool.sharing_plan import HostTopology, MempoolNumaConfig, RankNuma


class TestNumaPlacement(unittest.TestCase):
    def test_private_pool_uses_swapped_mapping_without_changing_capacity(self):
        layout = PoolLayout(KVLayout(1, 16, 128, 1, 576), KVLayout(1, 16, 128, 1, 576))
        config = MempoolNumaConfig(rank_map=(RankNuma("p", 0, 2), RankNuma("p", 1, 0)))
        with patch(
            "ascend_mempool.sharing_plan.local_online_nodes", return_value="0,2"
        ):
            sdk = FakeBM(0)
            manager = MempoolKVManager.create(
                layout, 0, bm_module=sdk, tp_rank=0, tp_size=2, numa=config, host="p"
            )
        self.assertEqual(sdk.handle.options["flags"], 130)
        self.assertEqual(sdk.handle.options["local_dram_size"], 2**30)
        self.assertEqual(manager.numa_node, 2)

    def test_applied_means_all_registered_owners_match_map_not_residency(self):
        layout = PoolLayout(KVLayout(1, 16, 128, 1, 576), KVLayout(1, 16, 128, 1, 576))
        plan = MempoolNumaConfig(
            rank_map=(RankNuma("p", 0, 2), RankNuma("p", 1, 0))
        ).plan(
            hosts=(HostTopology("p", (0, 1), "0,2"),),
            tp_size=2,
            role="prefill",
            instance_id="k1",
            kv_identity="glm",
            layout=layout,
        )
        registrations = [
            dict(rank=0, host="p", numa_node=2, owner_tp_rank=0),
            dict(rank=1, host="p", numa_node=0, owner_tp_rank=1),
        ]
        self.assertEqual(plan.report(registrations)["allocation_status"], "applied")
        self.assertFalse(
            any(r["placement_verified"] for r in plan.report(registrations)["ranks"])
        )
        for invalid in (
            registrations[:1],
            registrations + [registrations[0]],
            [dict(registrations[0], numa_node=0), registrations[1]],
            [dict(registrations[0], owner_tp_rank=1), registrations[1]],
        ):
            with (
                self.subTest(invalid=invalid),
                self.assertRaisesRegex(ValueError, "NUMA_REGISTRATION_MISMATCH"),
            ):
                plan.report(invalid)


class TestResidencyEvidence(unittest.TestCase):
    def test_samples_all_regions_and_counts_only_the_owned_mapping(self):
        import tempfile
        from pathlib import Path

        from ascend_mempool.placement import sample_pool_placement

        with tempfile.TemporaryDirectory() as directory:
            proc = Path(directory)
            (proc / "maps").write_text(
                "10000-14000 rw-s 0 00:00 0 /dev/davinci_manager\n20000-24000 rw-p 0 00:00 0\n"
            )
            (proc / "numa_maps").write_text(
                "10000 default N2=4 kernelpagesize_kB=4\n20000 default N0=4 kernelpagesize_kB=4\n"
            )
            queried = []

            def query(addresses):
                queried.extend(addresses)
                return [2] * len(addresses)

            evidence = sample_pool_placement(
                0x10000,
                16384,
                2,
                {"alive_main": (0, 8192), "prefix_index": (8192, 8192)},
                proc=proc,
                query_pages=query,
                page_size=4096,
            )
        self.assertTrue(evidence["placement_verified"])
        self.assertEqual(evidence["resident_bytes"], {2: 16384})
        self.assertEqual(set(queried), {0x10000, 0x11000, 0x12000, 0x13000})
        self.assertEqual(
            set(evidence["regions"]), {"alive_main", "prefix_index", "allocation"}
        )

    def test_driver_opaque_pages_wrong_node_and_partial_residency_never_pass(self):
        import tempfile
        from pathlib import Path

        from ascend_mempool.placement import sample_pool_placement

        cases = (
            ("10000 default file=/dev/davinci_manager\n", [-14] * 4),
            ("10000 default N2=4 kernelpagesize_kB=4\n", [2, 0, 2, 2]),
            ("10000 default N2=3 kernelpagesize_kB=4\n", [2] * 4),
            ("10000 default N2=4 kernelpagesize_kB=4\n", PermissionError(1, "denied")),
        )
        with tempfile.TemporaryDirectory() as directory:
            proc = Path(directory)
            (proc / "maps").write_text(
                "10000-14000 rw-s 0 00:00 0 /dev/davinci_manager\n"
            )
            for numa_row, statuses in cases:
                (proc / "numa_maps").write_text(numa_row)

                def query(addresses):
                    if isinstance(statuses, Exception):
                        raise statuses
                    return statuses

                result = sample_pool_placement(
                    0x10000,
                    16384,
                    2,
                    {"alive_main": (0, 16384)},
                    proc=proc,
                    query_pages=query,
                    page_size=4096,
                )
                with self.subTest(statuses=statuses):
                    self.assertFalse(result["placement_verified"])
