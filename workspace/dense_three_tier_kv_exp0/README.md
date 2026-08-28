# Dense Three-Tier KV Experiment 0

`run_mooncake.sh` starts Mooncake master/store. `run_exp0.sh` starts and
restarts SGLang, then calls `exp0.py` to prepare requests, verify cache sources,
measure TTFT, and summarize three runs.

## Setup

Use the Ascend SGLang environment and install its matching Mooncake package:

```bash
python -m pip install mooncake-transfer-engine-npu==0.3.12.post1
cp mooncake_worker.example.json mooncake_worker.json
cp mooncake_store.example.json mooncake_store.json
```

Edit the top of both shell scripts and replace every `CHANGE_ME` in the JSON
files. Keep `global_segment_size=0` for the SGLang worker and
`640000000000` for the store.

- `local`: worker, store, and master use the same host; run both scripts there.
- `remote`: worker uses the SGLang host, store/master use the second host. Keep
  an identical copy of `mooncake_store.json` beside `run_exp0.sh` for preflight.

## Run

First keep Mooncake running in one terminal on the store machine:

```bash
workspace/dense_three_tier_kv_exp0/run_mooncake.sh
```

Then run the matching stage on the SGLang machine:

```bash
# Step 1: colocated L3
workspace/dense_three_tier_kv_exp0/run_exp0.sh local 64k

# Step 2: L3 on the second machine
workspace/dense_three_tier_kv_exp0/run_exp0.sh remote 64k
```

Results are separated under `results/local/...` and `results/remote/...`.
The 20 GB/rank L2 supports the isolated 64K procedure; 128K is recorded as
`skipped`. Official Ascend MLA uses `kernel_ascend + page_first_kv_split`.
