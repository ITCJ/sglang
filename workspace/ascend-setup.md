# A3 环境部署记录

更新：2026-09-08。本文记录本次双节点 A3 环境实际走通的步骤、失败经验及尚未完成的验证。远端结果由操作者手工回报；本地代码工作区不是目标机器。

## 当前结论

目标是两节点 Ascend 910C（A3）超节点上的 Dense 三层 KV 实验，计划见 [实验方案](../DENSE_THREE_TIER_KV_EXPERIMENT_PLAN.md)。远端访问外网困难，两台机器分别手动操作。

**两台已通过 Fabric 模式下的单节点测试和双节点远端 Store 数据校验。SGLang 在 TP16+DP1 下已启动并通过操作者的聊天请求，但尚未完成 HiCache 集成、TP16+DPA16 内存验证及正式实验。** 物理 HCCS 路径尚未独立确认。

| 项目 | 当前掌握的信息 |
| --- | --- |
| 容器系统 | Ubuntu 22.04，aarch64（Ubuntu 包架构名为 arm64） |
| SGLang 基础环境 | 使用原始 Ascend SGLang 镜像重新创建容器；版本系列 v0.5.16 / CANN 9.0.0 / A3 |
| 镜像身份 | 实际 digest 尚未记录；不能只凭标签证明两台镜像完全相同 |
| CANN | 当前容器报告 9.0.0 |
| 宿主机驱动 | 原为 25.5.0；平台升级后，两节点报告 26.1.1，容器内驱动文件也已更新 |
| Mooncake | 安装脚本固定 `mooncake-transfer-engine-npu==0.3.12.post1`，安装及导入检查通过 |
| 本地测试 | 两台均回报 `F1:LOCAL_OK`，并确认原生日志含 `Fabric mem mode is enabled` |
| 普通 Verbs/RoCE | 宿主机能枚举两个 HCA，link_layer 为 Ethernet；容器中仍为 `0 HCAs found`，原因未查明 |

## 传输路线与边界

RoCE 是基于 Ethernet 的 RDMA，`Ethernet` 不表示低带宽。Rocky 是操作系统名，与 RoCE 不同。

本次优先验证 Ascend Direct / Fabric Memory，是为了利用 A3 超节点能力访问远端 Host 内存，并不是因为容器枚举不到 RoCE 就能断言 Fabric 可用。两条路径必须各自验证。

Mooncake 当前官方 Ascend Direct 文档说明，A3 Fabric Memory 要求 CANN 9.0+、HDK 26.0+；超节点内的该传输路径默认使用 HCCS。实际安装版本的支持情况仍以初始化、分配日志和测试为准。

本地脚本在自己的子进程中设置：

```text
protocol = ascend
ASCEND_ENABLE_USE_FABRIC_MEM = 1
HCCL_INTRA_ROCE_ENABLE = 0
ASCEND_GLOBAL_RESOURCE_CONFIG = {"fabric_memory.max_capacity":4}
```

这些设置不会永久写入交互式 shell，也不会自动修改原实验的 JSON。设置 NPU device 后才初始化 Store。

单节点 put/get 可能走本地复制路径。`Fabric mem mode is enabled` 加上成功初始化和数据校验，证明当前配置下的本地功能通过；不能证明实际经过跨节点 HCCS，更不代表测得了 Fabric 带宽。

## 每个节点如何部署

### 1. 宿主机准备

平台负责 Ascend 驱动/固件及超节点网络。不要用容器内 pip 或 apt 升级宿主机驱动。

在宿主机核对：

```bash
npu-smi info
grep '^Version=' /usr/local/Ascend/driver/version.info
```

准备仓库和本地已有的同一基础镜像。在仓库目录更新代码，然后创建新容器：

```bash
git pull --ff-only
CONTAINER_NAME="<自选新名字>" IMAGE="<本地原始SGLang镜像名或ID>" \
bash workspace/docker_run_sglang_ascend.sh "<代码父目录>" "<模型目录>"
```

第一个参数是包含 `sglang` 目录的父目录。第二个参数须为存在的目录；只测试 Store 时不需要模型，可以与第一个参数相同。实际名字、IP、路径通过参数填写，不写进公开仓库。

这是 `docker run` 启动脚本，不是 Dockerfile 构建脚本。默认镜像为 `quay.io/ascend/sglang:cann9.0.0-a3-v0.5.16`，默认容器名为 `sglang_ascend`。早期脚本使用过不同顺序的镜像标签；受限网络环境应显式指定已经存在的镜像，避免意外下载。使用新容器名保留旧容器。

脚本使用 host 网络、privileged、无限 memlock，并挂载设备、Ascend driver、代码和模型；存在时挂载 `/etc/hccn.conf`。因此容器内 `/usr/local/Ascend/driver/version.info` 来自宿主机。

现在不挂载宿主机 RDMA 用户态库及 provider 配置，改在容器内通过 Ubuntu 包管理器安装。脚本仍把镜像的 SGLang Python 包替换为指向挂载仓库的软链接：这是自定义开发环境，不是未修改的官方镜像，后续需固定仓库 commit 并核对依赖匹配。

### 2. 容器内安装依赖

在容器内以 root、仓库目录为当前目录，依次运行：

```bash
bash workspace/install_rdma_runtime.sh
bash workspace/install_mooncake.sh
bash workspace/diagnose_mooncake.sh
```

RDMA 安装脚本实时显示 apt 输出及阶段，完整日志在 `/tmp/mooncake-rdma-install-*.log`。安装的系统包是 `libibverbs1`、`ibverbs-providers`、`librdmacm1`、`rdma-core`、`ibverbs-utils`、`libnl-3-200`、`libnl-route-3-200`，递归依赖由 apt 处理。

这一步借用官方通过包管理器安装系统依赖的方法，不是照搬整份 Ascend 源码构建依赖脚本。后者还包含编译工具、源码下载及 OpenMPI 卸载等操作。

Mooncake 安装脚本应出现 `Mooncake Transfer Engine + Store: OK` 和 `mooncake_master:` 路径。重建容器不会继承旧容器内后来安装的包。

网络检查独立、可选：

```bash
bash workspace/check_network.sh
```

它显示 GitHub、pip 源、Ubuntu 源的访问结果，每个地址最多尝试 3 次、每次 20 秒。安装脚本不会自动运行它。GitHub 不通不意味着 Ubuntu 源不通，GitHub 直接访问也可能与 git remote 的代理路径不同。预检查成功不保证大文件下载成功。

当前尚未制作完整离线安装包或固化实验镜像。网络确实无法安装时，应准备匹配发行版/架构的离线依赖或搬运已构建镜像，不能把联网安装成功当作已保证。

### 3. 单节点功能验证

```bash
python3 workspace/check_fabric_local.py
bash workspace/show_fabric_log.sh
```

默认使用逻辑 NPU 0，可用 `--device <逻辑编号>` 调整。测试使用独立临时 master，不复用实验 master，不加载模型；Store 为 1 GiB、本地缓冲为 1 GiB（Fabric 分配有 GB 级对齐要求），实际载荷很小。

测试顺序：导入 → 配置检查 → 设置 device → Ascend Store setup → 4 KiB、64 KiB、1 MiB 写入读回逐字节校验 → close → 正常进程退出。脚本结束会清理自己启动的进程，日志保留在 `/tmp/mooncake-fabric-local-*`。

成功证据：

```text
F1:LOCAL_OK
Fabric mem mode is enabled
verified 4096 bytes
verified 65536 bytes
verified 1048576 bytes
```

日志查看脚本自动选择当前容器最新一次测试日志。失败时报告所处阶段及退出码；先看对应 `probe.log` 或 `master.log`，不要直接重装环境。

## 排障经验

### 模型启动最新进度

双机 Store 测试已由操作者回报 `F2:REMOTE_OK`，双机日志报告 Fabric 模式启用；尚未独立确认物理 HCCS 链路。

V3.1 W8A8 使用 TP16+DP16 时，曾报告加载内存增量 56.44 GiB、剩余 4.64 GiB，自动静态预算 0.665 导致 KV 预算不足。改为 TP16+DP1 后模型启动成功，操作者已通过 `/v1/chat/completions` 得到回答。这不是正式 TP16+DPA16 实验配置通过。

`run_model.sh` 当前是 TP16+DP1 的诊断启动脚本，未开启 HiCache。`col.sh` 原为 V3.2-Exp 的 TP16+DP1，不能仅用版本差异解释内存变化。Attention TP 随 DP 改变，权重分片方式也会改变。

模型启动后在同容器另一个终端运行：

```bash
bash workspace/check_model.sh
# 基础请求通过后，运行非流式、流式、多轮三个功能检查：
bash workspace/check_model.sh --suite
```

检查只使用 `/v1/chat/completions`，从 `/v1/models` 发现模型 ID；也可通过 `--model` 指定。`SERVER_URL` 指定服务地址，`API_KEY` 可指定鉴权。完整请求和响应保存在 `/tmp/sglang-chat-*`，不会把鉴权头写入日志。空文本、仅 reasoning、截断结束或缺失 SSE 结束标志不会被算作通过。若因生成上限截断，可用 `--max-tokens 512` 调整。这些检查验证响应协议和非空完整答案，答案语义需查看输出，不是模型精度评测。

聊天功能检查不会强制 DP rank、清空缓存或宣称 KV 命中来源，也不是正式 TTFT 性能结果。原实验 `/generate` 驱动仍保留，用于精确 token 输入和缓存来源控制；不能因为原始聊天提示词返回空文本就认定该接口不可用。正式实验还需解决 DP16 内存余量、Fabric HiCache 配置和生产请求集成。

| 现象 | 已知含义及处理经验 |
| --- | --- |
| `link already exists` 但 `still missing` | 文件存在不等于动态库加载成功，必须看实际 loader 错误 |
| 缺 `libnl-3.so.200` / `libnl-route-3.so.200` | 早期只挂载部分 RDMA `.so`，未配齐系统依赖；改用 Ubuntu 包安装 |
| `R1:MOUNT` | 宿主机库的单文件挂载阻挡包安装；保留旧容器，用更新后的启动脚本新建 |
| `corrupted size vs. prev_size` | 曾在一个 Mooncake 0.3.11.post1 集成镜像出现；导入结束后退出时 SIGABRT。根因未查明，不能归因于硬件或断言所有该版本均有问题 |
| 新环境 `D2:000000F00` | 库/导入/master/NPU/memlock 检查通过，Verbs 枚举仍为零；不是 Fabric 验收结果 |
| 驱动 25.5.0 | 低于当前官方 Fabric 文档要求；本次由平台升级到 26.1.1，再完成本地功能验证 |

`D2:` 后 9 位依次对应：ibverbs、rdmacm、Mooncake 导入、master、RDMA sysfs、uverbs、ibv 枚举、npu-smi、memlock。`0` 是该项检查成功；其他代码需结合具体检查解释。详细信息由 `diagnose_mooncake.sh` 存入 `/tmp/mooncake-diag-*.log`。

旧 `fix_container_rdma_soname.sh` 仅是历史修补工具，不是当前标准安装步骤。`col.sh` 会改系统参数，且不是当前 Dense 三层 KV 配置，不要当环境检查脚本运行。

## 下一步：跨节点验证与实验

两机不需要互相配置 SSH。操作者可分别进入两个终端启动进程，通过 IP 通信。SSH 免密不是 Mooncake 依赖；实际服务端口、传输引擎连接及底层 Fabric 网络必须可达，知道 IP 或 ping 成功也不能代替传输验证。

待完成的双节点验证：

1. 第二台运行独立 master/store 并贡献小容量存储。
2. 第一台作为客户端，`global_segment_size=0`，避免写入自己的本地 Store。
3. 明确使用 Ascend/Fabric 配置，写入唯一测试 key，读回并校验；保留两端分配、连接、后端日志。
4. 确认对象在第二台，以及实际使用的传输路径；成功后才扩大容量。

双节点脚本为 `check_fabric_pair.py`。两台更新仓库后，先在第二台容器运行：

```bash
python3 workspace/check_fabric_pair.py target --local-ip <第二台IP>
```

等待 `F2:READY`，保持终端运行。在第一台容器执行：

```bash
python3 workspace/check_fabric_pair.py client --local-ip <第一台IP> --target-ip <第二台IP>
```

默认 master 端口 50071，两端可用相同 `--port` 修改；Ascend 引擎还会建立额外连接，不能仅开放 master 端口就认定传输可用。默认逻辑 NPU 0，可用 `--device` 修改。测试不下载依赖。

客户端写入进程退出后，再启动全新的读取进程；两个客户端都不贡献存储段。`F2:REMOTE_OK` 表示读回的数据正确且客户端正常退出，不单独证明物理 HCCS 路径。日志保存在 `/tmp/mooncake-fabric-pair-*`；`show_fabric_log.sh` 可以打印最近一次本地或双机 worker 日志。客户端结束后在第二台按 Ctrl+C 清理测试服务，临时 Store 内的数据随服务结束释放，日志保留。

该脚本已由操作者回报 `F2:REMOTE_OK`。不要直接把现有 `dense_three_tier_kv_exp0/run_mooncake.sh` 当作这一步：其示例 JSON 及 `exp0.py` 仍固定/校验 `protocol=rdma`。

双节点功能通过后，再同步调整实验方案、配置校验与 SGLang 启动环境，以验证 SGLang 的实际 Ascend MLA HiCache 路径、生产请求缓存来源、容量与 TTFT。`F1:LOCAL_OK` 不证明这些集成项可用。最后固定镜像 digest、仓库 commit、wheel/系统包版本、启动参数及两节点环境记录。

## 官方参考

- [SGLang Ascend 安装](https://github.com/sgl-project/sglang/blob/main/docs/docs/hardware-platforms/ascend-npus/getting-started/installation.mdx)
- [Mooncake 安装与 Store smoke test](https://github.com/kvcache-ai/Mooncake/blob/main/docs/source/getting_started/quick-start.md)
- [Mooncake Ascend 依赖安装脚本](https://github.com/kvcache-ai/Mooncake/blob/main/scripts/ascend/dependencies_ascend_installation.sh)
- [Ascend Direct / Fabric Memory](https://github.com/kvcache-ai/Mooncake/blob/main/docs/source/design/transfer-engine/ascend_direct_transport.md)

以上主分支链接会更新；本次经验不等于未来版本的兼容性承诺。修改环境前应对照实际安装版本。

## 协作约定

- 公开代码不写入真实容器名、IP、内部路径或凭据，运行时通过参数提供。
- 远端由操作者手工执行；需要其拉取的代码改动应提交并 push 后再通知执行。
- 诊断结果便于手工回报；安装和网络检查显示正常可读的进度与错误，不隐藏过程。
- 一次只给下一步需要执行的操作，不把尚未验证的推测表述为结论。
- 文档文件名短、可读，例如 `ascend-setup.md`，避免冗长的全大写文件名。
