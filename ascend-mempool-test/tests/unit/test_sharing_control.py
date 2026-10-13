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
