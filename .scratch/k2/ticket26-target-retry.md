# Ticket26 008：只重试target

先由用户确认31/32上一轮任务清理完毕，再在原验收容器分别运行以下命令。
此次只启动阶段2 target。007的copy/fetch/reference证据会校验并复制到008。
运行源码、配置、模型或包版本不一致就退出，不自动重做reference。
相邻提交若仅修改验收脚本，则通过python源码树及未提交源码校验允许复用。

007失败发生于Graph前eager warmup，尚未定位507899根因；本轮沿用原运行参数观察是否重现。
本次功能源码、kernel、SDK及默认进程清理不变。

## 31容器

```bash
cd /home/cryang/sglang &&
git pull --ff-only &&
python3 -u ascend-mempool-test/scripts/run_ticket26.py \
  --role prefill \
  --config /home/cryang/mempool-min-ticket01-3132.json \
  --run-id k2-ticket26-008 \
  --stage phase2 \
  --phase2-reference-run-id k2-ticket26-007 \
  --stop-after-target \
  --sdk-build-id mf114-c01f3ad-ticket11-batchfix \
  --sgl-source /home/cryang/sgl-kernel-npu \
  --prefill-host 10.120.72.31 \
  --decode-host 10.120.72.32 \
  --sdk-source-confirmation '用户已核对31和32 SDK源码HEAD一致，两侧分别编译'
```

## 32容器

```bash
cd /home/cryang/sglang &&
git pull --ff-only &&
python3 -u ascend-mempool-test/scripts/run_ticket26.py \
  --role decode \
  --config /home/cryang/mempool-min-ticket01-3132.json \
  --run-id k2-ticket26-008 \
  --stage phase2 \
  --phase2-reference-run-id k2-ticket26-007 \
  --stop-after-target \
  --sdk-build-id mf114-c01f3ad-ticket11-batchfix \
  --sgl-source /home/cryang/sgl-kernel-npu \
  --prefill-host 10.120.72.31 \
  --decode-host 10.120.72.32 \
  --sdk-source-confirmation '用户已核对31和32 SDK源码HEAD一致，两侧分别编译'
```

## 本段结果

启动输出应有REUSED components/reference、next=target；不会再START copy、fetch或reference launcher。
正常结束输出TARGET_PASSED；32另检查已完成长生成的cache eviction证据。任意失败返回非零。
summary.json与matrix报告均标记passed_execution_scope。
summary.json另记stopped_after=target、full_acceptance=incomplete。
这不是全部V1–V10通过；capacity、timeout、双rank微测和TP16微测尚未运行。

日志：/home/cryang/log/mempool-min/k2-ticket26-008-phase2/k2-ticket26-008-phase2-b64-sd512/{prefill,decode}/target/launcher.log。
两侧根目录summary.json记录本段范围及复用来源；外层汇总在/home/cryang/log/k2/k2-ticket26-008/。

本段按原流程停止自身进程、等待退出并检查端口。结束后由用户人工确认清理，再提供下一段命令。
若再次失败，保留最早异常及target环境快照；不自动重试、不自动启动下一段。
