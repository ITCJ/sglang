# Ticket02-1：NUMA 分配与共享计划

本入口验证配置和分组计划，只运行 CPU 代码，不分配 BM 内存、不启动服务。
`contribution_bytes` 是计划值；`placement_verified: false` 表示未观测物理 NUMA 页落点。
真实组启动、映射和 P/D 计划交换属于02-2。

## 一条命令检查

在完整仓库 checkout 中，使用已安装 CPU PyTorch、NumPy、msgspec 的 Python 环境：

```bash
python3 ascend-mempool-test/scripts/verify_numa_plan.py --self-test
```

入口自动设置源码路径，隔离已有 mempool 环境变量，运行配置、分组、旧启动入口和
偶数节点报告检查的回归测试，并保存1/2/4成员的计划示例。默认输出到
`/home/cryang/log/numa-plan/<时间戳>-<PID>/`，包含 `tests.log` 和 `report.json`。
失败返回非零；报告必须为 `status: passed`，且测试 `failures` / `errors` 都为0。
这是 CPU 配置 gate，不能替代 NPU smoke 或完整服务验收。

Mac 上可指定可写的新目录，例如：

```bash
/tmp/sglang-staging-venv/bin/python ascend-mempool-test/scripts/verify_numa_plan.py \
  --self-test --output-dir /tmp/numa-plan-check-01
```

该虚拟环境路径仅是本次开发环境示例。输出目录必须尚不存在，避免覆盖已有证据。
入口没有子服务或设备资源需要清理。

## 预览部署分组

先从部署拓扑确认每个全局 TP rank 所在的物理主机及该主机的在线 NUMA 节点。
`host` 必须是可跨 P/D 比较的物理主机身份，不能把同机不同容器名称当作不同主机。
本切片消费明确提供的拓扑，不自动探测远端主机。

例如 `topology.json`：

```json
[
  {"host": "host-p", "ranks": [0, 1, 2, 3], "online_nodes": "0,2"}
]
```

`rank-map.json` 必须覆盖本侧全部 ranks，节点可重复：

```json
[
  {"host": "host-p", "rank": 0, "node": 0},
  {"host": "host-p", "rank": 1, "node": 0},
  {"host": "host-p", "rank": 2, "node": 2},
  {"host": "host-p", "rank": 3, "node": 2}
]
```

显式映射和旧节点列表冲突，因此使用映射时需要清除旧变量：

```bash
env -u SGLANG_NPU_MEMPOOL_LOCAL_NUMA_NODE SGLANG_NPU_MEMPOOL_SHARE=1 \
  python3 ascend-mempool-test/scripts/verify_numa_plan.py \
  --topology topology.json --mempool-rank-numa-map rank-map.json \
  --tp-size 4 --role prefill \
  --instance-id deployment-01 --kv-identity glm52-bf16-w4a8 \
  --layers 78 --prefill-capacity 4096 --decode-capacity 4096
```

也可使用旧列表，不传 `--mempool-rank-numa-map`：

```bash
SGLANG_NPU_MEMPOOL_SHARE=1 SGLANG_NPU_MEMPOOL_LOCAL_NUMA_NODE=0,2 \
  python3 ascend-mempool-test/scripts/verify_numa_plan.py \
  --topology topology.json --tp-size 4 --role prefill
```

2026-10-22起，节点列表按每台主机的rank升序连续均分，替代原轮转规则。
以上得到分配 `0,0,2,2` 和 `{0,1}`、`{2,3}` 两个共享组。
TP16配8个节点时每个节点两个ranks，配4个节点时每个节点四个ranks。
不能整除时，列表靠前的节点多分一个rank；节点多于ranks时只使用前若干节点。
每台主机独立均分，使用该主机按全局rank排序后的本机序号，不按全局rank取模。
改为 `SGLANG_NPU_MEMPOOL_SHARE=0` 时节点分配相同，计划变为每 rank 独立池。
开关只接受字面值 `0` / `1`，未设置等于 `0`。
旧列表无效时，独立模式保留告警后使用 `flags=0` 的行为；共享模式明确失败。
显式映射无论开关值如何，都拒绝重复/缺失 rank、非法节点、未知主机及冲突配置。
显式映射不被自动均分覆盖；例如仍可明确配置非相邻组 `{0,2}`、`{1,3}`。

## 计划合同与接入边界

- 同一物理主机、同一 NUMA 节点的 ranks 在共享模式组成一组，成员按全局 TP rank
  排序，首名是本侧 owner。组大小可以是1、2、4或其他值，不固定为8组。
- 一个 g 成员组规划 `2g` 个 BM participants：P 为 `0..g-1`，D 为 `g..2g-1`。
  每侧 owner 贡献完整逻辑池，其他成员贡献0；KV 容量保持原值。
- 每个成员在 payload 之后拥有64字节 probe 区间，额外空间纳入对齐后的贡献与 stride。
  这里只计算偏移，尚未写 probe 或取得设备映射。
- store 端口为 `base_port + owner_tp_rank`。每组为各成员保留互不重叠的 `2g`
  NIC 端口范围；实际监听端口包含 participant 偏移。独立模式保留原端口公式。
  规划拒绝端口溢出、store/NIC 范围重叠；`validate_peer()` 额外拒绝同机跨侧 NIC 冲突。
- `validate_peer()` 校验相反的 P/D 角色及相同的 instance、KV identity、共享开关、
  成员集合、layout、pool ID、store 基址。两侧可用不同 host、NUMA 编号和 NIC 基址。
  预览 CLI 只输出本侧计划，不做远端计划交换。
- pool identity 包含 instance、KV identity、成员、完整 layout、pool ID 和 writer 规则。
  P/D 的物理 host、节点编号不进入逻辑身份。`--instance-id` 和 `--kv-identity`
  当前由调用者提供；服务自动获取及交换实际模型/量化身份留到02-2。

`report.json` 的每 rank 记录包含节点、group、owner、participant、贡献、stride、probe
偏移、端口和 pool identity，全部属于规划信息。预览过程中没有 pool 所有权、设备
同步或释放操作；这些资源生命周期继续由后续生产 manager 实现。

本次已注册服务参数 `--mempool-rank-numa-map` 和环境开关，但正式 mempool 服务
遇到 `share=1` 或显式映射会在 BM 初始化前报 `requires group startup (ticket02-2)`。
这样可以避免用户要求的分组被现有 rank-pair 分配器静默忽略。默认 `share=0`、
不传显式映射的旧服务启动路径继续可用。
