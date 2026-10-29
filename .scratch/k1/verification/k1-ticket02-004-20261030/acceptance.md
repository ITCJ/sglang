# K1-ticket02：用户 NPU004 验收确认

- 日期：2026-10-30。
- 验收人：用户；agent未连接NPU运行测试。
- 本轮交付版本：`fa97a49820528882075e77a46db2cfd7f124cb59`。
- 环境：31 / `10.120.72.31` 为Prefill，32 / `10.120.72.32` 为Decode，P/D各TP16。
- 模式：`session-acceptance`；run-id：`k1-ticket02-004`。
- 配置：`/home/cryang/mempool-min-ticket01-3132.json`。
- 日志：两机 `/home/cryang/log/mempool-min/k1-ticket02-004/`。

## 用户确认

> 没问题，可以验收

用户在运行004、回传两轮启动日志后明确确认验收。依据该确认关闭02。
本地保存的是用户反馈与交付上下文；未收到004的完整summary.json或session-acceptance.json，
不将此记录表述为agent独立核验了全部原始报告。

## 接受范围

按本票最新约定，接受share=0池适配器、按副本ready/free记账、owner/member注册，
以及两轮精简功能验收中的请求内容、Graph、复用、故障、进程/端口清理与重启。
V3使用独立答案，V8使用真实注册的CPU容量拒绝重放。
退出沿用SGLang默认父进程/进程清理方法，不要求worker主动返回或逐rank BM关闭回执。
公共scheduler和P/D循环已恢复票前版本，本票最终生产代码净改动只在8个Ascend/NPU文件。

本次确认不增加物理NUMA页落点、退出后DRAM页数或性能验收结论。
NPU002/003的原始失败记录保持不变；已撤回的主动退出扩展不再构成本票验收门槛。

## 本轮命令

31：

```bash
bash ascend-mempool-test/scripts/run_mempool_min.sh \
  --role prefill --mode session-acceptance \
  --config /home/cryang/mempool-min-ticket01-3132.json \
  --run-id k1-ticket02-004
```

32：

```bash
bash ascend-mempool-test/scripts/run_mempool_min.sh \
  --role decode --mode session-acceptance \
  --config /home/cryang/mempool-min-ticket01-3132.json \
  --run-id k1-ticket02-004
```

## 关闭与依赖

K1-ticket02的State改为closed，验收项按用户确认勾选。
01已获用户验收，因此05、06、07的直接依赖均已满足；本次未启动这些票的实现。
