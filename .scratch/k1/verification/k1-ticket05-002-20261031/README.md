# K1-ticket05 NPU002：自动化精简功能验收通过

2026-10-31，用户回传31侧完整summary与共享验收报告，均为passed；32侧提供两轮启动终端片段。
本目录JSON依据用户粘贴内容转录，保留user_accepted=false；未访问NPU或在本机重跑硬件验收。
32侧独立summary及最终退出码未回传；P汇总已核验D证据、双端清理及端口释放。
用户已确认接受本次精简范围，05已closed；[人工确认](acceptance.md)单独记录，原始JSON不改写。

## 执行环境与复现

- 版本：db4b3ac1b926b9274f533f3c8f086bc1cd8034ce，两侧身份检查通过，代码无差异。
- 31 / 10.120.72.31为P，32 / 10.120.72.32为D，各TP16，网络bond4。
- 模型：/data_lib/data/models/GLM-5.2-w4a8；page128，P slot8192，D slot128，prefix40页。
- P共享组{0,1}…{14,15}，连续分块写；prefix读取策略round_robin。
- CANN/ATB/MemFabric环境沿用前票。torch2.10.0+cpu、torch-npu2.10.0、sgl-kernel-npu2026.9.0、MemFabric1.1.4。
- 两侧日志根：/home/cryang/log/mempool-min/k1-ticket05-002/。

两机仓库根目录先P后D，分别执行：

```bash
bash ascend-mempool-test/scripts/run_mempool_min.sh \
  --role prefill --mode p-sharing-acceptance \
  --config /home/cryang/mempool-min-ticket01-3132.json --run-id k1-ticket05-002
```

```bash
bash ascend-mempool-test/scripts/run_mempool_min.sh \
  --role decode --mode p-sharing-acceptance \
  --config /home/cryang/mempool-min-ticket01-3132.json --run-id k1-ticket05-002
```

## 结果与解释

fetch、target、restart全部通过；两轮各6个请求，覆盖ABCE、长生成及提前EOS。
两轮各14条ready事件，reader_counts_zero=true；equivalent_events=null保持不变。
001的第二请求分配故障在002未复现，两个完整服务轮次已越过该路径。

P主KV注册为8份，每NUMA注册1份；各P/D rank通过自身设备映射执行unidex_copy跨写者哨兵读取。
每NUMA主KV有效载荷11777605632字节（10.96875GiB），分配16106127360字节（15GiB）。
八个NUMA的分配总量120GiB，包含私有Index K、prefix及对齐等；不能把总分配量叫作主KV载荷。
物理驻留检查仍为false：注册/贡献量一致不证明OS物理页实际驻留。

| 请求 | 命中tokens | 每组偶/奇rank新写主KV行数 | 解读 |
|---|---:|---:|---|
| A | 0 | 2048 / 2048 | 4096冷请求按两半分写 |
| B | 4096 | 0 / 1024 | 命中部分由fill恢复，新后缀归后半写者 |
| C | 3968 | 0 / 128 | 最后一页重新计算，归后半写者 |
| E | 0 | 2048 / 2048 | 独立冷请求 |
| 长生成 | 0 | 2048 / 2048 | 4096冷prompt |
| 提前EOS | 0 | 128 / 0 | 单页prompt归偶rank |

超时注入的第7请求以SR heartbeat timeout触发fail-stop；保留16个rank原生预留及所有额度，未自动free。
worker_exit=137属于该故障注入场景，配套process_cleanup_verified=true，随后同端口重启通过。
本报告未直接检查完整退出日志，不将通过结果解释为MemFabric析构崩溃已被独立排除。

## 覆盖边界

保留原报告全部limitations：
- prefix内容验证使用计算源快照和恢复后的native HBM；未额外读回alive/prefix/fill DRAM。
- V3使用独立题目答案，未重启参考服务比较，也未另跑冷A/C稳定性。
- V8使用实际NPU注册的CPU容量拒绝重放，未启动独立容量故障worker。
- 物理NUMA驻留和性能未验收。
- 退出沿用SGLang父进程清理及重启检查；逐rank BM leave/uninitialize、退出后物理DRAM页数未验收。

本次只归档结果和更新进度，不修改功能源码、测试或验收门槛。
001失败记录保留；05已关闭，08的03/05依赖满足，仍等待07验收。
