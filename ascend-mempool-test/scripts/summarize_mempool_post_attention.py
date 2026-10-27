"""Align ticket03 operator snapshots by logical token, never relax acceptance."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

import torch
from torch import Tensor

ACTUAL = (
    "residual_full",
    "pre_reduce",
    "reduced",
    "residual_in",
    "normalized",
    "residual_out",
    "mlp_input",
    "gate_up",
    "down_input",
    "mlp_output",
    "layer0_output",
    "layer0_residual",
    "layer1_hidden",
    "layer1_residual",
    "next_norm_hidden",
    "next_norm_residual",
    "next_normalized",
    "next_residual_out",
    "next_attention_input",
)
REPLICATED = {"residual_full", "pre_reduce", "next_attention_input"}
LOCAL_POINTS = (
    "reduced",
    "residual_in",
    "normalized",
    "residual_out",
    "mlp_input",
    "gate_up",
    "down_input",
    "mlp_output",
)
REPLAY_POINTS = {
    "norm": ("normalized", "residual_out"),
    "mlp": ("input", "gate_up", "down_input", "output"),
}
EDGES = (
    ("residual_full", "residual_in"),
    ("normalized", "mlp_input"),
    ("residual_out", "layer0_residual"),
    ("mlp_output", "layer0_output"),
    ("layer0_output", "layer1_hidden"),
    ("layer0_residual", "layer1_residual"),
    ("layer1_hidden", "next_norm_hidden"),
    ("layer1_residual", "next_norm_residual"),
    ("next_normalized", "next_attention_input"),
)


def difference(left: Tensor, right: Tensor, positions: list[int]) -> dict[str, Any]:
    if left.shape != right.shape or left.ndim != 2 or left.shape[0] != len(positions):
        raise ValueError("comparison has incompatible tensor shapes")
    a, b = left.float(), right.float()
    finite = torch.isfinite(a) & torch.isfinite(b)
    changed = left != right
    delta = torch.where(finite, (a - b).abs(), 0)
    first = changed.nonzero()[:1].tolist()
    return dict(
        elements=left.numel(),
        changed=int(changed.sum()),
        nonfinite=int((~finite).sum()),
        max_abs=float(delta.max()) if delta.numel() else 0.0,
        rms=float(delta.square().mean().sqrt()) if delta.numel() else 0.0,
        first_difference=[positions[first[0][0]], first[0][1]] if first else None,
        rows=[
            dict(
                token=p,
                changed=int(changed[i].sum()),
                nonfinite=int((~finite[i]).sum()),
                max_abs=float(delta[i].max()),
                rms=float(delta[i].square().mean().sqrt()),
            )
            for i, p in enumerate(positions)
        ],
    )


def equal(stats: dict[str, Any]) -> bool:
    return bool(stats["changed"] == 0 and stats["nonfinite"] == 0)


def short(stats: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in stats.items() if k != "rows"}


def _record(row: dict[str, Any], name: str, positions: list[int]) -> Tensor:
    record = row["records"][name]
    if record["positions"] != positions:
        raise ValueError(f"rank {row['rank']} {name}: logical positions differ")
    value = record["values"]
    if (
        not isinstance(value, Tensor)
        or value.ndim != 2
        or value.shape[0] != len(positions)
    ):
        raise ValueError(f"rank {row['rank']} {name}: invalid tensor")
    return value


def _local_positions(row: dict[str, Any]) -> list[int]:
    positions = row["positions"]
    width = len(positions) // row["world_size"]
    start = row["rank"] * width
    tail = row["prompt_tokens"] - row["page_size"]
    return [p for p in positions[start : start + width] if p >= tail]


def _merge(
    rows: list[dict[str, Any]], name: str, tail: list[int], *, replicated: bool = False
) -> Tensor:
    if replicated:
        values = [_record(row, name, tail) for row in rows]
        if not all(torch.equal(values[0], value) for value in values[1:]):
            raise ValueError(f"{name}: replicated full rows differ across ranks")
        return values[0]
    pairs: dict[int, Tensor] = {}
    for row in rows:
        record = row["records"][name]
        positions = record["positions"]
        if name.startswith("actual.") and name[7:] in LOCAL_POINTS:
            if positions != _local_positions(row):
                raise ValueError(f"{name}: unexpected rank owner")
        values = _record(row, name, positions)
        for p, value in zip(positions, values):
            if p in pairs:
                raise ValueError(f"{name}: duplicate logical token {p}")
            pairs[p] = value
    if sorted(pairs) != tail:
        raise ValueError(f"{name}: missing or out-of-range logical rows")
    return torch.stack([pairs[p] for p in tail])


def _validate(rows: dict[str, list[dict[str, Any]]], ranks: int) -> list[int]:
    first = rows["cold"][0]
    prompt, page = first["prompt_tokens"], first["page_size"]
    if page <= 0 or prompt % ranks or page % ranks or prompt // ranks < page:
        raise ValueError("diagnostic requires a complete tail on the cold final rank")
    tail = list(range(prompt - page, prompt))
    for label, group in rows.items():
        hit = 0 if label == "cold" else prompt - page
        for rank, row in enumerate(group):
            if (
                row.get("schema"),
                row.get("rank"),
                row.get("world_size"),
                row.get("complete"),
            ) != (1, rank, ranks, True):
                raise ValueError(
                    f"{label} rank {rank}: schema/rank/completeness mismatch"
                )
            if any(
                row.get(key) != first.get(key)
                for key in (
                    "input_sha256",
                    "reference_rid",
                    "prompt_tokens",
                    "page_size",
                )
            ):
                raise ValueError(f"{label} rank {rank}: request identity mismatch")
            if (
                not isinstance(row["input_sha256"], str)
                or len(row["input_sha256"]) != 64
            ):
                raise ValueError("invalid input identity hash")
            if row.get("hit_len") != hit or row.get("positions") != list(
                range(hit, prompt)
            ):
                raise ValueError(
                    f"{label} rank {rank}: request position mapping mismatch"
                )
            if row["rid"] != group[0]["rid"] or (
                row["rid"] == row["reference_rid"]
            ) != (label == "cold"):
                raise ValueError(f"{label} rank {rank}: cold/reused identity mismatch")
            if not row["weights"].get("tensors") or not row["weights"].get("settings"):
                raise ValueError("missing weights or implementation settings")
            expected = {"actual." + name for name in ACTUAL} | {"reduce.same_shape"}
            if label == "cold":
                expected |= {"reduce.tail", "reduce.rotated"}
                expected |= {
                    f"{op}.{kind}.{point}"
                    for op, points in REPLAY_POINTS.items()
                    for kind in ("full", "repeat", "chunks")
                    for point in points
                }
                if set(row["canonical_inputs"]) != {"hidden", "residual", "normalized"}:
                    raise ValueError("missing canonical input hashes")
            if set(row["records"]) != expected:
                raise ValueError(
                    f"{label} rank {rank}: missing/extra checkpoints {sorted(expected ^ set(row['records']))}"
                )
            for name in expected:
                record = row["records"][name]
                values = _record(row, name, record["positions"])
                if len(set(record["positions"])) != len(record["positions"]) or not set(
                    record["positions"]
                ) <= set(tail):
                    raise ValueError(f"{name}: duplicate or out-of-range positions")
                if name.startswith("actual."):
                    actual_rows = (
                        prompt - hit
                        if name[7:] in REPLICATED
                        else (prompt - hit) // ranks
                    )
                    if record["input_rows"] != actual_rows:
                        raise ValueError(f"{name}: unexpected operator row count")
                elif name.startswith(("norm.", "mlp.")):
                    size = page // ranks if ".chunks." in name else prompt // ranks
                    if record["positions"] != tail or record["input_rows"] != size:
                        raise ValueError(f"{name}: invalid controlled replay shape")
                if values.dtype != first["records"].get(name, record)["values"].dtype:
                    raise ValueError(f"{name}: dtype differs across cases/ranks")
            marker = row["mapping_marker"]
            if marker["rows"] != prompt - hit or marker["bits"] != max(
                1, prompt.bit_length()
            ):
                raise ValueError("logical-row marker used a different shape")
    return tail


def _state_differences(left: dict[str, Any], right: dict[str, Any]) -> list[str]:
    return [
        key for key in sorted(set(left) | set(right)) if left.get(key) != right.get(key)
    ]


def analyze(rows: dict[str, list[dict[str, Any]]], ranks: int) -> dict[str, Any]:
    tail = _validate(rows, ranks)
    cold, reused = rows["cold"], rows["reused"]
    actual: dict[str, dict[str, Tensor]] = {}
    mapping: dict[str, Any] = {}
    for label, group in rows.items():
        actual[label] = {
            name: _merge(group, "actual." + name, tail, replicated=name in REPLICATED)
            for name in ACTUAL
            if name != "pre_reduce"
        }
        mapping[label] = {
            f"{a}->{b}": short(difference(actual[label][a], actual[label][b], tail))
            for a, b in EDGES
        }
        mapping[label]["markers"] = [row["mapping_marker"] for row in group]
    boundaries = {
        name: difference(actual["cold"][name], actual["reused"][name], tail)
        for name in actual["cold"]
    }
    pre_reduce = [
        difference(
            _record(a, "actual.pre_reduce", tail),
            _record(c, "actual.pre_reduce", tail),
            tail,
        )
        for a, c in zip(cold, reused)
    ]
    reduced: dict[str, Tensor] = {}
    for label, group in rows.items():
        for name in (
            ("same_shape", "tail", "rotated") if label == "cold" else ("same_shape",)
        ):
            expected_rows = len(group[0]["positions"]) if name != "tail" else len(tail)
            for row in group:
                if (
                    row["records"]["reduce." + name]["input_rows"]
                    != expected_rows // ranks
                ):
                    raise ValueError("reduction replay has the wrong local shape")
                source = row["positions"]
                if name == "tail":
                    source = tail
                elif name == "rotated":
                    source = source[-len(tail) :] + source[: -len(tail)]
                width = len(source) // ranks
                expected = [
                    p
                    for p in source[row["rank"] * width : (row["rank"] + 1) * width]
                    if p in tail
                ]
                _record(row, "reduce." + name, expected)
            reduced[label + "." + name] = _merge(group, "reduce." + name, tail)
    reduce_stats = {
        "cold_repeat": difference(
            actual["cold"]["reduced"], reduced["cold.same_shape"], tail
        ),
        "reused_repeat": difference(
            actual["reused"]["reduced"], reduced["reused.same_shape"], tail
        ),
        "full_vs_tail": difference(
            reduced["cold.same_shape"], reduced["cold.tail"], tail
        ),
        "full_vs_rotated": difference(
            reduced["cold.same_shape"], reduced["cold.rotated"], tail
        ),
        "tail_vs_reused": difference(
            reduced["cold.tail"], actual["reused"]["reduced"], tail
        ),
    }
    for label, group in rows.items():
        total = torch.stack(
            [_record(row, "actual.pre_reduce", tail).float() for row in group]
        ).sum(0)
        reduce_stats[label + "_vs_cpu_fp32_sum"] = difference(
            total, actual[label]["reduced"].float(), tail
        )
    weights = []
    for a, c in zip(cold, reused):
        reference = cold[-1]["weights"]
        weights.append(
            dict(
                rank=a["rank"],
                **{
                    phase + "_" + scope: _state_differences(left[scope], right[scope])
                    for phase, left, right in (
                        ("ac", a["weights"], c["weights"]),
                        ("rank", reference, a["weights"]),
                    )
                    for scope in ("tensors", "settings")
                },
            )
        )
    weights_ok = all(
        not value for row in weights for key, value in row.items() if key != "rank"
    )
    canonical_ok = all(
        row["canonical_inputs"] == cold[-1]["canonical_inputs"] for row in cold
    )
    if not canonical_ok:
        raise ValueError("controlled replay input hashes differ across ranks")
    local = []
    for row in cold:
        comparisons = {}
        for op, points in REPLAY_POINTS.items():
            for point in points:
                base = _record(row, f"{op}.full.{point}", tail)
                comparisons[f"{op}.{point}.repeat"] = difference(
                    base, _record(row, f"{op}.repeat.{point}", tail), tail
                )
                comparisons[f"{op}.{point}.shape"] = difference(
                    base, _record(row, f"{op}.chunks.{point}", tail), tail
                )
                for kind in ("full", "chunks"):
                    comparisons[f"{op}.{point}.rank_{kind}"] = difference(
                        _record(cold[-1], f"{op}.{kind}.{point}", tail),
                        _record(row, f"{op}.{kind}.{point}", tail),
                        tail,
                    )
        local.append(dict(rank=row["rank"], comparisons=comparisons))
    faithful = {
        name: difference(actual["cold"][point], _record(cold[-1], name, tail), tail)
        for name, point in (
            ("norm.full.normalized", "normalized"),
            ("norm.full.residual_out", "residual_out"),
            ("mlp.full.input", "mlp_input"),
            ("mlp.full.output", "mlp_output"),
        )
    }
    mapping_ok = all(
        equal(stats)
        for case in mapping.values()
        for name, stats in case.items()
        if name != "markers"
    ) and all(
        m["changed"] == 0 and m["nonzero_padding"] == 0
        for case in mapping.values()
        for m in case["markers"]
    )
    numeric = (
        list(boundaries.values())
        + pre_reduce
        + list(reduce_stats.values())
        + list(faithful.values())
    )
    numeric += [stats for row in local for stats in row["comparisons"].values()]
    nonfinite = sum(s["nonfinite"] for s in numeric)
    repeat_ok = all(
        equal(s)
        for row in local
        for name, s in row["comparisons"].items()
        if name.endswith(".repeat")
    )
    repeat_ok = (
        repeat_ok
        and equal(reduce_stats["cold_repeat"])
        and equal(reduce_stats["reused_repeat"])
    )
    faithful_ok = all(equal(s) for s in faithful.values())
    tokens = []
    for token in dict.fromkeys(
        [tail[0], tail[min(93, len(tail) - 1)], tail[-min(6, len(tail))]]
    ):
        index = tail.index(token)
        tokens.append(
            dict(
                token=token,
                cold_owner=ranks - 1,
                reused_owner=index // (len(tail) // ranks),
                pre_reduce_changed_by_rank=[
                    s["rows"][index]["changed"] for s in pre_reduce
                ],
                boundaries={name: s["rows"][index] for name, s in boundaries.items()},
                reductions={name: s["rows"][index] for name, s in reduce_stats.items()},
            )
        )
    return dict(
        purpose="post-attention diagnosis; not hardware acceptance",
        complete=True,
        checks_ok=mapping_ok
        and weights_ok
        and not nonfinite
        and repeat_ok
        and faithful_ok,
        errors=[],
        mapping_ok=mapping_ok,
        weights_ok=weights_ok,
        canonical_inputs_ok=canonical_ok,
        replay_repeat_ok=repeat_ok,
        replay_matches_actual=faithful_ok,
        nonfinite=nonfinite,
        first_changed_boundary=next(
            (
                name
                for name in (
                    "residual_full",
                    "reduced",
                    "residual_in",
                    "residual_out",
                    "normalized",
                    "mlp_input",
                    "gate_up",
                    "down_input",
                    "mlp_output",
                    "next_norm_hidden",
                    "next_norm_residual",
                    "next_normalized",
                    "next_attention_input",
                )
                if not equal(boundaries[name])
            ),
            None,
        ),
        boundaries=boundaries,
        pre_reduce=pre_reduce,
        reductions=reduce_stats,
        mapping=mapping,
        weights=weights,
        implementation_settings=cold[-1]["weights"]["settings"],
        local_replays=local,
        replay_vs_actual=faithful,
        tokens=tokens,
    )


def summarize(root: Path, *, ranks: int = 16) -> dict[str, Any]:
    report: dict[str, Any] = dict(
        purpose="post-attention diagnosis; not hardware acceptance",
        complete=False,
        checks_ok=False,
        errors=[],
    )
    try:
        errors = sorted(root.glob("error-rank*.json"))
        if errors:
            # Retain every rank record, but print an identical failure only once.
            # A concatenated traceback otherwise gets cut off by compact().
            records = [json.loads(path.read_text()) for path in errors]
            report["probe_errors"] = records
            groups: dict[tuple[str, str], list[int]] = {}
            for record in records:
                key = (record["hook"], record["error"])
                groups.setdefault(key, []).append(record["rank"])
            report["errors"] = [
                f"probe errors: hook={hook}; ranks={sorted(ranks)}; {error}"
                for (hook, error), ranks in groups.items()
            ]
            return report
        expected = {
            f"{label}-rank{rank}.pt"
            for label in ("cold", "reused")
            for rank in range(ranks)
        }
        found = {
            path.name
            for label in ("cold", "reused")
            for path in root.glob(label + "-rank*.pt")
        }
        if found != expected:
            raise ValueError(f"artifact coverage differs: {sorted(found ^ expected)}")
        rows = {
            label: [
                torch.load(
                    root / f"{label}-rank{rank}.pt",
                    map_location="cpu",
                    weights_only=True,
                )
                for rank in range(ranks)
            ]
            for label in ("cold", "reused")
        }
        report.update(analyze(rows, ranks))
    except Exception as exc:
        report["errors"].append(f"{type(exc).__name__}: {exc}")
    return report


def compact(report: dict[str, Any]) -> dict[str, Any]:
    result = {
        key: report[key] for key in ("purpose", "complete", "checks_ok", "errors")
    }
    result["errors"] = [str(error)[:2000] for error in result["errors"][:4]]
    if not report["complete"]:
        return result
    result.update(
        {
            key: report[key]
            for key in (
                "mapping_ok",
                "weights_ok",
                "canonical_inputs_ok",
                "replay_repeat_ok",
                "replay_matches_actual",
                "nonfinite",
                "first_changed_boundary",
            )
        }
    )
    result["reductions"] = {
        key: short(value) for key, value in report["reductions"].items()
    }
    result["local_effects"] = {
        op + "_" + effect: [
            row["rank"]
            for row in report["local_replays"]
            if any(
                not equal(stats)
                for name, stats in row["comparisons"].items()
                if name.startswith(op + ".") and name.endswith("." + effect)
            )
        ]
        for op in REPLAY_POINTS
        for effect in ("shape", "rank_full", "rank_chunks")
    }
    result["weights_different"] = [
        dict(
            rank=r["rank"],
            **{
                key: dict(count=len(value), first=value[:3])
                for key, value in r.items()
                if key != "rank" and value
            },
        )
        for r in report["weights"]
        if any(v for k, v in r.items() if k != "rank")
    ]
    return result


def print_summary(report: dict[str, Any]) -> None:
    print("REUSE_POST " + json.dumps(compact(report)), flush=True)
    for row in report.get("tokens", []):
        print("REUSE_POST_TOKEN " + json.dumps(row), flush=True)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("root", type=Path)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    report = summarize(args.root)
    if args.output:
        args.output.write_text(json.dumps(report, indent=2) + "\n")
    print_summary(report)
    return 0 if report["complete"] and report["checks_ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
