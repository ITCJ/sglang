"""Check that the allocation diagnostic retains concurrent pools and isolates pairs."""

import json
import os
import shlex
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

SCRIPTS = Path(__file__).resolve().parents[2] / "scripts"
sys.path.insert(0, str(SCRIPTS))
import check_bm_even_numa as even_numa
import check_bm_numa as numa
import verify_bm_startup as startup


class TestStartupHold(unittest.TestCase):
    """Missing workers or failed probes must not satisfy the pool coexistence check."""

    def test_waits_for_the_other_local_pool(self):
        """The first allocation stays alive until the other worker reports ready."""
        with tempfile.TemporaryDirectory() as directory:
            ready = Path(directory)

            def publish_peer(_delay):
                self.assertTrue((ready / "device-0.ready").exists())
                (ready / "device-1.ready").touch()

            with patch.object(startup.time, "sleep", side_effect=publish_peer) as sleep:
                startup.hold_local_pools(ready, [0, 1], 0, 1)
            sleep.assert_called_once()

    def test_missing_pool_times_out(self):
        """One successful worker cannot make a multi-device run pass on its own."""
        with tempfile.TemporaryDirectory() as directory:
            with patch.object(startup.time, "monotonic", side_effect=[0, 0, 2]):
                with patch.object(startup.time, "sleep"):
                    with self.assertRaisesRegex(TimeoutError, "devices=\\[1\\]"):
                        startup.hold_local_pools(Path(directory), [0, 1], 0, 1)

    def test_reused_worker_marker_is_rejected(self):
        """A repeated invocation cannot use its own stale success marker."""
        with tempfile.TemporaryDirectory() as directory:
            ready = Path(directory)
            startup.hold_local_pools(ready, [0], 0, 1)
            with self.assertRaises(FileExistsError):
                startup.hold_local_pools(ready, [0], 0, 1)

    def test_failed_probe_never_marks_the_pool_ready(self):
        """Remote data must pass verification before it counts towards the barrier."""
        with tempfile.TemporaryDirectory() as directory:
            manager = Mock()
            manager.layout.probe_bytes = 64
            manager.verify_local_probe.side_effect = RuntimeError("wrong marker")
            args = SimpleNamespace(
                rank=1,
                device_id=0,
                local_ready_dir=Path(directory),
                local_devices=[0, 1],
                timeout=1,
            )
            with self.assertRaisesRegex(RuntimeError, "wrong marker"):
                startup.verify_probe_and_hold(manager, args)
            self.assertEqual(list(Path(directory).iterdir()), [])


class TestStartupLauncher(unittest.TestCase):
    """Validate real launcher arguments without importing an NPU runtime."""

    def test_sixteen_pairs_have_distinct_ports_and_one_shared_local_barrier(self):
        """Each worker gets its own store/control/NIC and holds the same device set."""
        with tempfile.TemporaryDirectory() as directory:
            for rank in (0, 1):
                local_ip = f"10.120.72.{31 + rank}"
                result = subprocess.run(
                    [
                        "bash",
                        str(SCRIPTS / "run_bm_startup_gate.sh"),
                        str(rank),
                        "10.120.72.31",
                        local_ip,
                        directory,
                    ],
                    env={
                        **os.environ,
                        "MEMPOOL_TEST_PYTHON": sys.executable,
                        "MEMPOOL_TEST_DRY_RUN": "1",
                        "MEMPOOL_TEST_DEVICES": " ".join(map(str, range(16))),
                    },
                    text=True,
                    capture_output=True,
                    check=True,
                )
                commands = [
                    shlex.split(line)
                    for line in result.stdout.splitlines()
                    if "--store-port" in line
                ]
                self.assertEqual(len(commands), 16)
                ports = set()
                ready_dirs = set()
                for device, command in enumerate(commands):

                    def value(option):
                        return command[command.index(option) + 1]

                    self.assertEqual(value("--rank"), str(rank))
                    self.assertEqual(value("--device-id"), str(device))
                    self.assertEqual(value("--head-ip"), "10.120.72.31")
                    self.assertEqual(value("--store-port"), str(18773 + 2 * device))
                    self.assertEqual(value("--control-port"), str(18774 + 2 * device))
                    self.assertEqual(
                        value("--nic-url"), f"tcp://{local_ip}:{25670 + 2 * device}"
                    )
                    ports.update([value("--store-port"), value("--control-port")])
                    ready_dirs.add(value("--local-ready-dir"))
                    start = command.index("--local-devices") + 1
                    self.assertEqual(
                        command[start : start + 16], list(map(str, range(16)))
                    )
                self.assertEqual(len(ports), 32)
                self.assertEqual(len(ready_dirs), 1)
                self.assertNotIn("ALL_BM_STARTUP_CHECKS_PASSED", result.stdout)

    def test_even_numa_profile_launches_sixteen_one_gib_pools_on_each_side(self):
        """The fixed profile overrides an old device subset and uses service sizes."""
        with tempfile.TemporaryDirectory() as directory:
            for rank in (0, 1):
                result = subprocess.run(
                    [
                        "bash",
                        str(SCRIPTS / "run_bm_startup_gate.sh"),
                        "--even-numa",
                        str(rank),
                        "10.120.72.31",
                        f"10.120.72.{31 + rank}",
                        directory,
                    ],
                    env={
                        **os.environ,
                        "MEMPOOL_TEST_PYTHON": sys.executable,
                        "MEMPOOL_TEST_DRY_RUN": "1",
                        "MEMPOOL_TEST_DEVICES": "0",
                        "SGLANG_NPU_MEMPOOL_LOCAL_NUMA_NODE": "0",
                    },
                    text=True,
                    capture_output=True,
                    check=True,
                )
                commands = [
                    shlex.split(line)
                    for line in result.stdout.splitlines()
                    if "--store-port" in line
                ]
                self.assertEqual(len(commands), 16)
                self.assertIn("pools-per-node=4", result.stdout)
                self.assertIn("per-device=1 GiB", result.stdout)
                for device, command in enumerate(commands):
                    args = startup.parse_args(
                        command[3 : command.index("--local-ready-dir")]
                    )
                    self.assertEqual(args.device_id, device)
                    self.assertEqual(args.rank, rank)
                    self.assertEqual(args.layers, 78)
                    self.assertEqual((args.s_p, args.s_d), (512, 512))
                    self.assertEqual(
                        startup.make_layout(args).signature()["contributions"],
                        [1 << 30, 1 << 30],
                    )
                self.assertNotIn("ALL_EVEN_NUMA_CHECKS_PASSED", result.stdout)

    def test_all_numa_profile_launches_48_gib_per_rank_on_each_side(self):
        """The requested 96 GiB per node is split across two adjacent ranks."""
        with tempfile.TemporaryDirectory() as directory:
            for rank, local_ip in ((0, "10.120.72.23"), (1, "10.120.72.25")):
                result = subprocess.run(
                    [
                        "bash",
                        str(SCRIPTS / "run_bm_startup_gate.sh"),
                        "--all-numa",
                        str(rank),
                        "10.120.72.23",
                        local_ip,
                        directory,
                    ],
                    env={
                        **os.environ,
                        "MEMPOOL_TEST_PYTHON": sys.executable,
                        "MEMPOOL_TEST_DRY_RUN": "1",
                        "MEMPOOL_TEST_DEVICES": "0",
                        "SGLANG_NPU_MEMPOOL_LOCAL_NUMA_NODE": "0,2,4,6",
                    },
                    text=True,
                    capture_output=True,
                    check=True,
                )
                commands = [
                    shlex.split(line)
                    for line in result.stdout.splitlines()
                    if "--store-port" in line
                ]
                self.assertEqual(len(commands), 16)
                for expected in (
                    "nodes=[0 1 2 3 4 5 6 7]",
                    "pools-per-node=2",
                    "GiB-per-node=96",
                    "total-local-GiB=768",
                    "per-device=48 GiB",
                ):
                    self.assertIn(expected, result.stdout)
                for device, command in enumerate(commands):
                    args = startup.parse_args(
                        command[3 : command.index("--local-ready-dir")]
                    )
                    self.assertEqual((args.rank, args.device_id), (rank, device))
                    self.assertEqual(args.head_ip, "10.120.72.23")
                    self.assertEqual(
                        args.nic_url, f"tcp://{local_ip}:{25670 + 2 * device}"
                    )
                    self.assertEqual(
                        startup.make_layout(args).signature()["contributions"],
                        [48 << 30, 48 << 30],
                    )
                self.assertNotIn("ALL_NUMA_CHECKS_PASSED", result.stdout)


class TestNumaReports(unittest.TestCase):
    """Native allocation evidence and clean exits must agree with the pool plan."""

    def write_reports(self, directory, rank=0, *, all_numa=False):
        """Build a synthetic completed run with the same log/report boundary."""
        nodes = (
            (0, 0, 1, 1, 2, 2, 3, 3, 4, 4, 5, 5, 6, 6, 7, 7)
            if all_numa
            else (0,) * 4 + (2,) * 4 + (4,) * 4 + (6,) * 4
        )
        node_list = "0,1,2,3,4,5,6,7" if all_numa else "0,2,4,6"
        contribution = (48 if all_numa else 1) << 30
        (directory / "ready").mkdir()
        (directory / "exits.tsv").write_text(
            "".join(f"{device}\t0\n" for device in range(16))
        )
        for device, node in enumerate(nodes):
            (directory / "ready" / f"device-{device}.ready").touch()
            report = dict(
                rank=rank,
                status="passed",
                layout=dict(contributions=[contribution, contribution]),
                checks=[
                    dict(
                        check="bm_peer_probe",
                        verified_bytes=64,
                        device_id=device,
                        local_devices=list(range(16)),
                    )
                ],
            )
            (directory / f"device-{device}.json").write_text(json.dumps(report))
            (directory / f"device-{device}.log").write_text(
                f"Creating mempool BM pool: tp_rank={device} "
                f"local_numa_nodes={node_list} numa_node={node} bm_flags={128 + node}\n"
                f"Try HalMemCreate ret:0 numa:{node} spend time:100 size:{contribution}\n"
                f"[BM_STARTUP] LOCAL_POOLS_READY rank={rank} device={device} "
                f"devices={list(range(16))}\n"
                f"[BM_STARTUP] PASSED rank={rank} device={device}\n"
            )

    def test_balanced_allocation_and_page_size_retry_pass(self):
        """A page-size fallback on the same even node is permitted on either side."""
        for rank in (0, 1):
            with tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                self.write_reports(root, rank)
                log = root / "device-0.log"
                log.write_text(
                    "Try HalMemCreate ret:6 numa:0 spend time:10 size:1073741824\n"
                    + log.read_text()
                )
                result = even_numa.check_reports(root, rank)
                self.assertEqual(result["status"], "passed", result["errors"])
                self.assertEqual(
                    result["successful_pools_by_numa"], {0: 4, 2: 4, 4: 4, 6: 4}
                )

    def test_all_numa_checker_reports_96_gib_per_node_on_both_sides(self):
        """The real CLI reports all eight nodes and the full 768 GiB per host."""
        for rank in (0, 1):
            with tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                self.write_reports(root, rank, all_numa=True)
                result = subprocess.run(
                    [
                        sys.executable,
                        str(SCRIPTS / "check_bm_numa.py"),
                        "--profile",
                        "all",
                        "--rank",
                        str(rank),
                        "--report-dir",
                        directory,
                    ],
                    text=True,
                    capture_output=True,
                    check=True,
                )
                self.assertIn("ALL_NUMA_CHECKS_PASSED", result.stdout)
                report = json.loads((root / "numa-summary.json").read_text())
                self.assertEqual(report["status"], "passed")
                self.assertEqual(report["local_dram_bytes_per_pool"], 48 << 30)
                self.assertEqual(report["expected_local_dram_bytes"], 768 << 30)
                self.assertEqual(
                    report["successful_pools_by_numa"],
                    {str(node): 2 for node in range(8)},
                )
                self.assertEqual(
                    report["successful_dram_bytes_by_numa"],
                    {str(node): 96 << 30 for node in range(8)},
                )

    def test_all_numa_checker_rejects_wrong_mapping_or_capacity(self):
        """Reject old profiles, round-robin requests, and per-rank size mistakes."""
        for defect in ("old_profile", "round_robin", "wrong_hal_size", "wrong_report"):
            with (
                self.subTest(defect=defect),
                tempfile.TemporaryDirectory() as directory,
            ):
                root = Path(directory)
                self.write_reports(root, all_numa=defect != "old_profile")
                if defect == "round_robin":
                    log = root / "device-1.log"
                    log.write_text(
                        log.read_text().replace("numa_node=0", "numa_node=1")
                    )
                elif defect == "wrong_hal_size":
                    log = root / "device-0.log"
                    log.write_text(
                        log.read_text().replace(f"size:{48 << 30}", f"size:{96 << 30}")
                    )
                elif defect == "wrong_report":
                    path = root / "device-0.json"
                    report = json.loads(path.read_text())
                    report["layout"]["contributions"] = [96 << 30, 96 << 30]
                    path.write_text(json.dumps(report))
                result = numa.check_reports(root, 0, "all")
                self.assertEqual(result["status"], "failed", result)

    def test_wrong_hal_node_size_or_failed_allocation_cannot_pass(self):
        """A successful Python report cannot hide wrong or missing HAL allocation."""
        for before, after in (
            ("numa:0", "numa:1"),
            ("numa:0", "numa:2"),
            ("ret:0", "ret:6"),
            ("size:1073741824", "size:2147483648"),
            ("Try HalMemCreate", "unrelated log"),
            ("LOCAL_POOLS_READY", "STILL_WAITING"),
            ("local_numa_nodes=0,2,4,6", "local_numa_nodes=default"),
            ("local_numa_nodes=0,2,4,6", "local_numa_nodes=0,6,4,2"),
        ):
            with self.subTest(change=after), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                self.write_reports(root)
                log = root / "device-0.log"
                log.write_text(log.read_text().replace(before, after))
                result = even_numa.check_reports(root, 0)
                self.assertEqual(result["status"], "failed")
                self.assertEqual(result["workers"][0]["status"], "failed")

    def test_native_abort_after_success_report_is_rejected(self):
        """Catch the observed SIGABRT during native destruction after RESULT."""
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.write_reports(root)
            exits = root / "exits.tsv"
            exits.write_text(exits.read_text().replace("9\t0\n", "9\t134\n"))
            result = even_numa.check_reports(root, 0)
            self.assertEqual(result["status"], "failed")
            self.assertIn("exit=134", result["workers"][9]["error"])

    def test_missing_worker_or_peer_probe_is_rejected(self):
        """All 16 reports and the remote readback are required for success."""
        for defect in ("missing_report", "failed_probe", "wrong_role"):
            with (
                self.subTest(defect=defect),
                tempfile.TemporaryDirectory() as directory,
            ):
                root = Path(directory)
                self.write_reports(root)
                report_path = root / "device-15.json"
                report = json.loads(report_path.read_text())
                if defect == "missing_report":
                    report_path.unlink()
                else:
                    if defect == "failed_probe":
                        report["checks"][0]["verified_bytes"] = 0
                    else:
                        report["rank"] = 1
                    report_path.write_text(json.dumps(report))
                result = even_numa.check_reports(root, 0)
                self.assertEqual(result["status"], "failed")
                self.assertEqual(result["workers"][15]["status"], "failed")


if __name__ == "__main__":
    unittest.main()
