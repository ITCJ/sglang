"""Exercise the real four-worker coordinator using loopback sockets."""

import concurrent.futures
import unittest
from unittest.mock import Mock, patch

from ascend_mempool.sharing_control import Cohort, CohortServer


class TestSharingControl(unittest.TestCase):
    tp_size = 2

    def test_four_members_exchange_and_all_drain_before_release(self):
        server = CohortServer("127.0.0.1", 0, "test", 3, self.tp_size)
        self.addCleanup(server.close)
        port = server.server.server_address[1]

        def worker(rank):
            client = Cohort("127.0.0.1", port, rank, "test", 3, self.tp_size)
            try:
                values = client.exchange(
                    "mapped", contribution=0 if rank % self.tp_size else 1024
                )
                client.exchange("drained", drained=True)
                return values
            finally:
                client.close()

        with concurrent.futures.ThreadPoolExecutor(
            max_workers=2 * self.tp_size
        ) as threads:
            outcomes = list(threads.map(worker, range(2 * self.tp_size)))
        self.assertEqual(
            outcomes[3],
            ([dict(contribution=1024)] + [dict(contribution=0)] * (self.tp_size - 1))
            * 2,
        )
        self.assertIsNone(server.error)

    def test_mismatched_phase_aborts_every_worker_but_still_allows_drain(self):
        server = CohortServer("127.0.0.1", 0, "test", 3, self.tp_size)
        self.addCleanup(server.close)
        port = server.server.server_address[1]

        def worker(rank):
            client = Cohort("127.0.0.1", port, rank, "test", 3, self.tp_size)
            try:
                with self.assertRaisesRegex(RuntimeError, "abort"):
                    client.exchange("failed" if rank == 0 else "measuring")
                return client.exchange("drained", drained=True)
            finally:
                client.close()

        with concurrent.futures.ThreadPoolExecutor(
            max_workers=2 * self.tp_size
        ) as threads:
            outcomes = list(threads.map(worker, range(2 * self.tp_size)))
        self.assertTrue(all(row["drained"] for row in outcomes[0]))


class TestTP4Control(TestSharingControl):
    tp_size = 4

    def test_late_clock_reply_still_reaches_sample_admission(self):
        # Inactive D ranks can receive their clock reply after the deadline.
        # They must still join /done; the active ranks' measured launch skew
        # decides whether the sample is accepted or retried.
        for rank in range(8):
            with self.subTest(rank=rank):
                client = Cohort.__new__(Cohort)
                client.rank, client.tp_size = rank, 4
                client.exchange = Mock(side_effect=[[], [dict(ns=0)] * 8, []])
                with patch(
                    "ascend_mempool.sharing_control.time.monotonic_ns",
                    return_value=150_000_000,
                ):
                    client.aligned_start("sample")
                client.exchange("sample/done", row=None)
                self.assertEqual(client.exchange.call_count, 3)

    def test_late_idle_member_does_not_abort_eight_worker_cohort(self):
        server = CohortServer("127.0.0.1", 0, "late", 3, 4)
        self.addCleanup(server.close)
        port = server.server.server_address[1]

        def worker(rank):
            client = Cohort("127.0.0.1", port, rank, "late", 3, 4)
            exchange = client.exchange

            def delayed_clock(phase, **payload):
                values = exchange(phase, **payload)
                if phase.endswith("/clock") and rank == 4:
                    # Replay an expired reply on one inactive D rank only.
                    # Relative time is local to D; P has unrelated timestamps.
                    return [dict(ns=s["ns"] - 1_000_000_000) for s in values]
                return values

            client.exchange = delayed_clock
            try:
                client.aligned_start("sample")
                rows = client.exchange("sample/done", active=rank < 4)
                client.exchange("drained", success=True, drained=True)
                return rows
            finally:
                client.close()

        with concurrent.futures.ThreadPoolExecutor(8) as threads:
            results = list(threads.map(worker, range(8)))
        self.assertTrue(all(r == results[0] for r in results))
        self.assertEqual(sum(r["active"] for r in results[0]), 4)
        self.assertIsNone(server.error)

    def test_launch_uses_all_four_local_clocks_and_no_remote_clock(self):
        client = Cohort.__new__(Cohort)
        client.rank, client.tp_size = 4, 4
        stamps = [dict(ns=10**15)] * 4 + [dict(ns=i * 1_000_000) for i in range(1, 5)]
        client.exchange = Mock(side_effect=[[], stamps])
        with (
            patch(
                "ascend_mempool.sharing_control.time.monotonic_ns",
                side_effect=[0, 0, 0, 200_000_000],
            ),
            patch("ascend_mempool.sharing_control.time.sleep") as sleep,
        ):
            client.aligned_start("test")
        sleep.assert_called_once_with(0.103)


if __name__ == "__main__":
    unittest.main()
