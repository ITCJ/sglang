# Ascend mempool 独立功能测试

Ticket 01 与02③的独立硬件验证入口。目标环境为同一 superpod 的两台 Ascend 机器，
每侧使用一张 NPU，MemFabric Hybrid **1.1.4**。不启动 SGLang server、router 或模型。
BM API 参考本地 `release/1.1` 的 `9fa9afbb`；两端运行时版本写入报告并相互核对。

## 目录与职责

```text
ascend-mempool-test/
  src/ascend_mempool/  runtime 加载入口、双来源 copy、验证数据
  scripts/            双机测试入口与两轮 gate runner
  tests/unit/         CPU 行为测试
  reports/            默认运行日志与 JSON 报告，已 gitignore
```

02 第一部分已将 `layout.py` 与 BM manager/view 移入
`python/sglang/srt/hardware_backend/npu/mempool/`，并新增容量配置与 runtime offload。
本测试 package 直接加载这些模块，绕过 SGLang public API；`pool.py` 保留兼容 import。
因此运行测试需要完整仓库 checkout，仍不依赖 SGLang server 或 SGLang 安装。

- `KVLayout` 表达每 layer 的 `[B_slots, S, N, D]` BF16 逻辑布局，校验坐标和
  UniDexCopy 范围；`PoolLayout` 分别计算 P/D 实际贡献，使用相同的最大贡献作为 rank stride。
- `MempoolKVManager` 拥有一个 BM handle；`MempoolKVView` 暴露逻辑索引、dtype 和
  local-device address。view 持有 manager，pool 关闭后拒绝继续返回地址。
- `SparseCopyInputs` 持有固定地址的 device tensors；`SparseKVCopy` 在设备上生成
  P/D 的 `src_index`、`dst_index`、`valid`，通过两次 UniDexCopy 写入同一输出。
- `CopyCase` 和 host reference 提供可重复的内容、binding、length 和 mask 验证。
  这里的 TCP channel 只用于测试 rendezvous、数据就绪和 drain，不是未来的 PD 控制实现。

每个 layer 以独立 base pointer 传给 UniDexCopy，单 layer span 不超过 `UINT32_MAX`，
row 不超过 32 KiB；整个 rank allocation 可以超过 4 GiB。
使用 910C VMM DRAM 的 1 GiB 对齐，额外保留 64 字节 mapping probe，probe 不覆盖 KV。
没有另一份完整 CPU/HBM source KV；初始化按小块经过 temporary NPU tensor 写入 BM。
copy op 的 CPU BF16 dtype 占位只有 2 字节，实际行数和 BM device pointer 显式传入。

## 执行路径与释放条件

1. 两侧通过 test channel 检查协议、角色、布局、共同 stride 和 SDK 版本。
2. P 启动 BM store，D 连接；双方 join，验证各 layer 的 device mapping 和 peer probe。
3. P 写入 prompt KV，D 写入 decode KV；双方完成同步写入后交换 `DATA_READY`。
4. D 先执行 eager，然后按 24/48 core 分别 capture 一个 Graph。
   capture 输入为 `prompt_only`，D source zero-valid；后续 replay 改为其他 case，验证两条路径均已入图。
5. D 在全部检查后 synchronize 并销毁 Graph，发送 `DRAINED`。
   P 完成自己的 drain，释放 pool 并回复 `P_RELEASED`；D 再关闭并回复 `D_CLOSED`。

所有 eager/replay 输出均与独立 host reference **逐元素精确比较**，包括应保留 sentinel
的 invalid 和 padded rows。NPU copy latency 是含 synchronize 的观察数据，没有性能门槛。
无法确认 D drain 或本端 synchronize 失败时，进程输出 `DRAIN_UNCONFIRMED` 并保留 pool。
该失败场景由人工处理：先停止 D，确认其 NPU 工作停止，再停止 P；不会因测试超时自动重用存储。

## Mac 检查

布局检查使用 Python 标准库，可直接运行：

```bash
python3 ascend-mempool-test/scripts/verify_graph.py --describe
python3 ascend-mempool-test/scripts/verify_graph.py --describe --s-d 32768
```

CPU 测试需要 CPU PyTorch。用独立虚拟环境安装开发检查工具，不安装 SGLang：

```bash
python3 -m venv /tmp/ascend-mempool-dev
/tmp/ascend-mempool-dev/bin/pip install torch mypy ruff isort
PYTHONPATH=ascend-mempool-test/src /tmp/ascend-mempool-dev/bin/python -m unittest discover -s ascend-mempool-test/tests/unit -v
/tmp/ascend-mempool-dev/bin/mypy --config-file ascend-mempool-test/pyproject.toml ascend-mempool-test/src ascend-mempool-test/scripts
/tmp/ascend-mempool-dev/bin/mypy --config-file ascend-mempool-test/pyproject.toml python/sglang/srt/hardware_backend/npu/mempool
/tmp/ascend-mempool-dev/bin/ruff check ascend-mempool-test
/tmp/ascend-mempool-dev/bin/ruff format --check ascend-mempool-test
/tmp/ascend-mempool-dev/bin/ruff check python/sglang/srt/hardware_backend/npu/mempool
/tmp/ascend-mempool-dev/bin/ruff format --check python/sglang/srt/hardware_backend/npu/mempool
/tmp/ascend-mempool-dev/bin/isort --check-only --settings-path ascend-mempool-test ascend-mempool-test
bash -n ascend-mempool-test/scripts/run_gate.sh
```

CPU 测试使用真实 CPU tensor 运算和 BM SDK boundary fake，验证布局、索引、内容写入语义与
handle lifetime。它们不执行 BM 或 NPU kernel，不证明远端读和 Graph capture/replay 已通过。
`test_config.py` 覆盖实际 MLA 维度与 P/D 独立容量；`test_offload.py` 检查 raw destination
写入的内容、bounds/padding mask、zero-valid warmup 与固定 metadata buffer 的重复使用。
`test_pair_startup.py` 检查 `P_i/D_i` 的 store 端口及 BM rank 映射、启动参数和失败清理。
生产 BM 启动入口位于 `MempoolKVManager.initialize_rank_pair()`；01 gate 保留原测试
初始化与控制流程，其通过记录不能替代新入口在真实 16 对 worker 中的验收。

## 02 Ascend 控制协议检查

② 的协议与单 rank 状态机可用系统 Python 直接检查，不依赖 `torch` 或 SGLang server：

```bash
PYTHONPATH=ascend-mempool-test/src python3 -m unittest discover -s ascend-mempool-test/tests/unit -p 'test_pd_*.py' -v
```

有 `mypy` 时，可对新增运行时模块做严格类型检查：

```bash
mypy --config-file ascend-mempool-test/pyproject.toml \
  python/sglang/srt/disaggregation/ascend/mempool_protocol.py \
  python/sglang/srt/disaggregation/ascend/mempool_control.py
```

这些测试覆盖 wire 编解码、peer 兼容、acquire/binding、双条件 decode ready、
`DONE` 后释放、重复消息与取消时的写入排空。还会模拟 PD receive callback：
早于 control attach 到达的 tagged frame 会按序排队，损坏的 frame 使 mempool 准入报错，
但普通 PD frame 继续通过；测试也覆盖取消与 binding 确认乱序，以及终态记录回收后
迟到 `ACQUIRE`/`DONE` 的处理。控制表最多保留 4096 个近期 request 记录，
旧请求的 D generation 与每个本地 slot 的 retired generation 单独保留以防止重新占用 slot；
slot proof 把 request、P/D lease 绑定，供记录回收后的重复消息校验。
`RELEASE_ACK` 确认精确allocation已不再占用P资源，覆盖DONE释放及安全rollback。
只有实际释放才更新retirement边界，结合session、签名proof和owner检查处理旧DONE；
不再逐请求永久保存release proof，也没有累计65,536次限制。普通unbound CANCEL不新增ACK往返。
测试包含连续65,537次释放、rollback记录回收前后确认一致、伪造消息拒绝和新owner隔离。
这些测试不建立真实 ZMQ 连接或 BM pool。
真实 16-rank 控制消息、P/D 双写和 Graph 请求路径需待③、④接线后通过 GLM-5.1 服务验证。

## NPU 前置检查

在两台机器使用同一版本代码和已有的 Ascend 环境，包含 `torch`、`torch_npu`、
`memfabric_hybrid==1.1.4`、以及提供 `npu.unidex_copy` raw-pointer op 的 `sgl_kernel_npu`。
沿用现有 benchmark 已验证的 CANN/MF 环境和各机 NIC URL。
在现有 NPU 环境执行，避免用 CPU 测试环境替换 NPU PyTorch。

两侧各自执行：

```bash
python3 ascend-mempool-test/scripts/verify_graph.py --check-env --device-id 0
```

保存输出，确认 MF 实际版本为 1.1.4，UniDexCopy schema 有 `src_ptr`、`src_rows`。
两端版本、BF16/layout 和 shared configuration 不一致时，paired test 在创建 pool 前报错。
确保 P 的 18573/18574 和 18673/18674 端口可访问且未被占用。
两轮测试的 DRAM 需求分别为 P/D 各 1 GiB，以及 P 1 GiB、D 2 GiB。

## 双机完整 gate

以下命令从 SGLang 仓库根目录执行。替换 `<P_IP>`、`<P_NIC_URL>` 和 `<D_NIC_URL>`；
device ID 可改为该机器可用的卡。先启动 P，再启动 D；两端 runner 自动执行等容量和不等容量两轮。

P 机器：

```bash
bash ascend-mempool-test/scripts/run_gate.sh 0 <P_IP> <P_NIC_URL> 0 /tmp/mempool-01-p
```

D 机器：

```bash
bash ascend-mempool-test/scripts/run_gate.sh 1 <P_IP> <D_NIC_URL> 0 /tmp/mempool-01-d
```

必要时，两侧设置相同 `MEMPOOL_TEST_TIMEOUT=1800` 延长等待；
`MEMPOOL_TEST_PYTHON` 可选择现有 NPU 环境的 Python。
runner 使用 `pipefail`，任一轮失败会以非零状态退出。

每轮默认检查 10 种 case：prompt-only、decode-only、混合、部分 masked、最后 slot/边界、
short written length、一个真实请求与 padding、zero-valid、空 batch、zero prompt。
每个 core count 执行 10 个 eager checks、首次 capture replay 和 2 轮各 10 个 replay checks，
合计 **62 条检查记录**。改变 slot、length、index 和 valid mask 不重新 capture。

通过判据：两侧两轮均输出 `ALL_CHECKS_PASSED`；JSON `status` 为 `passed`；
D 每份报告含 62 条 `checks`；不等容量报告的贡献为 `[1073741824, 2147483648]`，
stride 为 `2147483648`。任何 mismatch、capture/replay 异常、timeout 或 cleanup failure 均不算通过。

## 单轮调试

需要调整参数时，两端使用相同 shared flags，P/D 分别指定 `--rank 0` / `--rank 1`
和各自 `--nic-url`。例如运行不等容量：

```bash
python3 -u ascend-mempool-test/scripts/verify_graph.py \
  --rank 0 --head-ip <P_IP> --nic-url <P_NIC_URL> --device-id 0 \
  --layers 2 --s-p 16384 --s-d 32768 --topk 64 --block-dims 24 48 \
  --report /tmp/mempool-p-debug.json
```

若需覆盖 16 个真实 row，双方加入 `--active-rows 16`；
若需较大的 sparse selection，双方加入 `--topk 1536`。
配置会先检查 per-layer 和 destination 范围，不支持的布局在分配前报错。

## 回传与当前状态

回传两侧 `--check-env` 输出、等容量/不等容量的 `.log` 和 `.json`、实际代码版本及使用的命令。
失败时保留完整 traceback、最后一个 PASS case、相关 MF 错误和 retained-pool 状态。
我们据此核对实现并调整脚本。Ticket 01 已于 2026-09-27 经用户确认验收并关闭。
02 的新增 runtime offload 与真实 server 集成仍待 NPU 验证。

## 02③ Runtime writer gate

③增加 `mempool/rows.py`、`runtime.py` 和 backend attach/write hook。P/D runtime
均通过 attention backend 使用；③提供数据路径和接口，④才接入配置、BM startup、
control tick、准入与 drain。因此此 gate 不启动 GLM5.1 server，也不宣称服务已通过。

`rows.py` 有意复制原 `SparseKVCacheManager.offload_v2` 的行推导，当前不修改原函数。
修正 padding 或 validity 时需核对两份逻辑；后续补特征测试后再考虑共享抽取。

调用顺序如下，eager 和 replay 都必须具有 host forward 边界：

```text
tick 批准 → bind(req_pool_idx, slot, prompt_tokens)
begin_forward([KVWriteExpectation(req_pool_idx, full_position, rows)])
  → eager: 每层 write_layer(...)
  → replay: begin_forward(..., replay=True) 后 graph.replay()
end_forward()  → 在相同提交 stream 上记录设备计数快照和完成事件
poll_completed() → 事件完成后核对每层/每 slot 的实际有效行数
  → 本地 writes_done / prompt_ready 供 control tick 使用
drain、远端安全与 tick 批准 → unbind()
```

capture 使用 `begin_forward([], capture=True)`，要求没有真实 binding 或 pending work，
捕获全部 invalid 的每层 writer，
然后 `end_forward()` 验证层覆盖。replay 不执行 Python write_layer，不能依赖该函数
做每次 replay 的 host 记账。binding 表和设备计数地址保持固定；forward 设备字段使用
Graph 自身的固定输入，更新与 replay 在同一提交 stream 排序。

`bind/unbind` 不能发生在 open forward 或该 request 的未完成写入期间。binding 更新
事件由下一次 begin_forward 等待；binding 更新统一在同一 scheduler stream 提交，
跨 scheduler/forward stream 的安装顺序明确。
写入、有效行计数及快照都在 forward producer stream，临时 source 不跨流。
`prompt_ready` 只证明本地写入完成；发布 KV_READY 还需远端可读性保证。
取消、远端读取 drain、DONE 和 slot ownership 由④负责，runtime 不自行发送消息或释放。

两机 gate 沿用01的环境/SDK/BM/test-channel/安全 teardown。默认两层、16 slots、
P每slot8 tokens、D每slot16 tokens、compact dim576；对齐后各机贡献1 GiB。
使用 P 的18773/18774端口、pool ID103。两端版本/配置必须一致，先启动 P，再启动 D。

P：

```bash
bash ascend-mempool-test/scripts/run_writer_gate.sh 0 <P_IP> <P_NIC_URL> 0 /tmp/mempool-02-writer-p
```

D：

```bash
bash ascend-mempool-test/scripts/run_writer_gate.sh 1 <P_IP> <D_NIC_URL> 0 /tmp/mempool-02-writer-d
```

与现有 runner 相同，可设置 `MEMPOOL_TEST_PYTHON` 和 `MEMPOOL_TEST_TIMEOUT`。
环境检查复用 `verify_graph.py --check-env`；新脚本也支持 `verify_writer.py --check-env`。
Mac 可执行 `verify_writer.py --describe`，只描述小测试布局。

每个24/48-core阶段都从 sentinel 重新开始，先 P 写/D远端读，再 D 写/P远端读。
D 使用真实 BM rank1/runtime decode 规则，避免在 P pool 上模拟错误的相对位置。
case 包含：ragged+MoE尾部padding+unbound、chunk prefix、同 slot内容改写、P最后slot，
decode全invalid捕获与replay、首行/下一行、不同prompt/slot重绑定、D最后一行。
最后一行前先 eager 写完整 prefix，再用同一个decode graph写 `S_D-1`，核对边界。
所有copy内容、未改动的slot及padding均由另一机器逐元素比较；完成事实还要通过runtime
设备计数检查，host期望行数不能代替实际copy内容。

通过判据：两侧 `ALL_CHECKS_PASSED`；各自JSON `status=passed`、包含20条 `checks`，
其中10条为decode replay。报错、内容不符、计数不符、timeout或teardown失败均不通过。
双方结束所有读写后交换 `WRITER_GATE_DRAINED`，才进入01已有的pool关闭握手。
无法确认安全drain时双方保留pool，不能超时强制复用；本 gate 双方均可能远端读，
不能沿用01“先停D再停P”的单向停止顺序。双向 retained pool 遇到 Ctrl+C 继续保留，
不执行 BM close；需先确认双方都停止远端读取，再协调终止测试进程。
回传两侧 writer `.log`/`.json`、版本/命令；NPU执行由用户完成。

Mac 新增 `test_rows.py` 与 `test_runtime.py`，覆盖四种行布局、chunk/local position、
容量边界、binding固定地址、漏/重复layer、invalid导致少写、意外额外写入、overlap快照、
fake admission及同一套gate案例的完整CPU参考值。Mac 不执行实际NPU Graph。
③与这个runtime gate同一轮交付NPU测试，ticket02保持open，仍需④及后续服务readback。
