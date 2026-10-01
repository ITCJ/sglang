"""Check that the allocation diagnostic retains concurrent pools and isolates pairs."""

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


if __name__ == "__main__":
    unittest.main()
