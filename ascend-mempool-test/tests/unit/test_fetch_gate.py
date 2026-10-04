"""Check the fetch gate's fixed slot-map kernel contract before paired setup."""

import io
import sys
import unittest
from contextlib import redirect_stderr
from pathlib import Path
from unittest.mock import patch

SCRIPTS = Path(__file__).resolve().parents[2] / "scripts"
sys.path.insert(0, str(SCRIPTS))
import verify_fetch
import verify_graph


class TestFetchGate(unittest.TestCase):
    def test_default_selection_width_matches_slot_map_lookup(self):
        args = verify_fetch.parse_args(["--describe", "--s-p", "8", "--s-d", "16"])
        self.assertEqual(args.topk, 2048)
        layout = verify_fetch.make_layout(args)
        self.assertEqual((layout.prompt.tokens, layout.decode.tokens), (8, 16))

    def test_unsupported_width_is_rejected_before_paired_setup(self):
        for rank in (0, 1):
            for topk in (8, 64):
                with self.subTest(rank=rank, topk=topk):
                    argv = [
                        "verify_fetch.py",
                        "--rank",
                        str(rank),
                        "--head-ip",
                        "127.0.0.1",
                        "--topk",
                        str(topk),
                    ]
                    stderr = io.StringIO()
                    with (
                        patch.object(sys, "argv", argv),
                        patch.object(verify_fetch, "run") as paired_setup,
                        redirect_stderr(stderr),
                    ):
                        self.assertEqual(verify_fetch.main(), 1)
                    paired_setup.assert_not_called()
                    self.assertIn(
                        "slot_map_lookup requires topk=2048", stderr.getvalue()
                    )

    def test_copy_only_gate_still_accepts_variable_width(self):
        self.assertEqual(verify_graph.parse_args(["--describe"]).topk, 64)
        self.assertEqual(verify_graph.parse_args(["--describe", "--topk", "8"]).topk, 8)


if __name__ == "__main__":
    unittest.main()
