"""Opt-in ticket03 layer-0 probes and controlled replays; never replace outputs.

Hooks are installed only on the P model. Artifacts retain logical token IDs so
the cold tail on rank 15 can be compared with the reused tail across all ranks.
Remove this diagnostic after the hardware mismatch is resolved.
"""

from __future__ import annotations

import functools
import hashlib
import inspect
import json
import os
import time
from collections.abc import Mapping
from pathlib import Path
from typing import Any, Callable

import torch
from torch import Tensor

from .debug_reuse import ReuseProbe

POST_DIR_ENV = "SGLANG_NPU_MEMPOOL_DEBUG_POST_DIR"
CPU_BUDGET_BYTES = 256 * 2**20
CPU_SCRATCH_BYTES = 64 * 2**20
CANONICAL_LIMIT_BYTES = 32 * 2**20
DEVICE_INPUT_LIMIT_BYTES = 64 * 2**20
HASH_CHUNK_BYTES = 8 * 2**20
SCHEMA = 1


def atomic_save(path: Path, value: Any) -> None:
    temporary = path.with_suffix(path.suffix + f".{os.getpid()}.tmp")
    torch.save(value, temporary)
    os.replace(temporary, path)


def tensor_digest(value: Tensor) -> dict[str, Any]:
    """Hash every logical weight/scale byte with bounded CPU readback scratch."""
    digest = hashlib.sha256()
    value = value.detach()
    if value.ndim == 0:
        digest.update(value.cpu().reshape(1).view(torch.uint8).numpy().tobytes())
    elif value.shape[0]:
        row_bytes = value[0].numel() * value.element_size()
        if row_bytes > HASH_CHUNK_BYTES:
            raise ValueError("weight digest row exceeds bounded readback scratch")
        step = max(1, HASH_CHUNK_BYTES // max(1, row_bytes))
        for start in range(0, value.shape[0], step):
            chunk = value[start : start + step].cpu().contiguous()
            digest.update(chunk.view(torch.uint8).numpy().tobytes())
    return dict(
        shape=list(value.shape),
        dtype=str(value.dtype),
        stride=list(value.stride()),
        sha256=digest.hexdigest(),
    )


def state_manifest(scopes: dict[str, Any]) -> dict[str, Any]:
    """Include registered parameters/buffers and unregistered tensor attributes."""
    tensors: dict[str, Any] = {}
    settings: dict[str, Any] = {}
    aliases: dict[int, dict[str, Any]] = {}
    for scope, module in scopes.items():
        for name, child in module.named_modules():
            prefix = scope + ("." + name if name else "")
            settings[prefix + ".class"] = (
                type(child).__module__ + "." + type(child).__qualname__
            )
            implementation = getattr(child, "_forward_method", child.forward)
            settings[prefix + ".forward_implementation"] = (
                getattr(implementation, "__module__", "")
                + "."
                + getattr(
                    implementation, "__qualname__", type(implementation).__qualname__
                )
            )
            objects = [(prefix, child)]
            seen = {id(child)}
            for object_name, obj in objects:
                # ModelSlim dispatches through layer.scheme; other methods own
                # their scheme/kernel. Follow these actual implementation links.
                for key in (
                    "quant_method",
                    "scheme",
                    "kernel",
                    "quant_config",
                    "quantization_config",
                ):
                    nested = getattr(obj, key, None)
                    if (
                        nested is not None
                        and id(nested) not in seen
                        and hasattr(nested, "__dict__")
                    ):
                        seen.add(id(nested))
                        objects.append((object_name + "." + key, nested))
                settings[object_name + ".class"] = (
                    type(obj).__module__ + "." + type(obj).__qualname__
                )
                attributes = dict(vars(obj))
                for registry in ("_parameters", "_buffers"):
                    attributes.update(attributes.get(registry, {}))
                for key, value in sorted(attributes.items()):
                    if (
                        key.startswith("_") and not isinstance(value, Tensor)
                    ) or key in ("training", "tp_rank"):
                        continue
                    full_name = object_name + "." + key
                    if isinstance(value, Tensor):
                        if id(value) not in aliases:
                            aliases[id(value)] = tensor_digest(value)
                        tensors[full_name] = aliases[id(value)]
                    elif value is None or type(value) in (bool, int, float, str):
                        settings[full_name] = value
                    elif isinstance(value, (list, tuple)) and all(
                        type(v) in (int, float, bool, str) for v in value
                    ):
                        settings[full_name] = list(value)
                    elif isinstance(value, Mapping):
                        # ModelSlim's scheme.quant_config is the JSON description.
                        encoded = json.dumps(
                            dict(value), sort_keys=True, allow_nan=False
                        ).encode()
                        if len(encoded) > HASH_CHUNK_BYTES:
                            raise ValueError(
                                "quantization settings exceed digest scratch budget"
                            )
                        settings[full_name] = dict(
                            entries=len(value),
                            sha256=hashlib.sha256(encoded).hexdigest(),
                        )
    if not tensors:
        raise ValueError("post-attention diagnostic found no model state")
    return dict(tensors=tensors, settings=settings)


class PostAttentionProbe:
    def __init__(
        self,
        probe: ReuseProbe,
        root: Path,
        *,
        rank: int,
        world_size: int,
        reduce_scatter: Callable[[Tensor, Tensor], Any],
    ) -> None:
        self.probe, self.root = probe, root
        self.rank, self.world_size = rank, world_size
        self.reduce_scatter = reduce_scatter
        self.root.mkdir(parents=True, exist_ok=True)
        self.active = False
        self.mode = "actual"
        self.positions: list[int] = []
        self.records: dict[str, Any] = {}
        self.canonical: dict[str, Any] = {}
        self.handles: list[Any] = []
        self.full_reduce_input: Tensor | None = None
        self.batch: Any = None
        self.layer0: Any = None
        self.scopes: dict[str, Any] = {}
        self.data: dict[str, Any] = {}
        self.replay_positions: list[int] = []
        self.resident_bytes = 0

    def _guard(self, function: Callable[..., None]) -> Callable[..., None]:
        @functools.wraps(function)
        def run(*args: Any, **kwargs: Any) -> None:
            try:
                function(*args, **kwargs)
            except Exception as exc:
                (self.root / f"error-rank{self.rank}.json").write_text(
                    json.dumps(
                        dict(
                            rank=self.rank,
                            rid=self.probe.rid,
                            hook=function.__name__,
                            error=f"{type(exc).__name__}: {exc}",
                        )
                    )
                )
                raise

        return run

    def install(self, model: Any) -> None:
        layers = {}
        for module in model.modules():
            if getattr(module, "layer_id", None) in (0, 1) and all(
                hasattr(module, k)
                for k in (
                    "self_attn",
                    "mlp",
                    "layer_communicator",
                    "input_layernorm",
                    "post_attention_layernorm",
                )
            ):
                if module.layer_id in layers:
                    raise ValueError("ambiguous decoder layer for post-attention probe")
                layers[module.layer_id] = module
        if set(layers) != {0, 1}:
            raise ValueError("post-attention probe requires decoder layers 0 and 1")
        self.layer0, layer1 = layers[0], layers[1]
        mlp = self.layer0.mlp
        communicator = self.layer0.layer_communicator
        function = communicator._communicate_with_all_reduce_and_layer_norm_fn
        function = getattr(function, "func", function)
        context = communicator._context
        if (
            getattr(function, "__name__", "") != "_scatter_hidden_states_and_residual"
            or (context.attn_tp_size, context.attn_tp_rank, context.attn_dp_size)
            != (self.world_size, self.rank, 1)
            or getattr(mlp, "tp_size", None) != 1
            or not all(hasattr(mlp, k) for k in ("gate_up_proj", "down_proj"))
            or self.probe.page_size % self.world_size
        ):
            raise ValueError(
                "post-attention replay requires scattered tokens and a replicated dense layer-0 MLP"
            )
        self.scopes = {
            "layer0.input_norm": self.layer0.input_layernorm,
            "layer0.post_norm": self.layer0.post_attention_layernorm,
            "layer0.mlp": mlp,
            "layer1.input_norm": layer1.input_layernorm,
        }

        def pre(module: Any, callback: Callable[..., None]) -> None:
            self.handles.append(
                module.register_forward_pre_hook(
                    self._guard(callback), with_kwargs=True
                )
            )

        def post(module: Any, callback: Callable[..., None]) -> None:
            self.handles.append(
                module.register_forward_hook(self._guard(callback), with_kwargs=True)
            )

        pre(self.layer0, self._layer0_pre)
        post(self.layer0.self_attn, self._attention_post)
        pre(self.layer0.post_attention_layernorm, self._norm_pre)
        post(self.layer0.post_attention_layernorm, self._norm_post)
        pre(mlp, self._mlp_pre)
        post(mlp, self._mlp_post)
        post(mlp.gate_up_proj, self._gate_post)
        pre(mlp.down_proj, self._down_pre)
        post(self.layer0, self._layer0_post)
        pre(layer1, self._layer1_pre)
        pre(layer1.input_layernorm, self._next_norm_pre)
        post(layer1.input_layernorm, self._next_norm_post)
        pre(layer1.self_attn, self._next_attention_pre)

    @staticmethod
    def _arguments(
        module: Any, args: tuple[Any, ...], kwargs: dict[str, Any]
    ) -> dict[str, Any]:
        return dict(inspect.signature(module.forward).bind(*args, **kwargs).arguments)

    def _token_positions(self, rows: int) -> list[int]:
        if self.mode != "actual":
            if rows != len(self.replay_positions):
                raise ValueError("replay operator changed its row mapping")
            return self.replay_positions
        if rows == len(self.positions):
            return self.positions
        width = len(self.positions) // self.world_size
        if rows != width:
            raise ValueError(
                f"unexpected token rows {rows}, full={len(self.positions)}, local={width}"
            )
        return self.positions[self.rank * width : (self.rank + 1) * width]

    def _capture(
        self, name: str, value: Tensor, positions: list[int] | None = None
    ) -> None:
        if not isinstance(value, Tensor) or value.ndim != 2:
            raise ValueError(f"{name}: diagnostic expects a two-dimensional tensor")
        positions = (
            self._token_positions(value.shape[0]) if positions is None else positions
        )
        if len(positions) != value.shape[0]:
            raise ValueError(f"{name}: tensor and logical positions differ")
        selected = [
            i
            for i, p in enumerate(positions)
            if p >= len(self.probe.input_ids) - self.probe.page_size
        ]
        size = len(selected) * value.shape[1] * value.element_size()
        if size > 2 * HASH_CHUNK_BYTES or self.resident_bytes + size > CPU_BUDGET_BYTES:
            raise ValueError("post-attention snapshots exceed CPU budget")
        # Copy a bounded slice before an in-place residual/collective can overwrite it.
        cpu = value.detach()[selected].cpu().clone()
        logical = [positions[i] for i in selected]
        record = dict(positions=logical, values=cpu, input_rows=value.shape[0])
        if name in self.records:
            old = self.records[name]
            if not self.mode.endswith("chunks") or set(old["positions"]) & set(logical):
                raise ValueError(f"duplicate diagnostic point: {name}")
            record = dict(
                positions=old["positions"] + logical,
                values=torch.cat((old["values"], cpu)),
                input_rows=value.shape[0],
            )
        self.resident_bytes += cpu.numel() * cpu.element_size()
        if self.resident_bytes > CPU_BUDGET_BYTES:
            raise ValueError("post-attention snapshots exceed CPU budget")
        self.records[name] = record

    def _layer0_pre(
        self, module: Any, args: tuple[Any, ...], kwargs: dict[str, Any]
    ) -> None:
        self.active = self.probe.observes_current() and not self.probe.finished
        if not self.active:
            return
        bound = self._arguments(module, args, kwargs)
        self.batch = bound["forward_batch"]
        self.positions = bound["positions"].detach().cpu().tolist()
        if (
            self.positions != list(range(self.probe.hit_len, len(self.probe.input_ids)))
            or len(self.positions) % self.world_size
        ):
            raise ValueError("post-attention input positions do not match this request")
        if self.batch.input_ids.detach().cpu().tolist() != list(
            self.probe.input_ids[self.probe.hit_len :]
        ):
            raise ValueError("post-attention input IDs differ from this request")
        if bound.get("residual") is not None:
            raise ValueError(
                "layer-0 residual must start from its actual embedding input"
            )
        self.mode, self.records, self.canonical = "actual", {}, {}
        self.resident_bytes = 0
        self.data = dict(
            schema=SCHEMA,
            rank=self.rank,
            world_size=self.world_size,
            rid=self.probe.rid,
            reference_rid=self.probe.reference_rid,
            input_sha256=hashlib.sha256(
                json.dumps(self.probe.input_ids).encode()
            ).hexdigest(),
            prompt_tokens=len(self.probe.input_ids),
            hit_len=self.probe.hit_len,
            page_size=self.probe.page_size,
            positions=self.positions,
            weights=state_manifest(self.scopes),
        )
        self._capture("actual.residual_full", bound["hidden_states"])

    def _attention_post(
        self, module: Any, args: tuple[Any, ...], kwargs: dict[str, Any], output: Any
    ) -> None:
        if self.active:
            value = output[0] if isinstance(output, tuple) else output
            if value.numel() * value.element_size() > DEVICE_INPUT_LIMIT_BYTES:
                raise ValueError(
                    "post-attention full reduction input exceeds device budget"
                )
            self._capture("actual.pre_reduce", value)
            self.full_reduce_input = value.detach().clone()

    def _norm_pre(
        self, module: Any, args: tuple[Any, ...], kwargs: dict[str, Any]
    ) -> None:
        if self.active and self.mode == "actual":
            x, residual = args[:2]
            self._capture("actual.reduced", x)
            self._capture("actual.residual_in", residual)
            if (
                self.probe.rid == self.probe.reference_rid
                and self.rank == self.world_size - 1
            ):
                # Four full local hidden-size tensors are retained for controls.
                if 4 * x.numel() * x.element_size() > CANONICAL_LIMIT_BYTES:
                    raise ValueError(
                        "canonical replay inputs exceed CPU scratch budget"
                    )
                self.canonical.update(
                    hidden=x.detach().cpu().clone(),
                    residual=residual.detach().cpu().clone(),
                    positions=self._token_positions(x.shape[0]),
                )

    def _norm_post(
        self, module: Any, args: tuple[Any, ...], kwargs: dict[str, Any], output: Any
    ) -> None:
        if self.active:
            prefix = "actual" if self.mode == "actual" else self.mode
            self._capture(prefix + ".normalized", output[0])
            self._capture(prefix + ".residual_out", output[1])
            if self.mode == "actual" and self.canonical:
                self.canonical["normalized"] = output[0].detach().cpu().clone()

    def _mlp_pre(
        self, module: Any, args: tuple[Any, ...], kwargs: dict[str, Any]
    ) -> None:
        if self.active:
            self._capture(
                self.mode + ".mlp_input"
                if self.mode == "actual"
                else self.mode + ".input",
                args[0],
            )

    def _mlp_post(
        self, module: Any, args: tuple[Any, ...], kwargs: dict[str, Any], output: Any
    ) -> None:
        if self.active:
            self._capture(
                "actual.mlp_output" if self.mode == "actual" else self.mode + ".output",
                output,
            )
            if self.mode == "actual" and self.canonical:
                self.canonical["mlp_output"] = output.detach().cpu().clone()

    def _gate_post(
        self, module: Any, args: tuple[Any, ...], kwargs: dict[str, Any], output: Any
    ) -> None:
        if self.active:
            self._capture(self.mode + ".gate_up", output[0])

    def _down_pre(
        self, module: Any, args: tuple[Any, ...], kwargs: dict[str, Any]
    ) -> None:
        if self.active:
            self._capture(self.mode + ".down_input", args[0])

    def _reduce_replays(self) -> None:
        value = self.full_reduce_input
        assert value is not None

        def run(name: str, inputs: Tensor, positions: list[int]) -> None:
            local = inputs.tensor_split(self.world_size)[self.rank]
            self.reduce_scatter(local, inputs)
            width = len(positions) // self.world_size
            self._capture(
                name, local, positions[self.rank * width : (self.rank + 1) * width]
            )

        run("reduce.same_shape", value.clone(), self.positions)
        if self.probe.rid == self.probe.reference_rid:
            page = self.probe.page_size
            run("reduce.tail", value[-page:].clone(), self.positions[-page:])
            run(
                "reduce.rotated",
                torch.cat((value[-page:], value[:-page])),
                self.positions[-page:] + self.positions[:-page],
            )
        bits = max(1, len(self.probe.input_ids).bit_length())
        if value.shape[1] < bits:
            raise ValueError("not enough columns for a lossless logical-row marker")
        marker = torch.zeros_like(value)
        # Build exact binary tags on CPU; only the actual collective runs on NPU.
        codes = ((torch.tensor(self.positions)[:, None] >> torch.arange(bits)) & 1).to(
            value.dtype
        )
        marker[:, :bits] = codes.to(value.device)
        output = marker.tensor_split(self.world_size)[self.rank]
        expected = codes.tensor_split(self.world_size)[self.rank] * self.world_size
        self.reduce_scatter(output, marker)
        output_cpu = output.detach().cpu()
        self.data["mapping_marker"] = dict(
            changed=int((output_cpu[:, :bits] != expected).sum().item()),
            nonzero_padding=int(torch.count_nonzero(output_cpu[:, bits:]).item()),
            rows=value.shape[0],
            dtype=str(value.dtype),
            bits=bits,
        )
        self.full_reduce_input = None

    def _local_replays(self, device: torch.device) -> None:
        canonical_path = self.root / "canonical.pt"
        if self.rank == self.world_size - 1:
            atomic_save(
                canonical_path,
                dict(
                    self.canonical,
                    reference_rid=self.probe.reference_rid,
                    input_sha256=self.data["input_sha256"],
                ),
            )
            self.canonical.clear()
        deadline = time.monotonic() + 60
        while not canonical_path.exists():
            if time.monotonic() >= deadline:
                raise TimeoutError("canonical rank did not publish diagnostic inputs")
            time.sleep(0.05)
        canonical = torch.load(canonical_path, map_location="cpu", weights_only=True)
        if (
            sum(
                value.numel() * value.element_size()
                for value in canonical.values()
                if isinstance(value, Tensor)
            )
            > CANONICAL_LIMIT_BYTES
        ):
            raise ValueError("canonical replay inputs exceed CPU scratch budget")
        if (
            canonical["reference_rid"] != self.probe.reference_rid
            or canonical["input_sha256"] != self.data["input_sha256"]
        ):
            raise ValueError("canonical replay input belongs to a different request")
        self.data["canonical_inputs"] = {
            key: tensor_digest(canonical[key])
            for key in ("hidden", "residual", "normalized")
        }
        positions = canonical["positions"]
        local = self.probe.page_size // self.world_size
        chunks = [
            (i, i + local)
            for i in range(len(positions) - self.probe.page_size, len(positions), local)
        ]
        for operation in ("norm", "mlp"):
            for kind in ("full", "repeat", "chunks"):
                self.mode = operation + "." + kind
                slices = chunks if kind == "chunks" else [(0, len(positions))]
                for start, end in slices:
                    self.replay_positions = positions[start:end]
                    if operation == "norm":
                        self.layer0.post_attention_layernorm(
                            canonical["hidden"][start:end].to(device).clone(),
                            canonical["residual"][start:end].to(device).clone(),
                        )
                    else:
                        self.layer0.mlp(
                            canonical["normalized"][start:end].to(device).clone(),
                            self.batch,
                            None,
                        )
        self.mode = "actual"
        self.canonical.clear()

    def _layer0_post(
        self, module: Any, args: tuple[Any, ...], kwargs: dict[str, Any], output: Any
    ) -> None:
        if self.active:
            self._capture("actual.layer0_output", output[0])
            self._capture("actual.layer0_residual", output[1])
            device = output[0].device
            with torch.no_grad():
                self._reduce_replays()
                if self.probe.rid == self.probe.reference_rid:
                    self._local_replays(device)

    def _layer1_pre(
        self, module: Any, args: tuple[Any, ...], kwargs: dict[str, Any]
    ) -> None:
        if self.active:
            bound = self._arguments(module, args, kwargs)
            self._capture("actual.layer1_hidden", bound["hidden_states"])
            self._capture("actual.layer1_residual", bound["residual"])

    def _next_norm_pre(
        self, module: Any, args: tuple[Any, ...], kwargs: dict[str, Any]
    ) -> None:
        if self.active:
            self._capture("actual.next_norm_hidden", args[0])
            self._capture("actual.next_norm_residual", args[1])

    def _next_norm_post(
        self, module: Any, args: tuple[Any, ...], kwargs: dict[str, Any], output: Any
    ) -> None:
        if self.active:
            self._capture("actual.next_normalized", output[0])
            self._capture("actual.next_residual_out", output[1])

    def _next_attention_pre(
        self, module: Any, args: tuple[Any, ...], kwargs: dict[str, Any]
    ) -> None:
        if not self.active:
            return
        bound = self._arguments(module, args, kwargs)
        self._capture("actual.next_attention_input", bound["hidden_states"])
        self.data["records"] = self.records
        self.data["complete"] = True
        self.data["cpu_snapshot_bytes"] = self.resident_bytes
        label = "cold" if self.probe.rid == self.probe.reference_rid else "reused"
        destination = self.root / f"{label}-rank{self.rank}.pt"
        if destination.exists():
            raise ValueError("duplicate post-attention artifact for this request")
        atomic_save(destination, self.data)
        self.records, self.canonical, self.data = {}, {}, {}
        self.active = False


def install_post_attention(
    model: Any, probe: ReuseProbe | None
) -> PostAttentionProbe | None:
    directory = os.environ.get(POST_DIR_ENV)
    if not directory:
        return None
    if probe is None:
        raise ValueError("post-attention tracing requires the existing reuse probe")
    from sglang.srt.runtime_context import get_parallel

    group = get_parallel().attn_tp_group
    if group.rank_in_group != probe.rank or group.world_size != 16:
        raise ValueError("post-attention tracing requires the registered TP16 group")
    trace = PostAttentionProbe(
        probe,
        Path(directory),
        rank=probe.rank,
        world_size=group.world_size,
        reduce_scatter=group.reduce_scatter_tensor,
    )
    trace.install(model)
    return trace
