---
title: Ticket04 完整自运行验收
subtitle: 逻辑ABCE、V1–V10、独立故障运行与源码阅读顺序
template: doc
theme: blueprint
lang: zh
date: 2026-10-27
status: ticket04-002完整NPU自动验收通过；等待用户确认关闭
---

**同一入口新增 `full-acceptance`，自动完成阶段 0 的完整验收。**
每机一条命令，依次运行独立参考组、目标组、容量故障和超时故障。
结果按 [输出约定](output-verification-policy.md) 判定。
本票完整 NPU 自动验收已通过，保持 open 等待用户确认关闭。

用户首轮 `ticket04-001` 在输入生成阶段失败，V1–V10 均未执行。
已修复模板 API 默认返回字典导致的误比较。
[修复记录](verification/ticket04-tokenizer-20261027/README.md)保存复现与检查证据。

用户回传 `ticket04-002` 的完整结果：V1–V10 全部通过。
稳定性、长生成、超时保护与双端清理也全部通过。
[本次 NPU 记录](verification/ticket04-002-20261027/README.md)保存终端原文与摘要。
以下保留本次运行命令；再次运行时使用新的 run-id。

## A 功能源码阅读顺序

| 顺序 | 文件 | 阅读目的 |
|---|---|---|
| 1 | [mempool_logical_cases.py](../../ascend-mempool-test/scripts/mempool_logical_cases.py) | 用真实模板生成精确长度逻辑题；分别报告标准答案与完整IDs差异 |
| 2 | [verify_mempool_reuse.py](../../ascend-mempool-test/scripts/verify_mempool_reuse.py) | 将既有ABC内容、映射和生命周期检查扩展到E及独立控制请求 |
| 3 | [verify_mempool_min.py](../../ascend-mempool-test/scripts/verify_mempool_min.py) | 保存每请求、每rank实际decode步数；验证长生成与空prefix超时 |
| 4 | [ascend/mempool_router_transport.py](../../python/sglang/srt/disaggregation/ascend/mempool_router_transport.py) | 在隔离故障运行限制D rank15的注册容量，并保留真实容量证据 |
| 5 | [verify_mempool_full.py](../../ascend-mempool-test/scripts/verify_mempool_full.py) | 校验容量拒绝并汇总V1–V10；区分failed与not-run |
| 6 | [run_mempool_min.py](../../ascend-mempool-test/scripts/run_mempool_min.py) | 串联双机阶段、独立池、请求、证据汇集、退出与清理 |

服务运行代码只改动一个 Ascend 文件，无主线目录功能修改。
其余功能代码属于既有独立 Ascend 验收工具。
双机进程、tokenizer fixture和离线报告需要由服务外入口管理。
模型内部 hook 无法代替这些职责，因此沿用 `ascend-mempool-test/scripts/`。
单元测试与解释文件不列入上面的阅读顺序。

## B 正常路径与释放条件

参考组使用独立服务和池，关闭 match_prefix。
目标组从空 prefix 开始运行 A→B→C→E，再运行长生成与提前 EOS。
两组使用同一份保存的 input IDs、模型身份和采样配置。

| 请求 | 输入token | hit_len | P新算token | prefix页／份 | D Index K预留页／rank |
|---|---:|---:|---:|---:|---:|
| A | 4096 | 0 | 4096 | 32 | 33 |
| B | 5120 | 4096 | 1024 | 40 | 41 |
| C | 4096 | 3968 | 128 | 40 | 33 |
| E | 4096 | 0 | 4096 | 40 | 33 |
| 长生成控制 | 4096 | 0 | 4096 | 40 | 33 |
| 提前EOS控制 | 128 | 0 | 128 | 40 | 2 |

逻辑题的答案依赖前部地点与初始数量、中部赠送量、尾部新增量。
B 的追加用户消息改变数量与路线，E 使用另一组事实。
完整模板必须满足B的前4096 IDs等于A，C等于A。
入口只增加完整句子和空白，不截断问题或任意拼接词表ID。
无法满足真实tokenizer合同就明确失败，并保留现场。

输出规则分别检查参考与目标的独立标准答案。
重复字段、缺字段、错误类型、截断或错误答案均不通过。
完整IDs是否相同、长度和首差独立记录。
独立冷算参考A/C另做同条件稳定性比较。
新出现的不一致记为anomaly，需分析后才能接受完整结果。

长生成强制128个输出token，并要求127–128次实际decode forward。
其报告只表示负载覆盖；ABCE和提前EOS使用独立答案检查。

保存、load_back、fill、ready和free_ack继续使用原来的严格证据。
P native先释放，P alive等待D结束及新增prefix保存完成后归还。
所有相关读写和依赖完成后，真实free_ack才允许后续请求复用槽位。
Graph padding不能替代每步真实常驻和执行请求数。

## C 完整 NPU 验收命令

两机使用相同提交、模型、依赖和配置。
先启动P机命令，再启动D机命令；后续自动协调。
31/32可沿用 `/home/cryang/mempool-min-ticket01-3132.json`。

完整模式自动启用40页prefix、match_prefix和内容验证。
使用新的run-id；旧运行目录不会被覆盖。

P机：

```bash
bash ascend-mempool-test/scripts/run_mempool_min.sh \
  --role prefill --mode full-acceptance \
  --config /home/cryang/mempool-min-ticket01-3132.json \
  --run-id ticket04-002
```

D机：

```bash
bash ascend-mempool-test/scripts/run_mempool_min.sh \
  --role decode --mode full-acceptance \
  --config /home/cryang/mempool-min-ticket01-3132.json \
  --run-id ticket04-002
```

其他部署使用 [完整配置模板](../../ascend-mempool-test/configs/mempool-min-full.example.json)。
填写真实P/D地址、模型路径、网卡和NUMA节点后，两机使用同一份配置。
启动预检核对代码和模型身份、可用容量及端口。
就绪等待BM映射、D Graph、原生握手和全rank注册。
硬件容量必须覆盖固定用例，入口不通过缩短输入绕过容量检查。

容量故障单独重启两侧服务。
它仅限制D rank15对Main报告的HBM页数，物理allocator不变。
日志同时保存真实页数、注入值和需求值。
此阶段要求注册被拒绝、请求返回503、目录账本保持空、D服务非零退出。

超时故障再次使用独立池和服务。
入口暂停自己启动的P进程组，确认D已实际预留资源后等待Main超时。
之后检查停止新操作、拒绝新请求、无提前free、D非零退出和双端清理。
预期故障全部符合合同，完整验收才可通过；任一检查失败，入口返回非零。
参考和正常目标组的日志独立保留。

## D 报告与回传材料

日志根目录为 `/home/cryang/log/mempool-min/ticket04-002/`。
P终端打印 `FULL_ACCEPTANCE`，列出V1–V10状态与报告位置。

| 文件／目录 | 内容 |
|---|---|
| `summary.json`、`prefill/v1-v10.json` | 总结果、逐项结论、独立答案及缺失项目 |
| 两侧`identity.json`、`peer-identity.json` | 版本、工作区差异、模型和部署身份 |
| 两侧`cases.json` | 完整input IDs、题面、模板、采样配置与标准答案 |
| `prefill/reference/`、`prefill/target/` | 完整输出、答案结果、日志和证据；目标另有资源快照 |
| `prefill/reference/stability.json` | 相同输入A/C的冷算稳定性、完整IDs长度与首差 |
| `prefill/fetch/` | P-only、D-only、混合来源适配器结果 |
| 两侧`capacity/`、`timeout/` | 注入证据、账本、故障日志、退出码和清理收据 |
| 各阶段`process-cleanup.json` | 两端已退出本轮拥有的进程组 |

回传根目录summary、P侧v1-v10及失败阶段的原始日志与JSON。
若两机失联，同时回传D侧summary与该阶段日志。
缺rank、缺设备完成或未执行项目不会默认算通过。
强制终止不能当作free_ack；物理NUMA页落点仍单独标为未验证。
旧ticket03合成C的失败记录继续保留。

## E 常驻 demo

完整配置模板默认启用prefix复用，关闭昂贵的内容回读验证。
将填写后的配置保存为 `/home/cryang/mempool-min-full.json`。
常驻模式继续从Main统一接收请求。

P机：

```bash
bash ascend-mempool-test/scripts/run_mempool_min.sh \
  --role prefill --mode serve \
  --config /home/cryang/mempool-min-full.json --run-id ticket04-serve-001
```

D机：

```bash
bash ascend-mempool-test/scripts/run_mempool_min.sh \
  --role decode --mode serve \
  --config /home/cryang/mempool-min-full.json --run-id ticket04-serve-001
```

Main的 `/ready` 表示控制与注册就绪。
`/status` 另行显示请求是否空闲、故障与账本。
业务请求发送到Main的 `/generate`，输入使用对齐的token IDs。
常驻服务启动成功只代表就绪，不替代完整验收。

## F 验证状态

最新修复后完整470项CPU测试通过，耗时44.284秒。
本次两文件lint、format、isort、3.9语法和源码strict mypy通过。
[修复审核](verification/ticket04-tokenizer-20261027/review.md)的两轴均0项发现。

原实现检查记录如下：469项CPU通过，耗时42.440秒。
9个Python文件通过lint、format、isort和3.9语法检查。
7个源码模块通过strict mypy；两轴review均0项未解决。
超长拒绝的独立性和同条件稳定性诊断已按审核修复。

[检查记录](verification/ticket04-cpu-20261027/checks.json)包含源码摘要和实际日志。
[两轴审核](verification/ticket04-cpu-20261027/review.md)保留发现、修复和复核结论。
公开GLM-5.2 tokenizer在本地通过完整输入生成。

用户已回传ticket04-002完整NPU自动验收通过。
参考组冷算A/C各17个输出IDs且完全一致；这不代表全部参考/目标IDs相同。
逐rank明细仍保存在远端，本地归档的是用户终端摘要。
等待用户确认后关闭本票。
