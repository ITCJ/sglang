"""Check the temporary diagnostic against real computed-source snapshots."""

import contextlib
import copy
import io
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import summarize_mempool_reuse
import test_prefix as prefix_fixture
import torch

from ascend_mempool.debug_reuse import DEBUG_ENV, ReuseProbe


class TestReuseProbe(unittest.TestCase):
    def test_real_probe_records_produce_complete_copyable_summary(self):
        probe = ReuseProbe(page_size=128, rank=0)
        with self.assertLogs("ascend_mempool.debug_reuse", level="INFO") as logs:
            for rid, tokens, hit in (
                ("A", 4096, 0),
                ("B", 5120, 4096),
                ("C", 4096, 3968),
            ):
                probe.begin_request(rid, list(range(tokens)), hit)
                batch = SimpleNamespace(
                    input_ids=torch.arange(hit, tokens),
                    positions=torch.arange(hit, tokens),
                    extend_prefix_lens=torch.tensor([hit]),
                    extend_seq_lens=torch.tensor([tokens - hit]),
                    out_cache_loc=torch.arange(128 + hit, 128 + tokens),
                )
                probe.forward(
                    batch,
                    block_table=torch.arange(1, tokens // 128 + 1).reshape(1, -1),
                    query_lengths=torch.tensor([tokens - hit]),
                    kv_lengths=torch.tensor([tokens]),
                    restored_indices=torch.arange(128, 128 + hit),
                    indexer_layer_ids=list(range(21)),
                    start_layer=0,
                )
                # Small feature dimensions exercise real emission/collection without NPU storage.
                values = {
                    (region, layer): torch.ones(tokens, 1, 2)
                    for region, count in (("main", 78), ("index", 21))
                    for layer in range(count)
                }
                if rid == "C":
                    values["main", 2][-1, 0, 0] = 2
                probe.finish(values)
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "prefill.log"
            path.write_text("\n".join(logs.output))
            report = summarize_mempool_reuse.summarize(path, ranks=1)
        self.assertTrue(report["complete"])
        self.assertTrue(report["ranks"][0]["metadata_ok"])
        with contextlib.redirect_stdout(io.StringIO()) as output:
            summarize_mempool_reuse.print_summary(report)
        rows = output.getvalue().splitlines()
        self.assertEqual(len(rows), 2)
        summary = json.loads(rows[1].split(" ", 1)[1])
        self.assertEqual(summary["first_main"]["layer"], 2)
        self.assertEqual(summary["first_main"]["max_abs"], 1)

    def test_store_emits_comparison_only_after_full_alive_validation(self):
        fixture = prefix_fixture.TestPrefixStore()
        self.addCleanup(fixture.doCleanups)
        with patch.dict("os.environ", {DEBUG_ENV: "1"}):
            store, manager, kernel, _ = fixture.make_store(verify=True)
        for rid, hit in (("A", 0), ("C", 2)):
            store.begin_request(rid, 3, 4, input_ids=[10, 11, 12, 13], hit_len=hit)
            for region, count in (("main", 2), ("index", 1)):
                for layer in range(count):
                    target = kernel.buffers[
                        manager.view(0, layer, region="alive_" + region).device_base
                    ]
                    values = torch.full_like(target[3, :4], 1)
                    if rid == "C" and region == "main" and layer == 1:
                        values[-1, 0, 0] = 2
                    store.capture_source(
                        region,
                        layer,
                        values,
                        torch.full((4,), 3),
                        torch.arange(4),
                        torch.ones(4, dtype=torch.bool),
                    )
                    target[3, :4] = values
            if hit:
                with self.assertNoLogs("ascend_mempool.debug_reuse", level="INFO"):
                    store.verify_alive(3, 2, start=2)
            with self.assertLogs("ascend_mempool.debug_reuse", level="INFO") as logs:
                store.verify_alive(3, 4)
            if rid == "C":
                rows = self.records(logs)
                self.assertEqual(rows[-1]["stage"], "comparison")
                self.assertEqual(sum(r.get("changed", 0) for r in rows), 1)
            store.release_source(rid)

    def test_same_prompt_tail_comparison_survives_release_and_skips_other_inputs(self):
        probe = ReuseProbe(page_size=2, rank=3)
        values = {
            ("main", 0): torch.ones(4, 1, 4, dtype=torch.bfloat16),
            ("main", 1): torch.ones(4, 1, 4, dtype=torch.bfloat16),
            ("index", 0): torch.ones(4, 1, 2, dtype=torch.bfloat16),
        }
        probe.begin_request("A", [10, 11, 12, 13], 0)
        with self.assertLogs("ascend_mempool.debug_reuse", level="INFO"):
            probe.finish(values)
        # The source request can be released/reused; retain only a cloned tail.
        values["main", 1][2, 0, 3] = 2
        values["index", 0][3, 0, 0] = 4
        probe.begin_request("B", [10, 11, 99, 98], 2)
        with self.assertLogs("ascend_mempool.debug_reuse", level="INFO") as logs:
            probe.finish(values)
        self.assertEqual(self.records(logs)[0]["stage"], "different_input")
        probe.begin_request("C", [10, 11, 12, 13], 2)
        with self.assertLogs("ascend_mempool.debug_reuse", level="INFO") as logs:
            probe.finish(values)
        rows = [r for r in self.records(logs) if r["stage"] == "layer"]
        self.assertEqual(len(rows), 3)
        by_key = {(r["region"], r["layer"]): r for r in rows}
        self.assertEqual(by_key["main", 0]["changed"], 0)
        self.assertEqual(by_key["main", 1]["changed"], 1)
        self.assertEqual(by_key["main", 1]["max_abs"], 1)
        self.assertEqual(by_key["main", 1]["first_difference"], [2, 0, 3])
        self.assertEqual(by_key["index", 0]["max_abs"], 3)
        self.assertEqual(self.records(logs)[-1]["reference_rid"], "A")
        self.assertEqual(self.records(logs)[-1]["page"], 1)

    @staticmethod
    def records(logs):
        return [json.loads(line.split("data=", 1)[1]) for line in logs.output]

    def test_kernel_metadata_checks_positions_lengths_and_noncontiguous_mapping(self):
        probe = ReuseProbe(page_size=2, rank=0)
        batch = SimpleNamespace(
            input_ids=torch.tensor([12, 13]),
            positions=torch.tensor([2, 3]),
            extend_prefix_lens=torch.tensor([2]),
            extend_seq_lens=torch.tensor([2]),
            out_cache_loc=torch.tensor([8, 9]),
        )

        def observe():
            probe.begin_request("C", [10, 11, 12, 13], 2)
            with self.assertLogs("ascend_mempool.debug_reuse", level="INFO") as logs:
                probe.forward(
                    batch,
                    block_table=torch.tensor([[2, 4]]),
                    query_lengths=torch.tensor([2]),
                    kv_lengths=torch.tensor([4]),
                    restored_indices=torch.tensor([4, 5]),
                    indexer_layer_ids=[1],
                    start_layer=0,
                )
            return self.records(logs)[0]

        self.assertTrue(all(observe()["checks"].values()))
        batch.positions = torch.tensor([0, 1])
        self.assertFalse(observe()["checks"]["positions"])
        batch.positions = torch.tensor([2, 3])
        batch.out_cache_loc = torch.tensor([6, 7])
        self.assertFalse(observe()["checks"]["block_table"])

    def test_summary_marks_missing_layer_bad_metadata_and_broken_log(self):
        rows = [
            dict(
                stage="forward",
                rank=0,
                rid=rid,
                hit_len=hit,
                input_sha256="a" * 64 if rid != "B" else "b" * 64,
                checks={
                    key: True
                    for key in (
                        "input_ids",
                        "positions",
                        "query_lengths",
                        "kv_lengths",
                        "prefix_lengths",
                        "extend_lengths",
                        "restored_length",
                        "suffix_length",
                        "block_table",
                        "unique_indices",
                    )
                },
            )
            for rid, hit in (("A", 0), ("B", 4096), ("C", 3968))
        ]
        rows.extend(
            dict(
                stage="layer",
                rank=0,
                rid="C",
                region=region,
                layer=layer,
                changed=int(layer == 2),
                nonfinite=0,
                reference_rid="A",
            )
            for region, count in (("main", 78), ("index", 21))
            for layer in range(count)
        )
        rows.append(
            dict(
                stage="comparison",
                rank=0,
                rid="C",
                page=31,
                hit_len=3968,
                reference_rid="A",
                main_layers=78,
                index_layers=21,
            )
        )
        rows.append(dict(stage="baseline", rank=0, rid="A", page=31, layers=99))
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "prefill.log"

            def check(records):
                path.write_text(
                    "\n".join(
                        summarize_mempool_reuse.TAG + json.dumps(row) for row in records
                    )
                )
                return summarize_mempool_reuse.summarize(path, ranks=1)

            result = check(rows)
            self.assertTrue(result["complete"])
            self.assertEqual(result["ranks"][0]["first_main"]["layer"], 2)
            self.assertFalse(check(rows[:3] + rows[4:])["complete"])
            for defect in (
                "no_baseline",
                "no_hash",
                "wrong_hash",
                "wrong_reference",
                "wrong_request",
                "missing_checks",
            ):
                with self.subTest(defect=defect):
                    damaged = copy.deepcopy(rows)
                    if defect == "no_baseline":
                        damaged.pop()
                    elif defect == "no_hash":
                        damaged[2].pop("input_sha256")
                    elif defect == "wrong_hash":
                        damaged[2]["input_sha256"] = "c" * 64
                    elif defect == "missing_checks":
                        damaged[2]["checks"] = {}
                    else:
                        for row in damaged:
                            if row["stage"] in ("layer", "comparison"):
                                key = (
                                    "rid"
                                    if defect == "wrong_request"
                                    else "reference_rid"
                                )
                                row[key] = "unrelated"
                    result = check(damaged)
                    self.assertFalse(result["complete"])
                    if defect == "missing_checks":
                        self.assertFalse(result["ranks"][0]["metadata_ok"])
            rows[2]["checks"]["positions"] = False
            self.assertFalse(check(rows)["ranks"][0]["metadata_ok"])
            path.write_text(path.read_text() + "\n" + summarize_mempool_reuse.TAG + "{")
            self.assertFalse(
                summarize_mempool_reuse.summarize(path, ranks=1)["complete"]
            )


if __name__ == "__main__":
    unittest.main()
