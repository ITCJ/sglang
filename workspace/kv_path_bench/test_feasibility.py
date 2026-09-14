import io
import unittest

from feasibility_check import destination, failure_code
from feasibility_log import LogStream


class DestinationTest(unittest.TestCase):
    def test_small_contiguous(self):
        self.assertEqual(destination(0, 1, False), 1)

    def test_max_scattered_covers_all_l1_pages(self):
        slots = [destination(page, 1024, True) for page in range(1024)]
        self.assertEqual(set(slots), set(range(1, 1025)))
        self.assertNotEqual(slots[1], 2)

    def test_short_result_only(self):
        terminal, logfile = io.StringIO(), io.StringIO()
        stream = LogStream(terminal, logfile)
        stream.write("FEASIBILITY_FAIL stage=NPU staging registration\n")
        stream.write("F4\n")
        self.assertEqual(terminal.getvalue(), "F4\n")
        self.assertIn("FEASIBILITY_FAIL", logfile.getvalue())
        self.assertEqual(failure_code("NPU staging registration"), "F4")


if __name__ == "__main__":
    unittest.main()
