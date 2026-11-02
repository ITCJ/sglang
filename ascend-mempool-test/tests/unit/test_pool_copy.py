import threading
import time
import unittest

from ascend_mempool.pool_copy import (
    CopyBlock,
    CopyTask,
    PoolCopyExecutor,
    CompletionUnknown,
    BMTransfer,
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
        return {"submit_return": 0, "wait_return": 0}


class TestPoolCopy(unittest.TestCase):
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
