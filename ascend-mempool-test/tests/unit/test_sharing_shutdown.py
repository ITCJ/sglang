"""Replay a slow D leave against the real all-worker shutdown protocol."""

import concurrent.futures
import threading
import unittest

from ascend_mempool.sharing_control import Cohort, CohortServer


class TestSharingShutdown(unittest.TestCase):
    def exercise(self, tp_size, shared, *, failed_work=False):
        total = 2 * tp_size
        owners = {0} if shared else set(range(tp_size))
        # D's last member is still leaving when P0 is ready to shut its Store.
        slow_rank = total - 1
        entered, release, store_shutdown = (threading.Event() for _ in range(3))
        lock = threading.Lock()
        drained, closed, clients = set(), set(), set()
        server = CohortServer("127.0.0.1", 0, "shutdown", 3, tp_size)
        port = server.server.server_address[1]

        def worker(rank):
            cohort = Cohort("127.0.0.1", port, rank, "shutdown", 3, tp_size)

            def drain():
                with lock:
                    drained.add(rank)

            def close_pool():
                self.assertEqual(len(drained), total)
                if rank == slow_rank:
                    entered.set()
                    self.assertTrue(release.wait(2))
                with lock:
                    closed.add(rank)

            def uninitialize():
                if rank in owners:
                    store_shutdown.set()
                    self.assertEqual(
                        len(closed), total, "Store closed before every pool left"
                    )
                    self.assertEqual(
                        clients,
                        set(range(total)) - owners,
                        "Store closed before its SDK clients",
                    )
                else:
                    with lock:
                        clients.add(rank)

            try:
                return cohort.shutdown(
                    drain=drain,
                    close_pool=close_pool,
                    uninitialize=uninitialize,
                    store_owner=rank in owners,
                    success=not (failed_work and rank == slow_rank),
                )
            finally:
                cohort.close()

        try:
            with concurrent.futures.ThreadPoolExecutor(total) as threads:
                futures = [threads.submit(worker, r) for r in range(total)]
                try:
                    self.assertTrue(entered.wait(2))
                    self.assertFalse(
                        store_shutdown.wait(0.1),
                        "P Store shut down while D was still leaving",
                    )
                finally:
                    release.set()
                results = [f.result(timeout=4) for f in futures]
            self.assertEqual(results, [not failed_work] * total)
            self.assertTrue(store_shutdown.is_set())
        finally:
            release.set()
            server.close()

    def test_tp4_shared_store_survives_slow_decode_leave(self):
        self.exercise(4, True)

    def test_tp1_and_tp2_independent_store_owners_wait_for_clients(self):
        for n in (1, 2):
            with self.subTest(tp_size=n):
                self.exercise(n, False)

    def test_failed_work_still_closes_pools_and_clients_before_store(self):
        self.exercise(4, True, failed_work=True)

    def test_cleanup_failure_is_reported_and_never_authorizes_store_shutdown(self):
        for broken in ("drain", "pool", "client"):
            with self.subTest(broken=broken):
                server = CohortServer("127.0.0.1", 0, broken, 3, 1)
                port = server.server.server_address[1]
                store_closed = threading.Event()

                def worker(rank):
                    client = Cohort("127.0.0.1", port, rank, broken, 3, 1)

                    def action(stage):
                        if rank == 1 and stage == broken:
                            raise RuntimeError("injected " + broken + " failure")
                        if rank == 0 and stage == "client":
                            store_closed.set()

                    try:
                        with self.assertRaisesRegex(RuntimeError, "injected " + broken):
                            client.shutdown(
                                drain=lambda: action("drain"),
                                close_pool=lambda: action("pool"),
                                uninitialize=lambda: action("client"),
                                store_owner=rank == 0,
                                success=True,
                            )
                    finally:
                        client.close()

                try:
                    with concurrent.futures.ThreadPoolExecutor(2) as threads:
                        list(threads.map(worker, (0, 1)))
                    self.assertFalse(store_closed.is_set())
                finally:
                    server.close()
