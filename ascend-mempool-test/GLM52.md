# Ticket10：GLM-5.2 紧凑 Index K 验收

容器内自动运行入口见 [双机自动验收](TICKET10_DOCKER.md)：每侧执行一次命令，
自动完成组件、四组服务、token对照及生命周期检查，日志统一保存到`/home/cryang/log/`。
下面保留逐步运行方式，供单阶段排查。

适用主模型、TP16/PP1、BF16 KV、普通非投机 decode。`indexer_types` 决定 producer，
GLM-5.2 为 21 个 Index K 层、78 个独立 compact KV 层。普通 sparse PD 和正式
mempool 使用同一布局选择。MTP、CP/DCP、HiCache/HiSparse 等路径保持原有布局策略。
硬件结果尚待用户执行；本页提供命令，不代表 NPU 或模型精度已验收。

## 1. 环境和容量

两侧各 16 张 NPU，使用同一提交、相同权重/tokenizer，复用 ticket03 已验证的
torch_npu、sgl_kernel_npu、ModelSlim 和 MemFabric Hybrid 1.1.4 环境。
在各机进入本仓库，加载本机 Ascend toolkit/ATB 环境及已安装的自定义算子。
以下命令按 Bash 执行，先替换地址、网卡和本地模型路径：

```bash
export P_IP='<P机器IP>' D_IP='<D机器IP>'
export HCCL_SOCKET_IFNAME='<通信网卡>' GLOO_SOCKET_IFNAME='<通信网卡>'
export MODEL_PATH=/data_lib/data/models/glm-q
export PYTHONPATH="$PWD/python${PYTHONPATH:+:$PYTHONPATH}"
INDEXER_IDS=(0 1 2 6 10 14 18 22 26 30 34 38 42 46 50 54 58 62 66 70 74)
```

服务测试使用最长 3072 个输入 token、最多 128 个输出 token：

```bash
export PREFILL_CAPACITY=3072 DECODE_CAPACITY=128 CONTEXT_LENGTH=3200
export MAX_TOTAL_TOKENS=4096
```

P/D 每 rank 各有 16 个 BM slot；每 token 的 compact KV 为 `78 × 576 × 2` 字节。
P 逻辑 BM 为 4,416,602,112 字节（4.113 GiB），D 为 184,025,088 字节（0.171 GiB）。
包含 64 字节 probe 后按 1 GiB 对齐，实际贡献为 P 5 GiB、D 1 GiB，公共 stride 5 GiB；
TP16 合计 P 80 GiB、D 16 GiB。用现有 NUMA/驱动窗口可运行的机器验证；
若该容量无法启动，保留长请求待验收项，不能用 context1024 代替。

若实际 pool size=4096、page_size=128，每 rank Index K HBM 为
`(4096 + 128) × 21 × 128 × 2 = 22,708,224` 字节，原全层布局为 84,344,832 字节。
每 token Index K 从 19,968 降至 5,376 字节，减少约 73.1%。P 的 78 层 native compact KV
仍为 379,551,744 字节；D 另有全层 sparse cache。实际分配以日志为准，
总 HBM 还包含模型、Graph、算子 workspace 等，不等于上述 Index K 数值。

## 2. 组件 gate（每侧 device 0，先 P 后 D）

先停止占用测试设备的服务。P 等待 peer 时立即启动 D。每机设置自己的 rank/IP：

P 机：`export GATE_RANK=0 LOCAL_IP="$P_IP"`。
D 机：`export GATE_RANK=1 LOCAL_IP="$D_IP"`。

两机执行同一条命令，验证真实 Index K-only copy、非连续页、aux、布局拒绝和 drain：

```bash
python3 -u ascend-mempool-test/scripts/verify_pd_transfer.py \
  --rank "$GATE_RANK" --head-ip "$P_IP" --local-ip "$LOCAL_IP" --device-id 0 \
  --layers 78 --indexer-layer-ids "${INDEXER_IDS[@]}" --timeout 600 \
  --report "/tmp/ticket10-transfer-$GATE_RANK.json" \
  2>&1 | tee "/tmp/ticket10-transfer-$GATE_RANK.log"
```

两机完成后执行同一条 BM 数值命令。每层使用不同已知 pattern，所有层复用相同选择位置，
覆盖 P-only、D-only、mixed、hit、zero-valid、padding、连续异步 replay 和 row/slot 重绑：

```bash
python3 -u ascend-mempool-test/scripts/verify_fetch.py \
  --rank "$GATE_RANK" --head-ip "$P_IP" --device-id 0 \
  --nic-url "tcp://$LOCAL_IP:24770" --store-port 18873 --control-port 18874 \
  --pool-id 0 --layers 78 --s-p 8 --s-d 16 --heads 1 --kv-dim 576 \
  --graph-rows 16 --active-rows 3 --topk 2048 \
  --block-dims 24 48 --replay-cycles 2 --warmup 3 --timeout 600 \
  --report "/tmp/ticket10-fetch-$GATE_RANK.json" \
  2>&1 | tee "/tmp/ticket10-fetch-$GATE_RANK.log"
```

预期 transfer 和 fetch 均输出 `ALL_CHECKS_PASSED`；
D 有 30 个 `FETCH_PASS`，每项 `queued_forwards=5`，完成检查为 `5 × 78` 层。
任意逐元素差异、缺层、布局误接受、过早释放、未 drain 均失败。
此 gate 注入选择位置，不运行模型 indexer；真实 21→57 复用链由下一节模型请求验收。
78 层输出及快照需要额外测试 HBM，不把该 gate 当性能测量。

## 3. 固定输入与四组服务对照

只准备一次输入，所有模式使用同一文件。脚本使用部署 tokenizer 的 chat template，
`enable_thinking=False`，保存最终 input IDs、可读输入、模板选项及配置文件 SHA256。
检查生成文件的 assistant 前缀确认模型模板已关闭 thinking；完整量化说明和实际
ModelSlim 加载日志还需要人工核对，文件摘要不能替代权重加载成功证据。

```bash
python3 ascend-mempool-test/scripts/verify_model_tokens.py prepare \
  --model-path "$MODEL_PATH" --output /tmp/ticket10-glm52-cases.json
```

依次运行 `native/eager`、`native/graph`、`formal/eager`、`formal/graph`。
每组 drain 后停止 P/D 和 router，再启动下一组。两机为当前组设置相同值：

```bash
export MODE=native GRAPH_MODE=eager
export LOG_DIR="/tmp/ticket10-glm52-$MODE-$GRAPH_MODE"
```

P 机先启动；formal 模式在 P 等待 BM join 时立即启动 D：

```bash
bash ascend-mempool-test/scripts/run_service.sh prefill "$MODE"
```

D 机：

```bash
bash ascend-mempool-test/scripts/run_service.sh decode "$MODE"
```

两侧 ready 后，在 P 的新终端（同样设置环境变量）启动 router：

```bash
python3 -m sglang_router.launch_router --pd-disaggregation --policy round_robin \
  --prefill "http://$P_IP:8000" 8995 --decode "http://$D_IP:8001" \
  --host 127.0.0.1 --port 6699 --mini-lb --request-timeout-secs 1800 \
  2>&1 | tee "$LOG_DIR/router.log"
```

在 P 的另一个终端运行请求：

```bash
python3 ascend-mempool-test/scripts/verify_model_tokens.py run \
  --url http://127.0.0.1:6699 --cases /tmp/ticket10-glm52-cases.json \
  --report "$LOG_DIR/tokens.json"
```

共 10 个串行请求、两轮切换长度；每轮包含一个单 token 的 zero-decode 请求及
1024、2047、2048、3072 个输入 token 的算术题。D 首个 forward 的 seq_len 为输入长度+1，
因此覆盖 `<2048`、`=2048`、`>2048`。非零请求必须至少输出 2 个 token、正常 stop、
回答 `17 + 25 = 42`；输入不含正确答案。脚本拒绝输入被截短、缺少 token ID、答案不符及
长度截断。先人工确认 native/eager 的英文输出合理，保存 sanity 结论，再做其他组。
若实际模板忽略 thinking 开关导致截断，记录失败并调整同一份 fixture 后重跑全部组。

四组完成后，在保存各组报告的机器比对：

```bash
for candidate in native-graph formal-eager formal-graph; do
  python3 ascend-mempool-test/scripts/verify_model_tokens.py compare \
    /tmp/ticket10-glm52-native-eager/tokens.json \
    "/tmp/ticket10-glm52-$candidate/tokens.json"
done
```

每组需 `TOKEN_COMPARISON_PASSED`；首个差异的位置和 token 会报错，保留两份完整响应排查。
同时核对各次启动的代码 SHA、config/量化文件相同及后端/非投机参数一致，
避免误用不同权重或只根据响应 model 名称判断版本。

## 4. 服务生命周期及 GLM-5.1 回归

将 D 的日志复制到分析机器，以下 `--decode-log` 替换为其实际本地路径。
formal/eager 与 formal/graph 分别检查；示例为 Graph：

```bash
python3 ascend-mempool-test/scripts/verify_service.py \
  --prefill-log /tmp/ticket10-glm52-formal-graph/prefill.log \
  --decode-log '<D日志本地路径>/decode.log' \
  --requests 10 --layers 78 --indexer-layer-ids "${INDEXER_IDS[@]}" \
  --execution graph --report /tmp/ticket10-glm52-formal-graph/service.json
```

eager 组使用对应日志及 `--execution eager`。预期所有 16 rank 的资源报告有
`model_layers=78`、精确 21 个 `indexer_layer_ids`、`registered_index_k_entries=21`。
Graph 启动参数为 bucket16；检查 capture shape=16，真实请求有 replay，
`layer_checks=forwards*78`，drain 后 DONE/RELEASE_ACK 完成且 free=16。
缺少任何证据、Index K 层号不同或 KV 完成检查误用21均失败。

GLM-5.1 使用相同提交和原权重路径重新 prepare，单独保存在 `/tmp/ticket10-glm51-cases.json`。
为容纳相同长请求，沿用本页容量并运行同样四组服务与 token 比对，目录前缀改为 `glm51`；
其配置无 shared 层时，资源 checker 使用 `INDEXER_IDS=($(seq 0 77))`，预期 78 个 Index K 层。
实际配置若不同，先按模型配置核对层集合。原 context1024 短配置可另按
[ticket03 服务说明](FORMAL_SERVICE.md)回归，不能运行本页长 fixture。

回传两机 SHA、各组 environment/config/完整 quant_model_description 文件、producer 映射日志、
组件 JSON/log、服务 JSON/log、cases 与 tokens JSON、逐 token 差异及 sanity 结论。
token 报告保存服务端 meta_info 和墙钟耗时；另按 ticket03 性能命令测量 TTFT/TPOT/吞吐，
使用相同负载和容量对照，性能结论由用户确认。本票不预设固定加速比例。
上述 NPU 结果及用户代码核对通过后，才按 verification.md 关闭10并解锁04。
