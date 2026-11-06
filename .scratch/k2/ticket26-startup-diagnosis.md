# Ticket26：target预热的index_add_边界诊断

用户在32执行3cbc3bf921的单卡探针，十项检查均通过。
它覆盖两组单stream输入，不能证明模型、多stream和共享池环境中的同类调用正常。
008的InplaceIndexAdd故障仍未定位，target未通过；本轮不重复相同完整验收。

## 最新结果：013正常异步启动通过

用户确认31成功标记，32报告diagnostic_startup_passed、cleanup=passed。
版本7358ebc26e，index_diagnosis=false；正常异步预热与Graph启动完成。
本轮没有生成请求，Graph replay和正式target仍未验收；单次成功支持候选修复方向，未证明唯一根因。
旧日志已由用户删除，原reference响应/报告不再可用；正式续跑需先确认备份及源码变化后的证据适用性。
本轮不重跑已过阶段、不自动推进下游。证据见[013用户结果](verification/ticket26-local/async-013-user-result.json)。

## 当前结论：009同步预热完成，010检查正常异步启动

用户回传009离线读取结果：warmup_evidence_complete，32组完整预热，每组390次调用。
missing、failures、parse_errors均为空，合计12,480次index_add_在诊断同步条件下完成。
原始summary因解析异常失败；离线结果不改写它，也不证明正式target验收或双机清理通过。
用户输出单独保存在[009证据](verification/ticket26-local/warmup-009-user-result.json)。

代码审计发现：materialize_selected_kv在主stream创建临时请求行号、mask和拷贝索引，
miss/refill/slot-map等stream继续读取；函数返回后，attention还会分配临时张量，然后才join回填/map。
原copy_ready事件保证生产先于消费，但未登记这些主stream临时存储的跨stream使用。
本轮在NPU manager的三条mempool消费路径补record_stream，让allocator等相应stream完成再复用存储。
不添加全设备同步，不扩建KV/Index K缓冲，不改变kernel、计数、控制协议或默认退出行为。
临时存储可能因此保留更久；内存峰值与Graph捕获行为仍需本轮实机核对。

这是已确认的生命周期登记缺口及候选修复，尚未证明它就是008故障根因。
另两项候选仍保留：AICPU算子并发问题、较早的拷贝破坏数据。
010只改变生命周期登记，并关闭009诊断同步；若正常异步启动仍失败，不能继续用同步成功宣称修复。

010前由用户人工确认31/32已清理。两侧各运行一次，下方31先启动等待32；无需重编译插件。
沿用008有效配置；既有预检和自身进程清理继续执行。只启动target到ready，不发生成请求，
不重跑组件/reference，不推进capacity、timeout或微测。结束后仍由用户人工确认清理。

31验收容器：

```bash
cd /home/cryang/sglang &&
git pull --ff-only &&
python3 -u ascend-mempool-test/scripts/diagnose_target_startup.py \
  --role prefill \
  --config /home/cryang/log/mempool-min/k2-ticket26-008-phase2/k2-ticket26-008-phase2-b64-sd512/prefill/effective-config.json \
  --run-id k2-ticket26-010-async \
  --without-index-sync
```

32验收容器：

```bash
cd /home/cryang/sglang &&
git pull --ff-only &&
python3 -u ascend-mempool-test/scripts/diagnose_target_startup.py \
  --role decode \
  --config /home/cryang/log/mempool-min/k2-ticket26-008-phase2/k2-ticket26-008-phase2-b64-sd512/decode/effective-config.json \
  --run-id k2-ticket26-010-async \
  --without-index-sync
```

两侧日志位于/home/cryang/log/k2/k2-ticket26-010-async/{prefill,decode}/。
预期summary.json中status=diagnostic_startup_passed、index_diagnosis=false；
coverage写明normal asynchronous warmup，target_acceptance仍为not-run。
成功标记TARGET_STARTUP_DIAGNOSED；失败非零。回传两侧summary和失败侧launcher.log首段异常。
本轮成功仅允许继续正式target验收，不据此进入其它验收阶段或解锁25。

本轮净改动阅读顺序：

- Ascend/NPU功能源码：sparsity_driven_kv_offload/manager.py，在临时张量的消费者stream登记存储使用。
- 通用主线功能源码：无改动。
- 脚本：diagnose_target_startup.py新增--without-index-sync，显式关闭继承的诊断开关。
- 测试：test_multibase_delivery.py用真实materialization入口核对延迟消费者的所有权登记；test_index_diagnosis.py检查异步启动开关和原失败清理。
- 文档/证据：本说明、ticket26 issue/delivery、进度D，以及用户009结果记录。

Mac检查：materialization相关12项、启动相关3项通过。
新生命周期测试首次因fixture未启用count_sources失败，修正fixture后通过；没有在CPU复现NPU驱动故障。
该测试模拟allocator持有存储，只验证调用方登记；硬件并发、allocator事件和Graph捕获仍待010验证；请求与Graph replay不在本轮范围。
脚本/测试ruff通过，四份源码格式检查通过；manager保持8条历史lint，新增0条。
Standards/Spec各0未解决；评审未重复运行测试。010未运行；26保持open、25不解锁。

## 009反馈后的离线读取

009用户回传：32报JSONDecodeError Extra data line1 column69，31随后peer failed。
本地已复现同样位置的错误：多个rank的完整JSON拼接到同一物理行，旧json.loads把整行当单个对象。
实际009日志片段尚待用户从32验收容器回传；宿主机同路径不存在，不能据此判断日志丢失。
解析改为逐marker读取一个JSON对象，并明确保留损坏/失败记录；仍要求16卡各两次完整预热记录。
新增--read-log只离线读取已有日志，写独立index-diagnosis.json，不连接peer、不加载NPU、不改原summary。
两项新回归先失败，修复后连同已有四项共六项通过；ruff通过，两轴评审各0未解决。

32验收容器使用更新后的脚本读取009，31无需运行，无需重启模型：

```bash
cd /home/cryang/sglang &&
python3 -u ascend-mempool-test/scripts/diagnose_target_startup.py \
  --read-log /home/cryang/log/k2/k2-ticket26-009-diag/decode/launcher.log
```

完整证据输出warmup_evidence_complete及completed_pairs=32。
这只确认保存的预热记录完整，不把原failed报告改成通过，不代表服务验收或清理已确认。
下方保留009启动说明供追溯；本次不再执行启动命令。

## 正常执行路径与证据

用户先人工确认31/32清理完毕，再各执行下面的一条命令，31先启动等待32。
读取008实际effective-config.json，保持原模型、池布局和观测配置；复用已编译插件并记录hash。
只启动一个target，预检、双机身份核对、服务就绪与自身进程清理由已有实现完成。
不加载reference、不跑组件、不发送生成请求、不进入capacity/timeout/微测。

32启用SGLANG_NPU_MEMPOOL_INDEX_DIAG=1，仅在Graph前两次eager warmup内安装TorchDispatchMode。
每遇到aten.index_add_，先打印调用位置、形状、stream，再同步先前设备任务并读取请求行索引。
随后原样调用算子并同步；上下文退出时恢复dispatch，Graph捕获本身不插入同步。
诊断不修改输入或计数，不打印KV数据。全设备同步会改变并发时序，因此成功不是修复证明。
HTTP服务自带的后续dummy warmup关闭；Graph初始化和原有两次预热保留。

预期区分：before_sync失败表示先前任务已失败；index_add/after_sync失败定位到该次调用区间。
read_indices失败单独记录；只有完整完成的预热才输出warmup_complete。
若同步后不再失败，说明需要继续检查原并发条件；不能直接把同步留作生产修复。

## 31容器

```bash
cd /home/cryang/sglang &&
git pull --ff-only &&
python3 -u ascend-mempool-test/scripts/diagnose_target_startup.py \
  --role prefill \
  --config /home/cryang/log/mempool-min/k2-ticket26-008-phase2/k2-ticket26-008-phase2-b64-sd512/prefill/effective-config.json \
  --run-id k2-ticket26-009-diag
```

## 32容器

```bash
cd /home/cryang/sglang &&
git pull --ff-only &&
python3 -u ascend-mempool-test/scripts/diagnose_target_startup.py \
  --role decode \
  --config /home/cryang/log/mempool-min/k2-ticket26-008-phase2/k2-ticket26-008-phase2-b64-sd512/decode/effective-config.json \
  --run-id k2-ticket26-009-diag
```

## 结果与回传

日志根目录：/home/cryang/log/k2/k2-ticket26-009-diag/{prefill,decode}/。
launcher.log保留服务原始异常和MEMPOOL_INDEX_DIAG标记，summary.json保留诊断范围及结果。
32就绪后核对16卡各两次warmup_complete且实际拦截计数大于零，再与31协调退出。
成功标记TARGET_STARTUP_DIAGNOSED，status为diagnostic_startup_passed，target_acceptance仍not-run。
失败非零、沿既有路径停止本轮自身进程。两侧结束后由用户人工确认清理，不自动重试。

32回传末尾诊断标记及第一段异常：

```bash
ticket26_diag=/home/cryang/log/k2/k2-ticket26-009-diag/decode
grep -n 'MEMPOOL_INDEX_DIAG' "$ticket26_diag/launcher.log" | tail -n 45
python3 - "$ticket26_diag/launcher.log" <<'PY'
import sys
from pathlib import Path
lines = Path(sys.argv[1]).read_text(errors='replace').splitlines()
for i, line in enumerate(lines):
    if 'Scheduler hit an exception' in line:
        print('\n'.join(lines[i:i+140]))
        break
PY
cat "$ticket26_diag/summary.json"
```

## 本轮净改动与验证

Ascend/NPU功能源码：

- mempool/index_diagnosis.py：默认关闭的预热诊断上下文；划分同步前后边界并打印索引。
- graph_runner/npu_cudagraph_backend.py：仅在原两次eager warmup包围诊断上下文。

通用主线功能源码：无改动。既有shutdown、计数和拷贝kernel均不修改。

脚本diagnose_target_startup.py提供一次双机target启动；测试test_index_diagnosis.py覆盖真实CPU计数结果、关闭/退出恢复、前后同步失败、TP16证据不齐和启动失败清理。
CPU第一轮3方法通过；补充启动失败清理用例后第二轮4方法通过，未再重复。
新增三文件ruff通过，四文件格式检查通过；既有graph backend保留9条历史lint，新增0条。
Standards与Spec各0未解决；尚未运行本轮NPU诊断，26保持open、25不解锁。
