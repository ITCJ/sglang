# Ascend mempool 阶段交付与 NPU 验收

## 执行环境

当前 agent 在开发 Mac 上工作；实际 NPU 测试由用户人工执行。
后续若用户明确开放 NPU 访问权限，再按新的授权安排执行测试。
本流程适用于本 feature 的每一份 implementation ticket。

实现以 [spec](spec.md) 为准，术语以 [CONTEXT.md](../../CONTEXT.md) 为准。
Ticket 01 的独立功能验证按用户确认放在仓库根目录 `ascend-mempool-test/`，
代码与测试脚本分别分类，不依赖 SGLang 服务启动，也不加入 SGLang 自带测试。
后续服务集成代码主要落在 Ascend/NPU 模块；必要的共享参数或生命周期 hook 应保持小且兼容现有模式。

## 每阶段的交付流程

1. 实现该 ticket 的完整可验证行为，并执行 Mac 上适用的静态检查和 CPU 测试。
   在 ticket 的 `Comments` 中记录修改、实际执行的检查、结果以及待验证内容。
2. 向用户核对本阶段的实现：用中文说明正常执行路径、数据所有权、同步和释放条件，
   给出实际代码位置，解释影响该阶段的设计选择，并回答用户对代码设计的疑惑。
   状态、变量及协议消息沿用英文名称。
3. 完成本阶段的 NPU 测试脚本和运行说明后交付用户。交付内容应足以直接执行：
   前置条件、P/D 启动顺序、环境变量和参数、逐机或逐 rank 的命令、测试输入、
   预期结果、失败判据，以及需要回传的日志和输出文件。
   命令和参数以已实现的脚本与 CLI 为准，机器地址及本地权重路径使用明确占位符。
4. 等待用户执行 NPU 测试。收到结果后分析日志，与用户核对现象，调整实现或测试脚本，
   提供对应的重测命令，直到该阶段的验收条件全部通过。
5. 用户确认本阶段的实现和 NPU 验收后，把执行环境、对应代码版本、命令、日志位置、
   实际结果和确认结论写入该 ticket 的 `Comments`，勾选已满足的验收项并关闭 ticket。
   这时才解锁依赖它的任务。整个 feature 的验收要求主线01–08及新增的
   [10: GLM-5.2算法适配](issues/10-glm52-indexer-sharing.md)完成；10在03后、04前执行。
   [09: NUMA分配跟进](issues/09-numa-allocation-followup.md)按用户2026-10-02决定延期，
   不构成主线blocking edge，也不是新增的demo验收门槛。其延期不替代真实KV、Graph
   与生命周期的硬件验收；这些检查可先用已可运行的小容量配置完成。

## 进度和证据

- `Status` 使用既有 triage vocabulary，`State` 单独记录 `open` / `closed`。
  代码交付、Mac 检查通过、测试脚本就绪时，仍保持 `State: open`；
  在 `Comments` 中明确写出“等待用户执行 NPU 验收”及尚未通过的验收项。
- 检查记录区分“agent 在 Mac 实际执行”“用户反馈的 NPU 结果”“尚未执行”。
  Mac 检查无法证明远端 BM 数据正确、NPU Graph capture/replay 通过或模型精度通过。
- 已反馈的 eager BM benchmark 和普通 sparse PD UniDexCopy Graph 结果可作为历史依据；
  当前 ticket 要求的 mempool Graph 或真实模型路径仍需对应的实测证据。
- 失败记录包含可复现输入、错误或差异、相关 rank 日志、定位结果、修复及重测结果。
  明确保留未解决问题；关闭 ticket 时应能从记录判断每项验收如何通过。
- 具体运行命令随各阶段的实际实现和脚本一起交付。
  Ticket 01 的环境检查、双机 gate 和日志清单见
  [独立测试说明](../../ascend-mempool-test/README.md)。
