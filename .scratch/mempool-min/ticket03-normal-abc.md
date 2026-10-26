---
title: Ticket03-005：原始 ABC 与普通恢复路径
subtitle: 固定请求和检查，仅改变两项边界控制
template: doc
theme: blueprint
lang: zh
date: 2026-10-27
status: 诊断对照入口已实现；等待用户运行 NPU
---

本轮重放 ticket03-004 保存的原始 ABC，请求不重新分词。
默认关闭强制非连续页、延迟 fill，使用正常恢复分配与调度。
内容校验与首层诊断探针保持开启，严格比较每个用例的完整 output IDs。
即使输出分叉，也先收齐 ABC 内容、命中和占用证据，再清理两机自己的进程并非零退出。

这是诊断对照，不代替完整验收。ticket03 保持 open，04 未解锁。
没有改动模型计算、既有探针或输出容差；本轮不注入超时故障。

## A 固定条件与独立控制

| 项目 | 本轮要求 |
|---|---|
| 请求 | 从旧 cases.json 读取，核对旧 reference/target responses 中的 cases 完全一致 |
| 原始顺序 | A=4096、B=5120、C=4096；B 的前4096与A相同，C与A相同 |
| 生成参数 | 完整保留 sampling_params，greedy、ignore_eos=true、固定128输出 |
| thinking | 原始用例已经关闭；重放保存的 IDs，不重新应用模板 |
| 配置与部署 | P/D 当前代码一致；与旧运行比较全部配置，只有两个新控制允许变化 |
| 模型与依赖 | 比较模型配置/量化/分词器 manifest 和依赖版本；权重沿用原路径，未对全部权重重新哈希 |
| 内容与探针 | verify_prefix=true，原首层探针开启；恢复、fill、suffix、alive、保留内容均检查 |
| 命中 | 0 / 4096 / 3968 |
| prefix 页占用 | 32 / 40 / 40；每个 TP rank 私有40页 |
| 输出 | 本轮重新运行独立冷算参考，然后与目标ABC逐个完整比较 |
| 进程 | 自动预检、fetch、启动、就绪、请求、检查、报告和自有进程清理 |

verify_prefix 现在只控制内容检查。
force_noncontiguous_restore 控制额外保留一页以强制非连续分配。
delay_prefix_fill 控制等 native_freed 发布后才提交 fill。
配置默认关闭两项；reuse-acceptance 为完整边界验收强制开启两项。

## B 双机命令

两机更新到交付提交，在 `/home/cryang/sglang` 执行各自一条命令。
沿用 `/home/cryang/mempool-min-ticket01-3132.json`；入口保存 effective-config.json。
两机必须保留各自 ticket03-004 的 identity.json，P 还需原 cases.json 和两组 responses.json。
旧日志只读；新 run-id 必须未使用，不自动删除或覆盖原结果。

31，P：

```bash
bash ascend-mempool-test/scripts/run_mempool_min.sh --role prefill --mode reuse-diagnostic --config /home/cryang/mempool-min-ticket01-3132.json --run-id ticket03-005-normal-abc --replay-run ticket03-004
```

32，D：

```bash
bash ascend-mempool-test/scripts/run_mempool_min.sh --role decode --mode reuse-diagnostic --config /home/cryang/mempool-min-ticket01-3132.json --run-id ticket03-005-normal-abc --replay-run ticket03-004
```

诊断模式自动启用原 --diagnose-reuse 探针，无需另加参数。
--describe 可离线显示有效配置，既不启动服务也不读取远端旧日志。
实际运行时源请求、配置、模型元数据或版本不一致会在启动模型前失败。

## C 回传与下一轮

日志保存到 `/home/cryang/log/mempool-min/ticket03-005-normal-abc/`。
P 的 summary.json 保留总结果，target/diagnostic-comparison.json 在两机均保存判定。
replay-source.json 记录旧运行身份、源文件 SHA256 和实际请求 payload SHA256。

回传 REUSE_COMPARISON、REUSE_DIAGNOSTIC、REUSE_OPERATORS 和错误信息即可。
target/output-comparison.json 记录每个用例是否相等、输出长度及首差位置。
target/evidence.json 保存命中、占用、内容、Graph 和生命周期结果。
target/reuse-diagnostic.json 保存原探针的完整结果，responses.json/snapshots.json 保留原始响应和状态。

输出比较或探针缺失、身份/元数据失败、非有限值均不能得到 passed。
内容/生命周期错误仍立即失败，已收集日志保留；输出差异会等 ABC 证据采集完毕后判失败。
complete=true 仍只表示探针记录完整；诊断 passed 也不关闭票。

若本轮通过，再以新 run-id 分别增加一个参数，另一项仍关闭。
下表是后续单因素对照入口，尚未运行。

| 对照 | 参数 | 另一项 |
|---|---|---|
| 只强制非连续页 | --force-noncontiguous-restore | fill 正常提交 |
| 只延迟 fill | --delay-prefix-fill | 原生正常分配 |
| 两项边界同时开启 | 两个参数都加 | 仍标为诊断；不替代 reuse-acceptance |

普通模式不要求分配结果一定连续，也不要求 fill 必须早于 native free。
它只取消强制控制；报告同时记录实际是否非连续、是否 native free 先于 fill。
当前报告两项观测布尔值表示所有 B/C rank 是否均满足，并非控制开关值。

## D 功能源码阅读顺序

| 顺序 | 文件 | 作用 |
|---|---|---|
| 1 | [mempool_router_protocol.py](../../python/sglang/srt/disaggregation/ascend/mempool_router_protocol.py) | 两个独立布尔配置，要求 prefix matching |
| 2 | [mempool_prefill_restore.py](../../python/sglang/srt/disaggregation/ascend/mempool_prefill_restore.py) | 仅按分配控制决定是否占用间隔页 |
| 3 | [mempool_subrouter.py](../../python/sglang/srt/disaggregation/ascend/mempool_subrouter.py) | 仅按调度控制决定 fill 是否等待 native free |
| 4 | [mempool_router_transport.py](../../python/sglang/srt/disaggregation/ascend/mempool_router_transport.py) | 安装独立配置并注册每 rank 控制值 |
| 5 | [mempool_router.py](../../python/sglang/srt/disaggregation/ascend/mempool_router.py) | 拒绝各 rank 不一致的控制配置 |
| 6 | [replay_mempool_reuse.py](../../ascend-mempool-test/scripts/replay_mempool_reuse.py) | 读取并验证保存的请求/身份，严格输出比较 |
| 7 | [verify_mempool_reuse.py](../../ascend-mempool-test/scripts/verify_mempool_reuse.py) | 按独立控制核对边界，保留全部内容和占用要求 |
| 8 | [verify_mempool_prefix.py](../../ascend-mempool-test/scripts/verify_mempool_prefix.py) | 普通恢复允许 fill/alive 与 native free 的实际先后关系 |
| 9 | [run_mempool_min.py](../../ascend-mempool-test/scripts/run_mempool_min.py) | 配对运行、原请求分块传输、证据采集、清理和最终判定 |

前五个是 Ascend 服务源码。后四个在既有硬件测试目录，需要在服务外读取旧日志、
协调两机、管理进程及核验报告；模型内部 Ascend/NPU hook 无法替代这些职责。
没有新增通用模型路径修改。

## E 验证边界

本机检查与审核记录保存在 [验证目录](verification/ticket03-normal-abc-20261027/checks.json)。
472 项 CPU 回归通过，9 模块 strict mypy、16 文件静态检查与Python3.9语法通过。
Standards / Spec 两轴审核均0项未解决。

CPU 测试覆盖两机协调与自有子进程清理、四种控制组合、真实 Main 历史和内容证据的负向校验。
外部模型/设备调用在 CPU 测试中替换；它不证明 NPU 数值分叉已修复。
本轮 NPU 尚未执行，由用户运行上述命令。

用户已确认独立 ticket03-ac-002 简单铅笔题 A/C 均正确、命中0/3968、完整IDs一致。
用户回传记录单独提交为0745deb337，未下载远端原始结果。
这项语义 smoke 不改变原始 ABC 的严格输出要求。
