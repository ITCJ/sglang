---
template: doc
theme: blueprint
lang: zh
title: Ticket03 S6 验收命令台
subtitle: 可编辑命令 · 先组件，后服务 · P/D 使用同一版本
version: 4baa6038d5
---

按顺序执行下面八个面板。每个命令框都能修改、复制和恢复原文。
修改会保存在当前浏览器；也可下载全部已编辑命令。
网页只编辑命令，请复制到对应机器的终端执行。

## A 准备：两端配置与版本

先停止占用目标 NPU 的旧服务或 gate。
沿用已验证的 Ascend、ATB、自定义算子环境。
在 P、D 分别执行以下配置，保持两端参数相同。
修改本页文本后，需重新复制执行，远端配置才会更新。

### A1 · P 和 D：创建公共配置

修改 IP、仓库、模型和网卡。每轮验收使用新的 RUN_ID。
后续命令会读取这份配置，并加载 Ascend 环境。

```bash
bash <<'SH'
set -eo pipefail
cat > /tmp/ticket03-s6-env.sh <<'ENV'
export S6_REPO=/home/cryang/sglang
export P_IP=10.120.72.31
export D_IP=10.120.72.32
export MODEL_PATH=/data_lib/data/models/GLM-5.1-w4a8
export SERVED_MODEL=GLM-5.1-w4a8
export HCCL_SOCKET_IFNAME=bond4
export GLOO_SOCKET_IFNAME=bond4
export RUN_ID=ticket03-s6-final-20261007-01
export ACCEPT_DIR="/tmp/$RUN_ID"
export ROUTER_URL=http://127.0.0.1:6699
export SSH_USER=root
export SGLANG_NPU_MEMPOOL_LOCAL_NUMA_NODE=0,2,4,6

source /usr/local/Ascend/ascend-toolkit/set_env.sh
source /usr/local/Ascend/nnal/atb/set_env.sh
export LD_LIBRARY_PATH="/usr/local/Ascend/ascend-toolkit/latest/opp/vendors/customize/op_api/lib${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}"
export PATH="/usr/local/Ascend/8.5.0/compiler/bishengir/bin:$PATH"
export PYTHONPATH="$S6_REPO/python${PYTHONPATH:+:$PYTHONPATH}"
ENV
source /tmp/ticket03-s6-env.sh
mkdir -p "$ACCEPT_DIR"
printf '公共配置已写入 /tmp/ticket03-s6-env.sh\n验收目录：%s\n' "$ACCEPT_DIR"
SH
```

### A2 · P 和 D：更新并记录版本

预期提交为 `4baa6038d5`。两端版本和依赖都要记录。
CPU 测试已通过；本页用于最终版本的硬件复验。

```bash
bash <<'SH'
set -eo pipefail
source /tmp/ticket03-s6-env.sh
cd "$S6_REPO"
git switch cryang/dev/mempool
git pull --ff-only origin cryang/dev/mempool
test "$(git rev-parse --short=10 HEAD)" = 4baa6038d5
{
  git rev-parse HEAD
  git status --short
  python3 --version
  python3 -c 'import msgspec; print("msgspec", msgspec.__version__)'
  npu-smi info
} 2>&1 | tee "$ACCEPT_DIR/version-$(hostname).txt"
SH
```

## B 第1步：双机 fetch 与 Graph

先执行 P；P 等待连接时立即执行 D。
不要等待 P 完成后才启动 D。
两个进程退出后，才能启动服务。

### B1 · P：rank 0

```bash
bash <<'SH'
set -eo pipefail
source /tmp/ticket03-s6-env.sh
cd "$S6_REPO"
mkdir -p "$ACCEPT_DIR/fetch"
python3 -u ascend-mempool-test/scripts/verify_fetch.py \
  --rank 0 --head-ip "$P_IP" --device-id 0 \
  --nic-url "tcp://$P_IP:24770" \
  --store-port 18873 --control-port 18874 --pool-id 0 \
  --s-p 8 --s-d 16 --layers 2 --heads 1 --kv-dim 576 \
  --graph-rows 16 --active-rows 3 --topk 2048 \
  --block-dims 24 48 --replay-cycles 2 --warmup 3 --timeout 600 \
  --report "$ACCEPT_DIR/fetch/p.json" \
  2>&1 | tee "$ACCEPT_DIR/fetch/p.log"
SH
```

### B2 · D：rank 1

```bash
bash <<'SH'
set -eo pipefail
source /tmp/ticket03-s6-env.sh
cd "$S6_REPO"
mkdir -p "$ACCEPT_DIR/fetch"
python3 -u ascend-mempool-test/scripts/verify_fetch.py \
  --rank 1 --head-ip "$P_IP" --device-id 0 \
  --nic-url "tcp://$D_IP:24770" \
  --store-port 18873 --control-port 18874 --pool-id 0 \
  --s-p 8 --s-d 16 --layers 2 --heads 1 --kv-dim 576 \
  --graph-rows 16 --active-rows 3 --topk 2048 \
  --block-dims 24 48 --replay-cycles 2 --warmup 3 --timeout 600 \
  --report "$ACCEPT_DIR/fetch/d.json" \
  2>&1 | tee "$ACCEPT_DIR/fetch/d.log"
SH
```

```callout ok 通过标准
双端打印 ALL_CHECKS_PASSED，进程退出0。
D 端有30个 FETCH_PASS，每项 queued_forwards=5。
检查 eager/replay、P/D miss、hit、padding 和 row/slot 重绑。
```

## C 第2步：四种模式的资源回归

在任意一台机器的空闲 device0 执行。
本轮尤其关注普通 staging 的成功和失败清理。
这个 gate 不启动模型，也不需要 BM peer。

### C1 · 单机：逐模式运行并核对报告

```bash
bash <<'SH'
set -eo pipefail
source /tmp/ticket03-s6-env.sh
cd "$S6_REPO"
mkdir -p "$ACCEPT_DIR/resources"
for mode in pd_prefill_mempool pd_decode_mempool local_offload pd_decode_offload; do
  python3 -u ascend-mempool-test/scripts/verify_resources.py \
    --mode "$mode" --device-id 0 \
    --report "$ACCEPT_DIR/resources/$mode.json" \
    2>&1 | tee "$ACCEPT_DIR/resources/$mode.log"
done
python3 - <<'PY'
import json
import os
from pathlib import Path
root = Path(os.environ['ACCEPT_DIR']) / 'resources'
for mode in ('pd_prefill_mempool', 'pd_decode_mempool', 'local_offload', 'pd_decode_offload'):
    data = json.loads((root / f'{mode}.json').read_text())
    assert data['success'] is True, data
    print('PASS', mode)
PY
SH
```

```callout ok 通过标准
4条 RESOURCE_PASS，4份报告均为 success=true。
正式模式旧 host KV/staging 为零。
普通模式 staging→host、host读取和复用检查通过。
```

## D 第3步：正式服务与生命周期

先启动 P，再启动 D，最后启动 router。
三个启动命令分别占用一个终端。
脚本使用 TP16、context1024、P/D各512 和 D Graph16。

### D1 · P：启动正式 prefill

```bash
bash <<'SH'
set -eo pipefail
source /tmp/ticket03-s6-env.sh
cd "$S6_REPO"
export LOG_DIR="$ACCEPT_DIR/formal"
bash ascend-mempool-test/scripts/run_service.sh prefill formal
SH
```

### D2 · D：启动正式 decode

```bash
bash <<'SH'
set -eo pipefail
source /tmp/ticket03-s6-env.sh
cd "$S6_REPO"
export LOG_DIR="$ACCEPT_DIR/formal"
bash ascend-mempool-test/scripts/run_service.sh decode formal
SH
```

### D3 · P 的新终端：启动 router

确认 P、D 均 ready 后再执行。

```bash
bash <<'SH'
set -eo pipefail
source /tmp/ticket03-s6-env.sh
cd "$S6_REPO"
mkdir -p "$ACCEPT_DIR/formal"
python3 -m sglang_router.launch_router \
  --pd-disaggregation --policy round_robin \
  --prefill "http://$P_IP:8000" 8995 \
  --decode "http://$D_IP:8001" \
  --host 127.0.0.1 --port 6699 --mini-lb \
  2>&1 | tee "$ACCEPT_DIR/formal/router.log"
SH
```

### D4 · P 的新终端：发送三个请求

首次验收只发这三个请求。完成 checker 后再发小题目和性能请求。
这里检查 token 数和生命周期，不用短输出判断回答质量。

```bash
bash <<'SH'
set -eo pipefail
source /tmp/ticket03-s6-env.sh
cd "$S6_REPO"
mkdir -p "$ACCEPT_DIR/formal"
curl --fail-with-body --max-time 900 -sS "$ROUTER_URL/generate" \
  -H 'Content-Type: application/json' \
  -d '{"text":"Reply with hello.","sampling_params":{"temperature":0,"max_new_tokens":1,"ignore_eos":true},"stream":false}' \
  -o "$ACCEPT_DIR/formal/zero.json"
curl --fail-with-body --max-time 900 -sS "$ROUTER_URL/generate" \
  -H 'Content-Type: application/json' \
  -d '{"text":"Briefly explain why the sky is blue.","sampling_params":{"temperature":0,"max_new_tokens":32,"ignore_eos":true},"stream":false}' \
  -o "$ACCEPT_DIR/formal/decode.json"
curl --fail-with-body --max-time 900 -sS "$ROUTER_URL/generate" \
  -H 'Content-Type: application/json' \
  -d '{"text":"Briefly explain why ice floats on water.","sampling_params":{"temperature":0,"max_new_tokens":32,"ignore_eos":true},"stream":false}' \
  -o "$ACCEPT_DIR/formal/reuse.json"
python3 - <<'PY'
import json
import os
from pathlib import Path
root = Path(os.environ['ACCEPT_DIR']) / 'formal'
for name, expected in (('zero', 1), ('decode', 32), ('reuse', 32)):
    result = json.loads((root / f'{name}.json').read_text())
    meta = result['meta_info']
    assert not result.get('error'), result
    assert meta['finish_reason']['type'] == 'length', result
    assert meta['completion_tokens'] == expected, result
    print(f'PASS {name}: {expected} tokens')
PY
SH
```

### D5 · P：复制日志快照并运行 checker

先等最后一个请求完成 DONE/RELEASE_ACK，所有rank恢复 free=16。
快照只包含上述三个请求，之后追加请求不会改变快照。
若使用外部启动脚本，请修改源日志路径。

```bash
bash <<'SH'
set -eo pipefail
source /tmp/ticket03-s6-env.sh
cd "$S6_REPO"
mkdir -p "$ACCEPT_DIR/checker"
cp "$ACCEPT_DIR/formal/prefill.log" "$ACCEPT_DIR/checker/p.log"
scp "$SSH_USER@$D_IP:$ACCEPT_DIR/formal/decode.log" "$ACCEPT_DIR/checker/d.log"
python3 ascend-mempool-test/scripts/verify_service.py \
  --prefill-log "$ACCEPT_DIR/checker/p.log" \
  --decode-log "$ACCEPT_DIR/checker/d.log" \
  --requests 3 --layers 78 \
  --report "$ACCEPT_DIR/checker/service-result.json"
SH
```

```callout ok 通过标准
三个请求分别输出1、32、32 tokens。
checker 打印 FORMAL_SERVICE_PASSED。
全16 ranks的Graph、handoff、slot复用和最终释放检查通过。
```

## E 第4步：回答正确性与性能

先完成 D5 的日志快照和 checker。
下面的小题目显式传入 enable_thinking=false。
模型模板需支持此开关；确认返回完整答案。

### E1 · P：小题目，不启用 thinking

MODE 默认为 formal；普通模式复验时改成 native。

```bash
bash <<'SH'
set -eo pipefail
source /tmp/ticket03-s6-env.sh
cd "$S6_REPO"
export MODE=formal
mkdir -p "$ACCEPT_DIR/$MODE"
python3 - <<'PY'
import json
import os
from pathlib import Path
payload = {
    'model': os.environ['SERVED_MODEL'],
    'messages': [{'role': 'user', 'content': '小明原有17个苹果，又买了25个，送给朋友9个。现在有多少个苹果？请只用一句话给出算式和答案。'}],
    'temperature': 0,
    'max_tokens': 256,
    'stream': False,
    'chat_template_kwargs': {'enable_thinking': False},
}
root = Path(os.environ['ACCEPT_DIR']) / os.environ['MODE']
(root / 'question.json').write_text(json.dumps(payload, ensure_ascii=False))
PY
curl --fail-with-body --max-time 900 -sS "$ROUTER_URL/v1/chat/completions" \
  -H 'Content-Type: application/json' \
  --data-binary "@$ACCEPT_DIR/$MODE/question.json" \
  -o "$ACCEPT_DIR/$MODE/answer.json"
python3 - <<'PY'
import json
import os
from pathlib import Path
root = Path(os.environ['ACCEPT_DIR']) / os.environ['MODE']
result = json.loads((root / 'answer.json').read_text())
choice = result['choices'][0]
assert choice['finish_reason'] == 'stop', result
print(choice['message']['content'])
print('usage:', result['usage'])
print('请人工确认：17 + 25 - 9 = 33，回答完整，无乱码。')
PY
SH
```

### E2 · P：三轮性能与汇总

保持此前负载：并发1、输入128、输出64，每轮64请求。
每轮预热3请求，temperature=0，seed=42。
每次执行生成独立目录，避免 JSONL 追加旧记录。
普通模式对照时，将 MODE 改为 native。

```bash
bash <<'SH'
set -eo pipefail
source /tmp/ticket03-s6-env.sh
cd "$S6_REPO"
export MODE=formal
mkdir -p "$ACCEPT_DIR/$MODE"
export BENCH_DIR
BENCH_DIR=$(mktemp -d "$ACCEPT_DIR/$MODE/bench.XXXXXX")
printf '本轮性能目录：%s\n' "$BENCH_DIR"
python3 - <<'PY'
import json
import os
from pathlib import Path
rows = [{'conversations': [
    {'from': 'human', 'value': f'Explain how water changes between ice, liquid and vapor. Example {i}.'},
    {'from': 'gpt', 'value': 'Heating and cooling change the state of water.'},
]} for i in range(64)]
(Path(os.environ['BENCH_DIR']) / 'inputs.json').write_text(json.dumps(rows))
PY
npu-smi info > "$BENCH_DIR/npu-smi.txt"
for round in 1 2 3; do
  python3 -m sglang.benchmark.serving \
    --backend sglang --base-url "$ROUTER_URL" \
    --model "$MODEL_PATH" \
    --dataset-name random --dataset-path "$BENCH_DIR/inputs.json" \
    --tokenize-prompt --random-input-len 128 --random-output-len 64 \
    --random-range-ratio 1 --num-prompts 64 --max-concurrency 1 \
    --request-rate inf --warmup-requests 3 --temperature 0 --seed 42 \
    --output-file "$BENCH_DIR/bench-$round.jsonl" \
    2>&1 | tee "$BENCH_DIR/bench-$round.log"
done
python3 - <<'PY' | tee "$BENCH_DIR/summary.txt"
import json
import os
import statistics
from pathlib import Path
root = Path(os.environ['BENCH_DIR'])
rows = []
for number in (1, 2, 3):
    path = root / f'bench-{number}.jsonl'
    lines = [line for line in path.read_text().splitlines() if line.strip()]
    assert len(lines) == 1, path
    row = json.loads(lines[0])
    assert row['completed'] == 64, row
    assert row['total_input_tokens'] == 8192, row
    assert row['total_output_tokens'] == 4096, row
    rows.append(row)
for key in ('mean_ttft_ms', 'mean_tpot_ms', 'mean_itl_ms', 'output_throughput'):
    values = [r[key] for r in rows]
    print(f'{key}: rounds={values}, median={statistics.median(values):.3f}')
print('结果目录：', root)
PY
SH
```

| 指标 | 此前三轮中位数 | 本次判定 |
| --- | ---: | --- |
| mean_ttft_ms | 910.199 ms | 查看新测量值与波动 |
| mean_tpot_ms | 129.747 ms | 查看新测量值与波动 |
| output_throughput | 7.044 tokens/s | 用户确认是否符合预期 |

每轮必须完成64请求、8192输入tokens和4096输出tokens。
历史数值用于参考，不是自动通过阈值。
此处只验收当前短请求负载，长上下文容量归 ticket09。

## F 第5步：同版本普通 sparse PD

先等正式请求完成，再在原终端停止 P、D 和 router。
确认旧进程退出，再按下面命令重启。
native 模式会关闭 MEMPOOL，继续开启 sparse offload。

### F1 · P：普通 prefill

```bash
bash <<'SH'
set -eo pipefail
source /tmp/ticket03-s6-env.sh
cd "$S6_REPO"
export LOG_DIR="$ACCEPT_DIR/native"
bash ascend-mempool-test/scripts/run_service.sh prefill native
SH
```

### F2 · D：普通 decode

```bash
bash <<'SH'
set -eo pipefail
source /tmp/ticket03-s6-env.sh
cd "$S6_REPO"
export LOG_DIR="$ACCEPT_DIR/native"
bash ascend-mempool-test/scripts/run_service.sh decode native
SH
```

### F3 · P：两端 ready 后启动 router

```bash
bash <<'SH'
set -eo pipefail
source /tmp/ticket03-s6-env.sh
cd "$S6_REPO"
mkdir -p "$ACCEPT_DIR/native"
python3 -m sglang_router.launch_router \
  --pd-disaggregation --policy round_robin \
  --prefill "http://$P_IP:8000" 8995 \
  --decode "http://$D_IP:8001" \
  --host 127.0.0.1 --port 6699 --mini-lb \
  2>&1 | tee "$ACCEPT_DIR/native/router.log"
SH
```

### F4 · P：检查普通 decode 与日志

随后将 E1、E2 中的 MODE 改为 native，再分别执行。
普通模式不运行正式资源合同的 verify_service.py。

```bash
bash <<'SH'
set -eo pipefail
source /tmp/ticket03-s6-env.sh
cd "$S6_REPO"
mkdir -p "$ACCEPT_DIR/native"
curl --fail-with-body --max-time 900 -sS "$ROUTER_URL/generate" \
  -H 'Content-Type: application/json' \
  -d '{"text":"Briefly explain why the sky is blue.","sampling_params":{"temperature":0,"max_new_tokens":32,"ignore_eos":true},"stream":false}' \
  -o "$ACCEPT_DIR/native/decode.json"
python3 - <<'PY'
import json
import os
from pathlib import Path
path = Path(os.environ['ACCEPT_DIR']) / 'native/decode.json'
result = json.loads(path.read_text())
assert not result.get('error'), result
meta = result['meta_info']
assert meta['completion_tokens'] == 32, result
assert meta['finish_reason']['type'] == 'length', result
print('PASS native decode: 32 tokens')
print(result['text'])
PY
scp "$SSH_USER@$D_IP:$ACCEPT_DIR/native/decode.log" "$ACCEPT_DIR/native/decode.log"
scp "$SSH_USER@$D_IP:$ACCEPT_DIR/native/decode-environment.txt" "$ACCEPT_DIR/native/decode-environment.txt"
grep -nE 'SGLANG_NPU_ENABLE_MEMPOOL|SGLANG_NPU_ENABLE_SPARSE_KV_OFFLOAD' \
  "$ACCEPT_DIR/native/prefill-environment.txt" "$ACCEPT_DIR/native/decode-environment.txt"
grep -nE 'Decode batch|npu graph|ERROR|Traceback|mempool|MEMPOOL_INIT' \
  "$ACCEPT_DIR/native/prefill.log" "$ACCEPT_DIR/native/decode.log" || true
SH
```

```callout info 普通模式的判定
两侧环境记录显示 MEMPOOL=0、SPARSE_KV_OFFLOAD=1。
确认模型没有启动 mempool BM，实际 decode 使用 Graph。
结合资源gate核对普通staging/host路径，再检查完整回答和性能。
grep输出只辅助检查，不自动宣布硬件路径通过。
```

## G 第6步：完整环境的 registered 检查

在配置好完整 SGLang 依赖的机器执行。
使用当前生产 Python 环境，不使用 Mac 的轻量替身。

### G1 · 单机：两项配置集成测试

```bash
bash <<'SH'
set -eo pipefail
source /tmp/ticket03-s6-env.sh
cd "$S6_REPO"
mkdir -p "$ACCEPT_DIR/registered"
python3 -B -m unittest discover \
  -s test/registered/unit/npu \
  -p test_sparsity_driven_kv_offload_config.py -v \
  2>&1 | tee "$ACCEPT_DIR/registered/sparse-config.log"
python3 -B -m unittest discover \
  -s test/registered/unit/model_executor \
  -p test_hisparse_pool_configurator.py -v \
  2>&1 | tee "$ACCEPT_DIR/registered/pool-configurator.log"
SH
```

两项 suite 全部通过。导入错误或测试失败均需保留完整日志。
CPU 的205项通过记录不能代替这两项检查。

## H 回传证据与命令编辑

完成全部请求后，保留 P/D 的最终日志和环境记录。
下面命令从 P 收集 D 的文件，再生成压缩包。
若资源或 registered 测试在 D 执行，另复制对应目录。

### H1 · P：收集两端证据

```bash
bash <<'SH'
set -eo pipefail
source /tmp/ticket03-s6-env.sh
mkdir -p "$ACCEPT_DIR/d-evidence"
scp -r "$SSH_USER@$D_IP:$ACCEPT_DIR/." "$ACCEPT_DIR/d-evidence/"
tar -czf "$ACCEPT_DIR.tar.gz" -C "$(dirname "$ACCEPT_DIR")" "$(basename "$ACCEPT_DIR")"
printf '证据包：%s.tar.gz\n' "$ACCEPT_DIR"
SH
```

回传 gate JSON/log、checker结果、完整回答和性能汇总。
同时说明普通模式是否正常，附两端版本记录。
任一测试失败时先保存日志，不把失败结果记为通过。
你确认最终硬件结果后，再关闭 ticket03。

### 编辑器操作

每个文本框支持复制、恢复原文和调整高度。
浏览器允许本地存储时，刷新后保留修改。
下载按钮导出当前编辑内容，用于保存或核对。
导出的文件包含不同机器的步骤，请按标题逐段执行。

```html
<div id="command-tools" aria-label="命令导出工具"></div>
<script>
document.addEventListener('DOMContentLoaded', () => {
  const entries = [];
  const storagePrefix = 'ticket03-s6-4baa6038d5-command-v1:';
  const status = document.createElement('p');
  status.setAttribute('role', 'status');
  status.setAttribute('aria-live', 'polite');
  const tools = document.getElementById('command-tools');
  const download = document.createElement('button');
  download.type = 'button';
  download.textContent = '下载全部已编辑命令';
  tools.append(download, status);
  document.querySelectorAll('pre.am-code > code[data-lang="bash"]').forEach((code, index) => {
    const pre = code.parentElement;
    let heading = pre.previousElementSibling;
    while (heading && !/^H[1-6]$/.test(heading.tagName)) heading = heading.previousElementSibling;
    const label = heading ? heading.textContent : '命令 ' + (index + 1);
    const original = code.textContent.replace(/\n$/, '');
    const key = storagePrefix + index;
    const box = document.createElement('div');
    const toolbar = document.createElement('div');
    const textarea = document.createElement('textarea');
    textarea.setAttribute('aria-label', label);
    textarea.spellcheck = false;
    textarea.wrap = 'off';
    textarea.rows = Math.min(20, Math.max(7, original.split('\n').length + 1));
    textarea.style.cssText = 'display:block;box-sizing:border-box;width:100%;resize:vertical;min-height:150px;padding:14px;font:13px/1.65 ui-monospace,SFMono-Regular,Consolas,monospace;color:var(--ink);background:var(--paper);border:1px solid var(--line-2);border-radius:6px;tab-size:2;';
    textarea.value = original;
    try { const saved = localStorage.getItem(key); if (saved !== null) textarea.value = saved; } catch (_) {}
    const copy = document.createElement('button');
    copy.type = 'button';
    copy.textContent = '复制命令';
    copy.addEventListener('click', async () => {
      try {
        if (navigator.clipboard && window.isSecureContext) {
          await navigator.clipboard.writeText(textarea.value);
        } else {
          textarea.focus(); textarea.select();
          if (!document.execCommand('copy')) throw new Error('manual-copy');
        }
        copy.textContent = '已复制';
        status.textContent = '已复制：' + label;
        setTimeout(() => { copy.textContent = '复制命令'; }, 1600);
      } catch (_) {
        textarea.focus(); textarea.select();
        status.textContent = '已选中命令，请按 Ctrl+C 或 ⌘C 复制。';
      }
    });
    const reset = document.createElement('button');
    reset.type = 'button';
    reset.textContent = '恢复原文';
    reset.addEventListener('click', () => {
      textarea.value = original;
      try { localStorage.removeItem(key); } catch (_) {}
      status.textContent = '已恢复：' + label;
    });
    const indicator = document.createElement('span');
    const save = () => {
      try {
        localStorage.setItem(key, textarea.value);
        indicator.textContent = textarea.value === original ? '原始命令' : '已在本机保存修改';
      } catch (_) { indicator.textContent = '本次可编辑；请下载保存'; }
    };
    textarea.addEventListener('input', save);
    reset.addEventListener('click', () => { indicator.textContent = '原始命令'; });
    indicator.textContent = textarea.value === original ? '原始命令' : '已恢复本机修改';
    toolbar.style.cssText = 'display:flex;gap:10px;align-items:center;flex-wrap:wrap;margin:12px 0 8px;';
    toolbar.append(copy, reset, indicator);
    box.append(toolbar, textarea);
    pre.replaceWith(box);
    entries.push({label, textarea});
  });
  download.addEventListener('click', () => {
    const text = '# Ticket03 S6 验收命令；按机器和步骤逐段执行\n\n' + entries.map(e => '# ' + e.label + '\n' + e.textarea.value).join('\n\n');
    const url = URL.createObjectURL(new Blob([text], {type:'text/plain;charset=utf-8'}));
    const link = document.createElement('a');
    link.href = url; link.download = 'ticket03-s6-edited-commands.txt';
    link.click(); setTimeout(() => URL.revokeObjectURL(url), 1000);
  });
  status.textContent = '已加载 ' + entries.length + ' 个可编辑命令框。';
});
</script>
```
