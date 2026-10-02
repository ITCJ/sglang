# BM 本地 NUMA 分配交叉诊断

2026-10-02用户决定：NUMA/大容量分配排查延期，全部证据与后续事项集中在
[ticket09](../.scratch/ascend-mempool/issues/09-numa-allocation-followup.md)。
本文保留复现命令供后续使用；当前demo主线先完成真实KV读回、attention切换和旧hostSHM
移除，不要求继续节点扫描或重复已通过的BM gate。

## 双机16池：临时只使用偶数NUMA节点

当前代码通过`SGLANG_NPU_MEMPOOL_LOCAL_NUMA_NODE=0,2,4,6`选择节点，
实际服务和独立测试均按`nodes[tp_rank % len(nodes)]`轮转。
16个TP worker平均分到四个偶数节点；每侧布局如下：

| NUMA节点 | device / 模拟TP rank | 池数 | 本地BM DRAM |
| --- | --- | --- | --- |
| 0 | 0、4、8、12 | 4 | 4 GiB |
| 2 | 1、5、9、13 | 4 | 4 GiB |
| 4 | 2、6、10、14 | 4 | 4 GiB |
| 6 | 3、7、11、15 | 4 | 4 GiB |

`run_bm_startup_gate.sh --even-numa`固定使用此配置：两侧各16个worker，
每个worker一个两rank BM pool的本地贡献，1GiB/rank，16GiB/host。
实际布局为78层、16slots、512个prompt/decode tokens、BF16 dim576；
仅写入并验证64字节peer probe，不加载模型、不填充整个KV、不执行Graph。
每个worker在探针通过后等待本机16池全部ready，保证曾同时持有全部本地池，
随后完成双侧drain/close并退出。

先把本次修改同步到P、D相同版本的SGLang仓库，在原容器和Python/CANN环境中运行。
要求每机有device0–15、在线NUMA节点0/2/4/6，各有至少4GiB可分配余量；
停止模型服务及此前测试的遗留worker，保持设备和下列测试端口空闲。
沿用已有gate依赖：torch、torch_npu、MF，以及已安装的sgl-kernel-npu。
每对device i使用P store `18773+2*i`、control `18774+2*i`、
本机NIC `25670+2*i`及pool ID103，相邻NIC端口预留给SDK。

在P（10.120.72.31）先启动，无需等待测试结束就启动D：

```bash
cd /home/cryang/sglang
bash ascend-mempool-test/scripts/run_bm_startup_gate.sh --even-numa \
  0 10.120.72.31 10.120.72.31 /tmp/bm-even-numa-p
```

在D（10.120.72.32）的另一终端启动：

```bash
cd /home/cryang/sglang
bash ascend-mempool-test/scripts/run_bm_startup_gate.sh --even-numa \
  1 10.120.72.31 10.120.72.32 /tmp/bm-even-numa-d
```

此选项在runner内设置`SGLANG_NPU_MEMPOOL_LOCAL_NUMA_NODE=0,2,4,6`、启动诊断=1、
device0–15，覆盖调用环境中的节点列表或device子集。它只影响本次runner子进程；
随后重跑真实服务仍须在两侧设置
`export SGLANG_NPU_MEMPOOL_LOCAL_NUMA_NODE=0,2,4,6`。默认timeout=600秒，
可用`MEMPOOL_TEST_TIMEOUT`调整；`MEMPOOL_TEST_PYTHON`可指定服务器解释器。
`MEMPOOL_TEST_DRY_RUN=1`仅打印16条命令，不创建任何BM池，不能算硬件通过。

节点列表若含不存在或未在线的ID，manager会提示后整份回退为`flags=0`；其他非法
列表或拓扑读取失败也会回退。本gate要求明确选择0/2/4/6，因此回退后即使BM创建成功
也不会判为偶数节点测试通过。旧`SGLANG_NPU_MEMPOOL_LOCAL_NUMA_NODE_COUNT`已取消。

runner打印本轮唯一`reports=.../run.XXXXXX`目录，每卡保存`.log/.json`，另有
`pids.tsv`与`exits.tsv`。所有worker退出后自动生成`even-numa-summary.json`，
校验每个worker的退出码为0、两侧贡献各1GiB、peer probe通过、本机16池共同ready、
请求的NUMA/flags与HAL成功分配节点一致，以及0/2/4/6各有4个成功池。
同一偶数节点内的页大小fallback允许；任何奇数节点请求、缺少日志、分配失败、
探针失败、缺失worker或RESULT之后SIGABRT/134都会判失败。

两侧均须退出0且打印：

```text
ALL_EVEN_NUMA_CHECKS_PASSED rank=0 pools=16 counts={0: 4, 2: 4, 4: 4, 6: 4} ...
ALL_EVEN_NUMA_CHECKS_PASSED rank=1 pools=16 counts={0: 4, 2: 4, 4: 4, 6: 4} ...
```

回传两侧终端最终输出、`even-numa-summary.json`、`exits.tsv`及失败卡完整`.log/.json`。
HAL日志确认成功调用所用的NUMA参数；若要确认物理页落点，可结合诊断中的各节点
内存增量，后台内存活动会影响这项观测。通过只说明此配置的BM创建、映射、探针与释放
完成；真实服务、Graph及KV内容仍需后续验收。

沿用原runner的故障行为：SDK内部阻塞未必受600秒timeout约束，无法确认peer drain
时gate可能保留池。没有最终汇总或进程未退出不算通过；按本轮`pids.tsv`和两侧日志
定位存活worker。runner不会自动清理其他进程。

### 2026-10-02 双机实测结果

用户在交付`5b100c0001`后回传此模式两侧终端汇总。当时该模式使用旧COUNT=8配置，
仍对应同一0/2/4/6分配；新列表后来已在D实际服务日志中确认生效，但该轮大容量
全部rank的最终结果尚未收到。P目录
`/tmp/bm-even-numa-p/run.aAPaAl`，D目录`/tmp/bm-even-numa-d/run.EreEQi`。
两侧device0–15全部PASSED，均有`ALL_EVEN_NUMA_CHECKS_PASSED`，
counts均为`{0:4,2:4,4:4,6:4}`。本轮32个worker均正常退出，HAL节点、64字节
peer probe与同时持池检查通过，未重现create2失败或退出134。用户确认结果无异常。

偶数节点规避在独立1GiB/rank配置下通过。服务使用该策略时，两侧启动前须显式export
`SGLANG_NPU_MEMPOOL_LOCAL_NUMA_NODE=0,2,4,6`；测试runner中的export不会设置其父shell。
原始逐卡日志和JSON仍在上述服务器目录，本轮仅收到终端汇总；此结果不证明奇数节点
故障已修复，也不替代真实模型KV内容验证。当前执行顺序见ticket02；NUMA实验按09延期。

## 单device/node交叉探针

用于区分 2026-10-02 TP16 服务中 `HalMemCreate ret:6` 与请求 NUMA 节点、
NPU device、服务加载/并发上下文之间的关系。它不是 PD 或 KV 正确性验收。

`scripts/probe_bm_numa.py` 是可单文件复制的临时诊断入口：仅依赖服务器已有的
torch、torch_npu、memfabric_hybrid；不导入 SGLang 或自定义算子，不加载权重。
每个独立进程初始化 world_size=1 的 BM，申请 1 GiB HOST/SDMA 池（pool_id=105），
核对本地实际容量后 destroy/uninitialize。测试在 join 和数据传输之前结束。
`--numa-node=-1` 传 flags=0，其他节点传 `0x80 | node`；直接使用探针参数，
不读取 `SGLANG_NPU_MEMPOOL_LOCAL_NUMA_NODE`，因此可以独立选择 device 与节点。

先停止失败服务及遗留 worker，在原服务容器、原 Python/CANN 环境运行。保持当前
内存状态，不需要再执行 drop_caches。选用空闲 device 0/1 和端口23300/23301；
端口占用时可改 `--store-port` / `--nic-port`。两台机器可分别执行，不需要 P/D 配对。
以下在服务器 SGLang 仓库根目录用 bash 执行（先把新增脚本同步到相应路径）：

```bash
LOCAL_IP=10.120.72.31  # D 机器改为10.120.72.32
REPORT_DIR=$(mktemp -d /tmp/bm-numa.XXXXXX)
for dev in 0 1; do
  for node in -1 0 1; do
    log="$REPORT_DIR/device-$dev-node-$node.log"
    if timeout --kill-after=5s 90s python3 ascend-mempool-test/scripts/probe_bm_numa.py \
      --host-ip "$LOCAL_IP" --device-id "$dev" --numa-node="$node" >"$log" 2>&1; then
      rc=0
    else
      rc=$?
    fi
    printf 'device=%s node=%s exit=%s log=%s\n' "$dev" "$node" "$rc" "$log"
  done
done
rg 'BM_NUMA|Try HalMemCreate|HalMemCreate failed' "$REPORT_DIR"
```

正常结果为 `allocation_ok=true`、`cleanup_errors=[]`、exit=0。重现目标故障须同时
出现 `failed_stage=bm.create2` 与 SDK 的 `HalMemCreate ret:6`，仅非零退出不能说明
NUMA 分配失败。import、set_device、initialize 失败要先处理环境/探针初始化问题。
timeout退出124/137或只有START/ALLOCATED而无RESULT时，保留完整日志，按最后阶段
定位；此时不能当作成功。回传六份日志及每个case退出码，两台机器的结果分别保留。

判读：

| 结果 | 支持的方向 |
| --- | --- |
| device0/1 都是 node0成功、node1返回6 | 跟指定NUMA节点/P2P DDR分配条件相关，弱化device奇偶假设 |
| 两节点都是 device0成功、device1失败 | 跟device上下文相关，弱化只由NUMA节点决定的假设 |
| 只有特定device/node组合失败 | 检查device到NUMA的可达性或驱动组合约束 |
| 默认分配成功，显式指定失败 | 默认路径与显式绑定路径存在差异；默认成功不证明实际落点 |
| 全部成功 | 串行独立路径未复现，继续对照并发/模型初始化上下文，不能宣布服务已修复 |

每次释放后再运行下一组，因此本实验不能验证16池并发容量或服务前置分配压力。
测试成功也不能证明物理落点、远端映射或真实KV读写正确。

Mac可执行 `--help` / `--describe` 检查CLI和flags；真正HAL结果须在NPU上获取。

## 2026-10-02 P 侧实测及后续采样

P的 `/tmp/bm-numa.qDNtCX` 六组结果：device0/1在默认和node0均成功，在node1均
出现HAL6（1GiB页及2MiB页）、create2失败。这已证明当前故障不需要模型/并发/PD
即可复现，且不只发生在奇数device；尚不能断言所有奇数节点永久不支持。
默认路径日志numa:4294967295是-1的无符号表示，不代表实际内存落点。

两份失败日志在RESULT输出后以SIGABRT/134退出。`cleanup_errors=[]`只表示显式
清理未捕获Python异常，不能证明原生析构正常。先查看未经过滤的结尾：

```bash
tail -n 80 /tmp/bm-numa.qDNtCX/device-0-node-1.log
tail -n 80 /tmp/bm-numa.qDNtCX/device-1-node-1.log
```

用户已补齐以上日志：BM uninitialize与RESULT之后，HYBM析构重试同一预留地址，
出现 `HalMemAddressFree return:-8`、`reserved space not found`、析构失败，
最后 `malloc_consolidate(): invalid chunk size`。本地MF的-8对应底层API已卸载；
日志支持失败对象晚于底层API清理的方向，尚未定位堆最初被破坏的位置。
`timeout`的dumped core提示是报告子进程崩溃，此次未达到90秒时限。

本地MF已有初始化失败回滚提交 `2e47225066f0372c62e524008f9c79f41ea1ac8c`，
需对照实际部署的 `c01f3ad842b9ff7412681a44b67141ce7a124c6d` 源码确认是否包含；
不能仅按版本号/构建日期推断。SDK修改后复测应同时确认默认/node0成功、node1如仍
被HAL拒绝则正常返回非零退出码而不abort；退出清理恢复不代表node1分配已恢复。

如果服务器已有GDB，可在相同容器/环境中复现并取得abort栈（约束与上文相同）：

```bash
timeout --kill-after=5s 120s gdb --batch \
  -ex 'set pagination off' \
  -ex run \
  -ex 'thread apply all bt 20' \
  --args python3 ascend-mempool-test/scripts/probe_bm_numa.py \
    --host-ip 10.120.72.31 --device-id 0 --numa-node=1 \
  > /tmp/bm-numa-gdb.log 2>&1
```

保留完整 `/tmp/bm-numa-gdb.log`。GDB退出码不等同于探针退出码；应查看它是否
报告SIGABRT及对应栈。栈可定位检测/析构位置，未必直接指出最早的堆损坏写入。

补齐P的节点能力时，复用上面的脚本，仅将外层改为 `for dev in 1`，内层改为
`for node in 2 3 4 5 6 7`，创建新的REPORT_DIR并保留退出码。D应单独运行原六组
对照，不能用P的结果替代。若P的2/4/6成功、3/5/7失败，可进一步固定实际候选
节点集合，再由驱动日志解释限制原因。

上述六组结果采集时，服务使用原先COUNT的`tp_rank % N`规则。当前代码已取消COUNT，
选择偶数节点用`SGLANG_NPU_MEMPOOL_LOCAL_NUMA_NODE=0,2,4,6`。
单节点探针仍直接使用`--numa-node`，可继续复现奇数节点失败。恢复默认分配路径可
在启动worker前unset该节点列表变量，并核对启动脚本没有重新export；`--even-numa`
测试模式会主动设置0/2/4/6。默认分配成功仍不构成物理落点、所有节点能力或大容量验收。
