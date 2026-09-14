import unittest

from feasibility_check import destination


class DestinationTest(unittest.TestCase):
    def test_small_contiguous(self):
        self.assertEqual(destination(0, 1, False), 1)

    def test_max_scattered_covers_all_l1_pages(self):
        slots = [destination(page, 1024, True) for page in range(1024)]
        self.assertEqual(set(slots), set(range(1, 1025)))
        self.assertNotEqual(slots[1], 2)


if __name__ == "__main__":
    unittest.main()
