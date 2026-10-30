# K1-ticket03 NPU006：双策略自动验收通过

2026-10-30，用户回传31侧prefill与32侧decode的完整summary.json，均为passed。
对应代码为`0337b9b9adb48baf160495594ec9b28908e890ba`；两端身份检查通过，
tracked diff为空、untracked为空，模型及依赖摘要一致。
用户随后明确确认验收并授权清理，ticket已closed；见[人工确认](acceptance.md)。
原始summary的user_accepted=false保持不变，自动结果与人工确认分别记录。

## 证据来源与执行环境

- [prefill.summary.json](prefill.summary.json)与[decode.summary.json](decode.summary.json)：
  按用户粘贴的完整JSON对象保存，保留所有字段和值及`user_accepted: false`。
- [split-read-timings.md](split-read-timings.md)：用户回传的P侧计时表。
- 原始运行目录：`/home/cryang/log/mempool-min/k1-ticket03-006/`。
  本机未连接NPU主机，也未收取逐rank原始日志；运行判据结论来自用户回传的runner汇总。
- 31/32各TP16，模型`/data_lib/data/models/GLM-5.2-w4a8`，接口bond4。
  page128，prompt capacity8192、decode capacity128，prefix40页，V2与profile开启。
- torch2.10.0+cpu、torch-npu2.10.0、sgl-kernel-npu2026.9.0、memfabric-hybrid1.1.4。
  完整配置和模型manifest保存在两份summary中。

两机各执行一次以下入口，角色分别为prefill和decode；命令复现见
[运行说明](../../../../ascend-mempool-test/K1_TICKET03.md)。

```bash
bash ascend-mempool-test/scripts/run_mempool_min.sh \
  --role prefill --mode split-read-acceptance \
  --config /home/cryang/mempool-min-ticket01-3132.json \
  --run-id k1-ticket03-006
```

006已使用；本记录不要求再运行该命令。

## 功能结果

| 检查 | 用户回传结果 |
|---|---|
| P/D身份与总体报告 | 两端passed，differences为空 |
| 两轮服务 | target轮转、restart连续分块，均passed |
| 内容oracle | 两策略的gather槽位及原生HBM与独立计算源快照精确一致 |
| 验证BM读取 | 两策略均bm_readback=false；报告明确没有额外BM验收读取 |
| B命中 | 4096 token，390,070,272 B，两策略一致 |
| C命中 | 3968 token，377,880,576 B，两策略一致 |
| 完成门控 | 报告确认ack晚于所有rank的设备完成回执 |
| 账本 | 每轮6请求、14等价事件，读者计数归零 |
| 策略输出 | A/B/C/E、128-token负载、提前EOS共6项output IDs完全一致 |
| fail-stop | SR heartbeat timeout后16个rank保留native与全部额度，无自动free |
| 清理与重启 | 故障进程清理、第二轮重启、双端owned进程组清理均passed |

主KV78层、每token576元素，Index K21层、每token128元素，元素2字节；
每token数据量为95,232 B，乘4096/3968恰好得到上述生产读取量。
A/B/C/E的独立标准答案和提前EOS的sum=5均通过；128-token用例只接受负载覆盖。
报告同时保留Graph、source fetch、slot reuse与ready/free检查通过的汇总结论。

`timeout_fail_stop.worker_exit=137`是故障注入后的预期进程清理结果。
它与004/005在服务启动阶段意外返回137的失败性质不同。
006走完两轮；这证明本轮启动成功，不将005短暂端口占用推定为已永久修复。

## 计时解释

单位ms；每个策略、每个命中长度只有1次请求。

| 策略 | 命中token | op2 | H2D | all_gather | scatter | control余量 |
|---|---:|---:|---:|---:|---:|---:|
| round_robin | 4096 | 7691.187 | 0.971 | 241.492 | 34.749 | 7413.975 |
| round_robin | 3968 | 4582.660 | 0.971 | 263.224 | 36.657 | 4281.808 |
| contiguous | 4096 | 1420.048 | 1.041 | 71.364 | 34.006 | 1313.638 |
| contiguous | 3968 | 1372.757 | 0.952 | 109.261 | 32.665 | 1229.879 |

控制余量约占op2的90%–96%，包含建表、排队、HBM→CPU验证、首次JIT及回执等待。
当前数据把大部分墙钟耗时定位在设备三段之外，但没有分拆这些因素各自的贡献。
不能把该余量当作纯生产控制开销，也不能断定全部变差均由CPU校验造成。

H2D约1ms，scatter约33–37ms，all_gather约71–263ms。
all_gather还可能包含参与rank到达时间差，不能据此计算纯网络带宽。
round_robin在首轮、contiguous在重启轮，样本少且轮次不同，暂不能归因策略性能差异。
T_lb_rank的n=16是同一次请求的16个rank，不是16次独立性能重复。

历史baseline01-002的op2中位数约98–108ms，仅作量级参照。
新旧验证开关、恢复路径与样本数不同；本轮无性能门槛，也不作性能验收结论。
具体开销归因仍未验证；本次不增加模型加载或服务重跑。

## 保留的验收边界

1. 根据用户选择，整个prefix验证链不额外读BM；alive/prefix/fill DRAM内容未直接核对。
   本轮证明被生产路径恢复到gather/native HBM的数据与计算源相符。
2. V3沿用独立fixture答案，没有新开正式服务对照或冷A/C稳定性重复。
3. V8用实际注册在CPU回放容量拒绝，没有独立worker失败运行。
4. 物理NUMA落点与生产性能未验收。
5. 退出沿用SGLang默认父进程清理；未接受逐rank BM leave/uninitialize或退出后物理DRAM页数。

实现版本此前的494项CPU、静态检查及双轴review见
[005验证改造](../k1-ticket03-005-20261030/README.md)。
本次只归档证据和更新进度，不改功能源码，不重复CPU或NPU测试。
用户确认后已完成临时计时/工程验收探针清理；清理后检查与006硬件结果分别记录，
见[交付说明](../../delivery-ticket03.md)。
