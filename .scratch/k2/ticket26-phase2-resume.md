# Ticket26 阶段2从reference继续

006的阶段1执行完成，阶段2copy/fetch通过；reference因31的19003绑定冲突失败。
事后端口已空闲，历史占用者未确认；此次只提供续跑，不将冲突标记为已修复。
在31/32原验收容器执行以下命令。007跳过阶段1，并复用006阶段2组件报告。
首先模型启动应位于phase2/reference/launcher.log；不会再次出现START copy或START fetch。
随后自动执行target、capacity、timeout、cache gate及两rank/16rank微基准。只跑64/512、微测两次的约定不变。

## 31

```bash
cd /home/cryang/sglang &&
git pull --ff-only &&
python3 -u ascend-mempool-test/scripts/run_ticket26.py \
  --role prefill \
  --config /home/cryang/mempool-min-ticket01-3132.json \
  --run-id k2-ticket26-007 \
  --start-from phase2 \
  --phase2-component-run-id k2-ticket26-006 \
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
  --run-id k2-ticket26-007 \
  --start-from phase2 \
  --phase2-component-run-id k2-ticket26-006 \
  --sdk-build-id mf114-c01f3ad-ticket11-batchfix \
  --sgl-source /home/cryang/sgl-kernel-npu \
  --prefill-host 10.120.72.31 \
  --decode-host 10.120.72.32 \
  --sdk-source-confirmation '用户已核对31和32 SDK源码HEAD一致，两侧分别编译'
```

007只汇总此次执行的phase2/benchmark；阶段1证据仍保留在005/006，不能把007报告说成重新执行了所有阶段。
组件复用仍校验passed/rank/geometry并复制原JSON与来源记录；不满足则失败，不自动重跑已通过测试。
汇总位于/home/cryang/log/k2/k2-ticket26-007/，服务日志位于/home/cryang/log/mempool-min/k2-ticket26-007-phase2/。
