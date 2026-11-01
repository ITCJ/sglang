# Ticket12 NPU002失败反馈

来源：用户在会话粘贴的32侧summary.json、target/launcher.log尾部和31/32终端；完整原始目录未回传。
版本95cac11ee328032862f4c47ed9150b44a67d5a5d，tracked diff为空；31/32，run-id k1-ticket12-002。

- D报告stages为page-first-copy、fetch，均通过；identity_check通过。
- D报告timeout_fail_stop通过：SR heartbeat timeout、16个native rank保留、全部配额保留、无自动释放；worker_exit=137。
- paired_process_cleanup为passed；随后OSError: [Errno 98] Address already in use。
- 依据stage顺序，错误定位到target清理后的released_port检查；并非尚未启动服务。当前实现未把失败端口写入异常。
- D退出日志出现memfabric HybmVaManager::RemoveOneVaInfo / MemEntityFactory析构崩溃及malloc_consolidate错误；保留为未解决现象。进程退出不等于库正常析构。
- 32容器没有ss。下一步以Python读取/proc/net/tcp及tcp6，配合相同bind/listen检查定位端口、TCP状态和持有PID。没有确认监听残留或TIME_WAIT哪种原因。

未拿到target完整内容/字节证据，未进入restart验收；不能认定端到端或整票通过。未远程访问硬件，未安排新一轮模型运行。
12保持open，11未解锁。001运行内容尚未提供，不据run-id推断验收轮数。

## 26088证据与修复

用户随后回传：

```text
BIND_FAILED 10.120.72.32 26088 OSError(98, 'Address already in use')
tcp port=26088 state=ESTABLISHED local=2048780A:65E8 remote=2333BE04:01BB inode=1386654595
```

对端解码为4.190.51.35:443；未查到持有PID，不推断进程身份。
旧检查覆盖25670..26181全部512个预留端口；实际D端点为25686+33*rank，rank=0..15。
26088不是本服务端点，却被清理检查扫描，导致误报。此证据不证明memfabric析构正常。

修复仅含验收脚本：run_mempool_min.py复用生产SharingPlan.session()生成实际NIC/store清单，
启动前和退出后共用；verify_mempool_session.py保留严格bind/listen，在错误中补充host:port。
不改生产源码、拷贝、完成语义、默认shutdown或清理行为。原始002仍failed。

CPU：抽取旧检查清单后，新回归因26088在清单中失败；修复后会话验收测试11项通过（1.427秒），
另补非共享/P共享/D共享两端清单测试1项、6个subtest通过（0.002秒）。未重复完整套件。
3文件ruff/format/isort/Python3.9 AST通过，详见[静态记录](static.txt)。

Standards独立复核：0硬违规，1低优先级重复布局构造建议（与memory_budget重复，非阻塞；本次保留简单局部实现）。
Spec独立复核：0问题，核对三种会话与启动前/清理后接线。两位reviewer未修改代码或运行测试。
NPU修复后未运行，memfabric退出析构崩溃及完整restart验收仍未验证。
