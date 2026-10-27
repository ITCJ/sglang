# ticket03 排查代码清理

2026-10-27用户要求撤销为C输出分叉而加入的实现代码，收敛到该问题之外已有验证证据的
ticket03版本。清理前HEAD为5d922b8686，运行代码基线为0308cd0814。

## 核对与恢复范围

0308cd0814之后的实现改动来自8579aa2bab、f3e522f9b6、1c0280132c、7229eb70f0和d9ba311dd4。
它们添加诊断记录、同输入重放、摘要以及普通路径对照的独立开关，没有新增必须保留的
prefix恢复/fill/后缀/释放修复。按用户要求精确恢复运行代码和配套测试，不回退分支历史。

- 恢复8个已有运行源文件，移除2个运行探针模块。
- 恢复3个测试脚本和6个既有测试文件，移除3个诊断脚本和3个专用测试模块。
- Python运行源码与测试入口/测试内容逐文件等于0308cd0814。
  测试README单独更新当前状态，不算运行代码差异。
- 保留原始ticket03的匹配、真实allocator恢复、独立fill与suffix、ready和free_ack合同。
- 保留原始verify_prefix内容校验及验收边界控制。verify_prefix恢复为原有联动方式；
  两个只为诊断拆出的配置字段和CLI参数一并移除。
- 保留所有硬件报告、ticket03关闭决定及后续逻辑题约定。
  本次不修改相邻独立启动脚本仓库的语义题客户端。

## 删除的临时接口

运行模块：debug_reuse.py、debug_post_attention.py。
测试脚本：replay_mempool_reuse.py、summarize_mempool_reuse.py、summarize_mempool_post_attention.py。
测试模块：test_mempool_reuse_probe.py、test_mempool_reuse_diagnostic.py、test_mempool_post_attention.py。

移除reuse-diagnostic、diagnose-reuse、diagnose-post-attention、replay-run，
以及force-noncontiguous-restore/delay-prefix-fill诊断参数。
RouterConfig不再接收force_noncontiguous_restore/delay_prefix_fill字段；
旧排查生成的配置不应作为新启动配置复用，应使用原始用户配置或现存配置模板。
原始serve/acceptance/prefix-acceptance/reuse-acceptance仍保留。

## 功能源码阅读顺序

1. [mempool_router_protocol.py](../../python/sglang/srt/disaggregation/ascend/mempool_router_protocol.py)：恢复原配置合同，移除两个独立诊断控制。
2. [mempool_prefill_restore.py](../../python/sglang/srt/disaggregation/ascend/mempool_prefill_restore.py)：恢复原verify_boundaries，保留真实allocator和prefix索引。
3. [mempool_subrouter.py](../../python/sglang/srt/disaggregation/ascend/mempool_subrouter.py)：恢复原请求绑定和fill门控，移除探针参数。
4. [mempool_router.py](../../python/sglang/srt/disaggregation/ascend/mempool_router.py)：恢复原注册校验，不再协商独立诊断控制。
5. [mempool_router_transport.py](../../python/sglang/srt/disaggregation/ascend/mempool_router_transport.py)：移除模型hook安装，恢复原恢复器装配和注册字段。
6. [prefix.py](../../python/sglang/srt/hardware_backend/npu/mempool/prefix.py)：移除探针状态与调用，保留原prefix存储和内容校验。
7. [ascend_backend.py](../../python/sglang/srt/hardware_backend/npu/attention/ascend_backend.py)：恢复原attention调用，移除元数据捕获和额外重放。
8. [deepseek_v2_attention_mla_npu.py](../../python/sglang/srt/hardware_backend/npu/modules/deepseek_v2_attention_mla_npu.py)：移除层输入和投影检查点。
9. [run_mempool_min.py](../../ascend-mempool-test/scripts/run_mempool_min.py)：恢复原四种模式和预检预算，移除诊断编排/报告。
10. [verify_mempool_prefix.py](../../ascend-mempool-test/scripts/verify_mempool_prefix.py)：恢复原严格内容及边界证据核对。
11. [verify_mempool_reuse.py](../../ascend-mempool-test/scripts/verify_mempool_reuse.py)：恢复原ABC内容、索引和生命周期证据合同。

服务改动全部在Ascend/NPU目录。后三个脚本负责服务外启动、离线证据和报告，
属于既有独立验证工具；恢复其接口需要在该目录完成，不能由模型内部hook替代。

## 验证与保留限制

已核对运行代码和测试树与0308cd0814完全相同；本机453项CPU回归通过，39.211秒。
17个恢复的Python文件通过lint/format/isort及Python3.9语法检查；9个模块strict mypy通过。
Standards与Spec最终均0项未解决。文档中的旧诊断入口、verify_prefix联动和关闭状态已修正。
[检查与源码摘要](verification/ticket03-cleanup-20261027/checks.json)、
[两轴review](verification/ticket03-cleanup-20261027/review.md)保留证据及实际范围。
不新增仅验证删除动作的实现镜像测试，复用原ticket03的完整行为套件。
未执行新的NPU测试。此清理不改变已定位的归约数值差异，不宣称原合成C输出已一致。

ticket03仍按用户授权的功能范围关闭；ticket04的新逻辑题、完整E/拒绝/故障组合仍待实施。
现存reuse-acceptance仍使用旧合成输入及严格比较，不能作为新输出约定已实现的证据。
历史诊断文档保留但标记入口已移除，不再把旧命令当作当前可用操作。
