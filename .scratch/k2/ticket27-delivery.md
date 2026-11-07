# Ticket27 实现交付

当前分支 `cryang/dev/mempool-min`。从 PR 分支 `cryang/pr/sparsity-pd-disagg-LRU` 移植容量和 timestamp-LRU + probation；保留现有四来源物化，仅回填 miss。默认 LRU 关闭，开启后 top-k2048、cache默认4096。

kernel 接口按用户给出的两个分支使用，合并由同事完成。旧独立扩展源码、构建脚本、加载兜底和环境路径注入已删除。用户在 Docker 安装组合 wheel 后运行验收。

[运行说明与最终源码阅读清单](../../ascend-mempool-test/TICKET27.md)分列五个NPU文件、两个通用文件和测试脚本。通用变更已在实施前说明。

## 本地验证

新增6个测试通过。完整集合执行一次649项；25项受影响测试复跑通过，另加1项host-offload LRU回归通过。共650项记录，643通过。余下7个方法在只读基点659b949ece上也复现相同错误，包含过期UINT32断言、端口10000预期、缺失available_port旧mock和历史IndexedCopy导入。

类型检查8个相关文件通过（Mac外部SDK缺失imports排除）；较宽mempool检查仍有外部依赖及既有类型/平台问题。Ruff、22个Python文件语法、启动shell语法、diff检查通过。详情见[checks.json](verification/ticket27-local/checks.json)。未重复全量，同一测试方法最多两轮。

## Standards

发现1项组件可能导入另一checkout的问题。已将当前repo/python前置到组件子进程和直接组件入口，并记录源代码HEAD。评审静态复核修复，最终0项未解决。未修改通用shutdown。

## Spec

同一项组件源码定位问题已修复并复核。容量、元数据、稳定hit、miss回填、有效性掩码、请求复用和旧编译清理均已接线，最终0项未解决。评审没有执行NPU测试。

Standards：1项已修复、0未解决。Spec：1项已修复、0未解决。

## 验收边界

用户授权在26关闭前独立实施27；不关闭26，也不标记22整体完成。27保持needs-info/open，等待用户NPU验收。

每台一条命令自动执行库预检、B1 eager/B16 Graph组件和真实双机服务LRU off/on完整token对照。组件使用合成HBM backing，不能替代真实BM服务验证。日志在`/home/cryang/log/k2/`；`--components-only`只算smoke。
