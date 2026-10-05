# Ascend mempool 功能与服务测试

Ticket 01、02③及03 S2/S3的独立硬件验证入口，以及02④的真实服务 shadow gate。

**当前状态（2026-10-05）：** 01、02已获用户确认验收并关闭。
[02总结](../.scratch/ascend-mempool/ticket-02-summary.md)记录代码路径、链路和实际证据；
[小容量真实KV读回](READBACK_SERVICE.md)保留为回归入口，配置为context1024、
P/D各512、TP16、D Graph width16。下一开发入口是
[03正式attention切换](../.scratch/ascend-mempool/issues/03-prefill-direct-offload.md)。
03 S1的配置与资源职责拆分已实现，继续使用02 shadow数据路径；完整环境/NPU回归
待执行，见[S1交付与复测](../.scratch/ascend-mempool/ticket-03-s1-summary.md)。
03 S2正式BM fetch代码已核对，Mac检查通过；K=2048双机独立NPU gate的30个case
通过，用户于2026-10-04确认。用户于2026-10-05确认S3资源gate通过；
03 S4传输精简代码与独立双机脚本已交付，等待用户NPU验收，见[S4 gate](PD_TRANSFER.md)。
03整票仍为open；现有服务仍启动shadow，正式cutover待S5；见
[S2修改清单与双机命令](../.scratch/ascend-mempool/ticket-03-s2-summary.md)。
NUMA/大容量分配排查已延期到ticket09。
BM 双机测试的目标环境为同一 superpod 的两台 Ascend 机器，S3资源gate只需单机。
原 graph/writer gate 每侧使用一张 NPU，BM启动诊断可选1到16张；使用MemFabric Hybrid
**1.1.4**。不启动 SGLang server、router 或模型。
BM API 参考本地 `release/1.1` 的 `9fa9afbb`；两端运行时版本写入报告并相互核对。

## 目录与职责

```text
ascend-mempool-test/
  src/ascend_mempool/  production 模块加载入口、验证数据
  src/ascend_sparse/   sparse生产模块加载入口及materialization测试资源fixture
  scripts/            双机测试入口与两轮 gate runner
  tests/unit/         CPU 行为测试
  reports/            默认运行日志与 JSON 报告，已 gitignore
```

02 第一部分已将 `layout.py` 与 BM manager/view 移入
`python/sglang/srt/hardware_backend/npu/mempool/`，并新增容量配置与 runtime offload。
本测试 package 直接加载这些模块，绕过 SGLang public API；`pool.py` 保留兼容 import。
02 读回阶段也将 `copy.py` 提升至该生产目录，独立 gate 和真实服务共用其路由实现。
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

CPU 测试需要 CPU PyTorch、NumPy 和 msgspec。用独立虚拟环境安装开发检查工具，不安装 SGLang：

```bash
python3 -m venv /tmp/ascend-mempool-dev
/tmp/ascend-mempool-dev/bin/pip install torch numpy msgspec mypy ruff isort
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
`test_sparse_config.py`覆盖普通/shadow/预留正式模式的资源能力、Index K容量计算及
非法启动组合。真实native pool构造和runner启动配置测试位于SGLang registered suite，
需要完整SGLang依赖和受支持的Python版本；执行命令见S1交付说明，不能以轻量suite
通过代替该集成检查。
`test_fetch.py`和`test_materialize.py`覆盖READBACK关闭的BM读取、实际writer前缀、
HBM hit/refill/reset、原host miss分支、实际attention输入、短top-k/多row/padding，
以及目标更换后Graph metadata的持有。测试fixture不执行正式服务资源构造器。
`test_fetch_gate.py`验证fetch CLI的固定K=2048约束、双机setup前拒绝非法宽度，
以及原copy-only gate的可变K兼容性；materialization另覆盖固定宽度下的小context和padding。
`test_sparse_resources.py`执行真实manager构造/host读写/请求hooks，验证正式模式旧资源为零、
普通/shadow恢复旧路径，并覆盖实际PD adapter的方法体。CPU替换SDK和serving allocator边界；
完整构造链及真实NPU copy用下面的S3 gate验证。
`test_pair_startup.py` 检查 `P_i/D_i` 的 store 端口及 BM rank 映射、启动参数和失败清理，
并用模拟时钟覆盖 P 晚90秒监听、P始终不可达、TCP连接超时、P不等待自身store及SDK错误直报。
生产 BM 启动入口位于 `MempoolKVManager.initialize_rank_pair()`；01 gate 保留原测试
初始化与控制流程，其通过记录不能替代新入口在真实 16 对 worker 中的验收。

## 03 S3：旧 host/staging 资源停用 gate（用户已确认通过）

2026-10-05用户反馈“S3 gate通过”。交付提交为`5b18a8046c`；本次未附远端实际SHA、
环境明细或报告内容。以下命令和判据保留为复跑入口，证据范围见[S3确认记录](../.scratch/ascend-mempool/ticket-03-s3-summary.md)。

新增`scripts/verify_resources.py`，使用实际NPU native/sparse pool、request/page allocator。
它检查正式P保留native K/V，正式D保留Index K和HBM cache，旧host SHM/mapping及
main-KV staging为零；普通/shadow执行staging→host、旧host写入/读取和miss→hit回归。
请求row复用和clear也必须通过。正式服务仍受S5保护，环境开关仍选择shadow；
此gate显式传入组件mode，既不启动模型也不连接BM peer。

前置条件：当前checkout的完整SGLang环境、受支持的Python（用户现有3.11）、
torch_npu、sgl_kernel_npu及CANN配置；任选一台机器的空闲NPU，下面使用device0。
各mode在独立进程运行；没有P/D启动先后、NIC或端口要求，不使用Python `-O`。
仓库根目录执行：

```bash
bash -o pipefail <<'SH'
set -eu
export PYTHONPATH="$PWD/python${PYTHONPATH:+:$PYTHONPATH}"
git rev-parse HEAD > /tmp/ticket03-s3-version.txt
for mode in pd_prefill_mempool pd_decode_mempool local_offload pd_decode_offload pd_decode_mempool_shadow; do
  python3 -u ascend-mempool-test/scripts/verify_resources.py \
    --mode "$mode" --device-id 0 \
    --report "/tmp/ticket03-s3-${mode}.json" \
    2>&1 | tee "/tmp/ticket03-s3-${mode}.log"
done
SH
```

固定配置：2层、2个真实request rows+row0、context16、page128、native tokens256、
KV维度512+64、Index K128、BF16、top-k2048。top-k仅位置0/2有效，其余-1。

| mode | host KV/映射/length metadata | staging bytes | HBM cache | native K/V | Index K |
| --- | --- | --- | --- | --- | --- |
| `pd_prefill_mempool` | 0 | 0 | 0 | >0 | >0 |
| `pd_decode_mempool` | 0 | 0 | >0 | 0 | >0 |
| `local_offload` | >0 | 0 | >0 | 0 | >0 |
| `pd_decode_offload` / `pd_decode_mempool_shadow` | >0 | >0 | >0 | 0 | >0 |

需要5条`RESOURCE_PASS`、5份`success=true` JSON及全部进程退出0；任意断言、
`RESOURCE_FAIL`、非零退出或缺失mode都判失败。回传`/tmp/ticket03-s3-version.txt`、
5份JSON及对应log。JSON记录实际保留buffer的字节数，正式D另将SHM分配入口设为失败陷阱。
PD adapter使用完整模块与构造器，只替换父transport启动/发送/状态sink；不发送真实跨机数据。
它不替代S4–S6的正式服务验证，S3代码完成也不代表ticket03关闭。
完整文件清单、Mac检查与registered回归命令见
[S3交付总结](../.scratch/ascend-mempool/ticket-03-s3-summary.md)。

## 03 S2：BM fetch / HBM cache / Graph gate（独立NPU验证已通过）

新增`scripts/verify_fetch.py`复用现有双机BM setup、已知内容和drain协议，直接调用
生产runtime、双源copy与cache materialization；READBACK关闭。测试覆盖P-only miss、
D-only miss、混合miss、refill后全命中、zero-valid、Graph row 0 padding、slot复用，
并验证capture后更换同形状eager目标，再重放原Graph。每轮D payload带独立标记，
避免用setup预填数据掩盖writer未完成。

小配置P/D各1GiB DRAM，Graph width16，3个真实rows，24/48 core各跑eager和两轮replay。
此gate调用真实`slot_map_lookup`，top-k张量宽度必须为**2048**；非空case每行只填2或4个
有效位置，其余列用-1补齐，P/D容量仍可为8/16。脚本默认2048，其他宽度在双机连接/
BM分配前拒绝。原`verify_graph.py`只验证copy，其可变top-k行为保留。
2026-10-04首次NPU运行使用初版命令K=8，在warmup失败且`checks=0`；随后K=2048
重测通过，用户确认无问题。D日志完整包含30条`FETCH_PASS`，P/D均有
`ALL_CHECKS_PASSED`；所有数据与独立host reference逐元素一致，全命中时
P/D BM copy行数都为0。两层的P-only/D-only/mixed计数分别为`[6,0]`/`[0,6]`/`[6,6]`。
此次回传的是控制台日志，JSON内容与机器实际Git SHA未独立核查；证据边界见S2总结。
此gate不加载模型、执行attention算子或验证正式服务分配/PD传输；生产attention接线
由CPU输入测试覆盖，实际NPU attention与正式服务smoke留待整票交付。
完整环境准备、逐机命令、计数矩阵、失败判据及日志清单见
[S2交付总结](../.scratch/ascend-mempool/ticket-03-s2-summary.md)。

## BM多卡启动诊断

当前偶数NUMA规避验证用 `run_bm_startup_gate.sh --even-numa`：两侧固定device0–15，
每池1GiB，NUMA0/2/4/6各4池，并自动核对HAL成功分配、peer probe与进程退出码。
完整逐机命令与成功判据见[偶数NUMA测试](BM_NUMA_DIAGNOSTIC.md)。下文不带该选项的
原诊断模式保留11GiB/rank容量。

用于区分单pair正常、模型服务中的16pair创建很慢这一现象。2026-10-01用户反馈
device0单pair的1GiB和11GiB writer gate均通过；11GiB的HalMemCreate耗时P约1.17秒、
D约2.01秒。下一步只验证多卡同时持有BM池，避免重复大量KV读写和Graph检查。

`verify_bm_startup.py`复用`verify_graph.run()`和生产`MempoolKVManager.create()/join()`。
每个worker使用72层、16slots、S_P=S_D=8192、dim576的逻辑布局，对齐后每侧贡献
11GiB。实际只写/readback各自64字节的peer probe；不填充逻辑KV或执行attention/Graph。
每个worker校验探针后等待本机所选devices全部ready，所有池同时存活后才进入原双侧
drain/close握手。这验证并行启动和同时占用，不保证16个HAL调用在同一时刻进入。

在原P/D容器、相同checkout中运行，保持模型服务停止，先P后D。默认选择device0–15，
每侧总共176GiB BM DRAM。先用`MEMPOOL_TEST_DEVICES=0`可做新诊断入口的单pair对照；
两侧必须使用相同device列表。独立pair按device i使用P store `18773+2*i`、
test control `18774+2*i`、本机NIC `25670+2*i`（相邻端口预留给SDK）、pool ID103。
这些端口布局沿用单pair writer gate，与真实service的store布局不同。

P容器：

```bash
bash ascend-mempool-test/scripts/run_bm_startup_gate.sh \
  0 10.120.72.31 10.120.72.31 /tmp/mempool-bm-startup-p
```

D容器：

```bash
bash ascend-mempool-test/scripts/run_bm_startup_gate.sh \
  1 10.120.72.31 10.120.72.32 /tmp/mempool-bm-startup-d
```

runner为每次调用新建`REPORT_DIR/run.XXXXXX`，打印完整路径，保存每卡`.log/.json`及
`pids.tsv`（device、容器可见PID）。同时启动各worker后等待它们退出。
成功要求两侧均有`ALL_BM_STARTUP_CHECKS_PASSED`，每个worker有`PROBE_VERIFIED`、
`LOCAL_POOLS_READY`和`[BM_STARTUP] PASSED`，JSON `status=passed`及一个
`bm_peer_probe` check。该结果不是writer/Graph或TP控制验收。

`MEMPOOL_TEST_PYTHON`、`MEMPOOL_TEST_TIMEOUT`默认分别是`python3`、600秒。
`MEMPOOL_TEST_DEVICES='0 1'`可选择子集；`MEMPOOL_TEST_DRY_RUN=1`只打印命令，
不启动worker。原生HAL调用可能阻塞在SDK内部，600秒不能保证打断这种调用。
超时、probe错误、缺少worker或非零退出均不能视作通过。runner不会自动kill其他worker；
中断runner也不表示所有后台worker已退出。需排查时按`pids.tsv`确认存活进程，
保持P/D进程可供抓栈；原gate对无法确认peer drain的pool仍采取保留策略。

回传该轮两侧run目录中的日志、JSON和PID表。可以先提取每个日志的
`BM_STARTUP|Creating mempool BM|Mempool BM|Try HalMemCreate|alloc mem success|Traceback|Error`
行，以区分HAL分配、create/join、映射和本机ready等待。
若16pair独立BM通过，继续检查模型已加载、D hostSHM、进程NUMA/cgroup限制和服务初始化
上下文的差异；若失败，先按device/分配耗时定位，不能直接认定为并发死锁。

2026-10-01用户回传本入口两侧device0–15全部PASSED，P报告目录为
`/tmp/mempool-bm-startup-p/run.fv3xez`，D为`/tmp/mempool-bm-startup-d/run.dKTy4b`。
这证明无模型条件下16pair、每侧176GiB同时占用的路径通过；本次回传不含逐卡耗时或JSON，
不能据此断言与单pair一样快，也不替代真实SGLang服务启动验收。

## 真实服务BM启动重测：增加诊断输出

更新两端相同版本的SGLang后，在原Docker中重跑P/D服务。复现此前卡住条件时，保留
该轮模型、容量、context和CPU绑定参数；小容量对照见下节。开启以下诊断变量：

```bash
export SGLANG_NPU_MEMPOOL_DIAGNOSTICS=1
export PYTHONUNBUFFERED=1
```

然后分别运行原P/D启动脚本，例如在已有`ascend-sglang-script`目录内执行
`bash pd-disaggregation/glm51mempool.sh`（沿用各机已经设置好的`LOCAL_HOST1`等配置）。
当前脚本默认采用下节的1024/512/512小容量配置；复现旧条件需恢复对应长度。
先P后D，P开始等待BM时就启动D，无需等P服务ready。该脚本已有`tee`保存
`/tmp/mempool-02-service-small/p.log`或`d.log`；重跑前保留旧日志。此次诊断不要求先启动router
或发送请求。首先核对`CONFIG`/`bm.create2`中的实际字节数；若与旧记录11811160064不同，
说明容量条件也变了，应随结果注明。

在两侧另一终端跟踪各自的完整日志：

```bash
# P
tail -F /tmp/mempool-02-service-small/p.log
# D
tail -F /tmp/mempool-02-service-small/d.log
```

每条`[MEMPOOL_INIT]`包含步骤以及PID；`END`给出耗时，超过15秒未返回会持续`WAIT`。
`CONFIG`给出原hostSHM占用，`SNAPSHOT`给出容器/主机内存与实际执行线程等待位置；
`MAPPING_PENDING`给出缺失的rank/GVA/offset。完整阶段说明及代码流程见
[mempool模块说明](../python/sglang/srt/hardware_backend/npu/mempool/README.md)。
两侧全部16个worker的`[MEMPOOL_INIT] READY`表示BM/runtime已完成，后续仍需Graph和PD
控制握手以及服务ready。出现FAIL、映射超时或清理阶段卡住时均保留完整上下文。

如果仍卡住，先保留两侧进程，收集至少两次WAIT再抓取日志；原生HAL调用并不保证能被
`--mempool-timeout`打断。回传两侧完整`p.log/d.log`、`git rev-parse HEAD`和实际启动参数；
可先用下面的过滤命令摘取关键信息（每机使用自己的日志文件）：

```bash
rg 'MEMPOOL_INIT|Mempool BM|Try HalMemCreate|AllocReserve|alloc mem success|export vmm|import rank|Mmap|Traceback' \
  /tmp/mempool-02-service-small/d.log
```

完成诊断后移除`SGLANG_NPU_MEMPOOL_DIAGNOSTICS`即可在下一次启动关闭后台采样和主动
设置MF INFO；基本阶段日志保留。诊断仅增强可观测性，尚未认定或修复服务卡住的根因。

### 按 TP rank 分配 NUMA（可选）

需要显式分配时，在P、D各自的容器中列出希望使用的本机NUMA节点。
以下选择0/2/4/6，在`ascend-sglang-script`目录运行已有服务启动脚本，先P后D：

```bash
export SGLANG_NPU_MEMPOOL_LOCAL_NUMA_NODE=0,2,4,6
bash pd-disaggregation/glm51mempool.sh
```

服务使用`numa_node = nodes[tp_rank % len(nodes)]`按列表顺序轮转。
上述配置中TP0/4/8/12→NUMA0，TP1/5/9/13→NUMA2，TP2/6/10/14→NUMA4，
TP3/7/11/15→NUMA6。支持单节点、不连续ID及逗号两侧空白，P/D可使用不同列表。
创建前用本机`/sys/devices/system/node/online`校验整份列表；若任一节点不存在或
未在线，会打印WARNING并整体回退默认分配`flags=0`，不会只保留有效节点。
空值、重复ID、非法格式、ID不在0..126内、拓扑读取/解析失败也提示后回退。
未设置变量时直接使用默认策略；回到默认模式用
`unset SGLANG_NPU_MEMPOOL_LOCAL_NUMA_NODE`。旧的
`SGLANG_NPU_MEMPOOL_LOCAL_NUMA_NODE_COUNT`已取消。配置生效需重启进程，已存在的pool不会迁移。

独立`run_bm_startup_gate.sh`也继承该变量；其每卡worker用`device_id`作为模拟TP rank，
按同一规则分配。这一入口原有的11GiB/rank容量不变，应先确认每节点累计预算。
真实服务仍可保留下一节的1GiB/rank小容量，先检查NUMA选择再扩大容量。

核对每个rank的`Creating mempool BM pool`日志：`local_numa_nodes=0,2,4,6`，
`numa_node=2*(tp_rank % 4)`，`bm_flags=128+numa_node`；对应字段也会出现在
`BEGIN stage=bm.create2`中。
这些字段记录请求的策略；开启上述启动诊断后，还应核对MF的`Try HalMemCreate`节点、
返回值和分配前后NUMA内存快照。P/D全部rank须完成BM/runtime ready，独立gate须两侧
全部PASSED；显式配置回退、参数不匹配、HAL错误或持续卡住均不算偶数节点验证通过。
回传两侧完整日志、实际在线节点和选择列表、启动参数和代码版本；
CPU测试不证明NPU机器的物理落点或内存压力下的驱动行为。

### 小容量服务对照（2026-10-02）

用户确认关闭D侧CPU亲和性后仍卡住，下一轮恢复`SGLANG_SET_CPU_AFFINITY=1`。
两侧`glm51mempool.sh`使用以下参数，TP16、16 slots和D Graph BS16沿用原配置：

```bash
--context-length 1024 \
--mempool-prefill-capacity 512 \
--mempool-decode-capacity 512
```

按此前GLM-5.1日志的78层、576维BF16、D原hostSHM 17行及context额外4列计算：

| 对照 | context | S_P / S_D | D原hostSHM，16 ranks | BM本地贡献，每机16 ranks |
| --- | ---: | ---: | ---: | ---: |
| 旧服务配置 | 8192 | 4096 / 4096 | 186.56 GiB | 96 GiB |
| 本轮小容量 | 1024 | 512 / 512 | 23.40 GiB | 16 GiB |
| 后续只放大BM | 1024 | 4096 / 4096 | 23.40 GiB | 96 GiB |

BM包含64字节probe并按1GiB对齐；实际大小以两侧`[MEMPOOL_INIT] CONFIG`为准。
小容量同时改变hostSHM、BM及部分context相关缓冲，单轮通过不能独立归因为hostSHM。
先确认两侧全部16 ranks的BM/runtime就绪、D Graph capture完成及服务ready，再使用
下文的短prompt/32输出token请求和日志检查验证shadow生命周期。
若后续恢复ticket09的NUMA排查，可保持context=1024，仅恢复两侧S_P/S_D=4096，
使用新的`LOG_DIR`重复运行；当前demo不执行这项容量对照。
该对照进一步区分context相关内存压力和BM容量影响；仍不能宣称只改变了hostSHM。

从服务启动前开始采集NUMA状态，保留本轮P/D完整日志、实际参数与`CONFIG`字节数。
若仍卡住，记录具体`WAIT stage`和内核栈，不将不同阶段的等待合并为同一个故障。

## 02 Ascend 控制协议检查

② 的协议与单 rank 状态机不依赖 SGLang server；新增 service/tick 检查使用 CPU torch，
因此统一使用上面的开发虚拟环境：

```bash
PYTHONPATH=ascend-mempool-test/src /tmp/ascend-mempool-dev/bin/python -m unittest discover -s ascend-mempool-test/tests/unit -p 'test_pd_*.py' -v
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
真实16-rank控制消息、P/D双写和Graph请求接线已加入④，尚待下述GLM-5.1服务验收。

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
02的runtime writer、真实server shadow双写、top-k读回、Graph和正常释放/物理slot复用
已于2026-10-03经用户确认通过并关闭，详细证据见[02总结](../.scratch/ascend-mempool/ticket-02-summary.md)。

## 02③ Runtime writer gate

③增加 `mempool/rows.py`、`runtime.py` 和 backend attach/write hook。P/D runtime
均通过 attention backend 使用；③提供数据路径和接口，④才接入配置、BM startup、
control tick、准入与 drain。因此此 gate 不启动 GLM5.1 server，也不宣称服务已通过。

`rows.py` 有意复制原 `SparseKVCacheManager.offload_v2` 的行推导，当前不修改原函数。
修正 padding 或 validity 时需核对两份逻辑；后续补特征测试后再考虑共享抽取。

调用顺序如下，eager 和 replay 都必须具有 host forward 边界：

```text
tick 批准 → binding = bind(req_pool_idx, slot=slot, prompt_tokens=prompt_tokens)
service 按 request attempt 保存 binding；真实请求投影 req.kv.req_pool_idx
  → assert_bound(req.kv.req_pool_idx, binding)
begin_forward([KVWriteExpectation(req_pool_idx, full_position, rows)])
  → eager: 每层 write_layer(...)
  → replay: begin_forward(..., replay=True) 后 graph.replay()
end_forward()  → 在相同提交 stream 上记录设备计数快照和完成事件
poll_completed() → 事件完成后核对每层/每 slot 的实际有效行数
  → 本地 writes_done / prompt_ready 供 control tick 使用
本地 completion 已消费 + 无未来 row 提交 + native 安全条件 + tick 批准
  → receipt = detach_row(binding)，按 attempt 保存完成事实，再归还原生资源
```

capture 使用 `begin_forward([], capture=True)`，要求没有真实 binding 或 pending work，
捕获全部 invalid 的每层 writer，
然后 `end_forward()` 验证层覆盖。replay 不执行 Python write_layer，不能依赖该函数
做每次 replay 的 host 记账。binding 表和设备计数地址保持固定；forward 设备字段使用
Graph 自身的固定输入，更新与 replay 在同一提交 stream 排序。

`bind/detach_row` 不能发生在 open forward 或该 request 的未消费 completion 期间。
event 已完成也必须先 `poll_completed()`。binding 更新
事件由下一次 begin_forward 等待；binding 更新统一在同一 scheduler stream 提交，
跨 scheduler/forward stream 的安装顺序明确。
写入、有效行计数及快照都在 forward producer stream，临时 source 不跨流。
`prompt_ready` 只证明本地写入完成；发布 KV_READY 还需远端可读性保证。
取消、远端读取 drain、DONE 和 slot ownership 由④负责，runtime 不自行发送消息或释放。

`KVRowBinding` 是 `bind()` 返回的本进程 attachment 对象，字段为 row、slot 和
prompt length；接入层按已批准的 request attempt 保存并传回同一个对象。
`assert_bound(row, binding)` 拒绝 missing/stale attachment，即使旧 row/slot/prompt
数值与新请求完全相同也不能通过。不要重建或跨进程序列化这个 attachment；TP 协调
使用 control 的协议 snapshot。runtime 不解析 Req/fake marker；④的 service 负责
真实 `req.kv.req_pool_idx` 投影、attempt 匹配和既有 fake 请求过滤。

`KVWriteReceipt` 不可变，保存 binding 和 submitted/completed 本地行数；detach 后
用它查看旧请求进度，不再通过已复用的 row 查询。P 的正常 detach 须等原 native
handoff 成功及本地安全，不必等整个 decode；**KV_READY 单独不足以允许 detach**，
旧 transfer 仍可能通过 staging 读取 HBM。P slot 继续由 control 保留到对应 DONE，
D 则先 whole-D drain 再 detach。取消/失败还须证明原 transfer 安全。

writer 只有 `write(values, *, slots, positions, valid)`；`MempoolWriteInputs` 已删除。
runtime 管理固定 binding 表，writer 不持有另一套输入缓存。边界检查和全 invalid
时仍 launch 的行为保留。

`MempoolPDControl.snapshot()` 返回 frozen dataclass、tuple 和 frozenset，可序列化，
包含当前 peer/fault、各 request 的 phase、P/D binding、readiness、待确认消息及
available slots。`owns_slot` 表示本侧当前实际占用；旧 record 的 slot 字段可能只是
保留的确认信息。`binding_confirmed/writes_pending` 为 P 事实，`transfer_ready` 为 D
事实。读取不消费 inbox、不转换状态，不暴露内部可变 record；service/tick 每次从
control 取新观察值，不能维护另一份可变协议状态机。

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
真实 Req 字段投影与 missing/stale attachment、row reuse/旧 slot 保留、control
snapshot 不可变，以及同一套gate案例的完整CPU参考值。真实 service 的 fake 过滤
属于④；Mac 不执行实际NPU Graph。
③与这个runtime gate同一轮交付NPU测试；当时ticket02保持open，等待④及服务readback。
后续两阶段验收已完成，当前状态见本页开头和02总结。

2026-10-01 接口优化后，`verify_writer.py` 已改为保存 `bind()` 返回值并调用
`detach_row(binding)`。上述两机命令、20条 checks/10条 replay 判据不变。
10月1日用户反馈本轮双机回归两端通过，日志均含20条PASS（10条replay）及
ALL_CHECKS_PASSED；交付版本为 `cfcafb4810`，远端hash与JSON文件未独立核验，
完整记录见 ticket02。本 gate 不替代④的真实 native handoff/TP 生命周期验收。

## 02④ 真实 GLM-5.1 shadow 服务 gate（已验收，保留回归入口）

④已接入 server 初始化、TP tick、真实请求准入、forward scope 和 native 回收。
本gate已由用户在NPU验收通过。后续复测先核对代码，再把同一版本部署到两侧；
保存各侧 `git rev-parse HEAD` 和 `git diff --stat`，避免只更新其中一台。

这一轮继续使用原 native KV/Index K/metadata 传输与 attention，P/D额外写入mempool。
在已反馈成功的小容量服务基础上，开启独立 BM top-k 读回，与旧路径 selected KV 比较；
逐层写入计数、控制生命周期同时检查。具体实现和证据边界见
[真实 KV 读回说明](READBACK_SERVICE.md)，AIME 精度验收仍留在后续阶段。

### 启动参数增量

基于你在NPU上已经跑通的 `glm51dis.sh` 修改，保留本机权重、网卡、IP和DeepEP配置。
两侧都增加环境变量：

```bash
export SGLANG_NPU_ENABLE_SPARSE_KV_OFFLOAD=1
export SGLANG_NPU_ENABLE_MEMPOOL=1
export SGLANG_NPU_MEMPOOL_READBACK=1
export SGLANG_NPU_MEMPOOL_LOCAL_NUMA_NODE=0,2,4,6
export SGLANG_DISAGGREGATION_BOOTSTRAP_TIMEOUT=600
```

两侧 `sglang.launch_server` 命令都追加以下参数，将占位符替换为实际地址。
`<P_BOOTSTRAP_PORT>` 必须与P已有的 `--disaggregation-bootstrap-port` 一致；样例是8995。

```text
--mempool-prefill-host <P_IP>
--mempool-bootstrap-port <P_BOOTSTRAP_PORT>
--mempool-base-port 19000
--mempool-pool-id 104
--mempool-nic tcp://<LOCAL_IP>:25670
--mempool-prefill-capacity 512
--mempool-decode-capacity 512
--mempool-timeout 600
```

`<LOCAL_IP>` 在P填P的MF网卡地址，在D填D的地址。端口关系：

| 用途 | 本轮示例 | 说明 |
| --- | --- | --- |
| BM store | P:19000–19015 | P_i启动，D_i连接 `19000+i` |
| MF NIC | 每侧25670–25701 | pair i传入 `25670+2*i`；MF再加BM rank 0/1，不与原TransferEngine端口共用 |
| 控制消息 | 既有Ascend ZMQ rank端口 | 通过P原bootstrap HTTP registry发现，不开第二套ZMQ reader |

`--mempool-timeout` 用于BM启动、控制peer心跳、取消后的native drain以及tick watchdog。
request acquire等待仍使用原PD bootstrap timeout，不因重试重新计时。
D进入 `bm.initialize()` 前，先按此参数等待对应P store的TCP listener；每次连接最多1秒，
失败后最多等待0.2秒，重试不重置deadline。MF 1.1的初始TCP连接默认仅重试60次，
不受 `BmConfig.init_timeout` 控制，因此必须在进入SDK前完成这段等待。
TCP等待、后续BM操作/映射等阶段分别使用该timeout，它不是整个模型加载/服务启动的总时限。
TCP可达只允许继续执行正式BM初始化，pool身份及映射仍按原流程校验；SDK错误直接报错。
P/D的device mapping查询在失败后等待1秒再重试，最后一次等待受剩余deadline限制。
每个rank各自轮询；MF对单次失败可能输出HYBM和SMEM两行，因此16个rank仍会有多行日志。

维持TP16、DP1、PP1、CP1、BF16、`--attention-backend ascend`、
`--disaggregation-transfer-backend ascend`、`--disable-radix-cache`；P保留
`--disable-cuda-graph`，D保留 `--cuda-graph-bs-decode 16`。不启用MLAPO、draft、
prefix复用、two-batch overlap或自动rebootstrap。普通scheduler overlap仍受支持。
一次新请求需使用新的bootstrap room，当前demo不接续同room的重试。

本轮两侧 `--context-length` 使用1024、`--max-prefill-tokens` 使用512，
保留16个running requests上限。mempool固定16个slots，原D hostSHM同时存在；
实际token预算和DRAM占用按你的机器调整，不能只按mempool容量推断整体内存。
本轮脚本只发送短prompt及最多32个输出token。

### 顺序与命令

1. 分别保存P/D完整新日志，例如 `/tmp/mempool-02-service-p.log` 和
   `/tmp/mempool-02-service-d.log`；不要追加到包含旧测试的日志。
2. 先启动P，随后启动D，不用等P服务ready。D模型加载更快时，会在
   `Waiting for P BM store tcp://<P_IP>:19000+i` 等待，期间每30秒报告剩余时间；
   P开始监听后出现 `P BM store ... is reachable`，再进入正式BM初始化。
   P等待D完成BM join是正常行为。P超过 `--mempool-timeout` 仍未监听时，D报
   `P BM store ... was not reachable within 600s` 并退出（数值随配置变化）。
   BM映射完成后D才capture；AscendKVManager建立后才握手control。
   两侧进入服务循环后，每个rank应出现 `mapping_ready` 和 `POOL_HELLO`/`POOL_READY`。
3. 启动已经验证过的PD router，沿用原P/D地址及bootstrap设置。
   不需要为mempool另起router或ZMQ服务。
4. 向该router发送三个串行请求：首token结束（零次decode）、实际decode、再来一个新请求。

```bash
python3 ascend-mempool-test/scripts/verify_shadow_service.py requests \
  --url http://<ROUTER_IP>:<ROUTER_PORT> \
  --decode-tokens 32 --timeout 900 \
  --output /tmp/mempool-02-service-requests.json
```

预期三条 `PASS case=...` 和 `REQUESTS_PASSED`。报告保存完整输入/输出、耗时和meta_info；
请同时查看生成文本是否正常。首个P输出token本身不需要D forward，后两项必须有真实Graph replay。

等待各rank的 `RELEASE_ACK` 后，把两侧完整日志放到同一台可运行Python的机器，执行：

```bash
python3 ascend-mempool-test/scripts/verify_shadow_service.py check-logs \
  --prefill-logs /tmp/mempool-02-service-p.log \
  --decode-logs /tmp/mempool-02-service-d.log \
  --requests 3 --require-readback --readback-layers 78 \
  --output /tmp/mempool-02-service-lifecycle.json
```

每个选项也可以传入该侧16个worker的独立日志。此gate假定设备ID为0–15，与现有样例一致。
通过输出为 `SHADOW_READBACK_PASSED`，JSON status为 `shadow_readback_passed`。
报告包含全rank的逐请求数值对照和不同attempt实际复用同一组P/D物理slot的证据。
request row独立记录在`rows`与`row_reused`中；D按FIFO轮换row不导致物理slot复用检查失败。
若尚未复用，等全部ACK后再次发送请求并保留同轮日志，详见[读回验收说明](READBACK_SERVICE.md)。
省略 `--require-readback` 时，仍可执行旧的
生命周期检查，结果为 `SHADOW_LIFECYCLE_PASSED (no KV readback)`，不能用于本轮数值验收。

检查器要求每侧16个rank的mapping、每个D设备的capture和真实replay、每个真实room的完整
`ACQUIRE/ACQUIRED/BOUND_ACK → READY + native transfer → decode → drain/DONE/ACK`
事件，以及最后全部16个slots可用。任何缺rank、缺replay、缺ACK、slot未归还、Traceback、
计数差异或协议fault都不通过。初始GVA映射重试后成功的既有MF日志不会被直接判错。

启动探测只建立/关闭TCP连接，不发送MF header或rank身份。MF 1.1的P listener可能为每个
pair记录一次 `Failed to read header from the socket connected from ...`；源码在登记peer前
关闭该探测连接并继续监听。只有紧邻上述探测、随后正式BM握手和mapping成功时，才能将
这一条视为探测日志；持续错误、正式握手失败或mapping超时仍需排查。
这次启动速度差异修复的NPU回归应保留D先完成加载的场景，并继续完成后面的服务请求gate；
Mac模拟测试不代替真实MF监听/握手及16对映射验证。

如果D停在BM启动阶段，按下面的阶段日志定位；TCP可达不等于BM初始化已经返回。

| 最后出现的阶段日志 | 尚未确认完成的调用 |
| --- | --- |
| `Initializing mempool BM pair`，没有 `Mempool BM initialize returned` | `bm.initialize()`，包括正式store连接/握手及HYBM初始化 |
| `Creating mempool BM pool`，没有 `Mempool BM pool created` | `bm.create2()`，包括本地内存分配与导出 |
| `Joining mempool BM pool`，没有 `Mempool BM join returned` | 原生 `handle.join()` |
| `Mempool BM join returned`，没有 `Mempool BM mappings ready` | 本地/远端device mapping尚未全部通过检查 |

初始化日志包含role、TP rank、PID、device ID和NIC，便于对应MF原生日志里的PID；
create日志包含实际local DRAM字节数和共同stride，join返回后记录P/D GVA base。
结合这些base判断失败地址属于哪个rank范围，不凭同一个十六进制地址猜测peer身份。
保存完整日志，再提取阶段行（分别在P/D机器执行对应行）：

```bash
grep -E 'mempool BM|Mempool BM|P BM store|Traceback|RuntimeError|TimeoutError' /tmp/mempool-02-service/p.log
grep -E 'mempool BM|Mempool BM|P BM store|Traceback|RuntimeError|TimeoutError' /tmp/mempool-02-service/d.log
```

P日志中 `ready` 只表明mempool prompt写完；`native_handoff` 后才允许
`row_detach/native_free`，其 `native_release` 事件通常仍显示P slot占用。
之后D `release send=DONE`、P `DONE send=RELEASE_ACK` 才结束persistent ownership。
事件含role/rank、room/attempt、P/D slot/generation、phase与free数；有动作的tick和drain记录耗时。

回传：两侧实际launch命令、环境版本/代码版本、完整P/D日志、两个JSON报告和异常文本。
基础取消路径在Mac已回归；容量压力、全套故障注入和精度矩阵继续归后续票。

### 故障后的停止

超时、失联或设备错误会报错终止，不能当作drain确认；不会合成DONE/ACK或自动销毁BM。
出现此类错误后同时停止接收新请求，协调停止两侧：确认D所有worker的NPU访问已停止，
再停止/回收P，确认两侧旧进程和NPU任务均结束后才重新启动完整16对。
不要只重启单个rank并接续旧session。
