# Ticket09 CPU与交付记录

日期2026-11-02；NPU未运行，ticket09保持open。

- 配置/注册、长生成、D哨兵、矩阵配置采用公开接口red→green。
- `full.log`：完整套件一次，553项，2 failures、25 errors。一个failure是既有test_config对旧UINT32_MAX边界的预期；另一个为本次严格四类拒绝后，旧报告夹具缺unsupported-text。
- `focused.log`：修正报告夹具后的关键39项通过，含四组配置、真实共享D CPU读写、末行、两页边界、四组独立地址oracle、完整报告与矩阵生成。
- `retry.log`：25个沙箱错误对应21个方法，沙箱外补跑全部通过。仅本机socket/子进程，未访问NPU。
- `mypy.log`：4个功能模块strict mypy通过；修改Python文件ruff及AST检查通过，git diff --check通过。
- `matrix-describe.json`：四组有效配置预览。计划与实现保持5120-token prefix容量。
- 未第三次重跑全套；后续评审修正的矩阵信号、run-id、报告保留、独立NPU边界组件以静态复核为主，硬件验证待用户。

Standards双轴复核：初始3项（信号清理、端口核验、默认路径调试工作）均已修复，复核无阻断。
Spec初始4项（边界fetch、末行、归档/端口、四组oracle）均已修复；复核追加矩阵run-id隔离并修复。
报告汇总另修正保留K1证据，失败保持failed，不用默认128参数覆盖最终结果。

验证边界和每机一条命令见 `ascend-mempool-test/K1_TICKET09.md`。
