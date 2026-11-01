# 11: K1-02c admit 与 fill 的池间搬运路径与分片执行器（行为不变）

**What to build:** 在票 08 定稿的 8 / 8 / 1 / 1 / 1 布局上，把 op1 fill 与 op3 admit 这两条 DRAM 到 DRAM 的池间搬运（主 KV 与 Index K；所有主存池间的连续块）统一改成按搬运清单（每层每页一段，主 KV 78 段加 Index K 21 段，两类同一张清单）走内存池库的批量异步接口；每个 SR 分片一个独立执行器线程与队列（绑本 NUMA、整张清单一次提交、拷贝期间释放 GIL），fill 现有的独立队列并入它；协调者在有序调度 tick 里下发任务、保护源、分配目标页，汇总完成、收齐该 op 全部参与分片的“目标可读”再向 main router ack；worker 只报源写完。调度线程与 SR 控制循环不等搬运。07、08 已把选页、地址解析、拷贝提交分开，本票只换底层的提交与完成回报。不碰 op2、P 的 write_through、D 的逐步写回、P 到 D 的 Index K 传输（模型工作内存与池之间的写回、恢复及动态索引回填继续独立处理）。副本数（8 / 8 / 1 / 1 / 1）、页归属、分片分工、串行、ack 语义、保护 / 发布 / 释放语义、main router 消息都不变。细节见 k1-tickets.md 第 15 节。

**Blocked by:** [08（K1-05b prefix 单份：8 段、numa interleave、按段读者、并行 admit、直接 fill）](08-k1-05b-prefix-single-copy-numa-interleave.md)

**Status:** ready-for-agent

**State:** open

- [ ] A / B / C / E 与 V1–V10 全过；V2 加 op1 后 alive、op3 后 prefix 与源逐页一致（主 KV 与 Index K 都比）
- [ ] op1 / op3 期间计算侧拷贝算子零调用；全部分片的清单段数之和 = 页数 × 99，字节数之和 = 页数 × 每页字节数
- [ ] 搬运期间调度 tick 间隔与空闲时一致，SR 仍处理消息
- [ ] 每个 op 的提交 < 目标可读 < ack < publish < 释放源 < 归还额度（结构化日志）
- [ ] 负测试：注入延迟后 ack、publish、释放同步推迟不提前；fail-stop 下执行器线程与共享会话关闭干净
- [ ] 票 08 的证据项（读字节计数、admit 每页单写者、写全部落本段）在新路径上复核不变
- [ ] 基线表与票 08 的表并列（不重跑 08），最多 2 轮、注明 n，重点 op1、op3（提交到完成两段）、T_tail、T_free
- [ ] 单 commit；Standards / Spec 两轴评审

## Comments

### 2026-11-01：导入同步稿

2026-10-31加票并改序到07/08之后；从08验收后的状态开工，阻塞09。尚未实现。
