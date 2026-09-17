"""Check deterministic page coverage without importing torch or an NPU runtime."""
import unittest

from bench import page_slots
from run import case_matrix


class MappingTest(unittest.TestCase):
    def test_contiguous_and_scattered_cover_same_slots_without_reserved_page(self):
        for count in (1, 2, 3, 8, 32, 128, 137, 512, 1024):
            with self.subTest(count=count):
                contiguous = page_slots(count)
                scattered = page_slots(count, True)
                self.assertEqual(contiguous, list(range(1, count + 1)))
                self.assertEqual(sorted(scattered), list(range(1, count * 2, 2)))
                self.assertEqual(scattered, page_slots(count, True))
                if count > 1:
                    self.assertNotEqual(scattered, contiguous)
                self.assertFalse(set(scattered) & set(range(2, count * 2, 2)))

    def test_1k_does_not_degenerate_to_contiguous(self):
        self.assertEqual(page_slots(8, True), [1, 3, 5, 7, 9, 11, 13, 15])

    def test_larger_formal_sizes_use_page_sized_gaps(self):
        for count in (32, 128, 512, 1024):
            self.assertEqual(page_slots(count, True),
                             list(range(1, count * 2, 2)))

    def test_invalid_page_counts(self):
        for count in (0, -1):
            with self.assertRaises(ValueError):
                page_slots(count, True)

    def test_single_size_and_smoke_respect_scatter_selection(self):
        self.assertEqual(case_matrix(4096, scatter=True), [(128, 'scattered'), (4096, 'scattered')])
        self.assertEqual(case_matrix(smoke=True), [(128, 'contiguous'), (128, 'scattered')])
        self.assertEqual(case_matrix(128, scatter=True), [(128, 'scattered')])


if __name__ == '__main__':
    unittest.main()
