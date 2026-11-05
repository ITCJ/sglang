# Ticket26：002 fetch定向重测

在各机sglang仓库根目录执行。只更新阶段1的Python适配层，复用002已编译插件；不改kernel、不加载模型、不重复页拷贝。两侧配对执行，每侧一条命令块。

## 10.120.72.31 / prefill

```bash
set -o pipefail
git pull --ff-only origin cryang/dev/mempool-min && \
ticket26_phase1=/home/cryang/log/k2/k2-ticket26-002/phase1-source-prefill && \
tar -xzf .scratch/k2/verification/ticket26-local/phase1-source.tar.gz \
  -C "$ticket26_phase1" \
  python/sglang/srt/hardware_backend/npu/mempool/multibase.py && \
SGLANG_UNIDEX_MULTIBASE_LIBRARY="$ticket26_phase1/python/sglang/srt/hardware_backend/npu/mempool/kernels/unidex_multibase/build/libunidex_multibase.so" \
python3 -u "$ticket26_phase1/ascend-mempool-test/scripts/verify_fetch.py" \
  --rank 0 --head-ip 10.120.72.31 --device-id 0 \
  --nic-url tcp://10.120.72.31:24770 --store-port 18873 --control-port 18874 \
  --pool-id 0 --layers 78 --s-p 64 --s-d 512 --page-size 64 \
  --heads 1 --kv-dim 576 --graph-rows 16 --active-rows 3 --topk 2048 \
  --block-dims 48 --replay-cycles 1 --warmup 1 --timeout 600 \
  --report /home/cryang/log/k2/k2-ticket26-002/fetch-retry-prefill.json \
  2>&1 | tee /home/cryang/log/k2/k2-ticket26-002/fetch-retry-prefill.log
```

## 10.120.72.32 / decode

```bash
set -o pipefail
git pull --ff-only origin cryang/dev/mempool-min && \
ticket26_phase1=/home/cryang/log/k2/k2-ticket26-002/phase1-source-decode && \
tar -xzf .scratch/k2/verification/ticket26-local/phase1-source.tar.gz \
  -C "$ticket26_phase1" \
  python/sglang/srt/hardware_backend/npu/mempool/multibase.py && \
SGLANG_UNIDEX_MULTIBASE_LIBRARY="$ticket26_phase1/python/sglang/srt/hardware_backend/npu/mempool/kernels/unidex_multibase/build/libunidex_multibase.so" \
python3 -u "$ticket26_phase1/ascend-mempool-test/scripts/verify_fetch.py" \
  --rank 1 --head-ip 10.120.72.31 --device-id 0 \
  --nic-url tcp://10.120.72.32:24770 --store-port 18873 --control-port 18874 \
  --pool-id 0 --layers 78 --s-p 64 --s-d 512 --page-size 64 \
  --heads 1 --kv-dim 576 --graph-rows 16 --active-rows 3 --topk 2048 \
  --block-dims 48 --replay-cycles 1 --warmup 1 --timeout 600 \
  --report /home/cryang/log/k2/k2-ticket26-002/fetch-retry-decode.json \
  2>&1 | tee /home/cryang/log/k2/k2-ticket26-002/fetch-retry-decode.log
```

两侧退出码均为0且报告通过，才算fetch重测通过；不代表服务或26整体通过。失败回传fetch-retry日志和JSON。
