"""Build (do not run) a staged copy of the known-working col.sh."""

import argparse
import hashlib
from pathlib import Path


parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument("--source", required=True)
parser.add_argument("--stage", type=int, choices=range(7), required=True)
parser.add_argument("--output", required=True)
args = parser.parse_args()
source = Path(args.source).resolve()
text = source.read_text()
original = text
changes = [
    ("--context-length 256", "--context-length 2816"),
    ("--max-prefill-tokens 512", "--max-prefill-tokens 2048"),
    ("--max-running-requests 4", "--max-running-requests 11"),
    ("--disable-cuda-graph", "--disable-cuda-graph --enable-profile-cuda-graph"),
    ("--dtype bfloat16", "--dtype bfloat16 --kv-cache-dtype bfloat16"),
    ("--disable-cuda-graph", "--cuda-graph-bs 1 11"),
]
for before, after in changes[:args.stage]:
    if text.count(before) != 1:
        raise SystemExit(f"Expected exactly one {before!r} in {source}; inspect your working script")
    text = text.replace(before, after, 1)
Path(args.output).write_text(text)
print(f"source={source} sha256={hashlib.sha256(original.encode()).hexdigest()}")
print(f"stage={args.stage}; generated={args.output}")
for before, after in changes[:args.stage]:
    print(f"  {before} -> {after}")
