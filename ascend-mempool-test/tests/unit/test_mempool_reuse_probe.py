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
from test_native_release import load_functions, load_methods

from ascend_mempool.debug_reuse import DEBUG_ENV, ReuseProbe


class TestReuseProbe(unittest.TestCase):
    def test_actual_ascend_attention_and_projection_keep_original_output(self):
        fixture = prefix_fixture.TestPrefixStore()
        self.addCleanup(fixture.doCleanups)
        with patch.dict("os.environ", {DEBUG_ENV: "1"}):
            store, _, _, _ = fixture.make_store(verify=True)
        store.begin_request("A", 3, 4, input_ids=[1, 2, 3, 4])
        calls = []

        def kernel(**kw):
            calls.append(kw["query"].shape[0])
            return kw["query"] + kw["query"].shape[0], None, None

        npu = SimpleNamespace(
            npu_sparse_flash_attention=kernel,
            npu_transpose_batchmatmul=lambda x, *a, **kw: x.clone(),
        )
        backend_type = load_methods(
            "hardware_backend/npu/attention/ascend_backend.py",
            "AscendAttnBackend",
            {"forward_sparse"},
            dict(
                torch=torch,
                torch_npu=npu,
                is_dsa_enable_prefill_cp=lambda: False,
                _expand_dsa_sparse_indices=lambda x: x,
            ),
            standalone=True,
        )
        backend = backend_type()
        backend.mempool_runtime = SimpleNamespace(prefix_store=store)
        backend.kv_lora_rank = backend.qk_rope_head_dim = 2
        backend.kv_cache_dtype = torch.bfloat16
        backend._pad_topk_indices = lambda x, n: x
        backend.token_to_kv_pool = SimpleNamespace(
            set_kv_buffer=lambda *a: None,
            get_kv_buffer=lambda layer: (torch.zeros(8, 1, 2), torch.zeros(8, 1, 2)),
            indexer_layer_ids=[0],
            start_layer=0,
        )
        backend.forward_metadata = SimpleNamespace(
            actual_seq_lengths_q=None,
            actual_seq_lengths_kv=None,
            seq_lens_cpu_int=torch.tensor([4]),
            block_tables=torch.tensor([[1, 3]]),
        )
        mode = SimpleNamespace(
            is_extend=lambda: True,
            is_draft_extend_v2=lambda: False,
            is_target_verify=lambda: False,
            is_extend_without_speculative=lambda: True,
        )
        batch = SimpleNamespace(
            forward_mode=mode,
            extend_seq_lens=torch.tensor([4]),
            extend_prefix_lens=torch.tensor([0]),
            positions=torch.arange(4),
            input_ids=torch.tensor([1, 2, 3, 4]),
            out_cache_loc=torch.tensor([2, 3, 6, 7]),
        )
        layer = SimpleNamespace(layer_id=0, tp_k_head_num=1, scaling=0.5)
        model = SimpleNamespace(
            layer_id=0,
            num_local_heads=2,
            kv_lora_rank=2,
            v_head_dim=2,
            w_vc=None,
            next_skip_topk=False,
            o_proj=lambda x: (x + 10, None),
        )
        model.attn_mqa = lambda q, k, v, b, **kw: backend.forward_sparse(
            q, k, v, layer, b, **kw
        )
        namespace = dict(
            torch=torch,
            torch_npu=npu,
            _is_npu_arch35=False,
            get_attn_backend=lambda: backend,
        )
        load_functions(
            "hardware_backend/npu/modules/deepseek_v2_attention_mla_npu.py",
            {"forward_dsa_core_npu", "_reuse_checkpoint"},
            namespace,
        )
        q = torch.zeros(4, 2, 2)
        k = torch.zeros(4, 1, 2)
        with self.assertLogs("ascend_mempool.debug_reuse", level="INFO") as logs:
            result, _ = namespace["forward_dsa_core_npu"](
                model,
                q,
                k,
                q,
                k,
                torch.zeros(4, 1, 2, dtype=torch.int32),
                batch,
                None,
                batch.positions,
                False,
            )
        self.assertEqual(calls, [4, 4, 2])
        self.assertTrue(torch.equal(result, torch.full((4, 4), 14.0)))
        names = {r.get("name") for r in self.records(logs)}
        self.assertTrue(
            {"layer0.value_projection", "layer0.output_projection", "tail_query"}
            <= names
        )

    def test_checkpoints_compare_identical_tail_and_keep_topk_order_separate(self):
        probe = ReuseProbe(page_size=2, rank=0)
        cold = torch.arange(16).reshape(4, 2, 2).float()
        topk = torch.tensor([[0, 1], [1, 0], [0, 2], [3, 1]])
        probe.begin_request("A", [1, 2, 3, 4], 0)
        with self.assertLogs("ascend_mempool.debug_reuse", level="INFO"):
            probe.checkpoint("layer0.query", cold)
            probe.checkpoint("layer0.topk", topk)
            probe.checkpoint("layer0.topk_set", topk.sort(dim=-1).values)
        current = cold[-2:].clone()
        cold.zero_()
        probe.begin_request("B", [1, 2, 9, 9], 2)
        with self.assertNoLogs("ascend_mempool.debug_reuse", level="INFO"):
            probe.checkpoint("layer0.query", current + 1)
        probe.begin_request("C", [1, 2, 3, 4], 2)
        with self.assertLogs("ascend_mempool.debug_reuse", level="INFO") as logs:
            probe.checkpoint("layer0.query", current)
            probe.checkpoint("layer0.topk", topk[-2:].flip(-1))
            probe.checkpoint("layer0.topk_set", topk[-2:].flip(-1).sort(dim=-1).values)
        rows = {r["name"]: r for r in self.records(logs)}
        self.assertEqual(rows["layer0.query"]["changed"], 0)
        self.assertEqual(rows["layer0.topk"]["changed"], 4)
        self.assertEqual(rows["layer0.topk_set"]["changed"], 0)
        self.assertTrue(all(r["reference_rid"] == "A" for r in rows.values()))

    def test_attention_replay_preserves_model_output_and_changes_only_query_extent(
        self,
    ):
        probe = ReuseProbe(page_size=2, rank=0)
        probe.begin_request("A", [1, 2, 3, 4], 0)
        query = torch.arange(16).reshape(4, 2, 2).float()
        kwargs = dict(
            query=query,
            query_rope=query.clone(),
            key=torch.zeros(8, 1, 2),
            value=torch.zeros(8, 1, 2),
            key_rope=torch.zeros(8, 1, 2),
            sparse_indices=torch.arange(8).reshape(4, 1, 2),
            actual_seq_lengths_query=torch.tensor([4]),
            actual_seq_lengths_kv=torch.tensor([4]),
            block_table=torch.tensor([[1, 3]]),
            layout_query="TND",
            layout_kv="PA_BSND",
            sparse_mode=3,
            attention_mode=2,
            scale_value=0.5,
        )
        calls = []

        def kernel(**kw):
            calls.append(kw)
            # Shape-dependent output makes the diagnostic red without simulating the NPU bug.
            return kw["query"] + kw["query"].shape[0], None, None

        original = kernel(**kwargs)[0]
        saved = original.clone()
        with self.assertLogs("ascend_mempool.debug_reuse", level="INFO") as logs:
            probe.inspect_attention(kwargs, original, kernel)
        self.assertTrue(torch.equal(original, saved))
        self.assertEqual(len(calls), 3)
        self.assertTrue(torch.equal(calls[-1]["query"], query[-2:]))
        self.assertEqual(calls[-1]["actual_seq_lengths_query"].tolist(), [2])
        for key in ("key", "value", "key_rope", "block_table", "actual_seq_lengths_kv"):
            self.assertIs(calls[-1][key], kwargs[key])
        replays = {
            r["name"]: r for r in self.records(logs) if r["stage"] == "attention_replay"
        }
        self.assertEqual(replays["same_shape"]["changed"], 0)
        self.assertEqual(replays["tail_query"]["max_abs"], 2)
        probe.begin_request("C", [1, 2, 3, 4], 2)
        tail_kwargs = calls[-1]
        current = kernel(**tail_kwargs)[0]
        with self.assertLogs("ascend_mempool.debug_reuse", level="INFO") as logs:
            probe.inspect_attention(tail_kwargs, current, kernel)
        replays = [r for r in self.records(logs) if r["stage"] == "attention_replay"]
        self.assertEqual(replays[0]["name"], "C_vs_A_tail")
        self.assertEqual(replays[0]["changed"], 0)
        self.assertEqual(len(calls), 4)  # No extra model/kernel run during reused C.

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
                for name in (
                    "layer0.input",
                    "layer0.value_projection",
                    "layer0.output_projection",
                    "layer1.input",
                ):
                    probe.checkpoint(name, torch.ones(tokens - hit, 1, 2))
                query = torch.ones(tokens - hit, 1, 2)
                kwargs = dict(
                    query=query,
                    query_rope=query.clone(),
                    sparse_indices=torch.zeros(tokens - hit, 1, 2, dtype=torch.int32),
                    actual_seq_lengths_query=torch.tensor([tokens - hit]),
                    actual_seq_lengths_kv=torch.tensor([tokens]),
                    layout_query="TND",
                    layout_kv="PA_BSND",
                    sparse_mode=3,
                    attention_mode=2,
                )
                probe.inspect_attention(
                    kwargs, query, lambda **kw: (kw["query"].clone(), None, None)
                )
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
        self.assertEqual(len(rows), 3)
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
            self.assertTrue(result["prefix_complete"])
            self.assertFalse(
                result["complete"]
            )  # Older logs lack the operator evidence.
            self.assertEqual(result["ranks"][0]["first_main"]["layer"], 2)
            self.assertFalse(check(rows[:3] + rows[4:])["prefix_complete"])
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
                    self.assertFalse(result["prefix_complete"])
                    if defect == "missing_checks":
                        self.assertFalse(result["ranks"][0]["metadata_ok"])
            rows[2]["checks"]["positions"] = False
            self.assertFalse(check(rows)["ranks"][0]["metadata_ok"])
            path.write_text(path.read_text() + "\n" + summarize_mempool_reuse.TAG + "{")
            self.assertFalse(
                summarize_mempool_reuse.summarize(path, ranks=1)["complete"]
            )

    def test_operator_summary_rejects_missing_duplicate_or_cross_request_evidence(self):
        rows = []
        for name in summarize_mempool_reuse.CHECKPOINTS:
            rows.extend(
                [
                    dict(stage="checkpoint_baseline", name=name, rid="A"),
                    dict(
                        stage="checkpoint",
                        name=name,
                        rid="C",
                        reference_rid="A",
                        changed=0,
                        nonfinite=0,
                    ),
                ]
            )
        for name in ("same_shape", "tail_query", "C_vs_A_tail"):
            rows.append(
                dict(
                    stage="attention_replay",
                    name=name,
                    rid="C" if name == "C_vs_A_tail" else "A",
                    reference_rid="A",
                )
            )
        check = lambda data: summarize_mempool_reuse.operator_summary(data, "A", "C")
        self.assertTrue(check(rows)["complete"])
        self.assertFalse(check(rows[1:])["complete"])
        self.assertFalse(check(rows + [rows[0]])["complete"])
        self.assertFalse(check(rows[:-1])["complete"])
        for stage in ("checkpoint_baseline", "checkpoint", "attention_replay"):
            damaged = copy.deepcopy(rows)
            next(r for r in damaged if r["stage"] == stage)["rid"] = "wrong"
            self.assertFalse(check(damaged)["complete"])

    def test_checkpoint_budget_and_disabled_store_do_not_run_extra_kernels(self):
        probe = ReuseProbe(page_size=2, rank=0)
        probe.begin_request("A", [1, 2, 3, 4], 0)
        with patch("ascend_mempool.debug_reuse.CHECKPOINT_BUDGET_BYTES", 4):
            with self.assertRaisesRegex(RuntimeError, "budget"):
                probe.checkpoint("layer0.input", torch.ones(4, 2))
        fixture = prefix_fixture.TestPrefixStore()
        self.addCleanup(fixture.doCleanups)
        with patch.dict("os.environ", {DEBUG_ENV: "0"}):
            store, _, _, _ = fixture.make_store(verify=True)
        store.begin_request("A", 3, 4)

        def unexpected(**kw):
            self.fail("disabled diagnostic called a kernel")

        with self.assertNoLogs("ascend_mempool.debug_reuse", level="INFO"):
            store.debug_attention(0, {}, torch.zeros(4, 2), unexpected)
            store.debug_checkpoint(0, "input", torch.zeros(4, 2))


if __name__ == "__main__":
    unittest.main()
