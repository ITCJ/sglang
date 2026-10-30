# K1-ticket03 NPU004：host映射在驱动中失败

用户在31/32运行split-read-acceptance、k1-ticket03-004；P终端报告：

```text
START launcher pid=118467 log=/home/cryang/log/mempool-min/k1-ticket03-004/prefill/target/launcher.log
RuntimeError: NPU service exited unexpectedly with 137
```

本次修复基点为`3319ceb83dca8f073b07101301e70dd2fdd946dc`。
用户未回传实机Git身份，但日志中的bm_flags=640–646证实新增bit9已经启用。
日志根目录为`/home/cryang/log/mempool-min/k1-ticket03-004/`，router.log为空。
目标容器没有rg，日志取证改用grep；没有要求安装工具或重跑模型。

## 首个有效失败与137

用户回传的14:55:01–14:55:04日志：

```text
14:55:01 TP3: BEGIN stage=bm.create2 ... local_dram_bytes=13958643712 ... numa_node=1 bm_flags=641
14:55:02.469721 HYBM: HalMemSetAccess failed: 65534
14:55:02.469807 HYBM: segment allocate slice with size: 13958643712 failed: -6
14:55:02.470388 SMEM: smem_bm_create2 entry init failed, result: -1
14:55:02 TP3: FAIL stage=bm.create2 ... RuntimeError('create bm handle failed.')
14:55:04 TP3: Scheduler hit an exception ... RuntimeError: create bm handle failed.
14:55:04 Received sigquit from a child process.
14:55:04 kill_process_tree called: parent_pid=118594, include_parent=True, pid=118594
run_service.sh: line 149: 118594 Killed "${common[@]}"
```

TP5、7、2、12也报告相同HalMemSetAccess错误。
错误发生在上一轮引入的DRAM_MAP_HOST_VA路径，尚未进入恢复或scatter。
137是服务Python进程被默认清理后经bash/pipefail传出的状态；首错不是137本身。
回传dmesg唯一OOM记录为2026-10-29 12:18:57的logcollector、pid27143，
不能作为2026-10-30本次服务OOM的证据。
AccLink read-header行之后BM initialize已经成功，不将它作为本轮首错。

上轮根据本地SDK源码启用host映射的修复，在此实机环境不成立。
这里撤销该路径；不尝试解释65534未公开的更深层含义，也不修改通用退出逻辑。

## 修复路径与同步

1. BM创建恢复已运行过的NUMA标志；移除cpu_access接线及local_cpu_view。
2. 每层执行既有H2D、all_gather后，把汇总内容复制成独立CPU快照。
3. 在同一恢复stream上scatter完成其输入读取后，使用同一汇总HBM缓冲，
   通过既有UniDexCopy读取本rank真实私有prefix的全部命中页；复制至CPU并核对快照。
4. 查询到整次恢复event完成后，再复用同一缓冲读私有prefix，核对保存来源和原生HBM。
   默认CPU拷贝等待诊断读取完成，完成回执沿用原门控。

主KV与Index K仍各一块汇总HBM缓冲，没有G2H内部暂存或第三块HBM数据缓冲。
诊断索引在每次恢复建立一次，两个类型/所有层复用；只有verify_prefix启用时存在。
每层两次额外私有副本读取仅用于V2，计入oracle_read_bytes，不计入生产read_bytes。
生产三步发射计数保持原口径；开启V2的实际执行还包括诊断NPU读取和D2H。
这会影响T_lb_rank/op2及rank间等待，四段表仍是验收诊断，不能作为纯生产性能。
CPU快照按层存在，不保留全模型新副本；完成后释放索引和汇总buffer引用。

## 测试边界与结果

复用已约定的BM SDK、PrefixStore内容恢复边界。
SDK替身遇到bit9重放HalMemSetAccess/create2失败；LOCAL_HOST读取被明确拒绝，
避免用普通CPU内存掩盖003的非法地址问题。
旧实现的既有V2正向用例在create2边界失败，记录见red.log。
这重放的是已观察到的SDK拒绝，不能模拟真实驱动的内部执行。
改写后11项定向CPU通过（0.955秒），随后补强缓冲复用、实际读量和私有副本篡改断言。
补强后完整CPU套件单轮通过：494项，46.780秒，见full-cpu.log。
7模块strict mypy、7文件ruff/isort/格式和Python 3.9语法检查通过，见static.log。
Standards与Spec两轴独立复审均为0项发现；没有因评审再改代码或重复完整测试。

已移除上轮要求bit9成功的1项测试；该行为被真实环境证伪。
普通session的NUMA位保留仍由既有测试覆盖，V2内容校验没有删除。
没有访问NPU，也没有修改baselinetest分支。

## Standards

0项硬标准违规，0项需处理的代码异味。功能修改仅在NPU目录，
默认退出路径不变；V2复用两块缓冲，测试复用既有内容恢复边界。
被实机否定的host映射成功测试已删除，覆盖变化与验收状态有记录。

## Spec

0项发现。CPU快照使用copy=True，oracle通过UniDexCopy读取真实私有副本。
scatter与诊断读取使用同一恢复stream；第二次读取在event完成后执行，
阻塞CPU拷贝完成后才生成回执并释放缓冲、索引。
两块HBM数据缓冲及生产三步口径保留，额外诊断读与性能局限已明确。

两轴均只读评审，未另跑测试；最严重问题：无。

## 本次修复阅读顺序

Ascend/NPU功能源码：

1. `prefix.py`：CPU快照、两块缓冲复用、真实私有副本读取及生命周期。
2. `manager.py`：撤销bit9与直接CPU视图接口。
3. `runtime.py`：撤销验收配置到cpu_access的初始化接线。

通用主线功能源码：无。
测试：test_pool.py、test_prefix.py、test_prefix_split_read.py、test_pool_session.py。
文档：本记录、票03 Comments、交付/运行说明与target-alignment D节。

NPU004失败，005待用户执行；State=open，验收项不勾选。
