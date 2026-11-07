# Ticket26：021两rank微测夹具修复重试

019双侧TIMEOUT_PASSED已收到，完整验收仍incomplete。
020在D的空写预热因batch缺少extend_seq_lens退出，22档测量尚未开始。
补全四个可选字段为None；用户人工确认两侧资源清理后运行021。
仅用现有run_bandwidth.py，每侧设备0、1，NUMA0，不加载模型。
不调用外层run_ticket26的benchmark循环，因此不会自动推进TP16。

## 两侧容器准备

沿用此前构建插件及用户已确认的两机SDK同源码、独立编译记录。
若SDK之后换过版本，需重新核对，不沿用旧确认文字。

```bash
cd /home/cryang/sglang &&
git pull --ff-only &&
mkdir -p /home/cryang/log/k2/k2-ticket26-021-pair &&
test -f python/sglang/srt/hardware_backend/npu/mempool/kernels/unidex_multibase/build/libunidex_multibase.so
```

不要预建prefill/decode子目录；入口用新角色目录避免覆盖已有报告。
两侧都需拉取本轮夹具修复；插件未改，不重新编译。

## 31容器先启动

```bash
cd /home/cryang/sglang &&
SGLANG_UNIDEX_MULTIBASE_LIBRARY=/home/cryang/sglang/python/sglang/srt/hardware_backend/npu/mempool/kernels/unidex_multibase/build/libunidex_multibase.so \
python3 -u ascend-mempool-test/scripts/run_bandwidth.py \
  --role prefill \
  --prefill-host 10.120.72.31 --decode-host 10.120.72.32 \
  --run-id k2-ticket26-021-pair --log-root /home/cryang/log/k2 \
  --suite remote-write --copy-backend multibase \
  --devices 0 1 --numa-nodes 0 --page-size 64 \
  --samples 1 --warmup 1 --iterations 2 --control-port 22000 \
  --sdk-source-confirmation '用户已核对31和32 SDK源码HEAD一致，两侧分别编译'
```

## 32容器随后启动

```bash
cd /home/cryang/sglang &&
SGLANG_UNIDEX_MULTIBASE_LIBRARY=/home/cryang/sglang/python/sglang/srt/hardware_backend/npu/mempool/kernels/unidex_multibase/build/libunidex_multibase.so \
python3 -u ascend-mempool-test/scripts/run_bandwidth.py \
  --role decode \
  --prefill-host 10.120.72.31 --decode-host 10.120.72.32 \
  --run-id k2-ticket26-021-pair --log-root /home/cryang/log/k2 \
  --suite remote-write --copy-backend multibase \
  --devices 0 1 --numa-nodes 0 --page-size 64 \
  --samples 1 --warmup 1 --iterations 2 --control-port 22000 \
  --sdk-source-confirmation '用户已核对31和32 SDK源码HEAD一致，两侧分别编译'
```

## 范围与回传

保留4K/8K/32K/128K各档local、remote、dual、remote+D读、dual+D读共20档。
另有D本地读基线与8K双写pipeline，共22档；保留多基址Graph专项。
写入测量使用Graph replay，每档一次预热、两次测量；小样本仅作粗测。
这不证明模型计算重叠，也不执行下一档TP16。
计划的两侧BM主机内存贡献合计分别为35GiB和22GiB，不是HBM预算。

成功沿用旧入口标记TICKET14_MEASUREMENTS_COMPLETE，此次内容由copy_backend=multibase标识。
回传两侧report.json、conclusions.md、runner.json，以及rank-*/multibase-graph.json。
路径为/home/cryang/log/k2/k2-ticket26-021-pair/prefill或decode。
若失败，回传failure.json和失败rank日志；不自动重试，已采样文件保留。
运行结束后人工确认清理，再安排TP16。

本轮只改测试支持multibase_graph.py：共用dummy batch构造，补四个正式默认None字段。
生产Ascend/NPU、通用主线、kernel、SDK、清理路径均无修改。
test_multibase_delivery.py定向检查真实runtime：原字段缺失先复现，修复后空写不改KV/计数，过期版本仍拒绝。
两轮为同一测试的红/绿检查；CPU不能证明NPU Graph通过，021待用户运行。
020的GVA日志保留，未证明它导致本次退出，不宣称已解决SDK映射问题。

本轮ruff通过；Standards与Spec评审各0项未解决。
