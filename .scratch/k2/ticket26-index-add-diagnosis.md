# Ticket26 008：target失败后的单卡诊断

008只启动target，复用007已通过的组件/reference，32仍在15:43:33–34失败。
报错继续包含aclrtMallocPhysical 507899、halMemCreate drvRetCode=42、2MiB分配失败。
本次新增Device7/PID2407981的设备记录：stream40/task62、InplaceIndexAdd、errorCode=0x2a，随后aclrtRecordEvent报507018。
这说明需要检查计数算子的执行，但不能从异步报错顺序认定该算子是首因。
137是服务退出结果；008的stability passed来自复用reference，不代表target通过。

007在捕获准备前余约30GB，且启动快照无NPU进程；008隔数小时仅启动target仍失败。
目前无法把这两次失败归因于快速切阶段，也未证明显存不足或驱动状态正常。
保留已通过证据；不重复第三轮相同服务，不进入capacity或后续阶段。

## 小探针范围

`ascend-mempool-test/scripts/diagnose_index_add.py`只需要32的一张空闲NPU。
不加载模型、不创建BM、不捕获Graph。它调用真实PageObservation的三项计数函数，
并复现runtime两项index_add_；输入使用bs16、17行、78层、topk2048及int64计数。
全padding和一条有效请求加padding各执行一次；检查第0/77层、重复索引0和有效行16。
每个命名调用后同步并核对所有行的精确计数，开始前将current写入report.json。
普通异常返回1；原生进程崩溃时，最后BEGIN/current用于定位未完成的调用范围。

此探针不能复现多rank、BM映射、跨stream或真实模型输入条件。
通过只排除这两组单卡eager输入下的直接失败；失败也需核对是否与服务同类错误。
没有修改功能源码、kernel、SDK或退出行为，也没有宣称507899已修复。

## 32容器执行一次

用户先人工确认该卡空闲；31本轮无需启动。使用Device7对应这次详细错误记录。
若失败，保留输出，不自动换配置重跑。

```bash
(
  set -e
  set -o pipefail
  cd /home/cryang/sglang
  git pull --ff-only
  ticket26_probe=/home/cryang/log/k2/k2-ticket26-008-index-add
  mkdir -p "$ticket26_probe"
  PYTORCH_NPU_ALLOC_CONF=expandable_segments:True TASK_QUEUE_ENABLE=0 \
    python3 -u ascend-mempool-test/scripts/diagnose_index_add.py \
      --device npu:7 --output "$ticket26_probe/report.json" \
      2>&1 | tee "$ticket26_probe/probe.log"
)
```

回传probe.log和report.json。成功末行是INDEX_ADD_PROBE_PASSED，报告含10项passed。
此结果不是target或完整验收通过；下一步由实际探针结果决定。

## 本地检查

Mac CPU执行一次，共10项通过；验证输入构造与断言，未复现NPU故障。
ruff检查及格式检查通过；Standards与Spec只读评审各0项未解决。
CPU原始输出保存在[index-add-cpu.json](verification/ticket26-local/index-add-cpu.json)。
26保持open，25不解锁；完整剩余五段仍是target、capacity、timeout、双rank微测、TP16微测。

用户随后回传32单卡十项全部PASS；[终端结果记录](verification/ticket26-local/index-add-npu-user-result.json)。
只确认该探针范围通过，服务故障仍未解决；下一步为[target预热边界诊断](ticket26-startup-diagnosis.md)。
