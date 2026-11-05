"""Resume service checks only from matching successful device reports."""

import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from run_mempool_min import read_component_reports


class TestComponentReuse(unittest.TestCase):
    def test_passed_reports_are_preserved_and_bad_evidence_rejected(self):
        config = SimpleNamespace(page_size=64, decode_capacity=512)
        copy = dict(status="passed", cases=[dict(status="passed", page_size=64)] * 3)
        fetch = dict(
            status="passed",
            rank=1,
            layout=dict(
                layout=dict(
                    prompt=dict(tokens=64, page_size=64),
                    decode=dict(tokens=512, page_size=64),
                )
            ),
            checks=[dict(case="page_boundaries")],
        )
        with tempfile.TemporaryDirectory() as directory:
            paths = [Path(directory) / name for name in ("copy.json", "fetch.json")]
            paths[0].write_text(json.dumps(copy))
            paths[1].write_text(json.dumps(fetch))
            self.assertEqual(read_component_reports(paths, config, 1), (copy, fetch))
            with self.assertRaisesRegex(ValueError, "rank and geometry"):
                read_component_reports(paths, config, 0)
            with self.assertRaisesRegex(ValueError, "rank and geometry"):
                read_component_reports(
                    paths, SimpleNamespace(page_size=128, decode_capacity=512), 1
                )
            fetch["status"] = "failed"
            paths[1].write_text(json.dumps(fetch))
            with self.assertRaisesRegex(ValueError, "passed reports"):
                read_component_reports(paths, config, 1)
