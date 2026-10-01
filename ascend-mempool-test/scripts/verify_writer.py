"""Two-machine runtime writes, remote BF16 readback and decode graph replay."""

from __future__ import annotations

import argparse
import json
import sys
import traceback
from pathlib import Path
from typing import TYPE_CHECKING, Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from verify_graph import make_layout, parse_args, run

from ascend_mempool.control import TestChannel
from ascend_mempool.copy import SparseCopyInputs, SparseKVCopy
from ascend_mempool.writer_cases import (
    WRITER_SENTINEL,
    WriterCase,
    decode_cases,
    prefill_cases,
)

if TYPE_CHECKING:
    from sglang.srt.hardware_backend.npu.mempool.manager import MempoolKVManager
    from sglang.srt.hardware_backend.npu.mempool.runtime import (
        KVRowBinding,
        MempoolRuntime,
    )
else:
    from ascend_mempool.pool import MempoolKVManager
    from ascend_mempool.runtime import KVRowBinding, MempoolRuntime


def stage_sentinel(manager: MempoolKVManager, args: argparse.Namespace) -> None:
    """Fill local logical storage, including slots never targeted by a write."""
    import torch

    layout = manager.layout.layout_for_rank(manager.rank)
    values = torch.full(
        (layout.tokens, layout.heads, layout.dim),
        WRITER_SENTINEL,
        dtype=torch.bfloat16,
        device="npu",
    )
    for layer in range(layout.layers):
        for slot in range(layout.slots):
            manager.view(manager.rank, layer).write_rows(slot, 0, values)


def load_decode_case(case: WriterCase, batch: Any, sources: list[Any]) -> None:
    """Update fixed inputs on the same stream that submits the captured graph."""
    fields = case.batch("cpu")
    batch.req_pool_indices.copy_(fields.req_pool_indices)
    batch.seq_lens.copy_(fields.seq_lens)
    batch.out_cache_loc.copy_(fields.out_cache_loc)
    for layer, source in enumerate(sources):
        source.copy_(case.values(layer, source.shape[-1]))


def submit_layers(runtime: MempoolRuntime, batch: Any, sources: list[Any]) -> None:
    """Use the public per-layer hook with temporary latent KV and RoPE keys."""
    for layer, values in enumerate(sources):
        split = values.shape[-1] // 2
        runtime.write_layer(layer, values[..., :split], values[..., split:], batch)


def remote_copies(
    manager: MempoolKVManager, owner: int, blocks: int
) -> list[SparseKVCopy]:
    """Read every logical row of the peer using ticket01's two-source fetch."""
    import torch

    layout = manager.layout.layout_for_rank(owner)
    inputs = SparseCopyInputs(16, layout.tokens, "npu")
    inputs.p_slots.copy_(torch.arange(16, device="npu"))
    inputs.d_slots.copy_(torch.arange(16, device="npu"))
    inputs.prompt_lengths.fill_(manager.layout.prompt.tokens)
    inputs.decode_lengths.fill_(manager.layout.decode.tokens)
    base = manager.layout.prompt.tokens if owner == 1 else 0
    inputs.positions.copy_(torch.arange(layout.tokens, device="npu") + base)
    inputs.active.fill_(True)
    inputs.valid.fill_(True)
    return [
        SparseKVCopy(
            manager.view(0, layer), manager.view(1, layer), inputs, block_dim=blocks
        )
        for layer in range(layout.layers)
    ]


def verify_remote(copies: list[SparseKVCopy], references: list[Any], torch: Any) -> int:
    """Synchronize peer reads and compare full contents, including untouched rows."""
    for copy in copies:
        copy.output.fill_(WRITER_SENTINEL)
        copy.gather()
    torch.npu.synchronize()
    elements = 0
    for layer, (copy, reference) in enumerate(zip(copies, references)):
        actual = copy.output.cpu()
        if not torch.equal(actual, reference):
            index = tuple(actual.ne(reference).nonzero()[0].tolist())
            raise AssertionError(
                f"remote writer mismatch layer={layer} index={index} "
                f"actual={actual[index].item()} expected={reference[index].item()}"
            )
        elements += actual.numel()
    return elements


def paired_writer_checks(
    manager: MempoolKVManager,
    args: argparse.Namespace,
    torch: Any,
    channel: TestChannel,
) -> list[dict[str, Any]]:
    """P writes prompts/D reads; D writes decode graphs/P reads remote contents."""
    results = []
    stream = torch.npu.Stream()
    with torch.npu.stream(stream):
        for blocks in args.block_dims:
            stage_sentinel(manager, args)
            channel.send("SENTINEL_READY", blocks=blocks)
            channel.expect("SENTINEL_READY")
            for owner in (0, 1):
                layout = manager.layout.layout_for_rank(owner)
                cases = (
                    prefill_cases(layout.tokens)
                    if owner == 0
                    else decode_cases(layout.tokens)
                )
                references = [
                    torch.full(layout.shape, WRITER_SENTINEL, dtype=torch.bfloat16)
                    for _ in range(layout.layers)
                ]
                runtime = None
                graph = None
                sources: list[Any] = []
                batch = None
                bound: dict[int, KVRowBinding] = {}
                copies = []
                if manager.rank == owner:
                    runtime = MempoolRuntime(
                        manager,
                        req_pool_rows=18,
                        max_context_len=manager.layout.prompt.tokens
                        + manager.layout.decode.tokens
                        + 1,
                        start_layer=0,
                        device="npu",
                        block_dim=blocks,
                    )
                    if owner == 1:
                        dummy = cases[0]
                        batch = dummy.batch("npu")
                        sources = [
                            dummy.values(layer, layout.dim).to("npu")
                            for layer in range(layout.layers)
                        ]
                        for _ in range(args.warmup):
                            runtime.begin_forward([])
                            submit_layers(runtime, batch, sources)
                            runtime.end_forward()
                            torch.npu.synchronize()
                            runtime.poll_completed()
                        torch.npu.synchronize()
                        graph = torch.npu.NPUGraph()
                        runtime.begin_forward([], capture=True)
                        with torch.npu.graph(
                            graph, stream=stream, auto_dispatch_capture=True
                        ):
                            submit_layers(runtime, batch, sources)
                        runtime.end_forward()
                else:
                    copies = remote_copies(manager, owner, blocks)

                for case in cases:
                    for layer, reference in enumerate(references):
                        case.update_reference(reference, layer)
                    if runtime is not None:
                        if case.reset_bindings:
                            for binding in bound.values():
                                runtime.detach_row(binding)
                            bound.clear()
                        for row, slot, prompt in case.bindings:
                            bound[row] = runtime.bind(
                                row, slot=slot, prompt_tokens=prompt
                            )
                        if graph is not None and case.decode:
                            load_decode_case(case, batch, sources)
                            runtime.begin_forward(case.writes, replay=True)
                            graph.replay()
                        else:
                            runtime.begin_forward(case.writes)
                            eager_sources = [
                                case.values(layer, layout.dim).to("npu")
                                for layer in range(layout.layers)
                            ]
                            submit_layers(runtime, case.batch("npu"), eager_sources)
                        runtime.end_forward()
                        torch.npu.synchronize()
                        runtime.poll_completed()
                        if not all(runtime.writes_done(row) for row in bound):
                            raise AssertionError(
                                "runtime did not observe completed writes"
                            )
                        if owner == 0 and case.name != "ragged_tail_unbound":
                            if not all(runtime.prompt_ready(row) for row in bound):
                                raise AssertionError(
                                    "complete prompt was not reported ready"
                                )
                        channel.send(
                            "WRITE_READY", case=case.name, owner=owner, blocks=blocks
                        )
                        verified = channel.expect("VERIFIED")
                        elements = verified["elements"]
                    else:
                        ready = channel.expect("WRITE_READY")
                        if (
                            ready.get("case"),
                            ready.get("owner"),
                            ready.get("blocks"),
                        ) != (case.name, owner, blocks):
                            raise RuntimeError(
                                "writer gate case order differs between peers"
                            )
                        elements = verify_remote(copies, references, torch)
                        channel.send("VERIFIED", case=case.name, elements=elements)
                    mode = "replay" if owner == 1 and case.decode else "eager"
                    results.append(
                        dict(
                            owner=owner,
                            mode=mode,
                            case=case.name,
                            block_dim=blocks,
                            verified_elements=elements,
                        )
                    )
                    print(
                        f"[rank {manager.rank}] PASS {mode} owner={owner} blocks={blocks} "
                        f"case={case.name} elements={elements}",
                        flush=True,
                    )
                torch.npu.synchronize()
                del graph, runtime, sources
    torch.npu.synchronize()
    # Both roles perform remote reads in this gate; require bilateral drain.
    channel.send("WRITER_GATE_DRAINED")
    channel.expect("WRITER_GATE_DRAINED")
    return results


def main() -> int:
    """Use the existing BM/SDK environment gate and paired teardown protocol."""
    try:
        args = parse_args(
            [
                "--s-p",
                "8",
                "--s-d",
                "16",
                "--kv-dim",
                "8",
                "--topk",
                "8",
                *sys.argv[1:],
            ]
        )
        if args.heads != 1 or args.graph_rows != 16 or args.s_p < 6 or args.s_d < 2:
            raise ValueError(
                "writer gate requires MLA, 16 graph rows, S_P>=6 and S_D>=2"
            )
        if args.kv_dim % 2:
            raise ValueError("writer gate requires an even compact KV dimension")
        layout = make_layout(args)
        if args.describe:
            print(json.dumps(layout.signature(), indent=2))
        else:
            run(args, layout, stage=stage_sentinel, paired_checks=paired_writer_checks)
        return 0
    except Exception:
        traceback.print_exc()
        return 1


if __name__ == "__main__":
    sys.exit(main())
