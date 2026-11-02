# Ticket09 NPU002用户回传

用户回传31/32终端摘要；指导版本eeadd76a8c，摘要未含实际git SHA。

- b128-sd512：两侧matrix条目passed、exit_code=0；P V1–V10、17-token冷A/C稳定性、长生成、超时与双端清理全部passed。
- b64-sd128：fetch、reference完成后进入target；P报JSONDecodeError，line 1 column 4965 (char 4964)，D报peer failed同因。
- 该组V3与reference稳定性passed，其余未完成；未进入capacity/timeout。不能把证据解析失败推断为KV功能失败。
- b64-sd512：not-run。

结合001，128/128与128/512已由用户终端摘要报告通过；64两组仍未验收，09保持open。
当前未取得错误JSON原文、文件/行号与堆栈。读取端有多处json.loads，不能仅凭字符位置定位。
暂不重跑模型；先离线检查已有target日志与JSON，确定是多进程日志交错、截断还是其它来源。
不放宽JSON检查或跳过坏记录；不将缺失证据算通过。

## 用户离线取证：确认长JSON交错

launcher.log与prefill.log内容相同，各两条损坏记录：
- 行4338，TP3，payload长度18177；char4964在页35字典后插入完整TP5日志头及另一份prefix_content。
- 行4340，TP13，payload长度33633；char12288在页77摘要字符串中插入TP0日志头及另一份prefix_content。
- 两条最终尾部各自为TP5/TP0的retained记录，rid=k1-ticket09-002-b64-sd128-4。

这确认并发rank输出的长JSON被交错，而非合法JSON内容数值不一致。
本地修复：target verify_prefix设置证据目录；prefix_content写独立内容寻址JSON文件，
launcher只输出prefix_content_file短引用。Reports.read仍按原日志行号读取全部内容。
旧inline日志仍支持；缺文件、坏JSON、重复记录和内容校验失败均不忽略。

新增长记录四rank写出/读入回归，包含80页摘要及4096个indices，短marker <512字节。
新测试先红后绿；连同prefix与证据检查共11项通过；8模块Linux目标strict mypy和ruff通过。
Standards/Spec复核均0阻断。真实NPU修复待用户运行；003从b64-sd128继续。
只改变验收证据输出，两个128组合继续保留既有通过结论。
