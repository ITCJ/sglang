---
title: Ticket26：整图 swap-in 对照
subtitle: 沿用 eager 数据，测整个 metadata 与 copy 图
template: doc
---

## A 范围和计时

用户要求在 eager 实验之后测 Graph，只看整个图耗时。
生产源码与 kernel 均不改；不运行模型、observe 或 trace。
图包含 lookup、hit/miss 索引、mask、页路由、任务表更新以及 copy。
Baseline 保持 hit/miss 双 stream，main 等待两路完成。
合并版在 miss stream 上执行一次 multibase，main 等待完成。

每种 B、每个版本做3次 eager预热、1次capture、1次不计时replay。
之后回放100次；每次都在计时外复制新selection与slot-map到固定地址。
图内重算可变metadata，源KV和BM映射固定；每轮在计时外校验结果。
不把 capture 时的固定metadata反复复制当作本实验。

| 指标 | 口径 |
|---|---|
| graph_ms | main stream 上，graph.replay前后两个NPU event的间隔，包含fork/join后的整个图 |
| total_ms | 提交开始至完成event同步返回的主机墙钟时间 |
| group_elapsed_ms | 同一D宿主机最早计时开始至最晚完成；包含rank启动偏差，不等于设备图耗时 |

Graph外event间隔仍可能含replay提交空隙，不是profiler纯kernel时间。
输入staging、output poison、capture、预热、正确性检查不计入graph_ms或total_ms。
rank协调发生在staging之前，整组墙钟可能体现staging造成的启动偏差。
不再输出每个copy和metadata分项计时，也不在copy前后插入计时event。

## B 配置

P/D各1、2、16rank，NUMA依次为0、0、0–7。每组都测B1/B16。
每请求TOPK2048、BF16、576维；保持78层page-first跨度，只测第0层。

每请求有效行：cache1657、P385、D5、current1。
同轮两版使用相同selection种子，每轮改变地址和任务顺序。
基线固定aa5852624e15df139df7c76bb8d757341b049713。
沿用微测current src_index.contiguous适配，其设备操作也进入图。


启动block_dim不变：baseline cache24，current/P/D各48；multibase48。
该值是启动配置，不是实测Vector Core占用率。
资源预算与eager相同：P/D分别为11/1、12/1、96/8 GiB。
NUMA请求配置不等于物理驻留已验证。

## C 第一组032命令

用户确认前轮资源已清理后，先31再32；脚本自动新建日志目录。
只改Python，无需重新编译kernel。两侧代码须一致。

31容器：

```bash
cd /home/cryang/sglang &&
git pull --ff-only &&
python3 -u ascend-mempool-test/scripts/run_swapin_eager.py \
  --role prefill --tp-size 1 --graph \
  --run-id k2-ticket26-032-graph-tp1 --control-port 23200
```

32容器：

```bash
cd /home/cryang/sglang &&
git pull --ff-only &&
python3 -u ascend-mempool-test/scripts/run_swapin_eager.py \
  --role decode --tp-size 1 --graph \
  --run-id k2-ticket26-032-graph-tp1 --control-port 23200
```

日志：/home/cryang/log/k2/k2-ticket26-032-graph-tp1/{prefill,decode}/。
成功标记SWAPIN_GRAPH_MEASURED，report.json为measured且cleanup=passed。
失败返回非零，保留report.json、rank-N.log和rank-N/failure.json。
通过后用户确认清理，再逐组执行TP2、TP16；不自动连续启动。

## D 文件和验证

生产功能源码：无改动。通用主线源码：无改动。

脚本阅读顺序：
1. run_swapin_eager.py：新增--graph，复用双机协调、报告收集与进程清理。
2. swapin_worker.py：抽取共同swap-in图体，保持eager计时边界。
3. swapin_graph.py：固定输入、capture、整图计时和逐轮输入更新。
4. swapin_experiment.py：按graph_ms/total_ms汇总，不伪造分项kernel时间。
5. test_swapin_graph.py：CPU检查配置、计时边界、地址不变且输入更新、报告完整性。

3项新增CPU检查首轮通过；适配器解耦后仅相关1项补跑，通过。
静态检查通过。CPU检查不证明NPU多stream capture与kernel兼容性。
Graph硬件待用户执行；ticket26保持open，25不解锁。

## E 已收到的 eager 结果

029-r2、030、031均measured、cleanup passed，每rank每case100样本。
以下为用户回传均值，单位ms；完整原始报告仍在用户机器。

| TP | B | baseline copy和→合并 | baseline总耗时→合并 |
|---|---:|---|---|
| 1 | 1 | 0.223867→0.067700 | 3.179971→2.791401 |
| 2 | 1 | 0.233841→0.071981 | 3.478559→2.890112 |
| 16 | 1 | 0.307151→0.087775 | 5.377714→4.515912 |
| 1 | 16 | 0.462790→0.361918 | 5.364872→5.141235 |
| 2 | 16 | 0.468161→0.362811 | 5.177883→5.009949 |
| 16 | 16 | 0.521587→0.363004 | 6.866775→6.050459 |

这些event间隔不是纯kernel时间，多stream之和不等于总耗时。
不能从eager改善直接推出模型D Graph性能，也不重跑已完成模型baseline。
