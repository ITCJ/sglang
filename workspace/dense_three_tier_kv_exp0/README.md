# Dense Three-Tier KV Experiment 0

## Current integration gate (Fabric, TP16+DP1)

The original formal experiment below remains RDMA/TP16+DPA16 and is not yet
validated on this deployment. First validate actual HiCache integration with
the working DP1 model configuration. This gate enables all three tiers but does
not certify cache-hit provenance, 64K capacity, or experimental latency results.

On the second node, inside the prepared container:

```bash
python3 workspace/setup/check_fabric_pair.py target --local-ip <store-IP>
```

Wait for `F2:READY`. On the first node, stop its previous model server before
launching the integration server:

```bash
mkdir -p /tmp/hicache-check
bash workspace/dense_three_tier_kv_exp0/check_stack.sh \
  '<model-path>' '<worker-IP>' '<store-IP>' \
  > /tmp/hicache-check/server.log 2>&1 &
bash workspace/setup/check_model.sh --suite
```

This uses an independent 1 GiB Store, 2 GiB HiCache per rank, and zero L3
contribution from SGLang workers. The Store master defaults to port 50071;
`STORE_PORT` can override it. Model logs must confirm storage initialization,
`kernel_ascend` and `page_first_kv_split`. A passing chat suite alone does not
prove L3 hits: the original experiment driver performs stricter cache-source
checks and still needs transport/capacity adaptation before formal runs.

This entry point is prepared but has not yet been run on the remote NPUs.

## Original formal experiment driver

`run_mooncake.sh` starts Mooncake master/store. `run_exp0.sh` starts and
restarts SGLang, then calls `exp0.py` to prepare requests, verify cache sources,
measure TTFT, and summarize three runs.

## Setup

Use the Ascend SGLang environment and install its matching Mooncake package:

```bash
workspace/setup/install_mooncake.sh
cp workspace/dense_three_tier_kv_exp0/mooncake_worker.example.json \
  workspace/dense_three_tier_kv_exp0/mooncake_worker.json
cp workspace/dense_three_tier_kv_exp0/mooncake_store.example.json \
  workspace/dense_three_tier_kv_exp0/mooncake_store.json
```

For the remote stage, run the installer in the SGLang environment on both
machines.

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
