# UNIDEX / SysV registered Host 本地基准

本目录当时有意选用上游 SysV registered Host，但因尚未发现外部 BM 映射接线，准备范围仅覆盖本地 L2→L1。远端 BM Host GVA 经 `gva_to_va(..., LOCAL_DEVICE)` 交给现有 UNIDEX `src_ptr` 的 [源码接线](https://github.com/hibikid/ascend-ub-bench/blob/f934478756ab5be92cfe409a3f6bc3baaf4b207f/remote_dram_sparse_copy_bench.py#L764-L785) 已接入本仓库的 [MemFabric 双端基准](../fabric_direct_bench/README.md#unidex-bm-映射补充实验)，而非本目录脚本。两种本地 Host 来源分别标记，不混同；远端适配尚未在目标 A3 实测。

## 已准备的本地范围

已准备的实现是单机 A3 的 UNIDEX + SysV registered Host L2→L1，沿用 `kv_path_bench/kv_transfer_bench.py` 的完整请求、逻辑 KV、page 映射和采样方法；原 ADXL/FAST2D 默认入口保留。下述 UNIDEX 单机命令不连接 Store、不需要 IP，不提供 UNIDEX L3 模式，也未实现 SysV FAST2D。该本地入口尚不使用已发现的远端映射接线。

61 层 BF16；128-token page；K=512、RoPE=64 分离。Host 为 `[page,61,128,1,D]`，NPU 为 `[61,page,128,1,D]`。slot 0 保留，scattered 有效 slot 间隔完整空 page。每 token 一行，逐层提交 K/RoPE；Host 零拷贝视图按完整物理 page 切片，K/RoPE 使用同一边界，各视图严格小于 4 GiB；CPU view 与 device alias 偏移一致。上游 size 传递没有 int 截断，因此每个组件仍分配一个完整 Host block，JSON 记录 allocation 与 views。128K scattered 的 L1 和 L2 各约 17.16 GiB，另需索引、运行时余量和注册资源；容量/注册失败即停止，不改请求或自动缩小批次。

`block_dim=24`，可用 `--block-dim` 覆盖。索引/mask 固定映射、范围/覆盖/唯一目标检查及上传同步在计时外，JSON 记录 `index_prepare_s/index_bytes/launch_count`。采样复用这些索引，不包含动态请求索引准备。`measure_batch` 计入全部 launch 和一次请求完成同步，目标清零及其同步在计时外。默认无数据校验，`correct=null`；完成同步/异常检查仍执行。`--validate` 在计时外逐字节检查 KV 和未用 Host/NPU page guards。

## L3→L1 对照设计边界

同事的 [远端正确性入口](https://github.com/hibikid/ascend-ub-bench/blob/f934478756ab5be92cfe409a3f6bc3baaf4b207f/remote_dram_unidex_copy_test.py#L260-L313) 及 [对照 benchmark](https://github.com/hibikid/ascend-ub-bench/blob/f934478756ab5be92cfe409a3f6bc3baaf4b207f/remote_dram_sparse_copy_bench.py#L745-L899) 证明已有 MemFabric 映射与 UNIDEX `src_ptr` 的源码接线，可作远端环境和数据路径的正确性参考。本仓库 `fabric_direct_bench` 已增加 BM local/remote UNIDEX 两条可选路径，以相同 BM 远端源、相同最终 L1 对照 BM GH2L；保留 61 层、分离 K/RoPE、page 映射、五档规模和完整请求。源静态准备及映射在计时外，索引准备耗时单列；每个样本计入全部 launch 和完成同步。目标 A3 功能与性能均待验证。

同事 benchmark 使用单层 `(16,32768,1,576)` 的随机 top-k、对多次调用只做一次末尾同步并报告平均值；这些数字不能直接与本仓库逐样本同步的整请求 median/p95 比较。本目录的本地 L2 脚本或旧 BM 结果也不代表新增 UNIDEX 远端性能。

## 前置条件与完整命令

**下文仅是本地 SysV L2 的独立命令，不用于新增双端实验；双端完整交接见 [MemFabric README](../fabric_direct_bench/README.md#unidex-bm-映射补充实验)。** 如后续安排本地 SysV 对照，先按主 Agent 提供的实际交付 commit 运行 `git pull --ff-only`、`git log -1 --oneline` 并核对 HEAD；当前没有新增远端功能或性能验证结果。

目标为 A3/910C、aarch64；停止模型及占用目标 NPU 的服务后执行，每进程只使用一设备。已记录环境为 Ubuntu 22.04、CANN 9.0.0、driver 26.1.1；实际 Python/torch/torch_npu、镜像 digest 必须采集，未知写 `unknown`。准备目标 CANN/驱动开发文件、CMake、C++ 编译器、make、Python3/pip3、torch/torch_npu（与目标 CANN/ABI 匹配）、setuptools、pybind11、wheel==0.45.1。不可假设准备机 wheel 适配目标架构和 Python/torch ABI。

### 联网准备机：固定源码与离线依赖

```sh
cd <SGLANG_REPO>
bash workspace/unidex_copy_bench/prepare_source.sh <NEW_SOURCE_PACKAGE_DIR>
```

脚本 clone [官方固定源码](https://github.com/sgl-project/sgl-kernel-npu/tree/d9261669b0303a28369d07c0eea0bd1627235dd6)，固定 commit `d9261669b0303a28369d07c0eea0bd1627235dd6` 并递归获取 gitlink 指定子模块。输出 `source.tar.gz`（排除所有 `.git`）、`SHA256SUMS`、源码 commit/submodule/file-SHA256 manifest 和 prepare.log。子模块来源由该 commit 的 `.gitmodules` 决定，需要准备机能连接这些源。只传源码包、SHA256SUMS 和 prepare.log，不传 checkout 中的 Git config。

离线依赖由用户依据目标版本清单准备匹配的 aarch64/Python wheelhouse，包含 wheel==0.45.1、setuptools、pybind11 及尚缺的目标 torch/torch_npu 依赖。在目标环境显式用 `python3 -m pip install --no-index --find-links <WHEELHOUSE> <EXPLICIT_PACKAGES>` 安装；本安装脚本不会自动联网或安装缺失依赖。不要用准备机默认 `pip download` 的平台选择代替目标适配核验。

### 目标客户端：解包、安装

`<TRANSFER_DIR>` 存放源码包及 SHA256SUMS，`<SOURCE_DIR>` 必须是新目录，`<INSTALL_LOG_DIR>` 也必须尚不存在。

```sh
cd <TRANSFER_DIR>
sha256sum --check SHA256SUMS
mkdir <SOURCE_DIR>
tar -xzf source.tar.gz -C <SOURCE_DIR>
cd <SGLANG_REPO>
bash workspace/unidex_copy_bench/install.sh <SOURCE_DIR> <INSTALL_LOG_DIR> <IMAGE_DIGEST>
```

安装先记录环境，再核验源码 manifest/版本、CANN acl/driver HAL headers、AscendC CMake 入口及离线依赖，显式设置 `SOC_VERSION/ASCEND_SOC_VERSION=Ascend910_9382`。`ASCEND_HOME_PATH` 可明确指定目标 CANN 根目录，否则检查标准 latest 目录；缺 headers/tools 在构建前失败，不自动下载。安装开始固定当前 `python3` 的 `sys.executable`；在已验证源码上仅调整 build.sh 的 Python/pip 调用、CMake Python 选择和 commit 读取，确保均使用同一解释器及离线 manifest commit。原入口备份、before/after hashes、`build-entry.diff/build-adaptation.json` 留在安装日志目录；native 算子源码不改。必须在同一 Python 环境运行随后校验/性能，environment.json 会记录实际解释器路径。

随后仍执行 `bash build.sh -a kernels Ascend910_9382`；A3-only ops 默认开启（包含 UNIDEX/shm），CATLASS 关闭。只安装本次生成、源码版本匹配的 sgl_kernel_npu wheel，使用 `--no-index --no-deps`；不构建/安装其余模块。固定上游选中 kernels 路径没有 git submodule/git describe/FetchContent/下载调用；原 CMake git rev-parse 失败仅作提示，版本头也提供 empty fallback，但离线适配防止误读父仓库 HEAD。唯一可见的 pip 自动安装是缺 wheel 时的分支，本入口前置检查并禁用索引。目标 CANN/torch_npu 内部工具行为仍需远端确认。必须使用新解包源码，避免上游 build.sh 清除已有 build/；适配后不再把该工作目录视为未经修改的上游源码。所有构建和目标库 import 仅发生在远端。

### 先做独立正确性校验

```sh
cd <SGLANG_REPO>
bash workspace/unidex_copy_bench/correctness.sh <NEW_CHECK_LOG_DIR> <SOURCE_DIR> <DEVICE_ID> <IMAGE_DIGEST>
```

环境采集只读取包 metadata、版本文件、Git/manifest、源文件 hash 和 IPC namespace，不 import torch/torch_npu、不分配/访问 NPU、不做数据测试。此独立入口才显式 `--validate`，先 128-token contiguous，再 4K scattered，warmup=0/repeats=1；每项 1800 秒超时，失败停止。它调用同一 bench，保留两份原始 JSON/测试日志、环境清单和 correctness-status.json。先通过此入口，再运行性能入口。

### 随后性能与收集

```sh
cd <SGLANG_REPO>
python3 workspace/kv_path_bench/performance_suite.py --copy-engine unidex --l2-only --device <DEVICE_ID> --block-dim 24 --kernel-source-dir <SOURCE_DIR> --image-digest <IMAGE_DIGEST> --warmup 2 --repeats 10 --timeout 1800
bash workspace/unidex_copy_bench/collect.sh <NEW_ARCHIVE.tar.gz> <INSTALL_LOG_DIR> <CHECK_LOG_DIR> <PERFORMANCE_RUN_DIR>
```

runner 打印 `RUN_DIR` 和各项 `CHILD_PID/COMMAND`，默认位于 `workspace/unidex_copy_bench/results/`，依次运行默认无校验的 128-token smoke、1K/4K/16K/64K/128K × contiguous/scattered，任何超时/失败停止。保存原始 JSON（含 samples、实际命令/进程 PID）、summary JSON/CSV 和日志，路径标签固定 `L2-L1_unidex_sysv_registered`，JSON/CSV 保留 `engine/host_memory/block_dim/validation_enabled`。summary 保留每项命令/退出码，超时/中断仍保留末尾 native 输出。collect.sh 原样打包安装 log/status、环境/两仓库版本/源 hash、构建入口 diff、校验/性能实际命令、原始 samples/校验开关和所有原始 JSON/CSV/log。单项调试可直接调用 bench 同样参数及 `--tokens/--layout/--output/--log`。请求 UNIDEX L3 模式（缺少 `--l2-only`）直接参数报错。

已有三套结果均存在时，可用 `python3 workspace/collect_latest_bench_results.py --include-unidex` 生成加上独立 UNIDEX 标签的截图汇总；默认收集范围仍为原三套。没有三套旧结果时，使用本 runner 自己的 summary.csv/JSON 和 collect.sh 即可。

## 成功标志

安装日志 `INSTALL_OK`；独立校验日志 `CORRECTNESS_OK`，两个 JSON 为 status=ok、validation_enabled=true、correct=true；性能日志 `ALL_OK`、summary status=ok。性能默认 correct=null，不代表逐字节验证通过。环境包含两仓库版本、目标包版本、CANN/driver、架构和用户传入的 digest，无法取得的字段明确 unknown。源码版本清单不代表注册或数据路径已在目标通过。

## 失败与回传

对失败阶段对应的 `<FAILED_LOG>` 只需运行一条：

```sh
tail -n 80 <FAILED_LOG>
```

回传退出码、stage/error、该日志末尾 80 行，以及 collect.sh 包含的安装日志/环境、校验 JSON/log、性能所有原始 JSON/CSV/log（包括失败 run）。可捕获的 Python/接口/清理错误输出失败 JSON 并非零退出；安装及独立校验有 EXIT status JSON。bench 在目标 import/分配前先写 running JSON，SIGTERM 会进入异常清理，但 SIGKILL/native crash/磁盘写失败不能保证最终单项失败 JSON，应以 runner/check status 和日志确认失败，不把残留 running JSON 当通过。同步失败时不调用 free_shm，保留 owner/alias/index 引用直到进程退出；这不证明驱动已恢复，应先确认该进程及 NPU 状态。

## 清理动作

正常结束先完成同步再 free_shm；上游成功 allocation 先 IPC_RMID 再 memset 全部 Host 字节为零，最后 attachment 消失后 OS 回收。free_shm 底层忽略 halHostUnregister/shmdt 返回码，因此 JSON 标记 `host_unregister_confirmed=null` 和“call returned; unregister/detach not confirmed”，调用返回不等于确认全部注销。它处理本进程所有注册对象，脚本须独立进程运行，不混入模型服务。UUID name 只是 allocator map key，SysV 使用 IPC_PRIVATE，上游不暴露 shmid，不能按 UUID 删除 OS 段。

超时 runner 仅结束自己启动的子进程组。手动中断后，在同一容器/IPC namespace 中依据日志 CHILD_PID 或 JSON process_pid 核对命令与启动时间；只有确认属于本次且仍存活，才对该单一 PID 发 TERM：

```sh
ps -p <BENCH_PID> -o pid,ppid,lstart,args
kill -TERM <VERIFIED_BENCH_PID>
```

已完成 IPC_RMID 的段无需人工 ipcrm。若怀疑进程恰在 shmget/shmat 到 IPC_RMID 的窗口被强杀，先只读列出该 PID 创建的段并保存 shmid，再核对详细信息：

```sh
awk 'NR == 1 || $5 == <BENCH_PID> {print}' /proc/sysvipc/shm
ipcs -m -i <RECORDED_SHMID>
```

仅在同一 IPC namespace、该进程已退出、shmid 不变、cpid/uid 属于本次且 ctime 落在本次开始/结束时间（JSON process_started_wall_s 与日志）内、nattch=0 时，才可对该明确核验的单段执行 `ipcrm -m <VERIFIED_SHMID>`；无法核验就保留并回传信息，不只凭 PID/同一用户批量删除。保留用户日志/结果/源码包，collect.sh 不删除输入；确认已回传后由用户自行决定清理归档和本次独立源码目录。本目录的 UNIDEX 单机步骤无需 Store 端操作；MemFabric 双端步骤按其独立交接清理。
