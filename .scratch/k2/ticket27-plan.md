---
title: Ticket 27：最小 LRU 移植与 kernel 库接入
subtitle: 规划记录；现已实施，用户 NPU 验收待运行
template: doc
theme: blueprint
lang: zh
---

## A 结论

**状态更新：27已实现。** [运行说明](../../ascend-mempool-test/TICKET27.md)包含最终源码与验收命令。
下列规划保留来源依据，实施与检查结论以票据Comments为准。

你的四项划分基本正确。还需请求复用重置，以及 Graph 和跨流同步。
当前已有上一轮 top-k 缓存；本票将其升级为 PR 的 LRU 策略。
建议独立一票，不合并整个 PR 分支。

| 项目 | 核对结果 |
|---|---|
| 当前分支 | cryang/dev/mempool-min，659b949ece |
| LRU 来源 | cryang/pr/sparsity-pd-disagg-LRU，d8422c1d76 |
| 新 kernel | feat/unidex-copy-multibase，fe23057，已 fetch |
| 当前缓存 | 每轮替换为 selected top-k |
| 目标缓存 | timestamp-LRU + probation，仅回填 miss |
| 当前状态 | Ticket 27 已建，needs-info/open |

## B 先补齐 kernel 依赖

新分支提供 multibase，但缺少 PR 的 LRU 接口。

| 接口 | multibase 分支 | 本地 LRU kernel ref |
|---|---|---|
| unidex_copy_multibase | ok 有 | 未作为组合库验证 |
| fused_timestamp_lru_metadata_update_with_probation | no 无 | ok 有 |
| parallel_lru_metadata_write | no 无 | ok 有 |
| slot_map_lookup 的 pos_mask_size | no 无 | ok 有 |

LRU kernel 参考为 `origin/bw/pr/feat_lru_with_probation`，e7a9cb60。
该 ref 本轮未刷新；最终库版本需重新固定。
用户同事将 fork 此 LRU 分支，再合入 multibase。
agent 不执行合并，按同事最终交付库接入。

最终 fork、branch 和 commit 尚待交付。
`build.sh -a kernels` 产出 wheel，之后还需安装 wheel。

## C 容量要拆成两个数

| 数量 | 用途 | PR 约束 |
|---|---|---|
| top-k | 当前 attention 的 selected KV | LRU 模式固定 2048 |
| cache capacity | 每请求每层驻留的历史 KV | factor × 2048，factor 为 1–4 |
| request storage rows | cache 的请求行数 | 含实际 request-pool padding 等行 |
| Graph batch width | 计算与索引模板的批宽 | 不扩大整份历史 cache |

默认 factor=2，cache 为 4096；selected KV 仍为 2048。
预留固定 HBM 后再计算 Index K 容量。
三张新增 int32 表也应计入预算。
PR 预算只判断 host offload，移植时须覆盖 mempool decode。

## D 保留当前数据路径

```flow
top-k -> slot-map查询: 命中位置
slot-map查询 -> 四来源物化: cache/current HBM 与 P/D BM
四来源物化 -> selected-KV: 等待物化完成
selected-KV -> LRU更新: 选 victim 并更新映射
LRU更新 -> miss回填: 从 selected HBM 写 cache
miss回填 -> 后续读取: 等待回填与映射完成
```

hit 保留原物理槽；miss 才回填。
新取入项使用 probation，默认 age=4。
请求行复用时重置映射、slot 顺序及时间戳。
保留当前 token、绑定、已写前缀和 padding 的有效性规则。
保持现有 attention 接口，不移植额外 overlap 重构。

## E 最小文件范围

| 分类 | 文件 | 用途 |
|---|---|---|
| NPU | sparsity_driven_kv_offload/config.py | 容量及固定 HBM 预算 |
| NPU | memory_pool_npu.py | NPU 预算适配 |
| NPU | sparsity_driven_kv_offload/manager.py | 分配、LRU、回填、重置 |
| NPU | mempool/multibase.py | 使用安装库 |
| NPU | mempool/runtime.py | 如实记录容量和策略 |
| 通用 | environ.py | 注册三个既有 PR 环境变量 |
| 通用 | mem_cache/kv_cache_configurator.py | 定容前调用 NPU 预算 |

通用预算入口掌握原始字节预算，manager 分配发生得更晚。
因此需要一个小调用，算法仍放在 NPU 目录。

预计七个功能源码文件；旧扩展、脚本与测试清理另列。
ENABLE_LRU 默认关闭，FACTOR 默认 2，PROBATION_AGE 默认 4。

旧编译路径整体清理：C++/CMake、构建脚本、加载兜底和路径注入。
诊断与 Graph 报告改为记录安装库身份。
保留有用的行为测试与历史验收证据。
本地未发现独立扩展 build 产物；Docker 未检查。

## F 实施与验收边界

1. 用户已授权当前分支实施，按两个kernel分支原接口接入。
2. 用户已允许26关闭前实施，kernel合并由同事执行。
3. 精简 TDD 验证预算、回填、复用及 padding。
4. 用户跑算子 Graph 和定向服务对照。
5. 完成双轴评审、独立 commit 与用户验收。

26与27均未关闭，用户NPU验收尚未运行。
本票覆盖 22 的容量/策略子集；跨请求合并和性能对比仍留 22。

沿用已接受的输出判据，日志放 `/home/cryang/log/`。
不再现场编译；不扩展性能排查或通用退出流程。

完整约束、源码基点和验收项见 [Ticket 27](issues/27-lru-cache-library-integration.md)。
