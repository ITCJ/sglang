# Ticket26：016长生成超时后的017 target重试

016前四个请求HTTP200，第五个8192输入/512输出请求在600秒总时限到达时失败。
16rank均记录step1@22:11:21至step315@22:21:19，Router先于末步于22:21:16报request timeout。
这支持时限不足为直接失败原因，不能把之后TP fault当作首发原因；较慢的性能原因未验证。
按该区间平均约1.9秒/步估算，512步约16分钟。本轮显式设operation_timeout=1800秒。
此字段同时影响请求、HTTP、SR操作和心跳时限；只改变本轮有效配置，生产默认及后续故障测试不改。
015 reference原配置600秒及输出保持原样。复用只允许该字段显式增大，其他源码/模型/配置/版本仍严格相同。
service-reused-from.json保存reference600/target1800；requested-config和effective-config保留前后配置。

六个请求共享prefix目录，末尾统一校验内容和占用；前四个HTTP200不是整组通过。
新进程不能延续step315或原prefix状态，所以017只重试target整组六例，重建目录再完成长生成和EOS。
组件/reference不重跑，016失败记录保留。用户确认两侧资源清理后手动执行，不自动重试或进入下一阶段。

## 两侧容器准备

```bash
cd /home/cryang/sglang &&
git pull --ff-only &&
mkdir -p /home/cryang/log/k2/k2-ticket26-017-target &&
cp /home/cryang/log/k2/k2-ticket26-015-reference/config.json \
   /home/cryang/log/k2/k2-ticket26-017-target/config.json
```

## 31容器先启动

```bash
cd /home/cryang/sglang &&
SGLANG_NPU_MEMPOOL_INDEX_DIAG=0 python3 -u ascend-mempool-test/scripts/run_mempool_min.py \
  --role prefill --mode full-acceptance \
  --config /home/cryang/log/k2/k2-ticket26-017-target/config.json \
  --log-root /home/cryang/log/k2 --run-id k2-ticket26-017-target \
  --page-allocation \
  --reference-service-report /home/cryang/log/k2/k2-ticket26-015-reference/prefill \
  --service-timeout 1800
```

## 32容器随后启动

```bash
cd /home/cryang/sglang &&
SGLANG_NPU_MEMPOOL_INDEX_DIAG=0 python3 -u ascend-mempool-test/scripts/run_mempool_min.py \
  --role decode --mode full-acceptance \
  --config /home/cryang/log/k2/k2-ticket26-017-target/config.json \
  --log-root /home/cryang/log/k2 --run-id k2-ticket26-017-target \
  --page-allocation \
  --reference-service-report /home/cryang/log/k2/k2-ticket26-015-reference/decode \
  --service-timeout 1800
```

两侧成功标记TARGET_PASSED，自动在target后停止；summary仍为passed_execution_scope/full_acceptance=incomplete。
这不是整个ticket通过；capacity、timeout及微测仍需后续分段。保留015和017全部JSON、code.patch及内容回执。
本轮不重新编译插件；日志位于017各角色target/launcher.log，31另有target/router.log。

## 修改与验证

功能源码（Ascend/NPU、通用主线）均无改动。
脚本run_mempool_min.py提供仅target单段适用的有限正数时限参数，并写入实际有效配置。
mempool_service_resume.py只允许显式增加operation_timeout，保留其他身份检查和原报告，记录变更。
测试test_service_resume.py覆盖CLI作用域、非法时限、显式时限变更、其它配置仍拒绝及原续跑路径。
Mac一轮11项通过，ruff通过；Standards/Spec各0未解决。无新增同步、设备分配或shutdown变更。
017待用户NPU运行，26保持open、25未解锁。
