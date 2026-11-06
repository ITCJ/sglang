# Ticket26 从005的capacity继续

先让两侧005验收入口退出完成自身清理。保留005日志，勿删除已完成阶段。
在两侧原验收容器执行；本轮run-id为006，复用005组件、reference和target。
仅跑64/512；capacity之后是timeout、phase2及benchmark，没有自动重复已完成阶段。
新启动第一条模型日志应在capacity/launcher.log，前面应出现REUSED components/reference/target。

## 31

```bash
cd /home/cryang/sglang &&
git pull --ff-only &&
python3 -u ascend-mempool-test/scripts/run_ticket26.py \
  --role prefill \
  --config /home/cryang/mempool-min-ticket01-3132.json \
  --run-id k2-ticket26-006 \
  --phase1-service-run-id k2-ticket26-005 \
  --sdk-build-id mf114-c01f3ad-ticket11-batchfix \
  --sgl-source /home/cryang/sgl-kernel-npu \
  --prefill-host 10.120.72.31 \
  --decode-host 10.120.72.32 \
  --sdk-source-confirmation '用户已核对31和32 SDK源码HEAD一致，两侧分别编译'
```

## 32

```bash
cd /home/cryang/sglang &&
git pull --ff-only &&
python3 -u ascend-mempool-test/scripts/run_ticket26.py \
  --role decode \
  --config /home/cryang/mempool-min-ticket01-3132.json \
  --run-id k2-ticket26-006 \
  --phase1-service-run-id k2-ticket26-005 \
  --sdk-build-id mf114-c01f3ad-ticket11-batchfix \
  --sgl-source /home/cryang/sgl-kernel-npu \
  --prefill-host 10.120.72.31 \
  --decode-host 10.120.72.32 \
  --sdk-source-confirmation '用户已核对31和32 SDK源码HEAD一致，两侧分别编译'
```

## 结果与范围

日志与报告：/home/cryang/log/k2/k2-ticket26-006/，服务证据在/home/cryang/log/mempool-min/k2-ticket26-006-phase1/。
复用前检查配置、模型、包版本、基点、功能源码；验收脚本允许本次修复。
原证据不改写run-id，service-reused-from.json记录005来源；最终汇总同时包含复用阶段与新执行阶段。
identity不符或缺报告会失败，不会偷偷重新启动reference/target。
CPU检查通过不等于实机通过；等待两侧结果后再更新验收状态。
