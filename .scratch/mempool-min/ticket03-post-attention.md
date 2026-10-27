---
title: Ticket03-007：修复配置摘要后重测三个边界
subtitle: 同一轮检查三个边界、逻辑行映射和权重副本
template: doc
theme: blueprint
lang: zh
date: 2026-10-27
status: 配置摘要修复与两轴审核通过；等待007 NPU重测
---

005 在取消强制非连续页和延迟 fill 后仍复现 C 的 output[15] 分叉。
006在首个A的_layer0_pre配置摘要中触发8MiB限制，没有得到算子对比结果。
007修复该诊断工具问题，继续重放004保存的原始ABC与同一组控制实验。
目标是区分通信、残差/归一化/MLP数值，以及逻辑行映射/副本问题。
ticket03仍open，04未解锁；诊断不能代替严格NPU验收。

## A 同一轮记录什么

| 边界 | 记录内容 | 作用 |
|---|---|---|
| 归约前 | 各rank局部o_proj全部尾页行 | 确认同一逻辑token的各个加数是否一致 |
| 归约后 | reduce-scatter输出、尚未相加的residual | 区分通信变化与后续计算 |
| 归一化后 | normalized、residual_out | 区分残差求和与归一化输出变化 |
| MLP内和之后 | 输入、gate_up输出、down_proj输入、MLP输出 | 缩小线性/激活/量化相关差异范围 |
| 下一层入口 | hidden、residual、归一化输入/输出、聚合后的attention输入 | 核对跨层传递和gather后的逻辑行 |

保存最后128个逻辑token的全部列，汇总全部逐行差异；终端重点打印3968、4061、4090。
三个token在A都归rank15；在C分别归rank0、11、15。
快照在原地操作之前复制；模型使用原始输出，重放结果不回填模型。

## B 控制实验与判断边界

| 控制 | 固定内容 | 改变内容 |
|---|---|---|
| reduce.same_shape | A或C真实归约前完整输入 | 重复调用相同通信接口，检查本次重放是否一致 |
| reduce.tail | A各rank的尾128行加数 | 4096变128；token归属随分片改变 |
| reduce.rotated | A各rank完整输入、消息大小 | 把尾128行移到开头，改变归属与块内位置 |
| norm.full/repeat/chunks | A rank15的同一份归约输出和residual | 每rank独立以256行、重复256行、逐8行执行 |
| mlp.full/repeat/chunks | A rank15的同一份真实归一化输出 | 每rank独立以256行、重复256行、逐8行执行 |

各rank通过本轮P本地目录读取同一份canonical输入，核对请求身份与完整摘要。
MLP控制始终使用同一份真实A归一化结果，因此不会被归约或归一化差异污染。
同时比较固定形状下不同rank结果，并对相关norm、MLP权重及固定scale做全内容哈希。
记录量化方法、实现类别和可读取的标量配置；本轮不捕获量化kernel内部临时scale。

归约控制可以检出消息形状或位置相关变化，不能仅凭结果推断具体通信算法或求和顺序。
使用CPU FP32求和作数值参照，但不将其当成精确实数答案，也不据此设置输出容差。
同输入重放不一致或不能复现原调用时，报告明确标记，不能据此排除该环节。

行映射检查包括：精确二进制token标记走同形状归约、逻辑位置覆盖/归属、残差切片、
相邻hidden/residual边界逐位传递和下一层gather结果。
当前入口只支持实际的TP16、attention DP1、首层dense MLP逐rank复制路径。
遇到不同通信路径、维度或TP切分MLP将失败，避免在不支持的配置上给出完整结论。

## C 每台机器一条命令

两机先更新到交付提交，在 `/home/cryang/sglang` 执行。
两机保留004的identity；P保留其cases、reference/target responses。
本轮使用新run-id，保持原sampling parameters、ignore_eos=true、固定128输出。

31，P：

```bash
bash ascend-mempool-test/scripts/run_mempool_min.sh --role prefill --mode reuse-diagnostic --config /home/cryang/mempool-min-ticket01-3132.json --run-id ticket03-007-post-attention --replay-run ticket03-004 --diagnose-post-attention
```

32，D：

```bash
bash ascend-mempool-test/scripts/run_mempool_min.sh --role decode --mode reuse-diagnostic --config /home/cryang/mempool-min-ticket01-3132.json --run-id ticket03-007-post-attention --replay-run ticket03-004 --diagnose-post-attention
```

入口自动完成身份核对、预检、参考/目标启动、请求、检查、报告和双方自有进程清理。
普通恢复的两个强制边界控制仍关闭，内容检查和原首层探针保持启用。
新开关两机必须相同，但只有P的target模型安装新探针；reference和D不安装。

## D 报告和开销

终端回传 REUSE_COMPARISON、REUSE_POST、三条REUSE_POST_TOKEN，以及任何错误。

原REUSE_RANK/REUSE_OPERATORS继续保留，用于核对007是否仍复现005的指纹。

日志根目录为 `/home/cryang/log/mempool-min/ticket03-007-post-attention/`。
P的target/post-attention.json保存完整逐行比较，target/post-attention/保存每rank的A/C快照。
两机的diagnostic-comparison.json均含紧凑结论；P的summary.json保存收集状态。

complete只表示记录完整；checks_ok还要求映射、副本、有限值、重复与重放可信性检查通过。
即使这些检查通过，完整output IDs有差异仍会非零退出。

每rank新增CPU快照上限256MiB、暂存预算64MiB，运行器在P内存预检中计入320MiB。
原归约输入的额外设备快照限制64MiB；重放还需要同量级临时输入及原算子自己的工作区。
快照、哈希和额外计算会增加耗时并改变执行时序，本轮不用于性能测量。
若原输出或已有算子指纹明显改变，须先评估诊断本身的影响，不能直接套用005结论。

## E 功能源码阅读顺序

| 顺序 | 文件 | 作用 |
|---|---|---|
| 1 | [debug_post_attention.py](../../python/sglang/srt/hardware_backend/npu/mempool/debug_post_attention.py) | 安装边界hooks、保存逻辑位置/副本摘要并执行同输入控制 |
| 2 | [mempool_router_transport.py](../../python/sglang/srt/disaggregation/ascend/mempool_router_transport.py) | 仅在P的显式诊断环境下绑定已有模型与prefix探针 |
| 3 | [summarize_mempool_post_attention.py](../../ascend-mempool-test/scripts/summarize_mempool_post_attention.py) | 跨rank逻辑行对齐、控制比较、映射及副本检查和终端摘要 |
| 4 | [run_mempool_min.py](../../ascend-mempool-test/scripts/run_mempool_min.py) | 新开关、身份/预算核对、自动采集和严格失败判定 |

前两项功能变更均在npu/ascend目录。
后两项是现有独立硬件测试工具的编排和离线分析，必须在P/D服务之外协调和读取各rank文件；
模型侧hook无法承担双机进程生命周期和清理后的汇总，所以放在ascend-mempool-test/scripts。
没有修改通用模型、通信或归一化的生产实现。

## F 验证状态

006工具交付前的验证：

482项CPU回归通过，4个功能模块strict mypy通过；新增测试用真实torch模块和线程化CPU集合操作，
验证别名写入前快照、跨rank行对齐、独立形状差异、错误映射、权重scale变化和缺失记录失败。

这是诊断工具验证，不是NPU数值复现；006随后在新探针内失败，原问题仍待定位。
Standards/Spec最终均0项未解决；量化scheme宿主遗漏已修复并用反例复验。
原检查记录见[006工具验证目录](verification/ticket03-post-attention-20261027/checks.json)。

007修复范围：配置Mapping改为分段规范JSON哈希，完整覆盖内容，记录编码总字节数。
共享配置仅在一次manifest中复用摘要，A/C仍重新哈希；长字符串先切片再转义。
同一错误的各rank合并显示，完整原始错误留在post-attention.json。

487项完整CPU回归、15项定向检查、2功能模块strict mypy和3文件静态/3.9语法通过。
Standards/Spec均0项未解决；007硬件尚未运行。
[修复验证记录](verification/ticket03-config-digest-fix-20261027/checks.json)保留原异常复现及红绿结果。

本次功能源码阅读顺序为debug_post_attention.py（配置完整摘要与暂存）、
summarize_mempool_post_attention.py（错误汇总）；上面的安装与运行器功能保持此前实现。
离线错误汇总在独立测试工具中读取服务退出后的文件，不能由模型内hook承担。
