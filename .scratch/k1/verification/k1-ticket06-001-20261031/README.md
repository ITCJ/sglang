# K1-ticket06 NPU001 回传核对

用户回传31侧`prefill/summary.json`全文，包含内嵌acceptance。
顶层、target、restart均为passed，两轮各6请求。
版本为`9b760a1addddec05ce2a01d2f2a554a7abe33f67`，双端身份检查通过。
tracked diff为空，未跟踪源码摘要为空。
自动验收通过；用户随后明确确认验收，ticket06已closed，见[人工确认](acceptance.md)。

## 来源与保存方式

[摘要字段](user-reported-summary-extract.json)来自用户贴出的JSON。
重复rank和轮次按相同指标压缩保存，不标作原始summary文件。
case名称依据当前入口的顺序标注，rid和计数来自回传。
原始`user_accepted=false`与limitations保持原值。

未收到32独立summary、原始registrations、P/D日志或单独acceptance文件。
31内嵌acceptance已经包含D的全部请求回执及双端清理结果。
同一acceptance无需再次回传；本次没有访问硬件或重跑测试。

## 环境与入口

31 / 10.120.72.31为P，32 / 10.120.72.32为D；各TP16，bond4。
模型为`/data_lib/data/models/GLM-5.2-w4a8`。
page128、P slot8192、D slot128、prefix40页；P/D共享均开启。
D使用features分工，每个rank写288维BF16，即每token每层576字节。
P按contiguous整页分写，prefix按round_robin分读。
torch2.10.0+cpu、torch-npu2.10.0、sgl-kernel-npu2026.9.0、MemFabric1.1.4。

使用[既有双机入口](../../../../ascend-mempool-test/K1_TICKET06.md)，
mode为`d-sharing-acceptance`，run-id为`k1-ticket06-001`。
配置为`/home/cryang/mempool-min-ticket01-3132.json`。
日志根为`/home/cryang/log/mempool-min/k1-ticket06-001/`。

## 结果核对

| 项目 | 回传结果及核对 |
|---|---|
| 两轮功能 | target/restart各6请求；ABCE、长生成、提前EOS通过 |
| P/D共享 | 主KV各8份；D每NUMA一份，BM分配1GiB，合计8GiB |
| D有效payload | 每份184025088字节，含78层、16槽、128token、576维BF16 |
| P分配 | 每NUMA15GiB，合计120GiB；验收CPU快照预算另计32GiB |
| 写入 | 17步每rank763776字节；128步5750784字节；7步314496字节 |
| 逐步整行 | 每层检查上一token；17步1248行，128步9906行，7步468行 |
| 回执覆盖 | 每请求rank0–15；两轮共192份回执，全部verified=true |
| 生命周期 | 每轮ready_events=14；全部读者归零，账本、释放复用及重启通过 |
| 故障 | SR heartbeat timeout；16个D rank保留native资源，全部额度保留，无自动free |
| 清理 | 故障worker退出137属于注入后的清理；双端自身进程组清理通过 |

[算术核对](arithmetic-check.json)检查写入为steps×78×576，哨兵为(steps−1)×78。
两轮共491712次逐层整行比较，D写入合计291852288字节。
这些是内容验证与字节计数，不代表吞吐或性能结果。

B的current_rows为1325，其余17步用例为1326。
该字段只数top-k实际选择且走miss路径的当前token供给，未要求每层每步都选中当前token。
写入字节和previous-row校验仍完整，因此少1本身不构成漏写证据。

## Graph证据层级

验收版本的`verify_mempool_shared.check_registration`要求D每rank的
Graph负测试与正测试通过；否则sharing.registration不能返回passed。
`verify_mempool_decode.check_completion`还要求真实请求含Graph replay。
因此本次汇总支持这两项检查已通过。
原始`shared_probe.decode_order.cases`和replay次数没有嵌入本次summary，未作逐值复核。

## 保留边界

- 物理NUMA页驻留仍为`physical_placement_verified=false`；分配量不替代驻留证据。
- 当前features策略已在NPU验证；layers策略和P私有+D共享组合仅有CPU证据。
- V3使用独立题目答案，未重跑参考模型或冷A/C稳定性；V8重放实际注册做CPU容量拒绝。
- 性能未测，退出沿用SGLang父进程清理；未独立验证主动BM退出及退出后物理页数量。
- prefix验证使用计算源快照和native HBM；P alive/prefix/fill DRAM未额外读回。

原limitations第一条沿用旧prefix文案，笼统写了alive DRAM不读回。
应限于P侧alive与prefix路径：本票D alive已经通过逐步哨兵读回。
本记录明确该范围，不改写用户回传的原始字段。

## 本轮改动

只归档用户结果、核对计数并更新票据与进度说明。
功能源码、测试入口净改动为0；没有新增CPU/NPU测试执行。
随后用户确认本次精简范围并授权清理工程插桩；06已关闭，09仅剩08依赖未满足。
