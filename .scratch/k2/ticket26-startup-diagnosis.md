# Ticket26：target预热的index_add_边界诊断

用户在32执行3cbc3bf921的单卡探针，十项检查均通过。
它覆盖两组单stream输入，不能证明模型、多stream和共享池环境中的同类调用正常。
008的InplaceIndexAdd故障仍未定位，target未通过；本轮不重复相同完整验收。

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
