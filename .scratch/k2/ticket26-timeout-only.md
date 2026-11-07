# Ticket26：019独立超时检查

用户回传018的31 CAPACITY_PASSED；该标记经过双侧容量证据与清理屏障。
32 summary未单独回传。017离线缓存淘汰检查passed，16条证据。
019只执行原超时故障阶段，完成后停止；reference、正常target请求组、capacity均不重复。
用户先确认31/32资源清理；019每侧仅启动一次模型，不重编译插件。

## 两侧容器准备

```bash
cd /home/cryang/sglang &&
git pull --ff-only &&
mkdir -p /home/cryang/log/k2/k2-ticket26-019-timeout &&
cp /home/cryang/log/k2/k2-ticket26-015-reference/config.json \
   /home/cryang/log/k2/k2-ticket26-019-timeout/config.json
```

## 31容器先启动

```bash
cd /home/cryang/sglang &&
SGLANG_NPU_MEMPOOL_INDEX_DIAG=0 python3 -u ascend-mempool-test/scripts/run_mempool_min.py \
  --role prefill --mode full-acceptance \
  --config /home/cryang/log/k2/k2-ticket26-019-timeout/config.json \
  --log-root /home/cryang/log/k2 --run-id k2-ticket26-019-timeout \
  --timeout-only
```

## 32容器随后启动

```bash
cd /home/cryang/sglang &&
SGLANG_NPU_MEMPOOL_INDEX_DIAG=0 python3 -u ascend-mempool-test/scripts/run_mempool_min.py \
  --role decode --mode full-acceptance \
  --config /home/cryang/log/k2/k2-ticket26-019-timeout/config.json \
  --log-root /home/cryang/log/k2 --run-id k2-ticket26-019-timeout \
  --timeout-only
```

## 路径与结果

正常就绪后暂停本轮自有P worker进程组，发一个请求使D预留资源。
故意等待operation_timeout=600秒，检查Router超时、D保留分配及Router拒绝后续请求。
这段约10分钟的等待是测试内容；随后恢复P进程组以便沿用既有退出/清理路径。
最终核验完整D日志、失败退出码、成对进程清理及端口释放。
故障注入后的异常日志不能单独判断失败；成功须双侧TIMEOUT_PASSED且脚本退出码0。

回传019各角色summary.json、timeout/timeout.json与timeout/process-cleanup.json。
summary应为passed_execution_scope/stopped_after=timeout/full_acceptance=incomplete。
若失败，回传31 timeout/router.log及双侧timeout/launcher.log。脚本失败非零，不自动重试。
本轮不做正常生成请求，不代表所有组件/微测或完整ticket已验收。

## 修改与验证

Ascend/NPU及通用主线功能源码均无改动。
脚本run_mempool_min.py增加互斥timeout-only入口，与capacity共用局部单段报告路径。
仍调用原stage的fault_only分支，保持超时证据和清理语义。
测试test_service_resume.py扩展原单段测试，覆盖双侧容量/超时互斥、只调用目标阶段及失败传播。
现有test_mempool_full.py的成对超时检查验证保留分配与自有进程清理，未修改。
Mac两项定向CPU检查一轮通过，ruff通过；Standards 0、Spec 0未解决。
NPU019待用户运行，26保持open，25未解锁。
