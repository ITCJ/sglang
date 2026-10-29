"""Check the self-running K1 acceptance boundary without loading an NPU model."""

import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))
import run_mempool_min as runner

from ascend_mempool_pd.mempool_router_protocol import RouterConfig


class TestNumaRunner(unittest.TestCase):
    def test_k1_mode_is_selectable_and_explicit_map_replaces_legacy_environment(self):
        args = runner.parse_args(
            [
                "--role",
                "prefill",
                "--mode",
                "numa-acceptance",
                "--config",
                "/tmp/config.json",
                "--run-id",
                "k1",
            ]
        )
        args.rank_numa_map = Path("/tmp/p-map.json")
        config = RouterConfig("p", "d", "/model")
        with patch.dict("os.environ", {"SGLANG_NPU_MEMPOOL_LOCAL_NUMA_NODE": "0,2"}):
            env = runner.service_environment(
                config, args, Path("/tmp/log"), target=True
            )
        self.assertEqual(env["MEMPOOL_RANK_NUMA_MAP"], "/tmp/p-map.json")
        self.assertNotIn("SGLANG_NPU_MEMPOOL_LOCAL_NUMA_NODE", env)
        ref = runner.service_environment(config, args, Path("/tmp/log"), target=False)
        self.assertNotIn("MEMPOOL_RANK_NUMA_MAP", ref)

    def test_complete_report_keeps_applied_separate_from_missing_physical_evidence(
        self,
    ):
        from mempool_numa_runner import placement_report

        config = RouterConfig("p", "d", "/model", prefix_pages=40)
        maps, registrations = {}, {}
        for role, host, size in (("prefill", "p", 13 * 2**30), ("decode", "d", 2**30)):
            maps[role] = [dict(host=host, rank=r, node=r // 2) for r in range(16)]
            registrations[role] = dict(
                ranks=[
                    dict(
                        rank=r,
                        host=host,
                        numa_node=r // 2,
                        owner_tp_rank=r,
                        numa_evidence=dict(placement_verified=False, resident_bytes={}),
                        numa_placement_verified=False,
                        local_dram_bytes=size,
                    )
                    for r in range(16)
                ]
            )
        report = placement_report(config, maps, registrations)
        self.assertEqual(report["status"], "failed")
        for side in report["roles"].values():
            self.assertEqual(side["plan"]["allocation_status"], "applied")
            self.assertEqual(side["unverified_ranks"], list(range(16)))
