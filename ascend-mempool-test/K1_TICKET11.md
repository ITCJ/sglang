# K1 ticket11：服务池间异步页块搬运

工程实现以 demo 提交 `746ec2d54c` 为基线。新路径通过配置 `pool_copy=true` 开启；
默认 false 保留既有路径，必须先部署下述 SDK 修复再开启。用户已确认31/32的002验收并关闭ticket11，09已解锁；[确认与边界](../.scratch/k1/verification/k1-ticket11-002/README.md)。

## 行为与阅读顺序

功能源码均为 Ascend/NPU 专用；以下路径前缀为 `python/sglang/srt/`：

1. `hardware_backend/npu/mempool/pool_copy.py`：复用 demo 的任务、结果与单执行线程。
   同一线程设置 NPU/NUMA affinity，提交异步 BM batch 并 wait；控制线程 submit/poll。
   超时从入队开始，沿用 `operation_timeout`；UNKNOWN 锁存，不让迟到成功恢复任务。
2. `hardware_backend/npu/mempool/prefix.py`：tick 构造主 KV/Index K 的 GVA 页块清单，
   库完成后才生成原有 receipt。admit 由段 owner 把本组 alive 主 KV、NUMA0 alive Index K
   复制到所属 prefix 段；fill 由组内分片按完整 prompt 长度分工，从各 prefix 段填本组 alive，
   Index K 默认仅 rank0 填 NUMA0。32 页共 admit64 块、fill288 块。
3. `disaggregation/ascend/mempool_router_protocol.py`：开关、SDK 构建标识、绑核及测试延迟配置；
   纳入 prefix policy 签名，要求参与者一致。
4. `hardware_backend/npu/mempool/runtime.py`：为每个 P 分片装配同一个平台适配层，连接池会话与设备。

通用主线功能源码改动为 0。load_back 仍用原设备索引拷贝/事件；P 写回、D 写入、P→D Index K 交接不变。
执行线程不持有 prefix 状态、不做集合通信。空清单也参与完成汇总。
admit 保持“库完成 → ack → publish → 释放源 → 归还额度”；fill 完成后才进入 prompt ready 汇总。
PENDING 继续 poll；FAILED/UNKNOWN 进入既有 fail-stop，不 ack、不发布、不释放在途区域，
由原有父进程清理 worker。正常释放门后清除执行器结果记录，不引入新的 shutdown 协议。

脚本/测试/其他文件单列：

- `scripts/run_pool_copy_acceptance.py`：复用既有两轮服务验收，生成新路径配置并核对搬运日志。
- `tests/unit/test_prefix_pool_copy.py`：公共 PrefixStore 接口的延迟、完整长度分工、块数和 UNKNOWN 验证。
- `tests/unit/test_pool_copy_acceptance.py`：验收报告必须拒绝缺失的延迟注入。
- `patches/memfabric-1.1.4-batch-error.patch`：SDK 单行错误传播修复，应用在独立 SDK 源码中。
- 本说明及 `.scratch/k1/verification/ticket11-service-cpu/`：交付与检查证据。

## SDK 前提：修复 batch 子提交错误传播

用户已确认两侧 `memfabric_hybrid=1.1.4`、`memfabric_zbal=1.1.2`。
核对的是本地 GitCode 仓库 tag `v1.1.4`，提交 `c01f3ad842b9ff7412681a44b67141ce7a124c6d`。
其中 `HostDataOpSDMA::BatchCopyG2G` 的子提交错误存在 `ret`，却最终只返回 `InnerWait`；
异步提交可能因此错误报告成功。补丁先返回非零 `ret`，成功时才走原 `InnerWait`。
`partial_succeed` 大块路径使用其它线程的流，不能用当前线程 wait 替代覆盖，故不改用该 API。

agent 未修改外部 SDK 仓库、安装包或远端主机。由部署方在独立 Linux SDK checkout 中构建；
以下从已有 GitCode 仓库创建独立 worktree，路径按实际位置替换：

```bash
git -C /path/to/memfabric_hybrid worktree add --detach /path/to/memfabric-ticket11 v1.1.4
cd /path/to/memfabric-ticket11
git apply --check /home/cryang/sglang/ascend-mempool-test/patches/memfabric-1.1.4-batch-error.patch
git apply /home/cryang/sglang/ascend-mempool-test/patches/memfabric-1.1.4-batch-error.patch
git submodule update --init --recursive
bash script/build_and_pack_run.sh --build_mode RELEASE --build_python ON --xpu_type NPU --build_test OFF --build_hcom OFF
```

构建命令来自该 tag 的 `doc/installation.md`。将 `output/` 生成的同一 run 包部署两侧，
并按实际安装位置 source `memfabric_hybrid/set_env.sh`；确认服务 Python 加载该构建配套的 wheel/库。
保留构建日志、产物校验和、安装位置和 `version.info`，不要用在线原版 1.1.4 wheel 覆盖修复。
zbal 保持当前 1.1.2。`--sdk-build-id` 是双方一致的审计标签，例如 `mf114-c01f3ad-ticket11-batchfix`，
不是运行时能力探测，也不能单独证明加载了修复库。

本机仅对该函数原文提取体运行 C++ 桩验证：首/末子提交失败及全部成功，原版失败、补丁版通过。
尚未完整编译 SDK 或执行硬件故障注入；该证据不证明部署二进制与源码一致。

## 31/32 验收命令

两侧使用同一工程提交、修复 SDK 构建和原 ticket12-004 配置，保留已验证的驱动/CANN/模型环境。
配置路径按实际位置替换。P 先启动，随后尽快启动 D；两侧相同 run-id，每次重测换新 run-id。
脚本自动生成旁路配置，不修改输入 JSON；设 `pool_copy=true` 与每个非空任务 50 ms 的提交前延迟。

31 / P（仓库根目录）：

```bash
python3 ascend-mempool-test/scripts/run_pool_copy_acceptance.py --role prefill --config /home/cryang/mempool-min-ticket01-3132.json --run-id k1-ticket11-002 --sdk-build-id mf114-c01f3ad-ticket11-batchfix
```

32 / D（仓库根目录）：

```bash
python3 ascend-mempool-test/scripts/run_pool_copy_acceptance.py --role decode --config /home/cryang/mempool-min-ticket01-3132.json --run-id k1-ticket11-002 --sdk-build-id mf114-c01f3ad-ticket11-batchfix
```

沿用 `prefix-sharing-acceptance` 的两轮六用例、就绪检查、请求/报告、故障用例、父进程回收和端口重绑。
不重跑 08/12，不另做八组 demo。报告目录：
`/home/cryang/log/mempool-min/k1-ticket11-002/{prefill,decode}/`。
回传双方 `pool-copy-acceptance.json` 及完整目录，包含 target/restart 日志、快照和既有验收报告。
脚本结束打印报告。两侧退出 0、基础验收通过且 P 搬运报告通过才算本轮通过；D 单侧通过不能代替 P 检查。

P 搬运检查覆盖：admit/fill 都实际执行；每操作 16 分片（含空清单）；清单操作与 Main 历史一致；
admit 2N、fill 9N 及对应字节数；每分片 submit < 库完成 < SR receipt；
非空任务实际延迟至少 50 ms 且 tick 至少 poll 两次。基础报告继续验证 ack/publish/free 顺序与 B/C 答案。
缺失日志、字节/页数/顺序不符或 SDK 失败均非零退出。

## 证据边界

- 服务（含验收服务）不新增目标读回：内容证据复用已通过 demo 的独立设备读回和服务 B/C 答案、
  字节计数、既有 V2；不宣称生产所有页均独立 DRAM 逐字节比对。
- 50 ms 注入验证 PENDING 时 tick 可推进、receipt 不提前；它是提交前延迟，
  不是硬件 DMA 在途故障注入。库真实 wait 的 GIL 证据来自此前 demo，不能把延迟 poll 当作同等证据。
- 服务记录实际 batch submit/wait 两段时间及 poll 最大间隔，供实机分析；
  延迟测试不作为性能结果，也不自动证明忙/闲 tick 间隔相同。
- SDK 子失败传播、真实 wait 超时/在途 fail-stop 的硬件证据仍待验证；CPU UNKNOWN 测试和已有服务故障测试不能替代。
- 正常使用配置 `pool_copy_delay_seconds=0`；用户已接受002服务验收并关闭11，以上未验证项仍保留。

CPU 与双轴复核详见 [检查记录](../.scratch/k1/verification/ticket11-service-cpu/README.md)。

## 001失败后的002重测

用户已确认两侧Docker装过SDK补丁。001在执行线程setup遇到OSError22，HTTP500为fail-stop的外层结果。
本次Python修复移除“目标NUMA CPU与继承线程mask求交”；Linux自行按容器cpuset限制绑定。
不需重编SDK，两侧更新工程代码后使用上方002命令。若再失败，回传`pool copy executor setup failed`
后的原始traceback和`pool_copy_complete`，先分析，不自动再启动第三轮。
[本地回归与证据边界](../.scratch/k1/verification/ticket11-affinity-fix/README.md)。
