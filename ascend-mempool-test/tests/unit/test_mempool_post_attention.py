"""Exercise diagnostic hooks on real torch modules; no NPU accuracy claim."""

import contextlib
import copy
import io
import json
import os
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import torch
from torch import nn

from ascend_mempool.debug_reuse import ReuseProbe


class Norm(nn.Module):
    def __init__(self):
        super().__init__()
        self.weight = nn.Parameter(torch.ones(16))
        self.variance_epsilon = 1e-6

    def forward(self, x, residual=None):
        if residual is None:
            return x * torch.rsqrt(x.square().mean(-1, keepdim=True) + 1e-6)
        added = x + residual
        # In-place kernels must not overwrite the observer's input snapshots.
        x.copy_(added)
        return added * torch.rsqrt(added.square().mean(-1, keepdim=True) + 1e-6), added


class Projection(nn.Module):
    def __init__(self):
        super().__init__()
        self.weight = nn.Parameter(torch.eye(16))

    def forward(self, x):
        return x @ self.weight, None


class MLP(nn.Module):
    tp_size = 1

    def __init__(self):
        super().__init__()
        self.gate_up_proj = Projection()
        self.down_proj = Projection()

    def forward(self, x, forward_batch=None, gemm_output_zero_allocator=None):
        up, _ = self.gate_up_proj(x)
        # An intentional shape-sensitive operator tests diagnostic sensitivity.
        out, _ = self.down_proj(up + (0.01 if x.shape[0] == 2 else 0))
        return out


class Attention(nn.Module):
    def forward(self, positions, hidden_states, forward_batch):
        return hidden_states.clone()


class Layer(nn.Module):
    def __init__(self, layer_id):
        super().__init__()
        self.layer_id = layer_id
        self.self_attn = Attention()
        self.input_layernorm = Norm()
        self.post_attention_layernorm = Norm()
        self.mlp = MLP()
        self.layer_communicator = SimpleNamespace(
            _communicate_with_all_reduce_and_layer_norm_fn=SimpleNamespace(
                __name__="_scatter_hidden_states_and_residual"
            ),
            _context=SimpleNamespace(attn_tp_size=1, attn_tp_rank=0, attn_dp_size=1),
        )

    def forward(self, positions, hidden_states, forward_batch, residual=None):
        if residual is None:
            residual = hidden_states
            hidden_states = self.input_layernorm(hidden_states)
        else:
            hidden_states, residual = self.input_layernorm(hidden_states, residual)
        hidden_states = self.self_attn(positions, hidden_states, forward_batch)
        hidden_states, residual = self.post_attention_layernorm(hidden_states, residual)
        hidden_states = self.mlp(hidden_states, forward_batch)
        return hidden_states, residual, None


class Model(nn.Module):
    def __init__(self):
        super().__init__()
        self.layers = nn.ModuleList([Layer(0), Layer(1)])

    def forward(self, positions, x, batch):
        x, residual, _ = self.layers[0](positions, x, batch)
        return self.layers[1](positions, x, batch, residual)


class Collective:
    """Real CPU sums/gathers, including aliasing of reduce-scatter input/output."""

    def __init__(self, ranks):
        self.ranks = ranks
        self.barrier = threading.Barrier(ranks, timeout=15)
        self.inputs = {}

    def exchange(self, rank, value, reduce):
        self.inputs[rank] = value.clone()
        self.barrier.wait()
        values = [self.inputs[i] for i in range(self.ranks)]
        result = torch.stack(values).sum(0) if reduce else torch.cat(values)
        self.barrier.wait()
        return result

    def reduce(self, rank, output, inputs):
        output.copy_(self.exchange(rank, inputs, True).tensor_split(self.ranks)[rank])


class DistributedLayer(Layer):
    def __init__(self, layer_id, rank, group):
        super().__init__(layer_id)
        self.rank, self.group = rank, group
        self.layer_communicator._context = SimpleNamespace(
            attn_tp_size=group.ranks, attn_tp_rank=rank, attn_dp_size=1
        )

    def forward(self, positions, hidden_states, forward_batch, residual=None):
        if self.layer_id == 1:
            x, residual = self.input_layernorm(hidden_states, residual)
            x = self.group.exchange(self.rank, x, False)
            return self.self_attn(positions, x, forward_batch), residual, None
        residual = hidden_states
        x = self.input_layernorm(hidden_states)
        x = self.self_attn(positions, x, forward_batch)
        local = x.tensor_split(self.group.ranks)[self.rank]
        self.group.reduce(self.rank, local, x)
        residual = residual.tensor_split(self.group.ranks)[self.rank]
        x, residual = self.post_attention_layernorm(local, residual)
        return self.mlp(x, forward_batch), residual, None


def artifacts(root, *, ranks=2, prompt=16, page=4):
    from ascend_mempool.debug_post_attention import PostAttentionProbe

    group = Collective(ranks)

    def execute(rank):
        with torch.no_grad():
            model = Model()
            model.layers = nn.ModuleList(
                [DistributedLayer(i, rank, group) for i in range(2)]
            )
            probe = ReuseProbe(page_size=page, rank=rank)
            trace = PostAttentionProbe(
                probe,
                root,
                rank=rank,
                world_size=ranks,
                reduce_scatter=lambda out, value: group.reduce(rank, out, value),
            )
            trace.install(model)
            for name, hit in (("A", 0), ("C", prompt - page)):
                probe.begin_request(name, list(range(prompt)), hit)
                batch = SimpleNamespace(input_ids=torch.arange(hit, prompt))
                value = torch.arange(prompt * 16).reshape(prompt, 16).float() / (
                    prompt * 16
                )
                model(torch.arange(hit, prompt), value[hit:].clone(), batch)

    with ThreadPoolExecutor(ranks) as workers:
        futures = [workers.submit(execute, rank) for rank in range(ranks)]
        for future in futures:
            future.result(timeout=30)
    return {
        label: [
            torch.load(root / f"{label}-rank{rank}.pt", weights_only=True)
            for rank in range(ranks)
        ]
        for label in ("cold", "reused")
    }


class TestPostAttention(unittest.TestCase):
    def test_tp16_4096_and_128_rows_preserve_logical_ownership(self):
        import summarize_mempool_post_attention as summary

        threads = torch.get_num_threads()
        try:
            torch.set_num_threads(1)
            with tempfile.TemporaryDirectory() as folder:
                rows = artifacts(Path(folder), ranks=16, prompt=4096, page=128)
                result = summary.analyze(rows, 16)
                self.assertTrue(result["checks_ok"])
                self.assertEqual(
                    [
                        (r["token"], r["cold_owner"], r["reused_owner"])
                        for r in result["tokens"]
                    ],
                    [(3968, 15, 0), (4061, 15, 11), (4090, 15, 15)],
                )
                self.assertEqual(
                    rows["cold"][0]["records"]["reduce.rotated"]["positions"],
                    list(range(3968, 4096)),
                )
                self.assertEqual(
                    rows["reused"][15]["records"]["actual.reduced"]["positions"],
                    list(range(4088, 4096)),
                )
        finally:
            torch.set_num_threads(threads)

    def test_hooks_keep_original_outputs_and_capture_shape_replays(self):
        from ascend_mempool.debug_post_attention import PostAttentionProbe

        with tempfile.TemporaryDirectory() as folder, torch.no_grad():
            root = Path(folder)
            model = Model()
            probe = ReuseProbe(page_size=2, rank=0)
            calls = []

            def reduce(output, inputs):
                calls.append(tuple(inputs.shape))
                output.copy_(inputs)

            inputs = torch.arange(128).reshape(8, 16).float() / 128
            batch = SimpleNamespace(
                positions=torch.arange(8), input_ids=torch.arange(8)
            )
            expected = model(batch.positions, inputs.clone(), batch)
            trace = PostAttentionProbe(
                probe, root, rank=0, world_size=1, reduce_scatter=reduce
            )
            trace.install(model)
            probe.begin_request("A", list(range(8)), 0)
            actual = model(batch.positions, inputs.clone(), batch)
            self.assertTrue(torch.equal(expected[0], actual[0]))
            cold = torch.load(root / "cold-rank0.pt", weights_only=True)
            records = cold["records"]
            self.assertEqual(records["actual.reduced"]["positions"], [6, 7])
            self.assertFalse(
                torch.equal(
                    records["actual.reduced"]["values"],
                    records["actual.residual_out"]["values"],
                )
            )
            self.assertFalse(
                torch.equal(
                    records["mlp.full.output"]["values"],
                    records["mlp.chunks.output"]["values"],
                )
            )
            self.assertTrue(
                torch.equal(
                    records["mlp.full.output"]["values"],
                    records["mlp.repeat.output"]["values"],
                )
            )
            self.assertEqual(cold["mapping_marker"]["changed"], 0)
            self.assertEqual(len(calls), 4)
            probe.begin_request("B", list(range(10)), 8)
            model(batch.positions, inputs.clone(), batch)
            self.assertEqual(len(calls), 4)
            probe.begin_request("C", list(range(8)), 6)
            batch = SimpleNamespace(
                positions=torch.arange(6, 8), input_ids=torch.arange(6, 8)
            )
            model(batch.positions, inputs[-2:].clone(), batch)
            reused = torch.load(root / "reused-rank0.pt", weights_only=True)
            self.assertEqual(reused["hit_len"], 6)
            self.assertEqual(reused["records"]["actual.reduced"]["positions"], [6, 7])
            self.assertEqual(len(calls), 6)

    def test_missing_artifacts_cannot_produce_a_complete_diagnosis(self):
        import summarize_mempool_post_attention as summarize

        with tempfile.TemporaryDirectory() as folder:
            report = summarize.summarize(Path(folder))
        self.assertFalse(report["complete"])
        self.assertTrue(report["errors"])

    def test_cross_rank_controls_and_first_boundary(self):
        import summarize_mempool_post_attention as summary

        with tempfile.TemporaryDirectory() as folder:
            rows = artifacts(Path(folder))
            result = summary.summarize(Path(folder), ranks=2)
            self.assertTrue(result["complete"], result["errors"])
            self.assertTrue(result["checks_ok"])
            self.assertEqual(result["first_changed_boundary"], "down_input")
            self.assertEqual(result["reductions"]["full_vs_tail"]["changed"], 0)
            self.assertEqual(
                summary.compact(result)["local_effects"]["mlp_shape"], [0, 1]
            )
            self.assertEqual(
                summary.compact(result)["local_effects"]["mlp_rank_full"], []
            )
            self.assertEqual(result["tokens"][0]["cold_owner"], 1)
            self.assertEqual(result["tokens"][0]["reused_owner"], 0)

            # A new reduction difference must not hide the independent MLP effect.
            rows["reused"][0]["records"]["actual.reduced"]["values"][0, 0] += 0.25
            changed = summary.analyze(rows, 2)
            self.assertEqual(changed["first_changed_boundary"], "reduced")
            self.assertFalse(changed["replay_repeat_ok"])
            self.assertEqual(changed["pre_reduce"][0]["changed"], 0)
            self.assertEqual(
                summary.compact(changed)["local_effects"]["mlp_shape"], [0, 1]
            )

    def test_mapping_weight_and_coverage_failures_are_independent(self):
        import summarize_mempool_post_attention as summary

        with tempfile.TemporaryDirectory() as folder:
            original = artifacts(Path(folder))
        rows = copy.deepcopy(original)
        rows["reused"][0]["records"]["actual.residual_in"]["values"] += 0.001
        result = summary.analyze(rows, 2)
        self.assertFalse(result["mapping_ok"])
        self.assertTrue(result["weights_ok"])
        rows = copy.deepcopy(original)
        digest = next(iter(rows["cold"][0]["weights"]["tensors"].values()))
        digest["sha256"] = "changed-scale-or-weight"
        result = summary.analyze(rows, 2)
        self.assertTrue(result["mapping_ok"])
        self.assertFalse(result["weights_ok"])
        for mutation in (
            "missing",
            "position",
            "duplicate",
            "identity",
            "canonical",
            "shape",
        ):
            with self.subTest(mutation=mutation):
                rows = copy.deepcopy(original)
                record = rows["reused"][0]["records"]["actual.reduced"]
                if mutation == "missing":
                    del rows["reused"][0]["records"]["actual.reduced"]
                elif mutation == "position":
                    record["positions"].reverse()
                elif mutation == "duplicate":
                    record["positions"][1] = record["positions"][0]
                elif mutation == "identity":
                    rows["reused"][1]["input_sha256"] = "0" * 64
                elif mutation == "canonical":
                    rows["cold"][0]["canonical_inputs"]["hidden"]["sha256"] = "0" * 64
                else:
                    rows["cold"][0]["records"]["mlp.chunks.output"]["input_rows"] = 8
                with self.assertRaises(ValueError):
                    summary.analyze(rows, 2)

    def test_state_digest_includes_scales_and_unregistered_tensors(self):
        from ascend_mempool.debug_post_attention import state_manifest, tensor_digest

        layer = Norm()
        layer.register_buffer("scale", torch.tensor([0.5], dtype=torch.bfloat16))
        layer.quant_method = SimpleNamespace(
            scheme=SimpleNamespace(scale=torch.ones(2), mode="per-token")
        )
        initial = state_manifest({"norm": layer})
        self.assertIn("norm.quant_method.scheme.scale", initial["tensors"])
        layer.quant_method.scheme.scale[1] += 1
        self.assertNotEqual(initial, state_manifest({"norm": layer}))
        view = torch.arange(12).reshape(3, 4).T
        self.assertEqual(
            tensor_digest(view)["sha256"], tensor_digest(view.contiguous())["sha256"]
        )
        self.assertNotEqual(
            tensor_digest(torch.tensor(1.0))["sha256"],
            tensor_digest(torch.tensor(2.0))["sha256"],
        )

    def test_modelslim_layer_owned_scheme_and_kernel_are_compared(self):
        from ascend_mempool.debug_post_attention import state_manifest

        layer = Projection()
        layer.quant_method = SimpleNamespace(
            quantization_config=SimpleNamespace(version="v1")
        )
        layer.scheme = SimpleNamespace(
            is_dynamic=True,
            quant_config={"mlp.weight": "W8A8_DYNAMIC"},
            kernel=SimpleNamespace(group_size=128, _scale=torch.ones(2)),
        )
        initial = state_manifest({"mlp": layer})
        self.assertTrue(initial["settings"]["mlp.scheme.is_dynamic"])
        self.assertEqual(initial["settings"]["mlp.scheme.kernel.group_size"], 128)
        self.assertIn("mlp.scheme.kernel._scale", initial["tensors"])
        self.assertEqual(
            initial["settings"]["mlp.quant_method.quantization_config.version"], "v1"
        )
        layer.scheme.quant_config["mlp.weight"] = "W8A8"
        self.assertNotEqual(
            initial["settings"]["mlp.scheme.quant_config"],
            state_manifest({"mlp": layer})["settings"]["mlp.scheme.quant_config"],
        )
        layer.scheme.is_dynamic = False
        self.assertNotEqual(initial, state_manifest({"mlp": layer}))

    def test_probe_rejects_unsupported_mlp_and_saves_hook_errors(self):
        from ascend_mempool.debug_post_attention import PostAttentionProbe

        with tempfile.TemporaryDirectory() as folder:
            probe = ReuseProbe(page_size=2, rank=0)
            trace = PostAttentionProbe(
                probe,
                Path(folder),
                rank=0,
                world_size=1,
                reduce_scatter=lambda out, value: None,
            )
            model = Model()
            model.layers[0].mlp.tp_size = 2
            with self.assertRaisesRegex(ValueError, "replicated dense"):
                trace.install(model)
            model.layers[0].mlp.tp_size = 1
            trace.install(model)
            probe.begin_request("A", list(range(8)), 0)
            batch = SimpleNamespace(input_ids=torch.arange(8))
            with self.assertRaisesRegex(ValueError, "positions"):
                model(torch.arange(8).flip(0), torch.zeros(8, 16), batch)
            saved = json.loads((Path(folder) / "error-rank0.json").read_text())
            self.assertEqual(saved["hook"], "_layer0_pre")


class TestPostRunner(unittest.TestCase):
    def test_scope_budget_and_command_contract(self):
        import run_mempool_min as runner

        from ascend_mempool.debug_post_attention import (
            CPU_BUDGET_BYTES,
            CPU_SCRATCH_BYTES,
        )
        from ascend_mempool_pd.mempool_router_protocol import RouterConfig

        config = RouterConfig(
            "p", "d", "/model", prefix_pages=40, verify_prefix=True, match_prefix=True
        )
        self.assertEqual(
            runner.POST_CPU_BYTES_PER_RANK, CPU_BUDGET_BYTES + CPU_SCRATCH_BYTES
        )
        args = SimpleNamespace(
            config=Path("config"),
            run_id="new",
            diagnose_reuse=True,
            diagnose_post_attention=True,
        )
        for role in ("prefill", "decode"):
            args.role = role
            for target in (False, True):
                with patch.dict(
                    os.environ, {runner.POST_DEBUG_ENV: "/stale/diagnostic"}
                ):
                    env = runner.service_environment(
                        config, args, Path("target"), target=target
                    )
                self.assertEqual(
                    runner.POST_DEBUG_ENV in env, role == "prefill" and target
                )
        before = runner.memory_budget(config, "prefill", diagnose_reuse=True)
        after = runner.memory_budget(
            config, "prefill", diagnose_reuse=True, diagnose_post_attention=True
        )
        self.assertGreaterEqual(after["required_bytes"], before["required_bytes"])
        self.assertEqual(after["post_attention_bytes_per_rank"], 320 * 2**20)
        self.assertEqual(
            runner.memory_budget(config, "decode", diagnose_post_attention=True)[
                "post_attention_bytes_per_rank"
            ],
            0,
        )
        command = [
            "--role",
            "prefill",
            "--mode",
            "reuse-diagnostic",
            "--config",
            "config",
            "--run-id",
            "006",
            "--replay-run",
            "004",
            "--diagnose-post-attention",
        ]
        self.assertTrue(runner.parse_args(command).diagnose_reuse)
        command[3] = "reuse-acceptance"
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            runner.parse_args(command)

    def test_missing_post_records_fail_even_when_outputs_match(self):
        import run_mempool_min as runner

        from ascend_mempool_pd.mempool_router_protocol import RouterConfig

        sent = []
        peer = SimpleNamespace(
            send=lambda tag, **value: sent.append(value), barrier=lambda name: None
        )
        args = SimpleNamespace(
            role="prefill", mode="reuse-diagnostic", diagnose_post_attention=True
        )
        config = RouterConfig(
            "p", "d", "/model", prefix_pages=40, verify_prefix=True, match_prefix=True
        )
        diagnosis = dict(
            complete=True,
            ranks=[
                dict(
                    identity_ok=True,
                    metadata_ok=True,
                    nonfinite=0,
                    operators=dict(checkpoints={}, replays={}),
                )
            ],
        )
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            instance = runner.Runner(args, config, peer, root)
            instance.report.update(
                output_comparison=[dict(name=name, equal=True) for name in "ABC"],
                target_evidence=dict(status="passed"),
            )
            with (
                patch.object(
                    runner.summarize_mempool_reuse, "summarize", return_value=diagnosis
                ),
                contextlib.redirect_stdout(io.StringIO()),
                self.assertRaisesRegex(ValueError, "probes_ok=False"),
            ):
                instance.finish_diagnostic(root)
            self.assertFalse(sent[0]["value"]["post_attention"]["complete"])
            self.assertLess(len(json.dumps(sent[0]).encode()), 65536)
            self.assertTrue((root / "post-attention.json").exists())


if __name__ == "__main__":
    unittest.main()
