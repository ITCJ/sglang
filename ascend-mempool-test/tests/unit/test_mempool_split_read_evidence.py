"""K1-03 acceptance rejects missing traffic, content or completion evidence."""

import contextlib
import copy
import io
import json
import tempfile
import unittest
from pathlib import Path

import run_mempool_min
import verify_mempool_split_read as verifier


class TestSplitReadEvidence(unittest.TestCase):
    def test_cli_describes_two_reader_strategies_without_loading_models(self):
        with tempfile.TemporaryDirectory() as folder:
            config = Path(folder) / "config.json"
            config.write_text(
                json.dumps(dict(prefill_host="p", decode_host="d", model_path="/model"))
            )
            for role in ("prefill", "decode"):
                output = io.StringIO()
                with contextlib.redirect_stdout(output):
                    code = run_mempool_min.main(
                        [
                            "--role",
                            role,
                            "--mode",
                            "split-read-acceptance",
                            "--config",
                            str(config),
                            "--run-id",
                            "k1-ticket03-001",
                            "--describe",
                        ]
                    )
                description = json.loads(output.getvalue())
                self.assertEqual(code, 0)
                self.assertEqual(description["service_rounds"], 2)
                self.assertEqual(
                    description["reader_assignments"], ["round_robin", "contiguous"]
                )
                self.assertTrue(description["config"]["verify_prefix"])
                self.assertTrue(description["config"]["match_prefix"])

    def test_complete_report_and_corrupted_evidence(self):
        snapshots, lines = [], []
        for rank in range(16):
            lines.append(
                "mempool split_read_buffers data="
                + json.dumps(
                    dict(
                        rank=rank,
                        rid="B",
                        slots=32,
                        page_size=128,
                        data_buffers=2,
                        buffer_bytes=dict(main=32 * 128 * 1152, index=32 * 128 * 256),
                    )
                )
            )
        for rid, count in (("B", 32), ("C", 31)):
            rows = []
            for rank in range(16):
                pages = list(range(rank, count, 16))
                rows.append(
                    dict(
                        rank=rank,
                        split_read=dict(
                            assignment="round_robin",
                            readers=16,
                            logical_pages=pages,
                            padded_pages=2,
                            read_bytes=len(pages) * 128 * 95232,
                            oracle_read_bytes=0,
                            oracle="computed_source",
                            gathered_verified=True,
                            native_verified=True,
                            path=["H2D", "all_gather", "scatter"],
                            launches=dict(H2D=99, all_gather=99, scatter=99),
                            data_buffers=2,
                            buffer_bytes=dict(
                                main=32 * 128 * 1152, index=32 * 128 * 256
                            ),
                            slot_table_builds=1,
                            slot_table_rows=count * 128,
                            slot_pages=[
                                p
                                for r in range(16)
                                for p in (r, r + 16 if r + 16 < count else None)
                            ],
                            completed_ns=100 + rank,
                        ),
                    )
                )
            snapshots.append(
                dict(
                    completed=[
                        dict(
                            rid=rid,
                            hit_len=count * 128,
                            reads=dict(
                                load_back=dict(
                                    command=dict(op_id=count), ack=dict(ranks=rows)
                                )
                            ),
                        )
                    ]
                )
            )
            lines.append(
                "mempool split_read_ack data="
                + json.dumps(dict(rid=rid, op_id=count, ack_ns=116))
            )
        with tempfile.TemporaryDirectory() as folder:
            log = Path(folder) / "prefill.log"
            log.write_text("\n".join(lines))
            self.assertEqual(
                verifier.check_split_read(log, snapshots, "round_robin")["status"],
                "passed",
            )
            for key, value in (
                ("logical_pages", [0, 1]),
                ("read_bytes", 0),
                ("oracle_read_bytes", 1),
                ("oracle", "private_replica"),
                ("completed_ns", 117),
                ("readers", 1),
                ("assignment", "contiguous"),
                ("data_buffers", 4),
                ("slot_table_builds", 2),
                ("slot_table_rows", 4097),
                ("slot_pages", [0] * 32),
                ("native_verified", False),
                ("gathered_verified", False),
                ("launches", dict(H2D=99, all_gather=99, scatter=198)),
            ):
                with self.subTest(key=key):
                    bad = copy.deepcopy(snapshots)
                    bad[0]["completed"][-1]["reads"]["load_back"]["ack"]["ranks"][0][
                        "split_read"
                    ][key] = value
                    with self.assertRaises(ValueError):
                        verifier.check_split_read(log, bad, "round_robin")
            log.write_text(lines[0])
            with self.assertRaises(ValueError):
                verifier.check_split_read(log, snapshots, "round_robin")

    def test_timing_uses_one_rank_for_device_parts_and_wall_residual_for_control(self):
        ranks = [
            dict(
                split_read=dict(
                    timings_ms=dict(
                        H2D=2.0, all_gather=3.0, scatter=1.0, T_lb_rank=12.0
                    )
                )
            )
            for _ in range(16)
        ]
        ranks[15]["split_read"]["timings_ms"]["H2D"] = 4.0
        data = [
            dict(
                completed=[
                    dict(
                        rid="B",
                        hit_len=4096,
                        reads=dict(
                            load_back=dict(elapsed_ms=20.0, ack=dict(ranks=ranks))
                        ),
                    )
                ]
            )
        ]
        row = verifier.timing_rows(data)[0]
        self.assertEqual(
            (
                row["rank"],
                row["H2D"],
                row["all_gather"],
                row["scatter"],
                row["control"],
            ),
            (15, 4.0, 3.0, 1.0, 12.0),
        )
        with tempfile.TemporaryDirectory() as folder:
            verifier.write_timing_table(
                Path(folder), [dict(assignment="round_robin", timings=[row])]
            )
            report = json.loads((Path(folder) / "split-read-timings.json").read_text())
            self.assertEqual(len(report["baseline"]), 6)
            self.assertIn(
                "all_gather", (Path(folder) / "split-read-timings.md").read_text()
            )
        ranks[15]["split_read"]["timings_ms"]["H2D"] = 30.0
        with self.assertRaisesRegex(ValueError, "smaller"):
            verifier.timing_rows(data)

    def test_missing_strategy_outputs_cannot_pass_by_empty_comparison(self):
        # A truncated responses file cannot make zip silently skip strategy checks.
        for count in (0, 5):
            with (
                self.subTest(outputs=count),
                self.assertRaisesRegex(ValueError, "all six"),
            ):
                verifier.compare_strategies(
                    dict(cases=[{}] * 6, results=[{}] * 6),
                    dict(cases=[{}] * 6, results=[{}] * count),
                )


if __name__ == "__main__":
    unittest.main()
