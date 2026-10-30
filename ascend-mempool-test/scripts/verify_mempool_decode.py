"""Validate real shared decode writes, previous-row reads, and Graph receipts."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from sglang.srt.disaggregation.ascend.mempool_router_protocol import RouterConfig
else:
    from ascend_mempool_pd.mempool_router_protocol import RouterConfig


def check_completion(
    data: dict[str, Any], rank: int, config: RouterConfig
) -> dict[str, Any]:
    group = next(g for g in config.decode_groups if rank in g)
    member, size = group.index(rank), len(group)
    proof = data["shared_decode"]
    steps, layers = data["forwards"], data["layers"]
    row_bytes = 1152
    owned_bytes = (
        layers * row_bytes // size
        if config.decode_write_assignment == "features"
        else len(range(member, layers, size)) * row_bytes
    )
    if (
        proof["assignment"] != config.decode_write_assignment
        or proof["member"] != member
        or proof["members"] != size
        or not proof["verified"]
        or proof["ordering"] != "compute_stream_before_post_attention_tp_collective"
        or proof["checked_rows"] != max(0, steps - 1) * layers
        or proof["written_bytes"] != steps * owned_bytes
    ):
        raise ValueError(
            "shared decode write coverage or previous-row checks incomplete"
        )
    if steps and not data["replay_forwards"]:
        raise ValueError("shared decode lacks real Graph replay")
    return dict(rank=rank, steps=steps, **proof)


def check_stage(root: Path, config: RouterConfig) -> dict[str, Any]:
    snapshots = json.loads((root / "snapshots.json").read_text())
    path = root / "decode-normal.log"
    if not path.exists():
        path = root / "decode.log"
    receipts = {}
    for line in path.read_text(errors="replace").splitlines():
        if "mempool decode_completion " not in line:
            continue
        fields = dict(re.findall(r"\b(rank|room)=(\d+)", line))
        key = int(fields["rank"]), int(fields["room"])
        if key in receipts:
            raise ValueError("duplicate shared decode receipt")
        receipts[key] = json.loads(line.split(" data=", 1)[1])
    results = []
    for snapshot in snapshots:
        done = snapshot["completed"][-1]
        for group in config.decode_groups:
            pages = [done["allocations"]["decode"][rank]["pages"] for rank in group]
            if any(p != pages[0] for p in pages):
                raise ValueError("D writers have different shared page descriptors")
        for rank in range(16):
            results.append(check_completion(receipts[rank, done["room"]], rank, config))
    if not results or not sum(r["checked_rows"] for r in results):
        raise ValueError("shared decode never read a previous token")
    return dict(
        status="passed",
        requests=len(snapshots),
        receipts=results,
        earliest_previous_position="prompt_len at decode step 1",
        read_scope="every previous token, all layers, independent of top-k",
    )
