"""Acceptance-only delayed writer test on the live D mapping and TP group."""

from __future__ import annotations

import importlib
from typing import Any

from .decode_write import DecodeWriter
from .manager import MempoolKVManager
from .offload import MempoolKVOffload


def verify_decode_order(
    manager: MempoolKVManager, assignment: str, group: Any
) -> dict[str, Any]:
    """Exercise the local-write -> TP collective -> peer-read dependency in a graph.

    The normal writer stays on the compute stream. This fixture alone moves it
    to a delayed stream, comparing the same graph with and without its event
    wait. It runs inside the ordered registration tick, before admission.
    """
    import torch

    importlib.import_module("sgl_kernel_npu.sparsity_driven_kv_offload")
    session = manager.session
    assert session is not None
    member, members = session.members.index(session.tp_rank), len(session.members)
    writer = DecodeWriter(member, members, assignment)
    layer = members - 1
    view = manager.view(1, layer)
    device = "npu"
    values = (
        torch.arange(view.layout.dim, device=device).to(torch.bfloat16).view(1, 1, -1)
        + 17
    )
    zeros = torch.zeros_like(values)
    slot = torch.tensor([view.layout.slots - 1], device=device)
    position = torch.zeros(1, dtype=torch.int64, device=device)
    valid = torch.tensor([writer.owns_layer(layer)], device=device)
    offload = MempoolKVOffload(view, feature_part=writer.feature_part)
    output = torch.empty_like(values)
    source = slot * view.layout.tokens
    read_valid = torch.ones(1, dtype=torch.bool, device=device)
    dtype = torch.empty(1, dtype=torch.bfloat16, device="cpu")
    signal = torch.zeros(1, dtype=torch.int32, device=device)
    delay_source = torch.ones((2048, 2048), dtype=torch.bfloat16, device=device)
    delay_target = torch.empty_like(delay_source)
    main, write_stream = torch.npu.Stream(), torch.npu.Stream()
    ready, done = torch.npu.Event(), torch.npu.Event()

    def collective() -> None:
        torch.distributed.all_reduce(signal, group=group.device_group)

    def reset() -> None:
        offload.write(zeros, slots=slot, positions=position, valid=valid)
        collective()
        torch.npu.synchronize()

    def launch(wait: bool) -> None:
        ready.record()
        with torch.npu.stream(write_stream):
            write_stream.wait_event(ready)
            if member == members - 1:
                for _ in range(128):
                    torch.mm(delay_source, delay_source, out=delay_target)
            offload.write(values, slots=slot, positions=position, valid=valid)
            done.record()
        if wait:
            main.wait_event(done)
        collective()
        torch.ops.npu.unidex_copy(
            dtype,
            output,
            source,
            position,
            read_valid,
            view.layout.slots * view.layout.tokens,
            1,
            view.layout.row_bytes,
            1,
            48,
            view.device_base,
            None,
        )
        main.wait_event(
            done
        )  # Drain only after the observed read in the negative case.

    results = []
    for wait in (False, True):
        reset()
        with torch.npu.stream(main):
            launch(wait)
        torch.npu.synchronize()
        graph = torch.npu.NPUGraph()
        with torch.npu.graph(graph, stream=main, auto_dispatch_capture=True):
            launch(wait)
        torch.npu.synchronize()
        reset()
        with torch.npu.stream(main):
            graph.replay()
        torch.npu.synchronize()
        actual, expected = output.cpu(), values.cpu()
        matches = int(torch.equal(actual, expected))
        start = (
            view.layout.dim * (members - 1) // members
            if assignment == "features"
            else 0
        )
        peer_matches = int(torch.equal(actual[..., start:], expected[..., start:]))
        flags = torch.tensor([matches, peer_matches], dtype=torch.int32, device=device)
        gathered = group.all_gather(flags).reshape(-1, 2).cpu().tolist()
        all_flags = [row[0] for row in gathered]
        if wait and not all(all_flags):
            raise RuntimeError("shared decode event wait did not protect the peer read")
        if not wait and all(gathered[r][1] for r in session.members[:-1]):
            raise RuntimeError(
                "shared decode negative test did not expose an early read"
            )
        results.append(
            dict(
                wait=wait,
                matched=bool(matches),
                tp_matches=all_flags,
                delayed_slice_matches=[row[1] for row in gathered],
            )
        )
    return dict(
        status="passed",
        engine="graph_replay",
        writer="unidex_copy",
        delayed_member=session.members[-1],
        assignment=assignment,
        negative_without_wait=True,
        positive_with_wait=True,
        cases=results,
    )
