# Ticket26：022独立TP16微测

021双侧两rank测量与清理通过；用户确认资源清理后运行022。
每侧16设备、8个NUMA组、8份D副本；预期完整11活跃源形状。
仅运行既有微测，不加载模型或重跑服务；保持samples1/warmup1/iterations2。

## 两侧容器准备

沿用此前构建插件及用户已确认的两机SDK同源码、独立编译记录。
若SDK之后换过版本，需重新核对，不沿用旧确认文字。

```bash
cd /home/cryang/sglang &&
mkdir -p /home/cryang/log/k2/k2-ticket26-022-tp16 &&
test -f python/sglang/srt/hardware_backend/npu/mempool/kernels/unidex_multibase/build/libunidex_multibase.so
```

不要预建prefill/decode子目录；入口用新角色目录避免覆盖已有报告。
沿用已通过021的代码和插件，无需拉取或重新编译。

## 31容器先启动

```bash
cd /home/cryang/sglang &&
SGLANG_UNIDEX_MULTIBASE_LIBRARY=/home/cryang/sglang/python/sglang/srt/hardware_backend/npu/mempool/kernels/unidex_multibase/build/libunidex_multibase.so \
python3 -u ascend-mempool-test/scripts/run_bandwidth.py \
  --role prefill \
  --prefill-host 10.120.72.31 --decode-host 10.120.72.32 \
  --run-id k2-ticket26-022-tp16 --log-root /home/cryang/log/k2 \
  --suite remote-write --copy-backend multibase \
  --devices 0 1 2 3 4 5 6 7 8 9 10 11 12 13 14 15 --numa-nodes 0 1 2 3 4 5 6 7 --page-size 64 \
  --samples 1 --warmup 1 --iterations 2 --control-port 22200 \
  --sdk-source-confirmation '用户已核对31和32 SDK源码HEAD一致，两侧分别编译'
```

## 32容器随后启动

```bash
cd /home/cryang/sglang &&
SGLANG_UNIDEX_MULTIBASE_LIBRARY=/home/cryang/sglang/python/sglang/srt/hardware_backend/npu/mempool/kernels/unidex_multibase/build/libunidex_multibase.so \
python3 -u ascend-mempool-test/scripts/run_bandwidth.py \
  --role decode \
  --prefill-host 10.120.72.31 --decode-host 10.120.72.32 \
  --run-id k2-ticket26-022-tp16 --log-root /home/cryang/log/k2 \
  --suite remote-write --copy-backend multibase \
  --devices 0 1 2 3 4 5 6 7 8 9 10 11 12 13 14 15 --numa-nodes 0 1 2 3 4 5 6 7 --page-size 64 \
  --samples 1 --warmup 1 --iterations 2 --control-port 22200 \
  --sdk-source-confirmation '用户已核对31和32 SDK源码HEAD一致，两侧分别编译'
```

## 范围与回传

保留4K/8K/32K/128K五种写场景、D读基线、8K双写pipeline共22档，以及Graph专项。
成功双侧输出TICKET14_MEASUREMENTS_COMPLETE，报告passed_measurement_scope且清理passed。
预期32份本地/peer Graph报告：source_capacity=12、post25_active_sources=11、full_shape=true。
D过期页表检查仍需passed；保留每rank报告，不以单rank代替全部。

回传两侧report.json、runner.json、conclusions.md和rank-*/multibase-graph.json。
目录/home/cryang/log/k2/k2-ticket26-022-tp16/prefill或decode。
失败回传failure.json和首个失败rank日志；不自动重试。

Mac实际CLI计划核对双侧配置一致、22档；BM主机贡献P193GiB、D176GiB，不是HBM预算。
本轮无代码改动、无新CPU测试；硬件由用户运行。
物理NUMA未验证、模型计算重叠未知、迁移同负载观测对照未完成；本段通过不自动关闭26或解锁25。


## 022 TP16通过及性能归档

用户回传31完成标记、prefill report/runner和32份本地/peer Graph汇总；32终端完成标记未单独回传。
报告passed_measurement_scope、cleanup passed；32份Graph均passed，容量12、活跃源11、full_shape=true，无缺失。
128K远端写287.839GB/s、双写314.699GB/s；相对021分别6.67倍、3.93倍。
D读基线6.037ms；128K远端写干扰12.626ms（2.092倍），双写9.001ms（1.491倍）。
本轮仅归档用户硬件证据和文档，无功能源码、脚本、测试改动或硬件重跑。
迁移同负载观测对照仍待整理，模型计算重叠未知；物理NUMA落点未验证。
26保持open，25未解锁。见ticket26-performance-021-022.md及verification/ticket26-local/tp16-022-user-result.json。
