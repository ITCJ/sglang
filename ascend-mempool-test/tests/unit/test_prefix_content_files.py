"""Long per-rank evidence survives a shared launcher log without losing ordering."""

import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from ascend_mempool.prefix import log_prefix_content
from verify_mempool_prefix import Reports


class TestPrefixContentFiles(unittest.TestCase):
    def test_large_rank_records_use_short_markers_and_preserve_complete_contents(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            expected = []
            with patch.dict(os.environ, {"SGLANG_NPU_MEMPOOL_EVIDENCE_DIR": folder}):
                with self.assertLogs("ascend_mempool.prefix", level="INFO") as captured:
                    for rank in (3, 5, 13, 0):
                        record = dict(
                            rid="r4",
                            rank=rank,
                            stage="retained",
                            pages={
                                str(p): {"main": str(rank) * 64, "index": "b" * 64}
                                for p in range(80)
                            },
                            prefix_indices=list(range(4096)),
                        )
                        expected.append(record)
                        log_prefix_content(record)
            self.assertTrue(all(len(line.encode()) < 512 for line in captured.output))
            log = root / "prefill.log"
            log.write_text("\n".join(captured.output) + "\n")
            result = Reports.read(log, 64)
            for number, record in enumerate(expected):
                self.assertEqual(
                    result.contents["r4", record["rank"], "retained"], (number, record)
                )
            paths = list((root / "prefix-content").glob("*.json"))
            paths[0].write_text("{broken")
            with self.assertRaises(json.JSONDecodeError):
                Reports.read(log, 64)
            paths[0].unlink()
            with self.assertRaises(FileNotFoundError):
                Reports.read(log, 64)
