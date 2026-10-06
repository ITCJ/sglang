# Ticket03 S5：正式 mempool 服务验收

本阶段代码开放正式P/D路径。Mac检查不能替代下面的NPU验收。
P=`10.120.72.31`，D=`10.120.72.32`；两机均在`/home/cryang/sglang`运行。
模型默认`/data_lib/data/models/GLM-5.1-w4a8`。地址、权重、网卡不同时用脚本环境变量覆盖。
需要两侧各16个NPU、同一代码版本、S2–S4已验证的torch_npu/sgl_kernel_npu/MemFabric。

`MEMPOOL=1`选择正式模式。S6.2已删除shadow及`SGLANG_NPU_MEMPOOL_READBACK`；
旧启动脚本移除该变量的export。当前版本不提供旧host参考比较入口。
本目录提供统一启动脚本，也可使用外部`ascend-sglang-script`仓库的`glm51mempool.sh`。
外部脚本保存为`p.log`/`d.log`，本目录脚本保存为`prefill.log`/`decode.log`，
运行下面的日志checker时按所用脚本调整路径。
BM目前使用DRAM；D保留的HBM sparse cache是另一个存储层。
本页不要求AIME26或逐token精度对照。curl回答检查与性能达标由用户分别确认。

## 1. 连续异步 replay 组件 gate

先停止占用device 0的旧gate/服务。两侧先加载各自已经验证的Ascend及自定义算子环境。
先运行P，看到listener或BM等待后立即运行D，不要等待P完成。

P端：

```bash
cd /home/cryang/sglang
PYTHONPATH="$PWD/python${PYTHONPATH:+:$PYTHONPATH}" python3 -u \
  ascend-mempool-test/scripts/verify_fetch.py \
  --rank 0 --head-ip 10.120.72.31 --device-id 0 \
  --nic-url tcp://10.120.72.31:24770 --store-port 18873 --control-port 18874 \
  --pool-id 0 --s-p 8 --s-d 16 --layers 2 --heads 1 --kv-dim 576 \
  --graph-rows 16 --active-rows 3 --topk 2048 \
  --block-dims 24 48 --replay-cycles 2 --warmup 3 --timeout 600 \
  --report /tmp/ticket03-s5-fetch-p.json 2>&1 | tee /tmp/ticket03-s5-fetch-p.log
```

D端：

```bash
cd /home/cryang/sglang
PYTHONPATH="$PWD/python${PYTHONPATH:+:$PYTHONPATH}" python3 -u \
  ascend-mempool-test/scripts/verify_fetch.py \
  --rank 1 --head-ip 10.120.72.31 --device-id 0 \
  --nic-url tcp://10.120.72.32:24770 --store-port 18873 --control-port 18874 \
  --pool-id 0 --s-p 8 --s-d 16 --layers 2 --heads 1 --kv-dim 576 \
  --graph-rows 16 --active-rows 3 --topk 2048 \
  --block-dims 24 48 --replay-cycles 2 --warmup 3 --timeout 600 \
  --report /tmp/ticket03-s5-fetch-d.json 2>&1 | tee /tmp/ticket03-s5-fetch-d.log
```

预期双方`ALL_CHECKS_PASSED`；D仍有30个`FETCH_PASS`，每项`queued_forwards=5`。
每轮先把测试输入放到NPU，连续提交五个forward及设备快照，最后才同步和poll。
独立pattern逐元素核对P miss、D miss、mixed、全hit、zero-valid和padding。
同时检查未收集完成前禁止detach、五次完成计数、row/slot重绑及capture输出地址保留。
snapshot会额外占用测试设备内存；这不是性能测量入口。

## 2. 启动正式服务

组件gate退出后启动server。两侧使用同一提交；保留启动时生成的环境清单。
先P再D，P等待BM join时就启动D。两侧各自执行：

```bash
cd /home/cryang/sglang
source /usr/local/Ascend/ascend-toolkit/set_env.sh
source /usr/local/Ascend/nnal/atb/set_env.sh
export LD_LIBRARY_PATH=/usr/local/Ascend/ascend-toolkit/latest/opp/vendors/customize/op_api/lib/${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}
export PATH=/usr/local/Ascend/8.5.0/compiler/bishengir/bin:$PATH
export P_IP=10.120.72.31 D_IP=10.120.72.32
export MODEL_PATH=/data_lib/data/models/GLM-5.1-w4a8
export HCCL_SOCKET_IFNAME=bond4 GLOO_SOCKET_IFNAME=bond4
export LOG_DIR=/tmp/ticket03-s5-formal
```

P端：

```bash
bash ascend-mempool-test/scripts/run_service.sh prefill formal
```

D端：

```bash
bash ascend-mempool-test/scripts/run_service.sh decode formal
```

两侧ready后，在P的另一个终端启动router：

```bash
python3 -m sglang_router.launch_router \
  --pd-disaggregation --policy round_robin \
  --prefill http://10.120.72.31:8000 8995 \
  --decode http://10.120.72.32:8001 \
  --host 127.0.0.1 --port 6699 --mini-lb \
  2>&1 | tee /tmp/ticket03-s5-formal/router.log
```

启动脚本保留已使用的GLM小容量配置：TP16/DP1/PP1、context1024、P/D容量各512、
max-running16、P eager、D Graph bucket16。正式服务的模型top-k仍是2048。
脚本不设置sysctl或CPU governor；复用已验证机器设置，并在性能对照时保持一致。
追加`--dry-run`可打印命令且不加载模型。每次启动覆盖同目录日志，重测请换LOG_DIR。
端口：HTTP8000/8001，P bootstrap8995，原TransferEngine store24670，
BM store19000–19015，各侧BM NIC25670–25701，dist-init各侧10000。

启动后每个rank应有`mempool resources`：

| 字段 | P | D |
| --- | --- | --- |
| mode | pd_prefill_mempool | pd_decode_mempool |
| host_kv_bytes / staging_bytes | 0 / 0 | 0 / 0 |
| transport_staging | false | false |
| native_kv_bytes | >0，P prefill使用 | 0 |
| sparse_cache_bytes | 0 | >0 |
| registered_main_kv_entries | 0 | 0 |
| registered_index_k_entries / index_k_bytes | >0 / >0 | >0 / >0 |

这些值从已创建的buffer及已注册的KVArgs统计；BM大小另见`mapping_ready`。

## 3. 生命周期与 Graph smoke

在router所在机器执行三个顺序请求。ignore_eos仅用于强制足够的decode步数。
保存原始HTTP响应；第一个请求应输出1个token，后两个各32个token。

```bash
curl --fail-with-body --max-time 900 -sS http://127.0.0.1:6699/generate \
  -H 'Content-Type: application/json' \
  -d '{"text":"Reply with hello.","sampling_params":{"temperature":0,"max_new_tokens":1,"ignore_eos":true},"stream":false}' \
  -o /tmp/ticket03-s5-formal/zero.json
curl --fail-with-body --max-time 900 -sS http://127.0.0.1:6699/generate \
  -H 'Content-Type: application/json' \
  -d '{"text":"Briefly explain why the sky is blue.","sampling_params":{"temperature":0,"max_new_tokens":32,"ignore_eos":true},"stream":false}' \
  -o /tmp/ticket03-s5-formal/decode.json
curl --fail-with-body --max-time 900 -sS http://127.0.0.1:6699/generate \
  -H 'Content-Type: application/json' \
  -d '{"text":"Briefly explain why ice floats on water.","sampling_params":{"temperature":0,"max_new_tokens":32,"ignore_eos":true},"stream":false}' \
  -o /tmp/ticket03-s5-formal/reuse.json
python3 - <<'PY'
import json
from pathlib import Path
for name, expected in (("zero", 1), ("decode", 32), ("reuse", 32)):
    result = json.loads(Path(f"/tmp/ticket03-s5-formal/{name}.json").read_text())
    meta = result["meta_info"]
    assert not result.get("error") and meta["finish_reason"]["type"] != "abort", result
    assert meta["completion_tokens"] == expected, result
    print(name, meta, result["text"])
PY
```

等待两侧最后一个请求全部`RELEASE_ACK`/`DONE`，最终`free=16`。
把P的prefill.log和D的decode.log放到同一台机器，例如P：

```bash
scp root@10.120.72.32:/tmp/ticket03-s5-formal/decode.log /tmp/ticket03-s5-formal/decode.log
python3 ascend-mempool-test/scripts/verify_service.py \
  --prefill-log /tmp/ticket03-s5-formal/prefill.log \
  --decode-log /tmp/ticket03-s5-formal/decode.log \
  --requests 3 --layers 78 --report /tmp/ticket03-s5-formal/service-result.json
```

预期`FORMAL_SERVICE_PASSED`。检查器要求双方全16 ranks的正式资源、Index K/aux成功
发送、D Graph capture/replay、请求attempt与P/D lease一致、真实fetch完成、释放顺序、
zero-decode、至少两个实际decode请求、P/D物理slot新generation复用、最终全部free16。
native transfer与BM ready允许任意先后，但必须都早于start_decode。
P侧必须按BOUND_ACK、start_prefill、row_detach、native_free、native_release、DONE
的顺序执行。P可凭完成的写入receipt在native_release之后发布ready，checker允许该顺序。
不同rank的top-k/counter数值可不同；每个rank分别核对覆盖和计数等式。

`fetch_result`含`forwards/replay_forwards/written_kv`、选中KV数、HBM hit数及P/D miss数。
计数跨层累计；它证明执行和完成事实，不声称与参考KV做了数值对照。
`written_kv`按实际提交forward核对，不要求等于completion_tokens减一；overlap可能多提交一步。
取消、运行错误、缺rank、过早释放或遗留资源会使checker失败。
只缺slot复用时可再发两次32-token请求并重新收集日志；其他失败保留现场后定位。

## 4. 用户 curl 小题目检查

完成上面的自动smoke后，在同一服务发送小题目，保留输入和输出：

```bash
cat > /tmp/ticket03-s5-formal/question.json <<'JSON'
{"text":"小题目：小明原有17个苹果，又买了25个，送给朋友9个。现在有多少个苹果？请用一句话给出算式和答案。","sampling_params":{"temperature":0,"max_new_tokens":256},"stream":false}
JSON
curl --fail-with-body --max-time 900 -sS http://127.0.0.1:6699/generate \
  -H 'Content-Type: application/json' \
  --data-binary @/tmp/ticket03-s5-formal/question.json \
  -o /tmp/ticket03-s5-formal/answer.json
python3 -m json.tool /tmp/ticket03-s5-formal/answer.json
```

预期`17 + 25 - 9 = 33`。用户核对回答正确、没有乱码或异常截断，并回传结论。
在D日志找这次新增的非零`fetch_result`，确认`replay_forwards=forwards>0`且最终释放。
如果模型使用长thinking而256输出不足，先保留这次结果，再改短题或在容量内调整上限；
不能把被截断的回答当通过。这里使用`/generate`原生文本接口，无chat模板的额外token。
当前D BM每slot容量为512；若512-token回答仍被截断，先缩短回答或核对模型template
支持的thinking开关，不能只把输出上限提高到1024/2048。增加容量需同步P/D配置并
满足context限制。reasoning parser只拆分响应字段，不会减少生成的token数。
本检查不等于正式数据集精度验收。

## 5. 性能测量与对照

按用户最新决定，**验收时由用户查看TTFT、TPOT和输出吞吐并判断**，不要求预先提供阈值。
以下命令给出固定负载和指标，不会自动宣布性能达标。
诊断对照使用本提交`MEMPOOL=0`的普通sparse PD：drain后停止两侧及router，用同一个脚本的
`prefill native`和`decode native`重启，再启动同样router。完成基线后用formal重启测量。
不要在同一进程里修改模式。LOG_DIR分别使用`/tmp/ticket03-s5-native`和`...-formal`。

固定工作负载：128个输入token、64个输出token、并发1、每轮64请求、预热3请求、
temperature0、ignore_eos=true、seed42、每种模式3轮。使用本地构造的文本负载，
不下载数据集；`--tokenize-prompt`用input_ids确保输入长度可复现。

```bash
python3 - <<'PY'
import json
from pathlib import Path
rows = [{"conversations": [
    {"from": "human", "value": f"Explain how water changes between ice, liquid and vapor. Example {i}."},
    {"from": "gpt", "value": "Heating and cooling change the state of water."}
]} for i in range(64)]
Path("/tmp/ticket03-s5-bench-inputs.json").write_text(json.dumps(rows))
PY
# 对应当前正在运行的模式；另一模式重启后只改变这个值。
mode=formal
for round in 1 2 3; do
  PYTHONPATH="$PWD/python${PYTHONPATH:+:$PYTHONPATH}" python3 -m sglang.benchmark.serving \
    --backend sglang --base-url http://127.0.0.1:6699 \
    --model /data_lib/data/models/GLM-5.1-w4a8 \
    --dataset-name random --dataset-path /tmp/ticket03-s5-bench-inputs.json \
    --tokenize-prompt --random-input-len 128 --random-output-len 64 --random-range-ratio 1 \
    --num-prompts 64 --max-concurrency 1 --request-rate inf --warmup-requests 3 \
    --temperature 0 --seed 42 \
    --output-file "/tmp/ticket03-s5-$mode/bench-$round.jsonl" \
    2>&1 | tee "/tmp/ticket03-s5-$mode/bench-$round.log"
done
```

每个输出文件须只有本次一条JSONL记录；重跑请换目录，benchmark可能追加记录。
比较三轮中位数及各轮原始值：`mean_ttft_ms`、`mean_tpot_ms`、`mean_itl_ms`、
`output_throughput`；同时看`p95_ttft_ms/p95_tpot_ms`及`completed=64`。
每轮`total_input_tokens=8192`、`total_output_tokens=4096`，异常/失败请求不能算性能通过。
TTFT/TPOT/ITL单位ms，throughput单位输出token/s。保留benchmark的测量窗口，不使用包含
idle的server滚动吞吐。保存双方启动环境文件、测量时的`npu-smi info`及服务完整日志；
对比resources/mapping_ready的HBM、host、BM字节数和`mempool drain`耗时。

把三轮mean_ttft_ms、mean_tpot_ms、output_throughput原始值及各自中位数交付用户，
由用户查看后确认是否满足预期。ITL、P95和formal/native比值作为诊断数据一起保留。
没有实测结果或用户确认时，性能状态仍为待验收；不把预先提供数值阈值作为运行前提。
性能问题若来自S5接线或同步，在S5修复并重测。

## 回传与关闭条件

回传两侧commit/环境、组件gate的JSON/log、完整P/D/router日志、service-result.json、
curl输入/输出/人工结论、两模式各三轮benchmark JSONL/log及用户性能判断。
不要只截最后一行ALL_CHECKS_PASSED。硬件失败后保留日志和请求参数，停止两侧旧实例
再按修复后的命令重测；单侧重启不延续旧BM session。

服务checker只自动判定执行链路。组件数据校验、真实服务、用户curl回答和已约定性能
全部通过并获用户确认，才完成S5并解锁S6。S6清理后的版本还需复验。
