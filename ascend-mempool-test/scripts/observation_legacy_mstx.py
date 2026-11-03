"""Recover this project's CANN 9.0 ranges from retained device endpoints.

Binary layouts: Ascend/msprof analysis/msparser/data_struct_size_constant.py.
The upstream msmodel/msproftx/msproftx_model.py pre-9.1 branch merges repeated
index IDs. Only host-confirmed mempool start/end ranges are paired here.
"""

from __future__ import annotations

import json
import struct
from collections import defaultdict
from decimal import Decimal
from pathlib import Path
from typing import Any, Iterator


def slices(folder: Path, pattern: str) -> bytes:
    paths = [p for p in folder.glob(pattern) if p.name.rsplit("_", 1)[-1].isdigit()]
    return b"".join(
        p.read_bytes()
        for p in sorted(paths, key=lambda p: int(p.name.rsplit("_", 1)[-1]))
    )


def endpoints(data: bytes) -> Iterator[tuple[int, int, int, int]]:
    offset = 0
    while offset < len(data):
        _, kind, length = struct.unpack_from("<BBH", data, offset)
        if length < 4 or offset + length > len(data):
            raise ValueError("incomplete CANN ts_track record")
        if kind == 10:
            if length != 40:
                raise ValueError("unsupported CANN step-trace layout")
            row = struct.unpack_from("<BBHIQQQHHHH", data, offset)
            if row[9] == 11:
                yield row[5], row[4], row[7], row[8]
        offset += length


def recover_ranges(path: Path, spans: list[dict[str, Any]]) -> list[dict[str, Any]]:
    root = path.parent.parent
    info = root / "profiler_info.json"
    if not info.exists() or not json.loads(info.read_text()).get(
        "cann_version", ""
    ).startswith("9.0."):
        return spans
    candidates: dict[tuple[str, int, Any], list[dict[str, Any]]] = defaultdict(list)
    stream_aliases: dict[int, set[int]] = defaultdict(set)
    for prof in sorted(root.glob("PROF_*")):
        host = prof / "host/data"
        names = {}
        for row in struct.iter_unpack(
            "<HH3IQ2HI2H4Ii5Qi156s", slices(host, "*.additional.msproftx.slice_*")
        ):
            # info_type=1 (extended MSTX), segment=0, event_type=2 (start/end).
            # mark_id at 19 links this host label to device index_id.
            name = row[22].split(b"\0", 1)[0].decode()
            if (
                row[6] == 1
                and row[7] == 0
                and row[14] == 2
                and name.startswith("mempool:")
            ):
                names[row[19]] = name[8:]
        streams = {}
        for row in struct.iter_unpack(
            "<HHIIIQHHHHH30x", slices(host, "*.compact.capture_stream_info.slice_*")
        ):
            # capture_status=0: (device_id, model_stream) -> original_stream.
            if row[6] == 0:
                streams[row[10], row[7]] = row[8]
                stream_aliases[row[7]].add(row[8])
        for device in sorted(prof.glob("device_*")):
            device_id = int(device.name.split("_")[-1])
            groups: dict[int, list[tuple[int, int, int]]] = defaultdict(list)
            for index, tick, stream, task in endpoints(
                slices(device / "data", "ts_track.data.*.slice_*")
            ):
                if index in names:
                    groups[index].append((tick, stream, task))
            if not groups:
                continue
            hardware = json.loads((device / f"info.json.{device_id}").read_text())
            frequency = Decimal(hardware["DeviceInfo"][0]["hwts_frequency"])
            for index, points in groups.items():
                points.sort()
                if len(points) % 2:
                    raise ValueError(
                        f"unpaired CANN 9.0 mempool range index_id={index}"
                    )
                stream = points[0][1]
                if any(p[1] != stream for p in points):
                    raise ValueError(
                        f"ambiguous CANN 9.0 range stream index_id={index}"
                    )
                candidates[names[index], stream, points[0][2]].append(
                    dict(
                        index=index,
                        points=points,
                        frequency=frequency,
                        logical_stream=streams.get((device_id, stream), stream),
                        source=str(device / "data"),
                    )
                )
    output = []
    for span in spans:
        aliases = stream_aliases.get(span["stream"], set())
        if len(aliases) > 1:
            raise ValueError(f"ambiguous captured stream: {span['stream']}")
        if aliases:
            # HCCL device tasks need the same capture mapping as MSTX ranges.
            span = dict(span, logical_stream=next(iter(aliases)))
        choices = candidates.get(
            (span["name"], span["stream"], span.get("task_id")), []
        )
        if not choices:
            output.append(span)
            continue
        if len(choices) != 1:
            raise ValueError(f"ambiguous raw range identity: {span['name']}")
        record = choices[0]
        points = record["points"]
        base: int = points[0][0]
        tick_frequency: Decimal = record["frequency"]

        def delta(tick: int) -> int:
            return round(Decimal(tick - base) * 1000 / tick_frequency)

        # Export supplies the calibrated absolute origin. Raw endpoints supply
        # all replay boundaries; verify the same first-to-last device duration.
        if abs(span["start_ns"] + delta(points[-1][0]) - span["end_ns"]) > 2:
            raise ValueError(f"raw/export device clock mismatch: {span['name']}")
        for start, end in zip(points[::2], points[1::2]):
            output.append(
                dict(
                    span,
                    start_ns=span["start_ns"] + delta(start[0]),
                    end_ns=span["start_ns"] + delta(end[0]),
                    logical_stream=record["logical_stream"],
                    timing_source="CANN9.0_raw_endpoints",
                    raw_index_id=record["index"],
                    raw_source=record["source"],
                )
            )
    return output
