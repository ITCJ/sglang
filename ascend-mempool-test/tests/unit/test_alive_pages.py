"""Request lifetime page allocation, independently of NPU libraries."""

import unittest

from ascend_mempool.alive_pages import AlivePages


class TestAlivePages(unittest.TestCase):
    def test_mixed_lengths_and_fragmented_reuse(self):
        pool = AlivePages(128, page_size=128)
        first = pool.reserve("4k", 4096)
        middle = pool.reserve("8k", 8192)
        last = pool.reserve("1k", 1024)
        self.assertEqual(
            (len(first.pages), len(middle.pages), len(last.pages)), (32, 64, 8)
        )
        self.assertEqual(pool.free_count, 24)
        pool.release(first)
        # A request can span the freed head and the remaining tail.
        scattered = pool.reserve("6k", 6144)
        self.assertEqual(scattered.pages, tuple(range(32)) + tuple(range(104, 120)))
        self.assertEqual(pool.free_count, 8)
        pool.release(middle)
        pool.release(last)
        pool.release(scattered)
        self.assertEqual(pool.free_count, 128)

    def test_reserve_rounds_by_configured_page_and_rejects_stale_release(self):
        pool = AlivePages(4, page_size=64)
        old = pool.reserve("request", 65)
        self.assertEqual(old.pages, (0, 1))
        pool.release(old)
        current = pool.reserve("request", 65)
        self.assertNotEqual(current.version, old.version)
        with self.assertRaisesRegex(RuntimeError, "stale"):
            pool.release(old)
        self.assertEqual(pool.free_count, 2)
        with self.assertRaisesRegex(ValueError, "capacity"):
            pool.reserve("too-large", 193)
        self.assertEqual(pool.free_count, 2)
        pool.release(current)


if __name__ == "__main__":
    unittest.main()
