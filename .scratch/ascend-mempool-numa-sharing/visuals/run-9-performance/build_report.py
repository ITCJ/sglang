"""Reproduce figures and the am Markdown draft from the user-provided excerpt."""
import csv
import json
from pathlib import Path

import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
from matplotlib import font_manager
import numpy as np

ROOT = Path(__file__).resolve().parent
rows = list(csv.DictReader((ROOT / 'results.csv').open()))
assert len(rows) == 43 and len({r['case'] for r in rows}) == 43
for row in rows:
    for k in list(row)[1:]:
        row[k] = float(row[k])
index = {r['case']: r for r in rows}
font_manager.fontManager.addfont('/System/Library/Fonts/Hiragino Sans GB.ttc')
font_name = font_manager.FontProperties(fname='/System/Library/Fonts/Hiragino Sans GB.ttc').get_name()
plt.rcParams.update({
    'font.family':font_name, 'font.size':12, 'axes.titlesize':16,
    'axes.labelsize':12, 'axes.unicode_minus':False,
    'figure.facecolor':'#ffffff', 'axes.facecolor':'#ffffff',
    'axes.edgecolor':'#d5dce5', 'axes.labelcolor':'#354256',
    'text.color':'#17253b', 'xtick.color':'#526176', 'ytick.color':'#354256',
    'savefig.facecolor':'#ffffff', 'svg.fonttype':'path',
})
BLUE, TEAL, RED = '#4878b5', '#078477', '#c75537'


def setup(ax):
    ax.spines[['top','right','left']].set_visible(False)
    ax.set_axisbelow(True)
    ax.grid(axis='x', color='#e9edf2')
    ax.tick_params(axis='both', length=0, pad=8)


def save(fig, name):
    fig.savefig(ROOT / f'{name}.png', dpi=180, bbox_inches='tight')
    fig.savefig(ROOT / f'{name}.svg', bbox_inches='tight')
    plt.close(fig)

# Figure 1: same logical task compared against fixed request bytes.
fig, axs = plt.subplots(1, 2, figsize=(12.4, 4.8), layout='constrained')
selected = ['logical-2049-token','logical-2049-feature','logical-2049-layer',
            'logical-2049-layer1','equal_bytes-2049-token']
labels = ['等逻辑 / token 分片','等逻辑 / feature 分片','等逻辑 / layer 0',
          '等逻辑 / layer 1','等字节 / token 对照']
for ax, side, title in zip(axs, ['prefill','decode'], ['P 侧','D 侧']):
    setup(ax)
    vals = [index[c][side+'_ratio'] for c in selected]
    y = np.arange(len(vals))
    ax.barh(y, vals, height=.56, color=[TEAL]*4+[BLUE])
    ax.axvline(1, color='#546579', linestyle='--', linewidth=1)
    for yi, v in zip(y, vals): ax.text(v+.02, yi, f'{v:.3f}  ({(v-1)*100:+.1f}%)', va='center', fontsize=11)
    ax.set(yticks=y, yticklabels=labels if side=='prefill' else [],
           xlim=(0,1.28), ylim=(4.6,-.65), title=title,
           xlabel='共享池 P50 / 独立池 P50  ·  越低越好')
    ax.set_xticks([0,.25,.5,.75,1])
fig.suptitle('大块写入：等逻辑任务省时，等字节任务基本持平', fontsize=18, fontweight='bold')
save(fig, '01-bulk-write')

# Figure 2: every dual-reader case; never merge incomparable workloads.
fig, ax = plt.subplots(figsize=(12.4,6.5),layout='constrained')
setup(ax)
read_names = [r['case'] for r in rows if r['case'].startswith(('read-p-','read-d-','read-mixed-'))]
labels=[]
for case in read_names:
    _, src, dist, active=case.split('-')
    labels.append(f'{src.upper():5s} / {"连续" if dist=="contiguous" else "离散"} / {active} 请求')
y=np.arange(len(read_names))
for side, off, color, marker, label in [('prefill',-.13,BLUE,'o','P 侧'),('decode',.13,TEAL,'s','D 侧')]:
    xs=np.array([(index[c][side+'_ratio']-1)*100 for c in read_names])
    ax.scatter(xs,y+off,s=55,c=color,marker=marker,label=label,zorder=3)
    for v,yi in zip(xs,y+off):ax.text(v+.10,yi,f'{v:+.1f}%',ha='left',va='center',fontsize=10,color=color)
ax.axvline(0,color='#526176',linestyle='--',linewidth=1)
ax.set(yticks=y,yticklabels=labels,ylim=(11.6,-.7),xlim=(-2.0,4.15),
       xlabel='共享池相对独立池的组 P50 变化（%） · 左侧更快',
       title='双 reader：24 个对照的变化范围为 -1.3% 至 +3.3%')
ax.legend(loc='lower right',frameon=False,ncol=2)
save(fig, '02-reader-change')

# Figure 3: cross-strategy comparison inside the same pool and side.
fig,axs=plt.subplots(1,2,figsize=(12.4,5.0),layout='constrained')
parallel=[]
for ax,side,title in zip(axs,['prefill','decode'],['P 侧','D 侧']):
    setup(ax)
    names=[]; serial=[]; par=[]
    for mode,caption in [('independent','独立池'),('shared','共享池')]:
        for active in [1,16]:
            a=index[f'current_serial-{active}'][f'{side}_{mode}_ms']
            b=index[f'current_parallel-{active}'][f'{side}_{mode}_ms']
            serial.append(a);par.append(b);names.append(f'{caption} / {active} 请求')
            parallel.append(dict(side=side,mode=mode,active=active,serial_ms=a,parallel_ms=b,increase_percent=(b/a-1)*100))
    y=np.arange(4)
    ax.barh(y-.17,serial,height=.29,color=BLUE,label='current_serial')
    ax.barh(y+.17,par,height=.29,color=RED,label='current_parallel')
    for yi,a,b in zip(y,serial,par):
        ax.text(a+.012,yi-.17,f'{a:.4f}',fontsize=10,va='center')
        ax.text(b+.012,yi+.17,f'{b:.4f}  +{(b/a-1)*100:.1f}%',fontsize=10,va='center')
    ax.set(yticks=y,yticklabels=names,xlim=(0,1.51),ylim=(3.6,-.8),title=title,xlabel='组 P50（ms） · 越低越好')
    ax.legend(loc='upper center',bbox_to_anchor=(.5,-.16),ncol=2,fontsize=10,frameon=False)
fig.suptitle('当前实现中，并行填充没有降低整组完成耗时',fontsize=18,fontweight='bold')
save(fig, '03-current-strategies')

# Figure 4: production materialization seam by cache hit rate.
fig,axs=plt.subplots(1,2,figsize=(12.4,4.6),layout='constrained')
for ax,side,title in zip(axs,['prefill','decode'],['P 侧','D 侧']):
    setup(ax)
    y=np.arange(3)
    for mode,offset,color,label in [('independent',-.16,BLUE,'独立池'),('shared',.16,TEAL,'共享池')]:
        vals=[index[f'materialize-{h}'][f'{side}_{mode}_ms'] for h in [0,50,100]]
        ax.barh(y+offset,vals,height=.28,color=color,label=label)
        for yi,v in zip(y+offset,vals):ax.text(v+.015,yi,f'{v:.4f}',fontsize=11,va='center')
    ax.set(yticks=y,yticklabels=['命中率 0%','命中率 50%','命中率 100%'],xlim=(0,1.98),ylim=(2.6,-.65),title=title,xlabel='组 P50（ms） · 越低越好')
    ax.legend(loc='lower right',frameon=False,fontsize=10)
fig.suptitle('Materialize：池模式差异小，缓存命中率影响更大',fontsize=18,fontweight='bold')
save(fig,'04-materialize')

# Export calculated evidence for reproducibility, without inventing raw samples.
calc=dict(
    bulk_latency_reduction_percent=[(1-index[c][s+'_ratio'])*100 for c in selected[:4] for s in ['prefill','decode']],
    read_change_percent=[(index[c][s+'_ratio']-1)*100 for c in read_names for s in ['prefill','decode']],
    current_strategy=parallel,
)
(ROOT/'derived.json').write_text(json.dumps(calc,ensure_ascii=False,indent=2)+'\n')


def picture(name, alt):
    # The skill has no scientific-chart component. Embed the plotting tool's
    # generated SVG through its documented escape hatch, using its own responsive
    # diagram wrapper. No handwritten layout, colors or SVG coordinates.
    import re
    svg = (ROOT / f'{name}.svg').read_text()
    svg = svg[svg.index('<svg'):]
    for identifier in set(re.findall(r'id="([^"]+)"', svg)):
        svg = svg.replace(f'id="{identifier}"', f'id="{name}-{identifier}"')
        svg = svg.replace(f'"#{identifier}"', f'"#{name}-{identifier}"')
        svg = svg.replace(f'url(#{identifier})', f'url(#{name}-{identifier})')
    svg = svg.replace('<svg ', f'<svg role="img" aria-label="{alt}" ', 1)
    return f'```svg\n<figure class="am-diagram">{svg}</figure>\n```\n'


def table(rows):
    text='| 用例 | P 独立 ms | P 共享 ms | P 比值 | D 独立 ms | D 共享 ms | D 比值 |\n'
    text+='|---|---:|---:|---:|---:|---:|---:|\n'
    for r in rows:
        text+=f"| `{r['case']}` | {r['prefill_independent_ms']:.4f} | {r['prefill_shared_ms']:.4f} | {r['prefill_ratio']:.3f} | {r['decode_independent_ms']:.4f} | {r['decode_shared_ms']:.4f} | {r['decode_ratio']:.3f} |\n"
    return text

md='''---
template: doc
theme: shadcn
mode: light
lang: zh
title: NUMA 共享池：性能结果解读
subtitle: 大块写入受益于去重；读路径基本持平；并行填充仍需优化
source: 用户回传 · run-9-resume · 2026-10-13
---
这份报告分析 Graph、block_dim=48 的组件结果。P／D 各 43 项，共 86 个池模式对照。

## A 先看结论
```callout ok 已观察到的收益
2049 行等逻辑写入的组 P50 下降 **37.2%～42.0%**。
相同逻辑工作量下，相当于约 **1.59～1.72 倍**的完成速率。
等字节写入基本持平，支持收益主要来自减少重复写入。
```

| 关注点 | 这批数据说明什么 | 当前判断 |
|---|---|---|
| 大块写入 | 两侧各四种分片对照都降低耗时 | ok 保留共享写入候选 |
| 双 reader | 24 个对照变化在 −1.3%～+3.3% | 组 P50 接近，尾延迟待查 |
| current 并行 | 同池内比串行慢约 19.8%～62.4% | warn 不据此宣称并行加速 |
| materialize | 共享／独立比值为 0.989～1.015 | 池模式差异较小 |
| 验收状态 | 组件采集完成，NUMA 归属仍待确认 | warn 尚未完成最终验收 |

读路径、写路径的字节量不同，不把所有用例平均成一个“总收益”。

## B 数据范围与读图口径
```kv cols=2
运行模式: Graph · block_dim=48
本机设备: 每侧 NPU 0／1 · 请求 NUMA node 0
P 主机: 10.120.72.31 · prefill
D 主机: 10.120.72.32 · decode
轮次来源: 复用 run-8 前两轮；run-9 补第三轮
当前状态: component_results_pending_numa
```

**图表均使用组 P50，单位为毫秒。** 它是同机两个 rank 的完成包络。
每批包络除以 repeats，再取有效批次的 P50。
主机提交和启动偏差会影响该指标。它不是纯 kernel 时长，也不是模型 TPOT。

**池模式比值＝共享池 P50 ÷ 独立池 P50。** 比值低于 1 表示共享更快。
百分比变化＝（比值 − 1）×100%。负值表示耗时下降。

P／D 表示消费者所在主机。`read-p`／`read-d` 表示读取哪一侧的 KV。
因此，P 侧读 P、D 侧读 D 是本地读。相反方向是远端读。

本页只使用用户粘贴的四位小数 P50 和三位小数比值。
池模式变化优先使用原表比值。同池串并行比值由已舍入的 P50 计算。
没有 P95、逐轮离散度、selected-ready 数值或总线计数器。
不能由这些数字判断统计显著性或物理带宽上限。

## C 大块写入：去重收益最明确
'''
md+=picture('01-bulk-write','2049 行写入的共享池与独立池组 P50 比值；虚线1为持平')
md+='''**等逻辑任务**：独立池的每个 rank 各写一份。
共享池的两个 rank 分片写入一份。逻辑结果相同，合计写入量减少。

**等字节任务**：两种池保持相同的 copy 请求字节量。
2049 行的比值为 P **1.005**、D **1.006**，基本持平。
这组对照支持“少写一份”解释，不能证明共享池的物理链路带宽更高。

| 2049 行分片方式 | P 组耗时下降 | D 组耗时下降 |
|---|---:|---:|
| token | 41.5% | 42.0% |
| feature | 37.2% | 37.6% |
| layer 0 | 38.2% | 37.8% |
| layer 1 | 37.7% | 37.9% |

**token 是本批大块写入的优先候选。** 它在两侧的 P50 都最低。
1 行、17 行结果方向不一致，不能把大块收益外推到每个 decode step。
例如 D 的 `equal_bytes-17-token` 增加 11.8%。

## D 双 reader：共享池未显示系统性读延迟恶化
'''
md+=picture('02-reader-change','全部12种双reader负载的P和D变化；以0为中心，负值更快')
md+='''图中包含本地、远端、mixed，以及连续／离散索引。
每侧覆盖 1 请求和 16 请求。24 个对照的变化范围为 **−1.3%～+3.3%**。
最大上升来自 P 的 `read-mixed-contiguous-16`：0.8005 → 0.8270 ms。
这些是 P50 差异，不是统计等价证明，也不套用服务验收的 3% 门槛。

1 请求连续读中，共享池的本地／远端差异更明显：

| 消费者 | 本地读 ms | 远端读 ms | 远端／本地 |
|---|---:|---:|---:|
| P | 0.3542 | 0.8335 | 约 2.35 倍 |
| D | 0.3519 | 0.8416 | 约 2.39 倍 |

这个例子说明读取位置影响很大。不能把上述倍数外推到所有 batch。
单 reader 结果在全量表中保留。当前摘录不足以计算双 reader 的带宽效率。

## E current 并行：看同池内的策略差异
'''
md+=picture('03-current-strategies','相同池模式下current_serial与current_parallel的组P50；并行在8个对照中都更慢')
md+='''这一图的比较对象是 **同一池中的并行与串行**。
它与“共享池／独立池”的比较不同。
两个池、两个消费者侧和两种请求数，共八个对照。
并行路径的组 P50 全部更高，增加约 **19.8%～62.4%**。

| 共享池场景 | 串行 ms | 并行 ms | 并行／串行 |
|---|---:|---:|---:|
| P · 1 请求 | 0.6276 | 0.7879 | 约 1.255 |
| P · 16 请求 | 0.8026 | 1.0898 | 约 1.358 |
| D · 1 请求 | 0.6272 | 0.9876 | 约 1.575 |
| D · 16 请求 | 0.7435 | 1.1154 | 约 1.500 |

另一个独立现象：P 的 `current_parallel-16` 在共享池中慢 **15.0%**。
但 D 的同项共享／独立比值为 **0.997**，没有同方向变化。

**现阶段先保留串行作为组件基线。** 这不代表并行路径没有潜力。
组完成包络包含 writer 收尾，无法判断 selected 是否提前可用。
下一步先核对已有 selected-ready 统计、逐轮数据和 trace。
再区分主机提交、stream 调度、同步和资源争用的影响。

## F Materialize：命中率效应大于池模式差异
'''
md+=picture('04-materialize','16请求的0、50、100百分比缓存命中率materialize组P50')
md+='''`materialize-0/50/100` 的后缀表示预置缓存命中率。
这三项都使用 16 请求，经过生产 cache materialize 接口。
组包络包含 fixture 每次调用前恢复缓存状态的开销。
因此它不等于线上完整 attention 时延。

同一命中率下，共享／独立比值为 **0.989～1.015**。
对共享池，从 0% 命中升到 100% 命中：
P 的 P50 约下降 **39.7%**，D 约下降 **50.4%**。
该对照改变了负载，不能把这部分收益归因于共享池。

## G 全量表：43 项 × 两侧
所有数值保留用户原始精度。每个“比值”都是共享／独立。
不按比值重排，保留原始用例顺序，便于核对日志。

'''
md+=table(rows)
md+='''
用例后缀说明：`logical/equal_bytes` 后的数字是写入行数。
`read` 和组合路径的 1／16 是有效请求数。
`feature/token/layer` 是写入分片方式；`layer1` 是另一层的检查。

`single-0/1` 是单访问者对照。`zero-valid` 是有效项为零的开销参考。
它们都不能代替真实有效负载的吞吐。

## H 当前决策与待补证据
```callout info 先分析已有报告，不重跑整套
保留共享池和大块 token 分片作为后续候选。
保留 current 串行基线，再诊断并行路径的开销。
这些是组件层判断，尚未解锁真实服务性能验收。
```

| 待核对项 | 目的 | 已有证据位置 |
|---|---|---|
| 各轮 P50／P95、对齐样本数量 | 判断波动和跨运行合并的影响 | 两侧 summary 的 comparison 与 groups |
| 每 rank device 与 selected 时间 | 分开看组收尾和 selected 可用时间 | comparison 的 per_rank 字段 |
| current 与发布 trace | 判断依赖和重叠，核对时序 | 新目录及复用源目录中的 trace |
| eager 与 block_dim=24 | 检查结论是否依赖 Graph／核心数 | 原 summary 的其他 comparison 项 |
| NUMA 归属证据 | 完成物理落点验收 | physical_allocations 与 SDK 分配证据 |

状态 `COMPONENT_RESULTS_PENDING_NUMA` 表示组件采集完成。
`numa-node=0` 是配置请求，本身不是物理页归属证明。
此前 `/proc` 的 driver 映射没有提供节点驻留计数。
本页不把未知落点判成错误落点，也不把 pending 改成验收通过。

**数据来源**：用户回传的 P／D `summary` 摘录。
前两轮来自 `run-8-full`；第三轮来自 `run-9-resume`。
合并数据跨两次运行，不声称它们是一次连续测量。

**代码口径**：`run_numa_sharing.py::summarize` 与 `sharing.py::group_sample`。
用例含义来自 `sharing_cases.py`、`sharing_copy.py`、`sharing_pipeline.py`。
本页未直接读取 NPU 机器上的原始 summary 或 trace。
'''
(ROOT/'report.md').write_text(md)
print(json.dumps(dict(rows=len(rows), charts=4, parallel_range=[min(r['increase_percent'] for r in parallel),max(r['increase_percent'] for r in parallel)]), ensure_ascii=False))
