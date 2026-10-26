"""Reject incomplete reuse evidence, even when outputs and retained pages agree."""

import copy
import json
import tempfile
import unittest
from pathlib import Path

import test_mempool_prefix_evidence as fixtures
import verify_mempool_reuse as verifier


class TestReuseEvidence(unittest.TestCase):
    def test_actual_main_history_and_all_rank_content_with_corruptions(self):
        snapshots, cases, lines = fixtures.TestPrefixEvidence().evidence(reuse=True)
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "prefill.log"

            def check(data=snapshots, records=lines):
                path.write_text("\n".join(records))
                return verifier.check_reuse(path, data, cases)

            self.assertEqual(check()["hit_lengths"], [0, 4096, 3968])
            # A fast rank returns the same completed local receipt each tick
            # until the slowest rank finishes; Main still receives one TP ack.
            repeated = list(lines)
            local = next(i for i, line in enumerate(repeated) if "event=fill " in line)
            repeated.insert(local, repeated[local])
            self.assertEqual(check(records=repeated)["pages_per_request"], [32, 40, 40])
            for defect in (
                "missing_rank",
                "contiguous",
                "wrong_mapping",
                "content",
                "early_ready",
                "early_fill",
                "wrong_hit",
            ):
                with self.subTest(defect=defect):
                    data, records = copy.deepcopy(snapshots), list(lines)
                    done = data[1]["completed"][-1]
                    if defect == "missing_rank":
                        done["reads"]["fill"]["ack"]["ranks"].pop()
                    elif defect == "early_fill":
                        trace = done["trace"]
                        a = next(
                            i for i, t in enumerate(trace) if t["kind"] == "fill_ack"
                        )
                        b = next(
                            i
                            for i, t in enumerate(trace)
                            if t["kind"] == "native_freed"
                        )
                        trace[a], trace[b] = trace[b], trace[a]
                    elif defect == "wrong_hit":
                        done["hit_len"] = 4096 - 128
                    else:
                        marker = (
                            '"stage": "load_back"'
                            if defect != "early_ready"
                            else '"fill_complete": false'
                        )
                        i = next(i for i, line in enumerate(records) if marker in line)
                        prefix, encoded = records[i].split("data=", 1)
                        value = json.loads(encoded)
                        if defect == "content":
                            value["pages"]["0"] = "f" * 64
                        elif defect == "early_ready":
                            value["ready_tokens"] = 5120
                        else:
                            indices = value["prefix_indices"]
                            if defect == "contiguous":
                                indices[:] = list(range(128, 4096 + 128))
                            else:
                                indices[128] += 1
                            done["reads"]["load_back"]["ack"]["ranks"][0][
                                "prefix_indices"
                            ] = indices.copy()
                        records[i] = prefix + "data=" + json.dumps(value)
                    with self.assertRaises(ValueError):
                        check(data, records)


if __name__ == "__main__":
    unittest.main()
