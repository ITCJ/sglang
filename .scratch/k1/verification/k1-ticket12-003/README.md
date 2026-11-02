# Ticket12 NPU003反馈：实际端口短暂不可复用

来源：用户粘贴31终端、32 summary.json及事后/proc检查；完整原始目录未回传。
版本bf4a49328177be1a8a483786a5b655efc82c7fba，tracked diff为空，identity_check通过。
page-first-copy/fetch通过；首轮SR heartbeat timeout注入及双端进程组清理记录通过，worker_exit=137。
随后D的26082报EADDRINUSE；26082=25686+33*12，是rank12的实际BM端点，不能归为002的宽范围误扫。
用户事后检查/proc/net/tcp及tcp6均无记录，无可见持有PID，说明失败时状态已消失，不能证明当时为TIME_WAIT。

本机Mac上用真实TCP连接主动关闭，分别让原监听socket开启/关闭SO_REUSEADDR，两次现有released_port均通过，未复现Linux错误。
本地Docker daemon不可用，没有访问NPU主机。下一步请用户在32运行纯TCP小实验，比较原socket复用选项，并记录关闭后的/proc状态；不重启模型。
未添加重试等待、跳过端口或放宽验收；没有修改生产源码。003仍failed，restart未验收，12仍open、11阻塞。
002的memfabric退出析构异常仍未解决；003未提供launcher日志，不据summary推断此次是否发生同样崩溃。

## 用户回传Linux纯TCP实验

在32容器执行，与模型/NPU无关：

```text
original SO_REUSEADDR=False, address=('127.0.0.1', 4307)
TCP state=06 (TIME_WAIT)
released_port: OSError(98, 'service port 127.0.0.1:4307 not released: [Errno 98] Address already in use')
original SO_REUSEADDR=True, address=('127.0.0.1', 25731)
TCP state=06 (TIME_WAIT)
released_port: passed
```

证明现有检查器的“允许TIME_WAIT”描述在Linux上并非普遍成立；不能只给新probe设SO_REUSEADDR就假定旧连接可复用。
此实验没有复现003的原始socket归属和选项，因此003根因仍不能直接定为TIME_WAIT。
已向用户提出仅针对TIME_WAIT的有界等待方案，沿用operation_timeout并保存阻塞状态；其他占用和超时仍失败。
依据用户“额外防御性编程先说明并同意”的规则，等待用户确认，尚未加入等待或重试，也未安排新模型运行。

## 用户批准与实现交付

用户明确选择“采用有界等待（推荐）”。现已实现：仅EADDRINUSE且匹配socket全为TIME_WAIT时等待，
必须真实bind/listen成功才放行；空快照、其他占用、读取失败或超时仍失败。
整个阶段共用operation_timeout期限，等待中仍检查peer错误；每端口JSON保存首末阻塞状态与耗时。
未修改生产功能或默认退出路径，003仍failed，实际根因未作已证实声明。

源码净改动：生产功能源码0；验收脚本verify_mempool_session.py实现状态采集与等待，
run_mempool_min.py传入阶段共用期限、peer检查及证据路径；verify_port_release.py提供Linux纯TCP自运行入口。
测试和说明另列：test_mempool_session_acceptance.py覆盖重绑成功、LISTEN/ESTABLISHED/CLOSE_WAIT、混合/空快照与超时；K1_TICKET12.md给出轻量命令。

本机CPU：新等待用例先因旧模块缺少time依赖/等待接口失败，接通后1项通过；
其余13项一次通过（1.481秒），含新负向方法的6个状态子用例。共14项；没有重复完整套件。
该新成功用例两轮后未再次运行，见[cpu-wait.txt](cpu-wait.txt)的其他13项记录。
[静态检查](static-wait.txt)：4文件ruff/format/isort/Python3.9 AST通过；轻量入口--help通过。
Standards与Spec独立只读评审各0项问题，未运行测试。
真实Linux等待仍待用户用轻量入口验证；此步骤不安排新一轮模型运行。

## 用户回传轻量等待验证通过

用户按c2ed808092交付的轻量入口回传终端JSON，归档为[linux-port-release.json](linux-port-release.json)。
来源为会话粘贴，未另回传主机SHA。回环端口36663首次/末次阻塞均为TIME_WAIT、inode=0；
等待60.40704846207518秒后真实bind/listen成功，status=passed。证明Linux轻量等待路径有效。
此结果不替代完整NPU验收，不把原003改为通过，也不证明memfabric析构异常已解决。
下一次完整入口使用新run-id k1-ticket12-004，两机统一c2ed808092；12仍open、11阻塞。
本次仅归档用户反馈，未执行新的本机测试或NPU运行。
