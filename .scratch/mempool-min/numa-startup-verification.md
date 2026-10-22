# 连续 NUMA 分配与全节点启动验证

日期：2026-10-23。分支：`cryang/dev/mempool-min`，开发基点 `d328af12bd`。

## 用户要求与实现

- 默认节点列表由轮转改为本机 rank 升序连续均分。规划器和实际 manager 共用分配函数；
  余数给前面的节点，显式映射保留。单机 TP16 配 8 个节点时，每节点两个相邻 rank。
- 新增 `run_bm_startup_gate.sh --all-numa`：P `10.120.72.23`、D `10.120.72.25`，
  NUMA 0–7，每节点 96 GiB。每 rank 本地贡献 48 GiB，每机合计 768 GiB。
- 仍是 16 对独立池，`SGLANG_NPU_MEMPOOL_SHARE=0`。这次没有开启生产共享组启动。
- 固定逻辑布局：78 层、16 个存储槽、每槽 35840 tokens、dim576 BF16；
  payload 加 64-byte probe 向上按 GiB 对齐，得到每 rank 48 GiB。
- `check_bm_numa.py` 检查配置、HAL 节点和容量、两侧贡献、peer probe、全部本地池
  共同 ready 及实际进程退出。全节点结果写 `numa-summary.json`，按节点报告成功容量。
  `--even-numa` 与原检查器入口继续可用。
- 双机运行命令、前置条件与回传材料见
  [BM_NUMA_DIAGNOSTIC.md](../../ascend-mempool-test/BM_NUMA_DIAGNOSTIC.md)。
  日志放 `/home/cryang/log/`，例子使用 1800 秒 timeout。

## Agent 实际执行的检查

Mac CPU 自运行 gate：

```bash
PYTHONPATH=/tmp/mempool-numa-test-deps-20261023 \
/private/tmp/sglang-merge-venv/bin/python \
  ascend-mempool-test/scripts/verify_numa_plan.py \
  --self-test --output-dir /tmp/sglang-numa-all-96g-20261023
```

结果：**51 项通过，0 failures、0 errors**。报告包含 `npu_tested=false`。
覆盖连续分组、余数、多主机、模拟 SDK flags、既有偶数节点配置回归，以及新增全节点
配置的双侧 dry-run、48 GiB 布局计算、报告 CLI、错误节点/容量和旧配置误用的拒绝。

Ruff lint、8 个相关 Python 文件的格式检查、`bash -n`、`git diff --check` 通过。
原 CPU 环境缺少 torch/msgspec，首次检查在导入时失败；临时依赖安装在独立的
`/tmp/mempool-numa-test-deps-20261023` 后完成上述检查，没有修改项目依赖声明。

## 待用户硬件验证

- 本次大容量建池、映射、probe、释放尚未在 NPU 上执行。
- 用户先前反馈 HCCS/Fabric 地址覆盖已达全部“2T”；这是用户机器反馈，尚未收到
  原始地址报告，不代替本次 768 GiB/host 的分配、映射与读取验收。
- 两侧正常退出并同时打印 `ALL_NUMA_CHECKS_PASSED` 与
  `ALL_BM_STARTUP_CHECKS_PASSED` 才通过；回传各自 `numa-summary.json`、`exits.tsv`
  及终端汇总，失败时加相关逐卡完整日志。
- 本入口只验证分配/映射/探针和正常释放；不填满整个 KV，不验证 Graph、真实模型
  或吞吐性能。既有共享实现 ticket 的未完成项及硬件验收状态不因此改变。

## Comments

2026-10-23：按用户最新部署及容量要求交付脚本、检查器、CPU 检查和双机命令。
用户已要求完成后推送 `cryang/dev/mempool-min`。硬件结果等待回传。

### 追加：两个本侧rank共享96 GiB（2026-10-23）

用户进一步要求两个TP rank共享一份96 GiB，一个BM组连接P两个与D两个NPU。
新增 `run_bm_shared_startup.sh` / `.py`，直接复用组件 `SharingPool` 和四方
`Cohort` 的建池、probe及退出协议。生产 manager 的共享启动限制未修改。

- 默认NUMA 0–7，node n绑定两侧各自的device `2*n` / `2*n+1`，八组同时分配。
  每组贡献 `[96 GiB, 0, 96 GiB, 0]`；每侧只有owner分配，follower无重复物理分配。
  每机总量768 GiB。支持 `--numa-nodes 0` 单组及任意0–7子集。
- 固定布局78层、16 slots、71680 tokens、BF16 dim576，加256-byte probe向上
  对齐到96 GiB。只做四方身份probe，不stage整个KV、不分配同等容量HBM tensor。
- 两侧检查run-id、代码指纹和配置；全部worker写完并核对probe后，父进程执行
  双机共同持池屏障，随后才释放worker进入Cohort drain/close。
- 复用“所有参与者drain → 所有池关闭 → 非store客户端退出 → store退出”。
  父进程自动启动、检查、收集报告并清理自己创建的进程；失败返回非零。
- `summary.json`保留实际贡献、owner/follower信息、逐节点容量和NUMA证据。
  driver mapping不可观测时仍明确标记physical placement未验证。P自动收集D汇总。
  命令、端口和日志位置见 `BM_NUMA_DIAGNOSTIC.md` 的共享版一节。

Agent在Mac实际执行：

```bash
PYTHONPATH=/tmp/mempool-numa-test-deps-20261023 \
/private/tmp/sglang-merge-venv/bin/python \
  ascend-mempool-test/scripts/verify_numa_plan.py \
  --self-test --output-dir /tmp/sglang-numa-shared-96g-20261023-loopback
```

**72项通过，0 failures、0 errors**。包括原51项、新共享容量与SDK参数、无重叠端口、
单组设备选择、拒绝独立池/错误容量/不完整释放报告、持池屏障、只跑probe的worker路径，
以及既有NUMA证据和真实本地socket的多人退出顺序测试。
首次沙箱内运行因不允许监听127.0.0.1，退出协议测试报PermissionError；获准在沙箱外
运行本地测试后全通过，未访问NPU或远端服务。`--describe`确认每owner96 GiB及每机
768 GiB；Ruff lint/format、shell语法和diff检查通过。

硬件状态：**等待用户NPU测试**。两机退出0并打印
`ALL_SHARED_BM_STARTUP_CHECKS_PASSED`才算该启动smoke通过，不能替代Graph、模型、
吞吐或生产共享服务验收。本次不关闭任何未验收ticket。
