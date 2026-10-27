"""Render the user's complete C/D CSV as reproducible figures and an am draft.

Only content and standard Matplotlib charts live here. Page layout is supplied
by the answer-me-with-html CLI. No performance data are synthesized.
"""

import csv
import hashlib
import json
import math
import re
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
from matplotlib import font_manager

ROOT = Path(__file__).resolve().parent
DATA = ROOT / "combined-results.csv"
rows = list(csv.DictReader(DATA.open()))
integer_columns = (
    "side", "tp_size", "batches", "copy_bytes_per_call", "logical_bytes_per_call"
)
for row in rows:
    for key in row:
        if key in integer_columns:
            row[key] = int(row[key])
        elif key == "numa_verified":
            row[key] = row[key] == "True"
        elif key not in ("case", "topology"):
            row[key] = float(row[key])

TOPS = ["1-I0", "2-I0", "2-S0", "2-ID", "4-I0", "4-S0", "4-ID"]
SOURCES = [("local", "本机 KV"), ("remote", "另一主机 KV"), ("mixed", "两侧各一半")]
index = {(r["side"], r["topology"], r["case"]): r for r in rows}
assert len(rows) == len(index) == 162
assert all(r["batches"] == 60 and not r["numa_verified"] for r in rows)
for side in (0, 1):
    for top in TOPS:
        cases = {f"write-logical-{n}" for n in (2048, 8192, 32768)}
        if top != "1-I0":
            cases |= {f"write-equal_total-{n}" for n in (2048, 8192, 32768)}
        cases |= {f"read-{src}-random-{n}" for src, _ in SOURCES for n in (1, 16)}
        assert cases == {r["case"] for r in rows if r["side"] == side and r["topology"] == top}

for r in rows:
    case, n = r["case"], r["tp_size"]
    logical = int(case.rsplit("-", 1)[1]) * 1152
    if case.startswith("read-"):
        logical *= 2048
    copies = n if case.startswith("read-") or (
        case.startswith("write-logical") and "-S" not in r["topology"]
    ) else 1
    assert r["logical_bytes_per_call"] == logical
    assert r["copy_bytes_per_call"] == logical * copies
    assert 0 < r["group_ms_p50"] <= r["group_ms_p95"]
    assert math.isclose(r["copy_payload_gbps_p50"] / r["logical_gbps_p50"], copies)


def row_at(side, top, case):
    return index[side, top, case]


def value(side, top, case, field="group_ms_p50"):
    return row_at(side, top, case)[field]


def pair(top, case, field="group_ms_p50", precision=4):
    return " / ".join(f"{value(s, top, case, field):.{precision}f}" for s in (0, 1))


def table(headers, body):
    return (
        "| " + " | ".join(headers) + " |\n"
        + "| " + " | ".join("---" for _ in headers) + " |\n"
        + "".join("| " + " | ".join(map(str, r)) + " |\n" for r in body)
        + "\n"
    )


font_path = "/System/Library/Fonts/Hiragino Sans GB.ttc"
font_manager.fontManager.addfont(font_path)
font_name = font_manager.FontProperties(fname=font_path).get_name()
plt.rcParams.update({
    "font.family": font_name,
    "font.size": 12,
    "axes.titlesize": 15,
    "axes.labelsize": 12,
    "axes.unicode_minus": False,
    "figure.facecolor": "#ffffff",
    "axes.facecolor": "#ffffff",
    "savefig.facecolor": "#ffffff",
    "axes.edgecolor": "#d6dee8",
    "text.color": "#1b2b40",
    "axes.labelcolor": "#3f5268",
    "xtick.color": "#52657b",
    "ytick.color": "#314459",
    "svg.fonttype": "path",
    "svg.hashsalt": "numa-cd-v1-resume2",
})
COLORS = ["#3267ad", "#008979"]


def setup(ax):
    ax.spines[["top", "right", "left"]].set_visible(False)
    ax.set_axisbelow(True)
    ax.grid(axis="x", color="#e8edf3")
    ax.tick_params(axis="both", length=0, pad=7)


def save(fig, name):
    fig.savefig(ROOT / f"{name}.svg", bbox_inches="tight", metadata={"Date": None})
    fig.savefig(ROOT / f"{name}.png", dpi=170, bbox_inches="tight")
    plt.close(fig)


def write_figure(name, equal_total=False):
    fig, ax = plt.subplots(figsize=(10, 6.1), layout="constrained")
    setup(ax)
    y = np.arange(len(TOPS))
    xmax = 0
    for side, offset in ((0, -.17), (1, .17)):
        cs = ["write-logical-32768" if t == "1-I0" or not equal_total
              else "write-equal_total-32768" for t in TOPS]
        p50 = [value(side, t, c) for t, c in zip(TOPS, cs)]
        p95 = [value(side, t, c, "group_ms_p95") for t, c in zip(TOPS, cs)]
        xmax = max(xmax, max(p95))
        ax.barh(y + offset, p50, height=.28, color=COLORS[side], label=["P 侧", "D 侧"][side])
        ax.hlines(y + offset, p50, p95, color="#26394d", lw=1.3)
        ax.scatter(p95, y + offset, marker="|", s=40, color="#26394d", zorder=3)
        for yi, median, tail in zip(y + offset, p50, p95):
            ax.annotate(f"{median:.4f}", (tail, yi), xytext=(6, 0),
                        textcoords="offset points", va="center", fontsize=12)
    labels = ["1-I0  ·  1 池 / node 0", "2-I0  ·  2 池 / 0,0",
              "2-S0  ·  1 池 / node 0", "2-ID  ·  2 池 / 0,2",
              "4-I0  ·  4 池 / 全部 node 0", "4-S0  ·  1 池 / node 0",
              "4-ID  ·  4 池 / 0,2,4,6"]
    ax.set(yticks=y, yticklabels=labels, ylim=(6.65, -.75), xlim=(0, xmax * 1.24),
           xlabel="整组 P50（ms） · 越短越快；细线端点为 P95")
    for sep in (.5, 3.5):
        ax.axhline(sep, color="#dce4ed", lw=.8, linestyle="--")
    ax.legend(loc="lower right", ncol=2, frameon=False)
    ax.set_title("等拷贝总量：整组都写 36 MiB" if equal_total
                 else "同一逻辑任务：36 MiB KV 更新", loc="left", pad=15, fontweight="bold")
    save(fig, name)


def read_figure(batch, name):
    # Shared axes make source and topology differences directly comparable.
    fig, axs = plt.subplots(1, 3, figsize=(11.3, 5.8), sharey=True, layout="constrained")
    scale = 1000 if batch == 1 else 1
    y = np.arange(len(TOPS))
    xmax = max(value(s, t, f"read-{src}-random-{batch}", "group_ms_p95") * scale
               for s in (0, 1) for t in TOPS for src, _ in SOURCES)
    for ax, (src, title) in zip(axs, SOURCES):
        setup(ax)
        case = f"read-{src}-random-{batch}"
        for side, off in ((0, -.17), (1, .17)):
            p50 = [value(side, t, case) * scale for t in TOPS]
            p95 = [value(side, t, case, "group_ms_p95") * scale for t in TOPS]
            ax.barh(y + off, p50, height=.28, color=COLORS[side], label=["P 侧", "D 侧"][side])
            ax.hlines(y + off, p50, p95, color="#26394d", lw=1.2)
            ax.scatter(p95, y + off, marker="|", s=30, color="#26394d", zorder=3)
        ax.set(yticks=y, yticklabels=TOPS, xlim=(0, xmax * 1.05), ylim=(6.6, -.8), title=title,
               xlabel=f"整组 P50（{'μs' if batch == 1 else 'ms'}）")
        for sep in (.5, 3.5):
            ax.axhline(sep, color="#dce4ed", lw=.8, linestyle="--")
    axs[0].legend(loc="lower right", frameon=False, fontsize=11)
    fig.suptitle(f"同索引 topk2048 · batch={batch} · 每 rank {'2.25' if batch == 1 else '36'} MiB",
                 fontsize=17, fontweight="bold")
    save(fig, name)


write_figure("01-write-logical")
write_figure("02-write-equal-total", equal_total=True)
read_figure(16, "03-read-batch16")
read_figure(1, "04-read-batch1")


def picture(name):
    # Scientific charts have no matching am component. Embed the plotting tool's
    # SVG through the documented escape hatch and existing responsive wrapper.
    svg = (ROOT / f"{name}.svg").read_text()
    svg = svg[svg.index("<svg"):]
    for identifier in set(re.findall(r'id="([^"]+)"', svg)):
        svg = svg.replace(f'id="{identifier}"', f'id="{name}-{identifier}"')
        svg = svg.replace(f'"#{identifier}"', f'"#{name}-{identifier}"')
        svg = svg.replace(f'url(#{identifier})', f'url(#{name}-{identifier})')
    return f'```svg\n<figure class="am-diagram">{svg}</figure>\n```\n\n'


derived = {
    "source_sha256": hashlib.sha256(DATA.read_bytes()).hexdigest(),
    "rows": len(rows),
    "interpretation": "P and D kept separate; ratios use reported group P50; no pooled quantile or confidence interval",
    "numa_verified_rows": sum(r["numa_verified"] for r in rows),
    "max_reported_launch_skew_p95_us": max(r["launch_skew_ms_p95"] for r in rows) * 1000,
    "comparisons": [],
}
for side in (0, 1):
    for n in (2, 4):
        for case in ("write-logical-32768", "write-equal_total-32768",
                     "read-local-random-16", "read-remote-random-16", "read-mixed-random-16"):
            i, s, d = [value(side, f"{n}-{top}", case) for top in ("I0", "S0", "ID")]
            derived["comparisons"].append({
                "side": side, "tp": n, "case": case,
                "I0_ms": i, "S0_ms": s, "ID_ms": d,
                "I0_over_S0": i / s, "S0_change_percent_vs_I0": (s / i - 1) * 100,
                "I0_over_ID": i / d, "S0_over_ID": s / d,
            })
(ROOT / "derived.json").write_text(json.dumps(derived, ensure_ascii=False, indent=2) + "\n")

md = '''---
template: doc
theme: shadcn
mode: light
lang: zh
title: TP1 / TP2 / TP4：大块写入与同时读取
subtitle: 共享池省下重复写入；分散配置提高吞吐；读取仍需每个 rank 搬运完整 KV
source: 用户回传 combined-results.csv · cd-v1-resume2 · 2026-10-14
---
本页分析新 C/D 实验的 162 行汇总。P、D 分开展示，保留全部 P50/P95。

## A 先看三个结论
```callout ok 共享写入的主要收益来自去重
36 MiB 逻辑任务下，TP4 共享池耗时约 **0.522 ms**。
TP4 集中配置的独立池耗时约 **2.081 ms**。
共享池用约四分之一的实际拷贝量，取得约 **3.98 倍**完成速率。
```

| 问题 | 这组结果给出的答案 |
|---|---|
| TP4 共享写入比 TP2 快多少？ | 36 MiB 下约快 1.1%～1.4%；两者都约 0.53 ms |
| 等实际拷贝量时，共享池还有巨大优势吗？ | TP4 两者都约 0.522 ms，P50 差异不足 0.1% |
| 四个 rank 读同一份 KV，是否只搬一次？ | 每 rank 都读完整 KV；batch=16 合计搬运 144 MiB |
| TP4 共享读取是否明显快于集中独立池？ | batch=16 下，三种来源的组 P50 差异均不足 0.4% |
| 哪个配置的吞吐增长最明显？ | 分散配置 4-ID：大块写入约 260 GB/s，本地读取约 350 GB/s |

```callout warn NUMA 结论仍有边界
162 行的 numa_verified 均为 false。
本文的“集中 / 分散”均指请求的分配配置。
这些数值可用于配置对比；物理 NUMA 落点仍待证明。
```

## B 到底测了什么
```kv cols=2
P 侧: 10.120.72.31 · side=0
D 侧: 10.120.72.32 · side=1
执行: Graph · block_dim=48
设备: NPU 0 / 1 / 2 / 3，TP1/2 取前缀
采样: 每项 3 轮 × 20 批，每批 64 次 replay
CPU: 固定 node 0，按 rank 绑定 CPU 子集
```

P 和 D 是运行测试的主机角色。它们的结果均为 copy 组件计时。
每次只让一侧的 rank 组发起读写，对侧维持池可用。

| 配置 | 同时运行的 rank | 每侧池数 | 请求的 NUMA 节点 | 每侧配置贡献 |
|---|---|---|---|---|
| 1-I0 | 1 | 1 | 0 | 2 GiB |
| 2-I0 | 2 | 2 | 0、0 | 4 GiB |
| 2-S0 | 2 | 1 | 0 | 2 GiB |
| 2-ID | 2 | 2 | 0、2 | 4 GiB |
| 4-I0 | 4 | 4 | 0、0、0、0 | 8 GiB |
| 4-S0 | 4 | 1 | 0 | 2 GiB |
| 4-ID | 4 | 4 | 0、2、4、6 | 8 GiB |

I0 = 每 rank 独立池，全部请求 node 0。S0 = 多 rank 共享 node 0 的一个池。
ID = 每 rank 独立池，分别请求不同节点。
容量按实现的分配规格列出；它不是逐页驻留测量。

**每行 KV 为 576 个 BF16，即 1152 bytes。只测一层。**

| 工作量定义 | 独立池每 rank | 共享池每 rank | 整组实际字节数 |
|---|---|---|---|
| C：同一逻辑任务 L | 写完整 L | 写互不重叠的 L/N | 独立 N×L；共享 L |
| C：等拷贝总量 L | 只写自己的 L/N | 写互不重叠的 L/N | 都为 L |
| D：同索引 sparse 读取 | 读自身副本的完整结果 | 读共享副本的完整结果 | 都为 N×读取结果 |

C 主实验的 L 为 2048、8192、32768 行，即 2.25、9、36 MiB。
共享目的地互不重叠：TP2 每 rank 写一半，TP4 每 rank 写四分之一。

D 从每请求的 65536 行中取 topk2048。
所有 rank 使用同一组索引及顺序，分别写入各自 HBM。
batch=1 每 rank 读 2.25 MiB；batch=16 每 rank 读 36 MiB。

local 指本机；remote 指另一台主机；mixed 指每侧各 1024 行。
remote 与同机跨 NUMA 访问是两个不同概念。

## C 大块写入：共享池避免重复写入
下图固定 32768 行、36 MiB 逻辑任务。
每个柱表示同机所有 rank 完成一轮写入的组 P50。
细线端点为 P95；越短越快。

'''
md += picture("01-write-logical")
body = []
for t in TOPS:
    c = "write-logical-32768"
    r = row_at(0, t, c)
    body.append([t, f"{r['copy_bytes_per_call'] / 2**20:g}", pair(t, c),
                 pair(t, c, "copy_payload_gbps_p50", 1), pair(t, c, "logical_gbps_p50", 1)])
md += table(["配置", "实际拷贝 MiB", "P / D 组 P50 ms", "P / D 合计 copy GB/s", "P / D 逻辑 GB/s"], body)
md += '''**TP2：共享池相对集中独立池，耗时下降 50.7%～51.2%。**
两侧的完成速率比为 2.03～2.05 倍。

**TP4：共享池相对集中独立池，耗时下降约 74.9%。**
两侧的完成速率比均约 3.98 倍。

共享池 TP2→TP4 仅从约 0.529 ms 降至 0.522 ms。
总任务仍为 36 MiB，每 rank 的份额从 18 MiB 降至 9 MiB。
集中配置的合计写入速率在本轮大块负载下约为 70～73 GB/s。
结果符合该配置下存在共同吞吐瓶颈的解释。

分散配置下，TP4 独立池完成四份写入约需 0.580 ms。
它与 TP1 写一份的约 0.575 ms 接近。
其实际拷贝量为 144 MiB，合计速率约 260 GB/s。
这里的 260 GB/s 与共享池的 72 GB/s，分子不同。

## D 等拷贝量对照：池共享本身带来多少收益
这里让整组都只写 36 MiB。
TP2 每 rank 写 18 MiB；TP4 每 rank 写 9 MiB。

```callout info 对照实验的语义
独立池此时只更新各自对应的分片。
它用来比较相同数据量的搬运成本。
它没有为每个独立池写出完整 KV 副本。
TP1 使用同义的 write-logical 结果作为基线。
```

'''
md += picture("02-write-equal-total")
body = []
for t in TOPS:
    c = "write-logical-32768" if t == "1-I0" else "write-equal_total-32768"
    body.append([t, pair(t, c), pair(t, c, "group_ms_p95"), pair(t, c, "copy_payload_gbps_p50", 1)])
md += table(["配置", "P / D 组 P50 ms", "P / D 组 P95 ms", "P / D 合计 copy GB/s"], body)
md += '''同为集中配置，TP4 独立池与共享池都约为 0.522 ms。
TP2 两种池模式的组 P50 差异为 0.2%～2.2%。
这组对照支持：主实验的优势主要来自减少重复写入。

分散配置的 TP4 只需约 0.159 ms，速率约 237 GB/s。
相对集中独立池，等拷贝量下约快 3.27～3.29 倍。
相同字节数下，分配配置对吞吐的影响比是否共享池更大。

## E 同时读取：共享池没有省掉各 rank 的搬运
本节固定 batch=16、每请求 topk2048。
所有 rank 读取同一组索引；每 rank 的输出均为 36 MiB。
TP1、TP2、TP4 的合计有效读取量分别为 36、72、144 MiB。
图中细线端点为 P95；下表列出 P50。

'''
md += picture("03-read-batch16")
body = []
for t in TOPS:
    body.append([t] + [pair(t, f"read-{s}-random-16") for s, _ in SOURCES])
md += table(["配置", "本机：P / D ms", "另一主机：P / D ms", "mixed：P / D ms"], body)
md += '''**TP4 共享池与集中独立池基本持平。**
三种来源在两侧的 P50 差异均不足 0.4%。
四个 rank 共享一份存储，并不等于整组只执行一次读取。

**共享池的本地读取随 rank 增多而变慢。**
TP2 约为 0.693 ms；TP4 约为 1.338 ms。
TP4 的合计字节数翻倍，组耗时也接近翻倍。
因此合计速率只从约 109 增至 113 GB/s。

下表用合计有效 copy 带宽观察扩展性。每格按 P / D 排列。

'''
body = []
for t in TOPS:
    body.append([t] + [pair(t, f"read-{s}-random-16", "copy_payload_gbps_p50", 1) for s, _ in SOURCES])
md += table(["配置", "本机 GB/s", "另一主机 GB/s", "mixed GB/s"], body)
md += '''分散配置 4-ID 的本地读取约为 0.431 ms。
它与 TP1 的约 0.427 ms 接近，但搬运四倍数据。
合计有效 copy 带宽约 350 GB/s，约为 TP1 的 3.96 倍。

同为 TP4 独立池，ID 相对 I0 的完成速率如下。

'''
body = []
for src, caption in SOURCES:
    c = f"read-{src}-random-16"
    ratios = [value(s, "4-I0", c) / value(s, "4-ID", c) for s in (0, 1)]
    body.append([caption, f"{ratios[0]:.2f}×", f"{ratios[1]:.2f}×"])
md += table(["数据来源", "P 侧 ID 相对 I0", "D 侧 ID 相对 I0"], body)
md += '''TP2 的远端读取约为 0.85～0.86 ms，配置差异较小。
TP4 的远端读取则从集中配置约 1.344 ms 降至分散配置约 0.850 ms。
本地与远端的扩展行为不同，不能合并成一个“读取加速比”。

## F 小 batch：保留例外，避免扩大收益
batch=1 时，每 rank 只读 2.25 MiB。
下图用微秒显示，表格仍保留毫秒。

'''
md += picture("04-read-batch1")
body = []
for t in TOPS:
    body.append([t] + [pair(t, f"read-{s}-random-1") for s, _ in SOURCES])
md += table(["配置", "本机：P / D ms", "另一主机：P / D ms", "mixed：P / D ms"], body)
md += '''小 batch 的差异并非都能复现到两侧。
4-S0 的 mixed 读取在 P 侧约快 17.1%，D 侧则约慢 0.9%。
不能把 P 侧这一项单独归纳为共享池的稳定收益。

另一个例外是 TP2、D 侧、batch=16 的 mixed 读取。
共享池 P50 为 0.7010 ms，集中独立池为 0.7721 ms。
对应 P95 却分别为 0.7694 ms 和 0.7810 ms。
P50 收益约 9.2%，P95 收益约 1.5%。
需要逐轮结果才能判断差异来自稳定改善还是批次分布变化。

## G 如何使用这些结论
| 目标 | 本次结果支持的选择 | 仍需保留的条件 |
|---|---|---|
| 减少副本及写入流量 | 共享池；TP4 写入一份，配置容量从 8 降至 2 GiB | 读输出仍需每 rank 一份 |
| 提高本地并发读取吞吐 | 分散独立池配置的结果更好 | 增加副本容量与重复写入量 |
| 固定总字节数的写入 | 分散配置具有更高有效吞吐 | 独立池分片对照不构成完整副本更新 |
| 决定模型服务最终策略 | 把本页作为组件证据 | 还需真实服务测试及物理 NUMA 证据 |

**计时口径：同机组完成包络 ÷ 64。**
包络为最后完成时间减最早开始时间。
它包含 Graph 提交、执行和完成等待。
源数据准备、地址分片、初始化与 CPU 校验不在计时区内。
这里没有计入完整 materialize、attention 或模型请求耗时。

P50/P95 来自 60 个批次的均摊时间。
它们不是单个请求或单次 replay 的尾延迟。
表中的 P / D 分别来自两侧，未合并分位数。

带宽直接采用 CSV 的中位数；单位为 GB/s，1 GB = 10⁹ bytes。
CSV 字段名虽含 gbps，数值表示字节速率。
它是有效 copy 吞吐，不是硬件链路计数器测出的总流量。

本页只收到汇总 CSV，没有逐轮原始样本。
不能由这些分位数计算置信区间，也不把微小差异称为统计显著。
各项报告的启动偏差 P95 最大约为 3.35 μs。
这个指标来自主机提交时间，不能证明每条 DMA 的重叠区间。

64 次 replay 复用同批索引；下一批切换索引。
没有应用层 selected KV 缓存复用，但未证明硬件缓存为冷态。
未测试“共享一份 KV、同时分布于多个 NUMA 节点”的混合布局。
共享 ticket 的最终验收状态仍保留为待确认。

## H 完整结果与数据来源
下表保留 81 组 P / D 配对，即全部 162 行结果。
同一行两侧的 TP、批次数、字节量与 NUMA 状态均一致。
全部为每侧 60 批，全部 NUMA 状态待验证。

表内 P50/P95 单位为 ms，字节数换算为 MiB。
copy GB/s 一列为整组实际拷贝的有效带宽。
表格较宽时可横向滚动。

'''
body = []
for r in rows:
    if r["side"]:
        continue
    d = row_at(1, r["topology"], r["case"])
    for k in ("tp_size", "batches", "copy_bytes_per_call", "logical_bytes_per_call", "numa_verified"):
        assert r[k] == d[k]
    body.append([r["topology"], f"`{r['case']}`",
                 f"{r['logical_bytes_per_call'] / 2**20:g}", f"{r['copy_bytes_per_call'] / 2**20:g}",
                 f"{r['group_ms_p50']:.4f} / {r['group_ms_p95']:.4f}",
                 f"{d['group_ms_p50']:.4f} / {d['group_ms_p95']:.4f}",
                 f"{r['copy_payload_gbps_p50']:.1f} / {d['copy_payload_gbps_p50']:.1f}"])
md += table(["配置", "case", "逻辑 MiB", "copy MiB", "P：P50 / P95", "D：P50 / P95", "P / D copy GB/s"], body)
md += '''数据由用户回传，来源文件为：

`/home/cryang/log/numa-cd/cd-v1-resume2/prefill/combined-results.csv`

测试两侧均完成 493/493，并正常清理退出。
续跑约耗时 18.3 分钟；它包含有效结果复用。
这不是从零执行整张矩阵的耗时。

本页只使用这轮新 C/D 数据，旧实验未混入统计。
本地目录同时保存原始 CSV、计算脚本、派生数据与独立图文件。
HTML 已内嵌全部图形，可单文件离线阅读。
'''
(ROOT / "report.md").write_text(md)
print(json.dumps({
    "rows": len(rows), "paired_rows": len(body), "panels": md.count("\n## "),
    "source_sha256": derived["source_sha256"],
    "figures": 4, "draft": str(ROOT / "report.md"),
}, ensure_ascii=False, indent=2))
