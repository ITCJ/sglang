---
title: Ticket26：eager swap-in 对照实验
subtitle: 三种 P/D 拓扑 × 两种请求数 × 两版，每个 kernel 100 轮
template: doc
---

## A 实验范围

只模拟 selected KV 物化，不启动模型服务。
P端初始化并保留真实BM来源；D端运行四来源复制。
Baseline固定aa5852624e15df139df7c76bb8d757341b049713。
对照版沿当前仅保留物化合并的生产代码。

每个rank、每种请求数，baseline与合并版各100轮。
Baseline每轮cache/current/P/D四次copy，合并版一次multibase。
每轮重新计算lookup输入、hit/miss索引、路由、mask及任务表。
地址表和缓冲区提前分配，一次性metadata初始化单列。

纯eager，不开Graph、observe或trace，没有copy预热轮。
第一轮也计入100轮，原始样本保留冷启动影响。
不测写回、refill、slot-map更新、attention或模型TTFT。
本实验不能替代模型服务验收。

## B 拓扑、数据与资源

| 组 | P/D各自rank数 | NUMA | 请求数B | P端BM预算 | D端BM预算 |
|---|---:|---|---|---:|---:|
| 029 | 1 | 0 | 1、16 | 11 GiB | 1 GiB |
| 030 | 2 | 0 | 1、16 | 12 GiB | 1 GiB |
| 031 | 16 | 0–7 | 1、16 | 96 GiB | 8 GiB |

预算来自现有PoolSession的分配及对齐规则，不是trace磁盘大小。
同组两种B共用支持16请求的池，每个NUMA保留一个共享副本。
请求NUMA配置不等于已经验证实际物理页驻留。

每请求top-k=2048，BF16、576维，每行1152字节。
B=1/16分别搬运2.25/36 MiB有效KV。
池保持78层page-first跨度，只初始化和测量第0层。
Prompt容量8192，Decode容量512；每个slot内反转物理页映射。

来源采用此前732576条记录的原始累计行数。
每个请求按最大余数法生成有限个任务，整数分配如下。

| 来源 | 原始累计行数 | 每请求有效行数 |
|---|---:|---:|
| HBM cache | 1206643520 | 1657 |
| P alive | 280470816 | 385 |
| D alive | 3565280 | 5 |
| HBM current | 725312 | 1 |

同一轮两版使用相同seed和逻辑选择，每轮改变地址与任务顺序。
合成选择与既有cache状态在计时外准备，源KV内容固定。
真实lookup、索引转换、页寻址和任务metadata在计时内重建。
这是复用总体来源比例，不是重放旧记录的地址局部性或请求时间序列。

## C 计时口径

| 字段 | 含义 |
|---|---|
| metadata_host_ms | metadata各区间的主机调用时间，不含copy提交函数区间 |
| metadata_device_span_ms | 主stream与copy stream上的metadata event区间之和 |
| kernels_ms | 各copy调用前后NPU event区间，逐kernel列出 |
| kernel_sum_ms | 各copy区间之和，baseline并行区间可能重叠 |
| copy_envelope_ms | 最早copy开始到最晚copy结束的跨度，含中间metadata/提交间隙 |
| total_ms | 主机从metadata开始到selected复制完成同步返回 |
| setup.metadata_init_ms | 一次性地址表、输入表和copier初始化，不计入100轮 |
| group_elapsed_ms | 同一D主机最早rank开始至最晚rank完成，单列启动偏差 |

Event区间可能包含eager主机提交间隙，不是profiler的纯kernel执行时间。
CPU metadata区间与设备执行可重叠，设备metadata总和也可能跨stream重叠。
这些分项不能相加替代总耗时；计时event本身有仪器开销。

Baseline保留hit stream与miss stream并行，current/P/D在miss stream顺序执行。
合并版使用miss stream；metadata就绪和消费者完成由event连接。
计时中间不插入同步，只在每轮完成后等待并取值。
每轮结果与独立CPU预期逐元素比较，校验和结果拷回不计时。

输入已满足绑定与已写前缀条件，fixture不运行服务状态机。
不测selected缓冲分配、不运行refill元数据或空hit兼容event。
以上为独立swap-in测量边界，不等于完整production forward耗时。

## D 实现与检查

生产功能源码：无改动；默认父进程清理路径保持不变。

| 脚本或测试 | 用途 |
|---|---|
| scripts/run_swapin_eager.py | 双机身份、baseline快照、rank启动、收集、原有进程清理 |
| scripts/swapin_worker.py | 真实BM、两版fetch接线、event计时、逐轮内容检查 |
| src/ascend_mempool/swapin_experiment.py | 比例、三组拓扑与B1/B16分开汇总 |
| tests/unit/test_swapin_eager.py | 拓扑、任务、计时、聚合和四来源对照 |

脚本从git提取baseline的copy/runtime原文，注入各自KVFetch。
共用的decode_write/indexed_copy/manager启动前校验baseline逐字一致。
复用原生hit helper和lookup ABI；lookup dtype转换也在计时内。
两侧记录Python源码身份、baseline文件哈希及SDK/kernel二进制身份。

5项CPU测试通过，含前置失败报告检查。
四来源测试前两轮停在夹具forward准备；用户授权仅补跑第3轮。
第3轮已通过四来源输出一致断言，最后一项变更metadata检查遇到BF16期望值误差。
期望值已按真实源张量修正，未进行第4轮，不能记该测试最终绿灯。

评审发现并修复runtime layout类型、lookup int32/bool ABI和失败报告遗漏。
Standards/Spec复核各0项未解决；NPU尚未运行。
CPU模拟结果不证明NPU event计时、远端BM或原生kernel正确。

029实机失败：baseline B1的expand().flatten()保留零步长。
原生copy拒绝非连续src_index，尚未得到有效性能结果。
只在微测current-copy适配层调用contiguous，不改baseline源码。
转换位于copy-start event之前，其成本计入metadata与总耗时。

新增回归测试调用真实DecodeKV.supply_current，覆盖B1/B16。
两轮内完成red/green，检查连续性、内容和无效行不变。
本次Standards/Spec复核各0未解决；NPU重试待用户执行。

## E 第一组029-r2重试命令

用户确认029失败进程已清理、设备资源可用后，先31再32。
本次仅改Python适配器，沿用已编译的kernel。
运行结束停在当前组，不自动启动030或031。
每侧自动新建目录，run-id不能重复使用。

31容器：

```bash
cd /home/cryang/sglang &&
git pull --ff-only &&
python3 -u ascend-mempool-test/scripts/run_swapin_eager.py \
  --role prefill --tp-size 1 \
  --run-id k2-ticket26-029-eager-tp1-r2 --control-port 22900
```

32容器：

```bash
cd /home/cryang/sglang &&
git pull --ff-only &&
python3 -u ascend-mempool-test/scripts/run_swapin_eager.py \
  --role decode --tp-size 1 \
  --run-id k2-ticket26-029-eager-tp1-r2 --control-port 22900
```

日志位于/home/cryang/log/k2/k2-ticket26-029-eager-tp1-r2/{prefill,decode}/。
成功标记SWAPIN_EAGER_MEASURED；报告report.json须为measured且cleanup=passed。
失败返回非零，查看report.json、rank-N.log及rank-N/failure.json。
每rank的result.json保留所有原始样本与setup时间，两侧交换报告。

## F 后续组与回传

| 组 | 参数替换 | 前置条件 |
|---|---|---|
| 030 | --tp-size 2 --run-id k2-ticket26-030-eager-tp2 --control-port 23000 | 029结果核对、用户确认清理 |
| 031 | --tp-size 16 --run-id k2-ticket26-031-eager-tp16 --control-port 23100 | 030结果核对、用户确认清理 |

每组都包含B1/B16，均使用相同100轮定义。
回传31的report.json，或先运行下列命令输出紧凑汇总。

```bash
python3 - <<'PY'
import json
from pathlib import Path
p = Path('/home/cryang/log/k2/k2-ticket26-029-eager-tp1-r2/prefill/report.json')
r = json.loads(p.read_text())
print('STATUS:', r['status'], 'CLEANUP:', r['cleanup'])
for name, data in r.get('comparison', {}).items():
    print(name, 'GROUP:', data['group_elapsed_ms'])
    for rank, metrics in data['per_rank'].items():
        print('RANK', rank, json.dumps(metrics, ensure_ascii=False))
PY
```

029首轮在B1 baseline current-copy入口失败，未产生有效测量。
029-r2尚未执行。26保持open；不自动解锁25。
