# BM 本地 NUMA 分配交叉诊断

用于区分 2026-10-02 TP16 服务中 `HalMemCreate ret:6` 与请求 NUMA 节点、
NPU device、服务加载/并发上下文之间的关系。它不是 PD 或 KV 正确性验收。

`scripts/probe_bm_numa.py` 是可单文件复制的临时诊断入口：仅依赖服务器已有的
torch、torch_npu、memfabric_hybrid；不导入 SGLang 或自定义算子，不加载权重。
每个独立进程初始化 world_size=1 的 BM，申请 1 GiB HOST/SDMA 池（pool_id=105），
核对本地实际容量后 destroy/uninitialize。测试在 join 和数据传输之前结束。
`--numa-node=-1` 传 flags=0，其他节点传 `0x80 | node`；直接使用探针参数，
不读取 `SGLANG_NPU_MEMPOOL_LOCAL_NUMA_NODE_COUNT`，因此可以独立选择 device 与节点。

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
