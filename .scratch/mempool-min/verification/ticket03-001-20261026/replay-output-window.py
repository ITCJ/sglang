"""Replay the reported strict-output failure; this does not run the NPU model."""

import json
from itertools import zip_longest
from pathlib import Path

record = json.loads(Path(__file__).with_name("diagnostic-output.json").read_text())
case = next(item for item in record["output_comparisons"] if "reference_window" in item)
first = next(
    (index for index, (left, right) in enumerate(zip_longest(
        case["reference_window"], case["target_window"]
    )) if left != right),
    None,
)
if first is not None:
    position = case["window_start"] + first
    raise SystemExit(
        f"FAIL {case['case']}: output[{position}], "
        f"reference={case['reference_window'][first]}, "
        f"target={case['target_window'][first]} (captured window replay only)"
    )
print("PASS captured output window equality (not full-output/NPU acceptance)")
