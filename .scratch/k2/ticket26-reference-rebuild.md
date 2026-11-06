# Ticket26：重建reference，单独续跑target

013双侧正常异步启动通过，版本7358ebc26e；生成请求与Graph replay尚未验收。
用户已删除旧日志及reference原始响应，2026-11-06明确授权只补跑一次reference。
历史已过组件不重跑，也不伪造组件通过报告；本轮范围只限服务证据，完整验收仍incomplete。

## 014预检失败后的015重试

014在START launcher之前报by_segment requires shared prefix，尚未运行reference模型。
service_ports推导私有reference端口时关闭prefix_shared却保留by_segment，临时配置违反校验契约。
现仅把该临时配置的prefix_read_assignment改为auto；传入配置和target读者分配不变。
本地msgspec0.20的replace不调用post-init，初次新测试未复现；手动严格校验复现同一ValueError。
测试现显式检查派生配置契约，验证实际64/512配置的P/D端口集合及原配置不变。
修复后两项定向检查通过，含既有19003残留阻断；没有重复NPU验收。
Standards/Spec各0未解决。以下命令使用新目录015，保留014失败记录。

## 本轮行为

--reference-only：预检、生成原完整服务用例、运行reference一次、保存响应及清理证据后退出。
保留原稳定性/内容检查，失败非零；单段入口固定page-allocation控制并拒绝两轮观测参数。
reference稳定性及答案、target答案及完整输出ID均在退出前判定。
不会启动copy/fetch、target、capacity或timeout。
--reference-service-report：下一段核对新reference的配置、功能源码、模型、包版本与成功状态，
恢复原始用例和输出，只运行target后退出；不放宽身份检查，不允许没有原始响应的精度对比。
默认SGLang退出和runner自身进程清理均不改变；每段之间由用户人工确认清理。
组件原始报告已丢失，完整V1–V10不能凭聊天记录重建为passed。26仍open、25未解锁。

## 准备：31/32容器各执行

```bash
cd /home/cryang/sglang &&
git pull --ff-only &&
mkdir -p /home/cryang/log/k2/k2-ticket26-015-reference &&
cp /home/cryang/log/k2/k2-ticket26-013-async/config.json \
   /home/cryang/log/k2/k2-ticket26-015-reference/config.json
```

## 31容器：先启动

```bash
cd /home/cryang/sglang &&
SGLANG_NPU_MEMPOOL_INDEX_DIAG=0 python3 -u ascend-mempool-test/scripts/run_mempool_min.py \
  --role prefill --mode full-acceptance \
  --config /home/cryang/log/k2/k2-ticket26-015-reference/config.json \
  --log-root /home/cryang/log/k2 \
  --run-id k2-ticket26-015-reference \
  --page-allocation --reference-only
```

## 32容器：随后启动

```bash
cd /home/cryang/sglang &&
SGLANG_NPU_MEMPOOL_INDEX_DIAG=0 python3 -u ascend-mempool-test/scripts/run_mempool_min.py \
  --role decode --mode full-acceptance \
  --config /home/cryang/log/k2/k2-ticket26-015-reference/config.json \
  --log-root /home/cryang/log/k2 \
  --run-id k2-ticket26-015-reference \
  --page-allocation --reference-only
```

两侧成功输出REFERENCE_PASSED；summary.json为passed_execution_scope、stopped_after=reference。
日志位于新目录的prefill/reference或decode/reference下；原始响应在prefill/reference/responses.json。
请保留015目录中的JSON证据及code.patch，至少到target精度对比完成；大launcher日志可另行归档。
下一段target使用--reference-service-report指向各自015的prefill/decode目录，自动在target结束后停止。
本次不同时给target启动指令，等用户回传reference结果和确认清理后再运行。

## 文件与检查

Ascend/NPU功能源码：无改动。通用主线功能源码：无改动。
脚本run_mempool_min.py增加两条服务单段入口；mempool_service_resume.py支持缺失组件报告时仅复用明确成功的reference。
测试test_service_resume.py覆盖两侧单段顺序、原始输出恢复、失败reference拒绝及原续跑行为；Mac首轮7项通过，评审修复后第二轮9项通过，ruff通过。
Standards/Spec各0未解决。新增控制只选择验收范围，没有设备分配、同步或shutdown修改；NPU补跑待用户执行。
