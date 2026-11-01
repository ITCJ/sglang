---
title: K1-ticket08：进度与执行计划
subtitle: syncfile 定义范围；本地代码和验收记录定义当前状态
template: doc
theme: blueprint
lang: zh
date: 2026-11-01
status: 规划完成，08 尚未实施
---

08 已解锁。目标是把 prefix 两类数据各改为单份。
下一轮 xhigh 按[完整执行计划](ticket08-implementation-plan.md)实施。
本轮没有修改功能，也没有运行 CPU 或 NPU 验收。

## A 目前走到哪里

| 项目 | 当前状态 |
|---|---|
| sparse 阶段 0 / L0 | 最终完整验收已获用户确认 |
| K1 01、02、03 | 映射、适配器、split_read 已验收关闭 |
| K1 05、06 | P/D alive 主 KV 共享已验收关闭 |
| K1 07 | 单份 Index K 已获用户确认关闭 |
| K1 08 | 前置全部满足，计划已完成，代码未实施 |
| K1 11、09 | 等待前票各自验收 |
| K1 04、10 | 尚未完成，不阻塞主线 |

07 严格脚本仍 failed。用户接受了已核对的功能范围。
源快照与正常恢复一致；歧义题冷、热输出相同。
未执行的重启、故障注入、物理驻留和性能仍未验证。

## B 08 要改变什么

| 数据 | 当前份数 | 08 目标 |
|---|---|---|
| P alive 主 KV | 8 | 8 |
| D alive 主 KV | 8 | 8 |
| P alive Index K | 1 | 1 |
| prefix 主 KV | 16 | 1，分 8 段 |
| prefix Index K | 16 | 1，同页同段 |

默认总容量保持 40 页，每段 5 页，页大小 128 tokens。
段 k 默认由 rank 2k 分配，位于 NUMA k。
16 个映射者共享八段数据，不增加物理副本。

## C 三条数据路径

| 操作 | 执行方式 | 完成条件 |
|---|---|---|
| admit / op3 | 八个 owner 各写本段；另有 single_rank 配置 | 所有实际写者完成后，按父页到子页发布 |
| load_back / op2 | 按段分给同 NUMA 读者，再 all_gather、scatter | 16 rank 都恢复完成 |
| fill / op1 | 八组各读全部段，填回各自主 KV 副本 | 八份主 KV 和单份 Index K 均完成 |

admit 从本组 alive 主 KV 读源；Index K 源始终是单份区域。
起始段按请求轮转；段满后选空闲页最多的段。
Main Router 每页保存一个不透明描述符。

## D 三个容易漏掉的改动

| 问题 | 计划处理 |
|---|---|
| 每段只有 5 页，但旧 view 的层跨度是 40 页 | 每段使用真实几何，逐层解析地址 |
| P 非 owner 仍会为探针分配 1 GiB | 复用 D 的 owner 探针分槽，让 follower 贡献为零 |
| 旧内容校验使用接收 rank 自己的快照 | 按真实主 KV 写者及 Index K 写者关联源摘要 |

沿用 07 的 32 参与者 BM 会话。
现有索引拷贝需要设备映射，范围覆盖实际访问。
不把 BM 地址当成可直接解引用的 CPU 指针。
静态预算预计 P 为 96 GiB、D 为 8 GiB，验收快照另计。

## E xhigh 的五步实现

```flow
配置与分段 -> 冷请求保存: 单份物理页可用
冷请求保存 -> 命中恢复: 单位置发布可用
命中恢复 -> 验收入口: 三条路径贯通
验收入口 -> 检查与交付: 两轮自运行
检查与交付 -> 用户NPU验收: 08保持open
```

每步立即更新 target-alignment 的 D 节。
功能代码预计只涉及 Ascend/NPU 目录。
交付包含源码阅读顺序、实际验证结果和逐机命令。

## F 验收边界

CPU 检查分段内容、单写者、全副本 fill 和延迟完成门控。
NPU 复用计算源快照与正常 load_back 的 HBM 内容。
沿用用户约定，不新增请求级 BM 直接读回。

两轮服务复用 A/B/C/E、长生成和 EOS 六请求。
07 的额外逻辑实验按入口选择，不由共享开关自动触发。
正常恢复内容、直接 DRAM 内容、NUMA 驻留和性能分别报告。
CPU 通过不代表 NPU 验收通过。

## G compact 后从哪里继续

[完整执行计划](ticket08-implementation-plan.md)包含接口、文件、测试和每步完成条件。
[syncfile ticket08](/Users/hibikid/Documents/syncfile/sglang-kv-explainer/mempool-min-demo/k1/issues/08-k1-05b-prefix-single-copy-numa-interleave.md)是范围来源。
本轮已确认正文与本地同步稿一致。

```flow LR
08单份布局 -> 08用户验收 -> 11异步搬运 -> 11用户验收 -> 09完整验收
```

08 沿用当前索引拷贝；批量异步执行器在 11 实现。
性能工具留测试分支，默认退出沿用 SGLang 原有路径。
