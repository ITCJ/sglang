# 10: X 旁路定位

**What to build:** 定位票 01 基线留下的三件事：A 比 E 多出的约 5.2 s（其中 gen 多约 4 s，落在 handoff 附近）归属于哪一段、是否可预热；C 的 fill 第 5 轮 781 ms 长尾是执行慢还是回执晚；每请求记 128 次 forward 而期望 127 是多算一步还是计数口径。结论回填各票的基线表说明；若发现是 bug 另开票。不阻塞任何票。细节见 k1-tickets.md 第 14 节。

**Blocked by:** None (can start immediately; 不阻塞任何票)

**Status:** ready-for-agent

**State:** open

- [ ] A 与 E 在 P 的 send 完成、D 的 handoff 到达、D 三条件齐、D 第 1 步开始四处打点对比，给出多出时间的归属和是否可预热
- [ ] C 的 fill 按分片分开记录拷贝开始 / 结束与 ack 到达，给出长尾归属
- [ ] 停止边界与步数口径核对，给出 128 对 127 的结论
- [ ] 结论写进各票基线表的说明；若是 bug 另开票并注明

## Comments

### 2026-10-29：同步 syncfile 定稿

按用户要求导入已定稿的票据。目标、验收项及依赖沿用原文；
本地补充 State 与依赖链接。尚未开始本票实现或 NPU 验收。
