"""Validate sharing geometry, whole-run holding and the no-model worker path."""

import concurrent.futures
import io
import json
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from copy import deepcopy
from pathlib import Path
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))

import run_bm_shared_startup as runner
import sharing_worker
from sharing_startup import check_reports, wait_for_release, wait_for_workers
from test_sharing_pool import GroupSDK

from ascend_mempool.sharing import SharingPlan, port_plan
from ascend_mempool.sharing_control import Cohort
from ascend_mempool.sharing_pool import SharingPool


def arguments(*extra):
    return runner.parse_args(
        [
            "--role",
            "prefill",
            "--prefill-host",
            "p",
            "--decode-host",
            "d",
            "--run-id",
            "shared96g",
            *extra,
        ]
    )


def reports_for(plan, side):
    reports = []
    for group in plan["groups"]:
        for member, device in enumerate(group["devices"]):
            reports.append(
                dict(
                    status="startup_passed",
                    closed=True,
                    cleanup_phase="closed",
                    startup=dict(
                        probes_verified=True, all_pools_live=True, participants=4
                    ),
                    config=dict(
                        plan=plan["plan"],
                        mode="shared",
                        side=side,
                        member=member,
                        device_id=device,
                        numa_node=group["numa_node"],
                    ),
                    mapping=dict(
                        tp_size=2,
                        participant=side * 2 + member,
                        logical_side=side,
                        member=member,
                        local_dram_bytes=plan["contributions"][side * 2 + member],
                        rank_stride_bytes=plan["bytes_per_numa"],
                        requested_numa_node=group["numa_node"],
                        owners={"0": {"gva": "0x100"}, "1": {"gva": "0x200"}},
                        numa=dict(
                            placement_verified=False,
                            status="unobservable_driver_mapping",
                        ),
                    ),
                )
            )
    return reports


class TestSharedStartup(unittest.TestCase):
    def test_all_nodes_share_one_96g_owner_per_side_and_disjoint_ports(self):
        plan = runner.describe(arguments())
        self.assertEqual(plan["bytes_per_host"], 768 << 30)
        self.assertEqual(plan["contributions"], [96 << 30, 0, 96 << 30, 0])
        occupied = {plan["control_port"]}
        for node, group in enumerate(plan["groups"]):
            self.assertEqual(group["devices"], [2 * node, 2 * node + 1])
            ports = [
                port_plan(group["control_port"], "shared", side, member)
                for side in (0, 1)
                for member in (0, 1)
            ]
            used = {ports[0]["store"], ports[0]["coordinator"], ports[0]["collective"]}
            used.update(port["nic_actual"] for port in ports)
            self.assertEqual(len(used), 7)
            self.assertTrue(used.isdisjoint(occupied))
            occupied.update(used)

    def test_single_node_keeps_contiguous_device_pair(self):
        plan = runner.describe(arguments("--numa-nodes", "7"))
        self.assertEqual(plan["bytes_per_host"], 96 << 30)
        self.assertEqual(plan["groups"][0]["devices"], [14, 15])

    def test_bad_configuration_fails_before_hardware(self):
        for extra in (
            ("--numa-nodes", "0", "0"),
            ("--numa-nodes", "8"),
            ("--control-port", "65500"),
            ("--control-port", "0"),
            ("--timeout", "nan"),
            ("--timeout", "0"),
            ("--run-id", "../old-run"),
            ("--decode-host", "p"),
        ):
            with self.subTest(extra=extra), redirect_stderr(io.StringIO()):
                with self.assertRaises(SystemExit):
                    arguments(*extra)

    def test_actual_sdk_arguments_and_large_owner_addresses(self):
        plan = SharingPlan(**runner.describe(arguments())["plan"])
        for side in (0, 1):
            for member in (0, 1):
                sdk = GroupSDK(side * 2 + member)
                with patch.object(Path, "read_text", return_value="0-7"):
                    pool = SharingPool(plan, "shared", side, member, sdk, numa_node=7)
                try:
                    pool.join(timeout=0.1)
                    self.assertEqual(sdk.handle.options["flags"], 0x87)
                    self.assertEqual(
                        sdk.handle.options["local_dram_size"], 0 if member else 96 << 30
                    )
                    self.assertEqual(sdk.handle.options["max_dram_size"], 96 << 30)
                    self.assertEqual(pool.world_size, 4)
                    self.assertEqual(pool.bases(1)[0] - pool.bases(0)[0], 192 << 30)
                    self.assertEqual(pool.layout.probe_bytes, 256)
                finally:
                    pool.close(drain=lambda: None)

    def test_reports_preserve_unobservable_numa_as_unverified(self):
        plan = runner.describe(arguments())
        for side in (0, 1):
            result = check_reports(reports_for(plan, side), plan, side)
            self.assertEqual(result["status"], "startup_passed")
            self.assertEqual(result["bytes_per_host"], 768 << 30)
            self.assertFalse(result["numa_placement_verified"])
            self.assertEqual(
                result["full_content_graph_performance_acceptance"], "not run"
            )

    def test_missing_duplicate_independent_or_unfinished_runs_fail(self):
        plan = runner.describe(arguments("--numa-nodes", "0"))
        original = reports_for(plan, 0)
        for mutation in (
            "missing",
            "duplicate",
            "follower",
            "capacity",
            "node",
            "probe",
            "hold",
            "close",
            "cleanup",
            "placement",
        ):
            reports = deepcopy(original)
            if mutation == "missing":
                reports.pop()
            elif mutation == "duplicate":
                reports[1] = reports[0]
            elif mutation == "follower":
                reports[1]["mapping"]["local_dram_bytes"] = 96 << 30
            elif mutation == "capacity":
                reports[0]["mapping"]["local_dram_bytes"] = 48 << 30
            elif mutation == "node":
                reports[0]["mapping"]["requested_numa_node"] = 1
            elif mutation == "probe":
                reports[0]["startup"]["probes_verified"] = False
            elif mutation == "hold":
                reports[0]["startup"]["all_pools_live"] = False
            elif mutation == "close":
                reports[0]["closed"] = False
            elif mutation == "cleanup":
                reports[0]["cleanup_phase"] = "waiting-all-pools-closed"
            else:
                reports[0]["mapping"]["numa"]["status"] = "mismatch"
            with self.subTest(mutation=mutation), self.assertRaises(RuntimeError):
                check_reports(reports, plan, 0)

    def test_ready_workers_keep_pools_until_parent_release(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            outputs = [root / "device-0", root / "device-1"]
            for out in outputs:
                out.mkdir()
            release = root / "release"
            with concurrent.futures.ThreadPoolExecutor(2) as threads:
                futures = [
                    threads.submit(wait_for_release, out, release, 3) for out in outputs
                ]
                try:
                    wait_for_workers(outputs, 2, lambda: None)
                    self.assertTrue(all(not future.done() for future in futures))
                finally:
                    release.touch()
                for future in futures:
                    future.result(timeout=2)

    def test_missing_worker_or_missing_parent_release_times_out(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with self.assertRaises(TimeoutError):
                wait_for_workers([root], 0, lambda: None)
            with self.assertRaises(TimeoutError):
                wait_for_release(root, root / "release", 0)
            with self.assertRaisesRegex(RuntimeError, "worker failed"):
                wait_for_workers(
                    [root], 1, Mock(side_effect=RuntimeError("worker failed"))
                )

    def test_startup_worker_never_stages_96g_or_runs_compute_benchmarks(self):
        plan = runner.describe(arguments("--numa-nodes", "0"))
        source = reports_for(plan, 0)[0]
        config = dict(
            source["config"],
            prefill_host="p",
            decode_host="d",
            control_port=20032,
            identity="test",
            timeout=1,
            smoke=True,
            startup_only=True,
            startup_release="unused",
        )
        pool = Mock(
            plan=SharingPlan(**plan["plan"]),
            mode="shared",
            member=0,
            world_size=4,
            events=[],
        )
        pool.report.return_value = source["mapping"]
        environment = dict(memfabric="v", torch="v", torch_npu="v", sgl_kernel_npu="v")
        torch, mf = Mock(), Mock()
        mf.initialize.return_value = mf.bm.initialize.return_value = 0
        mf.bm.bm_rank_id.return_value = 0
        cohort = Mock()

        def exchange(phase, **payload):
            if phase == "allocations":
                return [
                    dict(bytes=size, numa_verified=False)
                    for size in plan["contributions"]
                ]
            return [payload] * 4

        cohort.exchange.side_effect = exchange
        cohort.shutdown.side_effect = lambda **kw: Cohort.shutdown(cohort, **kw)
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp)
            with (
                patch.object(
                    sharing_worker.os,
                    "sched_getaffinity",
                    return_value={0},
                    create=True,
                ),
                patch.object(sharing_worker.os, "sched_setaffinity", create=True),
                patch.object(Path, "read_text", return_value="0"),
                patch.object(
                    sharing_worker,
                    "load_runtime",
                    return_value=(torch, mf, environment),
                ),
                patch.object(sharing_worker, "SharingPool", return_value=pool) as cls,
                patch.object(sharing_worker, "Cohort", return_value=cohort),
                patch.object(sharing_worker, "wait_for_release") as hold,
                patch.object(sharing_worker, "stage_local_kv") as stage,
                patch.object(sharing_worker, "benchmark") as benchmark,
                patch.object(sharing_worker, "delayed_publication") as publication,
                redirect_stdout(io.StringIO()),
            ):
                cls.check.side_effect = SharingPool.check
                self.assertEqual(sharing_worker.run(config, output), 0)
            result = json.loads((output / "report.json").read_text())
        self.assertEqual(result["status"], "startup_passed")
        self.assertEqual(result["cleanup_phase"], "closed")
        self.assertTrue(result["closed"])
        hold.assert_called_once()
        pool.verify_probe.assert_called_once_with(torch)
        pool.rejoin_follower.assert_not_called()
        stage.assert_not_called()
        benchmark.assert_not_called()
        publication.assert_not_called()
        torch.distributed.init_process_group.assert_not_called()
        mf.bm.uninitialize.assert_called_once()


if __name__ == "__main__":
    unittest.main()
