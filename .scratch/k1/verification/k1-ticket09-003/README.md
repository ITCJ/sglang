# Ticket09 NPU003及四组合核

2026-11-03用户回传31/P、32/D完整终端摘要。两侧003矩阵JSON按原值录入同目录。
指导版本6ce84c770b；回传未含实际git SHA，不把指导版本当作已核验部署版本。

| B | S_D | D每槽页数 | 批次 | 两侧结果 |
|---|---|---|---|---|
| 128 | 128 | 1 | 001 | passed / exit_code=0 |
| 128 | 512 | 4 | 002 | passed / exit_code=0 |
| 64 | 128 | 2 | 003 | passed / exit_code=0 |
| 64 | 512 | 8 | 003 | passed / exit_code=0 |

003的两组P FULL_ACCEPTANCE均报告V1–V10、long_generation、timeout_fail_stop、paired_cleanup通过。
冷reference A/C稳定性通过，均17 tokens且IDs相等。D矩阵两组均passed。
003仅覆盖剩余两组，不能单独称为一次四组全跑；四组合核依据001/002/003。
001/002后修复限验收端口和证据输出，未改变KV数据算法，保留前两组已通过结论。
原失败记录不删除。此次未执行任何新CPU或NPU测试，不要求重跑。

## 证据与关闭边界

本地取得的是用户终端及矩阵摘要，不是两机完整原始目录。
逐rank K1证据、prefix-content文件、拓扑、平台版本及身份哈希仍在远端，未逐项独立复核。
通过入口会核验D哨兵、末行、Graph顺序、实际页大小边界与共享搬运；摘要通过是入口结果，不能替代原始数值。
物理NUMA驻留、性能、真实SDK在途故障/超时、P目标全量独立读回的既有边界保持不变。
日志根目录分别为 /home/cryang/log/mempool-min/k1-ticket09-001/、002/、003/（后两者同前缀）。
2026-11-03用户明确回复“关闭”，接受四组合核结果；ticket09已closed。
用户确认单独记录在acceptance.json；原始自动报告中的user_accepted=false保持原值。
