# Target hardware boundary

- Ascend experiments in this workspace target Ascend A3 (910C) machines. The machine running the current Codex session is not a target machine.
- Do not run target-machine model serving, profiling, or performance benchmarks in this session. Perform static checks here, provide commands for the target machine, and use results reported from that machine for runtime conclusions.

# Agent 工作规范

以下规范适用于本仓库中由 Agent 协助开展的 Ascend 远端实验。

## 文档分工

- `workspace/ascend-setup.md`：环境、容器和依赖安装。
- `workspace/fabric-note.md`：本地保存的测试事实、命令、日志和结果；不提交 Git。
- `workspace/research-status.md`：当前研究目标、已确认结论、方向和边界。
- `workspace/kv-path-plan.md`：KV 路径实验设计和交接任务。当前任务入口以 `workspace/research-status.md` 指向的计划为准；旧的正式实验方案只作历史参考。

不要把测试结果写进环境部署文档，也不要把临时判断写进实验计划。研究方向变化时更新 `workspace/research-status.md`，实验设计变化时更新对应的实验计划。

## 远端执行

实验 NPU、Store 和完整执行环境都在 Agent 无法连接的远端服务器上。本地工作区只用于编写和静态检查代码，不能代替远端环境；本机不执行任何实验、功能测试、单元测试、集成测试或性能测试。所有测试均由用户在远端服务器手动执行并回传结果。凡是需要远端拉取的代码或脚本，按以下顺序处理：

1. 在本地修改并检查文件。
2. 提交前列出准确的文件清单和 diff；不把无关的未跟踪文件加入暂存区。
3. 用户明确要求后再提交和 push，并报告分支名和 commit。
4. 远端先运行 `git pull --ff-only`，再用 `git log -1 --oneline` 核对 commit，之后才能执行测试命令。

远端命令必须使用占位符，例如 `<CLIENT_IP>`、`<STORE_IP>`、`<MODEL_PATH>`；不把真实 IP、容器名、内部路径或凭据写入仓库。一次交接给出一个完整的小实验，包括目的、两端命令、成功标志、失败时的短日志命令和清理方法，不把一个实验拆成只交付一个步骤。

## 网络与传输

- 测量某个互联网络时，使用该网络接口在两台机器上的 IP。使用 RoCE 互联网卡的 IP 运行 `protocol=tcp`，表示普通 TCP 经过该网卡，不表示使用 RDMA。
- SSH 使用的管理 IP 只有在明确测量管理网络或作为对照时才使用。
- Mooncake 的 `protocol=tcp`、Mooncake Fabric/Ascend transport、以及 SGLang `--disaggregation-transfer-backend ascend` 是不同路径，不能用一个测试结果替代另一个。
- 使用当前 Ascend Mooncake 构建运行 TCP probe 时，脚本需要设置 `MC_FORCE_TCP=1`，避免引擎初始化时自动安装 Ascend transport。

## 测试安全与证据

- 新实验或新环境开始时，Agent 应提醒用户先手动运行最小规模的数据正确性校验，并提供明确的校验命令；通过后再进行性能测试。涉及 pinned Host 内存或 NPU 的测试应在模型停止时进行。
- 后续实验的数据正确性校验应做成可选项（例如 `--validate`），性能模式默认关闭，包括性能套件中的冒烟步骤。校验由用户显式开启，不要求每组规模、布局、路径或每轮性能采样都重复校验；独立的正确性测试入口仍应执行校验。
- 关闭数据校验时仍须检查接口返回值、异常及传输完成状态；结果应明确记录是否启用校验，不能把未校验的性能运行标记为数据正确性通过。
- 失败时先停止依赖该服务的后续步骤，记录阶段、退出码和对应日志末尾；不要仅凭 ping、端口可连或初始化成功宣称数据路径可用。
- 报告中区分：功能通过、传输路径确认、性能测量和物理链路确认。`TCP:REMOTE_OK` 只证明 TCP Store 读写校验通过。
- 记录两端仓库 commit、镜像 digest、CANN、驱动、Mooncake wheel、启动参数、使用的 IP 和日志目录。

## 实验公平性

- 与 HiCache 比较时，先对齐其现有对象管理：一个逻辑 page 对应一个 Store key，一次可以批量读取多个对象。
- 不自行改变 page 对象粒度、请求数据或目标地址后，再把差异归因于传输路径。
- L1、L2 使用实际 Ascend MLA 布局；L3 的对象组织和片段顺序在任务中明确写出。新增布局、对象聚合、缓存策略或流水优化都要单独标记为实验变量。
- 比较多条路径时，使用相同逻辑 KV、相同 page 映射和相同批量划分；必要的转换、拷贝、同步和暂存内存计入对应路径。
- TCP 只能作为功能或诊断路径，不能作为 Ascend 超节点高速互联的性能 baseline。

## 交接格式

每次安排远端测试时，消息按以下顺序写：

1. 本次实验要回答的问题。
2. 远端前置条件和两端完整命令。
3. 预期成功标志。
4. 失败时执行的一条短日志命令，以及需要回报的日志范围。
5. 测试结束后的清理动作。

收到结果后，先更新本地 `workspace/fabric-note.md` 的事实记录；只有研究结论或实验设计改变时，才分别更新 `workspace/research-status.md` 或对应的实验计划。
