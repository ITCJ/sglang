import threading
import time
import unittest
from unittest.mock import patch

from ascend_mempool.pool_copy import (
    BMTransfer,
    CompletionUnknown,
    CopyBlock,
    CopyTask,
    PoolCopyExecutor,
    device_thread_setup,
)


class Backend:
    def __init__(self, failure=False):
        self.entered = threading.Event()
        self.release = threading.Event()
        self.failure = failure
        self.calls = []

    def transfer(self, blocks):
        self.calls.append(blocks)
        self.entered.set()
        self.release.wait(2)
        if self.failure:
            raise RuntimeError("SDK failure")


class TestPoolCopy(unittest.TestCase):
    def test_setup_rebinds_inherited_affinity_to_pool_numa(self):
        # Scheduler affinity is narrower than the container's allowed CPUs.
        # Linux intersects the requested mask with cpuset itself; the old
        # thread mask must not constrain a new binding to a different node.
        allowed = {0, 1, 4, 5}
        bound = []

        def set_affinity(tid, requested):
            effective = requested & allowed
            if not effective:
                raise OSError(22, "Invalid argument")
            self.assertEqual(tid, 0)
            bound.append(effective)

        backend = Backend()
        backend.release.set()
        with (
            patch("torch.npu", create=True) as npu,
            patch("pathlib.Path.read_text", return_value="4-7\n"),
            patch("os.sched_getaffinity", return_value={0, 1}, create=True),
            patch("os.sched_setaffinity", side_effect=set_affinity, create=True),
        ):
            executor = PoolCopyExecutor(
                backend,
                timeout=2,
                setup=lambda: device_thread_setup("npu:2", 1),
            )
            executor.submit(CopyTask("request", 1, "admit", (CopyBlock(8, 16, 4),)))
            executor.close()
            result = executor.poll(1)
            self.assertEqual(result.state, "READY", result.error)
            npu.set_device.assert_called_once_with("npu:2")
        self.assertEqual(bound, [{4, 5}])
        self.assertEqual(len(backend.calls), 1)

    def test_completion_timeout_failure_and_empty(self):
        for mode in ("ready", "timeout", "failure", "empty"):
            with self.subTest(mode=mode):
                backend = Backend(mode == "failure")
                clock = [10.0]
                executor = PoolCopyExecutor(backend, timeout=1, clock=lambda: clock[0])
                task = CopyTask(
                    "request",
                    3,
                    "fill",
                    () if mode == "empty" else (CopyBlock(10, 20, 32),),
                )
                executor.submit(task)
                if mode != "empty":
                    self.assertTrue(backend.entered.wait(1))
                    self.assertEqual(executor.poll(3).state, "PENDING")
                if mode == "timeout":
                    clock[0] = 12
                    self.assertEqual(executor.poll(3).state, "UNKNOWN")
                backend.release.set()
                deadline = time.monotonic() + 1
                while (
                    executor.poll(3).state == "PENDING" and time.monotonic() < deadline
                ):
                    time.sleep(0.001)
                executor.close()
                result = executor.poll(3)
                self.assertEqual(
                    result.state,
                    {"timeout": "UNKNOWN", "failure": "FAILED"}.get(mode, "READY"),
                )
                self.assertEqual(result.task, task)
                self.assertEqual(len(backend.calls), 0 if mode == "empty" else 1)


class TestBMTransfer(unittest.TestCase):
    def test_demo_measures_the_same_batch_and_wait_calls(self):
        from types import SimpleNamespace

        from verify_pool_sdma import MeasuredHandle

        calls = []

        class Handle:
            def copy_data_batch(self, *args, **kwargs):
                calls.append((args, kwargs))
                return 0

            def wait(self):
                calls.append("wait")
                return 0

        handle = MeasuredHandle(Handle())
        adapter = BMTransfer(handle, SimpleNamespace(BmCopyType=SimpleNamespace(G2G=5)))
        adapter.transfer((CopyBlock(8, 16, 4),))
        self.assertEqual(
            calls, [(([8], [16], [4], 1, 5), dict(flags=1, stream=0)), "wait"]
        )
        self.assertLessEqual(
            handle.evidence["submit_enter"], handle.evidence["submit_returned"]
        )
        self.assertLessEqual(
            handle.evidence["submit_returned"], handle.evidence["wait_returned"]
        )

    def test_wait_failure_is_unknown(self):
        from types import SimpleNamespace

        class Handle:
            def copy_data_batch(self, *args, **kwargs):
                return 0

            def wait(self):
                return -2004

        adapter = BMTransfer(
            Handle(), SimpleNamespace(BmCopyType=SimpleNamespace(G2G=5))
        )
        with self.assertRaises(CompletionUnknown):
            adapter.transfer((CopyBlock(8, 16, 4),))
