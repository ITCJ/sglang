"""Ticket03 S2: real BM misses, HBM hits/refill and a reused decode graph."""

from __future__ import annotations

import argparse
import json
import sys
import traceback
from pathlib import Path
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any, Optional, Sequence

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from verify_graph import make_layout
from verify_graph import parse_args as parse_graph_args
from verify_graph import run

from ascend_mempool.control import TestChannel
from ascend_mempool.verification import kv_pattern
from ascend_sparse.fixture import allocate_cache

if TYPE_CHECKING:
    from sglang.srt.hardware_backend.npu.mempool.manager import MempoolKVManager
    from sglang.srt.hardware_backend.npu.mempool.runtime import (
        KVWriteExpectation,
        MempoolRuntime,
    )
else:
    from ascend_mempool.runtime import KVWriteExpectation, MempoolRuntime


def parse_args(argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
    """Match the fixed-width lookup before opening either side's BM pool."""
    args = parse_graph_args(argv, default_topk=2048)
    if args.topk != 2048:
        raise ValueError(
            f"slot_map_lookup requires topk=2048, got {args.topk}; "
            "run the fetch gate with --topk 2048 and pad unused positions with -1"
        )
    return args


def fetch_checks(
    manager: MempoolKVManager,
    args: argparse.Namespace,
    torch: Any,
    channel: TestChannel,
) -> list[dict[str, Any]]:
    """P retains its staged prompt data until D has drained all reads."""
    if manager.rank == 0:
        return []
    results = []
    stream = torch.npu.Stream()
    context = args.s_p + args.s_d
    layers = args.layers
    batch = SimpleNamespace(
        batch_size=args.graph_rows,
        forward_mode=SimpleNamespace(is_decode=lambda: True),
        req_pool_indices=torch.zeros(args.graph_rows, dtype=torch.int64, device="npu"),
        seq_lens=torch.zeros(args.graph_rows, dtype=torch.int64, device="npu"),
        out_cache_loc=torch.zeros(args.graph_rows, dtype=torch.int64, device="npu"),
        extend_seq_lens=None,
        extend_prefix_lens=None,
        extend_seq_lens_cpu=None,
        global_num_token_non_padded_cpu=None,
    )
    positions = torch.full(
        (args.graph_rows, args.topk), -1, dtype=torch.int64, device="npu"
    )
    sources = [
        torch.zeros(
            (args.graph_rows, args.heads, args.kv_dim),
            dtype=torch.bfloat16,
            device="npu",
        )
        for _ in range(layers)
    ]
    graph_outputs = [
        torch.zeros(
            (args.graph_rows, args.topk, args.heads, args.kv_dim),
            dtype=torch.bfloat16,
            device="npu",
        )
        for _ in range(layers)
    ]
    stream.wait_stream(torch.npu.current_stream())

    with torch.npu.stream(stream):
        for block_index, blocks in enumerate(args.block_dims):
            outputs = graph_outputs
            copied = torch.zeros((layers, 2), dtype=torch.int64, device="npu")
            source_ids = {
                manager.view(rank, layer).device_base: (layer, rank)
                for layer in range(layers)
                for rank in (0, 1)
            }

            def measured_copy(*params: Any) -> None:
                copied[source_ids[params[10]]] = params[4].sum()
                torch.ops.npu.unidex_copy(*params)

            runtime = MempoolRuntime(
                manager,
                req_pool_rows=18,
                max_context_len=context,
                start_layer=0,
                device="npu",
                block_dim=blocks,
                fetch_enabled=True,
                fetch_kernel=measured_copy,
            )
            cache = allocate_cache(
                rows=18,
                context=context,
                topk=args.topk,
                layers=layers,
                heads=args.heads,
                dim=args.kv_dim,
                device="npu",
            )

            def launch() -> None:
                for layer in range(layers):
                    split = args.kv_dim // 2
                    values = sources[layer]
                    runtime.write_layer(
                        layer, values[..., :split], values[..., split:], batch
                    )
                    outputs[layer].zero_()
                    cache.materialize_selected_kv(
                        SimpleNamespace(layer_id=layer),
                        batch,
                        positions,
                        outputs[layer],
                        stream,
                        mempool_runtime=runtime,
                    )
                    for event in (
                        cache.hit_done,
                        cache.miss_done,
                        cache.refill_done,
                        cache.slot_map_done,
                    ):
                        stream.wait_event(event)

            # Capture with no bindings and both BM sources masked out. Replay
            # must still fetch real data after the same graph inputs are updated.
            batch.req_pool_indices.zero_()
            batch.seq_lens.zero_()
            positions.fill_(-1)
            for _ in range(args.warmup):
                runtime.begin_forward([], capture=True)
                launch()
                runtime.end_forward()
            torch.npu.synchronize()
            graph = torch.npu.NPUGraph()
            runtime.begin_forward([], capture=True)
            with torch.npu.graph(graph, stream=stream, auto_dispatch_capture=True):
                launch()
            runtime.end_forward()

            for cycle in range(args.replay_cycles + 1):
                mode = "eager" if cycle == 0 else "replay"
                # Eager replaces the destination after capture; the graph must
                # retain its original output and fixed metadata for later replay.
                outputs = (
                    [torch.empty_like(output) for output in graph_outputs]
                    if cycle == 0
                    else graph_outputs
                )
                epoch = -(1 + block_index * (args.replay_cycles + 1) + cycle)
                bindings = []
                p_slots, d_slots = [], []
                batch.req_pool_indices.zero_()
                batch.seq_lens.zero_()
                for index in range(args.active_rows):
                    row = index + 1
                    p_slot, d_slot = (index + 2 + cycle) % 16, (index + 5 + cycle) % 16
                    p_slots.append(p_slot)
                    d_slots.append(d_slot)
                    bindings.append(
                        runtime.bind(
                            row, slot=d_slot, prompt_tokens=4, prompt_slot=p_slot
                        )
                    )
                    batch.req_pool_indices[index] = row
                cache.reset_requests(list(range(1, 17)))
                for offset, (case, selected) in enumerate(
                    (
                        ("p_miss", [0, 3]),
                        ("d_miss", [4, 5]),
                        ("mixed", [0, 3, 4, 6]),
                        ("all_hit", [0, 3, 4, 6]),
                        ("zero_valid", []),
                    )
                ):
                    if case != "all_hit":
                        cache.reset_requests(list(range(1, 17)))
                    positions.fill_(-1)
                    positions[: args.active_rows, : len(selected)] = torch.tensor(
                        selected, dtype=torch.int64, device="npu"
                    )
                    batch.seq_lens[: args.active_rows] = 5 + offset
                    for layer, values in enumerate(sources):
                        values.zero_()
                        for index, slot in enumerate(d_slots):
                            values[index].copy_(
                                kv_pattern(
                                    1, layer, slot, offset, 1, args.heads, args.kv_dim
                                )[0]
                            )
                            # Different from setup and every preceding cycle:
                            # a missing writer dependency cannot pass on old KV.
                            values[index, :, 0] = epoch
                    runtime.begin_forward(
                        [
                            KVWriteExpectation(row + 1, 4 + offset, 1)
                            for row in range(args.active_rows)
                        ],
                        replay=cycle > 0,
                    )
                    if cycle:
                        graph.replay()
                    else:
                        launch()
                    runtime.end_forward()
                    torch.npu.synchronize()
                    runtime.poll_completed()
                    expected_counts = {
                        "p_miss": [2, 0],
                        "d_miss": [0, 2],
                        "mixed": [2, 2],
                        "all_hit": [0, 0],
                        "zero_valid": [0, 0],
                    }[case]
                    counts = copied.cpu().tolist()
                    if (
                        counts
                        != [[count * args.active_rows for count in expected_counts]]
                        * layers
                    ):
                        raise AssertionError(
                            f"unexpected BM miss counts case={case}: {counts}"
                        )
                    verified = 0
                    for layer, output in enumerate(outputs):
                        actual = output.cpu()
                        expected = torch.zeros_like(actual)
                        for row, (p_slot, d_slot) in enumerate(zip(p_slots, d_slots)):
                            for column, position in enumerate(selected):
                                rank, slot, token = (
                                    (0, p_slot, position)
                                    if position < 4
                                    else (1, d_slot, position - 4)
                                )
                                expected[row, column] = kv_pattern(
                                    rank, layer, slot, token, 1, args.heads, args.kv_dim
                                )[0]
                                if rank == 1:
                                    expected[row, column, :, 0] = epoch
                        if not torch.equal(actual, expected):
                            raise AssertionError(
                                f"fetch mismatch mode={mode} case={case} layer={layer}"
                            )
                        verified += actual.numel()
                    result = dict(
                        mode=mode,
                        cycle=cycle,
                        case=case,
                        block_dim=blocks,
                        decode_epoch=epoch,
                        verified_elements=verified,
                        copied_per_layer=counts,
                    )
                    results.append(result)
                    print(f"[D] FETCH_PASS {json.dumps(result)}", flush=True)
                for binding in bindings:
                    runtime.detach_row(binding)
                cache.reset_requests(list(range(1, 17)))
            torch.npu.synchronize()
            del graph
    return results


def main() -> int:
    try:
        args = parse_args()
        if args.s_p < 4 or args.s_d < 5:
            raise ValueError("fetch gate requires --s-p >= 4 and --s-d >= 5")
        if len(args.block_dims) * (args.replay_cycles + 1) > 256:
            raise ValueError("fetch gate requires at most 256 distinct BF16 epochs")
        layout = make_layout(args)
        if args.describe:
            print(json.dumps(layout.signature(), indent=2))
        else:
            run(args, layout, paired_checks=fetch_checks)
        return 0
    except Exception:
        traceback.print_exc()
        return 1


if __name__ == "__main__":
    sys.exit(main())
