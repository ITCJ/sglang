# Ticket26：020两rank多基址微测

019的31 TIMEOUT_PASSED已收到，完整验收仍incomplete。
32最终输出未单独回传；确认32也结束并人工确认资源清理后运行020。
仅用现有run_bandwidth.py，每侧设备0、1，NUMA0，不加载模型。
不调用外层run_ticket26的benchmark循环，因此不会自动推进TP16。

## 两侧容器准备

沿用此前构建插件及用户已确认的两机SDK同源码、独立编译记录。
若SDK之后换过版本，需重新核对，不沿用旧确认文字。

```bash
cd /home/cryang/sglang &&
mkdir -p /home/cryang/log/k2/k2-ticket26-020-pair &&
test -f python/sglang/srt/hardware_backend/npu/mempool/kernels/unidex_multibase/build/libunidex_multibase.so
```

不要预建prefill/decode子目录；入口用新角色目录避免覆盖已有报告。
代码仍为已拉取的f51d1f7d0e；本轮仅记录结果与命令，无需拉取或重新编译。

## 31容器先启动

```bash
cd /home/cryang/sglang &&
SGLANG_UNIDEX_MULTIBASE_LIBRARY=/home/cryang/sglang/python/sglang/srt/hardware_backend/npu/mempool/kernels/unidex_multibase/build/libunidex_multibase.so \
python3 -u ascend-mempool-test/scripts/run_bandwidth.py \
  --role prefill \
  --prefill-host 10.120.72.31 --decode-host 10.120.72.32 \
  --run-id k2-ticket26-020-pair --log-root /home/cryang/log/k2 \
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
  --run-id k2-ticket26-020-pair --log-root /home/cryang/log/k2 \
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
路径为/home/cryang/log/k2/k2-ticket26-020-pair/prefill或decode。
若失败，回传failure.json和失败rank日志；不自动重试，已采样文件保留。
运行结束后人工确认清理，再安排TP16。

本轮仅文档与用户结果记录，无功能源码、脚本和测试变更。
Mac使用真实CLI解析及describe核对双侧配置、22档与池布局；未运行硬件或重跑模型。
