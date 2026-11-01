# K1-07 用户验收确认（2026-11-01）

用户确认：“这确实可能会引起量化模型的误判。但这不属于我们的错误。我觉得可以验收了。”
按用户接受的功能范围关闭07，解除08的07依赖。不是严格九例脚本或完整V1–V10全通过。

## 用户实机反馈

- 环境：31/32，配置 `/home/cryang/mempool-min-ticket01-3132.json`。
- 实现版本420270d493，入口index-sharing-acceptance，run-id=k1-ticket07-002。
  第1轮9请求执行完成，原6例及index-cold/index-full-hit通过。
  index-update命中4096符合预期，但输出代码围栏和pencils=12（预期17），26 tokens正常结束。
  精度断言失败，未进入本轮故障注入及第2轮restart，不将这些检查记为通过。
- 用户补跑现有check_reuse离线校验通过：16 ranks、78层main、21层Index K；
  源快照与正常恢复内容一致，覆盖prefix/suffix、非连续映射和native free/fill顺序。
- 诊断版本fde9a38128，入口index-cold-diagnosis，run-id=k1-ticket07-cold-001。
  同共享布局，原样重放失败输入，关闭前缀匹配；实际hit_len=0。
  冷、热文本及全部26个输出token完全相同，均为pencils=12及代码围栏。
  严格答案检查仍失败，终端确认服务已清理；没有新增BM读回。
- 原始日志在 `/home/cryang/log/mempool-min/k1-ticket07-002/` 和
  `/home/cryang/log/mempool-min/k1-ticket07-cold-001/` 的prefill/decode目录。
  冷对照用户回传保存在 [cold-user-report.json](cold-user-report.json)。
  运行命令见 [入口说明](../../../../ascend-mempool-test/K1_TICKET07.md)。

## 接受边界

题面“original ... ignoring any other updates”可能被理解为从最初17支重新计数，
得到17−7+2=12，而预期是在上一轮最终22支上计数得到17。
用户接受此模型/题面行为不作为本票系统实现缺陷；没有证明具体量化误差机制。
冷、热一致说明本例错误不依赖前缀命中；不把它推广为对所有共享路径的完整证明。
不修改提示词、预期答案或严格JSON解析来制造通过，也不新增BM读取。

NUMA物理驻留、性能、双写者配置以及完整V1–V10未由本次证据全部验证。
002的故障注入和第2轮restart未执行；注册/写者字节计数未单独收集成完整通过报告。
这些边界保留在记录中；最终组合验收由09承接。08现在可实施，本轮没有开始08。
