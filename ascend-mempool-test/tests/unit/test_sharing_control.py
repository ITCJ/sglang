"""Exercise the real four-worker coordinator using loopback sockets."""

import concurrent.futures
import unittest

from ascend_mempool.sharing_control import Cohort, CohortServer


class TestSharingControl(unittest.TestCase):
    def test_four_members_exchange_and_all_drain_before_release(self):
        server = CohortServer("127.0.0.1", 0, "test", 3)
        self.addCleanup(server.close)
        port = server.server.server_address[1]

        def worker(rank):
            client = Cohort("127.0.0.1", port, rank, "test", 3)
            try:
                values = client.exchange("mapped", contribution=0 if rank % 2 else 1024)
                client.exchange("drained", drained=True)
                return values
            finally:
                client.close()

        with concurrent.futures.ThreadPoolExecutor(max_workers=4) as threads:
            outcomes = list(threads.map(worker, range(4)))
        self.assertEqual(
            outcomes[3], [dict(contribution=1024), dict(contribution=0)] * 2
        )
        self.assertIsNone(server.error)

    def test_mismatched_phase_aborts_every_worker_but_still_allows_drain(self):
        server = CohortServer("127.0.0.1", 0, "test", 3)
        self.addCleanup(server.close)
        port = server.server.server_address[1]

        def worker(rank):
            client = Cohort("127.0.0.1", port, rank, "test", 3)
            try:
                with self.assertRaisesRegex(RuntimeError, "abort"):
                    client.exchange("failed" if rank == 0 else "measuring")
                return client.exchange("drained", drained=True)
            finally:
                client.close()

        with concurrent.futures.ThreadPoolExecutor(max_workers=4) as threads:
            outcomes = list(threads.map(worker, range(4)))
        self.assertTrue(all(row["drained"] for row in outcomes[0]))


if __name__ == "__main__":
    unittest.main()
