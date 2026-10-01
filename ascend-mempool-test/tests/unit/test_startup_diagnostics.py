"""Exercise stall observation and container evidence without an NPU runtime."""

import os
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from test_pool import FakeBM

from ascend_mempool import diagnostics
from ascend_mempool.layout import KVLayout, PoolLayout
from ascend_mempool.pool import MempoolKVManager


class TestStartupDiagnostics(unittest.TestCase):
    def test_blocked_sdk_is_observed_and_failure_stops_the_monitor(self):
        """The sampler observes the SDK caller, then leaves the SDK error intact."""
        caller_tid = threading.get_native_id()
        caller_python_id = threading.get_ident()
        observed = threading.Event()
        failure = RuntimeError("allocation failed")
        samples = []
        sdk = FakeBM(rank=0)
        kv = KVLayout(layers=1, slots=16, tokens=8, heads=1, dim=8)

        def sample(context, tid, *, resources, python_thread=None):
            if python_thread is not None:
                samples.append((context, tid, python_thread, threading.get_native_id()))
                observed.set()

        def blocked_create(**options):
            self.assertTrue(observed.wait(2), "no observation while SDK was blocked")
            raise failure

        with (
            patch.dict(os.environ, SGLANG_NPU_MEMPOOL_DIAGNOSTICS="1"),
            patch.object(diagnostics, "_WAIT_SECONDS", 0.005),
            patch.object(diagnostics, "_sample", side_effect=sample),
            patch.object(sdk, "create2", side_effect=blocked_create),
            self.assertLogs(diagnostics.logger, level="INFO"),
        ):
            with self.assertRaises(RuntimeError) as raised:
                MempoolKVManager.create(PoolLayout(kv, kv), rank=0, bm_module=sdk)
        self.assertIs(raised.exception, failure)
        self.assertTrue(samples)
        for context, tid, python_thread, sampling_tid in samples:
            self.assertIn("stage=bm.create2", context)
            self.assertEqual((tid, python_thread), (caller_tid, caller_python_id))
            self.assertNotEqual(sampling_tid, caller_tid)
        self.assertFalse(
            any(t.name == "mempool-startup-watch" for t in threading.enumerate())
        )

    def test_disabled_diagnostics_never_spawn_or_sample(self):
        """Ordinary startup has no background diagnostics or procfs reads."""
        with (
            patch.dict(os.environ, SGLANG_NPU_MEMPOOL_DIAGNOSTICS="0"),
            patch.object(diagnostics.threading, "Thread") as thread,
            patch.object(diagnostics, "_sample") as sample,
        ):
            with diagnostics.startup_stage("test", resources=True):
                pass
        thread.assert_not_called()
        sample.assert_not_called()

    def test_procfs_permission_errors_do_not_abort_startup(self):
        """Docker restrictions must reduce evidence, not change BM behavior."""
        with (
            patch.dict(os.environ, SGLANG_NPU_MEMPOOL_DIAGNOSTICS="1"),
            patch.object(Path, "open", side_effect=PermissionError(13, "denied")),
            self.assertLogs(diagnostics.logger, level="INFO") as logs,
        ):
            with diagnostics.startup_stage("test", resources=True):
                pass
        self.assertTrue(any("PermissionError" in line for line in logs.output))

    def test_monitor_start_failure_does_not_abort_startup(self):
        """A lack of spare threads must not cause an unrelated allocation failure."""
        with (
            patch.dict(os.environ, SGLANG_NPU_MEMPOOL_DIAGNOSTICS="1"),
            patch.object(
                threading.Thread, "start", side_effect=RuntimeError("no threads")
            ),
        ):
            with diagnostics.startup_stage("test"):
                pass

    def test_cgroup_v2_resolves_subtree_and_visible_parent_limit(self):
        """A permissive leaf must not hide a tighter visible container limit."""
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            proc, mount = base / "proc", base / "cgroup"
            proc.mkdir()
            (mount / "worker").mkdir(parents=True)
            (proc / "cgroup").write_text("0::/tenant/container/worker\n")
            (proc / "mountinfo").write_text(
                f"10 1 0:1 /tenant/container {mount} rw - cgroup2 cgroup rw\n"
            )
            (mount / "memory.max").write_text("1048576\n")
            (mount / "worker/memory.max").write_text("max\n")
            limits = diagnostics._cgroup_memory(proc)["visible_limits"]
            self.assertEqual(set(limits), {str(mount), str(mount / "worker")})
            self.assertEqual(limits[str(mount)]["memory.max"], "1048576")
            self.assertEqual(limits[str(mount / "worker")]["memory.max"], "max")

    def test_cgroup_v1_uses_the_memory_controller(self):
        """Resolve memory separately from unrelated cgroup v1 controllers."""
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            proc, mount = base / "proc", base / "memory"
            proc.mkdir()
            mount.mkdir()
            (proc / "cgroup").write_text("2:cpu:/other\n3:memory:/container\n")
            (proc / "mountinfo").write_text(
                f"10 1 0:1 /container {mount} rw - cgroup cgroup rw,memory\n"
            )
            (mount / "memory.limit_in_bytes").write_text("2097152\n")
            limits = diagnostics._cgroup_memory(proc)["visible_limits"]
            self.assertEqual(set(limits), {str(mount)})
            self.assertEqual(limits[str(mount)]["memory.limit_in_bytes"], "2097152")


if __name__ == "__main__":
    unittest.main()
