"""Build the TP2/TP4 report from user-supplied, rounded summary excerpts.

Run with system python3 (Matplotlib required). No hardware access or benchmark
execution. TP2 source is preserved verbatim; derived ratios are explicitly
separate from the ratios supplied in the excerpts.
"""
import csv
import hashlib
import json
import re
from pathlib import Path

import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
from matplotlib import font_manager
import numpy as np

ROOT = Path(__file__).resolve().parent
original = ROOT.parent / 'run-9-performance/results.csv'
(ROOT / 'tp2-results.csv').write_bytes(original.read_bytes())
SIDES = [('prefill', 'P'), ('decode', 'D')]
DATA = {}
checks = []
for width, count in [(2, 43), (4, 53)]:
    path = ROOT / f'tp{width}-results.csv'
    with path.open() as stream:
        raw = list(csv.DictReader(stream))
    assert len(raw) == count and len({r['case'] for r in raw}) == count
    DATA[width] = {r['case']: {k: float(v) for k, v in r.items() if k != 'case'} for r in raw}
    for case, values in DATA[width].items():
        for side, _ in SIDES:
            a, b, q = (values[f'{side}_{k}'] for k in ['independent_ms', 'shared_ms', 'ratio'])
            assert a > 0 and b > 0 and q > 0
            # Check overlapping rounding intervals, not exact b/a equality.
            lo, hi = (b-.00005)/(a+.00005), (b+.00005)/(a-.00005)
            assert lo <= q+.0005 and hi >= q-.0005, (width, case, side, a, b, q)
    checks.append({'tp_size': width, 'cases': count, 'side_comparisons': count*2,
                   'source_sha256': hashlib.sha256(path.read_bytes()).hexdigest()})
assert set(DATA[2]) <= set(DATA[4])

names = list(DATA[4])
READ = [c for c in names if c.startswith(('read-p-', 'read-d-', 'read-mixed-'))]
BULK = ['logical-2049-token', 'logical-2049-feature']
assert len(READ) == 12


def value(width, case, side, mode='shared'):
    return DATA[width][case][f'{side}_{mode}_ms']


def change(case, side):
    return (value(4, case, side) / value(2, case, side) - 1) * 100


def pool_change(width, case, side):
    return (DATA[width][case][side+'_ratio'] - 1) * 100


def extent(values):
    return f'{min(values):+.1f}%～{max(values):+.1f}%'


cross = {active: [change(c, s) for c in READ if c.endswith('-'+str(active)) for s, _ in SIDES] for active in [1, 16]}
pool = {w: [pool_change(w, c, s) for c in READ for s, _ in SIDES] for w in [2, 4]}
assert round(min(cross[16]), 1) == 42.4 and round(max(cross[16]), 1) == 91.6
assert round(min(pool[4]), 1) == -1.6 and round(max(pool[4]), 1) == 1.3

# A normalized data file retains the supplied ratios. A different field names
# the cross-width ratio derived from rounded P50, never an observed raw sample.
with (ROOT/'combined-results.csv').open('w', newline='') as stream:
    fields = ['tp_size', 'case', 'side', 'independent_p50_ms', 'shared_p50_ms', 'supplied_shared_over_independent', 'derived_tp4_shared_over_tp2_shared']
    writer = csv.DictWriter(stream, fieldnames=fields)
    writer.writeheader()
    for width in [2, 4]:
        for case, row in DATA[width].items():
            for side, caption in SIDES:
                writer.writerow(dict(tp_size=width, case=case, side=caption,
                    independent_p50_ms=f'{value(width,case,side,"independent"):.4f}',
                    shared_p50_ms=f'{value(width,case,side):.4f}',
                    supplied_shared_over_independent=f'{row[side+"_ratio"]:.3f}',
                    derived_tp4_shared_over_tp2_shared=f'{value(4,case,side)/value(2,case,side):.6f}' if case in DATA[2] else ''))

font_path = '/System/Library/Fonts/Hiragino Sans GB.ttc'
font_manager.fontManager.addfont(font_path)
font = font_manager.FontProperties(fname=font_path).get_name()
plt.rcParams.update({'font.family': font, 'font.size': 12, 'axes.titlesize': 15,
    'axes.unicode_minus': False, 'figure.facecolor': 'white', 'axes.facecolor': 'white',
    'axes.edgecolor': '#d7e0eb', 'text.color': '#192c46', 'axes.labelcolor': '#405570',
    'xtick.color': '#405570', 'ytick.color': '#243a56', 'svg.fonttype': 'path'})
COLORS = {2: '#467bb5', 4: '#008477'}
PALE = {2: '#c8daed', 4: '#b5dcd4'}


def plot_pair(cases, labels, title, name, max_x, height):
    fig, axes = plt.subplots(1, 2, figsize=(14.4, height), layout='constrained')
    for ax, (side, caption) in zip(axes, SIDES):
        y = np.arange(len(cases))
        ax.spines[['top', 'right', 'left']].set_visible(False)
        ax.set_axisbelow(True)
        ax.grid(axis='x', color='#e9edf2')
        ax.tick_params(length=0, pad=8)
        for width, mode, offset in [(2, 'independent', -.315), (2, 'shared', -.105),
                                    (4, 'independent', .105), (4, 'shared', .315)]:
            xs = [value(width, c, side, mode) for c in cases]
            independent = mode == 'independent'
            ax.barh(y+offset, xs, height=.175,
                    color=PALE[width] if independent else COLORS[width],
                    edgecolor=COLORS[width], linewidth=.5,
                    hatch='///' if independent else None,
                    label=f'TP{width} {"独立池" if independent else "共享池"}')
            for yy, xx in zip(y+offset, xs):
                ax.text(xx+max_x*.012, yy, f'{xx:.4f}', va='center', fontsize=10,
                        color='#405570' if independent else COLORS[width])
        ax.set(yticks=y, yticklabels=labels, ylim=(len(cases)-.4, -.75), xlim=(0, max_x),
               title=f'{caption} 侧', xlabel='组 P50（ms） · 越低越好')
        ax.legend(loc='upper center', bbox_to_anchor=(.5, -.10), ncol=2, frameon=False, fontsize=11)
    fig.suptitle(title, fontsize=18, fontweight='bold')
    for suffix in ['png', 'svg']:
        fig.savefig(ROOT/f'{name}.{suffix}', dpi=160, bbox_inches='tight')
    plt.close(fig)


plot_pair(BULK, ['2049 行 / token 分片', '2049 行 / feature 分片'],
    'C · 大块写入：每 TP 独占一池，与多个 TP 分片共享一池', '01-bulk-write', .185, 5.5)
for active, xmax in [(1, 1.1), (16, 1.8)]:
    cases = [c for c in READ if c.endswith('-'+str(active))]
    labels = []
    for c in cases:
        _, src, dist, _ = c.split('-')
        labels.append(f'{src.upper()} 来源 / {"连续" if dist=="contiguous" else "离散"}')
    plot_pair(cases, labels,
        f'D · {active} 请求：'+('两种 TP 数量、两种池模式的耗时接近' if active == 1 else '两种池模式的读取耗时均随 TP 数量增加'),
        f'0{2 if active==1 else 3}-read-{active}', xmax, 9.6)


def picture(name, alt):
    svg = (ROOT/f'{name}.svg').read_text()
    svg = svg[svg.index('<svg'):]
    # Namespace Matplotlib IDs so standalone figures coexist in one HTML page.
    for ident in set(re.findall(r'id="([^"]+)"', svg)):
        svg = svg.replace(f'id="{ident}"', f'id="{name}-{ident}"')
        svg = svg.replace(f'"#{ident}"', f'"#{name}-{ident}"')
        svg = svg.replace(f'url(#{ident})', f'url(#{name}-{ident})')
    svg = svg.replace('<svg ', f'<svg role="img" aria-label="{alt}" ', 1)
    # The skill offers no scientific chart component. Embed tool-generated SVG
    # via its documented escape hatch and standard responsive figure wrapper.
    return f'```svg\n<figure class="am-diagram">{svg}</figure>\n```\n'


def table(headers, rows):
    return '\n| '+' | '.join(headers)+' |\n| '+' | '.join(['---']+['---:']*(len(headers)-1))+' |\n'+''.join('| '+' | '.join(map(str, row))+' |\n' for row in rows)+'\n'


def compare_table(cases):
    rows = []
    for case in cases:
        for side, caption in SIDES:
            vals = [f'{value(w,case,side,m):.4f}' if case in DATA[w] else '不适用' for w in [2,4] for m in ['independent','shared']]
            rows.append([f'{caption} / `{case}`', *vals, f'{change(case,side):+.1f}%' if case in DATA[2] else '不适用'])
    return table(['消费者 / 用例', 'TP2 独立 ms', 'TP2 共享 ms', 'TP4 独立 ms', 'TP4 共享 ms', '共享 TP4 较 TP2'], rows)


md = '''---
template: doc
theme: shadcn
mode: light
lang: zh
title: TP2 × TP4：共享 NUMA 性能对照
subtitle: 每 TP 独占一池 × TP2 共享 × TP4 共享 · Graph / block_dim 48
source: 用户回传 TP2 与 TP4 汇总摘录 · 2026-10-13
---
**先看共享池从 2 个 TP 增至 4 个 TP 后的耗时变化。**
再用各自的独立池判断池模式差异。

## A 三个最重要的结果
'''
md += table(['关注点', 'TP2 共享 → TP4 共享', '解读'], [
    ['2049 行 token 写入', 'P：0.0459 → 0.0447 ms；D：0.0454 → 0.0442 ms', '两侧耗时均约下降 2.6%，接近持平'],
    ['1 请求并发读取', extent(cross[1]), '12 个两侧对照的耗时接近'],
    ['16 请求并发读取', extent(cross[16]), 'reader 数量翻倍；合计读取量也翻倍']])
md += '''```callout info 怎样解释读写差异
共享写入由多个 TP 分工完成一份 KV。
共享读取要求每个 TP 都读到完整 KV。
因此，增加 TP 数量不会减少读取总量。
```
'''
md += f'''TP4 读路径的共享／独立池耗时变化为 **{extent(pool[4])}**。
跨 TP 数量的读延迟上升，不能直接归因于共享池。

## B 比较的对象与计时口径
```callout info 本次补入的“单 TP 占用一个 mempool”
独立池模式中，每个 TP 独占一个 mempool。
TP2 独立池是 2 个 TP 同时访问各自的池。
TP4 独立池是 4 个 TP 同时访问各自的池。
它们不是整台机器只有 1 个 TP 工作的 TP1 实验。
```
'''
md += table(['配置', 'TP2', 'TP4'], [
    ['每侧设备', 'NPU 0 / 1', 'NPU 0 / 1 / 2 / 3'],
    ['目标 NUMA', '0', '0'],
    ['图与 kernel 配置', 'Graph / block_dim=48', 'Graph / block_dim=48'],
    ['P / D 容量（tokens）', '8192 / 16384', '16384 / 16384'],
    ['池层数 / slots', '2 / 16', '4 / 16'],
    ['KV 类型 / 特征维度 / top-k', 'BF16 / 576 / 2048', 'BF16 / 576 / 2048'],
    ['数据来源', 'run-9-resume 汇总；复用前两轮并补第三轮', '本次 summary.json 与 decode-summary.json 摘录'],
    ['摘录范围', '43 用例 × P/D', '53 用例 × P/D；selected_component']])
md += '''配置按历史交付记录和本次命令默认值列出。
本页没有重新读取 NPU 主机上的原始配置文件。
两次运行的池容量和层数不同，不能视为严格固定配置的扩展效率实验。

**组 P50**：取同侧各 TP 最早开始到最晚完成的包络。
每批耗时除以 repeats，再汇总有效批次的 P50。
它包含主机提交偏差，不是纯 kernel 时长，也不是模型 TPOT。

**P / D 是消费者所在主机。** `read-p` / `read-d` 是 KV 来源。
P 读 P、D 读 D 为本地读；相反方向为远端读。
每个读用例会让目标消费侧的所有 TP 同时读取。

图中蓝色为 TP2，绿色为 TP4。
浅色斜线为独立池，深色实心为共享池。
图表均以毫秒为单位，越低越好。
跨 TP 变化由四位小数 P50 计算，属于近似值。
同一 TP 数量内的池模式变化使用用户提供的三位小数比值。

## C 大块写入：增加 writer，去重收益更大
'''
md += picture('01-bulk-write', '2049行大块写入：TP2独立、TP2共享、TP4独立、TP4共享的组P50')
md += '''**等逻辑任务始终是 2049 行 KV。**
独立模式中，每个 TP 各写一份。
共享模式中，各 TP 分片写完一份。
token 分片分配不同的行；feature 分片分配不同的特征列。
两种分片都让多个 writer 同时执行。

独立池总计写 TP 数量份 KV，共享池总计写一份 KV。
因此，独立池与共享池的等逻辑测试并非等字节写入。
'''
md += compare_table(BULK)
md += table(['分片 / 消费者', 'TP2 共享较独立', 'TP4 共享较独立'], [
    [f'{case.rsplit("-",1)[1]} / {cap}', f'{pool_change(2,case,side):+.1f}%', f'{pool_change(4,case,side):+.1f}%']
    for case in BULK for side, cap in SIDES])
md += '''**token 是本批大块写入的优先候选。**
TP4 的 token 写入耗时与 TP2 接近。
feature 在 TP4 中则增加约 9.3%～11.5%。
共享相对独立的收益扩大，支持减少重复写入的解释。
这些结果不代表小于 2049 行的写入也有相同收益。

**等字节对照：同一 TP 数量内，两种池的 copy 字节量相同。**
'''
md += table(['宽度 / 消费者', '独立 ms', '共享 ms', '共享 / 独立'], [
    [f'TP{w} / {cap}', f'{value(w,"equal_bytes-2049-token",s,"independent"):.4f}',
     f'{value(w,"equal_bytes-2049-token",s):.4f}', f'{DATA[w]["equal_bytes-2049-token"][s+"_ratio"]:.3f}']
    for w in [2,4] for s,cap in SIDES])
md += '''两种 TP 数量的等字节对照都接近持平。
这不能证明共享池提高了物理链路带宽。
跨宽度时，TP2 合计写 2049×2 行，TP4 合计写 2049×4 行。
因此，不把这组绝对耗时变化当作同工作量的退化。

**layer 分片只作为参考。**
每个计时用例只写一层，由对应 layer owner 执行。
它不代表多个 writer 同时写同一层。
'''
md += compare_table(['logical-2049-layer'+suffix for suffix in ['', '1', '2', '3']])
md += '''## D 并发读取：单请求持平，16 请求随 TP 数增加变慢
多个 TP 读取同一份逻辑 selected KV，每个 TP 都获得完整结果。
共享池复用存储，不会把一次读取广播给其他 TP。
独立模式则从各自池中的等价副本读取。

1 / 16 表示活跃请求数，不是 TP 数或 token 数。
每个请求选择 2048 行、每行 576 个 BF16 元素。
'''
md += table(['活跃请求数', '每个 TP 读取量', 'TP2 合计', 'TP4 合计'], [
    ['1', '2.25 MiB', '4.50 MiB', '9.00 MiB'], ['16', '36 MiB', '72 MiB', '144 MiB']])
md += '''这里统计应用请求的 copy 字节量，不是物理总线流量。
'''
md += picture('02-read-1', '1请求读取：TP2和TP4的独立池与共享池，共四组对照')
md += f'**1 请求：共享 TP4 较 TP2 变化为 {extent(cross[1])}。**\n\n'
md += picture('03-read-16', '16请求读取：TP2和TP4的独立池与共享池，共四组对照')
md += f'**16 请求：共享 TP4 较 TP2 变化为 {extent(cross[16])}。**\n\n'
md += '''**下面用独立池检验：共享模式本身是否明显变慢。**
'''
md += table(['比较范围', 'TP2 共享较独立', 'TP4 共享较独立'], [
    [f'{active} 请求；P/D 合计 12 个对照',
     extent([pool_change(2,c,s) for c in READ if c.endswith('-'+str(active)) for s,_ in SIDES]),
     extent([pool_change(4,c,s) for c in READ if c.endswith('-'+str(active)) for s,_ in SIDES])]
    for active in [1,16]])
md += '''TP4 中，共享与独立的读取耗时仍很接近。
这批数据没有显示明显的共享池专属读延迟惩罚。
reader 数量和合计工作量增加，与容量变化同时发生。
仅凭这些 P50，不能定位具体的带宽或调度瓶颈。

**读取完整对照表。** 同名用例、同一消费者逐项比较。
'''
md += compare_table(READ)
md += '''**补充：只有一个 TP 发起读写的已有用例。**
这些 single 用例只启用指定成员，其他成员不执行该项读写。
下表使用独立池结果，即指定 TP 访问其独占的 mempool。
它仍处于 TP2 / TP4 测试组中，不是独立部署的 TP1 实验。

单 writer 用例只写 **17 行**。
单 reader 用例读 **1 请求、2048 行、混合来源、离散索引**。
不能把它们冒充 2049 行大块单 writer 或 16 请求单 reader 结果。
计时仍是组包络，不是指定成员的纯设备耗时。
'''
md += table(['测试组 / 指定成员', 'P 单 writer ms', 'D 单 writer ms',
             'P 单 reader ms', 'D 单 reader ms'], [
    [f'TP{w} / member-{member}',
     *[f'{value(w, f"{op}-single-{member}", side, "independent"):.4f}'
       for op in ['logical', 'read'] for side, _ in SIDES]]
    for w in [2,4] for member in range(w)])
md += '''## E 结果边界与当前建议
```callout info 当前建议
保留共享池大块 token 分片写入方案。
4 个 TP 读取同一份 KV 时，按 4 份读取量规划负载。
16 请求的组耗时已有上升，不直接宣称 TP4 具有线性扩展能力。
```
```callout warn 仍是组件结果，NUMA 验收未完成
两组状态均为 component_results_pending_numa。
目标节点设为 NUMA 0，不等于已证实实际物理页归属。
本页只汇总 Graph / block_dim=48，不扩大覆盖范围。
```
本页没有原始采样、P95、逐轮离散度或总线计数器。
小幅 P50 差异不能证明统计显著性或性能等价。
不将不同工作量的用例平均为一个总收益。
这份组件结果也不能替代 SGLang 服务端的性能验收。

## F 全量数值附录
保留其他模块的数据，便于追溯。
下列比值均为共享池 P50 / 独立池 P50，低于 1 表示共享更快。
TP4 独有的 layer2/3 和单 rank 用例没有对应 TP2 值。
这些用例不参与跨宽度计算。
'''
for width in [2,4]:
    md += f'\n**TP{width}：每侧 {len(DATA[width])} 项。**\n\n'
    md += table(['用例', 'P 独立 ms', 'P 共享 ms', 'P 比值', 'D 独立 ms', 'D 共享 ms', 'D 比值'], [
        [f'`{case}`', *[f'{r[s+"_"+k]:.{3 if k=="ratio" else 4}f}' for s,_ in SIDES for k in ['independent_ms','shared_ms','ratio']]]
        for case,r in DATA[width].items()])

(ROOT/'report.md').write_text(md)
(ROOT/'derived.json').write_text(json.dumps({'sources': checks,
    'cross_width_shared_read_change_percent': cross, 'within_width_read_pool_change_percent': pool,
    'cross_width_shared_bulk_change_percent': {c: {s: change(c,s) for s,_ in SIDES} for c in BULK},
    'input_side_comparisons': 192, 'matched_case_count': len(DATA[2]),
    'rounding_interval_checks_passed': True}, ensure_ascii=False, indent=2)+'\n')
print(json.dumps({'checks': checks, 'read_1': extent(cross[1]), 'read_16': extent(cross[16]),
                  'TP4_pool_read': extent(pool[4]), 'draft': str(ROOT/'report.md')}, ensure_ascii=False, indent=2))
