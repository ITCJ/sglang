import asyncio
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from sglang.test.ci.ci_register import register_cpu_ci
from sglang.test.test_utils import maybe_stub_sgl_kernel

maybe_stub_sgl_kernel()

from sglang.srt.managers.io_struct import (
    FlushCacheReqInput,
    FlushCacheReqOutput,
    WaitUntilIdleReqInput,
    msgpack_decode,
    msgpack_encode,
)
from sglang.srt.managers.scheduler import Scheduler
from sglang.srt.managers.scheduler_components.flush_wrapper import (
    SchedulerFlushWrapper,
)
from sglang.srt.managers.tokenizer_control_mixin import TokenizerControlMixin

register_cpu_ci(est_time=14, suite="base-a-test-cpu")
register_cpu_ci(est_time=8, suite="base-c-test-cpu")


class TestSchedulerFlushCache(unittest.TestCase):
    def _new_scheduler(self) -> Scheduler:
        scheduler = Scheduler.__new__(Scheduler)
        scheduler.ipc_channels = MagicMock()
        scheduler.flush_cache = MagicMock(return_value=True)
        scheduler.is_fully_idle = MagicMock(return_value=False)
        scheduler.flush_wrapper = SchedulerFlushWrapper(
            flush_cache=scheduler.flush_cache,
            is_fully_idle=scheduler.is_fully_idle,
            ipc_channels=scheduler.ipc_channels,
        )
        return scheduler

    def test_immediate_flush_no_timeout(self):
        """No timeout → flush immediately regardless of idle state."""
        scheduler = self._new_scheduler()
        scheduler.flush_cache.return_value = False

        output = scheduler.flush_wrapper.handle(FlushCacheReqInput(timeout_s=None))

        self.assertFalse(output.success)
        scheduler.flush_cache.assert_called_once()

    def test_immediate_flush_when_idle(self):
        """Positive timeout but already idle → flush immediately."""
        scheduler = self._new_scheduler()
        scheduler.is_fully_idle.return_value = True

        output = scheduler.flush_wrapper.handle(FlushCacheReqInput(timeout_s=5.0))

        self.assertTrue(output.success)
        scheduler.flush_cache.assert_called_once()

    def test_defers_when_busy(self):
        """Positive timeout + busy → defers, returns None."""
        scheduler = self._new_scheduler()
        req = FlushCacheReqInput(timeout_s=3.0)

        with patch(
            "sglang.srt.managers.scheduler_components.flush_wrapper.time.monotonic",
            return_value=10.0,
        ):
            output = scheduler.flush_wrapper.handle(req)

        self.assertIsNone(output)
        pending_req, deadline = scheduler.flush_wrapper._pending
        self.assertIs(pending_req, req)
        self.assertEqual(deadline, 13.0)

    def test_rejects_when_already_pending(self):
        """Any new request is rejected while another is pending."""
        scheduler = self._new_scheduler()
        scheduler.flush_wrapper._pending = (FlushCacheReqInput(timeout_s=10.0), 999.0)

        for timeout in [None, 5.0]:
            output = scheduler.flush_wrapper.handle(
                FlushCacheReqInput(timeout_s=timeout)
            )
            self.assertFalse(output.success)
            self.assertIn("already in progress", output.message)

        scheduler.flush_cache.assert_not_called()

    def test_pending_flush_completes_on_idle(self):
        scheduler = self._new_scheduler()
        scheduler.is_fully_idle.return_value = True
        req = FlushCacheReqInput(timeout_s=1.0)
        scheduler.flush_wrapper._pending = (req, 111.0)

        scheduler.flush_wrapper.check_pending()

        self.assertIsNone(scheduler.flush_wrapper._pending)
        scheduler.flush_cache.assert_called_once()
        out = scheduler.ipc_channels.send_to_tokenizer.send_output.call_args.args[0]
        self.assertTrue(out.success)

    def test_pending_flush_expires_on_timeout(self):
        scheduler = self._new_scheduler()
        req = FlushCacheReqInput(timeout_s=1.0)
        scheduler.flush_wrapper._pending = (req, 99.0)

        with patch(
            "sglang.srt.managers.scheduler_components.flush_wrapper.time.monotonic",
            return_value=100.0,
        ):
            scheduler.flush_wrapper.check_pending()

        self.assertIsNone(scheduler.flush_wrapper._pending)
        scheduler.flush_cache.assert_not_called()
        out = scheduler.ipc_channels.send_to_tokenizer.send_output.call_args.args[0]
        self.assertFalse(out.success)

    def test_pending_flush_survives_before_deadline(self):
        scheduler = self._new_scheduler()
        req = FlushCacheReqInput(timeout_s=5.0)
        scheduler.flush_wrapper._pending = (req, 101.0)

        with patch(
            "sglang.srt.managers.scheduler_components.flush_wrapper.time.monotonic",
            return_value=100.0,
        ):
            scheduler.flush_wrapper.check_pending()

        self.assertIsNotNone(scheduler.flush_wrapper._pending)
        scheduler.ipc_channels.send_to_tokenizer.send_output.assert_not_called()

    def test_wait_only_immediate_when_idle_without_flushing(self):
        scheduler = self._new_scheduler()
        scheduler.is_fully_idle.return_value = True

        output = scheduler.flush_wrapper.handle(WaitUntilIdleReqInput(timeout_s=5.0))

        self.assertTrue(output.success)
        scheduler.flush_cache.assert_not_called()

    def test_wait_only_nonblocking_check_when_busy(self):
        scheduler = self._new_scheduler()

        output = scheduler.flush_wrapper.handle(WaitUntilIdleReqInput(timeout_s=0.0))

        self.assertFalse(output.success)
        self.assertIn("not idle", output.message)
        self.assertIsNone(scheduler.flush_wrapper._pending)
        scheduler.flush_cache.assert_not_called()

    def test_wait_only_defers_when_busy(self):
        scheduler = self._new_scheduler()
        req = WaitUntilIdleReqInput(timeout_s=3.0)

        with patch(
            "sglang.srt.managers.scheduler_components.flush_wrapper.time.monotonic",
            return_value=10.0,
        ):
            output = scheduler.flush_wrapper.handle(req)

        self.assertIsNone(output)
        pending_req, deadline = scheduler.flush_wrapper._pending
        self.assertIs(pending_req, req)
        self.assertEqual(deadline, 13.0)
        scheduler.flush_cache.assert_not_called()

    def test_pending_wait_only_completes_without_flushing(self):
        scheduler = self._new_scheduler()
        scheduler.is_fully_idle.return_value = True
        req = WaitUntilIdleReqInput(timeout_s=1.0)
        scheduler.flush_wrapper._pending = (req, 111.0)

        with patch(
            "sglang.srt.managers.scheduler_components.flush_wrapper.time.monotonic",
            return_value=110.0,
        ):
            scheduler.flush_wrapper.check_pending()

        self.assertIsNone(scheduler.flush_wrapper._pending)
        scheduler.flush_cache.assert_not_called()
        out = scheduler.ipc_channels.send_to_tokenizer.send_output.call_args.args[0]
        self.assertTrue(out.success)

    def test_pending_wait_only_expires_without_flushing(self):
        scheduler = self._new_scheduler()
        req = WaitUntilIdleReqInput(timeout_s=1.0)
        scheduler.flush_wrapper._pending = (req, 99.0)

        with patch(
            "sglang.srt.managers.scheduler_components.flush_wrapper.time.monotonic",
            return_value=100.0,
        ):
            scheduler.flush_wrapper.check_pending()

        self.assertIsNone(scheduler.flush_wrapper._pending)
        scheduler.flush_cache.assert_not_called()
        out = scheduler.ipc_channels.send_to_tokenizer.send_output.call_args.args[0]
        self.assertFalse(out.success)

    def test_expired_wait_only_does_not_succeed_when_idle(self):
        scheduler = self._new_scheduler()
        scheduler.is_fully_idle.return_value = True
        req = WaitUntilIdleReqInput(timeout_s=1.0)
        scheduler.flush_wrapper._pending = (req, 99.0)

        with patch(
            "sglang.srt.managers.scheduler_components.flush_wrapper.time.monotonic",
            return_value=100.0,
        ):
            scheduler.flush_wrapper.check_pending()

        scheduler.is_fully_idle.assert_not_called()
        scheduler.flush_cache.assert_not_called()
        out = scheduler.ipc_channels.send_to_tokenizer.send_output.call_args.args[0]
        self.assertFalse(out.success)

    def test_wait_request_round_trips_over_msgpack(self):
        req = WaitUntilIdleReqInput(timeout_s=5.0)

        decoded = msgpack_decode(msgpack_encode(req))

        self.assertIsInstance(decoded, WaitUntilIdleReqInput)
        self.assertIsInstance(decoded, FlushCacheReqInput)
        self.assertEqual(decoded.timeout_s, 5.0)


class TestTokenizerWaitUntilIdle(unittest.IsolatedAsyncioTestCase):
    async def test_requires_every_scheduler_to_be_idle(self):
        manager = MagicMock()
        manager.auto_create_handle_loop = MagicMock()
        manager.asyncio_tasks = set()
        manager.flush_cache_communicator = AsyncMock(
            return_value=[
                FlushCacheReqOutput(success=True),
                FlushCacheReqOutput(
                    success=False, message="Timed out waiting for idle state."
                ),
            ]
        )

        output = await TokenizerControlMixin.wait_until_idle(manager, timeout_s=5.0)

        self.assertFalse(output.success)
        self.assertEqual(output.message, "Timed out waiting for idle state.")
        request = manager.flush_cache_communicator.call_args.args[0]
        self.assertIsInstance(request, WaitUntilIdleReqInput)
        self.assertEqual(request.timeout_s, 5.0)

    async def test_cancellation_does_not_cancel_shared_communicator(self):
        manager = MagicMock()
        manager.auto_create_handle_loop = MagicMock()
        manager.asyncio_tasks = set()
        entered = asyncio.Event()
        release = asyncio.Event()
        completed = asyncio.Event()

        async def communicate(_request):
            entered.set()
            try:
                await release.wait()
                return [FlushCacheReqOutput(success=True)]
            finally:
                completed.set()

        manager.flush_cache_communicator = AsyncMock(side_effect=communicate)
        task = asyncio.create_task(
            TokenizerControlMixin.wait_until_idle(manager, timeout_s=5.0)
        )
        await entered.wait()

        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task

        self.assertFalse(completed.is_set())
        release.set()
        await asyncio.wait_for(completed.wait(), timeout=1.0)
        await asyncio.sleep(0)
        self.assertEqual(manager.asyncio_tasks, set())


if __name__ == "__main__":
    unittest.main()
