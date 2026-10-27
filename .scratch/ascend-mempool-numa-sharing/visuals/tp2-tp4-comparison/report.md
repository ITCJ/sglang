---
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

| 关注点 | TP2 共享 → TP4 共享 | 解读 |
| --- | ---: | ---: |
| 2049 行 token 写入 | P：0.0459 → 0.0447 ms；D：0.0454 → 0.0442 ms | 两侧耗时均约下降 2.6%，接近持平 |
| 1 请求并发读取 | -1.3%～+2.0% | 12 个两侧对照的耗时接近 |
| 16 请求并发读取 | +42.4%～+91.6% | reader 数量翻倍；合计读取量也翻倍 |

```callout info 怎样解释读写差异
共享写入由多个 TP 分工完成一份 KV。
共享读取要求每个 TP 都读到完整 KV。
因此，增加 TP 数量不会减少读取总量。
```
TP4 读路径的共享／独立池耗时变化为 **-1.6%～+1.3%**。
跨 TP 数量的读延迟上升，不能直接归因于共享池。

## B 比较的对象与计时口径
```callout info 本次补入的“单 TP 占用一个 mempool”
独立池模式中，每个 TP 独占一个 mempool。
TP2 独立池是 2 个 TP 同时访问各自的池。
TP4 独立池是 4 个 TP 同时访问各自的池。
它们不是整台机器只有 1 个 TP 工作的 TP1 实验。
```

| 配置 | TP2 | TP4 |
| --- | ---: | ---: |
| 每侧设备 | NPU 0 / 1 | NPU 0 / 1 / 2 / 3 |
| 目标 NUMA | 0 | 0 |
| 图与 kernel 配置 | Graph / block_dim=48 | Graph / block_dim=48 |
| P / D 容量（tokens） | 8192 / 16384 | 16384 / 16384 |
| 池层数 / slots | 2 / 16 | 4 / 16 |
| KV 类型 / 特征维度 / top-k | BF16 / 576 / 2048 | BF16 / 576 / 2048 |
| 数据来源 | run-9-resume 汇总；复用前两轮并补第三轮 | 本次 summary.json 与 decode-summary.json 摘录 |
| 摘录范围 | 43 用例 × P/D | 53 用例 × P/D；selected_component |

配置按历史交付记录和本次命令默认值列出。
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
```svg
<figure class="am-diagram"><svg role="img" aria-label="2049行大块写入：TP2独立、TP2共享、TP4独立、TP4共享的组P50" xmlns:xlink="http://www.w3.org/1999/xlink" width="1045.19952pt" height="404.395285pt" viewBox="0 0 1045.19952 404.395285" xmlns="http://www.w3.org/2000/svg" version="1.1">
 <metadata>
  <rdf:RDF xmlns:dc="http://purl.org/dc/elements/1.1/" xmlns:cc="http://creativecommons.org/ns#" xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#">
   <cc:Work>
    <dc:type rdf:resource="http://purl.org/dc/dcmitype/StillImage"/>
    <dc:date>2026-10-13T20:32:00.012641</dc:date>
    <dc:format>image/svg+xml</dc:format>
    <dc:creator>
     <cc:Agent>
      <dc:title>Matplotlib v3.9.4, https://matplotlib.org/</dc:title>
     </cc:Agent>
    </dc:creator>
   </cc:Work>
  </rdf:RDF>
 </metadata>
 <defs>
  <style type="text/css">*{stroke-linejoin: round; stroke-linecap: butt}</style>
 </defs>
 <g id="01-bulk-write-figure_1">
  <g id="01-bulk-write-patch_1">
   <path d="M 0 404.395285 
L 1045.19952 404.395285 
L 1045.19952 -0 
L 0 -0 
z
" style="fill: #ffffff"/>
  </g>
  <g id="01-bulk-write-axes_1">
   <g id="01-bulk-write-patch_2">
    <path d="M 147.75125 328.078684 
L 517.41576 328.078684 
L 517.41576 48.312668 
L 147.75125 48.312668 
z
" style="fill: #ffffff"/>
   </g>
   <g id="01-bulk-write-matplotlib.axis_1">
    <g id="01-bulk-write-xtick_1">
     <g id="01-bulk-write-line2d_1">
      <path d="M 147.75125 328.078684 
L 147.75125 48.312668 
" clip-path="url(#01-bulk-write-paeb002f85f)" style="fill: none; stroke: #e9edf2; stroke-width: 0.8; stroke-linecap: square"/>
     </g>
     <g id="01-bulk-write-line2d_2"/>
     <g id="01-bulk-write-text_1">
      <!-- 0.000 -->
      <g style="fill: #405570" transform="translate(130.5425 345.269934) scale(0.12 -0.12)">
       <defs>
        <path id="01-bulk-write-HiraginoSansGB-W3-30" d="M 2099 -58 
C 2688 -58 3136 166 3456 614 
C 3738 1005 3904 1690 3904 2464 
C 3904 2995 3821 3507 3661 3949 
C 3584 4160 3456 4358 3277 4538 
C 2989 4838 2579 4992 2106 4992 
C 1472 4992 979 4723 672 4205 
C 448 3834 301 3142 301 2464 
C 301 1939 384 1434 544 986 
C 621 768 755 570 928 397 
C 1229 96 1626 -58 2099 -58 
z
M 2099 4550 
C 2547 4550 2893 4352 3104 3968 
C 3270 3667 3392 3034 3392 2477 
C 3392 2003 3328 1581 3194 1184 
C 3136 998 3034 832 2893 685 
C 2701 486 2426 384 2106 384 
C 1670 384 1338 570 1114 941 
C 941 1242 813 1869 813 2464 
C 813 2931 877 3366 1011 3757 
C 1062 3917 1165 4077 1299 4224 
C 1504 4442 1766 4550 2099 4550 
z
" transform="scale(0.015625)"/>
        <path id="01-bulk-write-HiraginoSansGB-W3-2e" d="M 448 672 
L 448 38 
L 1082 38 
L 1082 672 
L 448 672 
z
" transform="scale(0.015625)"/>
       </defs>
       <use xlink:href="#01-bulk-write-HiraginoSansGB-W3-30"/>
       <use xlink:href="#01-bulk-write-HiraginoSansGB-W3-2e" x="65.699997"/>
       <use xlink:href="#01-bulk-write-HiraginoSansGB-W3-30" x="89.699982"/>
       <use xlink:href="#01-bulk-write-HiraginoSansGB-W3-30" x="155.399979"/>
       <use xlink:href="#01-bulk-write-HiraginoSansGB-W3-30" x="221.099976"/>
      </g>
     </g>
    </g>
    <g id="01-bulk-write-xtick_2">
     <g id="01-bulk-write-line2d_3">
      <path d="M 197.705914 328.078684 
L 197.705914 48.312668 
" clip-path="url(#01-bulk-write-paeb002f85f)" style="fill: none; stroke: #e9edf2; stroke-width: 0.8; stroke-linecap: square"/>
     </g>
     <g id="01-bulk-write-line2d_4"/>
     <g id="01-bulk-write-text_2">
      <!-- 0.025 -->
      <g style="fill: #405570" transform="translate(180.497164 345.269934) scale(0.12 -0.12)">
       <defs>
        <path id="01-bulk-write-HiraginoSansGB-W3-32" d="M 3027 38 
C 3347 38 3558 26 3814 0 
L 3814 538 
C 3571 506 3354 493 3034 493 
L 992 493 
C 1069 800 1158 973 1357 1235 
C 1568 1504 1773 1683 2278 2022 
C 3008 2509 3174 2630 3366 2848 
C 3616 3123 3744 3430 3744 3757 
C 3744 4512 3123 4998 2157 4998 
C 1594 4998 1158 4832 870 4506 
C 659 4269 563 4045 467 3584 
L 1005 3494 
C 1050 3814 1107 3968 1229 4141 
C 1421 4416 1734 4557 2157 4557 
C 2810 4557 3219 4243 3219 3757 
C 3219 3366 3008 3085 2374 2656 
C 1632 2150 1619 2144 1395 1952 
C 992 1606 723 1254 563 851 
C 467 608 448 512 378 6 
C 582 32 678 38 1082 38 
L 3027 38 
z
" transform="scale(0.015625)"/>
        <path id="01-bulk-write-HiraginoSansGB-W3-35" d="M 1261 4461 
L 2765 4461 
C 3066 4461 3270 4448 3514 4410 
L 3514 4954 
C 3258 4915 3110 4902 2765 4902 
L 1446 4902 
C 1069 4902 966 4909 768 4934 
C 781 4774 781 4749 781 4659 
C 781 4538 774 4461 762 4275 
L 621 2829 
C 595 2554 576 2419 525 2170 
L 1005 2125 
C 1075 2298 1146 2387 1299 2509 
C 1530 2688 1811 2778 2138 2778 
C 2816 2778 3258 2317 3258 1613 
C 3258 877 2778 390 2061 390 
C 1690 390 1370 525 1152 768 
C 1056 877 1011 960 947 1139 
L 422 986 
C 576 678 666 544 826 384 
C 1133 96 1549 -51 2048 -51 
C 3104 -51 3776 608 3776 1638 
C 3776 2112 3635 2490 3347 2765 
C 3059 3046 2662 3200 2195 3200 
C 1754 3200 1382 3053 1075 2758 
C 1114 2931 1114 2931 1126 3046 
L 1261 4461 
z
" transform="scale(0.015625)"/>
       </defs>
       <use xlink:href="#01-bulk-write-HiraginoSansGB-W3-30"/>
       <use xlink:href="#01-bulk-write-HiraginoSansGB-W3-2e" x="65.699997"/>
       <use xlink:href="#01-bulk-write-HiraginoSansGB-W3-30" x="89.699982"/>
       <use xlink:href="#01-bulk-write-HiraginoSansGB-W3-32" x="155.399979"/>
       <use xlink:href="#01-bulk-write-HiraginoSansGB-W3-35" x="221.099976"/>
      </g>
     </g>
    </g>
    <g id="01-bulk-write-xtick_3">
     <g id="01-bulk-write-line2d_5">
      <path d="M 247.660577 328.078684 
L 247.660577 48.312668 
" clip-path="url(#01-bulk-write-paeb002f85f)" style="fill: none; stroke: #e9edf2; stroke-width: 0.8; stroke-linecap: square"/>
     </g>
     <g id="01-bulk-write-line2d_6"/>
     <g id="01-bulk-write-text_3">
      <!-- 0.050 -->
      <g style="fill: #405570" transform="translate(230.451827 345.269934) scale(0.12 -0.12)">
       <use xlink:href="#01-bulk-write-HiraginoSansGB-W3-30"/>
       <use xlink:href="#01-bulk-write-HiraginoSansGB-W3-2e" x="65.699997"/>
       <use xlink:href="#01-bulk-write-HiraginoSansGB-W3-30" x="89.699982"/>
       <use xlink:href="#01-bulk-write-HiraginoSansGB-W3-35" x="155.399979"/>
       <use xlink:href="#01-bulk-write-HiraginoSansGB-W3-30" x="221.099976"/>
      </g>
     </g>
    </g>
    <g id="01-bulk-write-xtick_4">
     <g id="01-bulk-write-line2d_7">
      <path d="M 297.615241 328.078684 
L 297.615241 48.312668 
" clip-path="url(#01-bulk-write-paeb002f85f)" style="fill: none; stroke: #e9edf2; stroke-width: 0.8; stroke-linecap: square"/>
     </g>
     <g id="01-bulk-write-line2d_8"/>
     <g id="01-bulk-write-text_4">
      <!-- 0.075 -->
      <g style="fill: #405570" transform="translate(280.406491 345.269934) scale(0.12 -0.12)">
       <defs>
        <path id="01-bulk-write-HiraginoSansGB-W3-37" d="M 3789 4934 
C 3565 4909 3424 4902 3046 4902 
L 1126 4902 
C 819 4902 646 4915 397 4947 
L 397 4403 
C 640 4435 819 4448 1120 4448 
L 3200 4448 
C 2630 3674 2355 3232 2048 2592 
C 1651 1773 1478 1197 1274 38 
L 1882 38 
C 1984 781 2054 1107 2221 1638 
C 2470 2419 2739 2963 3315 3853 
C 3565 4237 3674 4378 3789 4467 
L 3789 4934 
z
" transform="scale(0.015625)"/>
       </defs>
       <use xlink:href="#01-bulk-write-HiraginoSansGB-W3-30"/>
       <use xlink:href="#01-bulk-write-HiraginoSansGB-W3-2e" x="65.699997"/>
       <use xlink:href="#01-bulk-write-HiraginoSansGB-W3-30" x="89.699982"/>
       <use xlink:href="#01-bulk-write-HiraginoSansGB-W3-37" x="155.399979"/>
       <use xlink:href="#01-bulk-write-HiraginoSansGB-W3-35" x="221.099976"/>
      </g>
     </g>
    </g>
    <g id="01-bulk-write-xtick_5">
     <g id="01-bulk-write-line2d_9">
      <path d="M 347.569904 328.078684 
L 347.569904 48.312668 
" clip-path="url(#01-bulk-write-paeb002f85f)" style="fill: none; stroke: #e9edf2; stroke-width: 0.8; stroke-linecap: square"/>
     </g>
     <g id="01-bulk-write-line2d_10"/>
     <g id="01-bulk-write-text_5">
      <!-- 0.100 -->
      <g style="fill: #405570" transform="translate(330.361154 345.269934) scale(0.12 -0.12)">
       <defs>
        <path id="01-bulk-write-HiraginoSansGB-W3-31" d="M 1030 4429 
C 1216 4454 1382 4467 1587 4467 
L 1933 4467 
L 1933 762 
C 1933 467 1920 262 1882 38 
L 2490 38 
C 2451 275 2438 461 2438 762 
L 2438 3872 
C 2438 4218 2458 4544 2496 4934 
C 2285 4909 2182 4902 1965 4902 
L 1562 4902 
C 1318 4902 1197 4909 1030 4934 
L 1030 4429 
z
" transform="scale(0.015625)"/>
       </defs>
       <use xlink:href="#01-bulk-write-HiraginoSansGB-W3-30"/>
       <use xlink:href="#01-bulk-write-HiraginoSansGB-W3-2e" x="65.699997"/>
       <use xlink:href="#01-bulk-write-HiraginoSansGB-W3-31" x="89.699982"/>
       <use xlink:href="#01-bulk-write-HiraginoSansGB-W3-30" x="155.399979"/>
       <use xlink:href="#01-bulk-write-HiraginoSansGB-W3-30" x="221.099976"/>
      </g>
     </g>
    </g>
    <g id="01-bulk-write-xtick_6">
     <g id="01-bulk-write-line2d_11">
      <path d="M 397.524568 328.078684 
L 397.524568 48.312668 
" clip-path="url(#01-bulk-write-paeb002f85f)" style="fill: none; stroke: #e9edf2; stroke-width: 0.8; stroke-linecap: square"/>
     </g>
     <g id="01-bulk-write-line2d_12"/>
     <g id="01-bulk-write-text_6">
      <!-- 0.125 -->
      <g style="fill: #405570" transform="translate(380.315818 345.269934) scale(0.12 -0.12)">
       <use xlink:href="#01-bulk-write-HiraginoSansGB-W3-30"/>
       <use xlink:href="#01-bulk-write-HiraginoSansGB-W3-2e" x="65.699997"/>
       <use xlink:href="#01-bulk-write-HiraginoSansGB-W3-31" x="89.699982"/>
       <use xlink:href="#01-bulk-write-HiraginoSansGB-W3-32" x="155.399979"/>
       <use xlink:href="#01-bulk-write-HiraginoSansGB-W3-35" x="221.099976"/>
      </g>
     </g>
    </g>
    <g id="01-bulk-write-xtick_7">
     <g id="01-bulk-write-line2d_13">
      <path d="M 447.479231 328.078684 
L 447.479231 48.312668 
" clip-path="url(#01-bulk-write-paeb002f85f)" style="fill: none; stroke: #e9edf2; stroke-width: 0.8; stroke-linecap: square"/>
     </g>
     <g id="01-bulk-write-line2d_14"/>
     <g id="01-bulk-write-text_7">
      <!-- 0.150 -->
      <g style="fill: #405570" transform="translate(430.270481 345.269934) scale(0.12 -0.12)">
       <use xlink:href="#01-bulk-write-HiraginoSansGB-W3-30"/>
       <use xlink:href="#01-bulk-write-HiraginoSansGB-W3-2e" x="65.699997"/>
       <use xlink:href="#01-bulk-write-HiraginoSansGB-W3-31" x="89.699982"/>
       <use xlink:href="#01-bulk-write-HiraginoSansGB-W3-35" x="155.399979"/>
       <use xlink:href="#01-bulk-write-HiraginoSansGB-W3-30" x="221.099976"/>
      </g>
     </g>
    </g>
    <g id="01-bulk-write-xtick_8">
     <g id="01-bulk-write-line2d_15">
      <path d="M 497.433895 328.078684 
L 497.433895 48.312668 
" clip-path="url(#01-bulk-write-paeb002f85f)" style="fill: none; stroke: #e9edf2; stroke-width: 0.8; stroke-linecap: square"/>
     </g>
     <g id="01-bulk-write-line2d_16"/>
     <g id="01-bulk-write-text_8">
      <!-- 0.175 -->
      <g style="fill: #405570" transform="translate(480.225145 345.269934) scale(0.12 -0.12)">
       <use xlink:href="#01-bulk-write-HiraginoSansGB-W3-30"/>
       <use xlink:href="#01-bulk-write-HiraginoSansGB-W3-2e" x="65.699997"/>
       <use xlink:href="#01-bulk-write-HiraginoSansGB-W3-31" x="89.699982"/>
       <use xlink:href="#01-bulk-write-HiraginoSansGB-W3-37" x="155.399979"/>
       <use xlink:href="#01-bulk-write-HiraginoSansGB-W3-35" x="221.099976"/>
      </g>
     </g>
    </g>
    <g id="01-bulk-write-text_9">
     <!-- 组 P50（ms） · 越低越好 -->
     <g style="fill: #405570" transform="translate(257.925692 360.909934) scale(0.12 -0.12)">
      <defs>
       <path id="01-bulk-write-HiraginoSansGB-W3-7ec4" d="M 646 1299 
C 845 1363 941 1389 1107 1421 
C 1344 1459 1344 1459 1440 1478 
C 1830 1549 1830 1549 2176 1606 
C 2432 1651 2432 1651 2592 1683 
L 2637 2080 
C 2202 1978 1933 1926 1152 1786 
C 1696 2432 2176 3091 2624 3814 
L 2240 4026 
C 2106 3744 2061 3661 1894 3398 
C 1498 3366 1498 3366 928 3328 
C 1158 3706 1408 4186 1632 4691 
C 1683 4794 1683 4794 1850 5165 
L 1434 5331 
C 1261 4813 1056 4301 858 3904 
C 608 3405 550 3328 384 3302 
L 493 2893 
C 691 2963 966 3002 1677 3053 
C 1555 2867 1555 2867 1152 2330 
C 851 1914 749 1805 634 1773 
C 544 1747 544 1747 518 1741 
L 646 1299 
z
M 5606 141 
L 5606 4275 
C 5606 4653 5606 4794 5626 5005 
C 5453 4986 5254 4979 4922 4979 
L 3699 4979 
C 3411 4979 3245 4986 3059 4998 
C 3078 4787 3085 4602 3085 4339 
L 3085 141 
L 2950 141 
C 2765 141 2598 154 2451 179 
L 2451 -256 
C 2624 -230 2816 -218 3027 -218 
L 5459 -218 
C 5766 -218 5952 -230 6144 -256 
L 6144 179 
C 6016 154 5837 141 5715 141 
L 5606 141 
z
M 3469 4608 
L 5222 4608 
L 5222 3405 
L 3469 3405 
L 3469 4608 
z
M 3469 3046 
L 5222 3046 
L 5222 1786 
L 3469 1786 
L 3469 3046 
z
M 3469 1427 
L 5222 1427 
L 5222 141 
L 3469 141 
L 3469 1427 
z
M 2534 928 
C 2086 755 1536 589 1069 480 
C 755 410 467 365 365 365 
C 352 365 339 365 314 365 
L 416 -90 
C 493 -58 531 -45 646 -19 
C 1152 102 1498 192 1798 294 
C 2336 474 2336 474 2534 531 
L 2534 928 
z
" transform="scale(0.015625)"/>
       <path id="01-bulk-write-HiraginoSansGB-W3-20" transform="scale(0.015625)"/>
       <path id="01-bulk-write-HiraginoSansGB-W3-50" d="M 1094 38 
C 1056 256 1043 454 1043 762 
L 1043 2022 
L 2470 2022 
C 3469 2022 4045 2566 4045 3507 
C 4045 4397 3488 4902 2502 4902 
L 1197 4902 
C 902 4902 723 4909 493 4922 
C 531 4653 544 4454 544 4192 
L 544 755 
C 544 454 531 269 493 38 
L 1094 38 
z
M 1043 2464 
L 1043 4461 
L 2470 4461 
C 3162 4461 3533 4122 3533 3482 
C 3533 2829 3149 2464 2470 2464 
L 1043 2464 
z
" transform="scale(0.015625)"/>
       <path id="01-bulk-write-HiraginoSansGB-W3-ff08" d="M 6157 -518 
C 5619 26 5408 314 5184 806 
C 4954 1318 4838 1850 4838 2432 
C 4838 3014 4954 3546 5184 4058 
C 5408 4550 5619 4838 6157 5382 
L 5587 5382 
C 5107 4832 4909 4525 4698 4006 
C 4486 3494 4390 2989 4390 2432 
C 4390 1875 4486 1370 4698 858 
C 4909 339 5107 32 5587 -518 
L 6157 -518 
z
" transform="scale(0.015625)"/>
       <path id="01-bulk-write-HiraginoSansGB-W3-6d" d="M 1018 38 
C 986 294 973 467 973 762 
L 973 2016 
C 973 2182 1069 2483 1184 2656 
C 1382 2963 1690 3142 2022 3142 
C 2483 3142 2739 2835 2739 2272 
L 2739 762 
C 2739 435 2726 269 2694 38 
L 3270 38 
C 3238 256 3226 435 3226 755 
L 3226 2016 
C 3226 2208 3315 2490 3437 2675 
C 3622 2970 3923 3142 4243 3142 
C 4755 3142 4979 2880 4979 2272 
L 4979 762 
C 4979 422 4973 275 4934 38 
L 5510 38 
C 5478 262 5466 435 5466 755 
L 5466 2336 
C 5466 2714 5402 2963 5248 3168 
C 5056 3424 4736 3558 4314 3558 
C 3776 3558 3430 3354 3130 2861 
C 3066 3053 3027 3130 2931 3232 
C 2739 3450 2470 3558 2106 3558 
C 1734 3558 1421 3443 1203 3226 
C 1075 3091 1018 3014 960 2906 
C 966 3027 973 3104 973 3136 
L 973 3488 
L 454 3488 
C 486 3258 499 3085 499 2765 
L 499 762 
C 499 461 486 269 454 38 
L 1018 38 
z
" transform="scale(0.015625)"/>
       <path id="01-bulk-write-HiraginoSansGB-W3-73" d="M 269 973 
C 333 710 378 602 474 467 
C 710 141 1158 -32 1741 -32 
C 2618 -32 3162 365 3162 1005 
C 3162 1267 3053 1510 2854 1670 
C 2662 1818 2458 1894 1837 2035 
C 1440 2118 1274 2170 1126 2253 
C 986 2330 909 2458 909 2605 
C 909 2944 1242 3181 1709 3181 
C 1997 3181 2227 3098 2374 2938 
C 2464 2848 2496 2771 2534 2598 
L 3040 2682 
C 2957 2976 2867 3123 2701 3264 
C 2470 3469 2131 3571 1696 3571 
C 1267 3571 922 3456 698 3238 
C 518 3066 422 2848 422 2618 
C 422 2336 544 2099 768 1946 
C 986 1798 1203 1722 1715 1613 
C 2189 1517 2317 1478 2470 1382 
C 2605 1299 2669 1165 2669 986 
C 2669 608 2317 371 1760 371 
C 1434 371 1178 448 1018 595 
C 890 710 838 826 794 1062 
L 269 973 
z
" transform="scale(0.015625)"/>
       <path id="01-bulk-write-HiraginoSansGB-W3-ff09" d="M 813 -518 
C 1293 32 1491 339 1702 858 
C 1914 1370 2010 1875 2010 2432 
C 2010 2989 1914 3494 1702 4006 
C 1491 4525 1293 4832 813 5382 
L 243 5382 
C 781 4845 992 4550 1216 4058 
C 1446 3546 1562 3014 1562 2432 
C 1562 1850 1446 1318 1216 806 
C 992 314 781 19 243 -518 
L 813 -518 
z
" transform="scale(0.015625)"/>
       <path id="01-bulk-write-HiraginoSansGB-W3-b7" d="M 3200 2957 
C 2912 2957 2675 2720 2675 2432 
C 2675 2144 2912 1907 3200 1907 
C 3488 1907 3725 2144 3725 2432 
C 3725 2720 3488 2957 3200 2957 
z
" transform="scale(0.015625)"/>
       <path id="01-bulk-write-HiraginoSansGB-W3-8d8a" d="M 4026 320 
C 4512 768 4723 979 4934 1254 
C 5242 723 5472 499 5696 499 
C 5914 499 6042 781 6170 1510 
C 6042 1562 5965 1613 5850 1709 
C 5773 1126 5715 922 5632 922 
C 5594 922 5549 966 5421 1133 
C 5299 1293 5254 1370 5152 1594 
C 5440 2099 5536 2349 5722 3066 
C 5760 3219 5786 3290 5818 3386 
L 5440 3526 
C 5370 3078 5197 2522 4979 2042 
C 4832 2477 4768 2931 4710 3962 
L 5408 3962 
C 5645 3962 5837 3949 6010 3923 
L 6010 4339 
C 5875 4314 5709 4307 5408 4307 
L 4698 4307 
C 4691 4371 4691 4390 4691 4467 
C 4691 5171 4691 5171 4717 5318 
L 4282 5318 
C 4314 5094 4314 5075 4314 4403 
C 4314 4365 4314 4352 4314 4307 
L 3667 4307 
C 3398 4307 3238 4314 3078 4333 
C 3098 4166 3104 4006 3104 3750 
L 3104 1389 
C 3085 1216 3066 1184 2950 1114 
L 3168 768 
C 3277 832 3277 832 3757 1120 
C 3853 1178 4013 1280 4333 1491 
L 4256 1850 
C 3846 1574 3699 1485 3526 1395 
C 3501 1382 3501 1382 3475 1370 
L 3475 3962 
L 4333 3962 
C 4403 2867 4512 2246 4749 1632 
C 4422 1190 4154 915 3757 621 
C 3770 614 3795 582 3853 531 
C 3904 480 3917 467 3974 390 
L 4026 320 
z
M 1549 4422 
L 1056 4422 
C 762 4422 646 4429 499 4454 
L 499 4038 
C 666 4064 819 4070 1056 4070 
L 1549 4070 
L 1549 3213 
L 960 3213 
C 666 3213 499 3226 352 3251 
L 352 2822 
C 518 2854 666 2861 960 2861 
L 1594 2861 
L 1594 653 
C 1382 845 1165 1133 1030 1395 
C 1056 1606 1075 1734 1082 1914 
C 1101 2266 1107 2278 1120 2400 
L 704 2426 
C 723 2310 723 2272 723 2170 
C 723 1229 531 442 141 -205 
C 282 -275 352 -320 474 -442 
C 710 0 870 448 960 947 
C 1139 659 1293 467 1485 294 
C 1869 -51 2253 -211 2906 -275 
C 3322 -320 3526 -326 4928 -326 
C 5146 -326 5760 -307 5997 -294 
C 6029 -109 6048 -38 6138 147 
C 5683 90 5037 58 4326 58 
C 3757 58 3232 77 2829 115 
C 2458 147 2221 218 1965 371 
L 1965 1677 
L 2317 1677 
C 2541 1677 2701 1664 2842 1638 
L 2842 2048 
C 2688 2022 2541 2016 2323 2016 
L 1965 2016 
L 1965 2861 
L 2336 2861 
C 2618 2861 2778 2848 2938 2822 
L 2938 3251 
C 2790 3226 2630 3213 2336 3213 
L 1926 3213 
L 1926 4070 
L 2246 4070 
C 2502 4070 2656 4064 2822 4038 
L 2822 4454 
C 2682 4429 2573 4422 2246 4422 
L 1926 4422 
L 1926 4678 
C 1926 4986 1933 5133 1958 5306 
L 1510 5306 
C 1542 5133 1549 4986 1549 4678 
L 1549 4422 
z
M 4947 5088 
C 5210 4870 5350 4710 5542 4442 
L 5811 4666 
C 5613 4922 5530 5011 5210 5299 
L 4947 5088 
z
" transform="scale(0.015625)"/>
       <path id="01-bulk-write-HiraginoSansGB-W3-4f4e" d="M 4198 2502 
C 4378 1261 4627 570 5094 19 
C 5318 -243 5466 -352 5606 -352 
C 5792 -352 5901 -262 6003 -32 
C 6074 128 6182 602 6208 851 
C 6074 941 5978 1018 5869 1133 
C 5830 525 5722 96 5606 96 
C 5478 96 5120 602 4941 1043 
C 4794 1395 4653 1990 4595 2502 
L 5395 2502 
C 5651 2502 5805 2496 5965 2470 
L 5965 2886 
C 5818 2861 5715 2854 5395 2854 
L 4550 2854 
C 4486 3405 4467 3706 4454 4582 
C 4896 4685 4960 4704 5722 4973 
L 5382 5338 
C 4883 5005 3507 4691 2387 4666 
C 2413 4499 2419 4378 2419 4083 
L 2419 205 
C 2419 83 2368 26 2272 6 
L 2413 -384 
C 2784 -256 3078 -173 3680 -32 
L 3712 365 
C 3200 224 2899 147 2816 128 
L 2816 2502 
L 4198 2502 
z
M 2816 2854 
L 2816 4320 
C 3315 4365 3648 4410 4058 4493 
C 4064 3930 4096 3437 4154 2854 
L 2816 2854 
z
M 1043 288 
C 1043 -32 1030 -243 1005 -480 
L 1472 -480 
C 1446 -256 1440 -38 1440 269 
L 1440 3750 
C 1773 4474 1773 4474 1946 4954 
C 1997 5088 2016 5139 2061 5248 
L 1613 5370 
C 1568 5088 1421 4627 1216 4186 
C 947 3597 608 3104 134 2611 
C 275 2451 314 2400 397 2246 
C 698 2592 870 2835 1069 3206 
C 1050 2976 1043 2714 1043 2438 
L 1043 288 
z
M 4749 -122 
C 4493 262 4218 589 3834 960 
L 3552 755 
C 3904 403 4179 58 4429 -358 
L 4749 -122 
z
" transform="scale(0.015625)"/>
       <path id="01-bulk-write-HiraginoSansGB-W3-597d" d="M 1094 3680 
C 928 3008 742 2458 493 1894 
C 954 1613 1075 1530 1517 1158 
C 1184 646 832 307 269 -38 
C 384 -141 461 -237 550 -378 
C 1139 38 1459 346 1773 838 
C 1792 864 1792 864 1811 890 
C 2074 621 2170 518 2445 173 
L 2726 512 
C 2374 909 2246 1037 2016 1242 
C 2029 1261 2029 1261 2048 1293 
C 2374 1894 2592 2733 2688 3776 
C 2707 3974 2707 3974 2720 4064 
C 2573 4045 2400 4038 2138 4038 
L 1581 4038 
C 1626 4250 1696 4634 1722 4806 
C 1773 5133 1786 5197 1811 5299 
L 1344 5344 
C 1350 5306 1350 5274 1350 5254 
C 1350 4998 1280 4499 1171 4038 
L 941 4038 
C 710 4038 538 4051 378 4077 
L 378 3642 
C 538 3667 698 3680 902 3680 
L 1094 3680 
z
M 2278 3680 
C 2176 2720 2016 2086 1715 1504 
C 1427 1728 1318 1805 973 2042 
C 1203 2637 1312 2989 1498 3680 
L 2278 3680 
z
M 4608 2605 
L 4608 3226 
C 5011 3565 5293 3846 5498 4102 
C 5907 4627 5907 4627 5952 4672 
L 5715 4966 
C 5613 4941 5485 4934 5222 4934 
L 3578 4934 
C 3309 4934 3098 4947 2931 4973 
L 2931 4518 
C 3091 4544 3322 4557 3558 4557 
L 5363 4557 
C 5037 4096 4787 3827 4378 3501 
L 4154 3501 
C 4186 3270 4198 3104 4198 2874 
L 4198 2605 
L 3328 2605 
C 3034 2605 2906 2611 2752 2643 
L 2752 2189 
C 2925 2214 3078 2227 3334 2227 
L 4198 2227 
L 4198 147 
C 4198 32 4154 13 3949 13 
C 3763 13 3488 45 3117 102 
C 3174 -70 3194 -147 3206 -333 
C 3546 -365 3802 -384 3974 -384 
C 4493 -384 4608 -294 4608 134 
L 4608 2227 
L 5664 2227 
C 5926 2227 6061 2221 6234 2189 
L 6234 2643 
C 6067 2611 5939 2605 5664 2605 
L 4608 2605 
z
" transform="scale(0.015625)"/>
      </defs>
      <use xlink:href="#01-bulk-write-HiraginoSansGB-W3-7ec4"/>
      <use xlink:href="#01-bulk-write-HiraginoSansGB-W3-20" x="99.999985"/>
      <use xlink:href="#01-bulk-write-HiraginoSansGB-W3-50" x="133.299973"/>
      <use xlink:href="#01-bulk-write-HiraginoSansGB-W3-35" x="199.499969"/>
      <use xlink:href="#01-bulk-write-HiraginoSansGB-W3-30" x="265.199966"/>
      <use xlink:href="#01-bulk-write-HiraginoSansGB-W3-ff08" x="330.899963"/>
      <use xlink:href="#01-bulk-write-HiraginoSansGB-W3-6d" x="430.899948"/>
      <use xlink:href="#01-bulk-write-HiraginoSansGB-W3-73" x="523.999939"/>
      <use xlink:href="#01-bulk-write-HiraginoSansGB-W3-ff09" x="577.699936"/>
      <use xlink:href="#01-bulk-write-HiraginoSansGB-W3-20" x="677.699921"/>
      <use xlink:href="#01-bulk-write-HiraginoSansGB-W3-b7" x="710.999908"/>
      <use xlink:href="#01-bulk-write-HiraginoSansGB-W3-20" x="810.999893"/>
      <use xlink:href="#01-bulk-write-HiraginoSansGB-W3-8d8a" x="844.299881"/>
      <use xlink:href="#01-bulk-write-HiraginoSansGB-W3-4f4e" x="944.299866"/>
      <use xlink:href="#01-bulk-write-HiraginoSansGB-W3-8d8a" x="1044.29985"/>
      <use xlink:href="#01-bulk-write-HiraginoSansGB-W3-597d" x="1144.299835"/>
     </g>
    </g>
   </g>
   <g id="01-bulk-write-matplotlib.axis_2">
    <g id="01-bulk-write-ytick_1">
     <g id="01-bulk-write-line2d_17"/>
     <g id="01-bulk-write-text_10">
      <!-- 2049 行 / token 分片 -->
      <g style="fill: #243a56" transform="translate(16.92 142.261881) scale(0.12 -0.12)">
       <defs>
        <path id="01-bulk-write-HiraginoSansGB-W3-34" d="M 198 1254 
C 410 1274 602 1280 960 1280 
L 2675 1280 
L 2675 730 
C 2675 442 2662 262 2624 38 
L 3206 38 
C 3168 275 3155 422 3155 730 
L 3155 1280 
L 3392 1280 
C 3667 1280 3827 1267 4064 1235 
L 4064 1766 
C 3834 1734 3667 1722 3398 1722 
L 3155 1722 
L 3155 3821 
C 3155 4326 3162 4525 3194 4902 
L 2714 4902 
C 2566 4678 2451 4518 2234 4250 
L 614 2227 
C 480 2067 333 1888 198 1734 
L 198 1254 
z
M 2682 1715 
L 768 1715 
L 2240 3571 
C 2426 3808 2586 4019 2720 4211 
C 2688 3885 2682 3680 2682 3341 
L 2682 1715 
z
" transform="scale(0.015625)"/>
        <path id="01-bulk-write-HiraginoSansGB-W3-39" d="M 416 934 
C 582 595 685 448 864 301 
C 1139 70 1498 -45 1939 -45 
C 2528 -45 2950 147 3270 550 
C 3616 979 3802 1702 3802 2592 
C 3802 3142 3744 3578 3616 3923 
C 3373 4589 2784 4998 2067 4998 
C 1018 4998 333 4333 333 3315 
C 333 2861 493 2438 768 2163 
C 1043 1888 1446 1741 1920 1741 
C 2400 1741 2758 1882 3085 2189 
C 3200 2310 3251 2374 3341 2547 
C 3328 2221 3309 1958 3283 1798 
C 3232 1498 3130 1184 3008 979 
C 2771 576 2438 390 1978 390 
C 1626 390 1350 493 1171 691 
C 1069 800 1018 890 941 1101 
L 416 934 
z
M 1978 2170 
C 1280 2170 838 2624 838 3347 
C 838 3597 902 3853 1005 4019 
C 1216 4358 1600 4563 2022 4563 
C 2707 4563 3219 4045 3219 3354 
C 3219 2675 2688 2170 1978 2170 
z
" transform="scale(0.015625)"/>
        <path id="01-bulk-write-HiraginoSansGB-W3-884c" d="M 5005 2957 
L 5370 2957 
C 5664 2957 5901 2944 6067 2918 
L 6067 3366 
C 5920 3341 5690 3328 5363 3328 
L 3117 3328 
C 2810 3328 2560 3341 2406 3366 
L 2406 2918 
C 2560 2944 2816 2957 3117 2957 
L 4576 2957 
L 4576 186 
C 4576 58 4525 32 4307 32 
C 4038 32 3776 58 3405 109 
C 3462 -38 3488 -154 3507 -333 
C 3802 -358 4051 -365 4282 -365 
C 4858 -365 5005 -256 5005 154 
L 5005 2957 
z
M 1792 5325 
C 1690 5088 1530 4864 1210 4544 
C 954 4282 723 4096 365 3866 
C 486 3770 557 3693 653 3552 
C 1229 3968 1690 4416 2074 4941 
C 2131 5018 2144 5037 2189 5082 
L 1792 5325 
z
M 2669 4525 
C 2861 4550 3078 4563 3334 4563 
L 5126 4563 
C 5382 4563 5594 4550 5786 4525 
L 5786 4973 
C 5606 4947 5414 4934 5126 4934 
L 3334 4934 
C 3046 4934 2842 4947 2669 4973 
L 2669 4525 
z
M 1306 314 
C 1306 -26 1293 -288 1261 -467 
L 1766 -467 
C 1734 -288 1722 -32 1722 314 
L 1722 2899 
C 1901 3110 2016 3258 2150 3475 
C 2246 3622 2266 3654 2342 3750 
L 1946 3974 
C 1882 3802 1786 3622 1638 3424 
C 1254 2906 845 2502 346 2163 
C 448 2061 518 1958 595 1798 
C 909 2042 1094 2221 1325 2515 
C 1312 2208 1306 2022 1306 1747 
L 1306 314 
z
" transform="scale(0.015625)"/>
        <path id="01-bulk-write-HiraginoSansGB-W3-2f" d="M 2432 4902 
L 122 -954 
L 602 -954 
L 2906 4902 
L 2432 4902 
z
" transform="scale(0.015625)"/>
        <path id="01-bulk-write-HiraginoSansGB-W3-74" d="M 858 4442 
C 890 4230 902 3987 902 3731 
L 902 3488 
L 646 3488 
C 378 3488 250 3494 70 3526 
L 70 3040 
C 282 3066 429 3078 646 3078 
L 902 3078 
L 902 787 
C 902 486 954 301 1082 173 
C 1210 45 1427 -26 1709 -26 
C 1971 -26 2170 13 2432 122 
L 2291 570 
C 2080 442 1952 403 1786 403 
C 1651 403 1542 442 1472 512 
C 1414 570 1389 666 1389 851 
L 1389 3078 
L 1696 3078 
C 1984 3078 2144 3072 2349 3040 
L 2349 3526 
C 2125 3494 1990 3488 1696 3488 
L 1389 3488 
L 1389 3731 
C 1389 4006 1402 4237 1434 4442 
L 858 4442 
z
" transform="scale(0.015625)"/>
        <path id="01-bulk-write-HiraginoSansGB-W3-6f" d="M 2010 -45 
C 3040 -45 3706 672 3706 1766 
C 3706 2867 3046 3571 2003 3571 
C 960 3571 294 2867 294 1760 
C 294 653 960 -45 2010 -45 
z
M 2016 3168 
C 2752 3168 3213 2630 3213 1766 
C 3213 902 2752 371 2003 371 
C 1254 371 787 902 787 1773 
C 787 2637 1254 3168 2016 3168 
z
" transform="scale(0.015625)"/>
        <path id="01-bulk-write-HiraginoSansGB-W3-6b" d="M 454 4902 
C 486 4672 499 4499 499 4179 
L 499 762 
C 499 454 486 269 454 38 
L 1024 38 
C 992 262 979 467 979 755 
L 979 1293 
L 1523 1811 
L 2374 646 
C 2694 218 2707 192 2778 38 
L 3507 38 
C 3366 179 3334 218 2950 710 
L 1869 2106 
L 2758 2931 
C 2771 2944 2803 2982 2861 3034 
C 2950 3123 3027 3194 3091 3251 
C 3264 3405 3264 3405 3354 3488 
L 2592 3488 
C 2502 3354 2413 3245 2138 2982 
L 979 1862 
L 979 4179 
C 979 4506 992 4672 1024 4902 
L 454 4902 
z
" transform="scale(0.015625)"/>
        <path id="01-bulk-write-HiraginoSansGB-W3-65" d="M 3475 1690 
C 3462 1862 3437 2112 3430 2182 
C 3302 3040 2707 3571 1882 3571 
C 922 3571 294 2854 294 1760 
C 294 653 928 -51 1933 -51 
C 2400 -51 2790 102 3091 397 
C 3238 544 3322 659 3443 928 
L 2995 1107 
C 2816 614 2458 358 1939 358 
C 1555 358 1242 512 1037 794 
C 864 1024 800 1261 781 1690 
L 3475 1690 
z
M 800 2093 
C 838 2336 870 2451 960 2611 
C 1152 2970 1478 3168 1869 3168 
C 2176 3168 2426 3072 2624 2880 
C 2829 2682 2925 2451 2963 2093 
L 800 2093 
z
" transform="scale(0.015625)"/>
        <path id="01-bulk-write-HiraginoSansGB-W3-6e" d="M 1030 38 
C 998 288 986 480 986 762 
L 986 1997 
C 1037 2253 1114 2413 1280 2630 
C 1549 2976 1837 3136 2195 3136 
C 2701 3136 2976 2829 2976 2253 
L 2976 762 
C 2976 480 2963 288 2931 38 
L 3507 38 
C 3475 250 3462 429 3462 755 
L 3462 2336 
C 3462 2650 3405 2886 3283 3072 
C 3078 3386 2720 3552 2259 3552 
C 1683 3552 1267 3302 966 2778 
C 986 2925 986 2976 986 3053 
L 986 3488 
L 454 3488 
C 493 3232 506 3053 506 2765 
L 506 762 
C 506 454 493 282 454 38 
L 1030 38 
z
" transform="scale(0.015625)"/>
        <path id="01-bulk-write-HiraginoSansGB-W3-5206" d="M 4435 2637 
C 4435 1530 4358 666 4224 275 
C 4179 147 4109 115 3917 115 
C 3661 115 3373 147 3002 218 
C 3066 32 3085 -64 3104 -237 
C 3520 -275 3718 -288 3942 -288 
C 4320 -288 4467 -230 4576 -32 
C 4742 269 4864 1350 4864 2592 
C 4870 2880 4870 2931 4890 3046 
C 4704 3021 4512 3014 4218 3014 
L 2112 3014 
C 1850 3014 1632 3027 1446 3053 
L 1446 2598 
C 1645 2624 1862 2637 2106 2637 
L 2509 2637 
C 2464 2061 2374 1722 2163 1318 
C 1856 736 1370 314 550 -83 
C 685 -211 749 -294 832 -442 
C 1613 -38 2112 397 2445 954 
C 2739 1440 2893 1965 2944 2637 
L 4435 2637 
z
M 2246 5293 
C 2214 5101 2131 4902 1971 4621 
C 1542 3853 992 3264 218 2746 
C 365 2624 448 2528 531 2400 
C 1434 3066 2054 3808 2547 4800 
C 2643 4998 2669 5050 2726 5126 
L 2246 5293 
z
M 3667 5114 
C 3699 5062 3738 4992 3770 4934 
C 4371 3757 4915 3110 5926 2362 
C 6022 2528 6106 2637 6227 2752 
C 5350 3328 4806 3904 4339 4742 
C 4205 4986 4147 5114 4096 5280 
L 3667 5114 
z
" transform="scale(0.015625)"/>
        <path id="01-bulk-write-HiraginoSansGB-W3-7247" d="M 1683 3661 
L 1683 4474 
C 1683 4768 1696 5011 1722 5222 
L 1229 5222 
C 1254 5005 1267 4787 1267 4429 
L 1267 3386 
C 1267 2374 1178 1600 1005 1050 
C 851 563 659 237 326 -96 
C 461 -179 582 -294 685 -435 
C 1222 205 1453 774 1594 1779 
L 3955 1779 
L 3955 262 
C 3955 -58 3942 -262 3917 -480 
L 4442 -480 
C 4410 -262 4397 -38 4397 256 
L 4397 1581 
C 4397 1798 4403 1990 4429 2182 
C 4218 2163 4070 2157 3789 2157 
L 1632 2157 
C 1658 2368 1670 2733 1683 3277 
L 5242 3277 
C 5504 3277 5722 3264 5914 3232 
L 5914 3706 
C 5728 3674 5510 3661 5235 3661 
L 3987 3661 
L 3987 4640 
C 3987 4954 3994 5120 4026 5318 
L 3520 5318 
C 3546 5120 3558 4934 3558 4640 
L 3558 3661 
L 1683 3661 
z
" transform="scale(0.015625)"/>
       </defs>
       <use xlink:href="#01-bulk-write-HiraginoSansGB-W3-32"/>
       <use xlink:href="#01-bulk-write-HiraginoSansGB-W3-30" x="65.699997"/>
       <use xlink:href="#01-bulk-write-HiraginoSansGB-W3-34" x="131.399994"/>
       <use xlink:href="#01-bulk-write-HiraginoSansGB-W3-39" x="197.499985"/>
       <use xlink:href="#01-bulk-write-HiraginoSansGB-W3-20" x="263.199982"/>
       <use xlink:href="#01-bulk-write-HiraginoSansGB-W3-884c" x="296.499969"/>
       <use xlink:href="#01-bulk-write-HiraginoSansGB-W3-20" x="396.499954"/>
       <use xlink:href="#01-bulk-write-HiraginoSansGB-W3-2f" x="429.799942"/>
       <use xlink:href="#01-bulk-write-HiraginoSansGB-W3-20" x="477.199936"/>
       <use xlink:href="#01-bulk-write-HiraginoSansGB-W3-74" x="510.499924"/>
       <use xlink:href="#01-bulk-write-HiraginoSansGB-W3-6f" x="549.699921"/>
       <use xlink:href="#01-bulk-write-HiraginoSansGB-W3-6b" x="612.199905"/>
       <use xlink:href="#01-bulk-write-HiraginoSansGB-W3-65" x="669.69989"/>
       <use xlink:href="#01-bulk-write-HiraginoSansGB-W3-6e" x="728.499878"/>
       <use xlink:href="#01-bulk-write-HiraginoSansGB-W3-20" x="790.299866"/>
       <use xlink:href="#01-bulk-write-HiraginoSansGB-W3-5206" x="823.599854"/>
       <use xlink:href="#01-bulk-write-HiraginoSansGB-W3-7247" x="923.599838"/>
      </g>
     </g>
    </g>
    <g id="01-bulk-write-ytick_2">
     <g id="01-bulk-write-line2d_18"/>
     <g id="01-bulk-write-text_11">
      <!-- 2049 行 / feature 分片 -->
      <g style="fill: #243a56" transform="translate(7.2 261.31125) scale(0.12 -0.12)">
       <defs>
        <path id="01-bulk-write-HiraginoSansGB-W3-66" d="M 915 3078 
L 915 762 
C 915 493 902 275 870 38 
L 1446 38 
C 1414 275 1402 454 1402 755 
L 1402 3078 
L 1722 3078 
C 2035 3078 2182 3072 2400 3040 
L 2400 3526 
C 2163 3494 1997 3488 1722 3488 
L 1402 3488 
L 1402 3840 
C 1402 4141 1427 4275 1498 4378 
C 1581 4493 1728 4550 1933 4550 
C 2131 4550 2291 4506 2464 4403 
L 2598 4832 
C 2368 4922 2170 4960 1920 4960 
C 1562 4960 1267 4845 1114 4646 
C 966 4461 915 4256 915 3866 
L 915 3488 
L 653 3488 
C 397 3488 243 3501 70 3526 
L 70 3040 
C 269 3066 429 3078 653 3078 
L 915 3078 
z
" transform="scale(0.015625)"/>
        <path id="01-bulk-write-HiraginoSansGB-W3-61" d="M 2733 38 
L 3251 38 
C 3213 294 3200 454 3200 742 
L 3200 2541 
C 3200 3232 2765 3590 1920 3590 
C 1440 3590 1056 3469 794 3226 
C 634 3085 550 2950 435 2656 
L 934 2547 
C 998 2739 1050 2829 1133 2918 
C 1312 3104 1574 3194 1926 3194 
C 2458 3194 2733 2970 2733 2534 
L 2733 2163 
C 1946 2144 1638 2106 1261 1997 
C 666 1830 346 1459 346 947 
C 346 339 774 -26 1478 -26 
C 1696 -26 1926 19 2144 102 
C 2362 186 2483 269 2733 493 
L 2733 38 
z
M 2733 1370 
C 2733 1114 2682 979 2534 819 
C 2285 544 1882 365 1510 365 
C 1094 365 832 589 832 941 
C 832 1248 992 1466 1318 1600 
C 1606 1722 1971 1766 2733 1786 
L 2733 1370 
z
" transform="scale(0.015625)"/>
        <path id="01-bulk-write-HiraginoSansGB-W3-75" d="M 2970 38 
L 3501 38 
C 3462 294 3450 474 3450 762 
L 3450 2765 
C 3450 3059 3462 3264 3494 3488 
L 2925 3488 
C 2957 3264 2970 3040 2970 2765 
L 2970 1530 
C 2912 1274 2842 1114 2675 890 
C 2419 544 2118 384 1754 384 
C 1254 384 979 704 979 1274 
L 979 2765 
C 979 3046 992 3264 1024 3488 
L 448 3488 
C 480 3283 493 3059 493 2771 
L 493 1190 
C 493 883 557 634 672 448 
C 877 141 1235 -26 1696 -26 
C 2272 -26 2669 211 2989 736 
C 2970 570 2970 525 2970 454 
L 2970 38 
z
" transform="scale(0.015625)"/>
        <path id="01-bulk-write-HiraginoSansGB-W3-72" d="M 2669 3488 
C 2400 3533 2291 3546 2099 3546 
C 1792 3546 1574 3482 1331 3322 
C 1197 3232 1101 3130 979 2931 
C 986 2976 986 3072 986 3123 
L 986 3488 
L 454 3488 
C 486 3258 499 3085 499 2765 
L 499 762 
C 499 499 486 333 454 38 
L 1030 38 
C 998 288 986 467 986 755 
L 986 2061 
C 1011 2349 1069 2502 1222 2701 
C 1414 2957 1683 3091 2010 3091 
C 2170 3091 2285 3072 2528 2995 
L 2669 3488 
z
" transform="scale(0.015625)"/>
       </defs>
       <use xlink:href="#01-bulk-write-HiraginoSansGB-W3-32"/>
       <use xlink:href="#01-bulk-write-HiraginoSansGB-W3-30" x="65.699997"/>
       <use xlink:href="#01-bulk-write-HiraginoSansGB-W3-34" x="131.399994"/>
       <use xlink:href="#01-bulk-write-HiraginoSansGB-W3-39" x="197.499985"/>
       <use xlink:href="#01-bulk-write-HiraginoSansGB-W3-20" x="263.199982"/>
       <use xlink:href="#01-bulk-write-HiraginoSansGB-W3-884c" x="296.499969"/>
       <use xlink:href="#01-bulk-write-HiraginoSansGB-W3-20" x="396.499954"/>
       <use xlink:href="#01-bulk-write-HiraginoSansGB-W3-2f" x="429.799942"/>
       <use xlink:href="#01-bulk-write-HiraginoSansGB-W3-20" x="477.199936"/>
       <use xlink:href="#01-bulk-write-HiraginoSansGB-W3-66" x="510.499924"/>
       <use xlink:href="#01-bulk-write-HiraginoSansGB-W3-65" x="552.199921"/>
       <use xlink:href="#01-bulk-write-HiraginoSansGB-W3-61" x="610.999908"/>
       <use xlink:href="#01-bulk-write-HiraginoSansGB-W3-74" x="668.499893"/>
       <use xlink:href="#01-bulk-write-HiraginoSansGB-W3-75" x="707.69989"/>
       <use xlink:href="#01-bulk-write-HiraginoSansGB-W3-72" x="769.499878"/>
       <use xlink:href="#01-bulk-write-HiraginoSansGB-W3-65" x="812.499863"/>
       <use xlink:href="#01-bulk-write-HiraginoSansGB-W3-20" x="871.29985"/>
       <use xlink:href="#01-bulk-write-HiraginoSansGB-W3-5206" x="904.599838"/>
       <use xlink:href="#01-bulk-write-HiraginoSansGB-W3-7247" x="1004.599823"/>
      </g>
     </g>
    </g>
   </g>
   <g id="01-bulk-write-patch_3">
    <path d="M 147.75125 89.682323 
L 304.409075 89.682323 
L 304.409075 110.515963 
L 147.75125 110.515963 
z
" clip-path="url(#01-bulk-write-paeb002f85f)" style="fill: url(#01-bulk-write-h37cf2cdc2a); stroke: #467bb5; stroke-width: 0.5; stroke-linejoin: miter"/>
   </g>
   <g id="01-bulk-write-patch_4">
    <path d="M 147.75125 208.731692 
L 304.608893 208.731692 
L 304.608893 229.565331 
L 147.75125 229.565331 
z
" clip-path="url(#01-bulk-write-paeb002f85f)" style="fill: url(#01-bulk-write-h37cf2cdc2a); stroke: #467bb5; stroke-width: 0.5; stroke-linejoin: miter"/>
   </g>
   <g id="01-bulk-write-patch_5">
    <path d="M 147.75125 114.682691 
L 239.468012 114.682691 
L 239.468012 135.51633 
L 147.75125 135.51633 
z
" clip-path="url(#01-bulk-write-paeb002f85f)" style="fill: #467bb5; stroke: #467bb5; stroke-width: 0.5; stroke-linejoin: miter"/>
   </g>
   <g id="01-bulk-write-patch_6">
    <path d="M 147.75125 233.732059 
L 246.261846 233.732059 
L 246.261846 254.565699 
L 147.75125 254.565699 
z
" clip-path="url(#01-bulk-write-paeb002f85f)" style="fill: #467bb5; stroke: #467bb5; stroke-width: 0.5; stroke-linejoin: miter"/>
   </g>
   <g id="01-bulk-write-patch_7">
    <path d="M 147.75125 139.683058 
L 428.696278 139.683058 
L 428.696278 160.516697 
L 147.75125 160.516697 
z
" clip-path="url(#01-bulk-write-paeb002f85f)" style="fill: url(#01-bulk-write-h8d297617cc); stroke: #008477; stroke-width: 0.5; stroke-linejoin: miter"/>
   </g>
   <g id="01-bulk-write-patch_8">
    <path d="M 147.75125 258.732427 
L 428.496459 258.732427 
L 428.496459 279.566066 
L 147.75125 279.566066 
z
" clip-path="url(#01-bulk-write-paeb002f85f)" style="fill: url(#01-bulk-write-h8d297617cc); stroke: #008477; stroke-width: 0.5; stroke-linejoin: miter"/>
   </g>
   <g id="01-bulk-write-patch_9">
    <path d="M 147.75125 164.683425 
L 237.070188 164.683425 
L 237.070188 185.517065 
L 147.75125 185.517065 
z
" clip-path="url(#01-bulk-write-paeb002f85f)" style="fill: #008477; stroke: #008477; stroke-width: 0.5; stroke-linejoin: miter"/>
   </g>
   <g id="01-bulk-write-patch_10">
    <path d="M 147.75125 283.732794 
L 255.453505 283.732794 
L 255.453505 304.566433 
L 147.75125 304.566433 
z
" clip-path="url(#01-bulk-write-paeb002f85f)" style="fill: #008477; stroke: #008477; stroke-width: 0.5; stroke-linejoin: miter"/>
   </g>
   <g id="01-bulk-write-patch_11">
    <path d="M 147.75125 328.078684 
L 517.41576 328.078684 
" style="fill: none; stroke: #d7e0eb; stroke-width: 0.8; stroke-linejoin: miter; stroke-linecap: square"/>
   </g>
   <g id="01-bulk-write-text_12">
    <!-- 0.0784 -->
    <g style="fill: #405570" transform="translate(308.845049 102.908518) scale(0.1 -0.1)">
     <defs>
      <path id="01-bulk-write-HiraginoSansGB-W3-38" d="M 2752 2586 
C 3322 2694 3680 3142 3680 3738 
C 3680 4525 3078 5005 2099 5005 
C 1133 5005 525 4512 525 3738 
C 525 3142 890 2688 1453 2586 
C 1216 2547 1075 2496 877 2368 
C 493 2118 307 1766 307 1306 
C 307 422 954 -64 2125 -64 
C 3251 -64 3891 429 3891 1306 
C 3891 1734 3731 2074 3411 2310 
C 3200 2470 3021 2547 2752 2586 
z
M 2099 4576 
C 2758 4576 3181 4237 3181 3699 
C 3181 3130 2778 2784 2106 2784 
C 1434 2784 1024 3130 1024 3699 
C 1024 4230 1446 4576 2099 4576 
z
M 2099 2342 
C 2886 2342 3366 1965 3366 1338 
C 3366 1075 3283 851 3123 704 
C 2906 486 2541 371 2093 371 
C 1299 371 832 730 832 1338 
C 832 1958 1312 2342 2099 2342 
z
" transform="scale(0.015625)"/>
     </defs>
     <use xlink:href="#01-bulk-write-HiraginoSansGB-W3-30"/>
     <use xlink:href="#01-bulk-write-HiraginoSansGB-W3-2e" x="65.699997"/>
     <use xlink:href="#01-bulk-write-HiraginoSansGB-W3-30" x="89.699982"/>
     <use xlink:href="#01-bulk-write-HiraginoSansGB-W3-37" x="155.399979"/>
     <use xlink:href="#01-bulk-write-HiraginoSansGB-W3-38" x="221.099976"/>
     <use xlink:href="#01-bulk-write-HiraginoSansGB-W3-34" x="286.799973"/>
    </g>
   </g>
   <g id="01-bulk-write-text_13">
    <!-- 0.0785 -->
    <g style="fill: #405570" transform="translate(309.044868 221.957886) scale(0.1 -0.1)">
     <use xlink:href="#01-bulk-write-HiraginoSansGB-W3-30"/>
     <use xlink:href="#01-bulk-write-HiraginoSansGB-W3-2e" x="65.699997"/>
     <use xlink:href="#01-bulk-write-HiraginoSansGB-W3-30" x="89.699982"/>
     <use xlink:href="#01-bulk-write-HiraginoSansGB-W3-37" x="155.399979"/>
     <use xlink:href="#01-bulk-write-HiraginoSansGB-W3-38" x="221.099976"/>
     <use xlink:href="#01-bulk-write-HiraginoSansGB-W3-35" x="286.799973"/>
    </g>
   </g>
   <g id="01-bulk-write-text_14">
    <!-- 0.0459 -->
    <g style="fill: #467bb5" transform="translate(243.903986 127.908885) scale(0.1 -0.1)">
     <use xlink:href="#01-bulk-write-HiraginoSansGB-W3-30"/>
     <use xlink:href="#01-bulk-write-HiraginoSansGB-W3-2e" x="65.699997"/>
     <use xlink:href="#01-bulk-write-HiraginoSansGB-W3-30" x="89.699982"/>
     <use xlink:href="#01-bulk-write-HiraginoSansGB-W3-34" x="155.399979"/>
     <use xlink:href="#01-bulk-write-HiraginoSansGB-W3-35" x="221.499969"/>
     <use xlink:href="#01-bulk-write-HiraginoSansGB-W3-39" x="287.199966"/>
    </g>
   </g>
   <g id="01-bulk-write-text_15">
    <!-- 0.0493 -->
    <g style="fill: #467bb5" transform="translate(250.697821 246.958254) scale(0.1 -0.1)">
     <defs>
      <path id="01-bulk-write-HiraginoSansGB-W3-33" d="M 288 1088 
C 403 762 480 621 614 467 
C 922 122 1402 -58 2048 -58 
C 3117 -58 3782 480 3782 1344 
C 3782 1754 3635 2093 3354 2310 
C 3136 2477 2938 2547 2566 2586 
C 2861 2643 3002 2694 3168 2816 
C 3450 3014 3616 3360 3616 3750 
C 3616 4518 3027 5005 2099 5005 
C 1542 5005 1107 4845 826 4538 
C 666 4371 589 4224 480 3898 
L 1011 3782 
C 1082 4019 1126 4122 1216 4237 
C 1395 4467 1677 4576 2067 4576 
C 2714 4576 3098 4256 3098 3706 
C 3098 3475 3002 3238 2842 3085 
C 2643 2886 2330 2790 1907 2790 
C 1734 2790 1581 2803 1434 2829 
L 1434 2323 
C 1606 2342 1766 2355 1946 2355 
C 2797 2355 3251 2010 3251 1376 
C 3251 768 2784 384 2042 384 
C 1715 384 1434 461 1216 614 
C 1018 755 928 890 826 1222 
L 288 1088 
z
" transform="scale(0.015625)"/>
     </defs>
     <use xlink:href="#01-bulk-write-HiraginoSansGB-W3-30"/>
     <use xlink:href="#01-bulk-write-HiraginoSansGB-W3-2e" x="65.699997"/>
     <use xlink:href="#01-bulk-write-HiraginoSansGB-W3-30" x="89.699982"/>
     <use xlink:href="#01-bulk-write-HiraginoSansGB-W3-34" x="155.399979"/>
     <use xlink:href="#01-bulk-write-HiraginoSansGB-W3-39" x="221.499969"/>
     <use xlink:href="#01-bulk-write-HiraginoSansGB-W3-33" x="287.199966"/>
    </g>
   </g>
   <g id="01-bulk-write-text_16">
    <!-- 0.1406 -->
    <g style="fill: #405570" transform="translate(433.132252 152.909253) scale(0.1 -0.1)">
     <defs>
      <path id="01-bulk-write-HiraginoSansGB-W3-36" d="M 3725 4006 
C 3539 4390 3411 4563 3187 4723 
C 2944 4896 2598 4992 2214 4992 
C 1658 4992 1203 4755 864 4294 
C 557 3878 378 3162 378 2362 
C 378 1453 608 736 1024 352 
C 1299 96 1728 -51 2182 -51 
C 3194 -51 3859 602 3859 1594 
C 3859 2579 3245 3194 2253 3194 
C 1811 3194 1472 3072 1171 2803 
C 1030 2682 966 2592 851 2394 
C 896 3091 954 3386 1120 3770 
C 1350 4294 1728 4563 2227 4563 
C 2547 4563 2797 4454 2989 4243 
C 3085 4134 3130 4045 3213 3840 
L 3725 4006 
z
M 3354 1581 
C 3354 1331 3290 1069 3187 902 
C 2989 582 2605 384 2189 384 
C 1498 384 979 896 979 1587 
C 979 1907 1088 2189 1299 2400 
C 1523 2624 1862 2765 2182 2765 
C 2931 2765 3354 2336 3354 1581 
z
" transform="scale(0.015625)"/>
     </defs>
     <use xlink:href="#01-bulk-write-HiraginoSansGB-W3-30"/>
     <use xlink:href="#01-bulk-write-HiraginoSansGB-W3-2e" x="65.699997"/>
     <use xlink:href="#01-bulk-write-HiraginoSansGB-W3-31" x="89.699982"/>
     <use xlink:href="#01-bulk-write-HiraginoSansGB-W3-34" x="155.399979"/>
     <use xlink:href="#01-bulk-write-HiraginoSansGB-W3-30" x="221.499969"/>
     <use xlink:href="#01-bulk-write-HiraginoSansGB-W3-36" x="287.199966"/>
    </g>
   </g>
   <g id="01-bulk-write-text_17">
    <!-- 0.1405 -->
    <g style="fill: #405570" transform="translate(432.932433 271.958621) scale(0.1 -0.1)">
     <use xlink:href="#01-bulk-write-HiraginoSansGB-W3-30"/>
     <use xlink:href="#01-bulk-write-HiraginoSansGB-W3-2e" x="65.699997"/>
     <use xlink:href="#01-bulk-write-HiraginoSansGB-W3-31" x="89.699982"/>
     <use xlink:href="#01-bulk-write-HiraginoSansGB-W3-34" x="155.399979"/>
     <use xlink:href="#01-bulk-write-HiraginoSansGB-W3-30" x="221.499969"/>
     <use xlink:href="#01-bulk-write-HiraginoSansGB-W3-35" x="287.199966"/>
    </g>
   </g>
   <g id="01-bulk-write-text_18">
    <!-- 0.0447 -->
    <g style="fill: #008477" transform="translate(241.506162 177.90962) scale(0.1 -0.1)">
     <use xlink:href="#01-bulk-write-HiraginoSansGB-W3-30"/>
     <use xlink:href="#01-bulk-write-HiraginoSansGB-W3-2e" x="65.699997"/>
     <use xlink:href="#01-bulk-write-HiraginoSansGB-W3-30" x="89.699982"/>
     <use xlink:href="#01-bulk-write-HiraginoSansGB-W3-34" x="155.399979"/>
     <use xlink:href="#01-bulk-write-HiraginoSansGB-W3-34" x="221.499969"/>
     <use xlink:href="#01-bulk-write-HiraginoSansGB-W3-37" x="287.59996"/>
    </g>
   </g>
   <g id="01-bulk-write-text_19">
    <!-- 0.0539 -->
    <g style="fill: #008477" transform="translate(259.889479 296.958989) scale(0.1 -0.1)">
     <use xlink:href="#01-bulk-write-HiraginoSansGB-W3-30"/>
     <use xlink:href="#01-bulk-write-HiraginoSansGB-W3-2e" x="65.699997"/>
     <use xlink:href="#01-bulk-write-HiraginoSansGB-W3-30" x="89.699982"/>
     <use xlink:href="#01-bulk-write-HiraginoSansGB-W3-35" x="155.399979"/>
     <use xlink:href="#01-bulk-write-HiraginoSansGB-W3-33" x="221.099976"/>
     <use xlink:href="#01-bulk-write-HiraginoSansGB-W3-39" x="286.799973"/>
    </g>
   </g>
   <g id="01-bulk-write-text_20">
    <!-- P 侧 -->
    <g style="fill: #192c46" transform="translate(317.621005 42.312668) scale(0.15 -0.15)">
     <defs>
      <path id="01-bulk-write-HiraginoSansGB-W3-4fa7" d="M 1299 3846 
C 1498 4288 1568 4480 1702 4896 
C 1760 5094 1779 5139 1811 5216 
L 1395 5350 
C 1376 5178 1325 4979 1222 4678 
C 954 3891 589 3238 90 2643 
C 218 2515 282 2432 352 2298 
C 589 2611 762 2906 934 3277 
C 915 2566 915 2566 915 2298 
L 915 333 
C 915 -32 902 -256 877 -474 
L 1325 -474 
C 1306 -250 1299 -64 1299 339 
L 1299 3846 
z
M 4851 1043 
C 4826 1203 4819 1395 4819 1715 
L 4819 4205 
C 4819 4531 4826 4698 4858 4877 
L 4390 4877 
C 4416 4691 4422 4512 4422 4205 
L 4422 1715 
C 4422 1389 4416 1222 4384 1043 
L 4851 1043 
z
M 2336 1050 
C 2330 1197 2323 1363 2323 1626 
L 2323 4730 
L 3584 4730 
L 3584 1645 
C 3584 1440 3571 1171 3558 1050 
L 3981 1050 
C 3968 1235 3962 1427 3962 1658 
L 3962 4506 
C 3962 4736 3968 4947 3981 5094 
C 3840 5075 3693 5069 3405 5069 
L 2483 5069 
C 2208 5069 2099 5075 1920 5088 
C 1939 4909 1946 4736 1946 4486 
L 1946 1626 
C 1946 1389 1939 1210 1926 1050 
L 2336 1050 
z
M 2989 557 
C 3322 282 3610 -32 3891 -403 
L 4160 -102 
C 3968 141 3744 365 3219 832 
L 2989 557 
z
M 5440 5184 
C 5466 4973 5478 4774 5478 4435 
L 5478 147 
C 5478 6 5427 -19 5165 -19 
C 4992 -19 4826 -6 4525 32 
C 4582 -102 4602 -179 4627 -371 
C 4851 -384 4934 -384 5152 -384 
C 5734 -384 5875 -288 5875 102 
L 5875 4435 
C 5875 4787 5888 5011 5914 5184 
L 5440 5184 
z
M 2726 4262 
C 2752 4032 2758 3910 2758 3437 
L 2758 2381 
C 2758 1466 2701 1082 2522 742 
C 2336 403 2099 166 1619 -154 
C 1779 -275 1811 -320 1914 -480 
C 2394 -115 2630 128 2816 442 
C 3072 864 3142 1299 3142 2381 
L 3142 3450 
C 3142 3802 3149 4058 3168 4262 
L 2726 4262 
z
" transform="scale(0.015625)"/>
     </defs>
     <use xlink:href="#01-bulk-write-HiraginoSansGB-W3-50"/>
     <use xlink:href="#01-bulk-write-HiraginoSansGB-W3-20" x="66.199997"/>
     <use xlink:href="#01-bulk-write-HiraginoSansGB-W3-4fa7" x="99.499985"/>
    </g>
   </g>
   <g id="01-bulk-write-legend_1">
    <g id="01-bulk-write-patch_12">
     <path d="M 232.449989 374.380598 
L 254.449989 374.380598 
L 254.449989 366.680598 
L 232.449989 366.680598 
z
" style="fill: url(#01-bulk-write-h37cf2cdc2a); stroke: #467bb5; stroke-width: 0.5; stroke-linejoin: miter"/>
    </g>
    <g id="01-bulk-write-text_21">
     <!-- TP2 独立池 -->
     <g style="fill: #192c46" transform="translate(263.249989 374.380598) scale(0.11 -0.11)">
      <defs>
       <path id="01-bulk-write-HiraginoSansGB-W3-54" d="M 2323 4454 
L 3315 4454 
C 3674 4454 3859 4442 4070 4410 
L 4070 4947 
C 3859 4915 3686 4902 3315 4902 
L 819 4902 
C 480 4902 314 4915 90 4947 
L 90 4410 
C 301 4442 499 4454 826 4454 
L 1824 4454 
L 1824 762 
C 1824 461 1811 237 1773 38 
L 2374 38 
C 2336 269 2323 448 2323 762 
L 2323 4454 
z
" transform="scale(0.015625)"/>
       <path id="01-bulk-write-HiraginoSansGB-W3-72ec" d="M 998 5101 
C 954 5152 928 5184 928 5184 
L 589 4934 
C 851 4717 1018 4499 1203 4134 
C 813 3840 666 3744 224 3533 
C 320 3430 384 3328 454 3187 
C 845 3411 1050 3552 1357 3789 
C 1466 3494 1542 3238 1594 2944 
C 1248 2355 851 1939 243 1542 
C 333 1459 403 1350 467 1203 
C 1037 1632 1376 1984 1670 2438 
C 1702 2125 1709 1907 1709 1542 
C 1709 902 1658 384 1568 154 
C 1517 6 1440 -51 1312 -51 
C 1139 -51 883 6 595 102 
C 640 -64 653 -166 659 -333 
C 934 -397 1139 -429 1325 -429 
C 1574 -429 1715 -371 1830 -230 
C 2010 6 2099 570 2099 1472 
C 2099 2522 1958 3366 1670 4051 
C 1958 4307 2195 4557 2477 4915 
C 2522 4973 2528 4979 2560 5018 
L 2208 5280 
C 2086 5011 1837 4698 1504 4397 
C 1402 4570 1274 4755 1190 4864 
L 998 5101 
z
M 4307 1722 
L 5178 1722 
C 5472 1722 5619 1715 5798 1696 
C 5779 1862 5773 2029 5773 2330 
L 5773 3488 
C 5773 3763 5779 3923 5798 4096 
C 5594 4070 5434 4064 5190 4064 
L 4307 4064 
L 4307 4621 
C 4307 4890 4320 5037 4352 5216 
L 3872 5216 
C 3910 5011 3917 4883 3917 4614 
L 3917 4064 
L 3142 4064 
C 2893 4064 2720 4070 2515 4096 
C 2534 3910 2541 3744 2541 3456 
L 2541 2317 
C 2541 2054 2534 1888 2522 1696 
C 2701 1715 2867 1722 3136 1722 
L 3917 1722 
L 3917 256 
C 3219 173 2637 122 2413 122 
C 2349 122 2323 122 2253 128 
L 2349 -333 
C 2483 -301 2573 -288 2694 -275 
C 4038 -115 4755 -6 5562 160 
C 5638 -19 5677 -134 5779 -474 
L 6157 -282 
C 5869 512 5734 806 5389 1395 
L 5030 1274 
C 5222 934 5280 826 5434 486 
C 5030 403 4678 346 4307 301 
L 4307 1722 
z
M 3917 2086 
L 2925 2086 
L 2925 3699 
L 3917 3699 
L 3917 2086 
z
M 4307 2086 
L 4307 3699 
L 5382 3699 
L 5382 2086 
L 4307 2086 
z
" transform="scale(0.015625)"/>
       <path id="01-bulk-write-HiraginoSansGB-W3-7acb" d="M 3168 4237 
L 3469 4429 
C 3315 4691 3104 4966 2784 5325 
L 2432 5101 
C 2739 4755 2861 4589 3066 4237 
L 1312 4237 
C 992 4237 781 4250 602 4282 
L 602 3802 
C 806 3827 1018 3840 1312 3840 
L 5062 3840 
C 5370 3840 5587 3827 5798 3802 
L 5798 4282 
C 5600 4250 5382 4237 5062 4237 
L 3168 4237 
z
M 1088 346 
C 774 346 557 358 352 384 
L 352 -102 
C 557 -70 768 -58 1088 -58 
L 5350 -58 
C 5664 -58 5850 -70 6054 -102 
L 6054 384 
C 5862 358 5645 346 5350 346 
L 3872 346 
C 4198 1043 4486 1786 4730 2579 
C 4922 3200 4922 3200 4986 3354 
L 4493 3520 
C 4397 2810 3885 1299 3418 346 
L 1088 346 
z
M 1459 3296 
C 1766 2528 1990 1715 2157 762 
L 2573 922 
C 2368 1971 2189 2611 1882 3450 
L 1459 3296 
z
" transform="scale(0.015625)"/>
       <path id="01-bulk-write-HiraginoSansGB-W3-6c60" d="M 3872 3520 
L 2874 3181 
L 2874 4038 
C 2874 4301 2886 4499 2912 4672 
L 2432 4672 
C 2458 4474 2470 4288 2470 4019 
L 2470 3046 
L 2330 2995 
C 2131 2931 1971 2886 1779 2861 
L 1901 2413 
C 2016 2470 2157 2528 2426 2624 
L 2470 2637 
L 2470 326 
C 2470 77 2534 -38 2720 -134 
C 2918 -230 3290 -262 4160 -262 
C 5165 -262 5568 -211 5754 -58 
C 5926 83 6022 358 6112 986 
C 5939 1024 5869 1050 5702 1146 
C 5658 614 5587 352 5472 256 
C 5331 154 4992 115 4134 115 
C 3533 115 3155 134 3034 179 
C 2912 218 2874 275 2874 422 
L 2874 2778 
L 3872 3123 
L 3872 1504 
C 3872 1242 3859 1069 3834 883 
L 4307 883 
C 4282 1056 4269 1229 4269 1498 
L 4269 3258 
L 5408 3654 
C 5395 2739 5344 2035 5280 1914 
C 5242 1837 5158 1798 5024 1798 
C 4877 1798 4736 1818 4525 1869 
C 4582 1709 4608 1619 4634 1466 
C 4794 1440 4902 1434 5069 1434 
C 5261 1434 5344 1453 5446 1504 
C 5555 1562 5638 1709 5683 1907 
C 5728 2150 5779 2970 5811 4205 
C 5690 4147 5523 4077 5312 4006 
L 4269 3654 
L 4269 4685 
C 4269 4941 4282 5114 4307 5293 
L 3834 5293 
C 3859 5107 3872 4934 3872 4691 
L 3872 3520 
z
M 608 4934 
C 1030 4646 1222 4480 1600 4064 
L 1875 4378 
C 1523 4736 1274 4947 870 5222 
L 608 4934 
z
M 269 3354 
C 736 3066 992 2867 1350 2509 
L 1594 2854 
C 1254 3162 979 3360 499 3654 
L 269 3354 
z
M 1331 1869 
C 1056 1005 698 294 378 -19 
L 749 -352 
C 813 -243 832 -211 877 -134 
C 986 45 1030 128 1094 250 
C 1299 640 1466 1043 1658 1613 
L 1331 1869 
z
" transform="scale(0.015625)"/>
      </defs>
      <use xlink:href="#01-bulk-write-HiraginoSansGB-W3-54"/>
      <use xlink:href="#01-bulk-write-HiraginoSansGB-W3-50" x="64.899994"/>
      <use xlink:href="#01-bulk-write-HiraginoSansGB-W3-32" x="131.099991"/>
      <use xlink:href="#01-bulk-write-HiraginoSansGB-W3-20" x="196.799988"/>
      <use xlink:href="#01-bulk-write-HiraginoSansGB-W3-72ec" x="230.099976"/>
      <use xlink:href="#01-bulk-write-HiraginoSansGB-W3-7acb" x="330.09996"/>
      <use xlink:href="#01-bulk-write-HiraginoSansGB-W3-6c60" x="430.099945"/>
     </g>
    </g>
    <g id="01-bulk-write-patch_13">
     <path d="M 232.449989 390.550598 
L 254.449989 390.550598 
L 254.449989 382.850598 
L 232.449989 382.850598 
z
" style="fill: #467bb5; stroke: #467bb5; stroke-width: 0.5; stroke-linejoin: miter"/>
    </g>
    <g id="01-bulk-write-text_22">
     <!-- TP2 共享池 -->
     <g style="fill: #192c46" transform="translate(263.249989 390.550598) scale(0.11 -0.11)">
      <defs>
       <path id="01-bulk-write-HiraginoSansGB-W3-5171" d="M 1920 3987 
L 1299 3987 
C 1043 3987 838 4000 653 4026 
L 653 3571 
C 826 3597 1005 3603 1299 3603 
L 1920 3603 
L 1920 1984 
L 1114 1984 
C 826 1984 563 1997 371 2029 
L 371 1549 
C 563 1574 838 1594 1114 1594 
L 5267 1594 
C 5555 1594 5843 1574 6035 1549 
L 6035 2029 
C 5862 2003 5555 1984 5267 1984 
L 4435 1984 
L 4435 3603 
L 5120 3603 
C 5427 3603 5638 3590 5798 3571 
L 5798 4026 
C 5619 4000 5389 3987 5120 3987 
L 4435 3987 
L 4435 4576 
C 4435 4838 4448 5043 4480 5235 
L 3974 5235 
C 4000 5088 4013 4909 4013 4570 
L 4013 3987 
L 2336 3987 
L 2336 4582 
C 2336 4864 2349 5094 2374 5242 
L 1875 5242 
C 1907 5082 1920 4877 1920 4582 
L 1920 3987 
z
M 2336 1984 
L 2336 3603 
L 4013 3603 
L 4013 1984 
L 2336 1984 
z
M 3731 1030 
C 4109 762 4179 710 4691 320 
C 5158 -32 5293 -141 5594 -435 
L 5882 -96 
C 5344 358 4666 864 4000 1306 
L 3731 1030 
z
M 2240 1293 
C 1965 915 1792 736 1434 467 
C 1126 243 896 122 397 -83 
C 544 -211 602 -275 698 -435 
C 1082 -256 1299 -134 1574 64 
C 1978 358 2234 602 2605 1056 
L 2240 1293 
z
" transform="scale(0.015625)"/>
       <path id="01-bulk-write-HiraginoSansGB-W3-4eab" d="M 3437 4794 
C 3270 5056 3142 5222 2925 5453 
L 2560 5261 
C 2714 5101 2816 4979 2950 4794 
L 1075 4794 
C 800 4794 621 4806 435 4832 
L 435 4410 
C 608 4429 826 4442 1069 4442 
L 5376 4442 
C 5626 4442 5830 4429 6010 4410 
L 6010 4832 
C 5824 4806 5651 4794 5376 4794 
L 3437 4794 
z
M 3021 1242 
L 966 1242 
C 666 1242 493 1254 333 1280 
L 333 858 
C 518 883 704 896 973 896 
L 3021 896 
L 3021 90 
C 3021 -45 2957 -90 2739 -90 
C 2470 -90 2195 -64 1850 -13 
C 1907 -154 1933 -256 1952 -410 
C 2246 -435 2515 -448 2790 -448 
C 3277 -448 3430 -326 3430 45 
L 3430 896 
L 5440 896 
C 5709 896 5894 883 6074 858 
L 6074 1280 
C 5907 1254 5734 1242 5440 1242 
L 3430 1242 
L 3430 1370 
C 4198 1574 4691 1786 5261 2150 
L 5062 2381 
C 4966 2362 4800 2355 4525 2355 
L 1600 2355 
C 1363 2355 1184 2362 992 2387 
L 992 1984 
C 1165 2010 1331 2016 1587 2016 
L 3731 2016 
C 4000 2016 4198 2022 4493 2042 
C 4090 1850 3795 1747 3283 1613 
L 3002 1613 
C 3014 1542 3021 1440 3021 1267 
L 3021 1242 
z
M 1229 3142 
C 1229 3027 1222 2867 1210 2752 
C 1370 2765 1530 2771 1741 2771 
L 4614 2771 
C 4858 2771 4998 2765 5171 2752 
C 5158 2854 5152 2957 5152 3117 
L 5152 3686 
C 5152 3840 5158 3942 5171 4051 
C 5030 4038 4928 4032 4608 4032 
L 1760 4032 
C 1542 4032 1382 4038 1210 4051 
C 1229 3904 1229 3853 1229 3674 
L 1229 3142 
z
M 1619 3706 
L 4762 3706 
L 4762 3098 
L 1619 3098 
L 1619 3706 
z
" transform="scale(0.015625)"/>
      </defs>
      <use xlink:href="#01-bulk-write-HiraginoSansGB-W3-54"/>
      <use xlink:href="#01-bulk-write-HiraginoSansGB-W3-50" x="64.899994"/>
      <use xlink:href="#01-bulk-write-HiraginoSansGB-W3-32" x="131.099991"/>
      <use xlink:href="#01-bulk-write-HiraginoSansGB-W3-20" x="196.799988"/>
      <use xlink:href="#01-bulk-write-HiraginoSansGB-W3-5171" x="230.099976"/>
      <use xlink:href="#01-bulk-write-HiraginoSansGB-W3-4eab" x="330.09996"/>
      <use xlink:href="#01-bulk-write-HiraginoSansGB-W3-6c60" x="430.099945"/>
     </g>
    </g>
    <g id="01-bulk-write-patch_14">
     <path d="M 343.562021 374.380598 
L 365.562021 374.380598 
L 365.562021 366.680598 
L 343.562021 366.680598 
z
" style="fill: url(#01-bulk-write-h8d297617cc); stroke: #008477; stroke-width: 0.5; stroke-linejoin: miter"/>
    </g>
    <g id="01-bulk-write-text_23">
     <!-- TP4 独立池 -->
     <g style="fill: #192c46" transform="translate(374.362021 374.380598) scale(0.11 -0.11)">
      <use xlink:href="#01-bulk-write-HiraginoSansGB-W3-54"/>
      <use xlink:href="#01-bulk-write-HiraginoSansGB-W3-50" x="64.899994"/>
      <use xlink:href="#01-bulk-write-HiraginoSansGB-W3-34" x="131.099991"/>
      <use xlink:href="#01-bulk-write-HiraginoSansGB-W3-20" x="197.199982"/>
      <use xlink:href="#01-bulk-write-HiraginoSansGB-W3-72ec" x="230.499969"/>
      <use xlink:href="#01-bulk-write-HiraginoSansGB-W3-7acb" x="330.499954"/>
      <use xlink:href="#01-bulk-write-HiraginoSansGB-W3-6c60" x="430.499939"/>
     </g>
    </g>
    <g id="01-bulk-write-patch_15">
     <path d="M 343.562021 390.550598 
L 365.562021 390.550598 
L 365.562021 382.850598 
L 343.562021 382.850598 
z
" style="fill: #008477; stroke: #008477; stroke-width: 0.5; stroke-linejoin: miter"/>
    </g>
    <g id="01-bulk-write-text_24">
     <!-- TP4 共享池 -->
     <g style="fill: #192c46" transform="translate(374.362021 390.550598) scale(0.11 -0.11)">
      <use xlink:href="#01-bulk-write-HiraginoSansGB-W3-54"/>
      <use xlink:href="#01-bulk-write-HiraginoSansGB-W3-50" x="64.899994"/>
      <use xlink:href="#01-bulk-write-HiraginoSansGB-W3-34" x="131.099991"/>
      <use xlink:href="#01-bulk-write-HiraginoSansGB-W3-20" x="197.199982"/>
      <use xlink:href="#01-bulk-write-HiraginoSansGB-W3-5171" x="230.499969"/>
      <use xlink:href="#01-bulk-write-HiraginoSansGB-W3-4eab" x="330.499954"/>
      <use xlink:href="#01-bulk-write-HiraginoSansGB-W3-6c60" x="430.499939"/>
     </g>
    </g>
   </g>
  </g>
  <g id="01-bulk-write-axes_2">
   <g id="01-bulk-write-patch_16">
    <path d="M 668.33501 328.078684 
L 1037.99952 328.078684 
L 1037.99952 48.312668 
L 668.33501 48.312668 
z
" style="fill: #ffffff"/>
   </g>
   <g id="01-bulk-write-matplotlib.axis_3">
    <g id="01-bulk-write-xtick_9">
     <g id="01-bulk-write-line2d_19">
      <path d="M 668.33501 328.078684 
L 668.33501 48.312668 
" clip-path="url(#01-bulk-write-p009e731841)" style="fill: none; stroke: #e9edf2; stroke-width: 0.8; stroke-linecap: square"/>
     </g>
     <g id="01-bulk-write-line2d_20"/>
     <g id="01-bulk-write-text_25">
      <!-- 0.000 -->
      <g style="fill: #405570" transform="translate(651.12626 345.269934) scale(0.12 -0.12)">
       <use xlink:href="#01-bulk-write-HiraginoSansGB-W3-30"/>
       <use xlink:href="#01-bulk-write-HiraginoSansGB-W3-2e" x="65.699997"/>
       <use xlink:href="#01-bulk-write-HiraginoSansGB-W3-30" x="89.699982"/>
       <use xlink:href="#01-bulk-write-HiraginoSansGB-W3-30" x="155.399979"/>
       <use xlink:href="#01-bulk-write-HiraginoSansGB-W3-30" x="221.099976"/>
      </g>
     </g>
    </g>
    <g id="01-bulk-write-xtick_10">
     <g id="01-bulk-write-line2d_21">
      <path d="M 718.289674 328.078684 
L 718.289674 48.312668 
" clip-path="url(#01-bulk-write-p009e731841)" style="fill: none; stroke: #e9edf2; stroke-width: 0.8; stroke-linecap: square"/>
     </g>
     <g id="01-bulk-write-line2d_22"/>
     <g id="01-bulk-write-text_26">
      <!-- 0.025 -->
      <g style="fill: #405570" transform="translate(701.080924 345.269934) scale(0.12 -0.12)">
       <use xlink:href="#01-bulk-write-HiraginoSansGB-W3-30"/>
       <use xlink:href="#01-bulk-write-HiraginoSansGB-W3-2e" x="65.699997"/>
       <use xlink:href="#01-bulk-write-HiraginoSansGB-W3-30" x="89.699982"/>
       <use xlink:href="#01-bulk-write-HiraginoSansGB-W3-32" x="155.399979"/>
       <use xlink:href="#01-bulk-write-HiraginoSansGB-W3-35" x="221.099976"/>
      </g>
     </g>
    </g>
    <g id="01-bulk-write-xtick_11">
     <g id="01-bulk-write-line2d_23">
      <path d="M 768.244337 328.078684 
L 768.244337 48.312668 
" clip-path="url(#01-bulk-write-p009e731841)" style="fill: none; stroke: #e9edf2; stroke-width: 0.8; stroke-linecap: square"/>
     </g>
     <g id="01-bulk-write-line2d_24"/>
     <g id="01-bulk-write-text_27">
      <!-- 0.050 -->
      <g style="fill: #405570" transform="translate(751.035587 345.269934) scale(0.12 -0.12)">
       <use xlink:href="#01-bulk-write-HiraginoSansGB-W3-30"/>
       <use xlink:href="#01-bulk-write-HiraginoSansGB-W3-2e" x="65.699997"/>
       <use xlink:href="#01-bulk-write-HiraginoSansGB-W3-30" x="89.699982"/>
       <use xlink:href="#01-bulk-write-HiraginoSansGB-W3-35" x="155.399979"/>
       <use xlink:href="#01-bulk-write-HiraginoSansGB-W3-30" x="221.099976"/>
      </g>
     </g>
    </g>
    <g id="01-bulk-write-xtick_12">
     <g id="01-bulk-write-line2d_25">
      <path d="M 818.199001 328.078684 
L 818.199001 48.312668 
" clip-path="url(#01-bulk-write-p009e731841)" style="fill: none; stroke: #e9edf2; stroke-width: 0.8; stroke-linecap: square"/>
     </g>
     <g id="01-bulk-write-line2d_26"/>
     <g id="01-bulk-write-text_28">
      <!-- 0.075 -->
      <g style="fill: #405570" transform="translate(800.990251 345.269934) scale(0.12 -0.12)">
       <use xlink:href="#01-bulk-write-HiraginoSansGB-W3-30"/>
       <use xlink:href="#01-bulk-write-HiraginoSansGB-W3-2e" x="65.699997"/>
       <use xlink:href="#01-bulk-write-HiraginoSansGB-W3-30" x="89.699982"/>
       <use xlink:href="#01-bulk-write-HiraginoSansGB-W3-37" x="155.399979"/>
       <use xlink:href="#01-bulk-write-HiraginoSansGB-W3-35" x="221.099976"/>
      </g>
     </g>
    </g>
    <g id="01-bulk-write-xtick_13">
     <g id="01-bulk-write-line2d_27">
      <path d="M 868.153664 328.078684 
L 868.153664 48.312668 
" clip-path="url(#01-bulk-write-p009e731841)" style="fill: none; stroke: #e9edf2; stroke-width: 0.8; stroke-linecap: square"/>
     </g>
     <g id="01-bulk-write-line2d_28"/>
     <g id="01-bulk-write-text_29">
      <!-- 0.100 -->
      <g style="fill: #405570" transform="translate(850.944914 345.269934) scale(0.12 -0.12)">
       <use xlink:href="#01-bulk-write-HiraginoSansGB-W3-30"/>
       <use xlink:href="#01-bulk-write-HiraginoSansGB-W3-2e" x="65.699997"/>
       <use xlink:href="#01-bulk-write-HiraginoSansGB-W3-31" x="89.699982"/>
       <use xlink:href="#01-bulk-write-HiraginoSansGB-W3-30" x="155.399979"/>
       <use xlink:href="#01-bulk-write-HiraginoSansGB-W3-30" x="221.099976"/>
      </g>
     </g>
    </g>
    <g id="01-bulk-write-xtick_14">
     <g id="01-bulk-write-line2d_29">
      <path d="M 918.108328 328.078684 
L 918.108328 48.312668 
" clip-path="url(#01-bulk-write-p009e731841)" style="fill: none; stroke: #e9edf2; stroke-width: 0.8; stroke-linecap: square"/>
     </g>
     <g id="01-bulk-write-line2d_30"/>
     <g id="01-bulk-write-text_30">
      <!-- 0.125 -->
      <g style="fill: #405570" transform="translate(900.899578 345.269934) scale(0.12 -0.12)">
       <use xlink:href="#01-bulk-write-HiraginoSansGB-W3-30"/>
       <use xlink:href="#01-bulk-write-HiraginoSansGB-W3-2e" x="65.699997"/>
       <use xlink:href="#01-bulk-write-HiraginoSansGB-W3-31" x="89.699982"/>
       <use xlink:href="#01-bulk-write-HiraginoSansGB-W3-32" x="155.399979"/>
       <use xlink:href="#01-bulk-write-HiraginoSansGB-W3-35" x="221.099976"/>
      </g>
     </g>
    </g>
    <g id="01-bulk-write-xtick_15">
     <g id="01-bulk-write-line2d_31">
      <path d="M 968.062991 328.078684 
L 968.062991 48.312668 
" clip-path="url(#01-bulk-write-p009e731841)" style="fill: none; stroke: #e9edf2; stroke-width: 0.8; stroke-linecap: square"/>
     </g>
     <g id="01-bulk-write-line2d_32"/>
     <g id="01-bulk-write-text_31">
      <!-- 0.150 -->
      <g style="fill: #405570" transform="translate(950.854241 345.269934) scale(0.12 -0.12)">
       <use xlink:href="#01-bulk-write-HiraginoSansGB-W3-30"/>
       <use xlink:href="#01-bulk-write-HiraginoSansGB-W3-2e" x="65.699997"/>
       <use xlink:href="#01-bulk-write-HiraginoSansGB-W3-31" x="89.699982"/>
       <use xlink:href="#01-bulk-write-HiraginoSansGB-W3-35" x="155.399979"/>
       <use xlink:href="#01-bulk-write-HiraginoSansGB-W3-30" x="221.099976"/>
      </g>
     </g>
    </g>
    <g id="01-bulk-write-xtick_16">
     <g id="01-bulk-write-line2d_33">
      <path d="M 1018.017655 328.078684 
L 1018.017655 48.312668 
" clip-path="url(#01-bulk-write-p009e731841)" style="fill: none; stroke: #e9edf2; stroke-width: 0.8; stroke-linecap: square"/>
     </g>
     <g id="01-bulk-write-line2d_34"/>
     <g id="01-bulk-write-text_32">
      <!-- 0.175 -->
      <g style="fill: #405570" transform="translate(1000.808905 345.269934) scale(0.12 -0.12)">
       <use xlink:href="#01-bulk-write-HiraginoSansGB-W3-30"/>
       <use xlink:href="#01-bulk-write-HiraginoSansGB-W3-2e" x="65.699997"/>
       <use xlink:href="#01-bulk-write-HiraginoSansGB-W3-31" x="89.699982"/>
       <use xlink:href="#01-bulk-write-HiraginoSansGB-W3-37" x="155.399979"/>
       <use xlink:href="#01-bulk-write-HiraginoSansGB-W3-35" x="221.099976"/>
      </g>
     </g>
    </g>
    <g id="01-bulk-write-text_33">
     <!-- 组 P50（ms） · 越低越好 -->
     <g style="fill: #405570" transform="translate(778.509452 360.909934) scale(0.12 -0.12)">
      <use xlink:href="#01-bulk-write-HiraginoSansGB-W3-7ec4"/>
      <use xlink:href="#01-bulk-write-HiraginoSansGB-W3-20" x="99.999985"/>
      <use xlink:href="#01-bulk-write-HiraginoSansGB-W3-50" x="133.299973"/>
      <use xlink:href="#01-bulk-write-HiraginoSansGB-W3-35" x="199.499969"/>
      <use xlink:href="#01-bulk-write-HiraginoSansGB-W3-30" x="265.199966"/>
      <use xlink:href="#01-bulk-write-HiraginoSansGB-W3-ff08" x="330.899963"/>
      <use xlink:href="#01-bulk-write-HiraginoSansGB-W3-6d" x="430.899948"/>
      <use xlink:href="#01-bulk-write-HiraginoSansGB-W3-73" x="523.999939"/>
      <use xlink:href="#01-bulk-write-HiraginoSansGB-W3-ff09" x="577.699936"/>
      <use xlink:href="#01-bulk-write-HiraginoSansGB-W3-20" x="677.699921"/>
      <use xlink:href="#01-bulk-write-HiraginoSansGB-W3-b7" x="710.999908"/>
      <use xlink:href="#01-bulk-write-HiraginoSansGB-W3-20" x="810.999893"/>
      <use xlink:href="#01-bulk-write-HiraginoSansGB-W3-8d8a" x="844.299881"/>
      <use xlink:href="#01-bulk-write-HiraginoSansGB-W3-4f4e" x="944.299866"/>
      <use xlink:href="#01-bulk-write-HiraginoSansGB-W3-8d8a" x="1044.29985"/>
      <use xlink:href="#01-bulk-write-HiraginoSansGB-W3-597d" x="1144.299835"/>
     </g>
    </g>
   </g>
   <g id="01-bulk-write-matplotlib.axis_4">
    <g id="01-bulk-write-ytick_3">
     <g id="01-bulk-write-line2d_35"/>
     <g id="01-bulk-write-text_34">
      <!-- 2049 行 / token 分片 -->
      <g style="fill: #243a56" transform="translate(537.50376 142.261881) scale(0.12 -0.12)">
       <use xlink:href="#01-bulk-write-HiraginoSansGB-W3-32"/>
       <use xlink:href="#01-bulk-write-HiraginoSansGB-W3-30" x="65.699997"/>
       <use xlink:href="#01-bulk-write-HiraginoSansGB-W3-34" x="131.399994"/>
       <use xlink:href="#01-bulk-write-HiraginoSansGB-W3-39" x="197.499985"/>
       <use xlink:href="#01-bulk-write-HiraginoSansGB-W3-20" x="263.199982"/>
       <use xlink:href="#01-bulk-write-HiraginoSansGB-W3-884c" x="296.499969"/>
       <use xlink:href="#01-bulk-write-HiraginoSansGB-W3-20" x="396.499954"/>
       <use xlink:href="#01-bulk-write-HiraginoSansGB-W3-2f" x="429.799942"/>
       <use xlink:href="#01-bulk-write-HiraginoSansGB-W3-20" x="477.199936"/>
       <use xlink:href="#01-bulk-write-HiraginoSansGB-W3-74" x="510.499924"/>
       <use xlink:href="#01-bulk-write-HiraginoSansGB-W3-6f" x="549.699921"/>
       <use xlink:href="#01-bulk-write-HiraginoSansGB-W3-6b" x="612.199905"/>
       <use xlink:href="#01-bulk-write-HiraginoSansGB-W3-65" x="669.69989"/>
       <use xlink:href="#01-bulk-write-HiraginoSansGB-W3-6e" x="728.499878"/>
       <use xlink:href="#01-bulk-write-HiraginoSansGB-W3-20" x="790.299866"/>
       <use xlink:href="#01-bulk-write-HiraginoSansGB-W3-5206" x="823.599854"/>
       <use xlink:href="#01-bulk-write-HiraginoSansGB-W3-7247" x="923.599838"/>
      </g>
     </g>
    </g>
    <g id="01-bulk-write-ytick_4">
     <g id="01-bulk-write-line2d_36"/>
     <g id="01-bulk-write-text_35">
      <!-- 2049 行 / feature 分片 -->
      <g style="fill: #243a56" transform="translate(527.78376 261.31125) scale(0.12 -0.12)">
       <use xlink:href="#01-bulk-write-HiraginoSansGB-W3-32"/>
       <use xlink:href="#01-bulk-write-HiraginoSansGB-W3-30" x="65.699997"/>
       <use xlink:href="#01-bulk-write-HiraginoSansGB-W3-34" x="131.399994"/>
       <use xlink:href="#01-bulk-write-HiraginoSansGB-W3-39" x="197.499985"/>
       <use xlink:href="#01-bulk-write-HiraginoSansGB-W3-20" x="263.199982"/>
       <use xlink:href="#01-bulk-write-HiraginoSansGB-W3-884c" x="296.499969"/>
       <use xlink:href="#01-bulk-write-HiraginoSansGB-W3-20" x="396.499954"/>
       <use xlink:href="#01-bulk-write-HiraginoSansGB-W3-2f" x="429.799942"/>
       <use xlink:href="#01-bulk-write-HiraginoSansGB-W3-20" x="477.199936"/>
       <use xlink:href="#01-bulk-write-HiraginoSansGB-W3-66" x="510.499924"/>
       <use xlink:href="#01-bulk-write-HiraginoSansGB-W3-65" x="552.199921"/>
       <use xlink:href="#01-bulk-write-HiraginoSansGB-W3-61" x="610.999908"/>
       <use xlink:href="#01-bulk-write-HiraginoSansGB-W3-74" x="668.499893"/>
       <use xlink:href="#01-bulk-write-HiraginoSansGB-W3-75" x="707.69989"/>
       <use xlink:href="#01-bulk-write-HiraginoSansGB-W3-72" x="769.499878"/>
       <use xlink:href="#01-bulk-write-HiraginoSansGB-W3-65" x="812.499863"/>
       <use xlink:href="#01-bulk-write-HiraginoSansGB-W3-20" x="871.29985"/>
       <use xlink:href="#01-bulk-write-HiraginoSansGB-W3-5206" x="904.599838"/>
       <use xlink:href="#01-bulk-write-HiraginoSansGB-W3-7247" x="1004.599823"/>
      </g>
     </g>
    </g>
   </g>
   <g id="01-bulk-write-patch_17">
    <path d="M 668.33501 89.682323 
L 824.793016 89.682323 
L 824.793016 110.515963 
L 668.33501 110.515963 
z
" clip-path="url(#01-bulk-write-p009e731841)" style="fill: url(#01-bulk-write-h37cf2cdc2a); stroke: #467bb5; stroke-width: 0.5; stroke-linejoin: miter"/>
   </g>
   <g id="01-bulk-write-patch_18">
    <path d="M 668.33501 208.731692 
L 824.992835 208.731692 
L 824.992835 229.565331 
L 668.33501 229.565331 
z
" clip-path="url(#01-bulk-write-p009e731841)" style="fill: url(#01-bulk-write-h37cf2cdc2a); stroke: #467bb5; stroke-width: 0.5; stroke-linejoin: miter"/>
   </g>
   <g id="01-bulk-write-patch_19">
    <path d="M 668.33501 114.682691 
L 759.052679 114.682691 
L 759.052679 135.51633 
L 668.33501 135.51633 
z
" clip-path="url(#01-bulk-write-p009e731841)" style="fill: #467bb5; stroke: #467bb5; stroke-width: 0.5; stroke-linejoin: miter"/>
   </g>
   <g id="01-bulk-write-patch_20">
    <path d="M 668.33501 233.732059 
L 766.046332 233.732059 
L 766.046332 254.565699 
L 668.33501 254.565699 
z
" clip-path="url(#01-bulk-write-p009e731841)" style="fill: #467bb5; stroke: #467bb5; stroke-width: 0.5; stroke-linejoin: miter"/>
   </g>
   <g id="01-bulk-write-patch_21">
    <path d="M 668.33501 139.683058 
L 948.081126 139.683058 
L 948.081126 160.516697 
L 668.33501 160.516697 
z
" clip-path="url(#01-bulk-write-p009e731841)" style="fill: url(#01-bulk-write-h8d297617cc); stroke: #008477; stroke-width: 0.5; stroke-linejoin: miter"/>
   </g>
   <g id="01-bulk-write-patch_22">
    <path d="M 668.33501 258.732427 
L 949.679675 258.732427 
L 949.679675 279.566066 
L 668.33501 279.566066 
z
" clip-path="url(#01-bulk-write-p009e731841)" style="fill: url(#01-bulk-write-h8d297617cc); stroke: #008477; stroke-width: 0.5; stroke-linejoin: miter"/>
   </g>
   <g id="01-bulk-write-patch_23">
    <path d="M 668.33501 164.683425 
L 756.654855 164.683425 
L 756.654855 185.517065 
L 668.33501 185.517065 
z
" clip-path="url(#01-bulk-write-p009e731841)" style="fill: #008477; stroke: #008477; stroke-width: 0.5; stroke-linejoin: miter"/>
   </g>
   <g id="01-bulk-write-patch_24">
    <path d="M 668.33501 283.732794 
L 777.236176 283.732794 
L 777.236176 304.566433 
L 668.33501 304.566433 
z
" clip-path="url(#01-bulk-write-p009e731841)" style="fill: #008477; stroke: #008477; stroke-width: 0.5; stroke-linejoin: miter"/>
   </g>
   <g id="01-bulk-write-patch_25">
    <path d="M 668.33501 328.078684 
L 1037.99952 328.078684 
" style="fill: none; stroke: #d7e0eb; stroke-width: 0.8; stroke-linejoin: miter; stroke-linecap: square"/>
   </g>
   <g id="01-bulk-write-text_36">
    <!-- 0.0783 -->
    <g style="fill: #405570" transform="translate(829.22899 102.908518) scale(0.1 -0.1)">
     <use xlink:href="#01-bulk-write-HiraginoSansGB-W3-30"/>
     <use xlink:href="#01-bulk-write-HiraginoSansGB-W3-2e" x="65.699997"/>
     <use xlink:href="#01-bulk-write-HiraginoSansGB-W3-30" x="89.699982"/>
     <use xlink:href="#01-bulk-write-HiraginoSansGB-W3-37" x="155.399979"/>
     <use xlink:href="#01-bulk-write-HiraginoSansGB-W3-38" x="221.099976"/>
     <use xlink:href="#01-bulk-write-HiraginoSansGB-W3-33" x="286.799973"/>
    </g>
   </g>
   <g id="01-bulk-write-text_37">
    <!-- 0.0784 -->
    <g style="fill: #405570" transform="translate(829.428809 221.957886) scale(0.1 -0.1)">
     <use xlink:href="#01-bulk-write-HiraginoSansGB-W3-30"/>
     <use xlink:href="#01-bulk-write-HiraginoSansGB-W3-2e" x="65.699997"/>
     <use xlink:href="#01-bulk-write-HiraginoSansGB-W3-30" x="89.699982"/>
     <use xlink:href="#01-bulk-write-HiraginoSansGB-W3-37" x="155.399979"/>
     <use xlink:href="#01-bulk-write-HiraginoSansGB-W3-38" x="221.099976"/>
     <use xlink:href="#01-bulk-write-HiraginoSansGB-W3-34" x="286.799973"/>
    </g>
   </g>
   <g id="01-bulk-write-text_38">
    <!-- 0.0454 -->
    <g style="fill: #467bb5" transform="translate(763.488653 127.908885) scale(0.1 -0.1)">
     <use xlink:href="#01-bulk-write-HiraginoSansGB-W3-30"/>
     <use xlink:href="#01-bulk-write-HiraginoSansGB-W3-2e" x="65.699997"/>
     <use xlink:href="#01-bulk-write-HiraginoSansGB-W3-30" x="89.699982"/>
     <use xlink:href="#01-bulk-write-HiraginoSansGB-W3-34" x="155.399979"/>
     <use xlink:href="#01-bulk-write-HiraginoSansGB-W3-35" x="221.499969"/>
     <use xlink:href="#01-bulk-write-HiraginoSansGB-W3-34" x="287.199966"/>
    </g>
   </g>
   <g id="01-bulk-write-text_39">
    <!-- 0.0489 -->
    <g style="fill: #467bb5" transform="translate(770.482306 246.958254) scale(0.1 -0.1)">
     <use xlink:href="#01-bulk-write-HiraginoSansGB-W3-30"/>
     <use xlink:href="#01-bulk-write-HiraginoSansGB-W3-2e" x="65.699997"/>
     <use xlink:href="#01-bulk-write-HiraginoSansGB-W3-30" x="89.699982"/>
     <use xlink:href="#01-bulk-write-HiraginoSansGB-W3-34" x="155.399979"/>
     <use xlink:href="#01-bulk-write-HiraginoSansGB-W3-38" x="221.499969"/>
     <use xlink:href="#01-bulk-write-HiraginoSansGB-W3-39" x="287.199966"/>
    </g>
   </g>
   <g id="01-bulk-write-text_40">
    <!-- 0.1400 -->
    <g style="fill: #405570" transform="translate(952.5171 152.909253) scale(0.1 -0.1)">
     <use xlink:href="#01-bulk-write-HiraginoSansGB-W3-30"/>
     <use xlink:href="#01-bulk-write-HiraginoSansGB-W3-2e" x="65.699997"/>
     <use xlink:href="#01-bulk-write-HiraginoSansGB-W3-31" x="89.699982"/>
     <use xlink:href="#01-bulk-write-HiraginoSansGB-W3-34" x="155.399979"/>
     <use xlink:href="#01-bulk-write-HiraginoSansGB-W3-30" x="221.499969"/>
     <use xlink:href="#01-bulk-write-HiraginoSansGB-W3-30" x="287.199966"/>
    </g>
   </g>
   <g id="01-bulk-write-text_41">
    <!-- 0.1408 -->
    <g style="fill: #405570" transform="translate(954.115649 271.958621) scale(0.1 -0.1)">
     <use xlink:href="#01-bulk-write-HiraginoSansGB-W3-30"/>
     <use xlink:href="#01-bulk-write-HiraginoSansGB-W3-2e" x="65.699997"/>
     <use xlink:href="#01-bulk-write-HiraginoSansGB-W3-31" x="89.699982"/>
     <use xlink:href="#01-bulk-write-HiraginoSansGB-W3-34" x="155.399979"/>
     <use xlink:href="#01-bulk-write-HiraginoSansGB-W3-30" x="221.499969"/>
     <use xlink:href="#01-bulk-write-HiraginoSansGB-W3-38" x="287.199966"/>
    </g>
   </g>
   <g id="01-bulk-write-text_42">
    <!-- 0.0442 -->
    <g style="fill: #008477" transform="translate(761.090829 177.90962) scale(0.1 -0.1)">
     <use xlink:href="#01-bulk-write-HiraginoSansGB-W3-30"/>
     <use xlink:href="#01-bulk-write-HiraginoSansGB-W3-2e" x="65.699997"/>
     <use xlink:href="#01-bulk-write-HiraginoSansGB-W3-30" x="89.699982"/>
     <use xlink:href="#01-bulk-write-HiraginoSansGB-W3-34" x="155.399979"/>
     <use xlink:href="#01-bulk-write-HiraginoSansGB-W3-34" x="221.499969"/>
     <use xlink:href="#01-bulk-write-HiraginoSansGB-W3-32" x="287.59996"/>
    </g>
   </g>
   <g id="01-bulk-write-text_43">
    <!-- 0.0545 -->
    <g style="fill: #008477" transform="translate(781.672151 296.958989) scale(0.1 -0.1)">
     <use xlink:href="#01-bulk-write-HiraginoSansGB-W3-30"/>
     <use xlink:href="#01-bulk-write-HiraginoSansGB-W3-2e" x="65.699997"/>
     <use xlink:href="#01-bulk-write-HiraginoSansGB-W3-30" x="89.699982"/>
     <use xlink:href="#01-bulk-write-HiraginoSansGB-W3-35" x="155.399979"/>
     <use xlink:href="#01-bulk-write-HiraginoSansGB-W3-34" x="221.099976"/>
     <use xlink:href="#01-bulk-write-HiraginoSansGB-W3-35" x="287.199966"/>
    </g>
   </g>
   <g id="01-bulk-write-text_44">
    <!-- D 侧 -->
    <g style="fill: #192c46" transform="translate(837.485234 42.312668) scale(0.15 -0.15)">
     <defs>
      <path id="01-bulk-write-HiraginoSansGB-W3-44" d="M 544 781 
C 544 461 531 269 493 19 
C 717 32 890 38 1197 38 
L 2278 38 
C 2918 38 3450 230 3834 608 
C 4275 1037 4518 1702 4518 2502 
C 4518 4032 3706 4902 2278 4902 
L 1197 4902 
C 890 4902 717 4909 493 4922 
C 531 4653 544 4467 544 4192 
L 544 781 
z
M 1043 486 
L 1043 4454 
L 2291 4454 
C 2816 4454 3213 4314 3494 4019 
C 3834 3680 4000 3168 4000 2502 
C 4000 1843 3814 1299 3462 941 
C 3174 634 2771 486 2266 486 
L 1043 486 
z
" transform="scale(0.015625)"/>
     </defs>
     <use xlink:href="#01-bulk-write-HiraginoSansGB-W3-44"/>
     <use xlink:href="#01-bulk-write-HiraginoSansGB-W3-20" x="75.799988"/>
     <use xlink:href="#01-bulk-write-HiraginoSansGB-W3-4fa7" x="109.099976"/>
    </g>
   </g>
   <g id="01-bulk-write-legend_2">
    <g id="01-bulk-write-patch_26">
     <path d="M 753.033749 374.380598 
L 775.033749 374.380598 
L 775.033749 366.680598 
L 753.033749 366.680598 
z
" style="fill: url(#01-bulk-write-h37cf2cdc2a); stroke: #467bb5; stroke-width: 0.5; stroke-linejoin: miter"/>
    </g>
    <g id="01-bulk-write-text_45">
     <!-- TP2 独立池 -->
     <g style="fill: #192c46" transform="translate(783.833749 374.380598) scale(0.11 -0.11)">
      <use xlink:href="#01-bulk-write-HiraginoSansGB-W3-54"/>
      <use xlink:href="#01-bulk-write-HiraginoSansGB-W3-50" x="64.899994"/>
      <use xlink:href="#01-bulk-write-HiraginoSansGB-W3-32" x="131.099991"/>
      <use xlink:href="#01-bulk-write-HiraginoSansGB-W3-20" x="196.799988"/>
      <use xlink:href="#01-bulk-write-HiraginoSansGB-W3-72ec" x="230.099976"/>
      <use xlink:href="#01-bulk-write-HiraginoSansGB-W3-7acb" x="330.09996"/>
      <use xlink:href="#01-bulk-write-HiraginoSansGB-W3-6c60" x="430.099945"/>
     </g>
    </g>
    <g id="01-bulk-write-patch_27">
     <path d="M 753.033749 390.550598 
L 775.033749 390.550598 
L 775.033749 382.850598 
L 753.033749 382.850598 
z
" style="fill: #467bb5; stroke: #467bb5; stroke-width: 0.5; stroke-linejoin: miter"/>
    </g>
    <g id="01-bulk-write-text_46">
     <!-- TP2 共享池 -->
     <g style="fill: #192c46" transform="translate(783.833749 390.550598) scale(0.11 -0.11)">
      <use xlink:href="#01-bulk-write-HiraginoSansGB-W3-54"/>
      <use xlink:href="#01-bulk-write-HiraginoSansGB-W3-50" x="64.899994"/>
      <use xlink:href="#01-bulk-write-HiraginoSansGB-W3-32" x="131.099991"/>
      <use xlink:href="#01-bulk-write-HiraginoSansGB-W3-20" x="196.799988"/>
      <use xlink:href="#01-bulk-write-HiraginoSansGB-W3-5171" x="230.099976"/>
      <use xlink:href="#01-bulk-write-HiraginoSansGB-W3-4eab" x="330.09996"/>
      <use xlink:href="#01-bulk-write-HiraginoSansGB-W3-6c60" x="430.099945"/>
     </g>
    </g>
    <g id="01-bulk-write-patch_28">
     <path d="M 864.145781 374.380598 
L 886.145781 374.380598 
L 886.145781 366.680598 
L 864.145781 366.680598 
z
" style="fill: url(#01-bulk-write-h8d297617cc); stroke: #008477; stroke-width: 0.5; stroke-linejoin: miter"/>
    </g>
    <g id="01-bulk-write-text_47">
     <!-- TP4 独立池 -->
     <g style="fill: #192c46" transform="translate(894.945781 374.380598) scale(0.11 -0.11)">
      <use xlink:href="#01-bulk-write-HiraginoSansGB-W3-54"/>
      <use xlink:href="#01-bulk-write-HiraginoSansGB-W3-50" x="64.899994"/>
      <use xlink:href="#01-bulk-write-HiraginoSansGB-W3-34" x="131.099991"/>
      <use xlink:href="#01-bulk-write-HiraginoSansGB-W3-20" x="197.199982"/>
      <use xlink:href="#01-bulk-write-HiraginoSansGB-W3-72ec" x="230.499969"/>
      <use xlink:href="#01-bulk-write-HiraginoSansGB-W3-7acb" x="330.499954"/>
      <use xlink:href="#01-bulk-write-HiraginoSansGB-W3-6c60" x="430.499939"/>
     </g>
    </g>
    <g id="01-bulk-write-patch_29">
     <path d="M 864.145781 390.550598 
L 886.145781 390.550598 
L 886.145781 382.850598 
L 864.145781 382.850598 
z
" style="fill: #008477; stroke: #008477; stroke-width: 0.5; stroke-linejoin: miter"/>
    </g>
    <g id="01-bulk-write-text_48">
     <!-- TP4 共享池 -->
     <g style="fill: #192c46" transform="translate(894.945781 390.550598) scale(0.11 -0.11)">
      <use xlink:href="#01-bulk-write-HiraginoSansGB-W3-54"/>
      <use xlink:href="#01-bulk-write-HiraginoSansGB-W3-50" x="64.899994"/>
      <use xlink:href="#01-bulk-write-HiraginoSansGB-W3-34" x="131.099991"/>
      <use xlink:href="#01-bulk-write-HiraginoSansGB-W3-20" x="197.199982"/>
      <use xlink:href="#01-bulk-write-HiraginoSansGB-W3-5171" x="230.499969"/>
      <use xlink:href="#01-bulk-write-HiraginoSansGB-W3-4eab" x="330.499954"/>
      <use xlink:href="#01-bulk-write-HiraginoSansGB-W3-6c60" x="430.499939"/>
     </g>
    </g>
   </g>
  </g>
  <g id="01-bulk-write-text_49">
   <!-- C · 大块写入：每 TP 独占一池，与多个 TP 分片共享一池 -->
   <g style="fill: #192c46" transform="translate(285.216322 21.15) scale(0.18 -0.18)">
    <defs>
     <path id="01-bulk-write-HiraginoSansGB-W3-43" d="M 4461 3667 
C 4307 4064 4186 4256 3962 4474 
C 3616 4819 3130 4992 2528 4992 
C 1920 4992 1434 4806 1062 4448 
C 589 3981 333 3283 333 2458 
C 333 1658 576 960 1024 512 
C 1389 141 1907 -51 2522 -51 
C 3238 -51 3821 211 4179 691 
C 4333 896 4416 1075 4525 1453 
L 3994 1581 
C 3936 1286 3872 1126 3738 941 
C 3475 595 3046 403 2541 403 
C 1510 403 851 1210 851 2477 
C 851 3750 1504 4544 2541 4544 
C 3008 4544 3392 4384 3648 4083 
C 3782 3930 3853 3795 3949 3520 
L 4461 3667 
z
" transform="scale(0.015625)"/>
     <path id="01-bulk-write-HiraginoSansGB-W3-5927" d="M 5274 3347 
C 5594 3347 5773 3334 5978 3302 
L 5978 3802 
C 5805 3770 5594 3757 5267 3757 
L 3328 3757 
C 3347 4038 3354 4301 3360 4544 
C 3360 4851 3373 4998 3405 5210 
L 2880 5210 
C 2912 5037 2925 4813 2925 4506 
C 2925 4275 2918 4064 2906 3872 
C 2899 3802 2899 3802 2899 3757 
L 1133 3757 
C 794 3757 582 3770 416 3802 
L 416 3302 
C 602 3334 781 3347 1120 3347 
L 2861 3347 
C 2829 3110 2810 2989 2784 2848 
C 2643 2157 2374 1581 1971 1114 
C 1555 634 1088 320 288 -6 
C 422 -147 474 -218 563 -397 
C 851 -262 992 -192 1165 -96 
C 1594 154 1990 480 2285 826 
C 2733 1344 3072 2061 3194 2771 
C 3661 1299 4442 371 5882 -390 
C 5984 -186 6035 -115 6163 38 
C 5434 352 4896 742 4429 1286 
C 3942 1850 3578 2573 3386 3347 
L 5274 3347 
z
" transform="scale(0.015625)"/>
     <path id="01-bulk-write-HiraginoSansGB-W3-5757" d="M 2848 2458 
C 2560 2458 2342 2470 2182 2496 
L 2182 2054 
C 2349 2080 2586 2093 2854 2093 
L 3501 2093 
C 3328 1389 3027 870 2554 442 
C 2291 211 2099 90 1722 -83 
C 1869 -198 1939 -288 2010 -448 
C 2669 -64 3034 262 3354 755 
C 3578 1101 3699 1389 3846 1926 
C 4186 928 4832 173 5862 -442 
C 5939 -256 5997 -173 6125 -26 
C 5146 461 4506 1158 4160 2093 
L 5446 2093 
C 5696 2093 5920 2080 6086 2054 
L 6086 2496 
C 5914 2470 5754 2458 5523 2458 
L 5478 2458 
L 5478 3776 
C 5478 4032 5485 4198 5504 4333 
C 5370 4314 5229 4307 4947 4307 
L 4038 4307 
L 4038 4685 
C 4038 5005 4045 5133 4077 5318 
L 3603 5318 
C 3635 5126 3648 4960 3648 4685 
L 3648 4307 
L 3053 4307 
C 2771 4307 2605 4314 2426 4339 
L 2426 3910 
C 2598 3936 2771 3942 3046 3942 
L 3648 3942 
L 3648 3661 
C 3648 3187 3622 2790 3571 2458 
L 2848 2458 
z
M 3955 2458 
C 4006 2848 4038 3290 4038 3750 
L 4038 3942 
L 5082 3942 
L 5082 2458 
L 3955 2458 
z
M 1517 3629 
L 1517 4570 
C 1517 4896 1523 5075 1555 5280 
L 1088 5280 
C 1114 5088 1126 4890 1126 4576 
L 1126 3629 
L 826 3629 
C 634 3629 480 3642 314 3661 
L 314 3238 
C 480 3264 627 3270 819 3270 
L 1126 3270 
L 1126 1178 
C 762 1056 442 966 250 941 
L 365 499 
C 416 525 570 582 704 621 
C 1382 851 1632 947 2208 1184 
L 2195 1587 
C 1875 1446 1766 1402 1517 1312 
L 1517 3270 
L 1728 3270 
C 1894 3270 1984 3264 2195 3245 
L 2195 3654 
C 2054 3635 1939 3629 1715 3629 
L 1517 3629 
z
" transform="scale(0.015625)"/>
     <path id="01-bulk-write-HiraginoSansGB-W3-5199" d="M 4371 3379 
C 4698 3379 4877 3373 5062 3347 
L 5062 3770 
C 4883 3738 4736 3731 4371 3731 
L 2202 3731 
C 2202 3750 2202 3757 2208 3776 
C 2227 3885 2253 4000 2278 4128 
C 2291 4205 2304 4256 2310 4288 
C 2323 4339 2323 4352 2336 4403 
L 1888 4442 
C 1882 4320 1875 4250 1850 4077 
C 1722 3238 1581 2592 1402 2061 
C 1510 2074 1530 2074 1626 2080 
C 1715 2080 1715 2080 1971 2099 
L 4986 2099 
C 4858 813 4832 608 4762 339 
C 4685 51 4589 -26 4282 -26 
C 3974 -26 3616 13 3085 102 
C 3098 70 3110 45 3117 26 
C 3123 6 3130 -13 3136 -32 
C 3168 -122 3174 -128 3187 -256 
C 3187 -269 3194 -301 3200 -339 
C 3661 -390 3974 -416 4269 -416 
C 4627 -416 4838 -352 4973 -205 
C 5101 -58 5165 173 5242 736 
C 5370 1830 5446 2406 5459 2477 
C 5299 2451 5101 2445 4813 2445 
L 1914 2445 
C 1984 2707 1997 2758 2061 3066 
C 2074 3136 2093 3238 2125 3379 
L 4371 3379 
z
M 966 3718 
L 966 4634 
L 5446 4634 
L 5446 3718 
L 5882 3718 
C 5856 3872 5843 4038 5843 4243 
L 5843 4512 
C 5843 4742 5850 4883 5869 5018 
C 5728 5005 5562 4998 5261 4998 
L 1139 4998 
C 838 4998 672 5005 538 5018 
C 557 4883 563 4736 563 4512 
L 563 4250 
C 563 4045 550 3885 525 3718 
L 966 3718 
z
M 4326 1306 
C 4147 1274 3974 1267 3616 1267 
L 1184 1267 
C 845 1267 646 1280 480 1306 
L 480 851 
C 653 877 851 890 1184 890 
L 3616 890 
C 3910 890 4115 877 4326 851 
L 4326 1306 
z
" transform="scale(0.015625)"/>
     <path id="01-bulk-write-HiraginoSansGB-W3-5165" d="M 2035 4902 
C 2656 4525 2758 4435 3034 4083 
C 2886 3590 2797 3283 2758 3162 
C 2509 2432 2144 1715 1766 1229 
C 1363 704 1018 403 352 6 
C 525 -141 589 -211 672 -358 
C 1440 192 1786 531 2202 1114 
C 2624 1696 2989 2458 3219 3226 
C 3290 3456 3290 3456 3322 3565 
C 3392 3392 3443 3270 3520 3066 
C 3808 2272 3834 2202 3949 1952 
C 4122 1581 4326 1210 4506 947 
C 4858 442 5197 102 5805 -352 
C 5939 -160 5997 -96 6163 26 
C 5702 326 5485 499 5267 717 
C 4960 1030 4698 1408 4422 1939 
C 4224 2330 4147 2509 3853 3334 
C 3622 3981 3450 4288 3155 4576 
C 2880 4838 2624 5037 2317 5222 
L 2035 4902 
z
" transform="scale(0.015625)"/>
     <path id="01-bulk-write-HiraginoSansGB-W3-ff1a" d="M 1722 3488 
L 1088 3488 
L 1088 2854 
L 1722 2854 
L 1722 3488 
z
M 1722 38 
L 1722 672 
L 1088 672 
L 1088 38 
L 1722 38 
z
" transform="scale(0.015625)"/>
     <path id="01-bulk-write-HiraginoSansGB-W3-6bcf" d="M 5094 4333 
C 5440 4333 5651 4320 5901 4294 
L 5901 4749 
C 5677 4717 5466 4704 5094 4704 
L 1888 4704 
C 1914 4755 1933 4781 1958 4832 
C 2118 5152 2144 5197 2221 5325 
L 1773 5440 
C 1741 5318 1683 5184 1594 4992 
C 1267 4339 858 3853 275 3430 
C 397 3322 467 3238 557 3085 
C 1030 3488 1357 3853 1664 4333 
L 5094 4333 
z
M 2752 3104 
C 3098 2790 3142 2746 3424 2374 
L 3731 2624 
C 3462 2944 3386 3021 3046 3315 
L 2752 3104 
z
M 2662 1581 
C 2944 1338 3104 1146 3334 813 
L 3642 1075 
C 3398 1382 3213 1581 2957 1811 
L 2662 1581 
z
M 4966 717 
C 5024 1088 5082 1555 5114 1920 
L 5491 1920 
C 5766 1920 5920 1914 6086 1888 
L 6086 2323 
C 5939 2298 5779 2291 5491 2291 
L 5139 2291 
C 5165 2650 5184 2982 5203 3309 
C 5216 3571 5222 3622 5242 3770 
C 5088 3750 4934 3744 4717 3744 
L 1952 3744 
C 1779 3744 1645 3750 1536 3763 
C 1523 3763 1498 3770 1466 3770 
C 1478 3699 1478 3674 1478 3616 
C 1478 3328 1414 2701 1350 2291 
L 941 2291 
C 627 2291 467 2298 307 2330 
L 307 1882 
C 461 1914 614 1920 922 1920 
L 1280 1920 
C 1120 1158 1024 781 851 326 
C 941 333 941 333 1216 346 
L 4474 346 
C 4435 166 4429 147 4403 83 
C 4358 -19 4262 -64 4102 -64 
C 3910 -64 3718 -45 3366 13 
C 3418 -154 3437 -256 3443 -422 
C 3738 -454 3891 -461 4077 -461 
C 4410 -461 4550 -422 4666 -294 
C 4768 -186 4819 -38 4896 346 
L 5152 346 
C 5440 346 5581 339 5747 314 
L 5747 749 
C 5600 723 5453 717 5165 717 
L 4966 717 
z
M 1382 717 
C 1510 1146 1626 1600 1690 1920 
L 4698 1920 
C 4621 1152 4621 1152 4550 717 
L 1382 717 
z
M 1760 2291 
C 1805 2611 1818 2714 1894 3373 
L 4787 3379 
C 4762 2810 4755 2688 4730 2291 
L 1760 2291 
z
" transform="scale(0.015625)"/>
     <path id="01-bulk-write-HiraginoSansGB-W3-5360" d="M 3251 4166 
L 3251 4704 
C 3251 4966 3264 5120 3296 5318 
L 2778 5318 
C 2810 5126 2822 4960 2822 4704 
L 2822 2432 
L 1485 2432 
C 1248 2432 1094 2438 934 2458 
C 954 2259 966 2086 966 1888 
L 966 166 
C 966 -51 954 -256 928 -467 
L 1389 -467 
L 1389 -38 
L 4960 -38 
L 4960 -467 
L 5427 -467 
C 5402 -275 5389 -64 5389 166 
L 5389 1888 
C 5389 2099 5395 2266 5421 2458 
C 5254 2438 5107 2432 4864 2432 
L 3251 2432 
L 3251 3782 
L 5152 3782 
C 5414 3782 5613 3770 5798 3744 
L 5798 4205 
C 5619 4179 5434 4166 5146 4166 
L 3251 4166 
z
M 1389 2048 
L 4966 2048 
L 4966 352 
L 1389 352 
L 1389 2048 
z
" transform="scale(0.015625)"/>
     <path id="01-bulk-write-HiraginoSansGB-W3-4e00" d="M 6016 2771 
C 5824 2739 5549 2726 5171 2726 
L 1229 2726 
C 858 2726 582 2739 378 2771 
L 378 2240 
C 582 2272 877 2285 1229 2285 
L 5171 2285 
C 5568 2285 5786 2272 6016 2240 
L 6016 2771 
z
" transform="scale(0.015625)"/>
     <path id="01-bulk-write-HiraginoSansGB-W3-ff0c" d="M 1370 691 
L 730 691 
L 730 58 
L 1101 58 
C 1107 -326 1050 -435 794 -550 
L 966 -762 
C 1286 -576 1370 -416 1370 58 
L 1370 691 
z
" transform="scale(0.015625)"/>
     <path id="01-bulk-write-HiraginoSansGB-W3-4e0e" d="M 1837 4429 
C 1907 5210 1907 5210 1933 5363 
L 1466 5363 
C 1472 5280 1472 5242 1472 5190 
C 1472 5005 1453 4634 1427 4301 
C 1370 3590 1312 3187 1210 2682 
C 1402 2694 1734 2707 1798 2707 
L 4960 2707 
C 4922 1760 4858 1190 4730 666 
C 4614 179 4512 77 4128 77 
C 3821 77 3469 109 2880 198 
C 2963 -13 2989 -90 3021 -256 
C 3546 -307 3859 -326 4160 -326 
C 4762 -326 4954 -166 5126 499 
C 5203 794 5306 1453 5338 1875 
C 5395 2778 5408 2918 5434 3110 
C 5306 3091 5056 3078 4749 3078 
L 1715 3078 
C 1766 3520 1773 3578 1811 4045 
L 4858 4045 
C 5126 4045 5293 4038 5459 4013 
L 5459 4461 
C 5286 4435 5146 4429 4845 4429 
L 1837 4429 
z
M 4224 1766 
C 4026 1741 3814 1728 3552 1728 
L 998 1728 
C 717 1728 499 1741 314 1766 
L 314 1312 
C 518 1338 730 1350 998 1350 
L 3552 1350 
C 3808 1350 4026 1338 4224 1312 
L 4224 1766 
z
" transform="scale(0.015625)"/>
     <path id="01-bulk-write-HiraginoSansGB-W3-591a" d="M 4781 4896 
C 4627 4877 4448 4864 4186 4864 
L 2970 4864 
C 3059 4960 3098 5011 3174 5101 
C 3309 5261 3315 5274 3379 5344 
L 2918 5459 
C 2829 5280 2701 5114 2470 4877 
C 1939 4326 1331 3910 576 3578 
C 685 3475 749 3392 826 3258 
C 1568 3629 2086 3981 2656 4531 
L 4390 4531 
C 3981 4026 3738 3789 3174 3354 
C 2957 3526 2675 3718 2330 3930 
L 2029 3693 
C 2381 3488 2630 3309 2848 3117 
C 2131 2624 1389 2285 550 2061 
C 659 1926 730 1818 774 1683 
C 1517 1933 2042 2170 2618 2509 
C 3277 2906 3917 3418 4422 3968 
C 4557 4109 4602 4166 4998 4653 
L 4781 4896 
z
M 3962 2547 
C 4102 2688 4154 2746 4397 3021 
L 3981 3181 
C 3846 2944 3616 2694 3283 2413 
C 2765 1971 2125 1626 1274 1338 
C 1376 1235 1427 1165 1517 992 
C 2022 1190 2458 1402 2835 1632 
C 3117 1805 3245 1901 3610 2214 
L 5331 2214 
C 4915 1690 4563 1350 4102 1024 
C 3789 1280 3584 1421 3226 1645 
L 2899 1408 
C 3251 1210 3501 1030 3693 858 
C 3744 813 3744 813 3757 800 
C 2995 333 2022 -6 1037 -154 
C 1165 -326 1190 -371 1267 -557 
C 2477 -256 3136 -13 3866 410 
C 4621 845 5158 1325 5741 2093 
C 5850 2234 5850 2234 5926 2330 
L 5696 2579 
C 5542 2560 5363 2547 5094 2547 
L 3962 2547 
z
" transform="scale(0.015625)"/>
     <path id="01-bulk-write-HiraginoSansGB-W3-4e2a" d="M 3469 5338 
L 3021 5504 
C 2925 5165 2733 4826 2406 4429 
C 1869 3770 1190 3219 397 2797 
C 544 2643 582 2592 678 2419 
C 1792 3072 2637 3885 3162 4806 
C 3590 4173 3872 3846 4346 3424 
C 4781 3040 5190 2746 5766 2413 
C 5850 2579 5907 2656 6042 2810 
C 5523 3059 5094 3341 4595 3763 
C 4058 4218 3744 4582 3386 5184 
L 3469 5338 
z
M 3437 -422 
C 3405 -192 3392 6 3392 250 
L 3392 2989 
C 3392 3251 3405 3450 3437 3648 
L 2918 3648 
C 2950 3443 2963 3226 2963 2989 
L 2963 250 
C 2963 13 2950 -198 2918 -422 
L 3437 -422 
z
" transform="scale(0.015625)"/>
    </defs>
    <use xlink:href="#01-bulk-write-HiraginoSansGB-W3-43"/>
    <use xlink:href="#01-bulk-write-HiraginoSansGB-W3-20" x="75.599991"/>
    <use xlink:href="#01-bulk-write-HiraginoSansGB-W3-b7" x="108.899979"/>
    <use xlink:href="#01-bulk-write-HiraginoSansGB-W3-20" x="208.899963"/>
    <use xlink:href="#01-bulk-write-HiraginoSansGB-W3-5927" x="242.199951"/>
    <use xlink:href="#01-bulk-write-HiraginoSansGB-W3-5757" x="342.199936"/>
    <use xlink:href="#01-bulk-write-HiraginoSansGB-W3-5199" x="442.199921"/>
    <use xlink:href="#01-bulk-write-HiraginoSansGB-W3-5165" x="542.199905"/>
    <use xlink:href="#01-bulk-write-HiraginoSansGB-W3-ff1a" x="642.19989"/>
    <use xlink:href="#01-bulk-write-HiraginoSansGB-W3-6bcf" x="742.199875"/>
    <use xlink:href="#01-bulk-write-HiraginoSansGB-W3-20" x="842.19986"/>
    <use xlink:href="#01-bulk-write-HiraginoSansGB-W3-54" x="875.499847"/>
    <use xlink:href="#01-bulk-write-HiraginoSansGB-W3-50" x="940.399841"/>
    <use xlink:href="#01-bulk-write-HiraginoSansGB-W3-20" x="1006.599838"/>
    <use xlink:href="#01-bulk-write-HiraginoSansGB-W3-72ec" x="1039.899826"/>
    <use xlink:href="#01-bulk-write-HiraginoSansGB-W3-5360" x="1139.899811"/>
    <use xlink:href="#01-bulk-write-HiraginoSansGB-W3-4e00" x="1239.899796"/>
    <use xlink:href="#01-bulk-write-HiraginoSansGB-W3-6c60" x="1339.89978"/>
    <use xlink:href="#01-bulk-write-HiraginoSansGB-W3-ff0c" x="1439.899765"/>
    <use xlink:href="#01-bulk-write-HiraginoSansGB-W3-4e0e" x="1539.89975"/>
    <use xlink:href="#01-bulk-write-HiraginoSansGB-W3-591a" x="1639.899734"/>
    <use xlink:href="#01-bulk-write-HiraginoSansGB-W3-4e2a" x="1739.899719"/>
    <use xlink:href="#01-bulk-write-HiraginoSansGB-W3-20" x="1839.899704"/>
    <use xlink:href="#01-bulk-write-HiraginoSansGB-W3-54" x="1873.199692"/>
    <use xlink:href="#01-bulk-write-HiraginoSansGB-W3-50" x="1938.099686"/>
    <use xlink:href="#01-bulk-write-HiraginoSansGB-W3-20" x="2004.299683"/>
    <use xlink:href="#01-bulk-write-HiraginoSansGB-W3-5206" x="2037.59967"/>
    <use xlink:href="#01-bulk-write-HiraginoSansGB-W3-7247" x="2137.599655"/>
    <use xlink:href="#01-bulk-write-HiraginoSansGB-W3-5171" x="2237.59964"/>
    <use xlink:href="#01-bulk-write-HiraginoSansGB-W3-4eab" x="2337.599625"/>
    <use xlink:href="#01-bulk-write-HiraginoSansGB-W3-4e00" x="2437.599609"/>
    <use xlink:href="#01-bulk-write-HiraginoSansGB-W3-6c60" x="2537.599594"/>
   </g>
  </g>
 </g>
 <defs>
  <clipPath id="01-bulk-write-paeb002f85f">
   <rect x="147.75125" y="48.312668" width="369.66451" height="279.766016"/>
  </clipPath>
  <clipPath id="01-bulk-write-p009e731841">
   <rect x="668.33501" y="48.312668" width="369.66451" height="279.766016"/>
  </clipPath>
 </defs>
 <defs>
  <pattern id="01-bulk-write-h37cf2cdc2a" patternUnits="userSpaceOnUse" x="0" y="0" width="72" height="72">
   <rect x="0" y="0" width="73" height="73" fill="#c8daed"/>
   <path d="M -36 36 
L 36 -36 
M -32 40 
L 40 -32 
M -28 44 
L 44 -28 
M -24 48 
L 48 -24 
M -20 52 
L 52 -20 
M -16 56 
L 56 -16 
M -12 60 
L 60 -12 
M -8 64 
L 64 -8 
M -4 68 
L 68 -4 
M 0 72 
L 72 0 
M 4 76 
L 76 4 
M 8 80 
L 80 8 
M 12 84 
L 84 12 
M 16 88 
L 88 16 
M 20 92 
L 92 20 
M 24 96 
L 96 24 
M 28 100 
L 100 28 
M 32 104 
L 104 32 
M 36 108 
L 108 36 
" style="fill: #467bb5; stroke: #467bb5; stroke-width: 1.0; stroke-linecap: butt; stroke-linejoin: miter"/>
  </pattern>
  <pattern id="01-bulk-write-h8d297617cc" patternUnits="userSpaceOnUse" x="0" y="0" width="72" height="72">
   <rect x="0" y="0" width="73" height="73" fill="#b5dcd4"/>
   <path d="M -36 36 
L 36 -36 
M -32 40 
L 40 -32 
M -28 44 
L 44 -28 
M -24 48 
L 48 -24 
M -20 52 
L 52 -20 
M -16 56 
L 56 -16 
M -12 60 
L 60 -12 
M -8 64 
L 64 -8 
M -4 68 
L 68 -4 
M 0 72 
L 72 0 
M 4 76 
L 76 4 
M 8 80 
L 80 8 
M 12 84 
L 84 12 
M 16 88 
L 88 16 
M 20 92 
L 92 20 
M 24 96 
L 96 24 
M 28 100 
L 100 28 
M 32 104 
L 104 32 
M 36 108 
L 108 36 
" style="fill: #008477; stroke: #008477; stroke-width: 1.0; stroke-linecap: butt; stroke-linejoin: miter"/>
  </pattern>
 </defs>
</svg>
</figure>
```
**等逻辑任务始终是 2049 行 KV。**
独立模式中，每个 TP 各写一份。
共享模式中，各 TP 分片写完一份。
token 分片分配不同的行；feature 分片分配不同的特征列。
两种分片都让多个 writer 同时执行。

独立池总计写 TP 数量份 KV，共享池总计写一份 KV。
因此，独立池与共享池的等逻辑测试并非等字节写入。

| 消费者 / 用例 | TP2 独立 ms | TP2 共享 ms | TP4 独立 ms | TP4 共享 ms | 共享 TP4 较 TP2 |
| --- | ---: | ---: | ---: | ---: | ---: |
| P / `logical-2049-token` | 0.0784 | 0.0459 | 0.1406 | 0.0447 | -2.6% |
| D / `logical-2049-token` | 0.0783 | 0.0454 | 0.1400 | 0.0442 | -2.6% |
| P / `logical-2049-feature` | 0.0785 | 0.0493 | 0.1405 | 0.0539 | +9.3% |
| D / `logical-2049-feature` | 0.0784 | 0.0489 | 0.1408 | 0.0545 | +11.5% |


| 分片 / 消费者 | TP2 共享较独立 | TP4 共享较独立 |
| --- | ---: | ---: |
| token / P | -41.5% | -68.2% |
| token / D | -42.0% | -68.4% |
| feature / P | -37.2% | -61.7% |
| feature / D | -37.6% | -61.3% |

**token 是本批大块写入的优先候选。**
TP4 的 token 写入耗时与 TP2 接近。
feature 在 TP4 中则增加约 9.3%～11.5%。
共享相对独立的收益扩大，支持减少重复写入的解释。
这些结果不代表小于 2049 行的写入也有相同收益。

**等字节对照：同一 TP 数量内，两种池的 copy 字节量相同。**

| 宽度 / 消费者 | 独立 ms | 共享 ms | 共享 / 独立 |
| --- | ---: | ---: | ---: |
| TP2 / P | 0.0782 | 0.0785 | 1.005 |
| TP2 / D | 0.0781 | 0.0786 | 1.006 |
| TP4 / P | 0.1401 | 0.1391 | 0.993 |
| TP4 / D | 0.1405 | 0.1410 | 1.003 |

两种 TP 数量的等字节对照都接近持平。
这不能证明共享池提高了物理链路带宽。
跨宽度时，TP2 合计写 2049×2 行，TP4 合计写 2049×4 行。
因此，不把这组绝对耗时变化当作同工作量的退化。

**layer 分片只作为参考。**
每个计时用例只写一层，由对应 layer owner 执行。
它不代表多个 writer 同时写同一层。

| 消费者 / 用例 | TP2 独立 ms | TP2 共享 ms | TP4 独立 ms | TP4 共享 ms | 共享 TP4 较 TP2 |
| --- | ---: | ---: | ---: | ---: | ---: |
| P / `logical-2049-layer` | 0.0787 | 0.0486 | 0.1409 | 0.0485 | -0.2% |
| D / `logical-2049-layer` | 0.0784 | 0.0488 | 0.1404 | 0.0486 | -0.4% |
| P / `logical-2049-layer1` | 0.0787 | 0.0490 | 0.1402 | 0.0492 | +0.4% |
| D / `logical-2049-layer1` | 0.0780 | 0.0484 | 0.1406 | 0.0486 | +0.4% |
| P / `logical-2049-layer2` | 不适用 | 不适用 | 0.1409 | 0.0510 | 不适用 |
| D / `logical-2049-layer2` | 不适用 | 不适用 | 0.1410 | 0.0501 | 不适用 |
| P / `logical-2049-layer3` | 不适用 | 不适用 | 0.1416 | 0.0482 | 不适用 |
| D / `logical-2049-layer3` | 不适用 | 不适用 | 0.1399 | 0.0495 | 不适用 |

## D 并发读取：单请求持平，16 请求随 TP 数增加变慢
多个 TP 读取同一份逻辑 selected KV，每个 TP 都获得完整结果。
共享池复用存储，不会把一次读取广播给其他 TP。
独立模式则从各自池中的等价副本读取。

1 / 16 表示活跃请求数，不是 TP 数或 token 数。
每个请求选择 2048 行、每行 576 个 BF16 元素。

| 活跃请求数 | 每个 TP 读取量 | TP2 合计 | TP4 合计 |
| --- | ---: | ---: | ---: |
| 1 | 2.25 MiB | 4.50 MiB | 9.00 MiB |
| 16 | 36 MiB | 72 MiB | 144 MiB |

这里统计应用请求的 copy 字节量，不是物理总线流量。
```svg
<figure class="am-diagram"><svg role="img" aria-label="1请求读取：TP2和TP4的独立池与共享池，共四组对照" xmlns:xlink="http://www.w3.org/1999/xlink" width="1045.19952pt" height="699.596655pt" viewBox="0 0 1045.19952 699.596655" xmlns="http://www.w3.org/2000/svg" version="1.1">
 <metadata>
  <rdf:RDF xmlns:dc="http://purl.org/dc/elements/1.1/" xmlns:cc="http://creativecommons.org/ns#" xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#">
   <cc:Work>
    <dc:type rdf:resource="http://purl.org/dc/dcmitype/StillImage"/>
    <dc:date>2026-10-13T20:32:00.252187</dc:date>
    <dc:format>image/svg+xml</dc:format>
    <dc:creator>
     <cc:Agent>
      <dc:title>Matplotlib v3.9.4, https://matplotlib.org/</dc:title>
     </cc:Agent>
    </dc:creator>
   </cc:Work>
  </rdf:RDF>
 </metadata>
 <defs>
  <style type="text/css">*{stroke-linejoin: round; stroke-linecap: butt}</style>
 </defs>
 <g id="02-read-1-figure_1">
  <g id="02-read-1-patch_1">
   <path d="M 0 699.596655 
L 1045.19952 699.596655 
L 1045.19952 -0 
L 0 -0 
z
" style="fill: #ffffff"/>
  </g>
  <g id="02-read-1-axes_1">
   <g id="02-read-1-patch_2">
    <path d="M 120.68 596.428736 
L 517.41576 596.428736 
L 517.41576 48.149543 
L 120.68 48.149543 
z
" style="fill: #ffffff"/>
   </g>
   <g id="02-read-1-matplotlib.axis_1">
    <g id="02-read-1-xtick_1">
     <g id="02-read-1-line2d_1">
      <path d="M 120.68 596.428736 
L 120.68 48.149543 
" clip-path="url(#02-read-1-p49ec9aeb87)" style="fill: none; stroke: #e9edf2; stroke-width: 0.8; stroke-linecap: square"/>
     </g>
     <g id="02-read-1-line2d_2"/>
     <g id="02-read-1-text_1">
      <!-- 0.0 -->
      <g style="fill: #405570" transform="translate(111.355625 613.619986) scale(0.12 -0.12)">
       <defs>
        <path id="02-read-1-HiraginoSansGB-W3-30" d="M 2099 -58 
C 2688 -58 3136 166 3456 614 
C 3738 1005 3904 1690 3904 2464 
C 3904 2995 3821 3507 3661 3949 
C 3584 4160 3456 4358 3277 4538 
C 2989 4838 2579 4992 2106 4992 
C 1472 4992 979 4723 672 4205 
C 448 3834 301 3142 301 2464 
C 301 1939 384 1434 544 986 
C 621 768 755 570 928 397 
C 1229 96 1626 -58 2099 -58 
z
M 2099 4550 
C 2547 4550 2893 4352 3104 3968 
C 3270 3667 3392 3034 3392 2477 
C 3392 2003 3328 1581 3194 1184 
C 3136 998 3034 832 2893 685 
C 2701 486 2426 384 2106 384 
C 1670 384 1338 570 1114 941 
C 941 1242 813 1869 813 2464 
C 813 2931 877 3366 1011 3757 
C 1062 3917 1165 4077 1299 4224 
C 1504 4442 1766 4550 2099 4550 
z
" transform="scale(0.015625)"/>
        <path id="02-read-1-HiraginoSansGB-W3-2e" d="M 448 672 
L 448 38 
L 1082 38 
L 1082 672 
L 448 672 
z
" transform="scale(0.015625)"/>
       </defs>
       <use xlink:href="#02-read-1-HiraginoSansGB-W3-30"/>
       <use xlink:href="#02-read-1-HiraginoSansGB-W3-2e" x="65.699997"/>
       <use xlink:href="#02-read-1-HiraginoSansGB-W3-30" x="89.699982"/>
      </g>
     </g>
    </g>
    <g id="02-read-1-xtick_2">
     <g id="02-read-1-line2d_3">
      <path d="M 192.813775 596.428736 
L 192.813775 48.149543 
" clip-path="url(#02-read-1-p49ec9aeb87)" style="fill: none; stroke: #e9edf2; stroke-width: 0.8; stroke-linecap: square"/>
     </g>
     <g id="02-read-1-line2d_4"/>
     <g id="02-read-1-text_2">
      <!-- 0.2 -->
      <g style="fill: #405570" transform="translate(183.4894 613.619986) scale(0.12 -0.12)">
       <defs>
        <path id="02-read-1-HiraginoSansGB-W3-32" d="M 3027 38 
C 3347 38 3558 26 3814 0 
L 3814 538 
C 3571 506 3354 493 3034 493 
L 992 493 
C 1069 800 1158 973 1357 1235 
C 1568 1504 1773 1683 2278 2022 
C 3008 2509 3174 2630 3366 2848 
C 3616 3123 3744 3430 3744 3757 
C 3744 4512 3123 4998 2157 4998 
C 1594 4998 1158 4832 870 4506 
C 659 4269 563 4045 467 3584 
L 1005 3494 
C 1050 3814 1107 3968 1229 4141 
C 1421 4416 1734 4557 2157 4557 
C 2810 4557 3219 4243 3219 3757 
C 3219 3366 3008 3085 2374 2656 
C 1632 2150 1619 2144 1395 1952 
C 992 1606 723 1254 563 851 
C 467 608 448 512 378 6 
C 582 32 678 38 1082 38 
L 3027 38 
z
" transform="scale(0.015625)"/>
       </defs>
       <use xlink:href="#02-read-1-HiraginoSansGB-W3-30"/>
       <use xlink:href="#02-read-1-HiraginoSansGB-W3-2e" x="65.699997"/>
       <use xlink:href="#02-read-1-HiraginoSansGB-W3-32" x="89.699982"/>
      </g>
     </g>
    </g>
    <g id="02-read-1-xtick_3">
     <g id="02-read-1-line2d_5">
      <path d="M 264.947549 596.428736 
L 264.947549 48.149543 
" clip-path="url(#02-read-1-p49ec9aeb87)" style="fill: none; stroke: #e9edf2; stroke-width: 0.8; stroke-linecap: square"/>
     </g>
     <g id="02-read-1-line2d_6"/>
     <g id="02-read-1-text_3">
      <!-- 0.4 -->
      <g style="fill: #405570" transform="translate(255.599737 613.619986) scale(0.12 -0.12)">
       <defs>
        <path id="02-read-1-HiraginoSansGB-W3-34" d="M 198 1254 
C 410 1274 602 1280 960 1280 
L 2675 1280 
L 2675 730 
C 2675 442 2662 262 2624 38 
L 3206 38 
C 3168 275 3155 422 3155 730 
L 3155 1280 
L 3392 1280 
C 3667 1280 3827 1267 4064 1235 
L 4064 1766 
C 3834 1734 3667 1722 3398 1722 
L 3155 1722 
L 3155 3821 
C 3155 4326 3162 4525 3194 4902 
L 2714 4902 
C 2566 4678 2451 4518 2234 4250 
L 614 2227 
C 480 2067 333 1888 198 1734 
L 198 1254 
z
M 2682 1715 
L 768 1715 
L 2240 3571 
C 2426 3808 2586 4019 2720 4211 
C 2688 3885 2682 3680 2682 3341 
L 2682 1715 
z
" transform="scale(0.015625)"/>
       </defs>
       <use xlink:href="#02-read-1-HiraginoSansGB-W3-30"/>
       <use xlink:href="#02-read-1-HiraginoSansGB-W3-2e" x="65.699997"/>
       <use xlink:href="#02-read-1-HiraginoSansGB-W3-34" x="89.699982"/>
      </g>
     </g>
    </g>
    <g id="02-read-1-xtick_4">
     <g id="02-read-1-line2d_7">
      <path d="M 337.081324 596.428736 
L 337.081324 48.149543 
" clip-path="url(#02-read-1-p49ec9aeb87)" style="fill: none; stroke: #e9edf2; stroke-width: 0.8; stroke-linecap: square"/>
     </g>
     <g id="02-read-1-line2d_8"/>
     <g id="02-read-1-text_4">
      <!-- 0.6 -->
      <g style="fill: #405570" transform="translate(327.756949 613.619986) scale(0.12 -0.12)">
       <defs>
        <path id="02-read-1-HiraginoSansGB-W3-36" d="M 3725 4006 
C 3539 4390 3411 4563 3187 4723 
C 2944 4896 2598 4992 2214 4992 
C 1658 4992 1203 4755 864 4294 
C 557 3878 378 3162 378 2362 
C 378 1453 608 736 1024 352 
C 1299 96 1728 -51 2182 -51 
C 3194 -51 3859 602 3859 1594 
C 3859 2579 3245 3194 2253 3194 
C 1811 3194 1472 3072 1171 2803 
C 1030 2682 966 2592 851 2394 
C 896 3091 954 3386 1120 3770 
C 1350 4294 1728 4563 2227 4563 
C 2547 4563 2797 4454 2989 4243 
C 3085 4134 3130 4045 3213 3840 
L 3725 4006 
z
M 3354 1581 
C 3354 1331 3290 1069 3187 902 
C 2989 582 2605 384 2189 384 
C 1498 384 979 896 979 1587 
C 979 1907 1088 2189 1299 2400 
C 1523 2624 1862 2765 2182 2765 
C 2931 2765 3354 2336 3354 1581 
z
" transform="scale(0.015625)"/>
       </defs>
       <use xlink:href="#02-read-1-HiraginoSansGB-W3-30"/>
       <use xlink:href="#02-read-1-HiraginoSansGB-W3-2e" x="65.699997"/>
       <use xlink:href="#02-read-1-HiraginoSansGB-W3-36" x="89.699982"/>
      </g>
     </g>
    </g>
    <g id="02-read-1-xtick_5">
     <g id="02-read-1-line2d_9">
      <path d="M 409.215098 596.428736 
L 409.215098 48.149543 
" clip-path="url(#02-read-1-p49ec9aeb87)" style="fill: none; stroke: #e9edf2; stroke-width: 0.8; stroke-linecap: square"/>
     </g>
     <g id="02-read-1-line2d_10"/>
     <g id="02-read-1-text_5">
      <!-- 0.8 -->
      <g style="fill: #405570" transform="translate(399.890723 613.619986) scale(0.12 -0.12)">
       <defs>
        <path id="02-read-1-HiraginoSansGB-W3-38" d="M 2752 2586 
C 3322 2694 3680 3142 3680 3738 
C 3680 4525 3078 5005 2099 5005 
C 1133 5005 525 4512 525 3738 
C 525 3142 890 2688 1453 2586 
C 1216 2547 1075 2496 877 2368 
C 493 2118 307 1766 307 1306 
C 307 422 954 -64 2125 -64 
C 3251 -64 3891 429 3891 1306 
C 3891 1734 3731 2074 3411 2310 
C 3200 2470 3021 2547 2752 2586 
z
M 2099 4576 
C 2758 4576 3181 4237 3181 3699 
C 3181 3130 2778 2784 2106 2784 
C 1434 2784 1024 3130 1024 3699 
C 1024 4230 1446 4576 2099 4576 
z
M 2099 2342 
C 2886 2342 3366 1965 3366 1338 
C 3366 1075 3283 851 3123 704 
C 2906 486 2541 371 2093 371 
C 1299 371 832 730 832 1338 
C 832 1958 1312 2342 2099 2342 
z
" transform="scale(0.015625)"/>
       </defs>
       <use xlink:href="#02-read-1-HiraginoSansGB-W3-30"/>
       <use xlink:href="#02-read-1-HiraginoSansGB-W3-2e" x="65.699997"/>
       <use xlink:href="#02-read-1-HiraginoSansGB-W3-38" x="89.699982"/>
      </g>
     </g>
    </g>
    <g id="02-read-1-xtick_6">
     <g id="02-read-1-line2d_11">
      <path d="M 481.348873 596.428736 
L 481.348873 48.149543 
" clip-path="url(#02-read-1-p49ec9aeb87)" style="fill: none; stroke: #e9edf2; stroke-width: 0.8; stroke-linecap: square"/>
     </g>
     <g id="02-read-1-line2d_12"/>
     <g id="02-read-1-text_6">
      <!-- 1.0 -->
      <g style="fill: #405570" transform="translate(472.024498 613.619986) scale(0.12 -0.12)">
       <defs>
        <path id="02-read-1-HiraginoSansGB-W3-31" d="M 1030 4429 
C 1216 4454 1382 4467 1587 4467 
L 1933 4467 
L 1933 762 
C 1933 467 1920 262 1882 38 
L 2490 38 
C 2451 275 2438 461 2438 762 
L 2438 3872 
C 2438 4218 2458 4544 2496 4934 
C 2285 4909 2182 4902 1965 4902 
L 1562 4902 
C 1318 4902 1197 4909 1030 4934 
L 1030 4429 
z
" transform="scale(0.015625)"/>
       </defs>
       <use xlink:href="#02-read-1-HiraginoSansGB-W3-31"/>
       <use xlink:href="#02-read-1-HiraginoSansGB-W3-2e" x="65.699997"/>
       <use xlink:href="#02-read-1-HiraginoSansGB-W3-30" x="89.699982"/>
      </g>
     </g>
    </g>
    <g id="02-read-1-text_7">
     <!-- 组 P50（ms） · 越低越好 -->
     <g style="fill: #405570" transform="translate(244.390068 629.259986) scale(0.12 -0.12)">
      <defs>
       <path id="02-read-1-HiraginoSansGB-W3-7ec4" d="M 646 1299 
C 845 1363 941 1389 1107 1421 
C 1344 1459 1344 1459 1440 1478 
C 1830 1549 1830 1549 2176 1606 
C 2432 1651 2432 1651 2592 1683 
L 2637 2080 
C 2202 1978 1933 1926 1152 1786 
C 1696 2432 2176 3091 2624 3814 
L 2240 4026 
C 2106 3744 2061 3661 1894 3398 
C 1498 3366 1498 3366 928 3328 
C 1158 3706 1408 4186 1632 4691 
C 1683 4794 1683 4794 1850 5165 
L 1434 5331 
C 1261 4813 1056 4301 858 3904 
C 608 3405 550 3328 384 3302 
L 493 2893 
C 691 2963 966 3002 1677 3053 
C 1555 2867 1555 2867 1152 2330 
C 851 1914 749 1805 634 1773 
C 544 1747 544 1747 518 1741 
L 646 1299 
z
M 5606 141 
L 5606 4275 
C 5606 4653 5606 4794 5626 5005 
C 5453 4986 5254 4979 4922 4979 
L 3699 4979 
C 3411 4979 3245 4986 3059 4998 
C 3078 4787 3085 4602 3085 4339 
L 3085 141 
L 2950 141 
C 2765 141 2598 154 2451 179 
L 2451 -256 
C 2624 -230 2816 -218 3027 -218 
L 5459 -218 
C 5766 -218 5952 -230 6144 -256 
L 6144 179 
C 6016 154 5837 141 5715 141 
L 5606 141 
z
M 3469 4608 
L 5222 4608 
L 5222 3405 
L 3469 3405 
L 3469 4608 
z
M 3469 3046 
L 5222 3046 
L 5222 1786 
L 3469 1786 
L 3469 3046 
z
M 3469 1427 
L 5222 1427 
L 5222 141 
L 3469 141 
L 3469 1427 
z
M 2534 928 
C 2086 755 1536 589 1069 480 
C 755 410 467 365 365 365 
C 352 365 339 365 314 365 
L 416 -90 
C 493 -58 531 -45 646 -19 
C 1152 102 1498 192 1798 294 
C 2336 474 2336 474 2534 531 
L 2534 928 
z
" transform="scale(0.015625)"/>
       <path id="02-read-1-HiraginoSansGB-W3-20" transform="scale(0.015625)"/>
       <path id="02-read-1-HiraginoSansGB-W3-50" d="M 1094 38 
C 1056 256 1043 454 1043 762 
L 1043 2022 
L 2470 2022 
C 3469 2022 4045 2566 4045 3507 
C 4045 4397 3488 4902 2502 4902 
L 1197 4902 
C 902 4902 723 4909 493 4922 
C 531 4653 544 4454 544 4192 
L 544 755 
C 544 454 531 269 493 38 
L 1094 38 
z
M 1043 2464 
L 1043 4461 
L 2470 4461 
C 3162 4461 3533 4122 3533 3482 
C 3533 2829 3149 2464 2470 2464 
L 1043 2464 
z
" transform="scale(0.015625)"/>
       <path id="02-read-1-HiraginoSansGB-W3-35" d="M 1261 4461 
L 2765 4461 
C 3066 4461 3270 4448 3514 4410 
L 3514 4954 
C 3258 4915 3110 4902 2765 4902 
L 1446 4902 
C 1069 4902 966 4909 768 4934 
C 781 4774 781 4749 781 4659 
C 781 4538 774 4461 762 4275 
L 621 2829 
C 595 2554 576 2419 525 2170 
L 1005 2125 
C 1075 2298 1146 2387 1299 2509 
C 1530 2688 1811 2778 2138 2778 
C 2816 2778 3258 2317 3258 1613 
C 3258 877 2778 390 2061 390 
C 1690 390 1370 525 1152 768 
C 1056 877 1011 960 947 1139 
L 422 986 
C 576 678 666 544 826 384 
C 1133 96 1549 -51 2048 -51 
C 3104 -51 3776 608 3776 1638 
C 3776 2112 3635 2490 3347 2765 
C 3059 3046 2662 3200 2195 3200 
C 1754 3200 1382 3053 1075 2758 
C 1114 2931 1114 2931 1126 3046 
L 1261 4461 
z
" transform="scale(0.015625)"/>
       <path id="02-read-1-HiraginoSansGB-W3-ff08" d="M 6157 -518 
C 5619 26 5408 314 5184 806 
C 4954 1318 4838 1850 4838 2432 
C 4838 3014 4954 3546 5184 4058 
C 5408 4550 5619 4838 6157 5382 
L 5587 5382 
C 5107 4832 4909 4525 4698 4006 
C 4486 3494 4390 2989 4390 2432 
C 4390 1875 4486 1370 4698 858 
C 4909 339 5107 32 5587 -518 
L 6157 -518 
z
" transform="scale(0.015625)"/>
       <path id="02-read-1-HiraginoSansGB-W3-6d" d="M 1018 38 
C 986 294 973 467 973 762 
L 973 2016 
C 973 2182 1069 2483 1184 2656 
C 1382 2963 1690 3142 2022 3142 
C 2483 3142 2739 2835 2739 2272 
L 2739 762 
C 2739 435 2726 269 2694 38 
L 3270 38 
C 3238 256 3226 435 3226 755 
L 3226 2016 
C 3226 2208 3315 2490 3437 2675 
C 3622 2970 3923 3142 4243 3142 
C 4755 3142 4979 2880 4979 2272 
L 4979 762 
C 4979 422 4973 275 4934 38 
L 5510 38 
C 5478 262 5466 435 5466 755 
L 5466 2336 
C 5466 2714 5402 2963 5248 3168 
C 5056 3424 4736 3558 4314 3558 
C 3776 3558 3430 3354 3130 2861 
C 3066 3053 3027 3130 2931 3232 
C 2739 3450 2470 3558 2106 3558 
C 1734 3558 1421 3443 1203 3226 
C 1075 3091 1018 3014 960 2906 
C 966 3027 973 3104 973 3136 
L 973 3488 
L 454 3488 
C 486 3258 499 3085 499 2765 
L 499 762 
C 499 461 486 269 454 38 
L 1018 38 
z
" transform="scale(0.015625)"/>
       <path id="02-read-1-HiraginoSansGB-W3-73" d="M 269 973 
C 333 710 378 602 474 467 
C 710 141 1158 -32 1741 -32 
C 2618 -32 3162 365 3162 1005 
C 3162 1267 3053 1510 2854 1670 
C 2662 1818 2458 1894 1837 2035 
C 1440 2118 1274 2170 1126 2253 
C 986 2330 909 2458 909 2605 
C 909 2944 1242 3181 1709 3181 
C 1997 3181 2227 3098 2374 2938 
C 2464 2848 2496 2771 2534 2598 
L 3040 2682 
C 2957 2976 2867 3123 2701 3264 
C 2470 3469 2131 3571 1696 3571 
C 1267 3571 922 3456 698 3238 
C 518 3066 422 2848 422 2618 
C 422 2336 544 2099 768 1946 
C 986 1798 1203 1722 1715 1613 
C 2189 1517 2317 1478 2470 1382 
C 2605 1299 2669 1165 2669 986 
C 2669 608 2317 371 1760 371 
C 1434 371 1178 448 1018 595 
C 890 710 838 826 794 1062 
L 269 973 
z
" transform="scale(0.015625)"/>
       <path id="02-read-1-HiraginoSansGB-W3-ff09" d="M 813 -518 
C 1293 32 1491 339 1702 858 
C 1914 1370 2010 1875 2010 2432 
C 2010 2989 1914 3494 1702 4006 
C 1491 4525 1293 4832 813 5382 
L 243 5382 
C 781 4845 992 4550 1216 4058 
C 1446 3546 1562 3014 1562 2432 
C 1562 1850 1446 1318 1216 806 
C 992 314 781 19 243 -518 
L 813 -518 
z
" transform="scale(0.015625)"/>
       <path id="02-read-1-HiraginoSansGB-W3-b7" d="M 3200 2957 
C 2912 2957 2675 2720 2675 2432 
C 2675 2144 2912 1907 3200 1907 
C 3488 1907 3725 2144 3725 2432 
C 3725 2720 3488 2957 3200 2957 
z
" transform="scale(0.015625)"/>
       <path id="02-read-1-HiraginoSansGB-W3-8d8a" d="M 4026 320 
C 4512 768 4723 979 4934 1254 
C 5242 723 5472 499 5696 499 
C 5914 499 6042 781 6170 1510 
C 6042 1562 5965 1613 5850 1709 
C 5773 1126 5715 922 5632 922 
C 5594 922 5549 966 5421 1133 
C 5299 1293 5254 1370 5152 1594 
C 5440 2099 5536 2349 5722 3066 
C 5760 3219 5786 3290 5818 3386 
L 5440 3526 
C 5370 3078 5197 2522 4979 2042 
C 4832 2477 4768 2931 4710 3962 
L 5408 3962 
C 5645 3962 5837 3949 6010 3923 
L 6010 4339 
C 5875 4314 5709 4307 5408 4307 
L 4698 4307 
C 4691 4371 4691 4390 4691 4467 
C 4691 5171 4691 5171 4717 5318 
L 4282 5318 
C 4314 5094 4314 5075 4314 4403 
C 4314 4365 4314 4352 4314 4307 
L 3667 4307 
C 3398 4307 3238 4314 3078 4333 
C 3098 4166 3104 4006 3104 3750 
L 3104 1389 
C 3085 1216 3066 1184 2950 1114 
L 3168 768 
C 3277 832 3277 832 3757 1120 
C 3853 1178 4013 1280 4333 1491 
L 4256 1850 
C 3846 1574 3699 1485 3526 1395 
C 3501 1382 3501 1382 3475 1370 
L 3475 3962 
L 4333 3962 
C 4403 2867 4512 2246 4749 1632 
C 4422 1190 4154 915 3757 621 
C 3770 614 3795 582 3853 531 
C 3904 480 3917 467 3974 390 
L 4026 320 
z
M 1549 4422 
L 1056 4422 
C 762 4422 646 4429 499 4454 
L 499 4038 
C 666 4064 819 4070 1056 4070 
L 1549 4070 
L 1549 3213 
L 960 3213 
C 666 3213 499 3226 352 3251 
L 352 2822 
C 518 2854 666 2861 960 2861 
L 1594 2861 
L 1594 653 
C 1382 845 1165 1133 1030 1395 
C 1056 1606 1075 1734 1082 1914 
C 1101 2266 1107 2278 1120 2400 
L 704 2426 
C 723 2310 723 2272 723 2170 
C 723 1229 531 442 141 -205 
C 282 -275 352 -320 474 -442 
C 710 0 870 448 960 947 
C 1139 659 1293 467 1485 294 
C 1869 -51 2253 -211 2906 -275 
C 3322 -320 3526 -326 4928 -326 
C 5146 -326 5760 -307 5997 -294 
C 6029 -109 6048 -38 6138 147 
C 5683 90 5037 58 4326 58 
C 3757 58 3232 77 2829 115 
C 2458 147 2221 218 1965 371 
L 1965 1677 
L 2317 1677 
C 2541 1677 2701 1664 2842 1638 
L 2842 2048 
C 2688 2022 2541 2016 2323 2016 
L 1965 2016 
L 1965 2861 
L 2336 2861 
C 2618 2861 2778 2848 2938 2822 
L 2938 3251 
C 2790 3226 2630 3213 2336 3213 
L 1926 3213 
L 1926 4070 
L 2246 4070 
C 2502 4070 2656 4064 2822 4038 
L 2822 4454 
C 2682 4429 2573 4422 2246 4422 
L 1926 4422 
L 1926 4678 
C 1926 4986 1933 5133 1958 5306 
L 1510 5306 
C 1542 5133 1549 4986 1549 4678 
L 1549 4422 
z
M 4947 5088 
C 5210 4870 5350 4710 5542 4442 
L 5811 4666 
C 5613 4922 5530 5011 5210 5299 
L 4947 5088 
z
" transform="scale(0.015625)"/>
       <path id="02-read-1-HiraginoSansGB-W3-4f4e" d="M 4198 2502 
C 4378 1261 4627 570 5094 19 
C 5318 -243 5466 -352 5606 -352 
C 5792 -352 5901 -262 6003 -32 
C 6074 128 6182 602 6208 851 
C 6074 941 5978 1018 5869 1133 
C 5830 525 5722 96 5606 96 
C 5478 96 5120 602 4941 1043 
C 4794 1395 4653 1990 4595 2502 
L 5395 2502 
C 5651 2502 5805 2496 5965 2470 
L 5965 2886 
C 5818 2861 5715 2854 5395 2854 
L 4550 2854 
C 4486 3405 4467 3706 4454 4582 
C 4896 4685 4960 4704 5722 4973 
L 5382 5338 
C 4883 5005 3507 4691 2387 4666 
C 2413 4499 2419 4378 2419 4083 
L 2419 205 
C 2419 83 2368 26 2272 6 
L 2413 -384 
C 2784 -256 3078 -173 3680 -32 
L 3712 365 
C 3200 224 2899 147 2816 128 
L 2816 2502 
L 4198 2502 
z
M 2816 2854 
L 2816 4320 
C 3315 4365 3648 4410 4058 4493 
C 4064 3930 4096 3437 4154 2854 
L 2816 2854 
z
M 1043 288 
C 1043 -32 1030 -243 1005 -480 
L 1472 -480 
C 1446 -256 1440 -38 1440 269 
L 1440 3750 
C 1773 4474 1773 4474 1946 4954 
C 1997 5088 2016 5139 2061 5248 
L 1613 5370 
C 1568 5088 1421 4627 1216 4186 
C 947 3597 608 3104 134 2611 
C 275 2451 314 2400 397 2246 
C 698 2592 870 2835 1069 3206 
C 1050 2976 1043 2714 1043 2438 
L 1043 288 
z
M 4749 -122 
C 4493 262 4218 589 3834 960 
L 3552 755 
C 3904 403 4179 58 4429 -358 
L 4749 -122 
z
" transform="scale(0.015625)"/>
       <path id="02-read-1-HiraginoSansGB-W3-597d" d="M 1094 3680 
C 928 3008 742 2458 493 1894 
C 954 1613 1075 1530 1517 1158 
C 1184 646 832 307 269 -38 
C 384 -141 461 -237 550 -378 
C 1139 38 1459 346 1773 838 
C 1792 864 1792 864 1811 890 
C 2074 621 2170 518 2445 173 
L 2726 512 
C 2374 909 2246 1037 2016 1242 
C 2029 1261 2029 1261 2048 1293 
C 2374 1894 2592 2733 2688 3776 
C 2707 3974 2707 3974 2720 4064 
C 2573 4045 2400 4038 2138 4038 
L 1581 4038 
C 1626 4250 1696 4634 1722 4806 
C 1773 5133 1786 5197 1811 5299 
L 1344 5344 
C 1350 5306 1350 5274 1350 5254 
C 1350 4998 1280 4499 1171 4038 
L 941 4038 
C 710 4038 538 4051 378 4077 
L 378 3642 
C 538 3667 698 3680 902 3680 
L 1094 3680 
z
M 2278 3680 
C 2176 2720 2016 2086 1715 1504 
C 1427 1728 1318 1805 973 2042 
C 1203 2637 1312 2989 1498 3680 
L 2278 3680 
z
M 4608 2605 
L 4608 3226 
C 5011 3565 5293 3846 5498 4102 
C 5907 4627 5907 4627 5952 4672 
L 5715 4966 
C 5613 4941 5485 4934 5222 4934 
L 3578 4934 
C 3309 4934 3098 4947 2931 4973 
L 2931 4518 
C 3091 4544 3322 4557 3558 4557 
L 5363 4557 
C 5037 4096 4787 3827 4378 3501 
L 4154 3501 
C 4186 3270 4198 3104 4198 2874 
L 4198 2605 
L 3328 2605 
C 3034 2605 2906 2611 2752 2643 
L 2752 2189 
C 2925 2214 3078 2227 3334 2227 
L 4198 2227 
L 4198 147 
C 4198 32 4154 13 3949 13 
C 3763 13 3488 45 3117 102 
C 3174 -70 3194 -147 3206 -333 
C 3546 -365 3802 -384 3974 -384 
C 4493 -384 4608 -294 4608 134 
L 4608 2227 
L 5664 2227 
C 5926 2227 6061 2221 6234 2189 
L 6234 2643 
C 6067 2611 5939 2605 5664 2605 
L 4608 2605 
z
" transform="scale(0.015625)"/>
      </defs>
      <use xlink:href="#02-read-1-HiraginoSansGB-W3-7ec4"/>
      <use xlink:href="#02-read-1-HiraginoSansGB-W3-20" x="99.999985"/>
      <use xlink:href="#02-read-1-HiraginoSansGB-W3-50" x="133.299973"/>
      <use xlink:href="#02-read-1-HiraginoSansGB-W3-35" x="199.499969"/>
      <use xlink:href="#02-read-1-HiraginoSansGB-W3-30" x="265.199966"/>
      <use xlink:href="#02-read-1-HiraginoSansGB-W3-ff08" x="330.899963"/>
      <use xlink:href="#02-read-1-HiraginoSansGB-W3-6d" x="430.899948"/>
      <use xlink:href="#02-read-1-HiraginoSansGB-W3-73" x="523.999939"/>
      <use xlink:href="#02-read-1-HiraginoSansGB-W3-ff09" x="577.699936"/>
      <use xlink:href="#02-read-1-HiraginoSansGB-W3-20" x="677.699921"/>
      <use xlink:href="#02-read-1-HiraginoSansGB-W3-b7" x="710.999908"/>
      <use xlink:href="#02-read-1-HiraginoSansGB-W3-20" x="810.999893"/>
      <use xlink:href="#02-read-1-HiraginoSansGB-W3-8d8a" x="844.299881"/>
      <use xlink:href="#02-read-1-HiraginoSansGB-W3-4f4e" x="944.299866"/>
      <use xlink:href="#02-read-1-HiraginoSansGB-W3-8d8a" x="1044.29985"/>
      <use xlink:href="#02-read-1-HiraginoSansGB-W3-597d" x="1144.299835"/>
     </g>
    </g>
   </g>
   <g id="02-read-1-matplotlib.axis_2">
    <g id="02-read-1-ytick_1">
     <g id="02-read-1-line2d_13"/>
     <g id="02-read-1-text_8">
      <!-- P 来源 / 连续 -->
      <g style="fill: #243a56" transform="translate(39.06 117.629115) scale(0.12 -0.12)">
       <defs>
        <path id="02-read-1-HiraginoSansGB-W3-6765" d="M 5306 2182 
C 5613 2182 5824 2170 6022 2144 
L 6022 2598 
C 5869 2566 5690 2554 5318 2554 
L 4371 2554 
C 4621 2931 4864 3379 5082 3859 
L 4685 4019 
C 4467 3443 4269 3059 3923 2554 
L 3334 2554 
L 3334 4230 
L 4979 4230 
C 5286 4230 5485 4218 5677 4192 
L 5677 4640 
C 5517 4608 5331 4595 4966 4595 
L 3334 4595 
L 3334 4806 
C 3334 5062 3341 5178 3373 5338 
L 2899 5338 
C 2925 5165 2938 5024 2938 4806 
L 2938 4595 
L 1421 4595 
C 1139 4595 947 4608 723 4640 
L 723 4192 
C 947 4218 1158 4230 1408 4230 
L 2938 4230 
L 2938 2554 
L 1082 2554 
C 787 2554 602 2566 384 2598 
L 384 2144 
C 640 2170 832 2182 1088 2182 
L 2758 2182 
C 2406 1734 2349 1664 2106 1434 
C 1587 922 986 506 326 186 
C 467 45 512 -19 614 -186 
C 1037 51 1389 288 1760 595 
C 2272 1018 2554 1338 2963 1971 
C 2944 1619 2938 1472 2938 1286 
L 2938 141 
C 2938 -122 2925 -288 2893 -480 
L 3379 -480 
C 3347 -294 3334 -122 3334 141 
L 3334 1286 
C 3334 1446 3328 1645 3309 1978 
C 3661 1472 3878 1210 4211 902 
C 4685 474 5178 141 5824 -186 
C 5914 -19 5984 83 6099 211 
C 5446 480 4928 806 4371 1293 
C 4070 1555 3930 1696 3526 2182 
L 5306 2182 
z
M 1248 3859 
C 1530 3424 1658 3174 1869 2656 
L 2227 2842 
C 1978 3386 1869 3597 1594 4026 
L 1248 3859 
z
" transform="scale(0.015625)"/>
        <path id="02-read-1-HiraginoSansGB-W3-6e90" d="M 4262 3987 
C 4371 4262 4397 4326 4486 4659 
L 5331 4659 
C 5574 4659 5818 4646 6003 4627 
L 6003 5056 
C 5818 5030 5638 5024 5325 5024 
L 2662 5024 
C 2368 5024 2227 5030 2054 5056 
C 2074 4774 2086 4474 2086 4211 
L 2086 4166 
L 2080 3360 
C 2074 2477 1978 1600 1824 1030 
C 1709 595 1587 307 1370 -38 
C 1555 -160 1594 -198 1709 -339 
C 2266 621 2483 1811 2483 3936 
L 2483 4659 
L 4077 4659 
C 4032 4416 3962 4192 3866 3987 
L 3507 3987 
C 3258 3987 3066 3994 2899 4013 
C 2912 3859 2918 3706 2918 3507 
L 2918 2259 
C 2918 2048 2912 1926 2893 1805 
C 3085 1824 3245 1830 3437 1830 
L 4090 1830 
L 4090 141 
C 4090 6 4058 -26 3904 -26 
C 3718 -26 3578 -13 3309 26 
C 3366 -134 3386 -224 3398 -378 
C 3565 -390 3782 -397 3923 -397 
C 4122 -397 4211 -384 4307 -346 
C 4422 -294 4486 -166 4486 26 
L 4486 1830 
L 5190 1830 
C 5389 1830 5530 1824 5702 1805 
C 5683 1926 5677 2048 5677 2304 
L 5677 3533 
C 5677 3763 5683 3891 5702 4013 
C 5523 3994 5357 3987 5107 3987 
L 4262 3987 
z
M 3296 3654 
L 5299 3654 
L 5299 3098 
L 3296 3098 
L 3296 3654 
z
M 3296 2765 
L 5299 2765 
L 5299 2170 
L 3296 2170 
L 3296 2765 
z
M 531 4922 
C 922 4666 1133 4474 1466 4090 
L 1754 4384 
C 1453 4710 1216 4915 819 5197 
L 531 4922 
z
M 243 3341 
C 672 3078 870 2925 1274 2522 
L 1530 2867 
C 1171 3206 922 3392 499 3648 
L 243 3341 
z
M 1133 1882 
C 890 1005 544 224 282 -38 
L 646 -365 
C 1088 499 1210 819 1453 1658 
L 1133 1882 
z
M 4947 1421 
C 5280 915 5472 544 5715 -102 
L 6067 96 
C 5830 678 5645 1024 5280 1594 
L 4947 1421 
z
M 3110 1562 
C 3034 1216 2982 1050 2854 794 
C 2720 518 2618 371 2387 96 
C 2522 13 2592 -45 2701 -166 
C 3110 371 3290 749 3482 1440 
L 3110 1562 
z
" transform="scale(0.015625)"/>
        <path id="02-read-1-HiraginoSansGB-W3-2f" d="M 2432 4902 
L 122 -954 
L 602 -954 
L 2906 4902 
L 2432 4902 
z
" transform="scale(0.015625)"/>
        <path id="02-read-1-HiraginoSansGB-W3-8fde" d="M 3149 4736 
L 2662 4736 
C 2381 4736 2195 4749 2016 4781 
L 2016 4339 
C 2195 4365 2406 4378 2656 4378 
L 3008 4378 
L 2656 3526 
C 2554 3270 2438 3149 2291 3123 
L 2394 2733 
C 2509 2758 2675 2771 2822 2771 
L 4013 2771 
L 4013 1914 
L 2605 1914 
C 2304 1914 2125 1926 1958 1958 
L 1958 1510 
C 2131 1536 2342 1549 2611 1549 
L 4013 1549 
L 4013 1088 
C 4013 755 4006 608 3968 384 
L 4435 384 
C 4397 589 4390 736 4390 1088 
L 4390 1549 
L 5434 1549 
C 5702 1549 5894 1536 6093 1510 
L 6093 1958 
C 5939 1926 5760 1914 5440 1914 
L 4390 1914 
L 4390 2771 
L 5030 2771 
C 5254 2771 5510 2758 5696 2733 
L 5696 3187 
C 5517 3162 5331 3149 5030 3149 
L 4390 3149 
L 4390 3379 
C 4390 3635 4403 3802 4435 3994 
L 3968 3994 
C 4000 3808 4013 3642 4013 3379 
L 4013 3149 
L 2906 3149 
L 3411 4378 
L 5299 4378 
C 5549 4378 5747 4365 5952 4339 
L 5952 4781 
C 5786 4749 5613 4736 5293 4736 
L 3558 4736 
C 3590 4800 3603 4838 3616 4864 
C 3706 5082 3706 5082 3802 5274 
L 3341 5389 
C 3296 5139 3258 4986 3168 4768 
L 3149 4736 
z
M 1638 2221 
C 1638 2509 1645 2675 1670 2867 
C 1510 2842 1395 2835 1190 2835 
L 851 2835 
C 602 2835 499 2842 358 2867 
L 358 2451 
C 506 2477 608 2483 826 2483 
L 1254 2483 
L 1254 858 
C 1171 832 1075 774 909 653 
C 762 544 518 314 237 13 
L 518 -320 
C 845 147 1203 486 1357 486 
C 1414 486 1485 448 1536 390 
C 1773 115 1773 115 1882 38 
C 2106 -115 2336 -198 2630 -237 
C 2906 -275 3424 -307 3827 -307 
C 4717 -307 5408 -294 6029 -262 
C 6074 -58 6080 -26 6182 205 
C 5626 128 4909 90 3981 90 
C 3194 90 2662 128 2419 205 
C 2150 288 1997 397 1766 666 
C 1709 736 1677 768 1638 806 
L 1638 2221 
z
M 435 4941 
C 864 4538 1062 4307 1427 3776 
L 1754 4070 
C 1350 4582 1146 4813 730 5197 
L 435 4941 
z
" transform="scale(0.015625)"/>
        <path id="02-read-1-HiraginoSansGB-W3-7eed" d="M 4026 1152 
C 3949 947 3827 768 3661 602 
C 3315 262 2886 58 2202 -115 
C 2310 -211 2368 -307 2438 -467 
C 3085 -275 3514 -51 3885 275 
C 4154 525 4326 787 4435 1152 
L 5466 1152 
C 5754 1152 5958 1139 6118 1120 
L 6118 1536 
C 5978 1510 5792 1504 5472 1504 
L 4512 1504 
C 4563 1811 4589 2150 4614 2778 
C 4621 2944 4621 2982 4653 3123 
L 4186 3130 
C 4205 2989 4211 2931 4211 2784 
C 4211 2285 4179 1818 4122 1504 
L 2989 1504 
C 2656 1504 2490 1517 2342 1542 
L 2342 1120 
C 2509 1139 2720 1152 3021 1152 
L 4026 1152 
z
M 3930 3725 
L 2989 3725 
C 2726 3725 2573 3738 2426 3763 
L 2426 3322 
C 2573 3347 2726 3360 3002 3360 
L 5542 3360 
C 5434 3117 5318 2918 5120 2669 
C 5254 2618 5363 2541 5466 2426 
C 5606 2637 5805 2950 5824 3008 
C 5965 3328 5978 3347 6048 3469 
L 5792 3744 
C 5670 3731 5574 3725 5350 3725 
L 4326 3725 
L 4326 4371 
L 5203 4371 
C 5478 4371 5683 4358 5856 4333 
L 5856 4762 
C 5709 4736 5478 4723 5197 4723 
L 4326 4723 
C 4333 4986 4339 5126 4365 5254 
L 3891 5254 
C 3917 5107 3923 5005 3930 4723 
L 3232 4723 
C 2950 4723 2707 4736 2566 4762 
L 2566 4333 
C 2720 4358 2963 4371 3232 4371 
L 3930 4371 
L 3930 3725 
z
M 1344 5254 
C 1229 4851 1094 4518 787 3898 
C 563 3443 461 3290 320 3206 
L 403 2842 
C 672 2886 928 2918 1408 2970 
C 909 2157 909 2157 742 1933 
C 627 1773 557 1715 416 1658 
L 538 1248 
C 736 1312 832 1331 2163 1606 
L 2202 2010 
C 1690 1869 1523 1830 1018 1715 
C 1421 2278 1779 2842 1997 3238 
C 2182 3584 2182 3584 2291 3763 
L 1920 3974 
C 1862 3866 1824 3776 1798 3718 
C 1722 3552 1670 3437 1651 3398 
C 1632 3360 1632 3360 1619 3334 
C 1120 3277 1120 3277 845 3258 
C 1024 3565 1101 3712 1190 3878 
C 1261 4019 1574 4672 1613 4762 
C 1715 5018 1715 5018 1747 5075 
L 1344 5254 
z
M 3987 2586 
C 3622 2854 3430 2982 3059 3187 
L 2822 2938 
C 3219 2726 3424 2592 3757 2317 
L 3987 2586 
z
M 2246 813 
C 1978 698 1907 678 1645 595 
C 1402 518 947 384 819 352 
C 640 301 378 256 294 256 
L 397 -186 
C 448 -160 467 -154 595 -122 
C 1254 64 1254 64 2259 422 
L 2246 813 
z
M 6029 -83 
C 5594 237 5152 512 4634 794 
L 4410 525 
C 5318 -45 5382 -90 5805 -435 
L 6029 -83 
z
M 3565 1965 
C 3238 2195 3155 2246 2605 2554 
L 2368 2291 
C 2733 2112 2938 1978 3334 1677 
L 3565 1965 
z
" transform="scale(0.015625)"/>
       </defs>
       <use xlink:href="#02-read-1-HiraginoSansGB-W3-50"/>
       <use xlink:href="#02-read-1-HiraginoSansGB-W3-20" x="66.199997"/>
       <use xlink:href="#02-read-1-HiraginoSansGB-W3-6765" x="99.499985"/>
       <use xlink:href="#02-read-1-HiraginoSansGB-W3-6e90" x="199.499969"/>
       <use xlink:href="#02-read-1-HiraginoSansGB-W3-20" x="299.499954"/>
       <use xlink:href="#02-read-1-HiraginoSansGB-W3-2f" x="332.799942"/>
       <use xlink:href="#02-read-1-HiraginoSansGB-W3-20" x="380.199936"/>
       <use xlink:href="#02-read-1-HiraginoSansGB-W3-8fde" x="413.499924"/>
       <use xlink:href="#02-read-1-HiraginoSansGB-W3-7eed" x="513.499908"/>
      </g>
     </g>
    </g>
    <g id="02-read-1-ytick_2">
     <g id="02-read-1-line2d_14"/>
     <g id="02-read-1-text_9">
      <!-- P 来源 / 离散 -->
      <g style="fill: #243a56" transform="translate(39.06 203.97792) scale(0.12 -0.12)">
       <defs>
        <path id="02-read-1-HiraginoSansGB-W3-79bb" d="M 4570 3046 
C 4166 3258 4115 3283 3667 3501 
C 3923 3648 4096 3757 4275 3891 
C 4346 3942 4358 3949 4416 3987 
L 4109 4243 
C 3968 4083 3648 3859 3296 3674 
C 2643 3955 2618 3968 2150 4147 
L 1933 3878 
C 2413 3693 2592 3622 2893 3488 
C 2528 3328 2106 3187 1677 3078 
C 1798 2989 1856 2918 1933 2778 
C 2387 2912 2861 3091 3296 3302 
C 3610 3155 3776 3066 4339 2739 
L 4570 3046 
z
M 5357 1498 
L 5357 96 
C 5357 -38 5325 -58 5146 -58 
C 4954 -58 4723 -38 4499 -6 
C 4550 -147 4563 -211 4595 -435 
C 4768 -448 4966 -454 5107 -454 
C 5613 -454 5754 -352 5754 19 
L 5754 1280 
C 5754 1536 5760 1702 5779 1882 
C 5587 1856 5434 1850 5203 1850 
L 2982 1850 
C 3021 1933 3085 2093 3174 2323 
L 4774 2323 
C 5005 2323 5146 2317 5280 2304 
C 5261 2451 5261 2528 5261 2784 
L 5261 3616 
C 5261 3840 5267 3955 5286 4115 
L 4845 4115 
C 4864 3962 4870 3834 4870 3629 
L 4870 2669 
L 1517 2669 
L 1517 3635 
C 1517 3840 1523 3981 1542 4115 
L 1101 4115 
C 1120 3955 1126 3834 1126 3616 
L 1126 2790 
C 1126 2541 1126 2464 1107 2304 
C 1248 2317 1370 2323 1600 2323 
L 2733 2323 
C 2630 2016 2611 1971 2560 1850 
L 1197 1850 
C 960 1850 806 1856 608 1882 
C 627 1702 634 1491 634 1242 
L 634 122 
C 634 -198 627 -365 602 -525 
L 1062 -525 
C 1037 -384 1030 -224 1030 122 
L 1030 1498 
L 2419 1498 
C 2118 762 2029 627 1773 544 
L 1894 179 
C 2234 230 3008 326 4211 474 
C 4269 365 4307 294 4410 90 
L 4736 288 
C 4454 768 4371 890 3968 1370 
L 3667 1210 
C 3853 992 3910 922 4000 794 
C 3539 730 3539 730 3110 685 
C 2963 672 2720 646 2426 614 
C 2579 902 2643 1043 2778 1363 
C 2816 1446 2816 1446 2835 1498 
L 5357 1498 
z
M 3488 4717 
C 3341 4973 3270 5069 3034 5357 
L 2694 5152 
C 2867 4947 2925 4870 3034 4717 
L 1069 4717 
C 794 4717 582 4730 403 4755 
L 403 4320 
C 608 4346 800 4352 1062 4352 
L 5402 4352 
C 5677 4352 5837 4346 6054 4320 
L 6054 4755 
C 5875 4730 5670 4717 5395 4717 
L 3488 4717 
z
" transform="scale(0.015625)"/>
        <path id="02-read-1-HiraginoSansGB-W3-6563" d="M 6170 4198 
C 5990 4166 5856 4160 5562 4160 
L 4282 4160 
C 4352 4371 4422 4653 4493 4954 
C 4550 5203 4557 5222 4582 5312 
L 4141 5395 
C 4141 5069 4006 4454 3827 3955 
C 3622 3405 3430 3046 3136 2714 
C 3232 2630 3315 2528 3405 2374 
C 3578 2611 3635 2701 3789 2995 
C 3904 2374 4166 1664 4454 1190 
C 4102 653 3738 307 3091 -109 
C 3283 -282 3296 -294 3386 -448 
C 3917 -96 4307 282 4691 826 
C 5094 269 5414 -32 6010 -435 
C 6086 -256 6099 -230 6246 -19 
C 5709 294 5261 704 4915 1190 
C 5344 1952 5555 2707 5645 3795 
C 5869 3795 6016 3782 6170 3757 
L 6170 4198 
z
M 5248 3795 
C 5184 3194 5165 3078 5075 2694 
C 4973 2266 4845 1901 4672 1568 
C 4358 2144 4186 2637 4064 3334 
C 4058 3366 4045 3411 4038 3475 
C 4096 3622 4102 3635 4160 3795 
L 5248 3795 
z
M 2688 4333 
L 2765 4333 
C 2995 4333 3155 4326 3296 4301 
L 3296 4710 
C 3142 4685 3027 4678 2765 4678 
L 2688 4678 
L 2688 4813 
C 2688 5082 2694 5203 2720 5338 
L 2278 5338 
C 2298 5203 2304 5107 2304 4813 
L 2304 4678 
L 1498 4678 
L 1498 4813 
C 1498 5126 1504 5216 1523 5338 
L 1082 5338 
C 1107 5178 1114 5075 1114 4813 
L 1114 4678 
L 992 4678 
C 730 4678 621 4685 461 4710 
L 461 4301 
C 602 4326 755 4333 992 4333 
L 1114 4333 
L 1114 3590 
L 870 3590 
C 634 3590 493 3603 320 3629 
L 320 3206 
C 506 3232 653 3245 870 3245 
L 2797 3245 
C 3053 3245 3168 3238 3341 3213 
L 3341 3622 
C 3181 3597 3078 3590 2803 3590 
L 2688 3590 
L 2688 4333 
z
M 2304 4333 
L 2304 3590 
L 1498 3590 
L 1498 4333 
L 2304 4333 
z
M 2611 762 
L 2611 115 
C 2611 6 2573 -19 2394 -19 
C 2234 -19 2080 -6 1875 32 
C 1933 -128 1939 -173 1965 -346 
C 2157 -358 2278 -365 2400 -365 
C 2880 -365 3002 -275 3002 64 
L 3002 2214 
C 3002 2528 3008 2688 3021 2829 
C 2854 2810 2669 2797 2445 2797 
L 1286 2797 
C 1069 2797 877 2810 698 2829 
C 710 2682 717 2547 717 2246 
L 717 237 
C 717 -58 704 -307 678 -474 
L 1139 -474 
C 1114 -314 1107 -96 1107 243 
L 1107 762 
L 2611 762 
z
M 2611 1094 
L 1107 1094 
L 1107 1619 
L 2611 1619 
L 2611 1094 
z
M 2611 1952 
L 1107 1952 
L 1107 2458 
L 2611 2458 
L 2611 1952 
z
" transform="scale(0.015625)"/>
       </defs>
       <use xlink:href="#02-read-1-HiraginoSansGB-W3-50"/>
       <use xlink:href="#02-read-1-HiraginoSansGB-W3-20" x="66.199997"/>
       <use xlink:href="#02-read-1-HiraginoSansGB-W3-6765" x="99.499985"/>
       <use xlink:href="#02-read-1-HiraginoSansGB-W3-6e90" x="199.499969"/>
       <use xlink:href="#02-read-1-HiraginoSansGB-W3-20" x="299.499954"/>
       <use xlink:href="#02-read-1-HiraginoSansGB-W3-2f" x="332.799942"/>
       <use xlink:href="#02-read-1-HiraginoSansGB-W3-20" x="380.199936"/>
       <use xlink:href="#02-read-1-HiraginoSansGB-W3-79bb" x="413.499924"/>
       <use xlink:href="#02-read-1-HiraginoSansGB-W3-6563" x="513.499908"/>
      </g>
     </g>
    </g>
    <g id="02-read-1-ytick_3">
     <g id="02-read-1-line2d_15"/>
     <g id="02-read-1-text_10">
      <!-- D 来源 / 连续 -->
      <g style="fill: #243a56" transform="translate(37.90875 290.315475) scale(0.12 -0.12)">
       <defs>
        <path id="02-read-1-HiraginoSansGB-W3-44" d="M 544 781 
C 544 461 531 269 493 19 
C 717 32 890 38 1197 38 
L 2278 38 
C 2918 38 3450 230 3834 608 
C 4275 1037 4518 1702 4518 2502 
C 4518 4032 3706 4902 2278 4902 
L 1197 4902 
C 890 4902 717 4909 493 4922 
C 531 4653 544 4467 544 4192 
L 544 781 
z
M 1043 486 
L 1043 4454 
L 2291 4454 
C 2816 4454 3213 4314 3494 4019 
C 3834 3680 4000 3168 4000 2502 
C 4000 1843 3814 1299 3462 941 
C 3174 634 2771 486 2266 486 
L 1043 486 
z
" transform="scale(0.015625)"/>
       </defs>
       <use xlink:href="#02-read-1-HiraginoSansGB-W3-44"/>
       <use xlink:href="#02-read-1-HiraginoSansGB-W3-20" x="75.799988"/>
       <use xlink:href="#02-read-1-HiraginoSansGB-W3-6765" x="109.099976"/>
       <use xlink:href="#02-read-1-HiraginoSansGB-W3-6e90" x="209.09996"/>
       <use xlink:href="#02-read-1-HiraginoSansGB-W3-20" x="309.099945"/>
       <use xlink:href="#02-read-1-HiraginoSansGB-W3-2f" x="342.399933"/>
       <use xlink:href="#02-read-1-HiraginoSansGB-W3-20" x="389.799927"/>
       <use xlink:href="#02-read-1-HiraginoSansGB-W3-8fde" x="423.099915"/>
       <use xlink:href="#02-read-1-HiraginoSansGB-W3-7eed" x="523.099899"/>
      </g>
     </g>
    </g>
    <g id="02-read-1-ytick_4">
     <g id="02-read-1-line2d_16"/>
     <g id="02-read-1-text_11">
      <!-- D 来源 / 离散 -->
      <g style="fill: #243a56" transform="translate(37.90875 376.66428) scale(0.12 -0.12)">
       <use xlink:href="#02-read-1-HiraginoSansGB-W3-44"/>
       <use xlink:href="#02-read-1-HiraginoSansGB-W3-20" x="75.799988"/>
       <use xlink:href="#02-read-1-HiraginoSansGB-W3-6765" x="109.099976"/>
       <use xlink:href="#02-read-1-HiraginoSansGB-W3-6e90" x="209.09996"/>
       <use xlink:href="#02-read-1-HiraginoSansGB-W3-20" x="309.099945"/>
       <use xlink:href="#02-read-1-HiraginoSansGB-W3-2f" x="342.399933"/>
       <use xlink:href="#02-read-1-HiraginoSansGB-W3-20" x="389.799927"/>
       <use xlink:href="#02-read-1-HiraginoSansGB-W3-79bb" x="423.099915"/>
       <use xlink:href="#02-read-1-HiraginoSansGB-W3-6563" x="523.099899"/>
      </g>
     </g>
    </g>
    <g id="02-read-1-ytick_5">
     <g id="02-read-1-line2d_17"/>
     <g id="02-read-1-text_12">
      <!-- MIXED 来源 / 连续 -->
      <g style="fill: #243a56" transform="translate(7.2 463.001835) scale(0.12 -0.12)">
       <defs>
        <path id="02-read-1-HiraginoSansGB-W3-4d" d="M 2554 710 
C 2682 422 2758 205 2790 38 
L 3245 38 
C 3296 250 3360 416 3488 704 
L 4794 3578 
C 4915 3840 5005 4096 5069 4358 
C 5043 4096 5024 3750 5024 3571 
L 5024 794 
C 5024 486 5011 256 4973 38 
L 5555 38 
C 5517 269 5504 442 5504 794 
L 5504 4141 
C 5504 4474 5517 4659 5555 4902 
L 4819 4902 
C 4781 4717 4710 4518 4602 4269 
L 3027 742 
L 1446 4269 
C 1350 4480 1274 4710 1235 4902 
L 493 4902 
C 531 4666 544 4461 544 4141 
L 544 794 
C 544 461 531 275 493 38 
L 1075 38 
C 1037 256 1024 454 1024 794 
L 1024 3533 
C 1024 3686 998 4083 979 4358 
C 1101 3968 1171 3770 1267 3565 
L 2554 710 
z
" transform="scale(0.015625)"/>
        <path id="02-read-1-HiraginoSansGB-W3-49" d="M 493 4902 
C 531 4672 544 4506 544 4186 
L 544 755 
C 544 435 531 275 493 38 
L 1101 38 
C 1062 256 1043 474 1043 755 
L 1043 4186 
C 1043 4454 1062 4691 1101 4902 
L 493 4902 
z
" transform="scale(0.015625)"/>
        <path id="02-read-1-HiraginoSansGB-W3-58" d="M 2304 2266 
L 3360 685 
C 3501 480 3571 358 3757 38 
L 4493 38 
C 4179 435 4026 640 3904 813 
L 2624 2611 
L 3750 4205 
C 3942 4474 4102 4685 4282 4902 
L 3558 4902 
C 3488 4730 3430 4634 3206 4294 
L 2323 2944 
L 1408 4294 
C 1235 4544 1126 4730 1050 4902 
L 333 4902 
C 531 4653 678 4454 858 4211 
L 1990 2611 
L 704 800 
C 570 614 486 493 134 38 
L 864 38 
C 973 250 1094 454 1248 691 
L 2304 2266 
z
" transform="scale(0.015625)"/>
        <path id="02-read-1-HiraginoSansGB-W3-45" d="M 3789 531 
C 3552 499 3373 486 3078 486 
L 1050 486 
L 1050 2368 
L 2733 2368 
C 3066 2368 3232 2355 3450 2323 
L 3450 2861 
C 3206 2829 3014 2816 2733 2816 
L 1050 2816 
L 1050 4454 
L 3021 4454 
C 3341 4454 3514 4442 3738 4410 
L 3738 4947 
C 3520 4915 3302 4902 3021 4902 
L 1197 4902 
C 890 4902 717 4909 493 4922 
C 531 4640 544 4467 544 4192 
L 544 781 
C 544 454 531 262 493 19 
C 710 32 890 38 1197 38 
L 3072 38 
C 3411 38 3584 26 3789 -6 
L 3789 531 
z
" transform="scale(0.015625)"/>
       </defs>
       <use xlink:href="#02-read-1-HiraginoSansGB-W3-4d"/>
       <use xlink:href="#02-read-1-HiraginoSansGB-W3-49" x="94.499985"/>
       <use xlink:href="#02-read-1-HiraginoSansGB-W3-58" x="119.399979"/>
       <use xlink:href="#02-read-1-HiraginoSansGB-W3-45" x="191.59996"/>
       <use xlink:href="#02-read-1-HiraginoSansGB-W3-44" x="255.899948"/>
       <use xlink:href="#02-read-1-HiraginoSansGB-W3-20" x="331.699936"/>
       <use xlink:href="#02-read-1-HiraginoSansGB-W3-6765" x="364.999924"/>
       <use xlink:href="#02-read-1-HiraginoSansGB-W3-6e90" x="464.999908"/>
       <use xlink:href="#02-read-1-HiraginoSansGB-W3-20" x="564.999893"/>
       <use xlink:href="#02-read-1-HiraginoSansGB-W3-2f" x="598.299881"/>
       <use xlink:href="#02-read-1-HiraginoSansGB-W3-20" x="645.699875"/>
       <use xlink:href="#02-read-1-HiraginoSansGB-W3-8fde" x="678.999863"/>
       <use xlink:href="#02-read-1-HiraginoSansGB-W3-7eed" x="778.999847"/>
      </g>
     </g>
    </g>
    <g id="02-read-1-ytick_6">
     <g id="02-read-1-line2d_18"/>
     <g id="02-read-1-text_13">
      <!-- MIXED 来源 / 离散 -->
      <g style="fill: #243a56" transform="translate(7.2 549.35064) scale(0.12 -0.12)">
       <use xlink:href="#02-read-1-HiraginoSansGB-W3-4d"/>
       <use xlink:href="#02-read-1-HiraginoSansGB-W3-49" x="94.499985"/>
       <use xlink:href="#02-read-1-HiraginoSansGB-W3-58" x="119.399979"/>
       <use xlink:href="#02-read-1-HiraginoSansGB-W3-45" x="191.59996"/>
       <use xlink:href="#02-read-1-HiraginoSansGB-W3-44" x="255.899948"/>
       <use xlink:href="#02-read-1-HiraginoSansGB-W3-20" x="331.699936"/>
       <use xlink:href="#02-read-1-HiraginoSansGB-W3-6765" x="364.999924"/>
       <use xlink:href="#02-read-1-HiraginoSansGB-W3-6e90" x="464.999908"/>
       <use xlink:href="#02-read-1-HiraginoSansGB-W3-20" x="564.999893"/>
       <use xlink:href="#02-read-1-HiraginoSansGB-W3-2f" x="598.299881"/>
       <use xlink:href="#02-read-1-HiraginoSansGB-W3-20" x="645.699875"/>
       <use xlink:href="#02-read-1-HiraginoSansGB-W3-79bb" x="678.999863"/>
       <use xlink:href="#02-read-1-HiraginoSansGB-W3-6563" x="778.999847"/>
      </g>
     </g>
    </g>
   </g>
   <g id="02-read-1-patch_3">
    <path d="M 120.68 78.153798 
L 248.068246 78.153798 
L 248.068246 93.263854 
L 120.68 93.263854 
z
" clip-path="url(#02-read-1-p49ec9aeb87)" style="fill: url(#02-read-1-h02a70d68cd); stroke: #467bb5; stroke-width: 0.5; stroke-linejoin: miter"/>
   </g>
   <g id="02-read-1-patch_4">
    <path d="M 120.68 164.496978 
L 248.71745 164.496978 
L 248.71745 179.607034 
L 120.68 179.607034 
z
" clip-path="url(#02-read-1-p49ec9aeb87)" style="fill: url(#02-read-1-h02a70d68cd); stroke: #467bb5; stroke-width: 0.5; stroke-linejoin: miter"/>
   </g>
   <g id="02-read-1-patch_5">
    <path d="M 120.68 250.840158 
L 422.704114 250.840158 
L 422.704114 265.950214 
L 120.68 265.950214 
z
" clip-path="url(#02-read-1-p49ec9aeb87)" style="fill: url(#02-read-1-h02a70d68cd); stroke: #467bb5; stroke-width: 0.5; stroke-linejoin: miter"/>
   </g>
   <g id="02-read-1-patch_6">
    <path d="M 120.68 337.183338 
L 422.848382 337.183338 
L 422.848382 352.293394 
L 120.68 352.293394 
z
" clip-path="url(#02-read-1-p49ec9aeb87)" style="fill: url(#02-read-1-h02a70d68cd); stroke: #467bb5; stroke-width: 0.5; stroke-linejoin: miter"/>
   </g>
   <g id="02-read-1-patch_7">
    <path d="M 120.68 423.526518 
L 335.818983 423.526518 
L 335.818983 438.636574 
L 120.68 438.636574 
z
" clip-path="url(#02-read-1-p49ec9aeb87)" style="fill: url(#02-read-1-h02a70d68cd); stroke: #467bb5; stroke-width: 0.5; stroke-linejoin: miter"/>
   </g>
   <g id="02-read-1-patch_8">
    <path d="M 120.68 509.869698 
L 335.314046 509.869698 
L 335.314046 524.979754 
L 120.68 524.979754 
z
" clip-path="url(#02-read-1-p49ec9aeb87)" style="fill: url(#02-read-1-h02a70d68cd); stroke: #467bb5; stroke-width: 0.5; stroke-linejoin: miter"/>
   </g>
   <g id="02-read-1-patch_9">
    <path d="M 120.68 96.285865 
L 248.428915 96.285865 
L 248.428915 111.395922 
L 120.68 111.395922 
z
" clip-path="url(#02-read-1-p49ec9aeb87)" style="fill: #467bb5; stroke: #467bb5; stroke-width: 0.5; stroke-linejoin: miter"/>
   </g>
   <g id="02-read-1-patch_10">
    <path d="M 120.68 182.629045 
L 250.015858 182.629045 
L 250.015858 197.739102 
L 120.68 197.739102 
z
" clip-path="url(#02-read-1-p49ec9aeb87)" style="fill: #467bb5; stroke: #467bb5; stroke-width: 0.5; stroke-linejoin: miter"/>
   </g>
   <g id="02-read-1-patch_11">
    <path d="M 120.68 268.972226 
L 421.297505 268.972226 
L 421.297505 284.082282 
L 120.68 284.082282 
z
" clip-path="url(#02-read-1-p49ec9aeb87)" style="fill: #467bb5; stroke: #467bb5; stroke-width: 0.5; stroke-linejoin: miter"/>
   </g>
   <g id="02-read-1-patch_12">
    <path d="M 120.68 355.315406 
L 424.687793 355.315406 
L 424.687793 370.425462 
L 120.68 370.425462 
z
" clip-path="url(#02-read-1-p49ec9aeb87)" style="fill: #467bb5; stroke: #467bb5; stroke-width: 0.5; stroke-linejoin: miter"/>
   </g>
   <g id="02-read-1-patch_13">
    <path d="M 120.68 441.658586 
L 337.333792 441.658586 
L 337.333792 456.768642 
L 120.68 456.768642 
z
" clip-path="url(#02-read-1-p49ec9aeb87)" style="fill: #467bb5; stroke: #467bb5; stroke-width: 0.5; stroke-linejoin: miter"/>
   </g>
   <g id="02-read-1-patch_14">
    <path d="M 120.68 528.001766 
L 334.268106 528.001766 
L 334.268106 543.111822 
L 120.68 543.111822 
z
" clip-path="url(#02-read-1-p49ec9aeb87)" style="fill: #467bb5; stroke: #467bb5; stroke-width: 0.5; stroke-linejoin: miter"/>
   </g>
   <g id="02-read-1-patch_15">
    <path d="M 120.68 114.417933 
L 248.176447 114.417933 
L 248.176447 129.52799 
L 120.68 129.52799 
z
" clip-path="url(#02-read-1-p49ec9aeb87)" style="fill: url(#02-read-1-hf8fe4700db); stroke: #008477; stroke-width: 0.5; stroke-linejoin: miter"/>
   </g>
   <g id="02-read-1-patch_16">
    <path d="M 120.68 200.761113 
L 248.573182 200.761113 
L 248.573182 215.87117 
L 120.68 215.87117 
z
" clip-path="url(#02-read-1-p49ec9aeb87)" style="fill: url(#02-read-1-hf8fe4700db); stroke: #008477; stroke-width: 0.5; stroke-linejoin: miter"/>
   </g>
   <g id="02-read-1-patch_17">
    <path d="M 120.68 287.104293 
L 423.497586 287.104293 
L 423.497586 302.21435 
L 120.68 302.21435 
z
" clip-path="url(#02-read-1-p49ec9aeb87)" style="fill: url(#02-read-1-hf8fe4700db); stroke: #008477; stroke-width: 0.5; stroke-linejoin: miter"/>
   </g>
   <g id="02-read-1-patch_18">
    <path d="M 120.68 373.447473 
L 424.218923 373.447473 
L 424.218923 388.55753 
L 120.68 388.55753 
z
" clip-path="url(#02-read-1-p49ec9aeb87)" style="fill: url(#02-read-1-hf8fe4700db); stroke: #008477; stroke-width: 0.5; stroke-linejoin: miter"/>
   </g>
   <g id="02-read-1-patch_19">
    <path d="M 120.68 459.790654 
L 335.494381 459.790654 
L 335.494381 474.90071 
L 120.68 474.90071 
z
" clip-path="url(#02-read-1-p49ec9aeb87)" style="fill: url(#02-read-1-hf8fe4700db); stroke: #008477; stroke-width: 0.5; stroke-linejoin: miter"/>
   </g>
   <g id="02-read-1-patch_20">
    <path d="M 120.68 546.133834 
L 335.818983 546.133834 
L 335.818983 561.24389 
L 120.68 561.24389 
z
" clip-path="url(#02-read-1-p49ec9aeb87)" style="fill: url(#02-read-1-hf8fe4700db); stroke: #008477; stroke-width: 0.5; stroke-linejoin: miter"/>
   </g>
   <g id="02-read-1-patch_21">
    <path d="M 120.68 132.550001 
L 248.428915 132.550001 
L 248.428915 147.660058 
L 120.68 147.660058 
z
" clip-path="url(#02-read-1-p49ec9aeb87)" style="fill: #008477; stroke: #008477; stroke-width: 0.5; stroke-linejoin: miter"/>
   </g>
   <g id="02-read-1-patch_22">
    <path d="M 120.68 218.893181 
L 248.320714 218.893181 
L 248.320714 234.003238 
L 120.68 234.003238 
z
" clip-path="url(#02-read-1-p49ec9aeb87)" style="fill: #008477; stroke: #008477; stroke-width: 0.5; stroke-linejoin: miter"/>
   </g>
   <g id="02-read-1-patch_23">
    <path d="M 120.68 305.236361 
L 423.425452 305.236361 
L 423.425452 320.346418 
L 120.68 320.346418 
z
" clip-path="url(#02-read-1-p49ec9aeb87)" style="fill: #008477; stroke: #008477; stroke-width: 0.5; stroke-linejoin: miter"/>
   </g>
   <g id="02-read-1-patch_24">
    <path d="M 120.68 391.579541 
L 422.704114 391.579541 
L 422.704114 406.689598 
L 120.68 406.689598 
z
" clip-path="url(#02-read-1-p49ec9aeb87)" style="fill: #008477; stroke: #008477; stroke-width: 0.5; stroke-linejoin: miter"/>
   </g>
   <g id="02-read-1-patch_25">
    <path d="M 120.68 477.922721 
L 335.097645 477.922721 
L 335.097645 493.032778 
L 120.68 493.032778 
z
" clip-path="url(#02-read-1-p49ec9aeb87)" style="fill: #008477; stroke: #008477; stroke-width: 0.5; stroke-linejoin: miter"/>
   </g>
   <g id="02-read-1-patch_26">
    <path d="M 120.68 564.265901 
L 334.700909 564.265901 
L 334.700909 579.375958 
L 120.68 579.375958 
z
" clip-path="url(#02-read-1-p49ec9aeb87)" style="fill: #008477; stroke: #008477; stroke-width: 0.5; stroke-linejoin: miter"/>
   </g>
   <g id="02-read-1-patch_27">
    <path d="M 120.68 596.428736 
L 517.41576 596.428736 
" style="fill: none; stroke: #d7e0eb; stroke-width: 0.8; stroke-linejoin: miter; stroke-linecap: square"/>
   </g>
   <g id="02-read-1-text_14">
    <!-- 0.3532 -->
    <g style="fill: #405570" transform="translate(252.829075 88.518201) scale(0.1 -0.1)">
     <defs>
      <path id="02-read-1-HiraginoSansGB-W3-33" d="M 288 1088 
C 403 762 480 621 614 467 
C 922 122 1402 -58 2048 -58 
C 3117 -58 3782 480 3782 1344 
C 3782 1754 3635 2093 3354 2310 
C 3136 2477 2938 2547 2566 2586 
C 2861 2643 3002 2694 3168 2816 
C 3450 3014 3616 3360 3616 3750 
C 3616 4518 3027 5005 2099 5005 
C 1542 5005 1107 4845 826 4538 
C 666 4371 589 4224 480 3898 
L 1011 3782 
C 1082 4019 1126 4122 1216 4237 
C 1395 4467 1677 4576 2067 4576 
C 2714 4576 3098 4256 3098 3706 
C 3098 3475 3002 3238 2842 3085 
C 2643 2886 2330 2790 1907 2790 
C 1734 2790 1581 2803 1434 2829 
L 1434 2323 
C 1606 2342 1766 2355 1946 2355 
C 2797 2355 3251 2010 3251 1376 
C 3251 768 2784 384 2042 384 
C 1715 384 1434 461 1216 614 
C 1018 755 928 890 826 1222 
L 288 1088 
z
" transform="scale(0.015625)"/>
     </defs>
     <use xlink:href="#02-read-1-HiraginoSansGB-W3-30"/>
     <use xlink:href="#02-read-1-HiraginoSansGB-W3-2e" x="65.699997"/>
     <use xlink:href="#02-read-1-HiraginoSansGB-W3-33" x="89.699982"/>
     <use xlink:href="#02-read-1-HiraginoSansGB-W3-35" x="155.399979"/>
     <use xlink:href="#02-read-1-HiraginoSansGB-W3-33" x="221.099976"/>
     <use xlink:href="#02-read-1-HiraginoSansGB-W3-32" x="286.799973"/>
    </g>
   </g>
   <g id="02-read-1-text_15">
    <!-- 0.3550 -->
    <g style="fill: #405570" transform="translate(253.478279 174.861381) scale(0.1 -0.1)">
     <use xlink:href="#02-read-1-HiraginoSansGB-W3-30"/>
     <use xlink:href="#02-read-1-HiraginoSansGB-W3-2e" x="65.699997"/>
     <use xlink:href="#02-read-1-HiraginoSansGB-W3-33" x="89.699982"/>
     <use xlink:href="#02-read-1-HiraginoSansGB-W3-35" x="155.399979"/>
     <use xlink:href="#02-read-1-HiraginoSansGB-W3-35" x="221.099976"/>
     <use xlink:href="#02-read-1-HiraginoSansGB-W3-30" x="286.799973"/>
    </g>
   </g>
   <g id="02-read-1-text_16">
    <!-- 0.8374 -->
    <g style="fill: #405570" transform="translate(427.464943 261.204561) scale(0.1 -0.1)">
     <defs>
      <path id="02-read-1-HiraginoSansGB-W3-37" d="M 3789 4934 
C 3565 4909 3424 4902 3046 4902 
L 1126 4902 
C 819 4902 646 4915 397 4947 
L 397 4403 
C 640 4435 819 4448 1120 4448 
L 3200 4448 
C 2630 3674 2355 3232 2048 2592 
C 1651 1773 1478 1197 1274 38 
L 1882 38 
C 1984 781 2054 1107 2221 1638 
C 2470 2419 2739 2963 3315 3853 
C 3565 4237 3674 4378 3789 4467 
L 3789 4934 
z
" transform="scale(0.015625)"/>
     </defs>
     <use xlink:href="#02-read-1-HiraginoSansGB-W3-30"/>
     <use xlink:href="#02-read-1-HiraginoSansGB-W3-2e" x="65.699997"/>
     <use xlink:href="#02-read-1-HiraginoSansGB-W3-38" x="89.699982"/>
     <use xlink:href="#02-read-1-HiraginoSansGB-W3-33" x="155.399979"/>
     <use xlink:href="#02-read-1-HiraginoSansGB-W3-37" x="221.099976"/>
     <use xlink:href="#02-read-1-HiraginoSansGB-W3-34" x="286.799973"/>
    </g>
   </g>
   <g id="02-read-1-text_17">
    <!-- 0.8378 -->
    <g style="fill: #405570" transform="translate(427.609211 347.547741) scale(0.1 -0.1)">
     <use xlink:href="#02-read-1-HiraginoSansGB-W3-30"/>
     <use xlink:href="#02-read-1-HiraginoSansGB-W3-2e" x="65.699997"/>
     <use xlink:href="#02-read-1-HiraginoSansGB-W3-38" x="89.699982"/>
     <use xlink:href="#02-read-1-HiraginoSansGB-W3-33" x="155.399979"/>
     <use xlink:href="#02-read-1-HiraginoSansGB-W3-37" x="221.099976"/>
     <use xlink:href="#02-read-1-HiraginoSansGB-W3-38" x="286.799973"/>
    </g>
   </g>
   <g id="02-read-1-text_18">
    <!-- 0.5965 -->
    <g style="fill: #405570" transform="translate(340.579812 433.890921) scale(0.1 -0.1)">
     <defs>
      <path id="02-read-1-HiraginoSansGB-W3-39" d="M 416 934 
C 582 595 685 448 864 301 
C 1139 70 1498 -45 1939 -45 
C 2528 -45 2950 147 3270 550 
C 3616 979 3802 1702 3802 2592 
C 3802 3142 3744 3578 3616 3923 
C 3373 4589 2784 4998 2067 4998 
C 1018 4998 333 4333 333 3315 
C 333 2861 493 2438 768 2163 
C 1043 1888 1446 1741 1920 1741 
C 2400 1741 2758 1882 3085 2189 
C 3200 2310 3251 2374 3341 2547 
C 3328 2221 3309 1958 3283 1798 
C 3232 1498 3130 1184 3008 979 
C 2771 576 2438 390 1978 390 
C 1626 390 1350 493 1171 691 
C 1069 800 1018 890 941 1101 
L 416 934 
z
M 1978 2170 
C 1280 2170 838 2624 838 3347 
C 838 3597 902 3853 1005 4019 
C 1216 4358 1600 4563 2022 4563 
C 2707 4563 3219 4045 3219 3354 
C 3219 2675 2688 2170 1978 2170 
z
" transform="scale(0.015625)"/>
     </defs>
     <use xlink:href="#02-read-1-HiraginoSansGB-W3-30"/>
     <use xlink:href="#02-read-1-HiraginoSansGB-W3-2e" x="65.699997"/>
     <use xlink:href="#02-read-1-HiraginoSansGB-W3-35" x="89.699982"/>
     <use xlink:href="#02-read-1-HiraginoSansGB-W3-39" x="155.399979"/>
     <use xlink:href="#02-read-1-HiraginoSansGB-W3-36" x="221.099976"/>
     <use xlink:href="#02-read-1-HiraginoSansGB-W3-35" x="286.799973"/>
    </g>
   </g>
   <g id="02-read-1-text_19">
    <!-- 0.5951 -->
    <g style="fill: #405570" transform="translate(340.074875 520.234101) scale(0.1 -0.1)">
     <use xlink:href="#02-read-1-HiraginoSansGB-W3-30"/>
     <use xlink:href="#02-read-1-HiraginoSansGB-W3-2e" x="65.699997"/>
     <use xlink:href="#02-read-1-HiraginoSansGB-W3-35" x="89.699982"/>
     <use xlink:href="#02-read-1-HiraginoSansGB-W3-39" x="155.399979"/>
     <use xlink:href="#02-read-1-HiraginoSansGB-W3-35" x="221.099976"/>
     <use xlink:href="#02-read-1-HiraginoSansGB-W3-31" x="286.799973"/>
    </g>
   </g>
   <g id="02-read-1-text_20">
    <!-- 0.3542 -->
    <g style="fill: #467bb5" transform="translate(253.189744 106.650269) scale(0.1 -0.1)">
     <use xlink:href="#02-read-1-HiraginoSansGB-W3-30"/>
     <use xlink:href="#02-read-1-HiraginoSansGB-W3-2e" x="65.699997"/>
     <use xlink:href="#02-read-1-HiraginoSansGB-W3-33" x="89.699982"/>
     <use xlink:href="#02-read-1-HiraginoSansGB-W3-35" x="155.399979"/>
     <use xlink:href="#02-read-1-HiraginoSansGB-W3-34" x="221.099976"/>
     <use xlink:href="#02-read-1-HiraginoSansGB-W3-32" x="287.199966"/>
    </g>
   </g>
   <g id="02-read-1-text_21">
    <!-- 0.3586 -->
    <g style="fill: #467bb5" transform="translate(254.776687 192.993449) scale(0.1 -0.1)">
     <use xlink:href="#02-read-1-HiraginoSansGB-W3-30"/>
     <use xlink:href="#02-read-1-HiraginoSansGB-W3-2e" x="65.699997"/>
     <use xlink:href="#02-read-1-HiraginoSansGB-W3-33" x="89.699982"/>
     <use xlink:href="#02-read-1-HiraginoSansGB-W3-35" x="155.399979"/>
     <use xlink:href="#02-read-1-HiraginoSansGB-W3-38" x="221.099976"/>
     <use xlink:href="#02-read-1-HiraginoSansGB-W3-36" x="286.799973"/>
    </g>
   </g>
   <g id="02-read-1-text_22">
    <!-- 0.8335 -->
    <g style="fill: #467bb5" transform="translate(426.058335 279.336629) scale(0.1 -0.1)">
     <use xlink:href="#02-read-1-HiraginoSansGB-W3-30"/>
     <use xlink:href="#02-read-1-HiraginoSansGB-W3-2e" x="65.699997"/>
     <use xlink:href="#02-read-1-HiraginoSansGB-W3-38" x="89.699982"/>
     <use xlink:href="#02-read-1-HiraginoSansGB-W3-33" x="155.399979"/>
     <use xlink:href="#02-read-1-HiraginoSansGB-W3-33" x="221.099976"/>
     <use xlink:href="#02-read-1-HiraginoSansGB-W3-35" x="286.799973"/>
    </g>
   </g>
   <g id="02-read-1-text_23">
    <!-- 0.8429 -->
    <g style="fill: #467bb5" transform="translate(429.448622 365.679809) scale(0.1 -0.1)">
     <use xlink:href="#02-read-1-HiraginoSansGB-W3-30"/>
     <use xlink:href="#02-read-1-HiraginoSansGB-W3-2e" x="65.699997"/>
     <use xlink:href="#02-read-1-HiraginoSansGB-W3-38" x="89.699982"/>
     <use xlink:href="#02-read-1-HiraginoSansGB-W3-34" x="155.399979"/>
     <use xlink:href="#02-read-1-HiraginoSansGB-W3-32" x="221.499969"/>
     <use xlink:href="#02-read-1-HiraginoSansGB-W3-39" x="287.199966"/>
    </g>
   </g>
   <g id="02-read-1-text_24">
    <!-- 0.6007 -->
    <g style="fill: #467bb5" transform="translate(342.094621 452.022989) scale(0.1 -0.1)">
     <use xlink:href="#02-read-1-HiraginoSansGB-W3-30"/>
     <use xlink:href="#02-read-1-HiraginoSansGB-W3-2e" x="65.699997"/>
     <use xlink:href="#02-read-1-HiraginoSansGB-W3-36" x="89.699982"/>
     <use xlink:href="#02-read-1-HiraginoSansGB-W3-30" x="155.399979"/>
     <use xlink:href="#02-read-1-HiraginoSansGB-W3-30" x="221.099976"/>
     <use xlink:href="#02-read-1-HiraginoSansGB-W3-37" x="286.799973"/>
    </g>
   </g>
   <g id="02-read-1-text_25">
    <!-- 0.5922 -->
    <g style="fill: #467bb5" transform="translate(339.028936 538.366169) scale(0.1 -0.1)">
     <use xlink:href="#02-read-1-HiraginoSansGB-W3-30"/>
     <use xlink:href="#02-read-1-HiraginoSansGB-W3-2e" x="65.699997"/>
     <use xlink:href="#02-read-1-HiraginoSansGB-W3-35" x="89.699982"/>
     <use xlink:href="#02-read-1-HiraginoSansGB-W3-39" x="155.399979"/>
     <use xlink:href="#02-read-1-HiraginoSansGB-W3-32" x="221.099976"/>
     <use xlink:href="#02-read-1-HiraginoSansGB-W3-32" x="286.799973"/>
    </g>
   </g>
   <g id="02-read-1-text_26">
    <!-- 0.3535 -->
    <g style="fill: #405570" transform="translate(252.937276 124.782336) scale(0.1 -0.1)">
     <use xlink:href="#02-read-1-HiraginoSansGB-W3-30"/>
     <use xlink:href="#02-read-1-HiraginoSansGB-W3-2e" x="65.699997"/>
     <use xlink:href="#02-read-1-HiraginoSansGB-W3-33" x="89.699982"/>
     <use xlink:href="#02-read-1-HiraginoSansGB-W3-35" x="155.399979"/>
     <use xlink:href="#02-read-1-HiraginoSansGB-W3-33" x="221.099976"/>
     <use xlink:href="#02-read-1-HiraginoSansGB-W3-35" x="286.799973"/>
    </g>
   </g>
   <g id="02-read-1-text_27">
    <!-- 0.3546 -->
    <g style="fill: #405570" transform="translate(253.334011 211.125517) scale(0.1 -0.1)">
     <use xlink:href="#02-read-1-HiraginoSansGB-W3-30"/>
     <use xlink:href="#02-read-1-HiraginoSansGB-W3-2e" x="65.699997"/>
     <use xlink:href="#02-read-1-HiraginoSansGB-W3-33" x="89.699982"/>
     <use xlink:href="#02-read-1-HiraginoSansGB-W3-35" x="155.399979"/>
     <use xlink:href="#02-read-1-HiraginoSansGB-W3-34" x="221.099976"/>
     <use xlink:href="#02-read-1-HiraginoSansGB-W3-36" x="287.199966"/>
    </g>
   </g>
   <g id="02-read-1-text_28">
    <!-- 0.8396 -->
    <g style="fill: #405570" transform="translate(428.258415 297.468697) scale(0.1 -0.1)">
     <use xlink:href="#02-read-1-HiraginoSansGB-W3-30"/>
     <use xlink:href="#02-read-1-HiraginoSansGB-W3-2e" x="65.699997"/>
     <use xlink:href="#02-read-1-HiraginoSansGB-W3-38" x="89.699982"/>
     <use xlink:href="#02-read-1-HiraginoSansGB-W3-33" x="155.399979"/>
     <use xlink:href="#02-read-1-HiraginoSansGB-W3-39" x="221.099976"/>
     <use xlink:href="#02-read-1-HiraginoSansGB-W3-36" x="286.799973"/>
    </g>
   </g>
   <g id="02-read-1-text_29">
    <!-- 0.8416 -->
    <g style="fill: #405570" transform="translate(428.979752 383.811877) scale(0.1 -0.1)">
     <use xlink:href="#02-read-1-HiraginoSansGB-W3-30"/>
     <use xlink:href="#02-read-1-HiraginoSansGB-W3-2e" x="65.699997"/>
     <use xlink:href="#02-read-1-HiraginoSansGB-W3-38" x="89.699982"/>
     <use xlink:href="#02-read-1-HiraginoSansGB-W3-34" x="155.399979"/>
     <use xlink:href="#02-read-1-HiraginoSansGB-W3-31" x="221.499969"/>
     <use xlink:href="#02-read-1-HiraginoSansGB-W3-36" x="287.199966"/>
    </g>
   </g>
   <g id="02-read-1-text_30">
    <!-- 0.5956 -->
    <g style="fill: #405570" transform="translate(340.25521 470.155057) scale(0.1 -0.1)">
     <use xlink:href="#02-read-1-HiraginoSansGB-W3-30"/>
     <use xlink:href="#02-read-1-HiraginoSansGB-W3-2e" x="65.699997"/>
     <use xlink:href="#02-read-1-HiraginoSansGB-W3-35" x="89.699982"/>
     <use xlink:href="#02-read-1-HiraginoSansGB-W3-39" x="155.399979"/>
     <use xlink:href="#02-read-1-HiraginoSansGB-W3-35" x="221.099976"/>
     <use xlink:href="#02-read-1-HiraginoSansGB-W3-36" x="286.799973"/>
    </g>
   </g>
   <g id="02-read-1-text_31">
    <!-- 0.5965 -->
    <g style="fill: #405570" transform="translate(340.579812 556.498237) scale(0.1 -0.1)">
     <use xlink:href="#02-read-1-HiraginoSansGB-W3-30"/>
     <use xlink:href="#02-read-1-HiraginoSansGB-W3-2e" x="65.699997"/>
     <use xlink:href="#02-read-1-HiraginoSansGB-W3-35" x="89.699982"/>
     <use xlink:href="#02-read-1-HiraginoSansGB-W3-39" x="155.399979"/>
     <use xlink:href="#02-read-1-HiraginoSansGB-W3-36" x="221.099976"/>
     <use xlink:href="#02-read-1-HiraginoSansGB-W3-35" x="286.799973"/>
    </g>
   </g>
   <g id="02-read-1-text_32">
    <!-- 0.3542 -->
    <g style="fill: #008477" transform="translate(253.189744 142.914404) scale(0.1 -0.1)">
     <use xlink:href="#02-read-1-HiraginoSansGB-W3-30"/>
     <use xlink:href="#02-read-1-HiraginoSansGB-W3-2e" x="65.699997"/>
     <use xlink:href="#02-read-1-HiraginoSansGB-W3-33" x="89.699982"/>
     <use xlink:href="#02-read-1-HiraginoSansGB-W3-35" x="155.399979"/>
     <use xlink:href="#02-read-1-HiraginoSansGB-W3-34" x="221.099976"/>
     <use xlink:href="#02-read-1-HiraginoSansGB-W3-32" x="287.199966"/>
    </g>
   </g>
   <g id="02-read-1-text_33">
    <!-- 0.3539 -->
    <g style="fill: #008477" transform="translate(253.081543 229.257584) scale(0.1 -0.1)">
     <use xlink:href="#02-read-1-HiraginoSansGB-W3-30"/>
     <use xlink:href="#02-read-1-HiraginoSansGB-W3-2e" x="65.699997"/>
     <use xlink:href="#02-read-1-HiraginoSansGB-W3-33" x="89.699982"/>
     <use xlink:href="#02-read-1-HiraginoSansGB-W3-35" x="155.399979"/>
     <use xlink:href="#02-read-1-HiraginoSansGB-W3-33" x="221.099976"/>
     <use xlink:href="#02-read-1-HiraginoSansGB-W3-39" x="286.799973"/>
    </g>
   </g>
   <g id="02-read-1-text_34">
    <!-- 0.8394 -->
    <g style="fill: #008477" transform="translate(428.186281 315.600764) scale(0.1 -0.1)">
     <use xlink:href="#02-read-1-HiraginoSansGB-W3-30"/>
     <use xlink:href="#02-read-1-HiraginoSansGB-W3-2e" x="65.699997"/>
     <use xlink:href="#02-read-1-HiraginoSansGB-W3-38" x="89.699982"/>
     <use xlink:href="#02-read-1-HiraginoSansGB-W3-33" x="155.399979"/>
     <use xlink:href="#02-read-1-HiraginoSansGB-W3-39" x="221.099976"/>
     <use xlink:href="#02-read-1-HiraginoSansGB-W3-34" x="286.799973"/>
    </g>
   </g>
   <g id="02-read-1-text_35">
    <!-- 0.8374 -->
    <g style="fill: #008477" transform="translate(427.464943 401.943945) scale(0.1 -0.1)">
     <use xlink:href="#02-read-1-HiraginoSansGB-W3-30"/>
     <use xlink:href="#02-read-1-HiraginoSansGB-W3-2e" x="65.699997"/>
     <use xlink:href="#02-read-1-HiraginoSansGB-W3-38" x="89.699982"/>
     <use xlink:href="#02-read-1-HiraginoSansGB-W3-33" x="155.399979"/>
     <use xlink:href="#02-read-1-HiraginoSansGB-W3-37" x="221.099976"/>
     <use xlink:href="#02-read-1-HiraginoSansGB-W3-34" x="286.799973"/>
    </g>
   </g>
   <g id="02-read-1-text_36">
    <!-- 0.5945 -->
    <g style="fill: #008477" transform="translate(339.858474 488.287125) scale(0.1 -0.1)">
     <use xlink:href="#02-read-1-HiraginoSansGB-W3-30"/>
     <use xlink:href="#02-read-1-HiraginoSansGB-W3-2e" x="65.699997"/>
     <use xlink:href="#02-read-1-HiraginoSansGB-W3-35" x="89.699982"/>
     <use xlink:href="#02-read-1-HiraginoSansGB-W3-39" x="155.399979"/>
     <use xlink:href="#02-read-1-HiraginoSansGB-W3-34" x="221.099976"/>
     <use xlink:href="#02-read-1-HiraginoSansGB-W3-35" x="287.199966"/>
    </g>
   </g>
   <g id="02-read-1-text_37">
    <!-- 0.5934 -->
    <g style="fill: #008477" transform="translate(339.461738 574.630305) scale(0.1 -0.1)">
     <use xlink:href="#02-read-1-HiraginoSansGB-W3-30"/>
     <use xlink:href="#02-read-1-HiraginoSansGB-W3-2e" x="65.699997"/>
     <use xlink:href="#02-read-1-HiraginoSansGB-W3-35" x="89.699982"/>
     <use xlink:href="#02-read-1-HiraginoSansGB-W3-39" x="155.399979"/>
     <use xlink:href="#02-read-1-HiraginoSansGB-W3-33" x="221.099976"/>
     <use xlink:href="#02-read-1-HiraginoSansGB-W3-34" x="286.799973"/>
    </g>
   </g>
   <g id="02-read-1-text_38">
    <!-- P 侧 -->
    <g style="fill: #192c46" transform="translate(304.08538 42.149543) scale(0.15 -0.15)">
     <defs>
      <path id="02-read-1-HiraginoSansGB-W3-4fa7" d="M 1299 3846 
C 1498 4288 1568 4480 1702 4896 
C 1760 5094 1779 5139 1811 5216 
L 1395 5350 
C 1376 5178 1325 4979 1222 4678 
C 954 3891 589 3238 90 2643 
C 218 2515 282 2432 352 2298 
C 589 2611 762 2906 934 3277 
C 915 2566 915 2566 915 2298 
L 915 333 
C 915 -32 902 -256 877 -474 
L 1325 -474 
C 1306 -250 1299 -64 1299 339 
L 1299 3846 
z
M 4851 1043 
C 4826 1203 4819 1395 4819 1715 
L 4819 4205 
C 4819 4531 4826 4698 4858 4877 
L 4390 4877 
C 4416 4691 4422 4512 4422 4205 
L 4422 1715 
C 4422 1389 4416 1222 4384 1043 
L 4851 1043 
z
M 2336 1050 
C 2330 1197 2323 1363 2323 1626 
L 2323 4730 
L 3584 4730 
L 3584 1645 
C 3584 1440 3571 1171 3558 1050 
L 3981 1050 
C 3968 1235 3962 1427 3962 1658 
L 3962 4506 
C 3962 4736 3968 4947 3981 5094 
C 3840 5075 3693 5069 3405 5069 
L 2483 5069 
C 2208 5069 2099 5075 1920 5088 
C 1939 4909 1946 4736 1946 4486 
L 1946 1626 
C 1946 1389 1939 1210 1926 1050 
L 2336 1050 
z
M 2989 557 
C 3322 282 3610 -32 3891 -403 
L 4160 -102 
C 3968 141 3744 365 3219 832 
L 2989 557 
z
M 5440 5184 
C 5466 4973 5478 4774 5478 4435 
L 5478 147 
C 5478 6 5427 -19 5165 -19 
C 4992 -19 4826 -6 4525 32 
C 4582 -102 4602 -179 4627 -371 
C 4851 -384 4934 -384 5152 -384 
C 5734 -384 5875 -288 5875 102 
L 5875 4435 
C 5875 4787 5888 5011 5914 5184 
L 5440 5184 
z
M 2726 4262 
C 2752 4032 2758 3910 2758 3437 
L 2758 2381 
C 2758 1466 2701 1082 2522 742 
C 2336 403 2099 166 1619 -154 
C 1779 -275 1811 -320 1914 -480 
C 2394 -115 2630 128 2816 442 
C 3072 864 3142 1299 3142 2381 
L 3142 3450 
C 3142 3802 3149 4058 3168 4262 
L 2726 4262 
z
" transform="scale(0.015625)"/>
     </defs>
     <use xlink:href="#02-read-1-HiraginoSansGB-W3-50"/>
     <use xlink:href="#02-read-1-HiraginoSansGB-W3-20" x="66.199997"/>
     <use xlink:href="#02-read-1-HiraginoSansGB-W3-4fa7" x="99.499985"/>
    </g>
   </g>
   <g id="02-read-1-legend_1">
    <g id="02-read-1-patch_28">
     <path d="M 218.914364 669.581968 
L 240.914364 669.581968 
L 240.914364 661.881968 
L 218.914364 661.881968 
z
" style="fill: url(#02-read-1-h02a70d68cd); stroke: #467bb5; stroke-width: 0.5; stroke-linejoin: miter"/>
    </g>
    <g id="02-read-1-text_39">
     <!-- TP2 独立池 -->
     <g style="fill: #192c46" transform="translate(249.714364 669.581968) scale(0.11 -0.11)">
      <defs>
       <path id="02-read-1-HiraginoSansGB-W3-54" d="M 2323 4454 
L 3315 4454 
C 3674 4454 3859 4442 4070 4410 
L 4070 4947 
C 3859 4915 3686 4902 3315 4902 
L 819 4902 
C 480 4902 314 4915 90 4947 
L 90 4410 
C 301 4442 499 4454 826 4454 
L 1824 4454 
L 1824 762 
C 1824 461 1811 237 1773 38 
L 2374 38 
C 2336 269 2323 448 2323 762 
L 2323 4454 
z
" transform="scale(0.015625)"/>
       <path id="02-read-1-HiraginoSansGB-W3-72ec" d="M 998 5101 
C 954 5152 928 5184 928 5184 
L 589 4934 
C 851 4717 1018 4499 1203 4134 
C 813 3840 666 3744 224 3533 
C 320 3430 384 3328 454 3187 
C 845 3411 1050 3552 1357 3789 
C 1466 3494 1542 3238 1594 2944 
C 1248 2355 851 1939 243 1542 
C 333 1459 403 1350 467 1203 
C 1037 1632 1376 1984 1670 2438 
C 1702 2125 1709 1907 1709 1542 
C 1709 902 1658 384 1568 154 
C 1517 6 1440 -51 1312 -51 
C 1139 -51 883 6 595 102 
C 640 -64 653 -166 659 -333 
C 934 -397 1139 -429 1325 -429 
C 1574 -429 1715 -371 1830 -230 
C 2010 6 2099 570 2099 1472 
C 2099 2522 1958 3366 1670 4051 
C 1958 4307 2195 4557 2477 4915 
C 2522 4973 2528 4979 2560 5018 
L 2208 5280 
C 2086 5011 1837 4698 1504 4397 
C 1402 4570 1274 4755 1190 4864 
L 998 5101 
z
M 4307 1722 
L 5178 1722 
C 5472 1722 5619 1715 5798 1696 
C 5779 1862 5773 2029 5773 2330 
L 5773 3488 
C 5773 3763 5779 3923 5798 4096 
C 5594 4070 5434 4064 5190 4064 
L 4307 4064 
L 4307 4621 
C 4307 4890 4320 5037 4352 5216 
L 3872 5216 
C 3910 5011 3917 4883 3917 4614 
L 3917 4064 
L 3142 4064 
C 2893 4064 2720 4070 2515 4096 
C 2534 3910 2541 3744 2541 3456 
L 2541 2317 
C 2541 2054 2534 1888 2522 1696 
C 2701 1715 2867 1722 3136 1722 
L 3917 1722 
L 3917 256 
C 3219 173 2637 122 2413 122 
C 2349 122 2323 122 2253 128 
L 2349 -333 
C 2483 -301 2573 -288 2694 -275 
C 4038 -115 4755 -6 5562 160 
C 5638 -19 5677 -134 5779 -474 
L 6157 -282 
C 5869 512 5734 806 5389 1395 
L 5030 1274 
C 5222 934 5280 826 5434 486 
C 5030 403 4678 346 4307 301 
L 4307 1722 
z
M 3917 2086 
L 2925 2086 
L 2925 3699 
L 3917 3699 
L 3917 2086 
z
M 4307 2086 
L 4307 3699 
L 5382 3699 
L 5382 2086 
L 4307 2086 
z
" transform="scale(0.015625)"/>
       <path id="02-read-1-HiraginoSansGB-W3-7acb" d="M 3168 4237 
L 3469 4429 
C 3315 4691 3104 4966 2784 5325 
L 2432 5101 
C 2739 4755 2861 4589 3066 4237 
L 1312 4237 
C 992 4237 781 4250 602 4282 
L 602 3802 
C 806 3827 1018 3840 1312 3840 
L 5062 3840 
C 5370 3840 5587 3827 5798 3802 
L 5798 4282 
C 5600 4250 5382 4237 5062 4237 
L 3168 4237 
z
M 1088 346 
C 774 346 557 358 352 384 
L 352 -102 
C 557 -70 768 -58 1088 -58 
L 5350 -58 
C 5664 -58 5850 -70 6054 -102 
L 6054 384 
C 5862 358 5645 346 5350 346 
L 3872 346 
C 4198 1043 4486 1786 4730 2579 
C 4922 3200 4922 3200 4986 3354 
L 4493 3520 
C 4397 2810 3885 1299 3418 346 
L 1088 346 
z
M 1459 3296 
C 1766 2528 1990 1715 2157 762 
L 2573 922 
C 2368 1971 2189 2611 1882 3450 
L 1459 3296 
z
" transform="scale(0.015625)"/>
       <path id="02-read-1-HiraginoSansGB-W3-6c60" d="M 3872 3520 
L 2874 3181 
L 2874 4038 
C 2874 4301 2886 4499 2912 4672 
L 2432 4672 
C 2458 4474 2470 4288 2470 4019 
L 2470 3046 
L 2330 2995 
C 2131 2931 1971 2886 1779 2861 
L 1901 2413 
C 2016 2470 2157 2528 2426 2624 
L 2470 2637 
L 2470 326 
C 2470 77 2534 -38 2720 -134 
C 2918 -230 3290 -262 4160 -262 
C 5165 -262 5568 -211 5754 -58 
C 5926 83 6022 358 6112 986 
C 5939 1024 5869 1050 5702 1146 
C 5658 614 5587 352 5472 256 
C 5331 154 4992 115 4134 115 
C 3533 115 3155 134 3034 179 
C 2912 218 2874 275 2874 422 
L 2874 2778 
L 3872 3123 
L 3872 1504 
C 3872 1242 3859 1069 3834 883 
L 4307 883 
C 4282 1056 4269 1229 4269 1498 
L 4269 3258 
L 5408 3654 
C 5395 2739 5344 2035 5280 1914 
C 5242 1837 5158 1798 5024 1798 
C 4877 1798 4736 1818 4525 1869 
C 4582 1709 4608 1619 4634 1466 
C 4794 1440 4902 1434 5069 1434 
C 5261 1434 5344 1453 5446 1504 
C 5555 1562 5638 1709 5683 1907 
C 5728 2150 5779 2970 5811 4205 
C 5690 4147 5523 4077 5312 4006 
L 4269 3654 
L 4269 4685 
C 4269 4941 4282 5114 4307 5293 
L 3834 5293 
C 3859 5107 3872 4934 3872 4691 
L 3872 3520 
z
M 608 4934 
C 1030 4646 1222 4480 1600 4064 
L 1875 4378 
C 1523 4736 1274 4947 870 5222 
L 608 4934 
z
M 269 3354 
C 736 3066 992 2867 1350 2509 
L 1594 2854 
C 1254 3162 979 3360 499 3654 
L 269 3354 
z
M 1331 1869 
C 1056 1005 698 294 378 -19 
L 749 -352 
C 813 -243 832 -211 877 -134 
C 986 45 1030 128 1094 250 
C 1299 640 1466 1043 1658 1613 
L 1331 1869 
z
" transform="scale(0.015625)"/>
      </defs>
      <use xlink:href="#02-read-1-HiraginoSansGB-W3-54"/>
      <use xlink:href="#02-read-1-HiraginoSansGB-W3-50" x="64.899994"/>
      <use xlink:href="#02-read-1-HiraginoSansGB-W3-32" x="131.099991"/>
      <use xlink:href="#02-read-1-HiraginoSansGB-W3-20" x="196.799988"/>
      <use xlink:href="#02-read-1-HiraginoSansGB-W3-72ec" x="230.099976"/>
      <use xlink:href="#02-read-1-HiraginoSansGB-W3-7acb" x="330.09996"/>
      <use xlink:href="#02-read-1-HiraginoSansGB-W3-6c60" x="430.099945"/>
     </g>
    </g>
    <g id="02-read-1-patch_29">
     <path d="M 218.914364 685.751968 
L 240.914364 685.751968 
L 240.914364 678.051968 
L 218.914364 678.051968 
z
" style="fill: #467bb5; stroke: #467bb5; stroke-width: 0.5; stroke-linejoin: miter"/>
    </g>
    <g id="02-read-1-text_40">
     <!-- TP2 共享池 -->
     <g style="fill: #192c46" transform="translate(249.714364 685.751968) scale(0.11 -0.11)">
      <defs>
       <path id="02-read-1-HiraginoSansGB-W3-5171" d="M 1920 3987 
L 1299 3987 
C 1043 3987 838 4000 653 4026 
L 653 3571 
C 826 3597 1005 3603 1299 3603 
L 1920 3603 
L 1920 1984 
L 1114 1984 
C 826 1984 563 1997 371 2029 
L 371 1549 
C 563 1574 838 1594 1114 1594 
L 5267 1594 
C 5555 1594 5843 1574 6035 1549 
L 6035 2029 
C 5862 2003 5555 1984 5267 1984 
L 4435 1984 
L 4435 3603 
L 5120 3603 
C 5427 3603 5638 3590 5798 3571 
L 5798 4026 
C 5619 4000 5389 3987 5120 3987 
L 4435 3987 
L 4435 4576 
C 4435 4838 4448 5043 4480 5235 
L 3974 5235 
C 4000 5088 4013 4909 4013 4570 
L 4013 3987 
L 2336 3987 
L 2336 4582 
C 2336 4864 2349 5094 2374 5242 
L 1875 5242 
C 1907 5082 1920 4877 1920 4582 
L 1920 3987 
z
M 2336 1984 
L 2336 3603 
L 4013 3603 
L 4013 1984 
L 2336 1984 
z
M 3731 1030 
C 4109 762 4179 710 4691 320 
C 5158 -32 5293 -141 5594 -435 
L 5882 -96 
C 5344 358 4666 864 4000 1306 
L 3731 1030 
z
M 2240 1293 
C 1965 915 1792 736 1434 467 
C 1126 243 896 122 397 -83 
C 544 -211 602 -275 698 -435 
C 1082 -256 1299 -134 1574 64 
C 1978 358 2234 602 2605 1056 
L 2240 1293 
z
" transform="scale(0.015625)"/>
       <path id="02-read-1-HiraginoSansGB-W3-4eab" d="M 3437 4794 
C 3270 5056 3142 5222 2925 5453 
L 2560 5261 
C 2714 5101 2816 4979 2950 4794 
L 1075 4794 
C 800 4794 621 4806 435 4832 
L 435 4410 
C 608 4429 826 4442 1069 4442 
L 5376 4442 
C 5626 4442 5830 4429 6010 4410 
L 6010 4832 
C 5824 4806 5651 4794 5376 4794 
L 3437 4794 
z
M 3021 1242 
L 966 1242 
C 666 1242 493 1254 333 1280 
L 333 858 
C 518 883 704 896 973 896 
L 3021 896 
L 3021 90 
C 3021 -45 2957 -90 2739 -90 
C 2470 -90 2195 -64 1850 -13 
C 1907 -154 1933 -256 1952 -410 
C 2246 -435 2515 -448 2790 -448 
C 3277 -448 3430 -326 3430 45 
L 3430 896 
L 5440 896 
C 5709 896 5894 883 6074 858 
L 6074 1280 
C 5907 1254 5734 1242 5440 1242 
L 3430 1242 
L 3430 1370 
C 4198 1574 4691 1786 5261 2150 
L 5062 2381 
C 4966 2362 4800 2355 4525 2355 
L 1600 2355 
C 1363 2355 1184 2362 992 2387 
L 992 1984 
C 1165 2010 1331 2016 1587 2016 
L 3731 2016 
C 4000 2016 4198 2022 4493 2042 
C 4090 1850 3795 1747 3283 1613 
L 3002 1613 
C 3014 1542 3021 1440 3021 1267 
L 3021 1242 
z
M 1229 3142 
C 1229 3027 1222 2867 1210 2752 
C 1370 2765 1530 2771 1741 2771 
L 4614 2771 
C 4858 2771 4998 2765 5171 2752 
C 5158 2854 5152 2957 5152 3117 
L 5152 3686 
C 5152 3840 5158 3942 5171 4051 
C 5030 4038 4928 4032 4608 4032 
L 1760 4032 
C 1542 4032 1382 4038 1210 4051 
C 1229 3904 1229 3853 1229 3674 
L 1229 3142 
z
M 1619 3706 
L 4762 3706 
L 4762 3098 
L 1619 3098 
L 1619 3706 
z
" transform="scale(0.015625)"/>
      </defs>
      <use xlink:href="#02-read-1-HiraginoSansGB-W3-54"/>
      <use xlink:href="#02-read-1-HiraginoSansGB-W3-50" x="64.899994"/>
      <use xlink:href="#02-read-1-HiraginoSansGB-W3-32" x="131.099991"/>
      <use xlink:href="#02-read-1-HiraginoSansGB-W3-20" x="196.799988"/>
      <use xlink:href="#02-read-1-HiraginoSansGB-W3-5171" x="230.099976"/>
      <use xlink:href="#02-read-1-HiraginoSansGB-W3-4eab" x="330.09996"/>
      <use xlink:href="#02-read-1-HiraginoSansGB-W3-6c60" x="430.099945"/>
     </g>
    </g>
    <g id="02-read-1-patch_30">
     <path d="M 330.026396 669.581968 
L 352.026396 669.581968 
L 352.026396 661.881968 
L 330.026396 661.881968 
z
" style="fill: url(#02-read-1-hf8fe4700db); stroke: #008477; stroke-width: 0.5; stroke-linejoin: miter"/>
    </g>
    <g id="02-read-1-text_41">
     <!-- TP4 独立池 -->
     <g style="fill: #192c46" transform="translate(360.826396 669.581968) scale(0.11 -0.11)">
      <use xlink:href="#02-read-1-HiraginoSansGB-W3-54"/>
      <use xlink:href="#02-read-1-HiraginoSansGB-W3-50" x="64.899994"/>
      <use xlink:href="#02-read-1-HiraginoSansGB-W3-34" x="131.099991"/>
      <use xlink:href="#02-read-1-HiraginoSansGB-W3-20" x="197.199982"/>
      <use xlink:href="#02-read-1-HiraginoSansGB-W3-72ec" x="230.499969"/>
      <use xlink:href="#02-read-1-HiraginoSansGB-W3-7acb" x="330.499954"/>
      <use xlink:href="#02-read-1-HiraginoSansGB-W3-6c60" x="430.499939"/>
     </g>
    </g>
    <g id="02-read-1-patch_31">
     <path d="M 330.026396 685.751968 
L 352.026396 685.751968 
L 352.026396 678.051968 
L 330.026396 678.051968 
z
" style="fill: #008477; stroke: #008477; stroke-width: 0.5; stroke-linejoin: miter"/>
    </g>
    <g id="02-read-1-text_42">
     <!-- TP4 共享池 -->
     <g style="fill: #192c46" transform="translate(360.826396 685.751968) scale(0.11 -0.11)">
      <use xlink:href="#02-read-1-HiraginoSansGB-W3-54"/>
      <use xlink:href="#02-read-1-HiraginoSansGB-W3-50" x="64.899994"/>
      <use xlink:href="#02-read-1-HiraginoSansGB-W3-34" x="131.099991"/>
      <use xlink:href="#02-read-1-HiraginoSansGB-W3-20" x="197.199982"/>
      <use xlink:href="#02-read-1-HiraginoSansGB-W3-5171" x="230.499969"/>
      <use xlink:href="#02-read-1-HiraginoSansGB-W3-4eab" x="330.499954"/>
      <use xlink:href="#02-read-1-HiraginoSansGB-W3-6c60" x="430.499939"/>
     </g>
    </g>
   </g>
  </g>
  <g id="02-read-1-axes_2">
   <g id="02-read-1-patch_32">
    <path d="M 641.26376 596.428736 
L 1037.99952 596.428736 
L 1037.99952 48.149543 
L 641.26376 48.149543 
z
" style="fill: #ffffff"/>
   </g>
   <g id="02-read-1-matplotlib.axis_3">
    <g id="02-read-1-xtick_7">
     <g id="02-read-1-line2d_19">
      <path d="M 641.26376 596.428736 
L 641.26376 48.149543 
" clip-path="url(#02-read-1-pf7902ebff6)" style="fill: none; stroke: #e9edf2; stroke-width: 0.8; stroke-linecap: square"/>
     </g>
     <g id="02-read-1-line2d_20"/>
     <g id="02-read-1-text_43">
      <!-- 0.0 -->
      <g style="fill: #405570" transform="translate(631.939385 613.619986) scale(0.12 -0.12)">
       <use xlink:href="#02-read-1-HiraginoSansGB-W3-30"/>
       <use xlink:href="#02-read-1-HiraginoSansGB-W3-2e" x="65.699997"/>
       <use xlink:href="#02-read-1-HiraginoSansGB-W3-30" x="89.699982"/>
      </g>
     </g>
    </g>
    <g id="02-read-1-xtick_8">
     <g id="02-read-1-line2d_21">
      <path d="M 713.397535 596.428736 
L 713.397535 48.149543 
" clip-path="url(#02-read-1-pf7902ebff6)" style="fill: none; stroke: #e9edf2; stroke-width: 0.8; stroke-linecap: square"/>
     </g>
     <g id="02-read-1-line2d_22"/>
     <g id="02-read-1-text_44">
      <!-- 0.2 -->
      <g style="fill: #405570" transform="translate(704.07316 613.619986) scale(0.12 -0.12)">
       <use xlink:href="#02-read-1-HiraginoSansGB-W3-30"/>
       <use xlink:href="#02-read-1-HiraginoSansGB-W3-2e" x="65.699997"/>
       <use xlink:href="#02-read-1-HiraginoSansGB-W3-32" x="89.699982"/>
      </g>
     </g>
    </g>
    <g id="02-read-1-xtick_9">
     <g id="02-read-1-line2d_23">
      <path d="M 785.531309 596.428736 
L 785.531309 48.149543 
" clip-path="url(#02-read-1-pf7902ebff6)" style="fill: none; stroke: #e9edf2; stroke-width: 0.8; stroke-linecap: square"/>
     </g>
     <g id="02-read-1-line2d_24"/>
     <g id="02-read-1-text_45">
      <!-- 0.4 -->
      <g style="fill: #405570" transform="translate(776.183497 613.619986) scale(0.12 -0.12)">
       <use xlink:href="#02-read-1-HiraginoSansGB-W3-30"/>
       <use xlink:href="#02-read-1-HiraginoSansGB-W3-2e" x="65.699997"/>
       <use xlink:href="#02-read-1-HiraginoSansGB-W3-34" x="89.699982"/>
      </g>
     </g>
    </g>
    <g id="02-read-1-xtick_10">
     <g id="02-read-1-line2d_25">
      <path d="M 857.665084 596.428736 
L 857.665084 48.149543 
" clip-path="url(#02-read-1-pf7902ebff6)" style="fill: none; stroke: #e9edf2; stroke-width: 0.8; stroke-linecap: square"/>
     </g>
     <g id="02-read-1-line2d_26"/>
     <g id="02-read-1-text_46">
      <!-- 0.6 -->
      <g style="fill: #405570" transform="translate(848.340709 613.619986) scale(0.12 -0.12)">
       <use xlink:href="#02-read-1-HiraginoSansGB-W3-30"/>
       <use xlink:href="#02-read-1-HiraginoSansGB-W3-2e" x="65.699997"/>
       <use xlink:href="#02-read-1-HiraginoSansGB-W3-36" x="89.699982"/>
      </g>
     </g>
    </g>
    <g id="02-read-1-xtick_11">
     <g id="02-read-1-line2d_27">
      <path d="M 929.798858 596.428736 
L 929.798858 48.149543 
" clip-path="url(#02-read-1-pf7902ebff6)" style="fill: none; stroke: #e9edf2; stroke-width: 0.8; stroke-linecap: square"/>
     </g>
     <g id="02-read-1-line2d_28"/>
     <g id="02-read-1-text_47">
      <!-- 0.8 -->
      <g style="fill: #405570" transform="translate(920.474483 613.619986) scale(0.12 -0.12)">
       <use xlink:href="#02-read-1-HiraginoSansGB-W3-30"/>
       <use xlink:href="#02-read-1-HiraginoSansGB-W3-2e" x="65.699997"/>
       <use xlink:href="#02-read-1-HiraginoSansGB-W3-38" x="89.699982"/>
      </g>
     </g>
    </g>
    <g id="02-read-1-xtick_12">
     <g id="02-read-1-line2d_29">
      <path d="M 1001.932633 596.428736 
L 1001.932633 48.149543 
" clip-path="url(#02-read-1-pf7902ebff6)" style="fill: none; stroke: #e9edf2; stroke-width: 0.8; stroke-linecap: square"/>
     </g>
     <g id="02-read-1-line2d_30"/>
     <g id="02-read-1-text_48">
      <!-- 1.0 -->
      <g style="fill: #405570" transform="translate(992.608258 613.619986) scale(0.12 -0.12)">
       <use xlink:href="#02-read-1-HiraginoSansGB-W3-31"/>
       <use xlink:href="#02-read-1-HiraginoSansGB-W3-2e" x="65.699997"/>
       <use xlink:href="#02-read-1-HiraginoSansGB-W3-30" x="89.699982"/>
      </g>
     </g>
    </g>
    <g id="02-read-1-text_49">
     <!-- 组 P50（ms） · 越低越好 -->
     <g style="fill: #405570" transform="translate(764.973827 629.259986) scale(0.12 -0.12)">
      <use xlink:href="#02-read-1-HiraginoSansGB-W3-7ec4"/>
      <use xlink:href="#02-read-1-HiraginoSansGB-W3-20" x="99.999985"/>
      <use xlink:href="#02-read-1-HiraginoSansGB-W3-50" x="133.299973"/>
      <use xlink:href="#02-read-1-HiraginoSansGB-W3-35" x="199.499969"/>
      <use xlink:href="#02-read-1-HiraginoSansGB-W3-30" x="265.199966"/>
      <use xlink:href="#02-read-1-HiraginoSansGB-W3-ff08" x="330.899963"/>
      <use xlink:href="#02-read-1-HiraginoSansGB-W3-6d" x="430.899948"/>
      <use xlink:href="#02-read-1-HiraginoSansGB-W3-73" x="523.999939"/>
      <use xlink:href="#02-read-1-HiraginoSansGB-W3-ff09" x="577.699936"/>
      <use xlink:href="#02-read-1-HiraginoSansGB-W3-20" x="677.699921"/>
      <use xlink:href="#02-read-1-HiraginoSansGB-W3-b7" x="710.999908"/>
      <use xlink:href="#02-read-1-HiraginoSansGB-W3-20" x="810.999893"/>
      <use xlink:href="#02-read-1-HiraginoSansGB-W3-8d8a" x="844.299881"/>
      <use xlink:href="#02-read-1-HiraginoSansGB-W3-4f4e" x="944.299866"/>
      <use xlink:href="#02-read-1-HiraginoSansGB-W3-8d8a" x="1044.29985"/>
      <use xlink:href="#02-read-1-HiraginoSansGB-W3-597d" x="1144.299835"/>
     </g>
    </g>
   </g>
   <g id="02-read-1-matplotlib.axis_4">
    <g id="02-read-1-ytick_7">
     <g id="02-read-1-line2d_31"/>
     <g id="02-read-1-text_50">
      <!-- P 来源 / 连续 -->
      <g style="fill: #243a56" transform="translate(559.64376 117.629115) scale(0.12 -0.12)">
       <use xlink:href="#02-read-1-HiraginoSansGB-W3-50"/>
       <use xlink:href="#02-read-1-HiraginoSansGB-W3-20" x="66.199997"/>
       <use xlink:href="#02-read-1-HiraginoSansGB-W3-6765" x="99.499985"/>
       <use xlink:href="#02-read-1-HiraginoSansGB-W3-6e90" x="199.499969"/>
       <use xlink:href="#02-read-1-HiraginoSansGB-W3-20" x="299.499954"/>
       <use xlink:href="#02-read-1-HiraginoSansGB-W3-2f" x="332.799942"/>
       <use xlink:href="#02-read-1-HiraginoSansGB-W3-20" x="380.199936"/>
       <use xlink:href="#02-read-1-HiraginoSansGB-W3-8fde" x="413.499924"/>
       <use xlink:href="#02-read-1-HiraginoSansGB-W3-7eed" x="513.499908"/>
      </g>
     </g>
    </g>
    <g id="02-read-1-ytick_8">
     <g id="02-read-1-line2d_32"/>
     <g id="02-read-1-text_51">
      <!-- P 来源 / 离散 -->
      <g style="fill: #243a56" transform="translate(559.64376 203.97792) scale(0.12 -0.12)">
       <use xlink:href="#02-read-1-HiraginoSansGB-W3-50"/>
       <use xlink:href="#02-read-1-HiraginoSansGB-W3-20" x="66.199997"/>
       <use xlink:href="#02-read-1-HiraginoSansGB-W3-6765" x="99.499985"/>
       <use xlink:href="#02-read-1-HiraginoSansGB-W3-6e90" x="199.499969"/>
       <use xlink:href="#02-read-1-HiraginoSansGB-W3-20" x="299.499954"/>
       <use xlink:href="#02-read-1-HiraginoSansGB-W3-2f" x="332.799942"/>
       <use xlink:href="#02-read-1-HiraginoSansGB-W3-20" x="380.199936"/>
       <use xlink:href="#02-read-1-HiraginoSansGB-W3-79bb" x="413.499924"/>
       <use xlink:href="#02-read-1-HiraginoSansGB-W3-6563" x="513.499908"/>
      </g>
     </g>
    </g>
    <g id="02-read-1-ytick_9">
     <g id="02-read-1-line2d_33"/>
     <g id="02-read-1-text_52">
      <!-- D 来源 / 连续 -->
      <g style="fill: #243a56" transform="translate(558.49251 290.315475) scale(0.12 -0.12)">
       <use xlink:href="#02-read-1-HiraginoSansGB-W3-44"/>
       <use xlink:href="#02-read-1-HiraginoSansGB-W3-20" x="75.799988"/>
       <use xlink:href="#02-read-1-HiraginoSansGB-W3-6765" x="109.099976"/>
       <use xlink:href="#02-read-1-HiraginoSansGB-W3-6e90" x="209.09996"/>
       <use xlink:href="#02-read-1-HiraginoSansGB-W3-20" x="309.099945"/>
       <use xlink:href="#02-read-1-HiraginoSansGB-W3-2f" x="342.399933"/>
       <use xlink:href="#02-read-1-HiraginoSansGB-W3-20" x="389.799927"/>
       <use xlink:href="#02-read-1-HiraginoSansGB-W3-8fde" x="423.099915"/>
       <use xlink:href="#02-read-1-HiraginoSansGB-W3-7eed" x="523.099899"/>
      </g>
     </g>
    </g>
    <g id="02-read-1-ytick_10">
     <g id="02-read-1-line2d_34"/>
     <g id="02-read-1-text_53">
      <!-- D 来源 / 离散 -->
      <g style="fill: #243a56" transform="translate(558.49251 376.66428) scale(0.12 -0.12)">
       <use xlink:href="#02-read-1-HiraginoSansGB-W3-44"/>
       <use xlink:href="#02-read-1-HiraginoSansGB-W3-20" x="75.799988"/>
       <use xlink:href="#02-read-1-HiraginoSansGB-W3-6765" x="109.099976"/>
       <use xlink:href="#02-read-1-HiraginoSansGB-W3-6e90" x="209.09996"/>
       <use xlink:href="#02-read-1-HiraginoSansGB-W3-20" x="309.099945"/>
       <use xlink:href="#02-read-1-HiraginoSansGB-W3-2f" x="342.399933"/>
       <use xlink:href="#02-read-1-HiraginoSansGB-W3-20" x="389.799927"/>
       <use xlink:href="#02-read-1-HiraginoSansGB-W3-79bb" x="423.099915"/>
       <use xlink:href="#02-read-1-HiraginoSansGB-W3-6563" x="523.099899"/>
      </g>
     </g>
    </g>
    <g id="02-read-1-ytick_11">
     <g id="02-read-1-line2d_35"/>
     <g id="02-read-1-text_54">
      <!-- MIXED 来源 / 连续 -->
      <g style="fill: #243a56" transform="translate(527.78376 463.001835) scale(0.12 -0.12)">
       <use xlink:href="#02-read-1-HiraginoSansGB-W3-4d"/>
       <use xlink:href="#02-read-1-HiraginoSansGB-W3-49" x="94.499985"/>
       <use xlink:href="#02-read-1-HiraginoSansGB-W3-58" x="119.399979"/>
       <use xlink:href="#02-read-1-HiraginoSansGB-W3-45" x="191.59996"/>
       <use xlink:href="#02-read-1-HiraginoSansGB-W3-44" x="255.899948"/>
       <use xlink:href="#02-read-1-HiraginoSansGB-W3-20" x="331.699936"/>
       <use xlink:href="#02-read-1-HiraginoSansGB-W3-6765" x="364.999924"/>
       <use xlink:href="#02-read-1-HiraginoSansGB-W3-6e90" x="464.999908"/>
       <use xlink:href="#02-read-1-HiraginoSansGB-W3-20" x="564.999893"/>
       <use xlink:href="#02-read-1-HiraginoSansGB-W3-2f" x="598.299881"/>
       <use xlink:href="#02-read-1-HiraginoSansGB-W3-20" x="645.699875"/>
       <use xlink:href="#02-read-1-HiraginoSansGB-W3-8fde" x="678.999863"/>
       <use xlink:href="#02-read-1-HiraginoSansGB-W3-7eed" x="778.999847"/>
      </g>
     </g>
    </g>
    <g id="02-read-1-ytick_12">
     <g id="02-read-1-line2d_36"/>
     <g id="02-read-1-text_55">
      <!-- MIXED 来源 / 离散 -->
      <g style="fill: #243a56" transform="translate(527.78376 549.35064) scale(0.12 -0.12)">
       <use xlink:href="#02-read-1-HiraginoSansGB-W3-4d"/>
       <use xlink:href="#02-read-1-HiraginoSansGB-W3-49" x="94.499985"/>
       <use xlink:href="#02-read-1-HiraginoSansGB-W3-58" x="119.399979"/>
       <use xlink:href="#02-read-1-HiraginoSansGB-W3-45" x="191.59996"/>
       <use xlink:href="#02-read-1-HiraginoSansGB-W3-44" x="255.899948"/>
       <use xlink:href="#02-read-1-HiraginoSansGB-W3-20" x="331.699936"/>
       <use xlink:href="#02-read-1-HiraginoSansGB-W3-6765" x="364.999924"/>
       <use xlink:href="#02-read-1-HiraginoSansGB-W3-6e90" x="464.999908"/>
       <use xlink:href="#02-read-1-HiraginoSansGB-W3-20" x="564.999893"/>
       <use xlink:href="#02-read-1-HiraginoSansGB-W3-2f" x="598.299881"/>
       <use xlink:href="#02-read-1-HiraginoSansGB-W3-20" x="645.699875"/>
       <use xlink:href="#02-read-1-HiraginoSansGB-W3-79bb" x="678.999863"/>
       <use xlink:href="#02-read-1-HiraginoSansGB-W3-6563" x="778.999847"/>
      </g>
     </g>
    </g>
   </g>
   <g id="02-read-1-patch_33">
    <path d="M 641.26376 78.153798 
L 943.612476 78.153798 
L 943.612476 93.263854 
L 641.26376 93.263854 
z
" clip-path="url(#02-read-1-pf7902ebff6)" style="fill: url(#02-read-1-h02a70d68cd); stroke: #467bb5; stroke-width: 0.5; stroke-linejoin: miter"/>
   </g>
   <g id="02-read-1-patch_34">
    <path d="M 641.26376 164.496978 
L 944.802683 164.496978 
L 944.802683 179.607034 
L 641.26376 179.607034 
z
" clip-path="url(#02-read-1-pf7902ebff6)" style="fill: url(#02-read-1-h02a70d68cd); stroke: #467bb5; stroke-width: 0.5; stroke-linejoin: miter"/>
   </g>
   <g id="02-read-1-patch_35">
    <path d="M 641.26376 250.840158 
L 767.894601 250.840158 
L 767.894601 265.950214 
L 641.26376 265.950214 
z
" clip-path="url(#02-read-1-pf7902ebff6)" style="fill: url(#02-read-1-h02a70d68cd); stroke: #467bb5; stroke-width: 0.5; stroke-linejoin: miter"/>
   </g>
   <g id="02-read-1-patch_36">
    <path d="M 641.26376 337.183338 
L 769.265143 337.183338 
L 769.265143 352.293394 
L 641.26376 352.293394 
z
" clip-path="url(#02-read-1-pf7902ebff6)" style="fill: url(#02-read-1-h02a70d68cd); stroke: #467bb5; stroke-width: 0.5; stroke-linejoin: miter"/>
   </g>
   <g id="02-read-1-patch_37">
    <path d="M 641.26376 423.526518 
L 856.510943 423.526518 
L 856.510943 438.636574 
L 641.26376 438.636574 
z
" clip-path="url(#02-read-1-pf7902ebff6)" style="fill: url(#02-read-1-h02a70d68cd); stroke: #467bb5; stroke-width: 0.5; stroke-linejoin: miter"/>
   </g>
   <g id="02-read-1-patch_38">
    <path d="M 641.26376 509.869698 
L 855.681405 509.869698 
L 855.681405 524.979754 
L 641.26376 524.979754 
z
" clip-path="url(#02-read-1-pf7902ebff6)" style="fill: url(#02-read-1-h02a70d68cd); stroke: #467bb5; stroke-width: 0.5; stroke-linejoin: miter"/>
   </g>
   <g id="02-read-1-patch_39">
    <path d="M 641.26376 96.285865 
L 944.802683 96.285865 
L 944.802683 111.395922 
L 641.26376 111.395922 
z
" clip-path="url(#02-read-1-pf7902ebff6)" style="fill: #467bb5; stroke: #467bb5; stroke-width: 0.5; stroke-linejoin: miter"/>
   </g>
   <g id="02-read-1-patch_40">
    <path d="M 641.26376 182.629045 
L 942.566536 182.629045 
L 942.566536 197.739102 
L 641.26376 197.739102 
z
" clip-path="url(#02-read-1-pf7902ebff6)" style="fill: #467bb5; stroke: #467bb5; stroke-width: 0.5; stroke-linejoin: miter"/>
   </g>
   <g id="02-read-1-patch_41">
    <path d="M 641.26376 268.972226 
L 768.183136 268.972226 
L 768.183136 284.082282 
L 641.26376 284.082282 
z
" clip-path="url(#02-read-1-pf7902ebff6)" style="fill: #467bb5; stroke: #467bb5; stroke-width: 0.5; stroke-linejoin: miter"/>
   </g>
   <g id="02-read-1-patch_42">
    <path d="M 641.26376 355.315406 
L 769.914347 355.315406 
L 769.914347 370.425462 
L 641.26376 370.425462 
z
" clip-path="url(#02-read-1-pf7902ebff6)" style="fill: #467bb5; stroke: #467bb5; stroke-width: 0.5; stroke-linejoin: miter"/>
   </g>
   <g id="02-read-1-patch_43">
    <path d="M 641.26376 441.658586 
L 855.320736 441.658586 
L 855.320736 456.768642 
L 641.26376 456.768642 
z
" clip-path="url(#02-read-1-pf7902ebff6)" style="fill: #467bb5; stroke: #467bb5; stroke-width: 0.5; stroke-linejoin: miter"/>
   </g>
   <g id="02-read-1-patch_44">
    <path d="M 641.26376 528.001766 
L 855.789605 528.001766 
L 855.789605 543.111822 
L 641.26376 543.111822 
z
" clip-path="url(#02-read-1-pf7902ebff6)" style="fill: #467bb5; stroke: #467bb5; stroke-width: 0.5; stroke-linejoin: miter"/>
   </g>
   <g id="02-read-1-patch_45">
    <path d="M 641.26376 114.417933 
L 943.396075 114.417933 
L 943.396075 129.52799 
L 641.26376 129.52799 
z
" clip-path="url(#02-read-1-pf7902ebff6)" style="fill: url(#02-read-1-hf8fe4700db); stroke: #008477; stroke-width: 0.5; stroke-linejoin: miter"/>
   </g>
   <g id="02-read-1-patch_46">
    <path d="M 641.26376 200.761113 
L 942.999339 200.761113 
L 942.999339 215.87117 
L 641.26376 215.87117 
z
" clip-path="url(#02-read-1-pf7902ebff6)" style="fill: url(#02-read-1-hf8fe4700db); stroke: #008477; stroke-width: 0.5; stroke-linejoin: miter"/>
   </g>
   <g id="02-read-1-patch_47">
    <path d="M 641.26376 287.104293 
L 770.816019 287.104293 
L 770.816019 302.21435 
L 641.26376 302.21435 
z
" clip-path="url(#02-read-1-pf7902ebff6)" style="fill: url(#02-read-1-hf8fe4700db); stroke: #008477; stroke-width: 0.5; stroke-linejoin: miter"/>
   </g>
   <g id="02-read-1-patch_48">
    <path d="M 641.26376 373.447473 
L 770.311083 373.447473 
L 770.311083 388.55753 
L 641.26376 388.55753 
z
" clip-path="url(#02-read-1-pf7902ebff6)" style="fill: url(#02-read-1-hf8fe4700db); stroke: #008477; stroke-width: 0.5; stroke-linejoin: miter"/>
   </g>
   <g id="02-read-1-patch_49">
    <path d="M 641.26376 459.790654 
L 855.39287 459.790654 
L 855.39287 474.90071 
L 641.26376 474.90071 
z
" clip-path="url(#02-read-1-pf7902ebff6)" style="fill: url(#02-read-1-hf8fe4700db); stroke: #008477; stroke-width: 0.5; stroke-linejoin: miter"/>
   </g>
   <g id="02-read-1-patch_50">
    <path d="M 641.26376 546.133834 
L 855.428937 546.133834 
L 855.428937 561.24389 
L 641.26376 561.24389 
z
" clip-path="url(#02-read-1-pf7902ebff6)" style="fill: url(#02-read-1-hf8fe4700db); stroke: #008477; stroke-width: 0.5; stroke-linejoin: miter"/>
   </g>
   <g id="02-read-1-patch_51">
    <path d="M 641.26376 132.550001 
L 944.081346 132.550001 
L 944.081346 147.660058 
L 641.26376 147.660058 
z
" clip-path="url(#02-read-1-pf7902ebff6)" style="fill: #008477; stroke: #008477; stroke-width: 0.5; stroke-linejoin: miter"/>
   </g>
   <g id="02-read-1-patch_52">
    <path d="M 641.26376 218.893181 
L 942.963272 218.893181 
L 942.963272 234.003238 
L 641.26376 234.003238 
z
" clip-path="url(#02-read-1-pf7902ebff6)" style="fill: #008477; stroke: #008477; stroke-width: 0.5; stroke-linejoin: miter"/>
   </g>
   <g id="02-read-1-patch_53">
    <path d="M 641.26376 305.236361 
L 770.779952 305.236361 
L 770.779952 320.346418 
L 641.26376 320.346418 
z
" clip-path="url(#02-read-1-pf7902ebff6)" style="fill: #008477; stroke: #008477; stroke-width: 0.5; stroke-linejoin: miter"/>
   </g>
   <g id="02-read-1-patch_54">
    <path d="M 641.26376 391.579541 
L 768.327404 391.579541 
L 768.327404 406.689598 
L 641.26376 406.689598 
z
" clip-path="url(#02-read-1-pf7902ebff6)" style="fill: #008477; stroke: #008477; stroke-width: 0.5; stroke-linejoin: miter"/>
   </g>
   <g id="02-read-1-patch_55">
    <path d="M 641.26376 477.922721 
L 856.438809 477.922721 
L 856.438809 493.032778 
L 641.26376 493.032778 
z
" clip-path="url(#02-read-1-pf7902ebff6)" style="fill: #008477; stroke: #008477; stroke-width: 0.5; stroke-linejoin: miter"/>
   </g>
   <g id="02-read-1-patch_56">
    <path d="M 641.26376 564.265901 
L 855.032201 564.265901 
L 855.032201 579.375958 
L 641.26376 579.375958 
z
" clip-path="url(#02-read-1-pf7902ebff6)" style="fill: #008477; stroke: #008477; stroke-width: 0.5; stroke-linejoin: miter"/>
   </g>
   <g id="02-read-1-patch_57">
    <path d="M 641.26376 596.428736 
L 1037.99952 596.428736 
" style="fill: none; stroke: #d7e0eb; stroke-width: 0.8; stroke-linejoin: miter; stroke-linecap: square"/>
   </g>
   <g id="02-read-1-text_56">
    <!-- 0.8383 -->
    <g style="fill: #405570" transform="translate(948.373305 88.518201) scale(0.1 -0.1)">
     <use xlink:href="#02-read-1-HiraginoSansGB-W3-30"/>
     <use xlink:href="#02-read-1-HiraginoSansGB-W3-2e" x="65.699997"/>
     <use xlink:href="#02-read-1-HiraginoSansGB-W3-38" x="89.699982"/>
     <use xlink:href="#02-read-1-HiraginoSansGB-W3-33" x="155.399979"/>
     <use xlink:href="#02-read-1-HiraginoSansGB-W3-38" x="221.099976"/>
     <use xlink:href="#02-read-1-HiraginoSansGB-W3-33" x="286.799973"/>
    </g>
   </g>
   <g id="02-read-1-text_57">
    <!-- 0.8416 -->
    <g style="fill: #405570" transform="translate(949.563512 174.861381) scale(0.1 -0.1)">
     <use xlink:href="#02-read-1-HiraginoSansGB-W3-30"/>
     <use xlink:href="#02-read-1-HiraginoSansGB-W3-2e" x="65.699997"/>
     <use xlink:href="#02-read-1-HiraginoSansGB-W3-38" x="89.699982"/>
     <use xlink:href="#02-read-1-HiraginoSansGB-W3-34" x="155.399979"/>
     <use xlink:href="#02-read-1-HiraginoSansGB-W3-31" x="221.499969"/>
     <use xlink:href="#02-read-1-HiraginoSansGB-W3-36" x="287.199966"/>
    </g>
   </g>
   <g id="02-read-1-text_58">
    <!-- 0.3511 -->
    <g style="fill: #405570" transform="translate(772.65543 261.204561) scale(0.1 -0.1)">
     <use xlink:href="#02-read-1-HiraginoSansGB-W3-30"/>
     <use xlink:href="#02-read-1-HiraginoSansGB-W3-2e" x="65.699997"/>
     <use xlink:href="#02-read-1-HiraginoSansGB-W3-33" x="89.699982"/>
     <use xlink:href="#02-read-1-HiraginoSansGB-W3-35" x="155.399979"/>
     <use xlink:href="#02-read-1-HiraginoSansGB-W3-31" x="221.099976"/>
     <use xlink:href="#02-read-1-HiraginoSansGB-W3-31" x="286.799973"/>
    </g>
   </g>
   <g id="02-read-1-text_59">
    <!-- 0.3549 -->
    <g style="fill: #405570" transform="translate(774.025972 347.547741) scale(0.1 -0.1)">
     <use xlink:href="#02-read-1-HiraginoSansGB-W3-30"/>
     <use xlink:href="#02-read-1-HiraginoSansGB-W3-2e" x="65.699997"/>
     <use xlink:href="#02-read-1-HiraginoSansGB-W3-33" x="89.699982"/>
     <use xlink:href="#02-read-1-HiraginoSansGB-W3-35" x="155.399979"/>
     <use xlink:href="#02-read-1-HiraginoSansGB-W3-34" x="221.099976"/>
     <use xlink:href="#02-read-1-HiraginoSansGB-W3-39" x="287.199966"/>
    </g>
   </g>
   <g id="02-read-1-text_60">
    <!-- 0.5968 -->
    <g style="fill: #405570" transform="translate(861.271772 433.890921) scale(0.1 -0.1)">
     <use xlink:href="#02-read-1-HiraginoSansGB-W3-30"/>
     <use xlink:href="#02-read-1-HiraginoSansGB-W3-2e" x="65.699997"/>
     <use xlink:href="#02-read-1-HiraginoSansGB-W3-35" x="89.699982"/>
     <use xlink:href="#02-read-1-HiraginoSansGB-W3-39" x="155.399979"/>
     <use xlink:href="#02-read-1-HiraginoSansGB-W3-36" x="221.099976"/>
     <use xlink:href="#02-read-1-HiraginoSansGB-W3-38" x="286.799973"/>
    </g>
   </g>
   <g id="02-read-1-text_61">
    <!-- 0.5945 -->
    <g style="fill: #405570" transform="translate(860.442234 520.234101) scale(0.1 -0.1)">
     <use xlink:href="#02-read-1-HiraginoSansGB-W3-30"/>
     <use xlink:href="#02-read-1-HiraginoSansGB-W3-2e" x="65.699997"/>
     <use xlink:href="#02-read-1-HiraginoSansGB-W3-35" x="89.699982"/>
     <use xlink:href="#02-read-1-HiraginoSansGB-W3-39" x="155.399979"/>
     <use xlink:href="#02-read-1-HiraginoSansGB-W3-34" x="221.099976"/>
     <use xlink:href="#02-read-1-HiraginoSansGB-W3-35" x="287.199966"/>
    </g>
   </g>
   <g id="02-read-1-text_62">
    <!-- 0.8416 -->
    <g style="fill: #467bb5" transform="translate(949.563512 106.650269) scale(0.1 -0.1)">
     <use xlink:href="#02-read-1-HiraginoSansGB-W3-30"/>
     <use xlink:href="#02-read-1-HiraginoSansGB-W3-2e" x="65.699997"/>
     <use xlink:href="#02-read-1-HiraginoSansGB-W3-38" x="89.699982"/>
     <use xlink:href="#02-read-1-HiraginoSansGB-W3-34" x="155.399979"/>
     <use xlink:href="#02-read-1-HiraginoSansGB-W3-31" x="221.499969"/>
     <use xlink:href="#02-read-1-HiraginoSansGB-W3-36" x="287.199966"/>
    </g>
   </g>
   <g id="02-read-1-text_63">
    <!-- 0.8354 -->
    <g style="fill: #467bb5" transform="translate(947.327365 192.993449) scale(0.1 -0.1)">
     <use xlink:href="#02-read-1-HiraginoSansGB-W3-30"/>
     <use xlink:href="#02-read-1-HiraginoSansGB-W3-2e" x="65.699997"/>
     <use xlink:href="#02-read-1-HiraginoSansGB-W3-38" x="89.699982"/>
     <use xlink:href="#02-read-1-HiraginoSansGB-W3-33" x="155.399979"/>
     <use xlink:href="#02-read-1-HiraginoSansGB-W3-35" x="221.099976"/>
     <use xlink:href="#02-read-1-HiraginoSansGB-W3-34" x="286.799973"/>
    </g>
   </g>
   <g id="02-read-1-text_64">
    <!-- 0.3519 -->
    <g style="fill: #467bb5" transform="translate(772.943965 279.336629) scale(0.1 -0.1)">
     <use xlink:href="#02-read-1-HiraginoSansGB-W3-30"/>
     <use xlink:href="#02-read-1-HiraginoSansGB-W3-2e" x="65.699997"/>
     <use xlink:href="#02-read-1-HiraginoSansGB-W3-33" x="89.699982"/>
     <use xlink:href="#02-read-1-HiraginoSansGB-W3-35" x="155.399979"/>
     <use xlink:href="#02-read-1-HiraginoSansGB-W3-31" x="221.099976"/>
     <use xlink:href="#02-read-1-HiraginoSansGB-W3-39" x="286.799973"/>
    </g>
   </g>
   <g id="02-read-1-text_65">
    <!-- 0.3567 -->
    <g style="fill: #467bb5" transform="translate(774.675176 365.679809) scale(0.1 -0.1)">
     <use xlink:href="#02-read-1-HiraginoSansGB-W3-30"/>
     <use xlink:href="#02-read-1-HiraginoSansGB-W3-2e" x="65.699997"/>
     <use xlink:href="#02-read-1-HiraginoSansGB-W3-33" x="89.699982"/>
     <use xlink:href="#02-read-1-HiraginoSansGB-W3-35" x="155.399979"/>
     <use xlink:href="#02-read-1-HiraginoSansGB-W3-36" x="221.099976"/>
     <use xlink:href="#02-read-1-HiraginoSansGB-W3-37" x="286.799973"/>
    </g>
   </g>
   <g id="02-read-1-text_66">
    <!-- 0.5935 -->
    <g style="fill: #467bb5" transform="translate(860.081565 452.022989) scale(0.1 -0.1)">
     <use xlink:href="#02-read-1-HiraginoSansGB-W3-30"/>
     <use xlink:href="#02-read-1-HiraginoSansGB-W3-2e" x="65.699997"/>
     <use xlink:href="#02-read-1-HiraginoSansGB-W3-35" x="89.699982"/>
     <use xlink:href="#02-read-1-HiraginoSansGB-W3-39" x="155.399979"/>
     <use xlink:href="#02-read-1-HiraginoSansGB-W3-33" x="221.099976"/>
     <use xlink:href="#02-read-1-HiraginoSansGB-W3-35" x="286.799973"/>
    </g>
   </g>
   <g id="02-read-1-text_67">
    <!-- 0.5948 -->
    <g style="fill: #467bb5" transform="translate(860.550435 538.366169) scale(0.1 -0.1)">
     <use xlink:href="#02-read-1-HiraginoSansGB-W3-30"/>
     <use xlink:href="#02-read-1-HiraginoSansGB-W3-2e" x="65.699997"/>
     <use xlink:href="#02-read-1-HiraginoSansGB-W3-35" x="89.699982"/>
     <use xlink:href="#02-read-1-HiraginoSansGB-W3-39" x="155.399979"/>
     <use xlink:href="#02-read-1-HiraginoSansGB-W3-34" x="221.099976"/>
     <use xlink:href="#02-read-1-HiraginoSansGB-W3-38" x="287.199966"/>
    </g>
   </g>
   <g id="02-read-1-text_68">
    <!-- 0.8377 -->
    <g style="fill: #405570" transform="translate(948.156904 124.782336) scale(0.1 -0.1)">
     <use xlink:href="#02-read-1-HiraginoSansGB-W3-30"/>
     <use xlink:href="#02-read-1-HiraginoSansGB-W3-2e" x="65.699997"/>
     <use xlink:href="#02-read-1-HiraginoSansGB-W3-38" x="89.699982"/>
     <use xlink:href="#02-read-1-HiraginoSansGB-W3-33" x="155.399979"/>
     <use xlink:href="#02-read-1-HiraginoSansGB-W3-37" x="221.099976"/>
     <use xlink:href="#02-read-1-HiraginoSansGB-W3-37" x="286.799973"/>
    </g>
   </g>
   <g id="02-read-1-text_69">
    <!-- 0.8366 -->
    <g style="fill: #405570" transform="translate(947.760168 211.125517) scale(0.1 -0.1)">
     <use xlink:href="#02-read-1-HiraginoSansGB-W3-30"/>
     <use xlink:href="#02-read-1-HiraginoSansGB-W3-2e" x="65.699997"/>
     <use xlink:href="#02-read-1-HiraginoSansGB-W3-38" x="89.699982"/>
     <use xlink:href="#02-read-1-HiraginoSansGB-W3-33" x="155.399979"/>
     <use xlink:href="#02-read-1-HiraginoSansGB-W3-36" x="221.099976"/>
     <use xlink:href="#02-read-1-HiraginoSansGB-W3-36" x="286.799973"/>
    </g>
   </g>
   <g id="02-read-1-text_70">
    <!-- 0.3592 -->
    <g style="fill: #405570" transform="translate(775.576848 297.468697) scale(0.1 -0.1)">
     <use xlink:href="#02-read-1-HiraginoSansGB-W3-30"/>
     <use xlink:href="#02-read-1-HiraginoSansGB-W3-2e" x="65.699997"/>
     <use xlink:href="#02-read-1-HiraginoSansGB-W3-33" x="89.699982"/>
     <use xlink:href="#02-read-1-HiraginoSansGB-W3-35" x="155.399979"/>
     <use xlink:href="#02-read-1-HiraginoSansGB-W3-39" x="221.099976"/>
     <use xlink:href="#02-read-1-HiraginoSansGB-W3-32" x="286.799973"/>
    </g>
   </g>
   <g id="02-read-1-text_71">
    <!-- 0.3578 -->
    <g style="fill: #405570" transform="translate(775.071912 383.811877) scale(0.1 -0.1)">
     <use xlink:href="#02-read-1-HiraginoSansGB-W3-30"/>
     <use xlink:href="#02-read-1-HiraginoSansGB-W3-2e" x="65.699997"/>
     <use xlink:href="#02-read-1-HiraginoSansGB-W3-33" x="89.699982"/>
     <use xlink:href="#02-read-1-HiraginoSansGB-W3-35" x="155.399979"/>
     <use xlink:href="#02-read-1-HiraginoSansGB-W3-37" x="221.099976"/>
     <use xlink:href="#02-read-1-HiraginoSansGB-W3-38" x="286.799973"/>
    </g>
   </g>
   <g id="02-read-1-text_72">
    <!-- 0.5937 -->
    <g style="fill: #405570" transform="translate(860.153699 470.155057) scale(0.1 -0.1)">
     <use xlink:href="#02-read-1-HiraginoSansGB-W3-30"/>
     <use xlink:href="#02-read-1-HiraginoSansGB-W3-2e" x="65.699997"/>
     <use xlink:href="#02-read-1-HiraginoSansGB-W3-35" x="89.699982"/>
     <use xlink:href="#02-read-1-HiraginoSansGB-W3-39" x="155.399979"/>
     <use xlink:href="#02-read-1-HiraginoSansGB-W3-33" x="221.099976"/>
     <use xlink:href="#02-read-1-HiraginoSansGB-W3-37" x="286.799973"/>
    </g>
   </g>
   <g id="02-read-1-text_73">
    <!-- 0.5938 -->
    <g style="fill: #405570" transform="translate(860.189766 556.498237) scale(0.1 -0.1)">
     <use xlink:href="#02-read-1-HiraginoSansGB-W3-30"/>
     <use xlink:href="#02-read-1-HiraginoSansGB-W3-2e" x="65.699997"/>
     <use xlink:href="#02-read-1-HiraginoSansGB-W3-35" x="89.699982"/>
     <use xlink:href="#02-read-1-HiraginoSansGB-W3-39" x="155.399979"/>
     <use xlink:href="#02-read-1-HiraginoSansGB-W3-33" x="221.099976"/>
     <use xlink:href="#02-read-1-HiraginoSansGB-W3-38" x="286.799973"/>
    </g>
   </g>
   <g id="02-read-1-text_74">
    <!-- 0.8396 -->
    <g style="fill: #008477" transform="translate(948.842175 142.914404) scale(0.1 -0.1)">
     <use xlink:href="#02-read-1-HiraginoSansGB-W3-30"/>
     <use xlink:href="#02-read-1-HiraginoSansGB-W3-2e" x="65.699997"/>
     <use xlink:href="#02-read-1-HiraginoSansGB-W3-38" x="89.699982"/>
     <use xlink:href="#02-read-1-HiraginoSansGB-W3-33" x="155.399979"/>
     <use xlink:href="#02-read-1-HiraginoSansGB-W3-39" x="221.099976"/>
     <use xlink:href="#02-read-1-HiraginoSansGB-W3-36" x="286.799973"/>
    </g>
   </g>
   <g id="02-read-1-text_75">
    <!-- 0.8365 -->
    <g style="fill: #008477" transform="translate(947.724101 229.257584) scale(0.1 -0.1)">
     <use xlink:href="#02-read-1-HiraginoSansGB-W3-30"/>
     <use xlink:href="#02-read-1-HiraginoSansGB-W3-2e" x="65.699997"/>
     <use xlink:href="#02-read-1-HiraginoSansGB-W3-38" x="89.699982"/>
     <use xlink:href="#02-read-1-HiraginoSansGB-W3-33" x="155.399979"/>
     <use xlink:href="#02-read-1-HiraginoSansGB-W3-36" x="221.099976"/>
     <use xlink:href="#02-read-1-HiraginoSansGB-W3-35" x="286.799973"/>
    </g>
   </g>
   <g id="02-read-1-text_76">
    <!-- 0.3591 -->
    <g style="fill: #008477" transform="translate(775.540781 315.600764) scale(0.1 -0.1)">
     <use xlink:href="#02-read-1-HiraginoSansGB-W3-30"/>
     <use xlink:href="#02-read-1-HiraginoSansGB-W3-2e" x="65.699997"/>
     <use xlink:href="#02-read-1-HiraginoSansGB-W3-33" x="89.699982"/>
     <use xlink:href="#02-read-1-HiraginoSansGB-W3-35" x="155.399979"/>
     <use xlink:href="#02-read-1-HiraginoSansGB-W3-39" x="221.099976"/>
     <use xlink:href="#02-read-1-HiraginoSansGB-W3-31" x="286.799973"/>
    </g>
   </g>
   <g id="02-read-1-text_77">
    <!-- 0.3523 -->
    <g style="fill: #008477" transform="translate(773.088233 401.943945) scale(0.1 -0.1)">
     <use xlink:href="#02-read-1-HiraginoSansGB-W3-30"/>
     <use xlink:href="#02-read-1-HiraginoSansGB-W3-2e" x="65.699997"/>
     <use xlink:href="#02-read-1-HiraginoSansGB-W3-33" x="89.699982"/>
     <use xlink:href="#02-read-1-HiraginoSansGB-W3-35" x="155.399979"/>
     <use xlink:href="#02-read-1-HiraginoSansGB-W3-32" x="221.099976"/>
     <use xlink:href="#02-read-1-HiraginoSansGB-W3-33" x="286.799973"/>
    </g>
   </g>
   <g id="02-read-1-text_78">
    <!-- 0.5966 -->
    <g style="fill: #008477" transform="translate(861.199639 488.287125) scale(0.1 -0.1)">
     <use xlink:href="#02-read-1-HiraginoSansGB-W3-30"/>
     <use xlink:href="#02-read-1-HiraginoSansGB-W3-2e" x="65.699997"/>
     <use xlink:href="#02-read-1-HiraginoSansGB-W3-35" x="89.699982"/>
     <use xlink:href="#02-read-1-HiraginoSansGB-W3-39" x="155.399979"/>
     <use xlink:href="#02-read-1-HiraginoSansGB-W3-36" x="221.099976"/>
     <use xlink:href="#02-read-1-HiraginoSansGB-W3-36" x="286.799973"/>
    </g>
   </g>
   <g id="02-read-1-text_79">
    <!-- 0.5927 -->
    <g style="fill: #008477" transform="translate(859.79303 574.630305) scale(0.1 -0.1)">
     <use xlink:href="#02-read-1-HiraginoSansGB-W3-30"/>
     <use xlink:href="#02-read-1-HiraginoSansGB-W3-2e" x="65.699997"/>
     <use xlink:href="#02-read-1-HiraginoSansGB-W3-35" x="89.699982"/>
     <use xlink:href="#02-read-1-HiraginoSansGB-W3-39" x="155.399979"/>
     <use xlink:href="#02-read-1-HiraginoSansGB-W3-32" x="221.099976"/>
     <use xlink:href="#02-read-1-HiraginoSansGB-W3-37" x="286.799973"/>
    </g>
   </g>
   <g id="02-read-1-text_80">
    <!-- D 侧 -->
    <g style="fill: #192c46" transform="translate(823.949609 42.149543) scale(0.15 -0.15)">
     <use xlink:href="#02-read-1-HiraginoSansGB-W3-44"/>
     <use xlink:href="#02-read-1-HiraginoSansGB-W3-20" x="75.799988"/>
     <use xlink:href="#02-read-1-HiraginoSansGB-W3-4fa7" x="109.099976"/>
    </g>
   </g>
   <g id="02-read-1-legend_2">
    <g id="02-read-1-patch_58">
     <path d="M 739.498124 669.581968 
L 761.498124 669.581968 
L 761.498124 661.881968 
L 739.498124 661.881968 
z
" style="fill: url(#02-read-1-h02a70d68cd); stroke: #467bb5; stroke-width: 0.5; stroke-linejoin: miter"/>
    </g>
    <g id="02-read-1-text_81">
     <!-- TP2 独立池 -->
     <g style="fill: #192c46" transform="translate(770.298124 669.581968) scale(0.11 -0.11)">
      <use xlink:href="#02-read-1-HiraginoSansGB-W3-54"/>
      <use xlink:href="#02-read-1-HiraginoSansGB-W3-50" x="64.899994"/>
      <use xlink:href="#02-read-1-HiraginoSansGB-W3-32" x="131.099991"/>
      <use xlink:href="#02-read-1-HiraginoSansGB-W3-20" x="196.799988"/>
      <use xlink:href="#02-read-1-HiraginoSansGB-W3-72ec" x="230.099976"/>
      <use xlink:href="#02-read-1-HiraginoSansGB-W3-7acb" x="330.09996"/>
      <use xlink:href="#02-read-1-HiraginoSansGB-W3-6c60" x="430.099945"/>
     </g>
    </g>
    <g id="02-read-1-patch_59">
     <path d="M 739.498124 685.751968 
L 761.498124 685.751968 
L 761.498124 678.051968 
L 739.498124 678.051968 
z
" style="fill: #467bb5; stroke: #467bb5; stroke-width: 0.5; stroke-linejoin: miter"/>
    </g>
    <g id="02-read-1-text_82">
     <!-- TP2 共享池 -->
     <g style="fill: #192c46" transform="translate(770.298124 685.751968) scale(0.11 -0.11)">
      <use xlink:href="#02-read-1-HiraginoSansGB-W3-54"/>
      <use xlink:href="#02-read-1-HiraginoSansGB-W3-50" x="64.899994"/>
      <use xlink:href="#02-read-1-HiraginoSansGB-W3-32" x="131.099991"/>
      <use xlink:href="#02-read-1-HiraginoSansGB-W3-20" x="196.799988"/>
      <use xlink:href="#02-read-1-HiraginoSansGB-W3-5171" x="230.099976"/>
      <use xlink:href="#02-read-1-HiraginoSansGB-W3-4eab" x="330.09996"/>
      <use xlink:href="#02-read-1-HiraginoSansGB-W3-6c60" x="430.099945"/>
     </g>
    </g>
    <g id="02-read-1-patch_60">
     <path d="M 850.610156 669.581968 
L 872.610156 669.581968 
L 872.610156 661.881968 
L 850.610156 661.881968 
z
" style="fill: url(#02-read-1-hf8fe4700db); stroke: #008477; stroke-width: 0.5; stroke-linejoin: miter"/>
    </g>
    <g id="02-read-1-text_83">
     <!-- TP4 独立池 -->
     <g style="fill: #192c46" transform="translate(881.410156 669.581968) scale(0.11 -0.11)">
      <use xlink:href="#02-read-1-HiraginoSansGB-W3-54"/>
      <use xlink:href="#02-read-1-HiraginoSansGB-W3-50" x="64.899994"/>
      <use xlink:href="#02-read-1-HiraginoSansGB-W3-34" x="131.099991"/>
      <use xlink:href="#02-read-1-HiraginoSansGB-W3-20" x="197.199982"/>
      <use xlink:href="#02-read-1-HiraginoSansGB-W3-72ec" x="230.499969"/>
      <use xlink:href="#02-read-1-HiraginoSansGB-W3-7acb" x="330.499954"/>
      <use xlink:href="#02-read-1-HiraginoSansGB-W3-6c60" x="430.499939"/>
     </g>
    </g>
    <g id="02-read-1-patch_61">
     <path d="M 850.610156 685.751968 
L 872.610156 685.751968 
L 872.610156 678.051968 
L 850.610156 678.051968 
z
" style="fill: #008477; stroke: #008477; stroke-width: 0.5; stroke-linejoin: miter"/>
    </g>
    <g id="02-read-1-text_84">
     <!-- TP4 共享池 -->
     <g style="fill: #192c46" transform="translate(881.410156 685.751968) scale(0.11 -0.11)">
      <use xlink:href="#02-read-1-HiraginoSansGB-W3-54"/>
      <use xlink:href="#02-read-1-HiraginoSansGB-W3-50" x="64.899994"/>
      <use xlink:href="#02-read-1-HiraginoSansGB-W3-34" x="131.099991"/>
      <use xlink:href="#02-read-1-HiraginoSansGB-W3-20" x="197.199982"/>
      <use xlink:href="#02-read-1-HiraginoSansGB-W3-5171" x="230.499969"/>
      <use xlink:href="#02-read-1-HiraginoSansGB-W3-4eab" x="330.499954"/>
      <use xlink:href="#02-read-1-HiraginoSansGB-W3-6c60" x="430.499939"/>
     </g>
    </g>
   </g>
  </g>
  <g id="02-read-1-text_85">
   <!-- D · 1 请求：两种 TP 数量、两种池模式的耗时接近 -->
   <g style="fill: #192c46" transform="translate(312.081322 20.986875) scale(0.18 -0.18)">
    <defs>
     <path id="02-read-1-HiraginoSansGB-W3-8bf7" d="M 3936 4531 
L 3936 4070 
L 3213 4070 
C 2938 4070 2784 4077 2624 4102 
L 2624 3718 
C 2810 3744 2957 3750 3206 3750 
L 3936 3750 
L 3936 3283 
L 2778 3283 
C 2464 3283 2285 3296 2125 3322 
L 2125 2912 
C 2310 2944 2477 2950 2778 2950 
L 5517 2950 
C 5805 2950 5978 2938 6157 2912 
L 6157 3322 
C 5984 3296 5811 3283 5517 3283 
L 4301 3283 
L 4301 3750 
L 5171 3750 
C 5434 3750 5587 3744 5766 3718 
L 5766 4102 
C 5606 4077 5459 4070 5178 4070 
L 4301 4070 
L 4301 4531 
L 5402 4531 
C 5690 4531 5843 4525 6016 4499 
L 6016 4890 
C 5830 4858 5715 4851 5402 4851 
L 4301 4851 
L 4301 4864 
C 4301 5101 4307 5216 4339 5382 
L 3898 5382 
C 3930 5216 3936 5094 3936 4864 
L 3936 4851 
L 2957 4851 
C 2662 4851 2547 4858 2362 4890 
L 2362 4499 
C 2534 4525 2669 4531 2957 4531 
L 3936 4531 
z
M 3136 614 
L 5242 614 
L 5242 154 
C 5242 0 5197 -32 5018 -32 
C 4819 -32 4563 -6 4307 45 
C 4365 -51 4397 -160 4422 -365 
C 4704 -378 4851 -384 5011 -384 
C 5510 -384 5626 -288 5626 109 
L 5626 1862 
C 5626 2189 5632 2381 5651 2547 
C 5523 2522 5370 2515 5062 2515 
L 3328 2515 
C 3066 2515 2912 2522 2733 2541 
C 2746 2368 2752 2278 2752 1984 
L 2752 243 
C 2752 -96 2746 -275 2720 -442 
L 3174 -442 
C 3149 -282 3136 -90 3136 243 
L 3136 614 
z
M 3136 934 
L 3136 1421 
L 5242 1421 
L 5242 934 
L 3136 934 
z
M 3136 1741 
L 3136 2182 
L 5242 2182 
L 5242 1741 
L 3136 1741 
z
M 1184 333 
C 1165 243 1120 179 1018 128 
L 1178 -256 
C 1414 -102 1517 -32 1555 -13 
C 1875 160 1875 160 2150 326 
C 2214 365 2310 422 2432 499 
L 2406 947 
C 2048 710 1907 627 1600 454 
L 1600 2720 
C 1600 3040 1606 3194 1632 3398 
C 1478 3379 1325 3366 1139 3366 
L 813 3366 
C 576 3366 480 3373 320 3398 
L 320 2950 
C 480 2976 576 2982 787 2982 
L 1184 2982 
L 1184 333 
z
M 826 5024 
C 1242 4717 1517 4461 1837 4064 
L 2144 4397 
C 1798 4762 1530 5005 1133 5306 
L 826 5024 
z
" transform="scale(0.015625)"/>
     <path id="02-read-1-HiraginoSansGB-W3-6c42" d="M 2982 4147 
L 1120 4147 
C 826 4147 640 4160 461 4186 
L 461 3744 
C 640 3770 838 3782 1107 3782 
L 2982 3782 
L 2982 192 
C 2982 45 2938 13 2688 13 
C 2438 13 2176 38 1850 90 
C 1914 -77 1933 -166 1952 -333 
C 2214 -358 2496 -371 2739 -371 
C 3008 -371 3130 -352 3219 -301 
C 3334 -230 3392 -96 3392 109 
L 3392 1997 
C 3386 2246 3379 2381 3379 2400 
C 3373 2534 3373 2534 3366 2707 
C 3360 2899 3360 2912 3354 3059 
C 3494 2624 3584 2432 3763 2118 
C 4006 1683 4301 1293 4634 947 
C 5018 557 5280 346 5747 58 
C 5850 218 5933 320 6048 435 
C 5574 685 5306 870 4947 1210 
C 4691 1453 4486 1677 4269 1971 
C 4627 2266 4934 2554 5152 2790 
C 5485 3155 5485 3155 5530 3206 
L 5171 3482 
C 4966 3123 4582 2701 4064 2272 
C 3763 2752 3533 3277 3405 3782 
L 5293 3782 
C 5581 3782 5779 3770 5952 3744 
L 5952 4186 
C 5773 4160 5581 4147 5293 4147 
L 3392 4147 
L 3392 4640 
C 3392 4947 3405 5126 3430 5325 
L 2944 5325 
C 2970 5139 2982 4947 2982 4640 
L 2982 4147 
z
M 3942 5024 
C 4384 4768 4570 4627 4922 4275 
L 5210 4550 
C 4826 4890 4672 5005 4218 5274 
L 3942 5024 
z
M 1069 3187 
C 1434 2829 1651 2554 1939 2061 
L 2278 2330 
C 1965 2797 1779 3027 1382 3430 
L 1069 3187 
z
M 2688 1939 
C 2266 1613 1990 1434 1459 1139 
C 1043 909 794 800 499 717 
L 710 282 
C 819 352 890 397 1024 467 
C 1933 960 2195 1120 2803 1555 
L 2688 1939 
z
" transform="scale(0.015625)"/>
     <path id="02-read-1-HiraginoSansGB-W3-ff1a" d="M 1722 3488 
L 1088 3488 
L 1088 2854 
L 1722 2854 
L 1722 3488 
z
M 1722 38 
L 1722 672 
L 1088 672 
L 1088 38 
L 1722 38 
z
" transform="scale(0.015625)"/>
     <path id="02-read-1-HiraginoSansGB-W3-4e24" d="M 3232 1683 
C 2989 2202 2854 2464 2554 2989 
C 2579 3187 2579 3187 2598 3341 
L 3814 3341 
C 3782 2931 3706 2509 3590 2182 
C 3411 1651 3181 1306 2803 979 
C 2925 870 2995 787 3078 659 
C 3418 1018 3571 1248 3782 1709 
C 3910 1997 4013 2272 4058 2477 
C 4435 1773 4454 1734 4883 787 
L 5210 1030 
C 4986 1491 4602 2214 4358 2637 
C 4211 2880 4211 2880 4147 2995 
C 4166 3162 4166 3219 4179 3341 
L 5254 3341 
L 5254 166 
C 5254 19 5210 -13 4998 -13 
C 4717 -13 4461 13 4083 90 
C 4141 -64 4160 -147 4173 -346 
C 4506 -378 4717 -390 4922 -390 
C 5216 -390 5363 -371 5459 -320 
C 5606 -250 5658 -134 5658 109 
L 5658 3142 
C 5658 3386 5664 3552 5683 3731 
C 5510 3712 5344 3706 5082 3706 
L 4205 3706 
C 4230 4250 4230 4250 4237 4691 
L 5357 4691 
C 5606 4691 5830 4678 6029 4653 
L 6029 5088 
C 5837 5062 5619 5050 5357 5050 
L 1062 5050 
C 787 5050 595 5062 390 5088 
L 390 4653 
C 614 4678 800 4691 1062 4691 
L 2259 4691 
C 2259 4160 2253 3942 2240 3706 
L 1331 3706 
C 1062 3706 909 3712 742 3731 
C 762 3546 768 3347 768 3123 
L 768 250 
C 768 -32 755 -250 730 -454 
L 1210 -454 
C 1184 -269 1171 -45 1171 250 
L 1171 3341 
L 2214 3341 
C 2176 2893 2106 2509 1990 2189 
C 1805 1651 1562 1280 1197 973 
C 1299 896 1370 813 1485 640 
C 1869 1050 2022 1274 2214 1702 
C 2330 1965 2394 2170 2464 2477 
C 2624 2182 2784 1792 2918 1402 
L 3232 1683 
z
M 3859 4691 
L 3859 4467 
C 3859 4179 3859 4179 3840 3706 
L 2624 3706 
C 2643 4090 2643 4115 2656 4691 
L 3859 4691 
z
" transform="scale(0.015625)"/>
     <path id="02-read-1-HiraginoSansGB-W3-79cd" d="M 1357 3686 
L 870 3686 
C 614 3686 474 3693 314 3725 
L 314 3296 
C 474 3322 627 3334 864 3334 
L 1299 3334 
C 1062 2515 685 1824 173 1280 
C 262 1184 333 1075 410 922 
C 646 1216 781 1408 954 1734 
C 1158 2112 1254 2355 1389 2816 
C 1370 2432 1357 2093 1357 1792 
L 1357 288 
C 1357 -19 1344 -230 1318 -454 
L 1773 -454 
C 1747 -250 1734 -38 1734 288 
L 1734 2714 
C 1984 2432 2170 2182 2387 1856 
L 2650 2106 
C 2355 2509 2144 2746 1734 3123 
L 1734 3334 
L 2144 3334 
C 2342 3334 2451 3328 2592 3302 
L 2592 3718 
C 2477 3693 2355 3686 2144 3686 
L 1734 3686 
L 1734 4691 
C 2054 4781 2291 4864 2483 4954 
C 2598 5005 2598 5005 2643 5018 
L 2387 5363 
C 2016 5101 1267 4890 320 4787 
C 422 4646 467 4563 506 4435 
C 934 4506 1088 4538 1357 4595 
L 1357 3686 
z
M 4141 1613 
L 4141 314 
C 4141 0 4128 -275 4102 -467 
L 4576 -467 
C 4550 -301 4538 -13 4538 320 
L 4538 1613 
L 5568 1613 
L 5568 1229 
L 6010 1229 
C 5984 1382 5978 1523 5978 1843 
L 5978 3514 
C 5978 3763 5984 3898 6003 4058 
C 5875 4038 5702 4032 5414 4032 
L 4538 4032 
L 4538 4582 
C 4538 4902 4550 5088 4576 5286 
L 4102 5286 
C 4128 5088 4141 4877 4141 4595 
L 4141 4032 
L 3341 4032 
C 3046 4032 2880 4038 2752 4058 
C 2771 3898 2778 3763 2778 3520 
L 2778 1805 
C 2778 1517 2771 1389 2752 1203 
L 3168 1203 
L 3168 1613 
L 4141 1613 
z
M 4141 3674 
L 4141 1958 
L 3168 1958 
L 3168 3674 
L 4141 3674 
z
M 4538 3674 
L 5568 3674 
L 5568 1958 
L 4538 1958 
L 4538 3674 
z
" transform="scale(0.015625)"/>
     <path id="02-read-1-HiraginoSansGB-W3-6570" d="M 6138 4115 
C 5965 4083 5837 4077 5542 4077 
L 4186 4077 
C 4294 4422 4314 4486 4397 4819 
C 4480 5133 4480 5133 4512 5222 
L 4077 5318 
C 4051 4915 3930 4410 3750 3942 
C 3526 3347 3296 2950 2963 2560 
C 3104 2483 3168 2438 3283 2310 
C 3501 2592 3597 2739 3750 3053 
C 3878 2285 4058 1760 4410 1171 
C 4019 634 3507 230 2752 -134 
C 2861 -243 2938 -339 3014 -493 
C 3731 -102 4211 288 4640 826 
C 5018 307 5478 -102 6061 -442 
C 6144 -262 6195 -173 6298 -45 
C 5690 275 5242 659 4877 1165 
C 5318 1875 5530 2618 5594 3712 
C 5888 3712 5997 3706 6138 3680 
L 6138 4115 
z
M 5197 3712 
C 5165 3136 5126 2867 5005 2451 
C 4909 2099 4813 1850 4640 1536 
C 4448 1875 4314 2195 4186 2643 
C 4096 2957 4096 2957 3994 3571 
C 4000 3590 4006 3597 4026 3642 
C 4032 3661 4038 3680 4051 3712 
L 5197 3712 
z
M 3014 314 
C 2765 474 2714 499 2445 640 
C 2803 1101 2957 1389 3162 2029 
C 2880 2016 2726 2010 2694 2010 
L 1798 2010 
C 1843 2099 1875 2176 1978 2426 
L 1587 2515 
C 1510 2259 1466 2150 1395 2010 
L 832 2010 
C 602 2010 486 2016 371 2042 
L 371 1638 
C 493 1664 614 1670 832 1670 
L 1222 1670 
C 1056 1382 986 1274 762 979 
C 1331 768 1498 698 1818 544 
C 1434 262 966 58 333 -96 
C 429 -218 480 -307 538 -448 
C 1350 -186 1830 51 2176 358 
C 2419 224 2515 160 2790 -38 
L 3014 314 
z
M 2650 1670 
C 2547 1389 2349 1075 2106 806 
C 1786 954 1670 1005 1299 1146 
C 1485 1427 1542 1517 1626 1670 
L 2650 1670 
z
M 2112 4019 
L 2112 4877 
C 2112 5056 2118 5165 2144 5306 
L 1715 5306 
C 1741 5158 1747 5043 1747 4877 
L 1747 4019 
L 915 4019 
C 666 4019 531 4026 397 4051 
L 397 3654 
C 531 3680 666 3686 902 3686 
L 1491 3686 
C 1338 3469 1325 3450 1235 3354 
C 979 3072 736 2893 288 2624 
C 410 2528 480 2451 570 2317 
C 1197 2765 1446 3021 1773 3578 
C 1754 3398 1747 3328 1747 3206 
L 1747 2848 
C 1747 2758 1741 2669 1722 2586 
L 2138 2586 
C 2118 2669 2112 2758 2112 2854 
L 2112 3245 
C 2157 3219 2182 3206 2240 3174 
C 2547 2995 2618 2944 2899 2701 
L 3117 3014 
C 2662 3302 2662 3302 2112 3565 
L 2112 3686 
L 2950 3686 
C 3168 3686 3270 3680 3386 3661 
L 3386 4045 
C 3277 4026 3181 4019 2957 4019 
L 2112 4019 
z
M 589 5030 
C 800 4742 915 4538 1050 4192 
L 1395 4333 
C 1248 4666 1133 4864 915 5178 
L 589 5030 
z
M 2931 5197 
C 2790 4832 2650 4589 2400 4282 
L 2720 4122 
C 2944 4410 3059 4608 3277 5037 
L 2931 5197 
z
" transform="scale(0.015625)"/>
     <path id="02-read-1-HiraginoSansGB-W3-91cf" d="M 3360 1075 
L 4666 1075 
C 4883 1075 5050 1069 5280 1050 
C 5267 1152 5261 1331 5261 1562 
L 5261 2266 
C 5261 2445 5267 2566 5280 2688 
C 5094 2669 4928 2662 4685 2662 
L 1754 2662 
C 1466 2662 1274 2669 1146 2688 
C 1158 2541 1165 2419 1165 2246 
L 1165 1568 
C 1165 1293 1165 1222 1146 1050 
C 1331 1069 1498 1075 1760 1075 
L 2989 1075 
L 2989 710 
L 1408 710 
C 1146 710 1005 717 864 742 
L 864 378 
C 1030 403 1165 410 1421 410 
L 2989 410 
L 2989 32 
L 1018 32 
C 723 32 557 38 365 70 
L 365 -314 
C 550 -288 749 -275 1018 -275 
L 5389 -275 
C 5658 -275 5850 -288 6042 -314 
L 6042 70 
C 5850 38 5683 32 5389 32 
L 3360 32 
L 3360 410 
L 5037 410 
C 5286 410 5421 403 5587 378 
L 5587 742 
C 5440 717 5312 710 5050 710 
L 3360 710 
L 3360 1075 
z
M 2989 2010 
L 1530 2010 
L 1530 2374 
L 2989 2374 
L 2989 2010 
z
M 3360 2010 
L 3360 2374 
L 4896 2374 
L 4896 2010 
L 3360 2010 
z
M 2989 1363 
L 1530 1363 
L 1530 1734 
L 2989 1734 
L 2989 1363 
z
M 3360 1363 
L 3360 1734 
L 4896 1734 
L 4896 1363 
L 3360 1363 
z
M 4742 3629 
C 5114 3629 5222 3622 5382 3610 
C 5363 3776 5357 3910 5357 4096 
L 5357 4678 
C 5357 4877 5363 4998 5382 5139 
C 5203 5120 5043 5114 4762 5114 
L 1664 5114 
C 1389 5114 1216 5120 1030 5139 
C 1050 4992 1056 4870 1056 4678 
L 1056 4096 
C 1056 3872 1050 3750 1030 3610 
C 1184 3622 1306 3629 1664 3629 
L 4742 3629 
z
M 1421 4826 
L 4992 4826 
L 4992 4512 
L 1421 4512 
L 1421 4826 
z
M 1421 4237 
L 4986 4237 
L 4986 3910 
L 1421 3910 
L 1421 4237 
z
M 365 2963 
C 531 2989 685 2995 998 2995 
L 5414 2995 
C 5734 2995 5875 2989 6042 2963 
L 6042 3341 
C 5869 3315 5677 3309 5408 3309 
L 998 3309 
C 730 3309 538 3315 365 3341 
L 365 2963 
z
" transform="scale(0.015625)"/>
     <path id="02-read-1-HiraginoSansGB-W3-3001" d="M 480 973 
C 960 576 1331 173 1741 -422 
L 2099 -83 
C 1664 506 1338 851 813 1274 
L 480 973 
z
" transform="scale(0.015625)"/>
     <path id="02-read-1-HiraginoSansGB-W3-6a21" d="M 5472 966 
C 5741 966 5901 954 6093 928 
L 6093 1338 
C 5907 1312 5728 1306 5466 1306 
L 4109 1306 
C 4134 1453 4141 1542 4160 1798 
L 5094 1798 
C 5344 1798 5517 1792 5690 1773 
C 5664 1933 5664 2048 5664 2304 
L 5664 3328 
C 5664 3565 5670 3693 5690 3846 
C 5536 3827 5363 3821 5101 3821 
L 3040 3821 
C 2771 3821 2592 3827 2451 3846 
C 2464 3699 2470 3552 2470 3334 
L 2470 2304 
C 2470 2093 2464 1946 2451 1773 
C 2650 1792 2842 1798 3034 1798 
L 3776 1798 
C 3750 1453 3750 1446 3725 1306 
L 2714 1306 
C 2419 1306 2221 1312 2067 1331 
L 2067 934 
C 2259 954 2464 966 2707 966 
L 3610 966 
C 3482 685 3309 486 3040 301 
C 2752 102 2394 -26 1824 -154 
C 1939 -288 1984 -371 2042 -506 
C 3046 -237 3635 160 3942 794 
C 4339 147 4960 -256 5946 -512 
C 5997 -358 6054 -256 6150 -122 
C 5197 45 4614 371 4237 966 
L 5472 966 
z
M 2842 3488 
L 5293 3488 
L 5293 2982 
L 2842 2982 
L 2842 3488 
z
M 2842 2656 
L 5293 2656 
L 5293 2125 
L 2842 2125 
L 2842 2656 
z
M 1491 2893 
C 1651 2643 1773 2438 1939 2118 
L 2221 2355 
C 2048 2630 1818 2957 1574 3251 
L 1491 3366 
L 1491 3693 
L 1754 3693 
C 1901 3693 2029 3686 2170 3674 
L 2170 4064 
C 2042 4045 1946 4038 1754 4038 
L 1491 4038 
L 1491 4666 
C 1491 4986 1498 5165 1523 5325 
L 1075 5325 
C 1101 5171 1107 5030 1107 4666 
L 1107 4038 
L 768 4038 
C 563 4038 422 4045 320 4064 
L 320 3667 
C 442 3686 589 3693 768 3693 
L 1056 3693 
C 883 2861 563 2112 160 1587 
C 250 1485 307 1395 390 1210 
C 736 1722 922 2176 1139 3040 
C 1126 2675 1114 2278 1114 2035 
L 1114 218 
C 1114 -115 1101 -346 1075 -525 
L 1530 -525 
C 1498 -352 1491 -128 1491 218 
L 1491 2893 
z
M 3526 4390 
L 4531 4390 
L 4531 4352 
C 4531 4198 4525 4109 4512 3994 
L 4928 3994 
C 4909 4096 4909 4186 4909 4358 
L 4909 4390 
L 5421 4390 
C 5696 4390 5888 4384 6080 4358 
L 6080 4774 
C 5901 4742 5728 4736 5414 4736 
L 4909 4736 
L 4909 4858 
C 4909 5056 4915 5165 4934 5286 
L 4499 5286 
C 4525 5165 4531 5043 4531 4858 
L 4531 4736 
L 3526 4736 
L 3526 4858 
C 3526 5056 3539 5165 3558 5286 
L 3123 5286 
C 3149 5158 3155 5050 3155 4858 
L 3155 4736 
L 2765 4736 
C 2451 4736 2310 4742 2157 4768 
L 2157 4365 
C 2330 4384 2470 4390 2765 4390 
L 3155 4390 
L 3155 4352 
C 3155 4192 3149 4115 3130 3994 
L 3552 3994 
C 3533 4109 3526 4186 3526 4358 
L 3526 4390 
z
" transform="scale(0.015625)"/>
     <path id="02-read-1-HiraginoSansGB-W3-5f0f" d="M 5363 3693 
C 5632 3693 5837 3680 6022 3654 
L 6022 4115 
C 5862 4090 5677 4077 5402 4077 
L 4198 4077 
C 4186 4371 4186 4499 4186 4634 
C 4186 4896 4198 5082 4224 5222 
L 3731 5222 
C 3763 4992 3763 4986 3770 4762 
C 3776 4288 3776 4288 3782 4077 
L 1120 4077 
C 838 4077 640 4090 461 4115 
L 461 3654 
C 678 3680 858 3693 1120 3693 
L 3795 3693 
C 3802 3674 3802 3667 3802 3642 
C 3846 2573 4013 1677 4307 915 
C 4602 154 5043 -352 5421 -352 
C 5581 -352 5715 -262 5830 -83 
C 5952 115 6029 371 6144 915 
C 5978 986 5907 1037 5766 1165 
C 5664 461 5549 115 5408 115 
C 5363 115 5293 160 5203 237 
C 5011 410 4838 704 4672 1146 
C 4531 1510 4448 1830 4352 2349 
C 4275 2810 4256 2963 4218 3693 
L 5363 3693 
z
M 2310 2349 
L 2963 2349 
C 3206 2349 3373 2342 3533 2317 
L 3533 2746 
C 3373 2720 3232 2714 2957 2714 
L 1299 2714 
C 1024 2714 870 2720 717 2746 
L 717 2317 
C 877 2342 1050 2349 1293 2349 
L 1901 2349 
L 1901 544 
C 1082 390 1082 390 883 371 
C 627 346 627 346 499 346 
L 595 -109 
C 672 -90 794 -64 883 -45 
C 1946 147 2803 333 3750 582 
L 3750 973 
C 2835 730 2675 691 2310 621 
L 2310 2349 
z
M 5600 4474 
C 5325 4755 5158 4896 4787 5165 
L 4493 4947 
C 4845 4698 5018 4544 5299 4230 
L 5600 4474 
z
" transform="scale(0.015625)"/>
     <path id="02-read-1-HiraginoSansGB-W3-7684" d="M 1651 4333 
C 1747 4544 1862 4864 1978 5210 
L 1523 5299 
C 1472 4966 1376 4646 1248 4333 
L 1120 4333 
C 800 4333 685 4339 550 4352 
C 570 4192 576 4045 576 3744 
L 576 499 
C 576 122 570 -115 544 -282 
L 973 -282 
L 973 282 
L 2221 282 
L 2221 -128 
L 2643 -128 
C 2624 26 2611 243 2611 531 
L 2611 3789 
C 2611 4019 2618 4186 2630 4352 
C 2573 4346 2387 4339 2067 4333 
L 1651 4333 
z
M 973 3981 
L 2221 3981 
L 2221 2554 
L 973 2554 
L 973 3981 
z
M 973 2202 
L 2221 2202 
L 2221 640 
L 973 640 
L 973 2202 
z
M 3846 4339 
C 3949 4595 4013 4768 4090 4992 
C 4128 5107 4160 5197 4179 5235 
L 3738 5318 
C 3725 5178 3661 4941 3571 4698 
C 3334 4045 3021 3501 2630 3085 
C 2739 3014 2854 2906 2938 2797 
C 3270 3226 3456 3520 3674 3962 
L 5517 3962 
C 5510 3021 5504 2579 5472 2067 
C 5427 1210 5344 595 5235 301 
C 5152 96 5069 38 4832 38 
C 4602 38 4314 70 3923 147 
C 3994 -13 4019 -122 4032 -307 
C 4429 -339 4614 -352 4845 -352 
C 5229 -352 5434 -237 5562 58 
C 5792 570 5875 1427 5933 3962 
C 5939 4198 5939 4250 5958 4365 
C 5830 4346 5651 4339 5395 4339 
L 3846 4339 
z
M 4742 1549 
C 4403 2234 4134 2656 3731 3142 
L 3405 2944 
C 3776 2496 4019 2093 4378 1325 
L 4742 1549 
z
" transform="scale(0.015625)"/>
     <path id="02-read-1-HiraginoSansGB-W3-8017" d="M 1856 1760 
C 2131 1498 2323 1286 2554 992 
L 2797 1254 
C 2445 1658 2227 1862 1856 2150 
L 1856 2362 
L 2291 2362 
C 2534 2362 2650 2355 2810 2330 
L 2810 2733 
C 2669 2707 2554 2701 2310 2701 
L 1856 2701 
L 1856 3309 
L 2170 3309 
C 2381 3309 2509 3302 2662 3283 
L 2662 3667 
C 2528 3648 2406 3642 2189 3642 
L 1856 3642 
L 1856 4237 
L 2259 4237 
C 2515 4237 2618 4230 2784 4205 
L 2784 4608 
C 2630 4582 2522 4576 2278 4576 
L 1856 4576 
L 1856 4800 
C 1856 5037 1862 5184 1888 5344 
L 1440 5344 
C 1466 5184 1478 5018 1478 4794 
L 1478 4576 
L 1030 4576 
C 774 4576 646 4582 486 4608 
L 486 4205 
C 640 4230 768 4237 1018 4237 
L 1478 4237 
L 1478 3642 
L 1114 3642 
C 883 3642 755 3648 614 3667 
L 614 3283 
C 762 3302 896 3309 1114 3309 
L 1478 3309 
L 1478 2701 
L 915 2701 
C 653 2701 531 2707 384 2733 
L 384 2330 
C 544 2355 691 2362 915 2362 
L 1370 2362 
C 1146 1747 742 1139 282 730 
C 390 614 454 518 518 371 
C 1018 928 1203 1222 1510 1965 
C 1485 1626 1478 1408 1478 1114 
L 1478 166 
C 1478 -122 1466 -288 1440 -467 
L 1894 -467 
C 1862 -288 1856 -96 1856 160 
L 1856 1760 
z
M 3904 3347 
L 3469 3283 
C 3232 3245 3053 3226 2931 3226 
C 2912 3226 2899 3226 2874 3226 
L 2925 2790 
C 3072 2835 3277 2880 3507 2918 
L 3904 2976 
L 3904 1965 
L 3392 1888 
C 3136 1850 2982 1830 2867 1830 
C 2848 1830 2829 1830 2803 1830 
L 2861 1395 
C 3002 1440 3226 1491 3443 1523 
L 3904 1594 
L 3904 173 
C 3904 -218 4070 -301 4870 -301 
C 5946 -301 6022 -218 6106 1037 
C 5914 1082 5869 1101 5702 1190 
C 5690 672 5658 397 5594 243 
C 5542 128 5350 83 4870 83 
C 4378 83 4301 109 4301 275 
L 4301 1651 
L 5453 1824 
C 5754 1869 5894 1882 6061 1888 
L 6003 2330 
C 5818 2266 5632 2221 5395 2189 
L 4301 2022 
L 4301 3040 
L 5280 3194 
C 5600 3245 5741 3264 5894 3264 
L 5837 3699 
C 5696 3642 5504 3597 5229 3558 
L 4301 3411 
L 4301 4403 
C 4838 4576 5235 4723 5581 4883 
C 5664 4922 5664 4922 5715 4947 
L 5376 5280 
C 5280 5197 5171 5133 4986 5050 
C 4403 4787 3693 4589 2918 4474 
C 3053 4294 3078 4250 3123 4109 
C 3488 4186 3603 4211 3904 4294 
L 3904 3347 
z
" transform="scale(0.015625)"/>
     <path id="02-read-1-HiraginoSansGB-W3-65f6" d="M 5254 3418 
L 5466 3418 
C 5670 3418 5875 3411 6074 3392 
L 6074 3827 
C 5907 3802 5722 3789 5459 3789 
L 5254 3789 
L 5254 4525 
C 5254 4838 5267 5056 5293 5274 
L 4800 5274 
C 4832 5088 4845 4826 4845 4525 
L 4845 3789 
L 3232 3789 
C 2963 3789 2733 3802 2566 3827 
L 2566 3379 
C 2733 3405 2982 3418 3245 3418 
L 4845 3418 
L 4845 256 
C 4845 115 4787 90 4525 90 
C 4224 90 4038 109 3648 166 
C 3718 6 3750 -90 3770 -269 
C 4064 -294 4288 -307 4550 -307 
C 5120 -307 5254 -205 5254 218 
L 5254 3418 
z
M 2842 2624 
C 3226 2170 3488 1773 3763 1197 
L 4115 1453 
C 3814 2003 3590 2323 3162 2835 
L 2842 2624 
z
M 486 589 
C 486 320 480 128 454 -90 
L 883 -90 
L 883 512 
L 1914 512 
L 1914 147 
L 2330 147 
C 2310 352 2304 480 2304 704 
L 2304 4262 
C 2304 4557 2310 4710 2323 4858 
C 2176 4845 2016 4838 1741 4838 
L 1050 4838 
C 781 4838 602 4845 467 4858 
C 486 4602 486 4499 486 4243 
L 486 589 
z
M 883 4480 
L 1914 4480 
L 1914 2938 
L 883 2938 
L 883 4480 
z
M 883 2579 
L 1914 2579 
L 1914 870 
L 883 870 
L 883 2579 
z
" transform="scale(0.015625)"/>
     <path id="02-read-1-HiraginoSansGB-W3-63a5" d="M 5491 1658 
C 5792 1658 5958 1651 6106 1626 
L 6106 2042 
C 5971 2016 5792 2010 5485 2010 
L 3744 2010 
C 3910 2298 4006 2464 4051 2547 
L 3616 2643 
C 3565 2477 3462 2259 3309 2010 
L 2694 2010 
C 2406 2010 2246 2016 2093 2042 
L 2093 1626 
C 2246 1651 2406 1658 2694 1658 
L 3078 1658 
C 2810 1274 2720 1165 2438 877 
C 3040 704 3251 634 3706 454 
C 3539 352 3251 224 3002 147 
C 2624 19 2298 -45 1779 -90 
C 1888 -230 1933 -307 1990 -467 
C 2899 -320 3578 -77 4109 282 
C 4672 32 4998 -141 5600 -531 
L 5901 -192 
C 5222 192 4954 326 4442 544 
C 4781 858 5011 1203 5178 1658 
L 5491 1658 
z
M 4762 1658 
C 4672 1395 4634 1325 4512 1152 
C 4390 986 4224 813 4070 691 
C 3718 826 3552 883 3053 1037 
C 3245 1267 3296 1325 3520 1658 
L 4762 1658 
z
M 2157 2701 
C 2349 2720 2438 2726 2714 2726 
L 5485 2726 
C 5786 2726 5965 2720 6106 2694 
L 6106 3110 
C 5971 3085 5792 3078 5478 3078 
L 4781 3078 
C 4979 3373 5107 3629 5280 4045 
L 4902 4160 
C 4749 3699 4608 3418 4384 3078 
L 2707 3078 
C 2432 3078 2266 3085 2138 3110 
L 2138 2714 
C 1875 2579 1754 2522 1530 2432 
L 1530 3680 
L 1741 3680 
C 1926 3680 2048 3674 2176 3654 
L 2176 4064 
C 2029 4045 1958 4038 1734 4038 
L 1530 4038 
L 1530 4659 
C 1530 4922 1542 5107 1574 5299 
L 1088 5299 
C 1120 5094 1133 4922 1133 4653 
L 1133 4038 
L 806 4038 
C 621 4038 480 4051 326 4077 
L 326 3654 
C 474 3674 621 3680 800 3680 
L 1133 3680 
L 1133 2278 
C 640 2099 448 2035 243 2010 
L 358 1568 
C 442 1613 550 1658 710 1709 
C 826 1754 928 1792 1018 1830 
C 1088 1856 1088 1856 1133 1875 
L 1133 96 
C 1133 -26 1075 -58 877 -58 
C 742 -58 602 -45 435 -19 
C 499 -160 518 -250 544 -416 
C 640 -429 685 -429 762 -429 
C 1370 -429 1530 -339 1530 26 
L 1530 2035 
C 1741 2125 1856 2176 2163 2330 
L 2157 2701 
z
M 4301 4589 
C 4083 4960 4083 4966 3814 5325 
L 3488 5171 
C 3667 4934 3686 4902 3866 4589 
L 2944 4589 
C 2624 4589 2464 4595 2310 4621 
L 2310 4205 
C 2451 4230 2624 4237 2925 4237 
L 5325 4237 
C 5632 4237 5811 4230 5946 4205 
L 5946 4621 
C 5798 4595 5626 4589 5306 4589 
L 4301 4589 
z
M 2848 4000 
C 3034 3667 3110 3494 3226 3149 
L 3584 3283 
C 3456 3629 3373 3814 3194 4134 
L 2848 4000 
z
" transform="scale(0.015625)"/>
     <path id="02-read-1-HiraginoSansGB-W3-8fd1" d="M 4550 3078 
L 4550 1056 
C 4550 742 4538 563 4512 416 
L 4998 416 
C 4966 570 4954 768 4954 1056 
L 4954 3078 
L 5472 3078 
C 5709 3078 5862 3072 6035 3046 
L 6035 3488 
C 5875 3462 5734 3456 5466 3456 
L 3181 3456 
C 3181 3571 3181 3584 3181 3642 
L 3181 4282 
C 4064 4390 4934 4589 5453 4794 
C 5741 4902 5741 4902 5811 4928 
L 5459 5293 
C 5286 5171 5088 5088 4672 4966 
C 4038 4774 3245 4634 2835 4634 
C 2816 4634 2784 4634 2752 4634 
C 2771 4448 2778 4218 2778 3789 
C 2778 2074 2643 1414 2176 845 
C 2336 736 2374 704 2509 544 
C 2957 1146 3123 1766 3174 3078 
L 4550 3078 
z
M 1299 883 
C 1216 858 1120 794 954 678 
C 806 570 563 339 282 38 
L 563 -294 
C 890 166 1248 512 1402 512 
C 1459 512 1530 474 1581 416 
C 1818 141 1818 141 1926 64 
C 2150 -90 2381 -173 2675 -211 
C 2950 -250 3469 -282 3872 -282 
C 4762 -282 5453 -269 6074 -237 
C 6118 -32 6125 0 6227 230 
C 5670 154 4960 115 4000 115 
C 3232 115 2707 154 2464 230 
C 2195 314 2042 422 1811 691 
C 1754 762 1722 794 1683 832 
L 1683 2246 
C 1683 2534 1690 2701 1715 2893 
C 1555 2867 1440 2861 1235 2861 
L 870 2861 
C 621 2861 518 2867 378 2893 
L 378 2477 
C 525 2502 627 2509 845 2509 
L 1299 2509 
L 1299 883 
z
M 576 4864 
C 954 4474 1133 4237 1478 3680 
L 1798 3968 
C 1459 4467 1261 4723 870 5126 
L 576 4864 
z
" transform="scale(0.015625)"/>
    </defs>
    <use xlink:href="#02-read-1-HiraginoSansGB-W3-44"/>
    <use xlink:href="#02-read-1-HiraginoSansGB-W3-20" x="75.799988"/>
    <use xlink:href="#02-read-1-HiraginoSansGB-W3-b7" x="109.099976"/>
    <use xlink:href="#02-read-1-HiraginoSansGB-W3-20" x="209.09996"/>
    <use xlink:href="#02-read-1-HiraginoSansGB-W3-31" x="242.399948"/>
    <use xlink:href="#02-read-1-HiraginoSansGB-W3-20" x="308.099945"/>
    <use xlink:href="#02-read-1-HiraginoSansGB-W3-8bf7" x="341.399933"/>
    <use xlink:href="#02-read-1-HiraginoSansGB-W3-6c42" x="441.399918"/>
    <use xlink:href="#02-read-1-HiraginoSansGB-W3-ff1a" x="541.399902"/>
    <use xlink:href="#02-read-1-HiraginoSansGB-W3-4e24" x="641.399887"/>
    <use xlink:href="#02-read-1-HiraginoSansGB-W3-79cd" x="741.399872"/>
    <use xlink:href="#02-read-1-HiraginoSansGB-W3-20" x="841.399857"/>
    <use xlink:href="#02-read-1-HiraginoSansGB-W3-54" x="874.699844"/>
    <use xlink:href="#02-read-1-HiraginoSansGB-W3-50" x="939.599838"/>
    <use xlink:href="#02-read-1-HiraginoSansGB-W3-20" x="1005.799835"/>
    <use xlink:href="#02-read-1-HiraginoSansGB-W3-6570" x="1039.099823"/>
    <use xlink:href="#02-read-1-HiraginoSansGB-W3-91cf" x="1139.099808"/>
    <use xlink:href="#02-read-1-HiraginoSansGB-W3-3001" x="1239.099792"/>
    <use xlink:href="#02-read-1-HiraginoSansGB-W3-4e24" x="1339.099777"/>
    <use xlink:href="#02-read-1-HiraginoSansGB-W3-79cd" x="1439.099762"/>
    <use xlink:href="#02-read-1-HiraginoSansGB-W3-6c60" x="1539.099747"/>
    <use xlink:href="#02-read-1-HiraginoSansGB-W3-6a21" x="1639.099731"/>
    <use xlink:href="#02-read-1-HiraginoSansGB-W3-5f0f" x="1739.099716"/>
    <use xlink:href="#02-read-1-HiraginoSansGB-W3-7684" x="1839.099701"/>
    <use xlink:href="#02-read-1-HiraginoSansGB-W3-8017" x="1939.099686"/>
    <use xlink:href="#02-read-1-HiraginoSansGB-W3-65f6" x="2039.09967"/>
    <use xlink:href="#02-read-1-HiraginoSansGB-W3-63a5" x="2139.099655"/>
    <use xlink:href="#02-read-1-HiraginoSansGB-W3-8fd1" x="2239.09964"/>
   </g>
  </g>
 </g>
 <defs>
  <clipPath id="02-read-1-p49ec9aeb87">
   <rect x="120.68" y="48.149543" width="396.73576" height="548.279194"/>
  </clipPath>
  <clipPath id="02-read-1-pf7902ebff6">
   <rect x="641.26376" y="48.149543" width="396.73576" height="548.279194"/>
  </clipPath>
 </defs>
 <defs>
  <pattern id="02-read-1-h02a70d68cd" patternUnits="userSpaceOnUse" x="0" y="0" width="72" height="72">
   <rect x="0" y="0" width="73" height="73" fill="#c8daed"/>
   <path d="M -36 36 
L 36 -36 
M -32 40 
L 40 -32 
M -28 44 
L 44 -28 
M -24 48 
L 48 -24 
M -20 52 
L 52 -20 
M -16 56 
L 56 -16 
M -12 60 
L 60 -12 
M -8 64 
L 64 -8 
M -4 68 
L 68 -4 
M 0 72 
L 72 0 
M 4 76 
L 76 4 
M 8 80 
L 80 8 
M 12 84 
L 84 12 
M 16 88 
L 88 16 
M 20 92 
L 92 20 
M 24 96 
L 96 24 
M 28 100 
L 100 28 
M 32 104 
L 104 32 
M 36 108 
L 108 36 
" style="fill: #467bb5; stroke: #467bb5; stroke-width: 1.0; stroke-linecap: butt; stroke-linejoin: miter"/>
  </pattern>
  <pattern id="02-read-1-hf8fe4700db" patternUnits="userSpaceOnUse" x="0" y="0" width="72" height="72">
   <rect x="0" y="0" width="73" height="73" fill="#b5dcd4"/>
   <path d="M -36 36 
L 36 -36 
M -32 40 
L 40 -32 
M -28 44 
L 44 -28 
M -24 48 
L 48 -24 
M -20 52 
L 52 -20 
M -16 56 
L 56 -16 
M -12 60 
L 60 -12 
M -8 64 
L 64 -8 
M -4 68 
L 68 -4 
M 0 72 
L 72 0 
M 4 76 
L 76 4 
M 8 80 
L 80 8 
M 12 84 
L 84 12 
M 16 88 
L 88 16 
M 20 92 
L 92 20 
M 24 96 
L 96 24 
M 28 100 
L 100 28 
M 32 104 
L 104 32 
M 36 108 
L 108 36 
" style="fill: #008477; stroke: #008477; stroke-width: 1.0; stroke-linecap: butt; stroke-linejoin: miter"/>
  </pattern>
 </defs>
</svg>
</figure>
```
**1 请求：共享 TP4 较 TP2 变化为 -1.3%～+2.0%。**

```svg
<figure class="am-diagram"><svg role="img" aria-label="16请求读取：TP2和TP4的独立池与共享池，共四组对照" xmlns:xlink="http://www.w3.org/1999/xlink" width="1045.19952pt" height="699.596655pt" viewBox="0 0 1045.19952 699.596655" xmlns="http://www.w3.org/2000/svg" version="1.1">
 <metadata>
  <rdf:RDF xmlns:dc="http://purl.org/dc/elements/1.1/" xmlns:cc="http://creativecommons.org/ns#" xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#">
   <cc:Work>
    <dc:type rdf:resource="http://purl.org/dc/dcmitype/StillImage"/>
    <dc:date>2026-10-13T20:32:00.516023</dc:date>
    <dc:format>image/svg+xml</dc:format>
    <dc:creator>
     <cc:Agent>
      <dc:title>Matplotlib v3.9.4, https://matplotlib.org/</dc:title>
     </cc:Agent>
    </dc:creator>
   </cc:Work>
  </rdf:RDF>
 </metadata>
 <defs>
  <style type="text/css">*{stroke-linejoin: round; stroke-linecap: butt}</style>
 </defs>
 <g id="03-read-16-figure_1">
  <g id="03-read-16-patch_1">
   <path d="M 0 699.596655 
L 1045.19952 699.596655 
L 1045.19952 -0 
L 0 -0 
z
" style="fill: #ffffff"/>
  </g>
  <g id="03-read-16-axes_1">
   <g id="03-read-16-patch_2">
    <path d="M 120.68 596.428736 
L 508.091385 596.428736 
L 508.091385 48.149543 
L 120.68 48.149543 
z
" style="fill: #ffffff"/>
   </g>
   <g id="03-read-16-matplotlib.axis_1">
    <g id="03-read-16-xtick_1">
     <g id="03-read-16-line2d_1">
      <path d="M 120.68 596.428736 
L 120.68 48.149543 
" clip-path="url(#03-read-16-p1bff6ad17d)" style="fill: none; stroke: #e9edf2; stroke-width: 0.8; stroke-linecap: square"/>
     </g>
     <g id="03-read-16-line2d_2"/>
     <g id="03-read-16-text_1">
      <!-- 0.0 -->
      <g style="fill: #405570" transform="translate(111.355625 613.619986) scale(0.12 -0.12)">
       <defs>
        <path id="03-read-16-HiraginoSansGB-W3-30" d="M 2099 -58 
C 2688 -58 3136 166 3456 614 
C 3738 1005 3904 1690 3904 2464 
C 3904 2995 3821 3507 3661 3949 
C 3584 4160 3456 4358 3277 4538 
C 2989 4838 2579 4992 2106 4992 
C 1472 4992 979 4723 672 4205 
C 448 3834 301 3142 301 2464 
C 301 1939 384 1434 544 986 
C 621 768 755 570 928 397 
C 1229 96 1626 -58 2099 -58 
z
M 2099 4550 
C 2547 4550 2893 4352 3104 3968 
C 3270 3667 3392 3034 3392 2477 
C 3392 2003 3328 1581 3194 1184 
C 3136 998 3034 832 2893 685 
C 2701 486 2426 384 2106 384 
C 1670 384 1338 570 1114 941 
C 941 1242 813 1869 813 2464 
C 813 2931 877 3366 1011 3757 
C 1062 3917 1165 4077 1299 4224 
C 1504 4442 1766 4550 2099 4550 
z
" transform="scale(0.015625)"/>
        <path id="03-read-16-HiraginoSansGB-W3-2e" d="M 448 672 
L 448 38 
L 1082 38 
L 1082 672 
L 448 672 
z
" transform="scale(0.015625)"/>
       </defs>
       <use xlink:href="#03-read-16-HiraginoSansGB-W3-30"/>
       <use xlink:href="#03-read-16-HiraginoSansGB-W3-2e" x="65.699997"/>
       <use xlink:href="#03-read-16-HiraginoSansGB-W3-30" x="89.699982"/>
      </g>
     </g>
    </g>
    <g id="03-read-16-xtick_2">
     <g id="03-read-16-line2d_3">
      <path d="M 163.725709 596.428736 
L 163.725709 48.149543 
" clip-path="url(#03-read-16-p1bff6ad17d)" style="fill: none; stroke: #e9edf2; stroke-width: 0.8; stroke-linecap: square"/>
     </g>
     <g id="03-read-16-line2d_4"/>
     <g id="03-read-16-text_2">
      <!-- 0.2 -->
      <g style="fill: #405570" transform="translate(154.401334 613.619986) scale(0.12 -0.12)">
       <defs>
        <path id="03-read-16-HiraginoSansGB-W3-32" d="M 3027 38 
C 3347 38 3558 26 3814 0 
L 3814 538 
C 3571 506 3354 493 3034 493 
L 992 493 
C 1069 800 1158 973 1357 1235 
C 1568 1504 1773 1683 2278 2022 
C 3008 2509 3174 2630 3366 2848 
C 3616 3123 3744 3430 3744 3757 
C 3744 4512 3123 4998 2157 4998 
C 1594 4998 1158 4832 870 4506 
C 659 4269 563 4045 467 3584 
L 1005 3494 
C 1050 3814 1107 3968 1229 4141 
C 1421 4416 1734 4557 2157 4557 
C 2810 4557 3219 4243 3219 3757 
C 3219 3366 3008 3085 2374 2656 
C 1632 2150 1619 2144 1395 1952 
C 992 1606 723 1254 563 851 
C 467 608 448 512 378 6 
C 582 32 678 38 1082 38 
L 3027 38 
z
" transform="scale(0.015625)"/>
       </defs>
       <use xlink:href="#03-read-16-HiraginoSansGB-W3-30"/>
       <use xlink:href="#03-read-16-HiraginoSansGB-W3-2e" x="65.699997"/>
       <use xlink:href="#03-read-16-HiraginoSansGB-W3-32" x="89.699982"/>
      </g>
     </g>
    </g>
    <g id="03-read-16-xtick_3">
     <g id="03-read-16-line2d_5">
      <path d="M 206.771419 596.428736 
L 206.771419 48.149543 
" clip-path="url(#03-read-16-p1bff6ad17d)" style="fill: none; stroke: #e9edf2; stroke-width: 0.8; stroke-linecap: square"/>
     </g>
     <g id="03-read-16-line2d_6"/>
     <g id="03-read-16-text_3">
      <!-- 0.4 -->
      <g style="fill: #405570" transform="translate(197.423606 613.619986) scale(0.12 -0.12)">
       <defs>
        <path id="03-read-16-HiraginoSansGB-W3-34" d="M 198 1254 
C 410 1274 602 1280 960 1280 
L 2675 1280 
L 2675 730 
C 2675 442 2662 262 2624 38 
L 3206 38 
C 3168 275 3155 422 3155 730 
L 3155 1280 
L 3392 1280 
C 3667 1280 3827 1267 4064 1235 
L 4064 1766 
C 3834 1734 3667 1722 3398 1722 
L 3155 1722 
L 3155 3821 
C 3155 4326 3162 4525 3194 4902 
L 2714 4902 
C 2566 4678 2451 4518 2234 4250 
L 614 2227 
C 480 2067 333 1888 198 1734 
L 198 1254 
z
M 2682 1715 
L 768 1715 
L 2240 3571 
C 2426 3808 2586 4019 2720 4211 
C 2688 3885 2682 3680 2682 3341 
L 2682 1715 
z
" transform="scale(0.015625)"/>
       </defs>
       <use xlink:href="#03-read-16-HiraginoSansGB-W3-30"/>
       <use xlink:href="#03-read-16-HiraginoSansGB-W3-2e" x="65.699997"/>
       <use xlink:href="#03-read-16-HiraginoSansGB-W3-34" x="89.699982"/>
      </g>
     </g>
    </g>
    <g id="03-read-16-xtick_4">
     <g id="03-read-16-line2d_7">
      <path d="M 249.817128 596.428736 
L 249.817128 48.149543 
" clip-path="url(#03-read-16-p1bff6ad17d)" style="fill: none; stroke: #e9edf2; stroke-width: 0.8; stroke-linecap: square"/>
     </g>
     <g id="03-read-16-line2d_8"/>
     <g id="03-read-16-text_4">
      <!-- 0.6 -->
      <g style="fill: #405570" transform="translate(240.492753 613.619986) scale(0.12 -0.12)">
       <defs>
        <path id="03-read-16-HiraginoSansGB-W3-36" d="M 3725 4006 
C 3539 4390 3411 4563 3187 4723 
C 2944 4896 2598 4992 2214 4992 
C 1658 4992 1203 4755 864 4294 
C 557 3878 378 3162 378 2362 
C 378 1453 608 736 1024 352 
C 1299 96 1728 -51 2182 -51 
C 3194 -51 3859 602 3859 1594 
C 3859 2579 3245 3194 2253 3194 
C 1811 3194 1472 3072 1171 2803 
C 1030 2682 966 2592 851 2394 
C 896 3091 954 3386 1120 3770 
C 1350 4294 1728 4563 2227 4563 
C 2547 4563 2797 4454 2989 4243 
C 3085 4134 3130 4045 3213 3840 
L 3725 4006 
z
M 3354 1581 
C 3354 1331 3290 1069 3187 902 
C 2989 582 2605 384 2189 384 
C 1498 384 979 896 979 1587 
C 979 1907 1088 2189 1299 2400 
C 1523 2624 1862 2765 2182 2765 
C 2931 2765 3354 2336 3354 1581 
z
" transform="scale(0.015625)"/>
       </defs>
       <use xlink:href="#03-read-16-HiraginoSansGB-W3-30"/>
       <use xlink:href="#03-read-16-HiraginoSansGB-W3-2e" x="65.699997"/>
       <use xlink:href="#03-read-16-HiraginoSansGB-W3-36" x="89.699982"/>
      </g>
     </g>
    </g>
    <g id="03-read-16-xtick_5">
     <g id="03-read-16-line2d_9">
      <path d="M 292.862838 596.428736 
L 292.862838 48.149543 
" clip-path="url(#03-read-16-p1bff6ad17d)" style="fill: none; stroke: #e9edf2; stroke-width: 0.8; stroke-linecap: square"/>
     </g>
     <g id="03-read-16-line2d_10"/>
     <g id="03-read-16-text_5">
      <!-- 0.8 -->
      <g style="fill: #405570" transform="translate(283.538463 613.619986) scale(0.12 -0.12)">
       <defs>
        <path id="03-read-16-HiraginoSansGB-W3-38" d="M 2752 2586 
C 3322 2694 3680 3142 3680 3738 
C 3680 4525 3078 5005 2099 5005 
C 1133 5005 525 4512 525 3738 
C 525 3142 890 2688 1453 2586 
C 1216 2547 1075 2496 877 2368 
C 493 2118 307 1766 307 1306 
C 307 422 954 -64 2125 -64 
C 3251 -64 3891 429 3891 1306 
C 3891 1734 3731 2074 3411 2310 
C 3200 2470 3021 2547 2752 2586 
z
M 2099 4576 
C 2758 4576 3181 4237 3181 3699 
C 3181 3130 2778 2784 2106 2784 
C 1434 2784 1024 3130 1024 3699 
C 1024 4230 1446 4576 2099 4576 
z
M 2099 2342 
C 2886 2342 3366 1965 3366 1338 
C 3366 1075 3283 851 3123 704 
C 2906 486 2541 371 2093 371 
C 1299 371 832 730 832 1338 
C 832 1958 1312 2342 2099 2342 
z
" transform="scale(0.015625)"/>
       </defs>
       <use xlink:href="#03-read-16-HiraginoSansGB-W3-30"/>
       <use xlink:href="#03-read-16-HiraginoSansGB-W3-2e" x="65.699997"/>
       <use xlink:href="#03-read-16-HiraginoSansGB-W3-38" x="89.699982"/>
      </g>
     </g>
    </g>
    <g id="03-read-16-xtick_6">
     <g id="03-read-16-line2d_11">
      <path d="M 335.908547 596.428736 
L 335.908547 48.149543 
" clip-path="url(#03-read-16-p1bff6ad17d)" style="fill: none; stroke: #e9edf2; stroke-width: 0.8; stroke-linecap: square"/>
     </g>
     <g id="03-read-16-line2d_12"/>
     <g id="03-read-16-text_6">
      <!-- 1.0 -->
      <g style="fill: #405570" transform="translate(326.584172 613.619986) scale(0.12 -0.12)">
       <defs>
        <path id="03-read-16-HiraginoSansGB-W3-31" d="M 1030 4429 
C 1216 4454 1382 4467 1587 4467 
L 1933 4467 
L 1933 762 
C 1933 467 1920 262 1882 38 
L 2490 38 
C 2451 275 2438 461 2438 762 
L 2438 3872 
C 2438 4218 2458 4544 2496 4934 
C 2285 4909 2182 4902 1965 4902 
L 1562 4902 
C 1318 4902 1197 4909 1030 4934 
L 1030 4429 
z
" transform="scale(0.015625)"/>
       </defs>
       <use xlink:href="#03-read-16-HiraginoSansGB-W3-31"/>
       <use xlink:href="#03-read-16-HiraginoSansGB-W3-2e" x="65.699997"/>
       <use xlink:href="#03-read-16-HiraginoSansGB-W3-30" x="89.699982"/>
      </g>
     </g>
    </g>
    <g id="03-read-16-xtick_7">
     <g id="03-read-16-line2d_13">
      <path d="M 378.954257 596.428736 
L 378.954257 48.149543 
" clip-path="url(#03-read-16-p1bff6ad17d)" style="fill: none; stroke: #e9edf2; stroke-width: 0.8; stroke-linecap: square"/>
     </g>
     <g id="03-read-16-line2d_14"/>
     <g id="03-read-16-text_7">
      <!-- 1.2 -->
      <g style="fill: #405570" transform="translate(369.629882 613.619986) scale(0.12 -0.12)">
       <use xlink:href="#03-read-16-HiraginoSansGB-W3-31"/>
       <use xlink:href="#03-read-16-HiraginoSansGB-W3-2e" x="65.699997"/>
       <use xlink:href="#03-read-16-HiraginoSansGB-W3-32" x="89.699982"/>
      </g>
     </g>
    </g>
    <g id="03-read-16-xtick_8">
     <g id="03-read-16-line2d_15">
      <path d="M 421.999966 596.428736 
L 421.999966 48.149543 
" clip-path="url(#03-read-16-p1bff6ad17d)" style="fill: none; stroke: #e9edf2; stroke-width: 0.8; stroke-linecap: square"/>
     </g>
     <g id="03-read-16-line2d_16"/>
     <g id="03-read-16-text_8">
      <!-- 1.4 -->
      <g style="fill: #405570" transform="translate(412.652154 613.619986) scale(0.12 -0.12)">
       <use xlink:href="#03-read-16-HiraginoSansGB-W3-31"/>
       <use xlink:href="#03-read-16-HiraginoSansGB-W3-2e" x="65.699997"/>
       <use xlink:href="#03-read-16-HiraginoSansGB-W3-34" x="89.699982"/>
      </g>
     </g>
    </g>
    <g id="03-read-16-xtick_9">
     <g id="03-read-16-line2d_17">
      <path d="M 465.045676 596.428736 
L 465.045676 48.149543 
" clip-path="url(#03-read-16-p1bff6ad17d)" style="fill: none; stroke: #e9edf2; stroke-width: 0.8; stroke-linecap: square"/>
     </g>
     <g id="03-read-16-line2d_18"/>
     <g id="03-read-16-text_9">
      <!-- 1.6 -->
      <g style="fill: #405570" transform="translate(455.721301 613.619986) scale(0.12 -0.12)">
       <use xlink:href="#03-read-16-HiraginoSansGB-W3-31"/>
       <use xlink:href="#03-read-16-HiraginoSansGB-W3-2e" x="65.699997"/>
       <use xlink:href="#03-read-16-HiraginoSansGB-W3-36" x="89.699982"/>
      </g>
     </g>
    </g>
    <g id="03-read-16-xtick_10">
     <g id="03-read-16-line2d_19">
      <path d="M 508.091385 596.428736 
L 508.091385 48.149543 
" clip-path="url(#03-read-16-p1bff6ad17d)" style="fill: none; stroke: #e9edf2; stroke-width: 0.8; stroke-linecap: square"/>
     </g>
     <g id="03-read-16-line2d_20"/>
     <g id="03-read-16-text_10">
      <!-- 1.8 -->
      <g style="fill: #405570" transform="translate(498.76701 613.619986) scale(0.12 -0.12)">
       <use xlink:href="#03-read-16-HiraginoSansGB-W3-31"/>
       <use xlink:href="#03-read-16-HiraginoSansGB-W3-2e" x="65.699997"/>
       <use xlink:href="#03-read-16-HiraginoSansGB-W3-38" x="89.699982"/>
      </g>
     </g>
    </g>
    <g id="03-read-16-text_11">
     <!-- 组 P50（ms） · 越低越好 -->
     <g style="fill: #405570" transform="translate(239.72788 629.259986) scale(0.12 -0.12)">
      <defs>
       <path id="03-read-16-HiraginoSansGB-W3-7ec4" d="M 646 1299 
C 845 1363 941 1389 1107 1421 
C 1344 1459 1344 1459 1440 1478 
C 1830 1549 1830 1549 2176 1606 
C 2432 1651 2432 1651 2592 1683 
L 2637 2080 
C 2202 1978 1933 1926 1152 1786 
C 1696 2432 2176 3091 2624 3814 
L 2240 4026 
C 2106 3744 2061 3661 1894 3398 
C 1498 3366 1498 3366 928 3328 
C 1158 3706 1408 4186 1632 4691 
C 1683 4794 1683 4794 1850 5165 
L 1434 5331 
C 1261 4813 1056 4301 858 3904 
C 608 3405 550 3328 384 3302 
L 493 2893 
C 691 2963 966 3002 1677 3053 
C 1555 2867 1555 2867 1152 2330 
C 851 1914 749 1805 634 1773 
C 544 1747 544 1747 518 1741 
L 646 1299 
z
M 5606 141 
L 5606 4275 
C 5606 4653 5606 4794 5626 5005 
C 5453 4986 5254 4979 4922 4979 
L 3699 4979 
C 3411 4979 3245 4986 3059 4998 
C 3078 4787 3085 4602 3085 4339 
L 3085 141 
L 2950 141 
C 2765 141 2598 154 2451 179 
L 2451 -256 
C 2624 -230 2816 -218 3027 -218 
L 5459 -218 
C 5766 -218 5952 -230 6144 -256 
L 6144 179 
C 6016 154 5837 141 5715 141 
L 5606 141 
z
M 3469 4608 
L 5222 4608 
L 5222 3405 
L 3469 3405 
L 3469 4608 
z
M 3469 3046 
L 5222 3046 
L 5222 1786 
L 3469 1786 
L 3469 3046 
z
M 3469 1427 
L 5222 1427 
L 5222 141 
L 3469 141 
L 3469 1427 
z
M 2534 928 
C 2086 755 1536 589 1069 480 
C 755 410 467 365 365 365 
C 352 365 339 365 314 365 
L 416 -90 
C 493 -58 531 -45 646 -19 
C 1152 102 1498 192 1798 294 
C 2336 474 2336 474 2534 531 
L 2534 928 
z
" transform="scale(0.015625)"/>
       <path id="03-read-16-HiraginoSansGB-W3-20" transform="scale(0.015625)"/>
       <path id="03-read-16-HiraginoSansGB-W3-50" d="M 1094 38 
C 1056 256 1043 454 1043 762 
L 1043 2022 
L 2470 2022 
C 3469 2022 4045 2566 4045 3507 
C 4045 4397 3488 4902 2502 4902 
L 1197 4902 
C 902 4902 723 4909 493 4922 
C 531 4653 544 4454 544 4192 
L 544 755 
C 544 454 531 269 493 38 
L 1094 38 
z
M 1043 2464 
L 1043 4461 
L 2470 4461 
C 3162 4461 3533 4122 3533 3482 
C 3533 2829 3149 2464 2470 2464 
L 1043 2464 
z
" transform="scale(0.015625)"/>
       <path id="03-read-16-HiraginoSansGB-W3-35" d="M 1261 4461 
L 2765 4461 
C 3066 4461 3270 4448 3514 4410 
L 3514 4954 
C 3258 4915 3110 4902 2765 4902 
L 1446 4902 
C 1069 4902 966 4909 768 4934 
C 781 4774 781 4749 781 4659 
C 781 4538 774 4461 762 4275 
L 621 2829 
C 595 2554 576 2419 525 2170 
L 1005 2125 
C 1075 2298 1146 2387 1299 2509 
C 1530 2688 1811 2778 2138 2778 
C 2816 2778 3258 2317 3258 1613 
C 3258 877 2778 390 2061 390 
C 1690 390 1370 525 1152 768 
C 1056 877 1011 960 947 1139 
L 422 986 
C 576 678 666 544 826 384 
C 1133 96 1549 -51 2048 -51 
C 3104 -51 3776 608 3776 1638 
C 3776 2112 3635 2490 3347 2765 
C 3059 3046 2662 3200 2195 3200 
C 1754 3200 1382 3053 1075 2758 
C 1114 2931 1114 2931 1126 3046 
L 1261 4461 
z
" transform="scale(0.015625)"/>
       <path id="03-read-16-HiraginoSansGB-W3-ff08" d="M 6157 -518 
C 5619 26 5408 314 5184 806 
C 4954 1318 4838 1850 4838 2432 
C 4838 3014 4954 3546 5184 4058 
C 5408 4550 5619 4838 6157 5382 
L 5587 5382 
C 5107 4832 4909 4525 4698 4006 
C 4486 3494 4390 2989 4390 2432 
C 4390 1875 4486 1370 4698 858 
C 4909 339 5107 32 5587 -518 
L 6157 -518 
z
" transform="scale(0.015625)"/>
       <path id="03-read-16-HiraginoSansGB-W3-6d" d="M 1018 38 
C 986 294 973 467 973 762 
L 973 2016 
C 973 2182 1069 2483 1184 2656 
C 1382 2963 1690 3142 2022 3142 
C 2483 3142 2739 2835 2739 2272 
L 2739 762 
C 2739 435 2726 269 2694 38 
L 3270 38 
C 3238 256 3226 435 3226 755 
L 3226 2016 
C 3226 2208 3315 2490 3437 2675 
C 3622 2970 3923 3142 4243 3142 
C 4755 3142 4979 2880 4979 2272 
L 4979 762 
C 4979 422 4973 275 4934 38 
L 5510 38 
C 5478 262 5466 435 5466 755 
L 5466 2336 
C 5466 2714 5402 2963 5248 3168 
C 5056 3424 4736 3558 4314 3558 
C 3776 3558 3430 3354 3130 2861 
C 3066 3053 3027 3130 2931 3232 
C 2739 3450 2470 3558 2106 3558 
C 1734 3558 1421 3443 1203 3226 
C 1075 3091 1018 3014 960 2906 
C 966 3027 973 3104 973 3136 
L 973 3488 
L 454 3488 
C 486 3258 499 3085 499 2765 
L 499 762 
C 499 461 486 269 454 38 
L 1018 38 
z
" transform="scale(0.015625)"/>
       <path id="03-read-16-HiraginoSansGB-W3-73" d="M 269 973 
C 333 710 378 602 474 467 
C 710 141 1158 -32 1741 -32 
C 2618 -32 3162 365 3162 1005 
C 3162 1267 3053 1510 2854 1670 
C 2662 1818 2458 1894 1837 2035 
C 1440 2118 1274 2170 1126 2253 
C 986 2330 909 2458 909 2605 
C 909 2944 1242 3181 1709 3181 
C 1997 3181 2227 3098 2374 2938 
C 2464 2848 2496 2771 2534 2598 
L 3040 2682 
C 2957 2976 2867 3123 2701 3264 
C 2470 3469 2131 3571 1696 3571 
C 1267 3571 922 3456 698 3238 
C 518 3066 422 2848 422 2618 
C 422 2336 544 2099 768 1946 
C 986 1798 1203 1722 1715 1613 
C 2189 1517 2317 1478 2470 1382 
C 2605 1299 2669 1165 2669 986 
C 2669 608 2317 371 1760 371 
C 1434 371 1178 448 1018 595 
C 890 710 838 826 794 1062 
L 269 973 
z
" transform="scale(0.015625)"/>
       <path id="03-read-16-HiraginoSansGB-W3-ff09" d="M 813 -518 
C 1293 32 1491 339 1702 858 
C 1914 1370 2010 1875 2010 2432 
C 2010 2989 1914 3494 1702 4006 
C 1491 4525 1293 4832 813 5382 
L 243 5382 
C 781 4845 992 4550 1216 4058 
C 1446 3546 1562 3014 1562 2432 
C 1562 1850 1446 1318 1216 806 
C 992 314 781 19 243 -518 
L 813 -518 
z
" transform="scale(0.015625)"/>
       <path id="03-read-16-HiraginoSansGB-W3-b7" d="M 3200 2957 
C 2912 2957 2675 2720 2675 2432 
C 2675 2144 2912 1907 3200 1907 
C 3488 1907 3725 2144 3725 2432 
C 3725 2720 3488 2957 3200 2957 
z
" transform="scale(0.015625)"/>
       <path id="03-read-16-HiraginoSansGB-W3-8d8a" d="M 4026 320 
C 4512 768 4723 979 4934 1254 
C 5242 723 5472 499 5696 499 
C 5914 499 6042 781 6170 1510 
C 6042 1562 5965 1613 5850 1709 
C 5773 1126 5715 922 5632 922 
C 5594 922 5549 966 5421 1133 
C 5299 1293 5254 1370 5152 1594 
C 5440 2099 5536 2349 5722 3066 
C 5760 3219 5786 3290 5818 3386 
L 5440 3526 
C 5370 3078 5197 2522 4979 2042 
C 4832 2477 4768 2931 4710 3962 
L 5408 3962 
C 5645 3962 5837 3949 6010 3923 
L 6010 4339 
C 5875 4314 5709 4307 5408 4307 
L 4698 4307 
C 4691 4371 4691 4390 4691 4467 
C 4691 5171 4691 5171 4717 5318 
L 4282 5318 
C 4314 5094 4314 5075 4314 4403 
C 4314 4365 4314 4352 4314 4307 
L 3667 4307 
C 3398 4307 3238 4314 3078 4333 
C 3098 4166 3104 4006 3104 3750 
L 3104 1389 
C 3085 1216 3066 1184 2950 1114 
L 3168 768 
C 3277 832 3277 832 3757 1120 
C 3853 1178 4013 1280 4333 1491 
L 4256 1850 
C 3846 1574 3699 1485 3526 1395 
C 3501 1382 3501 1382 3475 1370 
L 3475 3962 
L 4333 3962 
C 4403 2867 4512 2246 4749 1632 
C 4422 1190 4154 915 3757 621 
C 3770 614 3795 582 3853 531 
C 3904 480 3917 467 3974 390 
L 4026 320 
z
M 1549 4422 
L 1056 4422 
C 762 4422 646 4429 499 4454 
L 499 4038 
C 666 4064 819 4070 1056 4070 
L 1549 4070 
L 1549 3213 
L 960 3213 
C 666 3213 499 3226 352 3251 
L 352 2822 
C 518 2854 666 2861 960 2861 
L 1594 2861 
L 1594 653 
C 1382 845 1165 1133 1030 1395 
C 1056 1606 1075 1734 1082 1914 
C 1101 2266 1107 2278 1120 2400 
L 704 2426 
C 723 2310 723 2272 723 2170 
C 723 1229 531 442 141 -205 
C 282 -275 352 -320 474 -442 
C 710 0 870 448 960 947 
C 1139 659 1293 467 1485 294 
C 1869 -51 2253 -211 2906 -275 
C 3322 -320 3526 -326 4928 -326 
C 5146 -326 5760 -307 5997 -294 
C 6029 -109 6048 -38 6138 147 
C 5683 90 5037 58 4326 58 
C 3757 58 3232 77 2829 115 
C 2458 147 2221 218 1965 371 
L 1965 1677 
L 2317 1677 
C 2541 1677 2701 1664 2842 1638 
L 2842 2048 
C 2688 2022 2541 2016 2323 2016 
L 1965 2016 
L 1965 2861 
L 2336 2861 
C 2618 2861 2778 2848 2938 2822 
L 2938 3251 
C 2790 3226 2630 3213 2336 3213 
L 1926 3213 
L 1926 4070 
L 2246 4070 
C 2502 4070 2656 4064 2822 4038 
L 2822 4454 
C 2682 4429 2573 4422 2246 4422 
L 1926 4422 
L 1926 4678 
C 1926 4986 1933 5133 1958 5306 
L 1510 5306 
C 1542 5133 1549 4986 1549 4678 
L 1549 4422 
z
M 4947 5088 
C 5210 4870 5350 4710 5542 4442 
L 5811 4666 
C 5613 4922 5530 5011 5210 5299 
L 4947 5088 
z
" transform="scale(0.015625)"/>
       <path id="03-read-16-HiraginoSansGB-W3-4f4e" d="M 4198 2502 
C 4378 1261 4627 570 5094 19 
C 5318 -243 5466 -352 5606 -352 
C 5792 -352 5901 -262 6003 -32 
C 6074 128 6182 602 6208 851 
C 6074 941 5978 1018 5869 1133 
C 5830 525 5722 96 5606 96 
C 5478 96 5120 602 4941 1043 
C 4794 1395 4653 1990 4595 2502 
L 5395 2502 
C 5651 2502 5805 2496 5965 2470 
L 5965 2886 
C 5818 2861 5715 2854 5395 2854 
L 4550 2854 
C 4486 3405 4467 3706 4454 4582 
C 4896 4685 4960 4704 5722 4973 
L 5382 5338 
C 4883 5005 3507 4691 2387 4666 
C 2413 4499 2419 4378 2419 4083 
L 2419 205 
C 2419 83 2368 26 2272 6 
L 2413 -384 
C 2784 -256 3078 -173 3680 -32 
L 3712 365 
C 3200 224 2899 147 2816 128 
L 2816 2502 
L 4198 2502 
z
M 2816 2854 
L 2816 4320 
C 3315 4365 3648 4410 4058 4493 
C 4064 3930 4096 3437 4154 2854 
L 2816 2854 
z
M 1043 288 
C 1043 -32 1030 -243 1005 -480 
L 1472 -480 
C 1446 -256 1440 -38 1440 269 
L 1440 3750 
C 1773 4474 1773 4474 1946 4954 
C 1997 5088 2016 5139 2061 5248 
L 1613 5370 
C 1568 5088 1421 4627 1216 4186 
C 947 3597 608 3104 134 2611 
C 275 2451 314 2400 397 2246 
C 698 2592 870 2835 1069 3206 
C 1050 2976 1043 2714 1043 2438 
L 1043 288 
z
M 4749 -122 
C 4493 262 4218 589 3834 960 
L 3552 755 
C 3904 403 4179 58 4429 -358 
L 4749 -122 
z
" transform="scale(0.015625)"/>
       <path id="03-read-16-HiraginoSansGB-W3-597d" d="M 1094 3680 
C 928 3008 742 2458 493 1894 
C 954 1613 1075 1530 1517 1158 
C 1184 646 832 307 269 -38 
C 384 -141 461 -237 550 -378 
C 1139 38 1459 346 1773 838 
C 1792 864 1792 864 1811 890 
C 2074 621 2170 518 2445 173 
L 2726 512 
C 2374 909 2246 1037 2016 1242 
C 2029 1261 2029 1261 2048 1293 
C 2374 1894 2592 2733 2688 3776 
C 2707 3974 2707 3974 2720 4064 
C 2573 4045 2400 4038 2138 4038 
L 1581 4038 
C 1626 4250 1696 4634 1722 4806 
C 1773 5133 1786 5197 1811 5299 
L 1344 5344 
C 1350 5306 1350 5274 1350 5254 
C 1350 4998 1280 4499 1171 4038 
L 941 4038 
C 710 4038 538 4051 378 4077 
L 378 3642 
C 538 3667 698 3680 902 3680 
L 1094 3680 
z
M 2278 3680 
C 2176 2720 2016 2086 1715 1504 
C 1427 1728 1318 1805 973 2042 
C 1203 2637 1312 2989 1498 3680 
L 2278 3680 
z
M 4608 2605 
L 4608 3226 
C 5011 3565 5293 3846 5498 4102 
C 5907 4627 5907 4627 5952 4672 
L 5715 4966 
C 5613 4941 5485 4934 5222 4934 
L 3578 4934 
C 3309 4934 3098 4947 2931 4973 
L 2931 4518 
C 3091 4544 3322 4557 3558 4557 
L 5363 4557 
C 5037 4096 4787 3827 4378 3501 
L 4154 3501 
C 4186 3270 4198 3104 4198 2874 
L 4198 2605 
L 3328 2605 
C 3034 2605 2906 2611 2752 2643 
L 2752 2189 
C 2925 2214 3078 2227 3334 2227 
L 4198 2227 
L 4198 147 
C 4198 32 4154 13 3949 13 
C 3763 13 3488 45 3117 102 
C 3174 -70 3194 -147 3206 -333 
C 3546 -365 3802 -384 3974 -384 
C 4493 -384 4608 -294 4608 134 
L 4608 2227 
L 5664 2227 
C 5926 2227 6061 2221 6234 2189 
L 6234 2643 
C 6067 2611 5939 2605 5664 2605 
L 4608 2605 
z
" transform="scale(0.015625)"/>
      </defs>
      <use xlink:href="#03-read-16-HiraginoSansGB-W3-7ec4"/>
      <use xlink:href="#03-read-16-HiraginoSansGB-W3-20" x="99.999985"/>
      <use xlink:href="#03-read-16-HiraginoSansGB-W3-50" x="133.299973"/>
      <use xlink:href="#03-read-16-HiraginoSansGB-W3-35" x="199.499969"/>
      <use xlink:href="#03-read-16-HiraginoSansGB-W3-30" x="265.199966"/>
      <use xlink:href="#03-read-16-HiraginoSansGB-W3-ff08" x="330.899963"/>
      <use xlink:href="#03-read-16-HiraginoSansGB-W3-6d" x="430.899948"/>
      <use xlink:href="#03-read-16-HiraginoSansGB-W3-73" x="523.999939"/>
      <use xlink:href="#03-read-16-HiraginoSansGB-W3-ff09" x="577.699936"/>
      <use xlink:href="#03-read-16-HiraginoSansGB-W3-20" x="677.699921"/>
      <use xlink:href="#03-read-16-HiraginoSansGB-W3-b7" x="710.999908"/>
      <use xlink:href="#03-read-16-HiraginoSansGB-W3-20" x="810.999893"/>
      <use xlink:href="#03-read-16-HiraginoSansGB-W3-8d8a" x="844.299881"/>
      <use xlink:href="#03-read-16-HiraginoSansGB-W3-4f4e" x="944.299866"/>
      <use xlink:href="#03-read-16-HiraginoSansGB-W3-8d8a" x="1044.29985"/>
      <use xlink:href="#03-read-16-HiraginoSansGB-W3-597d" x="1144.299835"/>
     </g>
    </g>
   </g>
   <g id="03-read-16-matplotlib.axis_2">
    <g id="03-read-16-ytick_1">
     <g id="03-read-16-line2d_21"/>
     <g id="03-read-16-text_12">
      <!-- P 来源 / 连续 -->
      <g style="fill: #243a56" transform="translate(39.06 117.629115) scale(0.12 -0.12)">
       <defs>
        <path id="03-read-16-HiraginoSansGB-W3-6765" d="M 5306 2182 
C 5613 2182 5824 2170 6022 2144 
L 6022 2598 
C 5869 2566 5690 2554 5318 2554 
L 4371 2554 
C 4621 2931 4864 3379 5082 3859 
L 4685 4019 
C 4467 3443 4269 3059 3923 2554 
L 3334 2554 
L 3334 4230 
L 4979 4230 
C 5286 4230 5485 4218 5677 4192 
L 5677 4640 
C 5517 4608 5331 4595 4966 4595 
L 3334 4595 
L 3334 4806 
C 3334 5062 3341 5178 3373 5338 
L 2899 5338 
C 2925 5165 2938 5024 2938 4806 
L 2938 4595 
L 1421 4595 
C 1139 4595 947 4608 723 4640 
L 723 4192 
C 947 4218 1158 4230 1408 4230 
L 2938 4230 
L 2938 2554 
L 1082 2554 
C 787 2554 602 2566 384 2598 
L 384 2144 
C 640 2170 832 2182 1088 2182 
L 2758 2182 
C 2406 1734 2349 1664 2106 1434 
C 1587 922 986 506 326 186 
C 467 45 512 -19 614 -186 
C 1037 51 1389 288 1760 595 
C 2272 1018 2554 1338 2963 1971 
C 2944 1619 2938 1472 2938 1286 
L 2938 141 
C 2938 -122 2925 -288 2893 -480 
L 3379 -480 
C 3347 -294 3334 -122 3334 141 
L 3334 1286 
C 3334 1446 3328 1645 3309 1978 
C 3661 1472 3878 1210 4211 902 
C 4685 474 5178 141 5824 -186 
C 5914 -19 5984 83 6099 211 
C 5446 480 4928 806 4371 1293 
C 4070 1555 3930 1696 3526 2182 
L 5306 2182 
z
M 1248 3859 
C 1530 3424 1658 3174 1869 2656 
L 2227 2842 
C 1978 3386 1869 3597 1594 4026 
L 1248 3859 
z
" transform="scale(0.015625)"/>
        <path id="03-read-16-HiraginoSansGB-W3-6e90" d="M 4262 3987 
C 4371 4262 4397 4326 4486 4659 
L 5331 4659 
C 5574 4659 5818 4646 6003 4627 
L 6003 5056 
C 5818 5030 5638 5024 5325 5024 
L 2662 5024 
C 2368 5024 2227 5030 2054 5056 
C 2074 4774 2086 4474 2086 4211 
L 2086 4166 
L 2080 3360 
C 2074 2477 1978 1600 1824 1030 
C 1709 595 1587 307 1370 -38 
C 1555 -160 1594 -198 1709 -339 
C 2266 621 2483 1811 2483 3936 
L 2483 4659 
L 4077 4659 
C 4032 4416 3962 4192 3866 3987 
L 3507 3987 
C 3258 3987 3066 3994 2899 4013 
C 2912 3859 2918 3706 2918 3507 
L 2918 2259 
C 2918 2048 2912 1926 2893 1805 
C 3085 1824 3245 1830 3437 1830 
L 4090 1830 
L 4090 141 
C 4090 6 4058 -26 3904 -26 
C 3718 -26 3578 -13 3309 26 
C 3366 -134 3386 -224 3398 -378 
C 3565 -390 3782 -397 3923 -397 
C 4122 -397 4211 -384 4307 -346 
C 4422 -294 4486 -166 4486 26 
L 4486 1830 
L 5190 1830 
C 5389 1830 5530 1824 5702 1805 
C 5683 1926 5677 2048 5677 2304 
L 5677 3533 
C 5677 3763 5683 3891 5702 4013 
C 5523 3994 5357 3987 5107 3987 
L 4262 3987 
z
M 3296 3654 
L 5299 3654 
L 5299 3098 
L 3296 3098 
L 3296 3654 
z
M 3296 2765 
L 5299 2765 
L 5299 2170 
L 3296 2170 
L 3296 2765 
z
M 531 4922 
C 922 4666 1133 4474 1466 4090 
L 1754 4384 
C 1453 4710 1216 4915 819 5197 
L 531 4922 
z
M 243 3341 
C 672 3078 870 2925 1274 2522 
L 1530 2867 
C 1171 3206 922 3392 499 3648 
L 243 3341 
z
M 1133 1882 
C 890 1005 544 224 282 -38 
L 646 -365 
C 1088 499 1210 819 1453 1658 
L 1133 1882 
z
M 4947 1421 
C 5280 915 5472 544 5715 -102 
L 6067 96 
C 5830 678 5645 1024 5280 1594 
L 4947 1421 
z
M 3110 1562 
C 3034 1216 2982 1050 2854 794 
C 2720 518 2618 371 2387 96 
C 2522 13 2592 -45 2701 -166 
C 3110 371 3290 749 3482 1440 
L 3110 1562 
z
" transform="scale(0.015625)"/>
        <path id="03-read-16-HiraginoSansGB-W3-2f" d="M 2432 4902 
L 122 -954 
L 602 -954 
L 2906 4902 
L 2432 4902 
z
" transform="scale(0.015625)"/>
        <path id="03-read-16-HiraginoSansGB-W3-8fde" d="M 3149 4736 
L 2662 4736 
C 2381 4736 2195 4749 2016 4781 
L 2016 4339 
C 2195 4365 2406 4378 2656 4378 
L 3008 4378 
L 2656 3526 
C 2554 3270 2438 3149 2291 3123 
L 2394 2733 
C 2509 2758 2675 2771 2822 2771 
L 4013 2771 
L 4013 1914 
L 2605 1914 
C 2304 1914 2125 1926 1958 1958 
L 1958 1510 
C 2131 1536 2342 1549 2611 1549 
L 4013 1549 
L 4013 1088 
C 4013 755 4006 608 3968 384 
L 4435 384 
C 4397 589 4390 736 4390 1088 
L 4390 1549 
L 5434 1549 
C 5702 1549 5894 1536 6093 1510 
L 6093 1958 
C 5939 1926 5760 1914 5440 1914 
L 4390 1914 
L 4390 2771 
L 5030 2771 
C 5254 2771 5510 2758 5696 2733 
L 5696 3187 
C 5517 3162 5331 3149 5030 3149 
L 4390 3149 
L 4390 3379 
C 4390 3635 4403 3802 4435 3994 
L 3968 3994 
C 4000 3808 4013 3642 4013 3379 
L 4013 3149 
L 2906 3149 
L 3411 4378 
L 5299 4378 
C 5549 4378 5747 4365 5952 4339 
L 5952 4781 
C 5786 4749 5613 4736 5293 4736 
L 3558 4736 
C 3590 4800 3603 4838 3616 4864 
C 3706 5082 3706 5082 3802 5274 
L 3341 5389 
C 3296 5139 3258 4986 3168 4768 
L 3149 4736 
z
M 1638 2221 
C 1638 2509 1645 2675 1670 2867 
C 1510 2842 1395 2835 1190 2835 
L 851 2835 
C 602 2835 499 2842 358 2867 
L 358 2451 
C 506 2477 608 2483 826 2483 
L 1254 2483 
L 1254 858 
C 1171 832 1075 774 909 653 
C 762 544 518 314 237 13 
L 518 -320 
C 845 147 1203 486 1357 486 
C 1414 486 1485 448 1536 390 
C 1773 115 1773 115 1882 38 
C 2106 -115 2336 -198 2630 -237 
C 2906 -275 3424 -307 3827 -307 
C 4717 -307 5408 -294 6029 -262 
C 6074 -58 6080 -26 6182 205 
C 5626 128 4909 90 3981 90 
C 3194 90 2662 128 2419 205 
C 2150 288 1997 397 1766 666 
C 1709 736 1677 768 1638 806 
L 1638 2221 
z
M 435 4941 
C 864 4538 1062 4307 1427 3776 
L 1754 4070 
C 1350 4582 1146 4813 730 5197 
L 435 4941 
z
" transform="scale(0.015625)"/>
        <path id="03-read-16-HiraginoSansGB-W3-7eed" d="M 4026 1152 
C 3949 947 3827 768 3661 602 
C 3315 262 2886 58 2202 -115 
C 2310 -211 2368 -307 2438 -467 
C 3085 -275 3514 -51 3885 275 
C 4154 525 4326 787 4435 1152 
L 5466 1152 
C 5754 1152 5958 1139 6118 1120 
L 6118 1536 
C 5978 1510 5792 1504 5472 1504 
L 4512 1504 
C 4563 1811 4589 2150 4614 2778 
C 4621 2944 4621 2982 4653 3123 
L 4186 3130 
C 4205 2989 4211 2931 4211 2784 
C 4211 2285 4179 1818 4122 1504 
L 2989 1504 
C 2656 1504 2490 1517 2342 1542 
L 2342 1120 
C 2509 1139 2720 1152 3021 1152 
L 4026 1152 
z
M 3930 3725 
L 2989 3725 
C 2726 3725 2573 3738 2426 3763 
L 2426 3322 
C 2573 3347 2726 3360 3002 3360 
L 5542 3360 
C 5434 3117 5318 2918 5120 2669 
C 5254 2618 5363 2541 5466 2426 
C 5606 2637 5805 2950 5824 3008 
C 5965 3328 5978 3347 6048 3469 
L 5792 3744 
C 5670 3731 5574 3725 5350 3725 
L 4326 3725 
L 4326 4371 
L 5203 4371 
C 5478 4371 5683 4358 5856 4333 
L 5856 4762 
C 5709 4736 5478 4723 5197 4723 
L 4326 4723 
C 4333 4986 4339 5126 4365 5254 
L 3891 5254 
C 3917 5107 3923 5005 3930 4723 
L 3232 4723 
C 2950 4723 2707 4736 2566 4762 
L 2566 4333 
C 2720 4358 2963 4371 3232 4371 
L 3930 4371 
L 3930 3725 
z
M 1344 5254 
C 1229 4851 1094 4518 787 3898 
C 563 3443 461 3290 320 3206 
L 403 2842 
C 672 2886 928 2918 1408 2970 
C 909 2157 909 2157 742 1933 
C 627 1773 557 1715 416 1658 
L 538 1248 
C 736 1312 832 1331 2163 1606 
L 2202 2010 
C 1690 1869 1523 1830 1018 1715 
C 1421 2278 1779 2842 1997 3238 
C 2182 3584 2182 3584 2291 3763 
L 1920 3974 
C 1862 3866 1824 3776 1798 3718 
C 1722 3552 1670 3437 1651 3398 
C 1632 3360 1632 3360 1619 3334 
C 1120 3277 1120 3277 845 3258 
C 1024 3565 1101 3712 1190 3878 
C 1261 4019 1574 4672 1613 4762 
C 1715 5018 1715 5018 1747 5075 
L 1344 5254 
z
M 3987 2586 
C 3622 2854 3430 2982 3059 3187 
L 2822 2938 
C 3219 2726 3424 2592 3757 2317 
L 3987 2586 
z
M 2246 813 
C 1978 698 1907 678 1645 595 
C 1402 518 947 384 819 352 
C 640 301 378 256 294 256 
L 397 -186 
C 448 -160 467 -154 595 -122 
C 1254 64 1254 64 2259 422 
L 2246 813 
z
M 6029 -83 
C 5594 237 5152 512 4634 794 
L 4410 525 
C 5318 -45 5382 -90 5805 -435 
L 6029 -83 
z
M 3565 1965 
C 3238 2195 3155 2246 2605 2554 
L 2368 2291 
C 2733 2112 2938 1978 3334 1677 
L 3565 1965 
z
" transform="scale(0.015625)"/>
       </defs>
       <use xlink:href="#03-read-16-HiraginoSansGB-W3-50"/>
       <use xlink:href="#03-read-16-HiraginoSansGB-W3-20" x="66.199997"/>
       <use xlink:href="#03-read-16-HiraginoSansGB-W3-6765" x="99.499985"/>
       <use xlink:href="#03-read-16-HiraginoSansGB-W3-6e90" x="199.499969"/>
       <use xlink:href="#03-read-16-HiraginoSansGB-W3-20" x="299.499954"/>
       <use xlink:href="#03-read-16-HiraginoSansGB-W3-2f" x="332.799942"/>
       <use xlink:href="#03-read-16-HiraginoSansGB-W3-20" x="380.199936"/>
       <use xlink:href="#03-read-16-HiraginoSansGB-W3-8fde" x="413.499924"/>
       <use xlink:href="#03-read-16-HiraginoSansGB-W3-7eed" x="513.499908"/>
      </g>
     </g>
    </g>
    <g id="03-read-16-ytick_2">
     <g id="03-read-16-line2d_22"/>
     <g id="03-read-16-text_13">
      <!-- P 来源 / 离散 -->
      <g style="fill: #243a56" transform="translate(39.06 203.97792) scale(0.12 -0.12)">
       <defs>
        <path id="03-read-16-HiraginoSansGB-W3-79bb" d="M 4570 3046 
C 4166 3258 4115 3283 3667 3501 
C 3923 3648 4096 3757 4275 3891 
C 4346 3942 4358 3949 4416 3987 
L 4109 4243 
C 3968 4083 3648 3859 3296 3674 
C 2643 3955 2618 3968 2150 4147 
L 1933 3878 
C 2413 3693 2592 3622 2893 3488 
C 2528 3328 2106 3187 1677 3078 
C 1798 2989 1856 2918 1933 2778 
C 2387 2912 2861 3091 3296 3302 
C 3610 3155 3776 3066 4339 2739 
L 4570 3046 
z
M 5357 1498 
L 5357 96 
C 5357 -38 5325 -58 5146 -58 
C 4954 -58 4723 -38 4499 -6 
C 4550 -147 4563 -211 4595 -435 
C 4768 -448 4966 -454 5107 -454 
C 5613 -454 5754 -352 5754 19 
L 5754 1280 
C 5754 1536 5760 1702 5779 1882 
C 5587 1856 5434 1850 5203 1850 
L 2982 1850 
C 3021 1933 3085 2093 3174 2323 
L 4774 2323 
C 5005 2323 5146 2317 5280 2304 
C 5261 2451 5261 2528 5261 2784 
L 5261 3616 
C 5261 3840 5267 3955 5286 4115 
L 4845 4115 
C 4864 3962 4870 3834 4870 3629 
L 4870 2669 
L 1517 2669 
L 1517 3635 
C 1517 3840 1523 3981 1542 4115 
L 1101 4115 
C 1120 3955 1126 3834 1126 3616 
L 1126 2790 
C 1126 2541 1126 2464 1107 2304 
C 1248 2317 1370 2323 1600 2323 
L 2733 2323 
C 2630 2016 2611 1971 2560 1850 
L 1197 1850 
C 960 1850 806 1856 608 1882 
C 627 1702 634 1491 634 1242 
L 634 122 
C 634 -198 627 -365 602 -525 
L 1062 -525 
C 1037 -384 1030 -224 1030 122 
L 1030 1498 
L 2419 1498 
C 2118 762 2029 627 1773 544 
L 1894 179 
C 2234 230 3008 326 4211 474 
C 4269 365 4307 294 4410 90 
L 4736 288 
C 4454 768 4371 890 3968 1370 
L 3667 1210 
C 3853 992 3910 922 4000 794 
C 3539 730 3539 730 3110 685 
C 2963 672 2720 646 2426 614 
C 2579 902 2643 1043 2778 1363 
C 2816 1446 2816 1446 2835 1498 
L 5357 1498 
z
M 3488 4717 
C 3341 4973 3270 5069 3034 5357 
L 2694 5152 
C 2867 4947 2925 4870 3034 4717 
L 1069 4717 
C 794 4717 582 4730 403 4755 
L 403 4320 
C 608 4346 800 4352 1062 4352 
L 5402 4352 
C 5677 4352 5837 4346 6054 4320 
L 6054 4755 
C 5875 4730 5670 4717 5395 4717 
L 3488 4717 
z
" transform="scale(0.015625)"/>
        <path id="03-read-16-HiraginoSansGB-W3-6563" d="M 6170 4198 
C 5990 4166 5856 4160 5562 4160 
L 4282 4160 
C 4352 4371 4422 4653 4493 4954 
C 4550 5203 4557 5222 4582 5312 
L 4141 5395 
C 4141 5069 4006 4454 3827 3955 
C 3622 3405 3430 3046 3136 2714 
C 3232 2630 3315 2528 3405 2374 
C 3578 2611 3635 2701 3789 2995 
C 3904 2374 4166 1664 4454 1190 
C 4102 653 3738 307 3091 -109 
C 3283 -282 3296 -294 3386 -448 
C 3917 -96 4307 282 4691 826 
C 5094 269 5414 -32 6010 -435 
C 6086 -256 6099 -230 6246 -19 
C 5709 294 5261 704 4915 1190 
C 5344 1952 5555 2707 5645 3795 
C 5869 3795 6016 3782 6170 3757 
L 6170 4198 
z
M 5248 3795 
C 5184 3194 5165 3078 5075 2694 
C 4973 2266 4845 1901 4672 1568 
C 4358 2144 4186 2637 4064 3334 
C 4058 3366 4045 3411 4038 3475 
C 4096 3622 4102 3635 4160 3795 
L 5248 3795 
z
M 2688 4333 
L 2765 4333 
C 2995 4333 3155 4326 3296 4301 
L 3296 4710 
C 3142 4685 3027 4678 2765 4678 
L 2688 4678 
L 2688 4813 
C 2688 5082 2694 5203 2720 5338 
L 2278 5338 
C 2298 5203 2304 5107 2304 4813 
L 2304 4678 
L 1498 4678 
L 1498 4813 
C 1498 5126 1504 5216 1523 5338 
L 1082 5338 
C 1107 5178 1114 5075 1114 4813 
L 1114 4678 
L 992 4678 
C 730 4678 621 4685 461 4710 
L 461 4301 
C 602 4326 755 4333 992 4333 
L 1114 4333 
L 1114 3590 
L 870 3590 
C 634 3590 493 3603 320 3629 
L 320 3206 
C 506 3232 653 3245 870 3245 
L 2797 3245 
C 3053 3245 3168 3238 3341 3213 
L 3341 3622 
C 3181 3597 3078 3590 2803 3590 
L 2688 3590 
L 2688 4333 
z
M 2304 4333 
L 2304 3590 
L 1498 3590 
L 1498 4333 
L 2304 4333 
z
M 2611 762 
L 2611 115 
C 2611 6 2573 -19 2394 -19 
C 2234 -19 2080 -6 1875 32 
C 1933 -128 1939 -173 1965 -346 
C 2157 -358 2278 -365 2400 -365 
C 2880 -365 3002 -275 3002 64 
L 3002 2214 
C 3002 2528 3008 2688 3021 2829 
C 2854 2810 2669 2797 2445 2797 
L 1286 2797 
C 1069 2797 877 2810 698 2829 
C 710 2682 717 2547 717 2246 
L 717 237 
C 717 -58 704 -307 678 -474 
L 1139 -474 
C 1114 -314 1107 -96 1107 243 
L 1107 762 
L 2611 762 
z
M 2611 1094 
L 1107 1094 
L 1107 1619 
L 2611 1619 
L 2611 1094 
z
M 2611 1952 
L 1107 1952 
L 1107 2458 
L 2611 2458 
L 2611 1952 
z
" transform="scale(0.015625)"/>
       </defs>
       <use xlink:href="#03-read-16-HiraginoSansGB-W3-50"/>
       <use xlink:href="#03-read-16-HiraginoSansGB-W3-20" x="66.199997"/>
       <use xlink:href="#03-read-16-HiraginoSansGB-W3-6765" x="99.499985"/>
       <use xlink:href="#03-read-16-HiraginoSansGB-W3-6e90" x="199.499969"/>
       <use xlink:href="#03-read-16-HiraginoSansGB-W3-20" x="299.499954"/>
       <use xlink:href="#03-read-16-HiraginoSansGB-W3-2f" x="332.799942"/>
       <use xlink:href="#03-read-16-HiraginoSansGB-W3-20" x="380.199936"/>
       <use xlink:href="#03-read-16-HiraginoSansGB-W3-79bb" x="413.499924"/>
       <use xlink:href="#03-read-16-HiraginoSansGB-W3-6563" x="513.499908"/>
      </g>
     </g>
    </g>
    <g id="03-read-16-ytick_3">
     <g id="03-read-16-line2d_23"/>
     <g id="03-read-16-text_14">
      <!-- D 来源 / 连续 -->
      <g style="fill: #243a56" transform="translate(37.90875 290.315475) scale(0.12 -0.12)">
       <defs>
        <path id="03-read-16-HiraginoSansGB-W3-44" d="M 544 781 
C 544 461 531 269 493 19 
C 717 32 890 38 1197 38 
L 2278 38 
C 2918 38 3450 230 3834 608 
C 4275 1037 4518 1702 4518 2502 
C 4518 4032 3706 4902 2278 4902 
L 1197 4902 
C 890 4902 717 4909 493 4922 
C 531 4653 544 4467 544 4192 
L 544 781 
z
M 1043 486 
L 1043 4454 
L 2291 4454 
C 2816 4454 3213 4314 3494 4019 
C 3834 3680 4000 3168 4000 2502 
C 4000 1843 3814 1299 3462 941 
C 3174 634 2771 486 2266 486 
L 1043 486 
z
" transform="scale(0.015625)"/>
       </defs>
       <use xlink:href="#03-read-16-HiraginoSansGB-W3-44"/>
       <use xlink:href="#03-read-16-HiraginoSansGB-W3-20" x="75.799988"/>
       <use xlink:href="#03-read-16-HiraginoSansGB-W3-6765" x="109.099976"/>
       <use xlink:href="#03-read-16-HiraginoSansGB-W3-6e90" x="209.09996"/>
       <use xlink:href="#03-read-16-HiraginoSansGB-W3-20" x="309.099945"/>
       <use xlink:href="#03-read-16-HiraginoSansGB-W3-2f" x="342.399933"/>
       <use xlink:href="#03-read-16-HiraginoSansGB-W3-20" x="389.799927"/>
       <use xlink:href="#03-read-16-HiraginoSansGB-W3-8fde" x="423.099915"/>
       <use xlink:href="#03-read-16-HiraginoSansGB-W3-7eed" x="523.099899"/>
      </g>
     </g>
    </g>
    <g id="03-read-16-ytick_4">
     <g id="03-read-16-line2d_24"/>
     <g id="03-read-16-text_15">
      <!-- D 来源 / 离散 -->
      <g style="fill: #243a56" transform="translate(37.90875 376.66428) scale(0.12 -0.12)">
       <use xlink:href="#03-read-16-HiraginoSansGB-W3-44"/>
       <use xlink:href="#03-read-16-HiraginoSansGB-W3-20" x="75.799988"/>
       <use xlink:href="#03-read-16-HiraginoSansGB-W3-6765" x="109.099976"/>
       <use xlink:href="#03-read-16-HiraginoSansGB-W3-6e90" x="209.09996"/>
       <use xlink:href="#03-read-16-HiraginoSansGB-W3-20" x="309.099945"/>
       <use xlink:href="#03-read-16-HiraginoSansGB-W3-2f" x="342.399933"/>
       <use xlink:href="#03-read-16-HiraginoSansGB-W3-20" x="389.799927"/>
       <use xlink:href="#03-read-16-HiraginoSansGB-W3-79bb" x="423.099915"/>
       <use xlink:href="#03-read-16-HiraginoSansGB-W3-6563" x="523.099899"/>
      </g>
     </g>
    </g>
    <g id="03-read-16-ytick_5">
     <g id="03-read-16-line2d_25"/>
     <g id="03-read-16-text_16">
      <!-- MIXED 来源 / 连续 -->
      <g style="fill: #243a56" transform="translate(7.2 463.001835) scale(0.12 -0.12)">
       <defs>
        <path id="03-read-16-HiraginoSansGB-W3-4d" d="M 2554 710 
C 2682 422 2758 205 2790 38 
L 3245 38 
C 3296 250 3360 416 3488 704 
L 4794 3578 
C 4915 3840 5005 4096 5069 4358 
C 5043 4096 5024 3750 5024 3571 
L 5024 794 
C 5024 486 5011 256 4973 38 
L 5555 38 
C 5517 269 5504 442 5504 794 
L 5504 4141 
C 5504 4474 5517 4659 5555 4902 
L 4819 4902 
C 4781 4717 4710 4518 4602 4269 
L 3027 742 
L 1446 4269 
C 1350 4480 1274 4710 1235 4902 
L 493 4902 
C 531 4666 544 4461 544 4141 
L 544 794 
C 544 461 531 275 493 38 
L 1075 38 
C 1037 256 1024 454 1024 794 
L 1024 3533 
C 1024 3686 998 4083 979 4358 
C 1101 3968 1171 3770 1267 3565 
L 2554 710 
z
" transform="scale(0.015625)"/>
        <path id="03-read-16-HiraginoSansGB-W3-49" d="M 493 4902 
C 531 4672 544 4506 544 4186 
L 544 755 
C 544 435 531 275 493 38 
L 1101 38 
C 1062 256 1043 474 1043 755 
L 1043 4186 
C 1043 4454 1062 4691 1101 4902 
L 493 4902 
z
" transform="scale(0.015625)"/>
        <path id="03-read-16-HiraginoSansGB-W3-58" d="M 2304 2266 
L 3360 685 
C 3501 480 3571 358 3757 38 
L 4493 38 
C 4179 435 4026 640 3904 813 
L 2624 2611 
L 3750 4205 
C 3942 4474 4102 4685 4282 4902 
L 3558 4902 
C 3488 4730 3430 4634 3206 4294 
L 2323 2944 
L 1408 4294 
C 1235 4544 1126 4730 1050 4902 
L 333 4902 
C 531 4653 678 4454 858 4211 
L 1990 2611 
L 704 800 
C 570 614 486 493 134 38 
L 864 38 
C 973 250 1094 454 1248 691 
L 2304 2266 
z
" transform="scale(0.015625)"/>
        <path id="03-read-16-HiraginoSansGB-W3-45" d="M 3789 531 
C 3552 499 3373 486 3078 486 
L 1050 486 
L 1050 2368 
L 2733 2368 
C 3066 2368 3232 2355 3450 2323 
L 3450 2861 
C 3206 2829 3014 2816 2733 2816 
L 1050 2816 
L 1050 4454 
L 3021 4454 
C 3341 4454 3514 4442 3738 4410 
L 3738 4947 
C 3520 4915 3302 4902 3021 4902 
L 1197 4902 
C 890 4902 717 4909 493 4922 
C 531 4640 544 4467 544 4192 
L 544 781 
C 544 454 531 262 493 19 
C 710 32 890 38 1197 38 
L 3072 38 
C 3411 38 3584 26 3789 -6 
L 3789 531 
z
" transform="scale(0.015625)"/>
       </defs>
       <use xlink:href="#03-read-16-HiraginoSansGB-W3-4d"/>
       <use xlink:href="#03-read-16-HiraginoSansGB-W3-49" x="94.499985"/>
       <use xlink:href="#03-read-16-HiraginoSansGB-W3-58" x="119.399979"/>
       <use xlink:href="#03-read-16-HiraginoSansGB-W3-45" x="191.59996"/>
       <use xlink:href="#03-read-16-HiraginoSansGB-W3-44" x="255.899948"/>
       <use xlink:href="#03-read-16-HiraginoSansGB-W3-20" x="331.699936"/>
       <use xlink:href="#03-read-16-HiraginoSansGB-W3-6765" x="364.999924"/>
       <use xlink:href="#03-read-16-HiraginoSansGB-W3-6e90" x="464.999908"/>
       <use xlink:href="#03-read-16-HiraginoSansGB-W3-20" x="564.999893"/>
       <use xlink:href="#03-read-16-HiraginoSansGB-W3-2f" x="598.299881"/>
       <use xlink:href="#03-read-16-HiraginoSansGB-W3-20" x="645.699875"/>
       <use xlink:href="#03-read-16-HiraginoSansGB-W3-8fde" x="678.999863"/>
       <use xlink:href="#03-read-16-HiraginoSansGB-W3-7eed" x="778.999847"/>
      </g>
     </g>
    </g>
    <g id="03-read-16-ytick_6">
     <g id="03-read-16-line2d_26"/>
     <g id="03-read-16-text_17">
      <!-- MIXED 来源 / 离散 -->
      <g style="fill: #243a56" transform="translate(7.2 549.35064) scale(0.12 -0.12)">
       <use xlink:href="#03-read-16-HiraginoSansGB-W3-4d"/>
       <use xlink:href="#03-read-16-HiraginoSansGB-W3-49" x="94.499985"/>
       <use xlink:href="#03-read-16-HiraginoSansGB-W3-58" x="119.399979"/>
       <use xlink:href="#03-read-16-HiraginoSansGB-W3-45" x="191.59996"/>
       <use xlink:href="#03-read-16-HiraginoSansGB-W3-44" x="255.899948"/>
       <use xlink:href="#03-read-16-HiraginoSansGB-W3-20" x="331.699936"/>
       <use xlink:href="#03-read-16-HiraginoSansGB-W3-6765" x="364.999924"/>
       <use xlink:href="#03-read-16-HiraginoSansGB-W3-6e90" x="464.999908"/>
       <use xlink:href="#03-read-16-HiraginoSansGB-W3-20" x="564.999893"/>
       <use xlink:href="#03-read-16-HiraginoSansGB-W3-2f" x="598.299881"/>
       <use xlink:href="#03-read-16-HiraginoSansGB-W3-20" x="645.699875"/>
       <use xlink:href="#03-read-16-HiraginoSansGB-W3-79bb" x="678.999863"/>
       <use xlink:href="#03-read-16-HiraginoSansGB-W3-6563" x="778.999847"/>
      </g>
     </g>
    </g>
   </g>
   <g id="03-read-16-patch_3">
    <path d="M 120.68 78.153798 
L 287.783444 78.153798 
L 287.783444 93.263854 
L 120.68 93.263854 
z
" clip-path="url(#03-read-16-p1bff6ad17d)" style="fill: url(#03-read-16-hc447d1c890); stroke: #467bb5; stroke-width: 0.5; stroke-linejoin: miter"/>
   </g>
   <g id="03-read-16-patch_4">
    <path d="M 120.68 164.496978 
L 275.343234 164.496978 
L 275.343234 179.607034 
L 120.68 179.607034 
z
" clip-path="url(#03-read-16-p1bff6ad17d)" style="fill: url(#03-read-16-hc447d1c890); stroke: #467bb5; stroke-width: 0.5; stroke-linejoin: miter"/>
   </g>
   <g id="03-read-16-patch_5">
    <path d="M 120.68 250.840158 
L 307.799699 250.840158 
L 307.799699 265.950214 
L 120.68 265.950214 
z
" clip-path="url(#03-read-16-p1bff6ad17d)" style="fill: url(#03-read-16-hc447d1c890); stroke: #467bb5; stroke-width: 0.5; stroke-linejoin: miter"/>
   </g>
   <g id="03-read-16-patch_6">
    <path d="M 120.68 337.183338 
L 307.950359 337.183338 
L 307.950359 352.293394 
L 120.68 352.293394 
z
" clip-path="url(#03-read-16-p1bff6ad17d)" style="fill: url(#03-read-16-hc447d1c890); stroke: #467bb5; stroke-width: 0.5; stroke-linejoin: miter"/>
   </g>
   <g id="03-read-16-patch_7">
    <path d="M 120.68 423.526518 
L 292.970452 423.526518 
L 292.970452 438.636574 
L 120.68 438.636574 
z
" clip-path="url(#03-read-16-p1bff6ad17d)" style="fill: url(#03-read-16-hc447d1c890); stroke: #467bb5; stroke-width: 0.5; stroke-linejoin: miter"/>
   </g>
   <g id="03-read-16-patch_8">
    <path d="M 120.68 509.869698 
L 295.187306 509.869698 
L 295.187306 524.979754 
L 120.68 524.979754 
z
" clip-path="url(#03-read-16-p1bff6ad17d)" style="fill: url(#03-read-16-hc447d1c890); stroke: #467bb5; stroke-width: 0.5; stroke-linejoin: miter"/>
   </g>
   <g id="03-read-16-patch_9">
    <path d="M 120.68 96.285865 
L 288.558267 96.285865 
L 288.558267 111.395922 
L 120.68 111.395922 
z
" clip-path="url(#03-read-16-p1bff6ad17d)" style="fill: #467bb5; stroke: #467bb5; stroke-width: 0.5; stroke-linejoin: miter"/>
   </g>
   <g id="03-read-16-patch_10">
    <path d="M 120.68 182.629045 
L 276.656128 182.629045 
L 276.656128 197.739102 
L 120.68 197.739102 
z
" clip-path="url(#03-read-16-p1bff6ad17d)" style="fill: #467bb5; stroke: #467bb5; stroke-width: 0.5; stroke-linejoin: miter"/>
   </g>
   <g id="03-read-16-patch_11">
    <path d="M 120.68 268.972226 
L 310.360919 268.972226 
L 310.360919 284.082282 
L 120.68 284.082282 
z
" clip-path="url(#03-read-16-p1bff6ad17d)" style="fill: #467bb5; stroke: #467bb5; stroke-width: 0.5; stroke-linejoin: miter"/>
   </g>
   <g id="03-read-16-patch_12">
    <path d="M 120.68 355.315406 
L 308.18711 355.315406 
L 308.18711 370.425462 
L 120.68 370.425462 
z
" clip-path="url(#03-read-16-p1bff6ad17d)" style="fill: #467bb5; stroke: #467bb5; stroke-width: 0.5; stroke-linejoin: miter"/>
   </g>
   <g id="03-read-16-patch_13">
    <path d="M 120.68 441.658586 
L 298.674009 441.658586 
L 298.674009 456.768642 
L 120.68 456.768642 
z
" clip-path="url(#03-read-16-p1bff6ad17d)" style="fill: #467bb5; stroke: #467bb5; stroke-width: 0.5; stroke-linejoin: miter"/>
   </g>
   <g id="03-read-16-patch_14">
    <path d="M 120.68 528.001766 
L 295.854515 528.001766 
L 295.854515 543.111822 
L 120.68 543.111822 
z
" clip-path="url(#03-read-16-p1bff6ad17d)" style="fill: #467bb5; stroke: #467bb5; stroke-width: 0.5; stroke-linejoin: miter"/>
   </g>
   <g id="03-read-16-patch_15">
    <path d="M 120.68 114.417933 
L 415.263313 114.417933 
L 415.263313 129.52799 
L 120.68 129.52799 
z
" clip-path="url(#03-read-16-p1bff6ad17d)" style="fill: url(#03-read-16-hdea91fd583); stroke: #008477; stroke-width: 0.5; stroke-linejoin: miter"/>
   </g>
   <g id="03-read-16-patch_16">
    <path d="M 120.68 200.761113 
L 418.384127 200.761113 
L 418.384127 215.87117 
L 120.68 215.87117 
z
" clip-path="url(#03-read-16-p1bff6ad17d)" style="fill: url(#03-read-16-hdea91fd583); stroke: #008477; stroke-width: 0.5; stroke-linejoin: miter"/>
   </g>
   <g id="03-read-16-patch_17">
    <path d="M 120.68 287.104293 
L 421.978443 287.104293 
L 421.978443 302.21435 
L 120.68 302.21435 
z
" clip-path="url(#03-read-16-p1bff6ad17d)" style="fill: url(#03-read-16-hdea91fd583); stroke: #008477; stroke-width: 0.5; stroke-linejoin: miter"/>
   </g>
   <g id="03-read-16-patch_18">
    <path d="M 120.68 373.447473 
L 414.251738 373.447473 
L 414.251738 388.55753 
L 120.68 388.55753 
z
" clip-path="url(#03-read-16-p1bff6ad17d)" style="fill: url(#03-read-16-hdea91fd583); stroke: #008477; stroke-width: 0.5; stroke-linejoin: miter"/>
   </g>
   <g id="03-read-16-patch_19">
    <path d="M 120.68 459.790654 
L 374.628163 459.790654 
L 374.628163 474.90071 
L 120.68 474.90071 
z
" clip-path="url(#03-read-16-p1bff6ad17d)" style="fill: url(#03-read-16-hdea91fd583); stroke: #008477; stroke-width: 0.5; stroke-linejoin: miter"/>
   </g>
   <g id="03-read-16-patch_20">
    <path d="M 120.68 546.133834 
L 418.104329 546.133834 
L 418.104329 561.24389 
L 120.68 561.24389 
z
" clip-path="url(#03-read-16-p1bff6ad17d)" style="fill: url(#03-read-16-hdea91fd583); stroke: #008477; stroke-width: 0.5; stroke-linejoin: miter"/>
   </g>
   <g id="03-read-16-patch_21">
    <path d="M 120.68 132.550001 
L 415.349404 132.550001 
L 415.349404 147.660058 
L 120.68 147.660058 
z
" clip-path="url(#03-read-16-p1bff6ad17d)" style="fill: #008477; stroke: #008477; stroke-width: 0.5; stroke-linejoin: miter"/>
   </g>
   <g id="03-read-16-patch_22">
    <path d="M 120.68 218.893181 
L 419.524838 218.893181 
L 419.524838 234.003238 
L 120.68 234.003238 
z
" clip-path="url(#03-read-16-p1bff6ad17d)" style="fill: #008477; stroke: #008477; stroke-width: 0.5; stroke-linejoin: miter"/>
   </g>
   <g id="03-read-16-patch_23">
    <path d="M 120.68 305.236361 
L 425.960171 305.236361 
L 425.960171 320.346418 
L 120.68 320.346418 
z
" clip-path="url(#03-read-16-p1bff6ad17d)" style="fill: #008477; stroke: #008477; stroke-width: 0.5; stroke-linejoin: miter"/>
   </g>
   <g id="03-read-16-patch_24">
    <path d="M 120.68 391.579541 
L 417.458644 391.579541 
L 417.458644 406.689598 
L 120.68 406.689598 
z
" clip-path="url(#03-read-16-p1bff6ad17d)" style="fill: #008477; stroke: #008477; stroke-width: 0.5; stroke-linejoin: miter"/>
   </g>
   <g id="03-read-16-patch_25">
    <path d="M 120.68 477.922721 
L 377.232428 477.922721 
L 377.232428 493.032778 
L 120.68 493.032778 
z
" clip-path="url(#03-read-16-p1bff6ad17d)" style="fill: #008477; stroke: #008477; stroke-width: 0.5; stroke-linejoin: miter"/>
   </g>
   <g id="03-read-16-patch_26">
    <path d="M 120.68 564.265901 
L 421.763215 564.265901 
L 421.763215 579.375958 
L 120.68 579.375958 
z
" clip-path="url(#03-read-16-p1bff6ad17d)" style="fill: #008477; stroke: #008477; stroke-width: 0.5; stroke-linejoin: miter"/>
   </g>
   <g id="03-read-16-patch_27">
    <path d="M 120.68 596.428736 
L 508.091385 596.428736 
" style="fill: none; stroke: #d7e0eb; stroke-width: 0.8; stroke-linejoin: miter; stroke-linecap: square"/>
   </g>
   <g id="03-read-16-text_18">
    <!-- 0.7764 -->
    <g style="fill: #405570" transform="translate(292.432381 88.518201) scale(0.1 -0.1)">
     <defs>
      <path id="03-read-16-HiraginoSansGB-W3-37" d="M 3789 4934 
C 3565 4909 3424 4902 3046 4902 
L 1126 4902 
C 819 4902 646 4915 397 4947 
L 397 4403 
C 640 4435 819 4448 1120 4448 
L 3200 4448 
C 2630 3674 2355 3232 2048 2592 
C 1651 1773 1478 1197 1274 38 
L 1882 38 
C 1984 781 2054 1107 2221 1638 
C 2470 2419 2739 2963 3315 3853 
C 3565 4237 3674 4378 3789 4467 
L 3789 4934 
z
" transform="scale(0.015625)"/>
     </defs>
     <use xlink:href="#03-read-16-HiraginoSansGB-W3-30"/>
     <use xlink:href="#03-read-16-HiraginoSansGB-W3-2e" x="65.699997"/>
     <use xlink:href="#03-read-16-HiraginoSansGB-W3-37" x="89.699982"/>
     <use xlink:href="#03-read-16-HiraginoSansGB-W3-37" x="155.399979"/>
     <use xlink:href="#03-read-16-HiraginoSansGB-W3-36" x="221.099976"/>
     <use xlink:href="#03-read-16-HiraginoSansGB-W3-34" x="286.799973"/>
    </g>
   </g>
   <g id="03-read-16-text_19">
    <!-- 0.7186 -->
    <g style="fill: #405570" transform="translate(279.992171 174.861381) scale(0.1 -0.1)">
     <use xlink:href="#03-read-16-HiraginoSansGB-W3-30"/>
     <use xlink:href="#03-read-16-HiraginoSansGB-W3-2e" x="65.699997"/>
     <use xlink:href="#03-read-16-HiraginoSansGB-W3-37" x="89.699982"/>
     <use xlink:href="#03-read-16-HiraginoSansGB-W3-31" x="155.399979"/>
     <use xlink:href="#03-read-16-HiraginoSansGB-W3-38" x="221.099976"/>
     <use xlink:href="#03-read-16-HiraginoSansGB-W3-36" x="286.799973"/>
    </g>
   </g>
   <g id="03-read-16-text_20">
    <!-- 0.8694 -->
    <g style="fill: #405570" transform="translate(312.448636 261.204561) scale(0.1 -0.1)">
     <defs>
      <path id="03-read-16-HiraginoSansGB-W3-39" d="M 416 934 
C 582 595 685 448 864 301 
C 1139 70 1498 -45 1939 -45 
C 2528 -45 2950 147 3270 550 
C 3616 979 3802 1702 3802 2592 
C 3802 3142 3744 3578 3616 3923 
C 3373 4589 2784 4998 2067 4998 
C 1018 4998 333 4333 333 3315 
C 333 2861 493 2438 768 2163 
C 1043 1888 1446 1741 1920 1741 
C 2400 1741 2758 1882 3085 2189 
C 3200 2310 3251 2374 3341 2547 
C 3328 2221 3309 1958 3283 1798 
C 3232 1498 3130 1184 3008 979 
C 2771 576 2438 390 1978 390 
C 1626 390 1350 493 1171 691 
C 1069 800 1018 890 941 1101 
L 416 934 
z
M 1978 2170 
C 1280 2170 838 2624 838 3347 
C 838 3597 902 3853 1005 4019 
C 1216 4358 1600 4563 2022 4563 
C 2707 4563 3219 4045 3219 3354 
C 3219 2675 2688 2170 1978 2170 
z
" transform="scale(0.015625)"/>
     </defs>
     <use xlink:href="#03-read-16-HiraginoSansGB-W3-30"/>
     <use xlink:href="#03-read-16-HiraginoSansGB-W3-2e" x="65.699997"/>
     <use xlink:href="#03-read-16-HiraginoSansGB-W3-38" x="89.699982"/>
     <use xlink:href="#03-read-16-HiraginoSansGB-W3-36" x="155.399979"/>
     <use xlink:href="#03-read-16-HiraginoSansGB-W3-39" x="221.099976"/>
     <use xlink:href="#03-read-16-HiraginoSansGB-W3-34" x="286.799973"/>
    </g>
   </g>
   <g id="03-read-16-text_21">
    <!-- 0.8701 -->
    <g style="fill: #405570" transform="translate(312.599296 347.547741) scale(0.1 -0.1)">
     <use xlink:href="#03-read-16-HiraginoSansGB-W3-30"/>
     <use xlink:href="#03-read-16-HiraginoSansGB-W3-2e" x="65.699997"/>
     <use xlink:href="#03-read-16-HiraginoSansGB-W3-38" x="89.699982"/>
     <use xlink:href="#03-read-16-HiraginoSansGB-W3-37" x="155.399979"/>
     <use xlink:href="#03-read-16-HiraginoSansGB-W3-30" x="221.099976"/>
     <use xlink:href="#03-read-16-HiraginoSansGB-W3-31" x="286.799973"/>
    </g>
   </g>
   <g id="03-read-16-text_22">
    <!-- 0.8005 -->
    <g style="fill: #405570" transform="translate(297.619389 433.890921) scale(0.1 -0.1)">
     <use xlink:href="#03-read-16-HiraginoSansGB-W3-30"/>
     <use xlink:href="#03-read-16-HiraginoSansGB-W3-2e" x="65.699997"/>
     <use xlink:href="#03-read-16-HiraginoSansGB-W3-38" x="89.699982"/>
     <use xlink:href="#03-read-16-HiraginoSansGB-W3-30" x="155.399979"/>
     <use xlink:href="#03-read-16-HiraginoSansGB-W3-30" x="221.099976"/>
     <use xlink:href="#03-read-16-HiraginoSansGB-W3-35" x="286.799973"/>
    </g>
   </g>
   <g id="03-read-16-text_23">
    <!-- 0.8108 -->
    <g style="fill: #405570" transform="translate(299.836243 520.234101) scale(0.1 -0.1)">
     <use xlink:href="#03-read-16-HiraginoSansGB-W3-30"/>
     <use xlink:href="#03-read-16-HiraginoSansGB-W3-2e" x="65.699997"/>
     <use xlink:href="#03-read-16-HiraginoSansGB-W3-38" x="89.699982"/>
     <use xlink:href="#03-read-16-HiraginoSansGB-W3-31" x="155.399979"/>
     <use xlink:href="#03-read-16-HiraginoSansGB-W3-30" x="221.099976"/>
     <use xlink:href="#03-read-16-HiraginoSansGB-W3-38" x="286.799973"/>
    </g>
   </g>
   <g id="03-read-16-text_24">
    <!-- 0.7800 -->
    <g style="fill: #467bb5" transform="translate(293.207203 106.650269) scale(0.1 -0.1)">
     <use xlink:href="#03-read-16-HiraginoSansGB-W3-30"/>
     <use xlink:href="#03-read-16-HiraginoSansGB-W3-2e" x="65.699997"/>
     <use xlink:href="#03-read-16-HiraginoSansGB-W3-37" x="89.699982"/>
     <use xlink:href="#03-read-16-HiraginoSansGB-W3-38" x="155.399979"/>
     <use xlink:href="#03-read-16-HiraginoSansGB-W3-30" x="221.099976"/>
     <use xlink:href="#03-read-16-HiraginoSansGB-W3-30" x="286.799973"/>
    </g>
   </g>
   <g id="03-read-16-text_25">
    <!-- 0.7247 -->
    <g style="fill: #467bb5" transform="translate(281.305065 192.993449) scale(0.1 -0.1)">
     <use xlink:href="#03-read-16-HiraginoSansGB-W3-30"/>
     <use xlink:href="#03-read-16-HiraginoSansGB-W3-2e" x="65.699997"/>
     <use xlink:href="#03-read-16-HiraginoSansGB-W3-37" x="89.699982"/>
     <use xlink:href="#03-read-16-HiraginoSansGB-W3-32" x="155.399979"/>
     <use xlink:href="#03-read-16-HiraginoSansGB-W3-34" x="221.099976"/>
     <use xlink:href="#03-read-16-HiraginoSansGB-W3-37" x="287.199966"/>
    </g>
   </g>
   <g id="03-read-16-text_26">
    <!-- 0.8813 -->
    <g style="fill: #467bb5" transform="translate(315.009855 279.336629) scale(0.1 -0.1)">
     <defs>
      <path id="03-read-16-HiraginoSansGB-W3-33" d="M 288 1088 
C 403 762 480 621 614 467 
C 922 122 1402 -58 2048 -58 
C 3117 -58 3782 480 3782 1344 
C 3782 1754 3635 2093 3354 2310 
C 3136 2477 2938 2547 2566 2586 
C 2861 2643 3002 2694 3168 2816 
C 3450 3014 3616 3360 3616 3750 
C 3616 4518 3027 5005 2099 5005 
C 1542 5005 1107 4845 826 4538 
C 666 4371 589 4224 480 3898 
L 1011 3782 
C 1082 4019 1126 4122 1216 4237 
C 1395 4467 1677 4576 2067 4576 
C 2714 4576 3098 4256 3098 3706 
C 3098 3475 3002 3238 2842 3085 
C 2643 2886 2330 2790 1907 2790 
C 1734 2790 1581 2803 1434 2829 
L 1434 2323 
C 1606 2342 1766 2355 1946 2355 
C 2797 2355 3251 2010 3251 1376 
C 3251 768 2784 384 2042 384 
C 1715 384 1434 461 1216 614 
C 1018 755 928 890 826 1222 
L 288 1088 
z
" transform="scale(0.015625)"/>
     </defs>
     <use xlink:href="#03-read-16-HiraginoSansGB-W3-30"/>
     <use xlink:href="#03-read-16-HiraginoSansGB-W3-2e" x="65.699997"/>
     <use xlink:href="#03-read-16-HiraginoSansGB-W3-38" x="89.699982"/>
     <use xlink:href="#03-read-16-HiraginoSansGB-W3-38" x="155.399979"/>
     <use xlink:href="#03-read-16-HiraginoSansGB-W3-31" x="221.099976"/>
     <use xlink:href="#03-read-16-HiraginoSansGB-W3-33" x="286.799973"/>
    </g>
   </g>
   <g id="03-read-16-text_27">
    <!-- 0.8712 -->
    <g style="fill: #467bb5" transform="translate(312.836047 365.679809) scale(0.1 -0.1)">
     <use xlink:href="#03-read-16-HiraginoSansGB-W3-30"/>
     <use xlink:href="#03-read-16-HiraginoSansGB-W3-2e" x="65.699997"/>
     <use xlink:href="#03-read-16-HiraginoSansGB-W3-38" x="89.699982"/>
     <use xlink:href="#03-read-16-HiraginoSansGB-W3-37" x="155.399979"/>
     <use xlink:href="#03-read-16-HiraginoSansGB-W3-31" x="221.099976"/>
     <use xlink:href="#03-read-16-HiraginoSansGB-W3-32" x="286.799973"/>
    </g>
   </g>
   <g id="03-read-16-text_28">
    <!-- 0.8270 -->
    <g style="fill: #467bb5" transform="translate(303.322945 452.022989) scale(0.1 -0.1)">
     <use xlink:href="#03-read-16-HiraginoSansGB-W3-30"/>
     <use xlink:href="#03-read-16-HiraginoSansGB-W3-2e" x="65.699997"/>
     <use xlink:href="#03-read-16-HiraginoSansGB-W3-38" x="89.699982"/>
     <use xlink:href="#03-read-16-HiraginoSansGB-W3-32" x="155.399979"/>
     <use xlink:href="#03-read-16-HiraginoSansGB-W3-37" x="221.099976"/>
     <use xlink:href="#03-read-16-HiraginoSansGB-W3-30" x="286.799973"/>
    </g>
   </g>
   <g id="03-read-16-text_29">
    <!-- 0.8139 -->
    <g style="fill: #467bb5" transform="translate(300.503451 538.366169) scale(0.1 -0.1)">
     <use xlink:href="#03-read-16-HiraginoSansGB-W3-30"/>
     <use xlink:href="#03-read-16-HiraginoSansGB-W3-2e" x="65.699997"/>
     <use xlink:href="#03-read-16-HiraginoSansGB-W3-38" x="89.699982"/>
     <use xlink:href="#03-read-16-HiraginoSansGB-W3-31" x="155.399979"/>
     <use xlink:href="#03-read-16-HiraginoSansGB-W3-33" x="221.099976"/>
     <use xlink:href="#03-read-16-HiraginoSansGB-W3-39" x="286.799973"/>
    </g>
   </g>
   <g id="03-read-16-text_30">
    <!-- 1.3687 -->
    <g style="fill: #405570" transform="translate(419.912249 124.782336) scale(0.1 -0.1)">
     <use xlink:href="#03-read-16-HiraginoSansGB-W3-31"/>
     <use xlink:href="#03-read-16-HiraginoSansGB-W3-2e" x="65.699997"/>
     <use xlink:href="#03-read-16-HiraginoSansGB-W3-33" x="89.699982"/>
     <use xlink:href="#03-read-16-HiraginoSansGB-W3-36" x="155.399979"/>
     <use xlink:href="#03-read-16-HiraginoSansGB-W3-38" x="221.099976"/>
     <use xlink:href="#03-read-16-HiraginoSansGB-W3-37" x="286.799973"/>
    </g>
   </g>
   <g id="03-read-16-text_31">
    <!-- 1.3832 -->
    <g style="fill: #405570" transform="translate(423.033063 211.125517) scale(0.1 -0.1)">
     <use xlink:href="#03-read-16-HiraginoSansGB-W3-31"/>
     <use xlink:href="#03-read-16-HiraginoSansGB-W3-2e" x="65.699997"/>
     <use xlink:href="#03-read-16-HiraginoSansGB-W3-33" x="89.699982"/>
     <use xlink:href="#03-read-16-HiraginoSansGB-W3-38" x="155.399979"/>
     <use xlink:href="#03-read-16-HiraginoSansGB-W3-33" x="221.099976"/>
     <use xlink:href="#03-read-16-HiraginoSansGB-W3-32" x="286.799973"/>
    </g>
   </g>
   <g id="03-read-16-text_32">
    <!-- 1.3999 -->
    <g style="fill: #405570" transform="translate(426.62738 297.468697) scale(0.1 -0.1)">
     <use xlink:href="#03-read-16-HiraginoSansGB-W3-31"/>
     <use xlink:href="#03-read-16-HiraginoSansGB-W3-2e" x="65.699997"/>
     <use xlink:href="#03-read-16-HiraginoSansGB-W3-33" x="89.699982"/>
     <use xlink:href="#03-read-16-HiraginoSansGB-W3-39" x="155.399979"/>
     <use xlink:href="#03-read-16-HiraginoSansGB-W3-39" x="221.099976"/>
     <use xlink:href="#03-read-16-HiraginoSansGB-W3-39" x="286.799973"/>
    </g>
   </g>
   <g id="03-read-16-text_33">
    <!-- 1.3640 -->
    <g style="fill: #405570" transform="translate(418.900675 383.811877) scale(0.1 -0.1)">
     <use xlink:href="#03-read-16-HiraginoSansGB-W3-31"/>
     <use xlink:href="#03-read-16-HiraginoSansGB-W3-2e" x="65.699997"/>
     <use xlink:href="#03-read-16-HiraginoSansGB-W3-33" x="89.699982"/>
     <use xlink:href="#03-read-16-HiraginoSansGB-W3-36" x="155.399979"/>
     <use xlink:href="#03-read-16-HiraginoSansGB-W3-34" x="221.099976"/>
     <use xlink:href="#03-read-16-HiraginoSansGB-W3-30" x="287.199966"/>
    </g>
   </g>
   <g id="03-read-16-text_34">
    <!-- 1.1799 -->
    <g style="fill: #405570" transform="translate(379.277099 470.155057) scale(0.1 -0.1)">
     <use xlink:href="#03-read-16-HiraginoSansGB-W3-31"/>
     <use xlink:href="#03-read-16-HiraginoSansGB-W3-2e" x="65.699997"/>
     <use xlink:href="#03-read-16-HiraginoSansGB-W3-31" x="89.699982"/>
     <use xlink:href="#03-read-16-HiraginoSansGB-W3-37" x="155.399979"/>
     <use xlink:href="#03-read-16-HiraginoSansGB-W3-39" x="221.099976"/>
     <use xlink:href="#03-read-16-HiraginoSansGB-W3-39" x="286.799973"/>
    </g>
   </g>
   <g id="03-read-16-text_35">
    <!-- 1.3819 -->
    <g style="fill: #405570" transform="translate(422.753266 556.498237) scale(0.1 -0.1)">
     <use xlink:href="#03-read-16-HiraginoSansGB-W3-31"/>
     <use xlink:href="#03-read-16-HiraginoSansGB-W3-2e" x="65.699997"/>
     <use xlink:href="#03-read-16-HiraginoSansGB-W3-33" x="89.699982"/>
     <use xlink:href="#03-read-16-HiraginoSansGB-W3-38" x="155.399979"/>
     <use xlink:href="#03-read-16-HiraginoSansGB-W3-31" x="221.099976"/>
     <use xlink:href="#03-read-16-HiraginoSansGB-W3-39" x="286.799973"/>
    </g>
   </g>
   <g id="03-read-16-text_36">
    <!-- 1.3691 -->
    <g style="fill: #008477" transform="translate(419.998341 142.914404) scale(0.1 -0.1)">
     <use xlink:href="#03-read-16-HiraginoSansGB-W3-31"/>
     <use xlink:href="#03-read-16-HiraginoSansGB-W3-2e" x="65.699997"/>
     <use xlink:href="#03-read-16-HiraginoSansGB-W3-33" x="89.699982"/>
     <use xlink:href="#03-read-16-HiraginoSansGB-W3-36" x="155.399979"/>
     <use xlink:href="#03-read-16-HiraginoSansGB-W3-39" x="221.099976"/>
     <use xlink:href="#03-read-16-HiraginoSansGB-W3-31" x="286.799973"/>
    </g>
   </g>
   <g id="03-read-16-text_37">
    <!-- 1.3885 -->
    <g style="fill: #008477" transform="translate(424.173774 229.257584) scale(0.1 -0.1)">
     <use xlink:href="#03-read-16-HiraginoSansGB-W3-31"/>
     <use xlink:href="#03-read-16-HiraginoSansGB-W3-2e" x="65.699997"/>
     <use xlink:href="#03-read-16-HiraginoSansGB-W3-33" x="89.699982"/>
     <use xlink:href="#03-read-16-HiraginoSansGB-W3-38" x="155.399979"/>
     <use xlink:href="#03-read-16-HiraginoSansGB-W3-38" x="221.099976"/>
     <use xlink:href="#03-read-16-HiraginoSansGB-W3-35" x="286.799973"/>
    </g>
   </g>
   <g id="03-read-16-text_38">
    <!-- 1.4184 -->
    <g style="fill: #008477" transform="translate(430.609108 315.600764) scale(0.1 -0.1)">
     <use xlink:href="#03-read-16-HiraginoSansGB-W3-31"/>
     <use xlink:href="#03-read-16-HiraginoSansGB-W3-2e" x="65.699997"/>
     <use xlink:href="#03-read-16-HiraginoSansGB-W3-34" x="89.699982"/>
     <use xlink:href="#03-read-16-HiraginoSansGB-W3-31" x="155.799973"/>
     <use xlink:href="#03-read-16-HiraginoSansGB-W3-38" x="221.499969"/>
     <use xlink:href="#03-read-16-HiraginoSansGB-W3-34" x="287.199966"/>
    </g>
   </g>
   <g id="03-read-16-text_39">
    <!-- 1.3789 -->
    <g style="fill: #008477" transform="translate(422.10758 401.943945) scale(0.1 -0.1)">
     <use xlink:href="#03-read-16-HiraginoSansGB-W3-31"/>
     <use xlink:href="#03-read-16-HiraginoSansGB-W3-2e" x="65.699997"/>
     <use xlink:href="#03-read-16-HiraginoSansGB-W3-33" x="89.699982"/>
     <use xlink:href="#03-read-16-HiraginoSansGB-W3-37" x="155.399979"/>
     <use xlink:href="#03-read-16-HiraginoSansGB-W3-38" x="221.099976"/>
     <use xlink:href="#03-read-16-HiraginoSansGB-W3-39" x="286.799973"/>
    </g>
   </g>
   <g id="03-read-16-text_40">
    <!-- 1.1920 -->
    <g style="fill: #008477" transform="translate(381.881365 488.287125) scale(0.1 -0.1)">
     <use xlink:href="#03-read-16-HiraginoSansGB-W3-31"/>
     <use xlink:href="#03-read-16-HiraginoSansGB-W3-2e" x="65.699997"/>
     <use xlink:href="#03-read-16-HiraginoSansGB-W3-31" x="89.699982"/>
     <use xlink:href="#03-read-16-HiraginoSansGB-W3-39" x="155.399979"/>
     <use xlink:href="#03-read-16-HiraginoSansGB-W3-32" x="221.099976"/>
     <use xlink:href="#03-read-16-HiraginoSansGB-W3-30" x="286.799973"/>
    </g>
   </g>
   <g id="03-read-16-text_41">
    <!-- 1.3989 -->
    <g style="fill: #008477" transform="translate(426.412151 574.630305) scale(0.1 -0.1)">
     <use xlink:href="#03-read-16-HiraginoSansGB-W3-31"/>
     <use xlink:href="#03-read-16-HiraginoSansGB-W3-2e" x="65.699997"/>
     <use xlink:href="#03-read-16-HiraginoSansGB-W3-33" x="89.699982"/>
     <use xlink:href="#03-read-16-HiraginoSansGB-W3-39" x="155.399979"/>
     <use xlink:href="#03-read-16-HiraginoSansGB-W3-38" x="221.099976"/>
     <use xlink:href="#03-read-16-HiraginoSansGB-W3-39" x="286.799973"/>
    </g>
   </g>
   <g id="03-read-16-text_42">
    <!-- P 侧 -->
    <g style="fill: #192c46" transform="translate(299.423192 42.149543) scale(0.15 -0.15)">
     <defs>
      <path id="03-read-16-HiraginoSansGB-W3-4fa7" d="M 1299 3846 
C 1498 4288 1568 4480 1702 4896 
C 1760 5094 1779 5139 1811 5216 
L 1395 5350 
C 1376 5178 1325 4979 1222 4678 
C 954 3891 589 3238 90 2643 
C 218 2515 282 2432 352 2298 
C 589 2611 762 2906 934 3277 
C 915 2566 915 2566 915 2298 
L 915 333 
C 915 -32 902 -256 877 -474 
L 1325 -474 
C 1306 -250 1299 -64 1299 339 
L 1299 3846 
z
M 4851 1043 
C 4826 1203 4819 1395 4819 1715 
L 4819 4205 
C 4819 4531 4826 4698 4858 4877 
L 4390 4877 
C 4416 4691 4422 4512 4422 4205 
L 4422 1715 
C 4422 1389 4416 1222 4384 1043 
L 4851 1043 
z
M 2336 1050 
C 2330 1197 2323 1363 2323 1626 
L 2323 4730 
L 3584 4730 
L 3584 1645 
C 3584 1440 3571 1171 3558 1050 
L 3981 1050 
C 3968 1235 3962 1427 3962 1658 
L 3962 4506 
C 3962 4736 3968 4947 3981 5094 
C 3840 5075 3693 5069 3405 5069 
L 2483 5069 
C 2208 5069 2099 5075 1920 5088 
C 1939 4909 1946 4736 1946 4486 
L 1946 1626 
C 1946 1389 1939 1210 1926 1050 
L 2336 1050 
z
M 2989 557 
C 3322 282 3610 -32 3891 -403 
L 4160 -102 
C 3968 141 3744 365 3219 832 
L 2989 557 
z
M 5440 5184 
C 5466 4973 5478 4774 5478 4435 
L 5478 147 
C 5478 6 5427 -19 5165 -19 
C 4992 -19 4826 -6 4525 32 
C 4582 -102 4602 -179 4627 -371 
C 4851 -384 4934 -384 5152 -384 
C 5734 -384 5875 -288 5875 102 
L 5875 4435 
C 5875 4787 5888 5011 5914 5184 
L 5440 5184 
z
M 2726 4262 
C 2752 4032 2758 3910 2758 3437 
L 2758 2381 
C 2758 1466 2701 1082 2522 742 
C 2336 403 2099 166 1619 -154 
C 1779 -275 1811 -320 1914 -480 
C 2394 -115 2630 128 2816 442 
C 3072 864 3142 1299 3142 2381 
L 3142 3450 
C 3142 3802 3149 4058 3168 4262 
L 2726 4262 
z
" transform="scale(0.015625)"/>
     </defs>
     <use xlink:href="#03-read-16-HiraginoSansGB-W3-50"/>
     <use xlink:href="#03-read-16-HiraginoSansGB-W3-20" x="66.199997"/>
     <use xlink:href="#03-read-16-HiraginoSansGB-W3-4fa7" x="99.499985"/>
    </g>
   </g>
   <g id="03-read-16-legend_1">
    <g id="03-read-16-patch_28">
     <path d="M 214.252177 669.581968 
L 236.252177 669.581968 
L 236.252177 661.881968 
L 214.252177 661.881968 
z
" style="fill: url(#03-read-16-hc447d1c890); stroke: #467bb5; stroke-width: 0.5; stroke-linejoin: miter"/>
    </g>
    <g id="03-read-16-text_43">
     <!-- TP2 独立池 -->
     <g style="fill: #192c46" transform="translate(245.052177 669.581968) scale(0.11 -0.11)">
      <defs>
       <path id="03-read-16-HiraginoSansGB-W3-54" d="M 2323 4454 
L 3315 4454 
C 3674 4454 3859 4442 4070 4410 
L 4070 4947 
C 3859 4915 3686 4902 3315 4902 
L 819 4902 
C 480 4902 314 4915 90 4947 
L 90 4410 
C 301 4442 499 4454 826 4454 
L 1824 4454 
L 1824 762 
C 1824 461 1811 237 1773 38 
L 2374 38 
C 2336 269 2323 448 2323 762 
L 2323 4454 
z
" transform="scale(0.015625)"/>
       <path id="03-read-16-HiraginoSansGB-W3-72ec" d="M 998 5101 
C 954 5152 928 5184 928 5184 
L 589 4934 
C 851 4717 1018 4499 1203 4134 
C 813 3840 666 3744 224 3533 
C 320 3430 384 3328 454 3187 
C 845 3411 1050 3552 1357 3789 
C 1466 3494 1542 3238 1594 2944 
C 1248 2355 851 1939 243 1542 
C 333 1459 403 1350 467 1203 
C 1037 1632 1376 1984 1670 2438 
C 1702 2125 1709 1907 1709 1542 
C 1709 902 1658 384 1568 154 
C 1517 6 1440 -51 1312 -51 
C 1139 -51 883 6 595 102 
C 640 -64 653 -166 659 -333 
C 934 -397 1139 -429 1325 -429 
C 1574 -429 1715 -371 1830 -230 
C 2010 6 2099 570 2099 1472 
C 2099 2522 1958 3366 1670 4051 
C 1958 4307 2195 4557 2477 4915 
C 2522 4973 2528 4979 2560 5018 
L 2208 5280 
C 2086 5011 1837 4698 1504 4397 
C 1402 4570 1274 4755 1190 4864 
L 998 5101 
z
M 4307 1722 
L 5178 1722 
C 5472 1722 5619 1715 5798 1696 
C 5779 1862 5773 2029 5773 2330 
L 5773 3488 
C 5773 3763 5779 3923 5798 4096 
C 5594 4070 5434 4064 5190 4064 
L 4307 4064 
L 4307 4621 
C 4307 4890 4320 5037 4352 5216 
L 3872 5216 
C 3910 5011 3917 4883 3917 4614 
L 3917 4064 
L 3142 4064 
C 2893 4064 2720 4070 2515 4096 
C 2534 3910 2541 3744 2541 3456 
L 2541 2317 
C 2541 2054 2534 1888 2522 1696 
C 2701 1715 2867 1722 3136 1722 
L 3917 1722 
L 3917 256 
C 3219 173 2637 122 2413 122 
C 2349 122 2323 122 2253 128 
L 2349 -333 
C 2483 -301 2573 -288 2694 -275 
C 4038 -115 4755 -6 5562 160 
C 5638 -19 5677 -134 5779 -474 
L 6157 -282 
C 5869 512 5734 806 5389 1395 
L 5030 1274 
C 5222 934 5280 826 5434 486 
C 5030 403 4678 346 4307 301 
L 4307 1722 
z
M 3917 2086 
L 2925 2086 
L 2925 3699 
L 3917 3699 
L 3917 2086 
z
M 4307 2086 
L 4307 3699 
L 5382 3699 
L 5382 2086 
L 4307 2086 
z
" transform="scale(0.015625)"/>
       <path id="03-read-16-HiraginoSansGB-W3-7acb" d="M 3168 4237 
L 3469 4429 
C 3315 4691 3104 4966 2784 5325 
L 2432 5101 
C 2739 4755 2861 4589 3066 4237 
L 1312 4237 
C 992 4237 781 4250 602 4282 
L 602 3802 
C 806 3827 1018 3840 1312 3840 
L 5062 3840 
C 5370 3840 5587 3827 5798 3802 
L 5798 4282 
C 5600 4250 5382 4237 5062 4237 
L 3168 4237 
z
M 1088 346 
C 774 346 557 358 352 384 
L 352 -102 
C 557 -70 768 -58 1088 -58 
L 5350 -58 
C 5664 -58 5850 -70 6054 -102 
L 6054 384 
C 5862 358 5645 346 5350 346 
L 3872 346 
C 4198 1043 4486 1786 4730 2579 
C 4922 3200 4922 3200 4986 3354 
L 4493 3520 
C 4397 2810 3885 1299 3418 346 
L 1088 346 
z
M 1459 3296 
C 1766 2528 1990 1715 2157 762 
L 2573 922 
C 2368 1971 2189 2611 1882 3450 
L 1459 3296 
z
" transform="scale(0.015625)"/>
       <path id="03-read-16-HiraginoSansGB-W3-6c60" d="M 3872 3520 
L 2874 3181 
L 2874 4038 
C 2874 4301 2886 4499 2912 4672 
L 2432 4672 
C 2458 4474 2470 4288 2470 4019 
L 2470 3046 
L 2330 2995 
C 2131 2931 1971 2886 1779 2861 
L 1901 2413 
C 2016 2470 2157 2528 2426 2624 
L 2470 2637 
L 2470 326 
C 2470 77 2534 -38 2720 -134 
C 2918 -230 3290 -262 4160 -262 
C 5165 -262 5568 -211 5754 -58 
C 5926 83 6022 358 6112 986 
C 5939 1024 5869 1050 5702 1146 
C 5658 614 5587 352 5472 256 
C 5331 154 4992 115 4134 115 
C 3533 115 3155 134 3034 179 
C 2912 218 2874 275 2874 422 
L 2874 2778 
L 3872 3123 
L 3872 1504 
C 3872 1242 3859 1069 3834 883 
L 4307 883 
C 4282 1056 4269 1229 4269 1498 
L 4269 3258 
L 5408 3654 
C 5395 2739 5344 2035 5280 1914 
C 5242 1837 5158 1798 5024 1798 
C 4877 1798 4736 1818 4525 1869 
C 4582 1709 4608 1619 4634 1466 
C 4794 1440 4902 1434 5069 1434 
C 5261 1434 5344 1453 5446 1504 
C 5555 1562 5638 1709 5683 1907 
C 5728 2150 5779 2970 5811 4205 
C 5690 4147 5523 4077 5312 4006 
L 4269 3654 
L 4269 4685 
C 4269 4941 4282 5114 4307 5293 
L 3834 5293 
C 3859 5107 3872 4934 3872 4691 
L 3872 3520 
z
M 608 4934 
C 1030 4646 1222 4480 1600 4064 
L 1875 4378 
C 1523 4736 1274 4947 870 5222 
L 608 4934 
z
M 269 3354 
C 736 3066 992 2867 1350 2509 
L 1594 2854 
C 1254 3162 979 3360 499 3654 
L 269 3354 
z
M 1331 1869 
C 1056 1005 698 294 378 -19 
L 749 -352 
C 813 -243 832 -211 877 -134 
C 986 45 1030 128 1094 250 
C 1299 640 1466 1043 1658 1613 
L 1331 1869 
z
" transform="scale(0.015625)"/>
      </defs>
      <use xlink:href="#03-read-16-HiraginoSansGB-W3-54"/>
      <use xlink:href="#03-read-16-HiraginoSansGB-W3-50" x="64.899994"/>
      <use xlink:href="#03-read-16-HiraginoSansGB-W3-32" x="131.099991"/>
      <use xlink:href="#03-read-16-HiraginoSansGB-W3-20" x="196.799988"/>
      <use xlink:href="#03-read-16-HiraginoSansGB-W3-72ec" x="230.099976"/>
      <use xlink:href="#03-read-16-HiraginoSansGB-W3-7acb" x="330.09996"/>
      <use xlink:href="#03-read-16-HiraginoSansGB-W3-6c60" x="430.099945"/>
     </g>
    </g>
    <g id="03-read-16-patch_29">
     <path d="M 214.252177 685.751968 
L 236.252177 685.751968 
L 236.252177 678.051968 
L 214.252177 678.051968 
z
" style="fill: #467bb5; stroke: #467bb5; stroke-width: 0.5; stroke-linejoin: miter"/>
    </g>
    <g id="03-read-16-text_44">
     <!-- TP2 共享池 -->
     <g style="fill: #192c46" transform="translate(245.052177 685.751968) scale(0.11 -0.11)">
      <defs>
       <path id="03-read-16-HiraginoSansGB-W3-5171" d="M 1920 3987 
L 1299 3987 
C 1043 3987 838 4000 653 4026 
L 653 3571 
C 826 3597 1005 3603 1299 3603 
L 1920 3603 
L 1920 1984 
L 1114 1984 
C 826 1984 563 1997 371 2029 
L 371 1549 
C 563 1574 838 1594 1114 1594 
L 5267 1594 
C 5555 1594 5843 1574 6035 1549 
L 6035 2029 
C 5862 2003 5555 1984 5267 1984 
L 4435 1984 
L 4435 3603 
L 5120 3603 
C 5427 3603 5638 3590 5798 3571 
L 5798 4026 
C 5619 4000 5389 3987 5120 3987 
L 4435 3987 
L 4435 4576 
C 4435 4838 4448 5043 4480 5235 
L 3974 5235 
C 4000 5088 4013 4909 4013 4570 
L 4013 3987 
L 2336 3987 
L 2336 4582 
C 2336 4864 2349 5094 2374 5242 
L 1875 5242 
C 1907 5082 1920 4877 1920 4582 
L 1920 3987 
z
M 2336 1984 
L 2336 3603 
L 4013 3603 
L 4013 1984 
L 2336 1984 
z
M 3731 1030 
C 4109 762 4179 710 4691 320 
C 5158 -32 5293 -141 5594 -435 
L 5882 -96 
C 5344 358 4666 864 4000 1306 
L 3731 1030 
z
M 2240 1293 
C 1965 915 1792 736 1434 467 
C 1126 243 896 122 397 -83 
C 544 -211 602 -275 698 -435 
C 1082 -256 1299 -134 1574 64 
C 1978 358 2234 602 2605 1056 
L 2240 1293 
z
" transform="scale(0.015625)"/>
       <path id="03-read-16-HiraginoSansGB-W3-4eab" d="M 3437 4794 
C 3270 5056 3142 5222 2925 5453 
L 2560 5261 
C 2714 5101 2816 4979 2950 4794 
L 1075 4794 
C 800 4794 621 4806 435 4832 
L 435 4410 
C 608 4429 826 4442 1069 4442 
L 5376 4442 
C 5626 4442 5830 4429 6010 4410 
L 6010 4832 
C 5824 4806 5651 4794 5376 4794 
L 3437 4794 
z
M 3021 1242 
L 966 1242 
C 666 1242 493 1254 333 1280 
L 333 858 
C 518 883 704 896 973 896 
L 3021 896 
L 3021 90 
C 3021 -45 2957 -90 2739 -90 
C 2470 -90 2195 -64 1850 -13 
C 1907 -154 1933 -256 1952 -410 
C 2246 -435 2515 -448 2790 -448 
C 3277 -448 3430 -326 3430 45 
L 3430 896 
L 5440 896 
C 5709 896 5894 883 6074 858 
L 6074 1280 
C 5907 1254 5734 1242 5440 1242 
L 3430 1242 
L 3430 1370 
C 4198 1574 4691 1786 5261 2150 
L 5062 2381 
C 4966 2362 4800 2355 4525 2355 
L 1600 2355 
C 1363 2355 1184 2362 992 2387 
L 992 1984 
C 1165 2010 1331 2016 1587 2016 
L 3731 2016 
C 4000 2016 4198 2022 4493 2042 
C 4090 1850 3795 1747 3283 1613 
L 3002 1613 
C 3014 1542 3021 1440 3021 1267 
L 3021 1242 
z
M 1229 3142 
C 1229 3027 1222 2867 1210 2752 
C 1370 2765 1530 2771 1741 2771 
L 4614 2771 
C 4858 2771 4998 2765 5171 2752 
C 5158 2854 5152 2957 5152 3117 
L 5152 3686 
C 5152 3840 5158 3942 5171 4051 
C 5030 4038 4928 4032 4608 4032 
L 1760 4032 
C 1542 4032 1382 4038 1210 4051 
C 1229 3904 1229 3853 1229 3674 
L 1229 3142 
z
M 1619 3706 
L 4762 3706 
L 4762 3098 
L 1619 3098 
L 1619 3706 
z
" transform="scale(0.015625)"/>
      </defs>
      <use xlink:href="#03-read-16-HiraginoSansGB-W3-54"/>
      <use xlink:href="#03-read-16-HiraginoSansGB-W3-50" x="64.899994"/>
      <use xlink:href="#03-read-16-HiraginoSansGB-W3-32" x="131.099991"/>
      <use xlink:href="#03-read-16-HiraginoSansGB-W3-20" x="196.799988"/>
      <use xlink:href="#03-read-16-HiraginoSansGB-W3-5171" x="230.099976"/>
      <use xlink:href="#03-read-16-HiraginoSansGB-W3-4eab" x="330.09996"/>
      <use xlink:href="#03-read-16-HiraginoSansGB-W3-6c60" x="430.099945"/>
     </g>
    </g>
    <g id="03-read-16-patch_30">
     <path d="M 325.364208 669.581968 
L 347.364208 669.581968 
L 347.364208 661.881968 
L 325.364208 661.881968 
z
" style="fill: url(#03-read-16-hdea91fd583); stroke: #008477; stroke-width: 0.5; stroke-linejoin: miter"/>
    </g>
    <g id="03-read-16-text_45">
     <!-- TP4 独立池 -->
     <g style="fill: #192c46" transform="translate(356.164208 669.581968) scale(0.11 -0.11)">
      <use xlink:href="#03-read-16-HiraginoSansGB-W3-54"/>
      <use xlink:href="#03-read-16-HiraginoSansGB-W3-50" x="64.899994"/>
      <use xlink:href="#03-read-16-HiraginoSansGB-W3-34" x="131.099991"/>
      <use xlink:href="#03-read-16-HiraginoSansGB-W3-20" x="197.199982"/>
      <use xlink:href="#03-read-16-HiraginoSansGB-W3-72ec" x="230.499969"/>
      <use xlink:href="#03-read-16-HiraginoSansGB-W3-7acb" x="330.499954"/>
      <use xlink:href="#03-read-16-HiraginoSansGB-W3-6c60" x="430.499939"/>
     </g>
    </g>
    <g id="03-read-16-patch_31">
     <path d="M 325.364208 685.751968 
L 347.364208 685.751968 
L 347.364208 678.051968 
L 325.364208 678.051968 
z
" style="fill: #008477; stroke: #008477; stroke-width: 0.5; stroke-linejoin: miter"/>
    </g>
    <g id="03-read-16-text_46">
     <!-- TP4 共享池 -->
     <g style="fill: #192c46" transform="translate(356.164208 685.751968) scale(0.11 -0.11)">
      <use xlink:href="#03-read-16-HiraginoSansGB-W3-54"/>
      <use xlink:href="#03-read-16-HiraginoSansGB-W3-50" x="64.899994"/>
      <use xlink:href="#03-read-16-HiraginoSansGB-W3-34" x="131.099991"/>
      <use xlink:href="#03-read-16-HiraginoSansGB-W3-20" x="197.199982"/>
      <use xlink:href="#03-read-16-HiraginoSansGB-W3-5171" x="230.499969"/>
      <use xlink:href="#03-read-16-HiraginoSansGB-W3-4eab" x="330.499954"/>
      <use xlink:href="#03-read-16-HiraginoSansGB-W3-6c60" x="430.499939"/>
     </g>
    </g>
   </g>
  </g>
  <g id="03-read-16-axes_2">
   <g id="03-read-16-patch_32">
    <path d="M 641.26376 596.428736 
L 1028.675145 596.428736 
L 1028.675145 48.149543 
L 641.26376 48.149543 
z
" style="fill: #ffffff"/>
   </g>
   <g id="03-read-16-matplotlib.axis_3">
    <g id="03-read-16-xtick_11">
     <g id="03-read-16-line2d_27">
      <path d="M 641.26376 596.428736 
L 641.26376 48.149543 
" clip-path="url(#03-read-16-p8379172ea5)" style="fill: none; stroke: #e9edf2; stroke-width: 0.8; stroke-linecap: square"/>
     </g>
     <g id="03-read-16-line2d_28"/>
     <g id="03-read-16-text_47">
      <!-- 0.0 -->
      <g style="fill: #405570" transform="translate(631.939385 613.619986) scale(0.12 -0.12)">
       <use xlink:href="#03-read-16-HiraginoSansGB-W3-30"/>
       <use xlink:href="#03-read-16-HiraginoSansGB-W3-2e" x="65.699997"/>
       <use xlink:href="#03-read-16-HiraginoSansGB-W3-30" x="89.699982"/>
      </g>
     </g>
    </g>
    <g id="03-read-16-xtick_12">
     <g id="03-read-16-line2d_29">
      <path d="M 684.309469 596.428736 
L 684.309469 48.149543 
" clip-path="url(#03-read-16-p8379172ea5)" style="fill: none; stroke: #e9edf2; stroke-width: 0.8; stroke-linecap: square"/>
     </g>
     <g id="03-read-16-line2d_30"/>
     <g id="03-read-16-text_48">
      <!-- 0.2 -->
      <g style="fill: #405570" transform="translate(674.985094 613.619986) scale(0.12 -0.12)">
       <use xlink:href="#03-read-16-HiraginoSansGB-W3-30"/>
       <use xlink:href="#03-read-16-HiraginoSansGB-W3-2e" x="65.699997"/>
       <use xlink:href="#03-read-16-HiraginoSansGB-W3-32" x="89.699982"/>
      </g>
     </g>
    </g>
    <g id="03-read-16-xtick_13">
     <g id="03-read-16-line2d_31">
      <path d="M 727.355179 596.428736 
L 727.355179 48.149543 
" clip-path="url(#03-read-16-p8379172ea5)" style="fill: none; stroke: #e9edf2; stroke-width: 0.8; stroke-linecap: square"/>
     </g>
     <g id="03-read-16-line2d_32"/>
     <g id="03-read-16-text_49">
      <!-- 0.4 -->
      <g style="fill: #405570" transform="translate(718.007366 613.619986) scale(0.12 -0.12)">
       <use xlink:href="#03-read-16-HiraginoSansGB-W3-30"/>
       <use xlink:href="#03-read-16-HiraginoSansGB-W3-2e" x="65.699997"/>
       <use xlink:href="#03-read-16-HiraginoSansGB-W3-34" x="89.699982"/>
      </g>
     </g>
    </g>
    <g id="03-read-16-xtick_14">
     <g id="03-read-16-line2d_33">
      <path d="M 770.400888 596.428736 
L 770.400888 48.149543 
" clip-path="url(#03-read-16-p8379172ea5)" style="fill: none; stroke: #e9edf2; stroke-width: 0.8; stroke-linecap: square"/>
     </g>
     <g id="03-read-16-line2d_34"/>
     <g id="03-read-16-text_50">
      <!-- 0.6 -->
      <g style="fill: #405570" transform="translate(761.076513 613.619986) scale(0.12 -0.12)">
       <use xlink:href="#03-read-16-HiraginoSansGB-W3-30"/>
       <use xlink:href="#03-read-16-HiraginoSansGB-W3-2e" x="65.699997"/>
       <use xlink:href="#03-read-16-HiraginoSansGB-W3-36" x="89.699982"/>
      </g>
     </g>
    </g>
    <g id="03-read-16-xtick_15">
     <g id="03-read-16-line2d_35">
      <path d="M 813.446598 596.428736 
L 813.446598 48.149543 
" clip-path="url(#03-read-16-p8379172ea5)" style="fill: none; stroke: #e9edf2; stroke-width: 0.8; stroke-linecap: square"/>
     </g>
     <g id="03-read-16-line2d_36"/>
     <g id="03-read-16-text_51">
      <!-- 0.8 -->
      <g style="fill: #405570" transform="translate(804.122223 613.619986) scale(0.12 -0.12)">
       <use xlink:href="#03-read-16-HiraginoSansGB-W3-30"/>
       <use xlink:href="#03-read-16-HiraginoSansGB-W3-2e" x="65.699997"/>
       <use xlink:href="#03-read-16-HiraginoSansGB-W3-38" x="89.699982"/>
      </g>
     </g>
    </g>
    <g id="03-read-16-xtick_16">
     <g id="03-read-16-line2d_37">
      <path d="M 856.492307 596.428736 
L 856.492307 48.149543 
" clip-path="url(#03-read-16-p8379172ea5)" style="fill: none; stroke: #e9edf2; stroke-width: 0.8; stroke-linecap: square"/>
     </g>
     <g id="03-read-16-line2d_38"/>
     <g id="03-read-16-text_52">
      <!-- 1.0 -->
      <g style="fill: #405570" transform="translate(847.167932 613.619986) scale(0.12 -0.12)">
       <use xlink:href="#03-read-16-HiraginoSansGB-W3-31"/>
       <use xlink:href="#03-read-16-HiraginoSansGB-W3-2e" x="65.699997"/>
       <use xlink:href="#03-read-16-HiraginoSansGB-W3-30" x="89.699982"/>
      </g>
     </g>
    </g>
    <g id="03-read-16-xtick_17">
     <g id="03-read-16-line2d_39">
      <path d="M 899.538017 596.428736 
L 899.538017 48.149543 
" clip-path="url(#03-read-16-p8379172ea5)" style="fill: none; stroke: #e9edf2; stroke-width: 0.8; stroke-linecap: square"/>
     </g>
     <g id="03-read-16-line2d_40"/>
     <g id="03-read-16-text_53">
      <!-- 1.2 -->
      <g style="fill: #405570" transform="translate(890.213642 613.619986) scale(0.12 -0.12)">
       <use xlink:href="#03-read-16-HiraginoSansGB-W3-31"/>
       <use xlink:href="#03-read-16-HiraginoSansGB-W3-2e" x="65.699997"/>
       <use xlink:href="#03-read-16-HiraginoSansGB-W3-32" x="89.699982"/>
      </g>
     </g>
    </g>
    <g id="03-read-16-xtick_18">
     <g id="03-read-16-line2d_41">
      <path d="M 942.583726 596.428736 
L 942.583726 48.149543 
" clip-path="url(#03-read-16-p8379172ea5)" style="fill: none; stroke: #e9edf2; stroke-width: 0.8; stroke-linecap: square"/>
     </g>
     <g id="03-read-16-line2d_42"/>
     <g id="03-read-16-text_54">
      <!-- 1.4 -->
      <g style="fill: #405570" transform="translate(933.235914 613.619986) scale(0.12 -0.12)">
       <use xlink:href="#03-read-16-HiraginoSansGB-W3-31"/>
       <use xlink:href="#03-read-16-HiraginoSansGB-W3-2e" x="65.699997"/>
       <use xlink:href="#03-read-16-HiraginoSansGB-W3-34" x="89.699982"/>
      </g>
     </g>
    </g>
    <g id="03-read-16-xtick_19">
     <g id="03-read-16-line2d_43">
      <path d="M 985.629436 596.428736 
L 985.629436 48.149543 
" clip-path="url(#03-read-16-p8379172ea5)" style="fill: none; stroke: #e9edf2; stroke-width: 0.8; stroke-linecap: square"/>
     </g>
     <g id="03-read-16-line2d_44"/>
     <g id="03-read-16-text_55">
      <!-- 1.6 -->
      <g style="fill: #405570" transform="translate(976.305061 613.619986) scale(0.12 -0.12)">
       <use xlink:href="#03-read-16-HiraginoSansGB-W3-31"/>
       <use xlink:href="#03-read-16-HiraginoSansGB-W3-2e" x="65.699997"/>
       <use xlink:href="#03-read-16-HiraginoSansGB-W3-36" x="89.699982"/>
      </g>
     </g>
    </g>
    <g id="03-read-16-xtick_20">
     <g id="03-read-16-line2d_45">
      <path d="M 1028.675145 596.428736 
L 1028.675145 48.149543 
" clip-path="url(#03-read-16-p8379172ea5)" style="fill: none; stroke: #e9edf2; stroke-width: 0.8; stroke-linecap: square"/>
     </g>
     <g id="03-read-16-line2d_46"/>
     <g id="03-read-16-text_56">
      <!-- 1.8 -->
      <g style="fill: #405570" transform="translate(1019.35077 613.619986) scale(0.12 -0.12)">
       <use xlink:href="#03-read-16-HiraginoSansGB-W3-31"/>
       <use xlink:href="#03-read-16-HiraginoSansGB-W3-2e" x="65.699997"/>
       <use xlink:href="#03-read-16-HiraginoSansGB-W3-38" x="89.699982"/>
      </g>
     </g>
    </g>
    <g id="03-read-16-text_57">
     <!-- 组 P50（ms） · 越低越好 -->
     <g style="fill: #405570" transform="translate(760.31164 629.259986) scale(0.12 -0.12)">
      <use xlink:href="#03-read-16-HiraginoSansGB-W3-7ec4"/>
      <use xlink:href="#03-read-16-HiraginoSansGB-W3-20" x="99.999985"/>
      <use xlink:href="#03-read-16-HiraginoSansGB-W3-50" x="133.299973"/>
      <use xlink:href="#03-read-16-HiraginoSansGB-W3-35" x="199.499969"/>
      <use xlink:href="#03-read-16-HiraginoSansGB-W3-30" x="265.199966"/>
      <use xlink:href="#03-read-16-HiraginoSansGB-W3-ff08" x="330.899963"/>
      <use xlink:href="#03-read-16-HiraginoSansGB-W3-6d" x="430.899948"/>
      <use xlink:href="#03-read-16-HiraginoSansGB-W3-73" x="523.999939"/>
      <use xlink:href="#03-read-16-HiraginoSansGB-W3-ff09" x="577.699936"/>
      <use xlink:href="#03-read-16-HiraginoSansGB-W3-20" x="677.699921"/>
      <use xlink:href="#03-read-16-HiraginoSansGB-W3-b7" x="710.999908"/>
      <use xlink:href="#03-read-16-HiraginoSansGB-W3-20" x="810.999893"/>
      <use xlink:href="#03-read-16-HiraginoSansGB-W3-8d8a" x="844.299881"/>
      <use xlink:href="#03-read-16-HiraginoSansGB-W3-4f4e" x="944.299866"/>
      <use xlink:href="#03-read-16-HiraginoSansGB-W3-8d8a" x="1044.29985"/>
      <use xlink:href="#03-read-16-HiraginoSansGB-W3-597d" x="1144.299835"/>
     </g>
    </g>
   </g>
   <g id="03-read-16-matplotlib.axis_4">
    <g id="03-read-16-ytick_7">
     <g id="03-read-16-line2d_47"/>
     <g id="03-read-16-text_58">
      <!-- P 来源 / 连续 -->
      <g style="fill: #243a56" transform="translate(559.64376 117.629115) scale(0.12 -0.12)">
       <use xlink:href="#03-read-16-HiraginoSansGB-W3-50"/>
       <use xlink:href="#03-read-16-HiraginoSansGB-W3-20" x="66.199997"/>
       <use xlink:href="#03-read-16-HiraginoSansGB-W3-6765" x="99.499985"/>
       <use xlink:href="#03-read-16-HiraginoSansGB-W3-6e90" x="199.499969"/>
       <use xlink:href="#03-read-16-HiraginoSansGB-W3-20" x="299.499954"/>
       <use xlink:href="#03-read-16-HiraginoSansGB-W3-2f" x="332.799942"/>
       <use xlink:href="#03-read-16-HiraginoSansGB-W3-20" x="380.199936"/>
       <use xlink:href="#03-read-16-HiraginoSansGB-W3-8fde" x="413.499924"/>
       <use xlink:href="#03-read-16-HiraginoSansGB-W3-7eed" x="513.499908"/>
      </g>
     </g>
    </g>
    <g id="03-read-16-ytick_8">
     <g id="03-read-16-line2d_48"/>
     <g id="03-read-16-text_59">
      <!-- P 来源 / 离散 -->
      <g style="fill: #243a56" transform="translate(559.64376 203.97792) scale(0.12 -0.12)">
       <use xlink:href="#03-read-16-HiraginoSansGB-W3-50"/>
       <use xlink:href="#03-read-16-HiraginoSansGB-W3-20" x="66.199997"/>
       <use xlink:href="#03-read-16-HiraginoSansGB-W3-6765" x="99.499985"/>
       <use xlink:href="#03-read-16-HiraginoSansGB-W3-6e90" x="199.499969"/>
       <use xlink:href="#03-read-16-HiraginoSansGB-W3-20" x="299.499954"/>
       <use xlink:href="#03-read-16-HiraginoSansGB-W3-2f" x="332.799942"/>
       <use xlink:href="#03-read-16-HiraginoSansGB-W3-20" x="380.199936"/>
       <use xlink:href="#03-read-16-HiraginoSansGB-W3-79bb" x="413.499924"/>
       <use xlink:href="#03-read-16-HiraginoSansGB-W3-6563" x="513.499908"/>
      </g>
     </g>
    </g>
    <g id="03-read-16-ytick_9">
     <g id="03-read-16-line2d_49"/>
     <g id="03-read-16-text_60">
      <!-- D 来源 / 连续 -->
      <g style="fill: #243a56" transform="translate(558.49251 290.315475) scale(0.12 -0.12)">
       <use xlink:href="#03-read-16-HiraginoSansGB-W3-44"/>
       <use xlink:href="#03-read-16-HiraginoSansGB-W3-20" x="75.799988"/>
       <use xlink:href="#03-read-16-HiraginoSansGB-W3-6765" x="109.099976"/>
       <use xlink:href="#03-read-16-HiraginoSansGB-W3-6e90" x="209.09996"/>
       <use xlink:href="#03-read-16-HiraginoSansGB-W3-20" x="309.099945"/>
       <use xlink:href="#03-read-16-HiraginoSansGB-W3-2f" x="342.399933"/>
       <use xlink:href="#03-read-16-HiraginoSansGB-W3-20" x="389.799927"/>
       <use xlink:href="#03-read-16-HiraginoSansGB-W3-8fde" x="423.099915"/>
       <use xlink:href="#03-read-16-HiraginoSansGB-W3-7eed" x="523.099899"/>
      </g>
     </g>
    </g>
    <g id="03-read-16-ytick_10">
     <g id="03-read-16-line2d_50"/>
     <g id="03-read-16-text_61">
      <!-- D 来源 / 离散 -->
      <g style="fill: #243a56" transform="translate(558.49251 376.66428) scale(0.12 -0.12)">
       <use xlink:href="#03-read-16-HiraginoSansGB-W3-44"/>
       <use xlink:href="#03-read-16-HiraginoSansGB-W3-20" x="75.799988"/>
       <use xlink:href="#03-read-16-HiraginoSansGB-W3-6765" x="109.099976"/>
       <use xlink:href="#03-read-16-HiraginoSansGB-W3-6e90" x="209.09996"/>
       <use xlink:href="#03-read-16-HiraginoSansGB-W3-20" x="309.099945"/>
       <use xlink:href="#03-read-16-HiraginoSansGB-W3-2f" x="342.399933"/>
       <use xlink:href="#03-read-16-HiraginoSansGB-W3-20" x="389.799927"/>
       <use xlink:href="#03-read-16-HiraginoSansGB-W3-79bb" x="423.099915"/>
       <use xlink:href="#03-read-16-HiraginoSansGB-W3-6563" x="523.099899"/>
      </g>
     </g>
    </g>
    <g id="03-read-16-ytick_11">
     <g id="03-read-16-line2d_51"/>
     <g id="03-read-16-text_62">
      <!-- MIXED 来源 / 连续 -->
      <g style="fill: #243a56" transform="translate(527.78376 463.001835) scale(0.12 -0.12)">
       <use xlink:href="#03-read-16-HiraginoSansGB-W3-4d"/>
       <use xlink:href="#03-read-16-HiraginoSansGB-W3-49" x="94.499985"/>
       <use xlink:href="#03-read-16-HiraginoSansGB-W3-58" x="119.399979"/>
       <use xlink:href="#03-read-16-HiraginoSansGB-W3-45" x="191.59996"/>
       <use xlink:href="#03-read-16-HiraginoSansGB-W3-44" x="255.899948"/>
       <use xlink:href="#03-read-16-HiraginoSansGB-W3-20" x="331.699936"/>
       <use xlink:href="#03-read-16-HiraginoSansGB-W3-6765" x="364.999924"/>
       <use xlink:href="#03-read-16-HiraginoSansGB-W3-6e90" x="464.999908"/>
       <use xlink:href="#03-read-16-HiraginoSansGB-W3-20" x="564.999893"/>
       <use xlink:href="#03-read-16-HiraginoSansGB-W3-2f" x="598.299881"/>
       <use xlink:href="#03-read-16-HiraginoSansGB-W3-20" x="645.699875"/>
       <use xlink:href="#03-read-16-HiraginoSansGB-W3-8fde" x="678.999863"/>
       <use xlink:href="#03-read-16-HiraginoSansGB-W3-7eed" x="778.999847"/>
      </g>
     </g>
    </g>
    <g id="03-read-16-ytick_12">
     <g id="03-read-16-line2d_52"/>
     <g id="03-read-16-text_63">
      <!-- MIXED 来源 / 离散 -->
      <g style="fill: #243a56" transform="translate(527.78376 549.35064) scale(0.12 -0.12)">
       <use xlink:href="#03-read-16-HiraginoSansGB-W3-4d"/>
       <use xlink:href="#03-read-16-HiraginoSansGB-W3-49" x="94.499985"/>
       <use xlink:href="#03-read-16-HiraginoSansGB-W3-58" x="119.399979"/>
       <use xlink:href="#03-read-16-HiraginoSansGB-W3-45" x="191.59996"/>
       <use xlink:href="#03-read-16-HiraginoSansGB-W3-44" x="255.899948"/>
       <use xlink:href="#03-read-16-HiraginoSansGB-W3-20" x="331.699936"/>
       <use xlink:href="#03-read-16-HiraginoSansGB-W3-6765" x="364.999924"/>
       <use xlink:href="#03-read-16-HiraginoSansGB-W3-6e90" x="464.999908"/>
       <use xlink:href="#03-read-16-HiraginoSansGB-W3-20" x="564.999893"/>
       <use xlink:href="#03-read-16-HiraginoSansGB-W3-2f" x="598.299881"/>
       <use xlink:href="#03-read-16-HiraginoSansGB-W3-20" x="645.699875"/>
       <use xlink:href="#03-read-16-HiraginoSansGB-W3-79bb" x="678.999863"/>
       <use xlink:href="#03-read-16-HiraginoSansGB-W3-6563" x="778.999847"/>
      </g>
     </g>
    </g>
   </g>
   <g id="03-read-16-patch_33">
    <path d="M 641.26376 78.153798 
L 828.986099 78.153798 
L 828.986099 93.263854 
L 641.26376 93.263854 
z
" clip-path="url(#03-read-16-p8379172ea5)" style="fill: url(#03-read-16-hc447d1c890); stroke: #467bb5; stroke-width: 0.5; stroke-linejoin: miter"/>
   </g>
   <g id="03-read-16-patch_34">
    <path d="M 641.26376 164.496978 
L 830.535744 164.496978 
L 830.535744 179.607034 
L 641.26376 179.607034 
z
" clip-path="url(#03-read-16-p8379172ea5)" style="fill: url(#03-read-16-hc447d1c890); stroke: #467bb5; stroke-width: 0.5; stroke-linejoin: miter"/>
   </g>
   <g id="03-read-16-patch_35">
    <path d="M 641.26376 250.840158 
L 809.852281 250.840158 
L 809.852281 265.950214 
L 641.26376 265.950214 
z
" clip-path="url(#03-read-16-p8379172ea5)" style="fill: url(#03-read-16-hc447d1c890); stroke: #467bb5; stroke-width: 0.5; stroke-linejoin: miter"/>
   </g>
   <g id="03-read-16-patch_36">
    <path d="M 641.26376 337.183338 
L 794.893897 337.183338 
L 794.893897 352.293394 
L 641.26376 352.293394 
z
" clip-path="url(#03-read-16-p8379172ea5)" style="fill: url(#03-read-16-hc447d1c890); stroke: #467bb5; stroke-width: 0.5; stroke-linejoin: miter"/>
   </g>
   <g id="03-read-16-patch_37">
    <path d="M 641.26376 423.526518 
L 820.958074 423.526518 
L 820.958074 438.636574 
L 641.26376 438.636574 
z
" clip-path="url(#03-read-16-p8379172ea5)" style="fill: url(#03-read-16-hc447d1c890); stroke: #467bb5; stroke-width: 0.5; stroke-linejoin: miter"/>
   </g>
   <g id="03-read-16-patch_38">
    <path d="M 641.26376 509.869698 
L 816.287615 509.869698 
L 816.287615 524.979754 
L 641.26376 524.979754 
z
" clip-path="url(#03-read-16-p8379172ea5)" style="fill: url(#03-read-16-hc447d1c890); stroke: #467bb5; stroke-width: 0.5; stroke-linejoin: miter"/>
   </g>
   <g id="03-read-16-patch_39">
    <path d="M 641.26376 96.285865 
L 829.287419 96.285865 
L 829.287419 111.395922 
L 641.26376 111.395922 
z
" clip-path="url(#03-read-16-p8379172ea5)" style="fill: #467bb5; stroke: #467bb5; stroke-width: 0.5; stroke-linejoin: miter"/>
   </g>
   <g id="03-read-16-patch_40">
    <path d="M 641.26376 182.629045 
L 828.534119 182.629045 
L 828.534119 197.739102 
L 641.26376 197.739102 
z
" clip-path="url(#03-read-16-p8379172ea5)" style="fill: #467bb5; stroke: #467bb5; stroke-width: 0.5; stroke-linejoin: miter"/>
   </g>
   <g id="03-read-16-patch_41">
    <path d="M 641.26376 268.972226 
L 807.721518 268.972226 
L 807.721518 284.082282 
L 641.26376 284.082282 
z
" clip-path="url(#03-read-16-p8379172ea5)" style="fill: #467bb5; stroke: #467bb5; stroke-width: 0.5; stroke-linejoin: miter"/>
   </g>
   <g id="03-read-16-patch_42">
    <path d="M 641.26376 355.315406 
L 795.776334 355.315406 
L 795.776334 370.425462 
L 641.26376 370.425462 
z
" clip-path="url(#03-read-16-p8379172ea5)" style="fill: #467bb5; stroke: #467bb5; stroke-width: 0.5; stroke-linejoin: miter"/>
   </g>
   <g id="03-read-16-patch_43">
    <path d="M 641.26376 441.658586 
L 819.107109 441.658586 
L 819.107109 456.768642 
L 641.26376 456.768642 
z
" clip-path="url(#03-read-16-p8379172ea5)" style="fill: #467bb5; stroke: #467bb5; stroke-width: 0.5; stroke-linejoin: miter"/>
   </g>
   <g id="03-read-16-patch_44">
    <path d="M 641.26376 528.001766 
L 817.492894 528.001766 
L 817.492894 543.111822 
L 641.26376 543.111822 
z
" clip-path="url(#03-read-16-p8379172ea5)" style="fill: #467bb5; stroke: #467bb5; stroke-width: 0.5; stroke-linejoin: miter"/>
   </g>
   <g id="03-read-16-patch_45">
    <path d="M 641.26376 114.417933 
L 941.356923 114.417933 
L 941.356923 129.52799 
L 641.26376 129.52799 
z
" clip-path="url(#03-read-16-p8379172ea5)" style="fill: url(#03-read-16-hdea91fd583); stroke: #008477; stroke-width: 0.5; stroke-linejoin: miter"/>
   </g>
   <g id="03-read-16-patch_46">
    <path d="M 641.26376 200.761113 
L 936.019255 200.761113 
L 936.019255 215.87117 
L 641.26376 215.87117 
z
" clip-path="url(#03-read-16-p8379172ea5)" style="fill: url(#03-read-16-hdea91fd583); stroke: #008477; stroke-width: 0.5; stroke-linejoin: miter"/>
   </g>
   <g id="03-read-16-patch_47">
    <path d="M 641.26376 287.104293 
L 935.481184 287.104293 
L 935.481184 302.21435 
L 641.26376 302.21435 
z
" clip-path="url(#03-read-16-p8379172ea5)" style="fill: url(#03-read-16-hdea91fd583); stroke: #008477; stroke-width: 0.5; stroke-linejoin: miter"/>
   </g>
   <g id="03-read-16-patch_48">
    <path d="M 641.26376 373.447473 
L 934.555701 373.447473 
L 934.555701 388.55753 
L 641.26376 388.55753 
z
" clip-path="url(#03-read-16-p8379172ea5)" style="fill: url(#03-read-16-hdea91fd583); stroke: #008477; stroke-width: 0.5; stroke-linejoin: miter"/>
   </g>
   <g id="03-read-16-patch_49">
    <path d="M 641.26376 459.790654 
L 894.372532 459.790654 
L 894.372532 474.90071 
L 641.26376 474.90071 
z
" clip-path="url(#03-read-16-p8379172ea5)" style="fill: url(#03-read-16-hdea91fd583); stroke: #008477; stroke-width: 0.5; stroke-linejoin: miter"/>
   </g>
   <g id="03-read-16-patch_50">
    <path d="M 641.26376 546.133834 
L 928.636916 546.133834 
L 928.636916 561.24389 
L 641.26376 561.24389 
z
" clip-path="url(#03-read-16-p8379172ea5)" style="fill: url(#03-read-16-hdea91fd583); stroke: #008477; stroke-width: 0.5; stroke-linejoin: miter"/>
   </g>
   <g id="03-read-16-patch_51">
    <path d="M 641.26376 132.550001 
L 942.69134 132.550001 
L 942.69134 147.660058 
L 641.26376 147.660058 
z
" clip-path="url(#03-read-16-p8379172ea5)" style="fill: #008477; stroke: #008477; stroke-width: 0.5; stroke-linejoin: miter"/>
   </g>
   <g id="03-read-16-patch_52">
    <path d="M 641.26376 218.893181 
L 935.201387 218.893181 
L 935.201387 234.003238 
L 641.26376 234.003238 
z
" clip-path="url(#03-read-16-p8379172ea5)" style="fill: #008477; stroke: #008477; stroke-width: 0.5; stroke-linejoin: miter"/>
   </g>
   <g id="03-read-16-patch_53">
    <path d="M 641.26376 305.236361 
L 935.760981 305.236361 
L 935.760981 320.346418 
L 641.26376 320.346418 
z
" clip-path="url(#03-read-16-p8379172ea5)" style="fill: #008477; stroke: #008477; stroke-width: 0.5; stroke-linejoin: miter"/>
   </g>
   <g id="03-read-16-patch_54">
    <path d="M 641.26376 391.579541 
L 934.663316 391.579541 
L 934.663316 406.689598 
L 641.26376 406.689598 
z
" clip-path="url(#03-read-16-p8379172ea5)" style="fill: #008477; stroke: #008477; stroke-width: 0.5; stroke-linejoin: miter"/>
   </g>
   <g id="03-read-16-patch_55">
    <path d="M 641.26376 477.922721 
L 894.566237 477.922721 
L 894.566237 493.032778 
L 641.26376 493.032778 
z
" clip-path="url(#03-read-16-p8379172ea5)" style="fill: #008477; stroke: #008477; stroke-width: 0.5; stroke-linejoin: miter"/>
   </g>
   <g id="03-read-16-patch_56">
    <path d="M 641.26376 564.265901 
L 925.150214 564.265901 
L 925.150214 579.375958 
L 641.26376 579.375958 
z
" clip-path="url(#03-read-16-p8379172ea5)" style="fill: #008477; stroke: #008477; stroke-width: 0.5; stroke-linejoin: miter"/>
   </g>
   <g id="03-read-16-patch_57">
    <path d="M 641.26376 596.428736 
L 1028.675145 596.428736 
" style="fill: none; stroke: #d7e0eb; stroke-width: 0.8; stroke-linejoin: miter; stroke-linecap: square"/>
   </g>
   <g id="03-read-16-text_64">
    <!-- 0.8722 -->
    <g style="fill: #405570" transform="translate(833.635036 88.518201) scale(0.1 -0.1)">
     <use xlink:href="#03-read-16-HiraginoSansGB-W3-30"/>
     <use xlink:href="#03-read-16-HiraginoSansGB-W3-2e" x="65.699997"/>
     <use xlink:href="#03-read-16-HiraginoSansGB-W3-38" x="89.699982"/>
     <use xlink:href="#03-read-16-HiraginoSansGB-W3-37" x="155.399979"/>
     <use xlink:href="#03-read-16-HiraginoSansGB-W3-32" x="221.099976"/>
     <use xlink:href="#03-read-16-HiraginoSansGB-W3-32" x="286.799973"/>
    </g>
   </g>
   <g id="03-read-16-text_65">
    <!-- 0.8794 -->
    <g style="fill: #405570" transform="translate(835.184681 174.861381) scale(0.1 -0.1)">
     <use xlink:href="#03-read-16-HiraginoSansGB-W3-30"/>
     <use xlink:href="#03-read-16-HiraginoSansGB-W3-2e" x="65.699997"/>
     <use xlink:href="#03-read-16-HiraginoSansGB-W3-38" x="89.699982"/>
     <use xlink:href="#03-read-16-HiraginoSansGB-W3-37" x="155.399979"/>
     <use xlink:href="#03-read-16-HiraginoSansGB-W3-39" x="221.099976"/>
     <use xlink:href="#03-read-16-HiraginoSansGB-W3-34" x="286.799973"/>
    </g>
   </g>
   <g id="03-read-16-text_66">
    <!-- 0.7833 -->
    <g style="fill: #405570" transform="translate(814.501218 261.204561) scale(0.1 -0.1)">
     <use xlink:href="#03-read-16-HiraginoSansGB-W3-30"/>
     <use xlink:href="#03-read-16-HiraginoSansGB-W3-2e" x="65.699997"/>
     <use xlink:href="#03-read-16-HiraginoSansGB-W3-37" x="89.699982"/>
     <use xlink:href="#03-read-16-HiraginoSansGB-W3-38" x="155.399979"/>
     <use xlink:href="#03-read-16-HiraginoSansGB-W3-33" x="221.099976"/>
     <use xlink:href="#03-read-16-HiraginoSansGB-W3-33" x="286.799973"/>
    </g>
   </g>
   <g id="03-read-16-text_67">
    <!-- 0.7138 -->
    <g style="fill: #405570" transform="translate(799.542834 347.547741) scale(0.1 -0.1)">
     <use xlink:href="#03-read-16-HiraginoSansGB-W3-30"/>
     <use xlink:href="#03-read-16-HiraginoSansGB-W3-2e" x="65.699997"/>
     <use xlink:href="#03-read-16-HiraginoSansGB-W3-37" x="89.699982"/>
     <use xlink:href="#03-read-16-HiraginoSansGB-W3-31" x="155.399979"/>
     <use xlink:href="#03-read-16-HiraginoSansGB-W3-33" x="221.099976"/>
     <use xlink:href="#03-read-16-HiraginoSansGB-W3-38" x="286.799973"/>
    </g>
   </g>
   <g id="03-read-16-text_68">
    <!-- 0.8349 -->
    <g style="fill: #405570" transform="translate(825.607011 433.890921) scale(0.1 -0.1)">
     <use xlink:href="#03-read-16-HiraginoSansGB-W3-30"/>
     <use xlink:href="#03-read-16-HiraginoSansGB-W3-2e" x="65.699997"/>
     <use xlink:href="#03-read-16-HiraginoSansGB-W3-38" x="89.699982"/>
     <use xlink:href="#03-read-16-HiraginoSansGB-W3-33" x="155.399979"/>
     <use xlink:href="#03-read-16-HiraginoSansGB-W3-34" x="221.099976"/>
     <use xlink:href="#03-read-16-HiraginoSansGB-W3-39" x="287.199966"/>
    </g>
   </g>
   <g id="03-read-16-text_69">
    <!-- 0.8132 -->
    <g style="fill: #405570" transform="translate(820.936551 520.234101) scale(0.1 -0.1)">
     <use xlink:href="#03-read-16-HiraginoSansGB-W3-30"/>
     <use xlink:href="#03-read-16-HiraginoSansGB-W3-2e" x="65.699997"/>
     <use xlink:href="#03-read-16-HiraginoSansGB-W3-38" x="89.699982"/>
     <use xlink:href="#03-read-16-HiraginoSansGB-W3-31" x="155.399979"/>
     <use xlink:href="#03-read-16-HiraginoSansGB-W3-33" x="221.099976"/>
     <use xlink:href="#03-read-16-HiraginoSansGB-W3-32" x="286.799973"/>
    </g>
   </g>
   <g id="03-read-16-text_70">
    <!-- 0.8736 -->
    <g style="fill: #467bb5" transform="translate(833.936355 106.650269) scale(0.1 -0.1)">
     <use xlink:href="#03-read-16-HiraginoSansGB-W3-30"/>
     <use xlink:href="#03-read-16-HiraginoSansGB-W3-2e" x="65.699997"/>
     <use xlink:href="#03-read-16-HiraginoSansGB-W3-38" x="89.699982"/>
     <use xlink:href="#03-read-16-HiraginoSansGB-W3-37" x="155.399979"/>
     <use xlink:href="#03-read-16-HiraginoSansGB-W3-33" x="221.099976"/>
     <use xlink:href="#03-read-16-HiraginoSansGB-W3-36" x="286.799973"/>
    </g>
   </g>
   <g id="03-read-16-text_71">
    <!-- 0.8701 -->
    <g style="fill: #467bb5" transform="translate(833.183056 192.993449) scale(0.1 -0.1)">
     <use xlink:href="#03-read-16-HiraginoSansGB-W3-30"/>
     <use xlink:href="#03-read-16-HiraginoSansGB-W3-2e" x="65.699997"/>
     <use xlink:href="#03-read-16-HiraginoSansGB-W3-38" x="89.699982"/>
     <use xlink:href="#03-read-16-HiraginoSansGB-W3-37" x="155.399979"/>
     <use xlink:href="#03-read-16-HiraginoSansGB-W3-30" x="221.099976"/>
     <use xlink:href="#03-read-16-HiraginoSansGB-W3-31" x="286.799973"/>
    </g>
   </g>
   <g id="03-read-16-text_72">
    <!-- 0.7734 -->
    <g style="fill: #467bb5" transform="translate(812.370455 279.336629) scale(0.1 -0.1)">
     <use xlink:href="#03-read-16-HiraginoSansGB-W3-30"/>
     <use xlink:href="#03-read-16-HiraginoSansGB-W3-2e" x="65.699997"/>
     <use xlink:href="#03-read-16-HiraginoSansGB-W3-37" x="89.699982"/>
     <use xlink:href="#03-read-16-HiraginoSansGB-W3-37" x="155.399979"/>
     <use xlink:href="#03-read-16-HiraginoSansGB-W3-33" x="221.099976"/>
     <use xlink:href="#03-read-16-HiraginoSansGB-W3-34" x="286.799973"/>
    </g>
   </g>
   <g id="03-read-16-text_73">
    <!-- 0.7179 -->
    <g style="fill: #467bb5" transform="translate(800.425271 365.679809) scale(0.1 -0.1)">
     <use xlink:href="#03-read-16-HiraginoSansGB-W3-30"/>
     <use xlink:href="#03-read-16-HiraginoSansGB-W3-2e" x="65.699997"/>
     <use xlink:href="#03-read-16-HiraginoSansGB-W3-37" x="89.699982"/>
     <use xlink:href="#03-read-16-HiraginoSansGB-W3-31" x="155.399979"/>
     <use xlink:href="#03-read-16-HiraginoSansGB-W3-37" x="221.099976"/>
     <use xlink:href="#03-read-16-HiraginoSansGB-W3-39" x="286.799973"/>
    </g>
   </g>
   <g id="03-read-16-text_74">
    <!-- 0.8263 -->
    <g style="fill: #467bb5" transform="translate(823.756045 452.022989) scale(0.1 -0.1)">
     <use xlink:href="#03-read-16-HiraginoSansGB-W3-30"/>
     <use xlink:href="#03-read-16-HiraginoSansGB-W3-2e" x="65.699997"/>
     <use xlink:href="#03-read-16-HiraginoSansGB-W3-38" x="89.699982"/>
     <use xlink:href="#03-read-16-HiraginoSansGB-W3-32" x="155.399979"/>
     <use xlink:href="#03-read-16-HiraginoSansGB-W3-36" x="221.099976"/>
     <use xlink:href="#03-read-16-HiraginoSansGB-W3-33" x="286.799973"/>
    </g>
   </g>
   <g id="03-read-16-text_75">
    <!-- 0.8188 -->
    <g style="fill: #467bb5" transform="translate(822.141831 538.366169) scale(0.1 -0.1)">
     <use xlink:href="#03-read-16-HiraginoSansGB-W3-30"/>
     <use xlink:href="#03-read-16-HiraginoSansGB-W3-2e" x="65.699997"/>
     <use xlink:href="#03-read-16-HiraginoSansGB-W3-38" x="89.699982"/>
     <use xlink:href="#03-read-16-HiraginoSansGB-W3-31" x="155.399979"/>
     <use xlink:href="#03-read-16-HiraginoSansGB-W3-38" x="221.099976"/>
     <use xlink:href="#03-read-16-HiraginoSansGB-W3-38" x="286.799973"/>
    </g>
   </g>
   <g id="03-read-16-text_76">
    <!-- 1.3943 -->
    <g style="fill: #405570" transform="translate(946.00586 124.782336) scale(0.1 -0.1)">
     <use xlink:href="#03-read-16-HiraginoSansGB-W3-31"/>
     <use xlink:href="#03-read-16-HiraginoSansGB-W3-2e" x="65.699997"/>
     <use xlink:href="#03-read-16-HiraginoSansGB-W3-33" x="89.699982"/>
     <use xlink:href="#03-read-16-HiraginoSansGB-W3-39" x="155.399979"/>
     <use xlink:href="#03-read-16-HiraginoSansGB-W3-34" x="221.099976"/>
     <use xlink:href="#03-read-16-HiraginoSansGB-W3-33" x="287.199966"/>
    </g>
   </g>
   <g id="03-read-16-text_77">
    <!-- 1.3695 -->
    <g style="fill: #405570" transform="translate(940.668192 211.125517) scale(0.1 -0.1)">
     <use xlink:href="#03-read-16-HiraginoSansGB-W3-31"/>
     <use xlink:href="#03-read-16-HiraginoSansGB-W3-2e" x="65.699997"/>
     <use xlink:href="#03-read-16-HiraginoSansGB-W3-33" x="89.699982"/>
     <use xlink:href="#03-read-16-HiraginoSansGB-W3-36" x="155.399979"/>
     <use xlink:href="#03-read-16-HiraginoSansGB-W3-39" x="221.099976"/>
     <use xlink:href="#03-read-16-HiraginoSansGB-W3-35" x="286.799973"/>
    </g>
   </g>
   <g id="03-read-16-text_78">
    <!-- 1.3670 -->
    <g style="fill: #405570" transform="translate(940.130121 297.468697) scale(0.1 -0.1)">
     <use xlink:href="#03-read-16-HiraginoSansGB-W3-31"/>
     <use xlink:href="#03-read-16-HiraginoSansGB-W3-2e" x="65.699997"/>
     <use xlink:href="#03-read-16-HiraginoSansGB-W3-33" x="89.699982"/>
     <use xlink:href="#03-read-16-HiraginoSansGB-W3-36" x="155.399979"/>
     <use xlink:href="#03-read-16-HiraginoSansGB-W3-37" x="221.099976"/>
     <use xlink:href="#03-read-16-HiraginoSansGB-W3-30" x="286.799973"/>
    </g>
   </g>
   <g id="03-read-16-text_79">
    <!-- 1.3627 -->
    <g style="fill: #405570" transform="translate(939.204638 383.811877) scale(0.1 -0.1)">
     <use xlink:href="#03-read-16-HiraginoSansGB-W3-31"/>
     <use xlink:href="#03-read-16-HiraginoSansGB-W3-2e" x="65.699997"/>
     <use xlink:href="#03-read-16-HiraginoSansGB-W3-33" x="89.699982"/>
     <use xlink:href="#03-read-16-HiraginoSansGB-W3-36" x="155.399979"/>
     <use xlink:href="#03-read-16-HiraginoSansGB-W3-32" x="221.099976"/>
     <use xlink:href="#03-read-16-HiraginoSansGB-W3-37" x="286.799973"/>
    </g>
   </g>
   <g id="03-read-16-text_80">
    <!-- 1.1760 -->
    <g style="fill: #405570" transform="translate(899.021468 470.155057) scale(0.1 -0.1)">
     <use xlink:href="#03-read-16-HiraginoSansGB-W3-31"/>
     <use xlink:href="#03-read-16-HiraginoSansGB-W3-2e" x="65.699997"/>
     <use xlink:href="#03-read-16-HiraginoSansGB-W3-31" x="89.699982"/>
     <use xlink:href="#03-read-16-HiraginoSansGB-W3-37" x="155.399979"/>
     <use xlink:href="#03-read-16-HiraginoSansGB-W3-36" x="221.099976"/>
     <use xlink:href="#03-read-16-HiraginoSansGB-W3-30" x="286.799973"/>
    </g>
   </g>
   <g id="03-read-16-text_81">
    <!-- 1.3352 -->
    <g style="fill: #405570" transform="translate(933.285853 556.498237) scale(0.1 -0.1)">
     <use xlink:href="#03-read-16-HiraginoSansGB-W3-31"/>
     <use xlink:href="#03-read-16-HiraginoSansGB-W3-2e" x="65.699997"/>
     <use xlink:href="#03-read-16-HiraginoSansGB-W3-33" x="89.699982"/>
     <use xlink:href="#03-read-16-HiraginoSansGB-W3-33" x="155.399979"/>
     <use xlink:href="#03-read-16-HiraginoSansGB-W3-35" x="221.099976"/>
     <use xlink:href="#03-read-16-HiraginoSansGB-W3-32" x="286.799973"/>
    </g>
   </g>
   <g id="03-read-16-text_82">
    <!-- 1.4005 -->
    <g style="fill: #008477" transform="translate(947.340277 142.914404) scale(0.1 -0.1)">
     <use xlink:href="#03-read-16-HiraginoSansGB-W3-31"/>
     <use xlink:href="#03-read-16-HiraginoSansGB-W3-2e" x="65.699997"/>
     <use xlink:href="#03-read-16-HiraginoSansGB-W3-34" x="89.699982"/>
     <use xlink:href="#03-read-16-HiraginoSansGB-W3-30" x="155.799973"/>
     <use xlink:href="#03-read-16-HiraginoSansGB-W3-30" x="221.499969"/>
     <use xlink:href="#03-read-16-HiraginoSansGB-W3-35" x="287.199966"/>
    </g>
   </g>
   <g id="03-read-16-text_83">
    <!-- 1.3657 -->
    <g style="fill: #008477" transform="translate(939.850324 229.257584) scale(0.1 -0.1)">
     <use xlink:href="#03-read-16-HiraginoSansGB-W3-31"/>
     <use xlink:href="#03-read-16-HiraginoSansGB-W3-2e" x="65.699997"/>
     <use xlink:href="#03-read-16-HiraginoSansGB-W3-33" x="89.699982"/>
     <use xlink:href="#03-read-16-HiraginoSansGB-W3-36" x="155.399979"/>
     <use xlink:href="#03-read-16-HiraginoSansGB-W3-35" x="221.099976"/>
     <use xlink:href="#03-read-16-HiraginoSansGB-W3-37" x="286.799973"/>
    </g>
   </g>
   <g id="03-read-16-text_84">
    <!-- 1.3683 -->
    <g style="fill: #008477" transform="translate(940.409918 315.600764) scale(0.1 -0.1)">
     <use xlink:href="#03-read-16-HiraginoSansGB-W3-31"/>
     <use xlink:href="#03-read-16-HiraginoSansGB-W3-2e" x="65.699997"/>
     <use xlink:href="#03-read-16-HiraginoSansGB-W3-33" x="89.699982"/>
     <use xlink:href="#03-read-16-HiraginoSansGB-W3-36" x="155.399979"/>
     <use xlink:href="#03-read-16-HiraginoSansGB-W3-38" x="221.099976"/>
     <use xlink:href="#03-read-16-HiraginoSansGB-W3-33" x="286.799973"/>
    </g>
   </g>
   <g id="03-read-16-text_85">
    <!-- 1.3632 -->
    <g style="fill: #008477" transform="translate(939.312252 401.943945) scale(0.1 -0.1)">
     <use xlink:href="#03-read-16-HiraginoSansGB-W3-31"/>
     <use xlink:href="#03-read-16-HiraginoSansGB-W3-2e" x="65.699997"/>
     <use xlink:href="#03-read-16-HiraginoSansGB-W3-33" x="89.699982"/>
     <use xlink:href="#03-read-16-HiraginoSansGB-W3-36" x="155.399979"/>
     <use xlink:href="#03-read-16-HiraginoSansGB-W3-33" x="221.099976"/>
     <use xlink:href="#03-read-16-HiraginoSansGB-W3-32" x="286.799973"/>
    </g>
   </g>
   <g id="03-read-16-text_86">
    <!-- 1.1769 -->
    <g style="fill: #008477" transform="translate(899.215174 488.287125) scale(0.1 -0.1)">
     <use xlink:href="#03-read-16-HiraginoSansGB-W3-31"/>
     <use xlink:href="#03-read-16-HiraginoSansGB-W3-2e" x="65.699997"/>
     <use xlink:href="#03-read-16-HiraginoSansGB-W3-31" x="89.699982"/>
     <use xlink:href="#03-read-16-HiraginoSansGB-W3-37" x="155.399979"/>
     <use xlink:href="#03-read-16-HiraginoSansGB-W3-36" x="221.099976"/>
     <use xlink:href="#03-read-16-HiraginoSansGB-W3-39" x="286.799973"/>
    </g>
   </g>
   <g id="03-read-16-text_87">
    <!-- 1.3190 -->
    <g style="fill: #008477" transform="translate(929.79915 574.630305) scale(0.1 -0.1)">
     <use xlink:href="#03-read-16-HiraginoSansGB-W3-31"/>
     <use xlink:href="#03-read-16-HiraginoSansGB-W3-2e" x="65.699997"/>
     <use xlink:href="#03-read-16-HiraginoSansGB-W3-33" x="89.699982"/>
     <use xlink:href="#03-read-16-HiraginoSansGB-W3-31" x="155.399979"/>
     <use xlink:href="#03-read-16-HiraginoSansGB-W3-39" x="221.099976"/>
     <use xlink:href="#03-read-16-HiraginoSansGB-W3-30" x="286.799973"/>
    </g>
   </g>
   <g id="03-read-16-text_88">
    <!-- D 侧 -->
    <g style="fill: #192c46" transform="translate(819.287421 42.149543) scale(0.15 -0.15)">
     <use xlink:href="#03-read-16-HiraginoSansGB-W3-44"/>
     <use xlink:href="#03-read-16-HiraginoSansGB-W3-20" x="75.799988"/>
     <use xlink:href="#03-read-16-HiraginoSansGB-W3-4fa7" x="109.099976"/>
    </g>
   </g>
   <g id="03-read-16-legend_2">
    <g id="03-read-16-patch_58">
     <path d="M 734.835937 669.581968 
L 756.835937 669.581968 
L 756.835937 661.881968 
L 734.835937 661.881968 
z
" style="fill: url(#03-read-16-hc447d1c890); stroke: #467bb5; stroke-width: 0.5; stroke-linejoin: miter"/>
    </g>
    <g id="03-read-16-text_89">
     <!-- TP2 独立池 -->
     <g style="fill: #192c46" transform="translate(765.635937 669.581968) scale(0.11 -0.11)">
      <use xlink:href="#03-read-16-HiraginoSansGB-W3-54"/>
      <use xlink:href="#03-read-16-HiraginoSansGB-W3-50" x="64.899994"/>
      <use xlink:href="#03-read-16-HiraginoSansGB-W3-32" x="131.099991"/>
      <use xlink:href="#03-read-16-HiraginoSansGB-W3-20" x="196.799988"/>
      <use xlink:href="#03-read-16-HiraginoSansGB-W3-72ec" x="230.099976"/>
      <use xlink:href="#03-read-16-HiraginoSansGB-W3-7acb" x="330.09996"/>
      <use xlink:href="#03-read-16-HiraginoSansGB-W3-6c60" x="430.099945"/>
     </g>
    </g>
    <g id="03-read-16-patch_59">
     <path d="M 734.835937 685.751968 
L 756.835937 685.751968 
L 756.835937 678.051968 
L 734.835937 678.051968 
z
" style="fill: #467bb5; stroke: #467bb5; stroke-width: 0.5; stroke-linejoin: miter"/>
    </g>
    <g id="03-read-16-text_90">
     <!-- TP2 共享池 -->
     <g style="fill: #192c46" transform="translate(765.635937 685.751968) scale(0.11 -0.11)">
      <use xlink:href="#03-read-16-HiraginoSansGB-W3-54"/>
      <use xlink:href="#03-read-16-HiraginoSansGB-W3-50" x="64.899994"/>
      <use xlink:href="#03-read-16-HiraginoSansGB-W3-32" x="131.099991"/>
      <use xlink:href="#03-read-16-HiraginoSansGB-W3-20" x="196.799988"/>
      <use xlink:href="#03-read-16-HiraginoSansGB-W3-5171" x="230.099976"/>
      <use xlink:href="#03-read-16-HiraginoSansGB-W3-4eab" x="330.09996"/>
      <use xlink:href="#03-read-16-HiraginoSansGB-W3-6c60" x="430.099945"/>
     </g>
    </g>
    <g id="03-read-16-patch_60">
     <path d="M 845.947968 669.581968 
L 867.947968 669.581968 
L 867.947968 661.881968 
L 845.947968 661.881968 
z
" style="fill: url(#03-read-16-hdea91fd583); stroke: #008477; stroke-width: 0.5; stroke-linejoin: miter"/>
    </g>
    <g id="03-read-16-text_91">
     <!-- TP4 独立池 -->
     <g style="fill: #192c46" transform="translate(876.747968 669.581968) scale(0.11 -0.11)">
      <use xlink:href="#03-read-16-HiraginoSansGB-W3-54"/>
      <use xlink:href="#03-read-16-HiraginoSansGB-W3-50" x="64.899994"/>
      <use xlink:href="#03-read-16-HiraginoSansGB-W3-34" x="131.099991"/>
      <use xlink:href="#03-read-16-HiraginoSansGB-W3-20" x="197.199982"/>
      <use xlink:href="#03-read-16-HiraginoSansGB-W3-72ec" x="230.499969"/>
      <use xlink:href="#03-read-16-HiraginoSansGB-W3-7acb" x="330.499954"/>
      <use xlink:href="#03-read-16-HiraginoSansGB-W3-6c60" x="430.499939"/>
     </g>
    </g>
    <g id="03-read-16-patch_61">
     <path d="M 845.947968 685.751968 
L 867.947968 685.751968 
L 867.947968 678.051968 
L 845.947968 678.051968 
z
" style="fill: #008477; stroke: #008477; stroke-width: 0.5; stroke-linejoin: miter"/>
    </g>
    <g id="03-read-16-text_92">
     <!-- TP4 共享池 -->
     <g style="fill: #192c46" transform="translate(876.747968 685.751968) scale(0.11 -0.11)">
      <use xlink:href="#03-read-16-HiraginoSansGB-W3-54"/>
      <use xlink:href="#03-read-16-HiraginoSansGB-W3-50" x="64.899994"/>
      <use xlink:href="#03-read-16-HiraginoSansGB-W3-34" x="131.099991"/>
      <use xlink:href="#03-read-16-HiraginoSansGB-W3-20" x="197.199982"/>
      <use xlink:href="#03-read-16-HiraginoSansGB-W3-5171" x="230.499969"/>
      <use xlink:href="#03-read-16-HiraginoSansGB-W3-4eab" x="330.499954"/>
      <use xlink:href="#03-read-16-HiraginoSansGB-W3-6c60" x="430.499939"/>
     </g>
    </g>
   </g>
  </g>
  <g id="03-read-16-text_93">
   <!-- D · 16 请求：两种池模式的读取耗时均随 TP 数量增加 -->
   <g style="fill: #192c46" transform="translate(297.168041 20.986875) scale(0.18 -0.18)">
    <defs>
     <path id="03-read-16-HiraginoSansGB-W3-8bf7" d="M 3936 4531 
L 3936 4070 
L 3213 4070 
C 2938 4070 2784 4077 2624 4102 
L 2624 3718 
C 2810 3744 2957 3750 3206 3750 
L 3936 3750 
L 3936 3283 
L 2778 3283 
C 2464 3283 2285 3296 2125 3322 
L 2125 2912 
C 2310 2944 2477 2950 2778 2950 
L 5517 2950 
C 5805 2950 5978 2938 6157 2912 
L 6157 3322 
C 5984 3296 5811 3283 5517 3283 
L 4301 3283 
L 4301 3750 
L 5171 3750 
C 5434 3750 5587 3744 5766 3718 
L 5766 4102 
C 5606 4077 5459 4070 5178 4070 
L 4301 4070 
L 4301 4531 
L 5402 4531 
C 5690 4531 5843 4525 6016 4499 
L 6016 4890 
C 5830 4858 5715 4851 5402 4851 
L 4301 4851 
L 4301 4864 
C 4301 5101 4307 5216 4339 5382 
L 3898 5382 
C 3930 5216 3936 5094 3936 4864 
L 3936 4851 
L 2957 4851 
C 2662 4851 2547 4858 2362 4890 
L 2362 4499 
C 2534 4525 2669 4531 2957 4531 
L 3936 4531 
z
M 3136 614 
L 5242 614 
L 5242 154 
C 5242 0 5197 -32 5018 -32 
C 4819 -32 4563 -6 4307 45 
C 4365 -51 4397 -160 4422 -365 
C 4704 -378 4851 -384 5011 -384 
C 5510 -384 5626 -288 5626 109 
L 5626 1862 
C 5626 2189 5632 2381 5651 2547 
C 5523 2522 5370 2515 5062 2515 
L 3328 2515 
C 3066 2515 2912 2522 2733 2541 
C 2746 2368 2752 2278 2752 1984 
L 2752 243 
C 2752 -96 2746 -275 2720 -442 
L 3174 -442 
C 3149 -282 3136 -90 3136 243 
L 3136 614 
z
M 3136 934 
L 3136 1421 
L 5242 1421 
L 5242 934 
L 3136 934 
z
M 3136 1741 
L 3136 2182 
L 5242 2182 
L 5242 1741 
L 3136 1741 
z
M 1184 333 
C 1165 243 1120 179 1018 128 
L 1178 -256 
C 1414 -102 1517 -32 1555 -13 
C 1875 160 1875 160 2150 326 
C 2214 365 2310 422 2432 499 
L 2406 947 
C 2048 710 1907 627 1600 454 
L 1600 2720 
C 1600 3040 1606 3194 1632 3398 
C 1478 3379 1325 3366 1139 3366 
L 813 3366 
C 576 3366 480 3373 320 3398 
L 320 2950 
C 480 2976 576 2982 787 2982 
L 1184 2982 
L 1184 333 
z
M 826 5024 
C 1242 4717 1517 4461 1837 4064 
L 2144 4397 
C 1798 4762 1530 5005 1133 5306 
L 826 5024 
z
" transform="scale(0.015625)"/>
     <path id="03-read-16-HiraginoSansGB-W3-6c42" d="M 2982 4147 
L 1120 4147 
C 826 4147 640 4160 461 4186 
L 461 3744 
C 640 3770 838 3782 1107 3782 
L 2982 3782 
L 2982 192 
C 2982 45 2938 13 2688 13 
C 2438 13 2176 38 1850 90 
C 1914 -77 1933 -166 1952 -333 
C 2214 -358 2496 -371 2739 -371 
C 3008 -371 3130 -352 3219 -301 
C 3334 -230 3392 -96 3392 109 
L 3392 1997 
C 3386 2246 3379 2381 3379 2400 
C 3373 2534 3373 2534 3366 2707 
C 3360 2899 3360 2912 3354 3059 
C 3494 2624 3584 2432 3763 2118 
C 4006 1683 4301 1293 4634 947 
C 5018 557 5280 346 5747 58 
C 5850 218 5933 320 6048 435 
C 5574 685 5306 870 4947 1210 
C 4691 1453 4486 1677 4269 1971 
C 4627 2266 4934 2554 5152 2790 
C 5485 3155 5485 3155 5530 3206 
L 5171 3482 
C 4966 3123 4582 2701 4064 2272 
C 3763 2752 3533 3277 3405 3782 
L 5293 3782 
C 5581 3782 5779 3770 5952 3744 
L 5952 4186 
C 5773 4160 5581 4147 5293 4147 
L 3392 4147 
L 3392 4640 
C 3392 4947 3405 5126 3430 5325 
L 2944 5325 
C 2970 5139 2982 4947 2982 4640 
L 2982 4147 
z
M 3942 5024 
C 4384 4768 4570 4627 4922 4275 
L 5210 4550 
C 4826 4890 4672 5005 4218 5274 
L 3942 5024 
z
M 1069 3187 
C 1434 2829 1651 2554 1939 2061 
L 2278 2330 
C 1965 2797 1779 3027 1382 3430 
L 1069 3187 
z
M 2688 1939 
C 2266 1613 1990 1434 1459 1139 
C 1043 909 794 800 499 717 
L 710 282 
C 819 352 890 397 1024 467 
C 1933 960 2195 1120 2803 1555 
L 2688 1939 
z
" transform="scale(0.015625)"/>
     <path id="03-read-16-HiraginoSansGB-W3-ff1a" d="M 1722 3488 
L 1088 3488 
L 1088 2854 
L 1722 2854 
L 1722 3488 
z
M 1722 38 
L 1722 672 
L 1088 672 
L 1088 38 
L 1722 38 
z
" transform="scale(0.015625)"/>
     <path id="03-read-16-HiraginoSansGB-W3-4e24" d="M 3232 1683 
C 2989 2202 2854 2464 2554 2989 
C 2579 3187 2579 3187 2598 3341 
L 3814 3341 
C 3782 2931 3706 2509 3590 2182 
C 3411 1651 3181 1306 2803 979 
C 2925 870 2995 787 3078 659 
C 3418 1018 3571 1248 3782 1709 
C 3910 1997 4013 2272 4058 2477 
C 4435 1773 4454 1734 4883 787 
L 5210 1030 
C 4986 1491 4602 2214 4358 2637 
C 4211 2880 4211 2880 4147 2995 
C 4166 3162 4166 3219 4179 3341 
L 5254 3341 
L 5254 166 
C 5254 19 5210 -13 4998 -13 
C 4717 -13 4461 13 4083 90 
C 4141 -64 4160 -147 4173 -346 
C 4506 -378 4717 -390 4922 -390 
C 5216 -390 5363 -371 5459 -320 
C 5606 -250 5658 -134 5658 109 
L 5658 3142 
C 5658 3386 5664 3552 5683 3731 
C 5510 3712 5344 3706 5082 3706 
L 4205 3706 
C 4230 4250 4230 4250 4237 4691 
L 5357 4691 
C 5606 4691 5830 4678 6029 4653 
L 6029 5088 
C 5837 5062 5619 5050 5357 5050 
L 1062 5050 
C 787 5050 595 5062 390 5088 
L 390 4653 
C 614 4678 800 4691 1062 4691 
L 2259 4691 
C 2259 4160 2253 3942 2240 3706 
L 1331 3706 
C 1062 3706 909 3712 742 3731 
C 762 3546 768 3347 768 3123 
L 768 250 
C 768 -32 755 -250 730 -454 
L 1210 -454 
C 1184 -269 1171 -45 1171 250 
L 1171 3341 
L 2214 3341 
C 2176 2893 2106 2509 1990 2189 
C 1805 1651 1562 1280 1197 973 
C 1299 896 1370 813 1485 640 
C 1869 1050 2022 1274 2214 1702 
C 2330 1965 2394 2170 2464 2477 
C 2624 2182 2784 1792 2918 1402 
L 3232 1683 
z
M 3859 4691 
L 3859 4467 
C 3859 4179 3859 4179 3840 3706 
L 2624 3706 
C 2643 4090 2643 4115 2656 4691 
L 3859 4691 
z
" transform="scale(0.015625)"/>
     <path id="03-read-16-HiraginoSansGB-W3-79cd" d="M 1357 3686 
L 870 3686 
C 614 3686 474 3693 314 3725 
L 314 3296 
C 474 3322 627 3334 864 3334 
L 1299 3334 
C 1062 2515 685 1824 173 1280 
C 262 1184 333 1075 410 922 
C 646 1216 781 1408 954 1734 
C 1158 2112 1254 2355 1389 2816 
C 1370 2432 1357 2093 1357 1792 
L 1357 288 
C 1357 -19 1344 -230 1318 -454 
L 1773 -454 
C 1747 -250 1734 -38 1734 288 
L 1734 2714 
C 1984 2432 2170 2182 2387 1856 
L 2650 2106 
C 2355 2509 2144 2746 1734 3123 
L 1734 3334 
L 2144 3334 
C 2342 3334 2451 3328 2592 3302 
L 2592 3718 
C 2477 3693 2355 3686 2144 3686 
L 1734 3686 
L 1734 4691 
C 2054 4781 2291 4864 2483 4954 
C 2598 5005 2598 5005 2643 5018 
L 2387 5363 
C 2016 5101 1267 4890 320 4787 
C 422 4646 467 4563 506 4435 
C 934 4506 1088 4538 1357 4595 
L 1357 3686 
z
M 4141 1613 
L 4141 314 
C 4141 0 4128 -275 4102 -467 
L 4576 -467 
C 4550 -301 4538 -13 4538 320 
L 4538 1613 
L 5568 1613 
L 5568 1229 
L 6010 1229 
C 5984 1382 5978 1523 5978 1843 
L 5978 3514 
C 5978 3763 5984 3898 6003 4058 
C 5875 4038 5702 4032 5414 4032 
L 4538 4032 
L 4538 4582 
C 4538 4902 4550 5088 4576 5286 
L 4102 5286 
C 4128 5088 4141 4877 4141 4595 
L 4141 4032 
L 3341 4032 
C 3046 4032 2880 4038 2752 4058 
C 2771 3898 2778 3763 2778 3520 
L 2778 1805 
C 2778 1517 2771 1389 2752 1203 
L 3168 1203 
L 3168 1613 
L 4141 1613 
z
M 4141 3674 
L 4141 1958 
L 3168 1958 
L 3168 3674 
L 4141 3674 
z
M 4538 3674 
L 5568 3674 
L 5568 1958 
L 4538 1958 
L 4538 3674 
z
" transform="scale(0.015625)"/>
     <path id="03-read-16-HiraginoSansGB-W3-6a21" d="M 5472 966 
C 5741 966 5901 954 6093 928 
L 6093 1338 
C 5907 1312 5728 1306 5466 1306 
L 4109 1306 
C 4134 1453 4141 1542 4160 1798 
L 5094 1798 
C 5344 1798 5517 1792 5690 1773 
C 5664 1933 5664 2048 5664 2304 
L 5664 3328 
C 5664 3565 5670 3693 5690 3846 
C 5536 3827 5363 3821 5101 3821 
L 3040 3821 
C 2771 3821 2592 3827 2451 3846 
C 2464 3699 2470 3552 2470 3334 
L 2470 2304 
C 2470 2093 2464 1946 2451 1773 
C 2650 1792 2842 1798 3034 1798 
L 3776 1798 
C 3750 1453 3750 1446 3725 1306 
L 2714 1306 
C 2419 1306 2221 1312 2067 1331 
L 2067 934 
C 2259 954 2464 966 2707 966 
L 3610 966 
C 3482 685 3309 486 3040 301 
C 2752 102 2394 -26 1824 -154 
C 1939 -288 1984 -371 2042 -506 
C 3046 -237 3635 160 3942 794 
C 4339 147 4960 -256 5946 -512 
C 5997 -358 6054 -256 6150 -122 
C 5197 45 4614 371 4237 966 
L 5472 966 
z
M 2842 3488 
L 5293 3488 
L 5293 2982 
L 2842 2982 
L 2842 3488 
z
M 2842 2656 
L 5293 2656 
L 5293 2125 
L 2842 2125 
L 2842 2656 
z
M 1491 2893 
C 1651 2643 1773 2438 1939 2118 
L 2221 2355 
C 2048 2630 1818 2957 1574 3251 
L 1491 3366 
L 1491 3693 
L 1754 3693 
C 1901 3693 2029 3686 2170 3674 
L 2170 4064 
C 2042 4045 1946 4038 1754 4038 
L 1491 4038 
L 1491 4666 
C 1491 4986 1498 5165 1523 5325 
L 1075 5325 
C 1101 5171 1107 5030 1107 4666 
L 1107 4038 
L 768 4038 
C 563 4038 422 4045 320 4064 
L 320 3667 
C 442 3686 589 3693 768 3693 
L 1056 3693 
C 883 2861 563 2112 160 1587 
C 250 1485 307 1395 390 1210 
C 736 1722 922 2176 1139 3040 
C 1126 2675 1114 2278 1114 2035 
L 1114 218 
C 1114 -115 1101 -346 1075 -525 
L 1530 -525 
C 1498 -352 1491 -128 1491 218 
L 1491 2893 
z
M 3526 4390 
L 4531 4390 
L 4531 4352 
C 4531 4198 4525 4109 4512 3994 
L 4928 3994 
C 4909 4096 4909 4186 4909 4358 
L 4909 4390 
L 5421 4390 
C 5696 4390 5888 4384 6080 4358 
L 6080 4774 
C 5901 4742 5728 4736 5414 4736 
L 4909 4736 
L 4909 4858 
C 4909 5056 4915 5165 4934 5286 
L 4499 5286 
C 4525 5165 4531 5043 4531 4858 
L 4531 4736 
L 3526 4736 
L 3526 4858 
C 3526 5056 3539 5165 3558 5286 
L 3123 5286 
C 3149 5158 3155 5050 3155 4858 
L 3155 4736 
L 2765 4736 
C 2451 4736 2310 4742 2157 4768 
L 2157 4365 
C 2330 4384 2470 4390 2765 4390 
L 3155 4390 
L 3155 4352 
C 3155 4192 3149 4115 3130 3994 
L 3552 3994 
C 3533 4109 3526 4186 3526 4358 
L 3526 4390 
z
" transform="scale(0.015625)"/>
     <path id="03-read-16-HiraginoSansGB-W3-5f0f" d="M 5363 3693 
C 5632 3693 5837 3680 6022 3654 
L 6022 4115 
C 5862 4090 5677 4077 5402 4077 
L 4198 4077 
C 4186 4371 4186 4499 4186 4634 
C 4186 4896 4198 5082 4224 5222 
L 3731 5222 
C 3763 4992 3763 4986 3770 4762 
C 3776 4288 3776 4288 3782 4077 
L 1120 4077 
C 838 4077 640 4090 461 4115 
L 461 3654 
C 678 3680 858 3693 1120 3693 
L 3795 3693 
C 3802 3674 3802 3667 3802 3642 
C 3846 2573 4013 1677 4307 915 
C 4602 154 5043 -352 5421 -352 
C 5581 -352 5715 -262 5830 -83 
C 5952 115 6029 371 6144 915 
C 5978 986 5907 1037 5766 1165 
C 5664 461 5549 115 5408 115 
C 5363 115 5293 160 5203 237 
C 5011 410 4838 704 4672 1146 
C 4531 1510 4448 1830 4352 2349 
C 4275 2810 4256 2963 4218 3693 
L 5363 3693 
z
M 2310 2349 
L 2963 2349 
C 3206 2349 3373 2342 3533 2317 
L 3533 2746 
C 3373 2720 3232 2714 2957 2714 
L 1299 2714 
C 1024 2714 870 2720 717 2746 
L 717 2317 
C 877 2342 1050 2349 1293 2349 
L 1901 2349 
L 1901 544 
C 1082 390 1082 390 883 371 
C 627 346 627 346 499 346 
L 595 -109 
C 672 -90 794 -64 883 -45 
C 1946 147 2803 333 3750 582 
L 3750 973 
C 2835 730 2675 691 2310 621 
L 2310 2349 
z
M 5600 4474 
C 5325 4755 5158 4896 4787 5165 
L 4493 4947 
C 4845 4698 5018 4544 5299 4230 
L 5600 4474 
z
" transform="scale(0.015625)"/>
     <path id="03-read-16-HiraginoSansGB-W3-7684" d="M 1651 4333 
C 1747 4544 1862 4864 1978 5210 
L 1523 5299 
C 1472 4966 1376 4646 1248 4333 
L 1120 4333 
C 800 4333 685 4339 550 4352 
C 570 4192 576 4045 576 3744 
L 576 499 
C 576 122 570 -115 544 -282 
L 973 -282 
L 973 282 
L 2221 282 
L 2221 -128 
L 2643 -128 
C 2624 26 2611 243 2611 531 
L 2611 3789 
C 2611 4019 2618 4186 2630 4352 
C 2573 4346 2387 4339 2067 4333 
L 1651 4333 
z
M 973 3981 
L 2221 3981 
L 2221 2554 
L 973 2554 
L 973 3981 
z
M 973 2202 
L 2221 2202 
L 2221 640 
L 973 640 
L 973 2202 
z
M 3846 4339 
C 3949 4595 4013 4768 4090 4992 
C 4128 5107 4160 5197 4179 5235 
L 3738 5318 
C 3725 5178 3661 4941 3571 4698 
C 3334 4045 3021 3501 2630 3085 
C 2739 3014 2854 2906 2938 2797 
C 3270 3226 3456 3520 3674 3962 
L 5517 3962 
C 5510 3021 5504 2579 5472 2067 
C 5427 1210 5344 595 5235 301 
C 5152 96 5069 38 4832 38 
C 4602 38 4314 70 3923 147 
C 3994 -13 4019 -122 4032 -307 
C 4429 -339 4614 -352 4845 -352 
C 5229 -352 5434 -237 5562 58 
C 5792 570 5875 1427 5933 3962 
C 5939 4198 5939 4250 5958 4365 
C 5830 4346 5651 4339 5395 4339 
L 3846 4339 
z
M 4742 1549 
C 4403 2234 4134 2656 3731 3142 
L 3405 2944 
C 3776 2496 4019 2093 4378 1325 
L 4742 1549 
z
" transform="scale(0.015625)"/>
     <path id="03-read-16-HiraginoSansGB-W3-8bfb" d="M 3859 1267 
C 3642 614 3040 192 1997 -64 
C 2144 -218 2170 -256 2234 -416 
C 2842 -224 3206 -51 3558 218 
C 3917 486 4160 858 4262 1267 
L 5459 1267 
C 5747 1267 5952 1254 6112 1235 
L 6112 1664 
C 5971 1638 5786 1632 5466 1632 
L 4326 1632 
C 4365 1901 4390 2214 4403 2541 
C 4422 3014 4422 3014 4454 3174 
L 4000 3181 
C 4026 3034 4026 2982 4026 2803 
C 4026 2374 3994 1933 3949 1632 
L 2765 1632 
C 2432 1632 2266 1645 2118 1670 
L 2118 1235 
C 2285 1254 2496 1267 2797 1267 
L 3859 1267 
z
M 3814 3853 
L 2694 3853 
C 2426 3853 2272 3866 2125 3891 
L 2125 3450 
C 2272 3475 2432 3488 2707 3488 
L 5453 3488 
C 5350 3187 5242 2957 5056 2662 
C 5222 2618 5306 2566 5434 2458 
C 5574 2720 5683 2950 5754 3123 
C 5862 3424 5888 3488 5939 3597 
L 5683 3872 
C 5562 3859 5466 3853 5248 3853 
L 4198 3853 
L 4198 4461 
L 5184 4461 
C 5459 4461 5664 4448 5837 4422 
L 5837 4851 
C 5690 4826 5459 4813 5184 4813 
L 4198 4813 
C 4205 5005 4218 5184 4237 5306 
L 3776 5306 
C 3802 5165 3808 5050 3814 4813 
L 3027 4813 
C 2746 4813 2502 4826 2362 4851 
L 2362 4422 
C 2515 4448 2752 4461 3027 4461 
L 3814 4461 
L 3814 3853 
z
M 1216 2957 
L 1210 602 
C 1210 461 1165 390 1018 275 
L 1184 -122 
C 1229 -96 1261 -77 1280 -64 
C 1395 6 1466 45 1498 64 
C 1734 211 1734 211 1837 269 
C 1997 378 1997 378 2131 448 
C 2170 474 2227 506 2304 550 
L 2272 1005 
C 1978 787 1882 730 1613 570 
L 1613 2682 
C 1613 3014 1619 3155 1645 3360 
C 1498 3341 1338 3328 1158 3328 
L 774 3328 
C 538 3328 442 3334 282 3360 
L 282 2925 
C 442 2950 538 2957 749 2957 
L 1216 2957 
z
M 3706 2746 
C 3341 3014 3142 3142 2778 3341 
L 2541 3085 
C 2938 2874 3142 2733 3475 2458 
L 3706 2746 
z
M 864 5018 
C 1248 4762 1536 4499 1888 4115 
L 2189 4435 
C 1811 4819 1504 5075 1171 5293 
L 864 5018 
z
M 6054 -13 
C 5549 282 5069 512 4467 755 
L 4275 454 
C 5203 26 5370 -58 5862 -365 
L 6054 -13 
z
M 3277 2125 
C 2957 2355 2874 2406 2317 2707 
L 2086 2438 
C 2451 2253 2656 2125 3053 1818 
L 3277 2125 
z
" transform="scale(0.015625)"/>
     <path id="03-read-16-HiraginoSansGB-W3-53d6" d="M 2899 4717 
L 2925 4717 
C 3078 4717 3078 4717 3360 4704 
L 3360 5101 
C 3213 5082 3098 5075 2880 5075 
L 851 5075 
C 659 5075 506 5088 352 5107 
L 352 4685 
C 512 4710 653 4717 819 4717 
L 870 4717 
L 870 928 
C 659 902 659 902 634 896 
C 403 864 397 864 288 864 
L 416 403 
C 621 474 864 525 1312 595 
C 1805 678 1805 678 2048 723 
C 2419 787 2419 787 2496 800 
L 2496 173 
C 2496 -83 2483 -307 2458 -493 
L 2938 -493 
C 2912 -339 2899 -115 2899 166 
L 2899 870 
C 3085 902 3232 928 3341 954 
L 3309 1318 
L 2899 1242 
L 2899 4717 
z
M 2496 4717 
L 2496 3770 
L 1274 3770 
L 1274 4717 
L 2496 4717 
z
M 2496 3437 
L 2496 2470 
L 1274 2470 
L 1274 3437 
L 2496 3437 
z
M 2496 2131 
L 2496 1178 
L 1274 986 
L 1274 2131 
L 2496 2131 
z
M 3725 4640 
C 3488 4640 3347 4646 3187 4672 
L 3187 4237 
C 3309 4256 3392 4262 3565 4269 
C 3642 3622 3712 3245 3827 2822 
C 3974 2298 4173 1818 4429 1376 
C 4038 819 3603 422 2995 58 
C 3104 -51 3174 -166 3251 -326 
C 3853 77 4192 403 4653 1018 
C 5024 480 5370 141 6035 -320 
C 6112 -134 6189 -13 6285 96 
C 5626 499 5280 826 4896 1376 
C 5395 2195 5754 3206 5901 4205 
C 5933 4448 5952 4582 5971 4666 
C 5818 4646 5658 4640 5491 4640 
L 3725 4640 
z
M 5498 4269 
C 5382 3539 5280 3142 5069 2605 
C 4947 2278 4819 2016 4666 1747 
C 4474 2106 4320 2490 4198 2931 
C 4077 3366 4038 3584 3955 4269 
L 5498 4269 
z
" transform="scale(0.015625)"/>
     <path id="03-read-16-HiraginoSansGB-W3-8017" d="M 1856 1760 
C 2131 1498 2323 1286 2554 992 
L 2797 1254 
C 2445 1658 2227 1862 1856 2150 
L 1856 2362 
L 2291 2362 
C 2534 2362 2650 2355 2810 2330 
L 2810 2733 
C 2669 2707 2554 2701 2310 2701 
L 1856 2701 
L 1856 3309 
L 2170 3309 
C 2381 3309 2509 3302 2662 3283 
L 2662 3667 
C 2528 3648 2406 3642 2189 3642 
L 1856 3642 
L 1856 4237 
L 2259 4237 
C 2515 4237 2618 4230 2784 4205 
L 2784 4608 
C 2630 4582 2522 4576 2278 4576 
L 1856 4576 
L 1856 4800 
C 1856 5037 1862 5184 1888 5344 
L 1440 5344 
C 1466 5184 1478 5018 1478 4794 
L 1478 4576 
L 1030 4576 
C 774 4576 646 4582 486 4608 
L 486 4205 
C 640 4230 768 4237 1018 4237 
L 1478 4237 
L 1478 3642 
L 1114 3642 
C 883 3642 755 3648 614 3667 
L 614 3283 
C 762 3302 896 3309 1114 3309 
L 1478 3309 
L 1478 2701 
L 915 2701 
C 653 2701 531 2707 384 2733 
L 384 2330 
C 544 2355 691 2362 915 2362 
L 1370 2362 
C 1146 1747 742 1139 282 730 
C 390 614 454 518 518 371 
C 1018 928 1203 1222 1510 1965 
C 1485 1626 1478 1408 1478 1114 
L 1478 166 
C 1478 -122 1466 -288 1440 -467 
L 1894 -467 
C 1862 -288 1856 -96 1856 160 
L 1856 1760 
z
M 3904 3347 
L 3469 3283 
C 3232 3245 3053 3226 2931 3226 
C 2912 3226 2899 3226 2874 3226 
L 2925 2790 
C 3072 2835 3277 2880 3507 2918 
L 3904 2976 
L 3904 1965 
L 3392 1888 
C 3136 1850 2982 1830 2867 1830 
C 2848 1830 2829 1830 2803 1830 
L 2861 1395 
C 3002 1440 3226 1491 3443 1523 
L 3904 1594 
L 3904 173 
C 3904 -218 4070 -301 4870 -301 
C 5946 -301 6022 -218 6106 1037 
C 5914 1082 5869 1101 5702 1190 
C 5690 672 5658 397 5594 243 
C 5542 128 5350 83 4870 83 
C 4378 83 4301 109 4301 275 
L 4301 1651 
L 5453 1824 
C 5754 1869 5894 1882 6061 1888 
L 6003 2330 
C 5818 2266 5632 2221 5395 2189 
L 4301 2022 
L 4301 3040 
L 5280 3194 
C 5600 3245 5741 3264 5894 3264 
L 5837 3699 
C 5696 3642 5504 3597 5229 3558 
L 4301 3411 
L 4301 4403 
C 4838 4576 5235 4723 5581 4883 
C 5664 4922 5664 4922 5715 4947 
L 5376 5280 
C 5280 5197 5171 5133 4986 5050 
C 4403 4787 3693 4589 2918 4474 
C 3053 4294 3078 4250 3123 4109 
C 3488 4186 3603 4211 3904 4294 
L 3904 3347 
z
" transform="scale(0.015625)"/>
     <path id="03-read-16-HiraginoSansGB-W3-65f6" d="M 5254 3418 
L 5466 3418 
C 5670 3418 5875 3411 6074 3392 
L 6074 3827 
C 5907 3802 5722 3789 5459 3789 
L 5254 3789 
L 5254 4525 
C 5254 4838 5267 5056 5293 5274 
L 4800 5274 
C 4832 5088 4845 4826 4845 4525 
L 4845 3789 
L 3232 3789 
C 2963 3789 2733 3802 2566 3827 
L 2566 3379 
C 2733 3405 2982 3418 3245 3418 
L 4845 3418 
L 4845 256 
C 4845 115 4787 90 4525 90 
C 4224 90 4038 109 3648 166 
C 3718 6 3750 -90 3770 -269 
C 4064 -294 4288 -307 4550 -307 
C 5120 -307 5254 -205 5254 218 
L 5254 3418 
z
M 2842 2624 
C 3226 2170 3488 1773 3763 1197 
L 4115 1453 
C 3814 2003 3590 2323 3162 2835 
L 2842 2624 
z
M 486 589 
C 486 320 480 128 454 -90 
L 883 -90 
L 883 512 
L 1914 512 
L 1914 147 
L 2330 147 
C 2310 352 2304 480 2304 704 
L 2304 4262 
C 2304 4557 2310 4710 2323 4858 
C 2176 4845 2016 4838 1741 4838 
L 1050 4838 
C 781 4838 602 4845 467 4858 
C 486 4602 486 4499 486 4243 
L 486 589 
z
M 883 4480 
L 1914 4480 
L 1914 2938 
L 883 2938 
L 883 4480 
z
M 883 2579 
L 1914 2579 
L 1914 870 
L 883 870 
L 883 2579 
z
" transform="scale(0.015625)"/>
     <path id="03-read-16-HiraginoSansGB-W3-5747" d="M 1146 3578 
L 864 3578 
C 595 3578 467 3584 307 3616 
L 307 3174 
C 467 3206 595 3213 864 3213 
L 1146 3213 
L 1146 1139 
C 691 966 429 877 262 851 
L 384 429 
C 474 467 589 512 704 557 
C 1485 864 1760 992 2342 1312 
L 2323 1715 
C 1984 1523 1882 1466 1549 1312 
L 1549 3213 
L 1683 3213 
C 1914 3213 2054 3206 2195 3187 
L 2195 3610 
C 2074 3584 1946 3578 1690 3578 
L 1549 3578 
L 1549 4653 
C 1549 4915 1555 5069 1587 5261 
L 1107 5261 
C 1133 5062 1146 4902 1146 4640 
L 1146 3578 
z
M 5434 3955 
C 5427 2074 5357 1114 5197 474 
C 5114 141 5024 58 4774 58 
C 4557 58 4218 96 3866 166 
C 3942 -19 3968 -122 3981 -282 
C 4358 -320 4525 -333 4723 -333 
C 5011 -333 5165 -301 5280 -230 
C 5446 -115 5562 173 5664 742 
C 5786 1466 5843 2458 5843 3859 
C 5843 4122 5850 4237 5869 4365 
C 5754 4352 5754 4352 5293 4339 
L 3386 4339 
C 3501 4627 3546 4742 3616 4973 
C 3667 5114 3680 5165 3712 5235 
L 3270 5350 
C 3226 5037 3091 4621 2906 4211 
C 2675 3712 2464 3418 2067 3014 
C 2202 2931 2227 2912 2323 2797 
C 2330 2778 2349 2758 2374 2726 
C 2758 3149 2970 3462 3213 3955 
L 5434 3955 
z
M 4922 2048 
C 4301 1664 3776 1414 3066 1158 
C 2867 1088 2771 1062 2611 1030 
L 2778 582 
C 2848 621 2899 640 3066 710 
C 3936 1069 4314 1248 4998 1626 
L 4922 2048 
z
M 4608 2451 
C 4122 2746 3866 2874 3104 3200 
L 2867 2893 
C 3731 2509 3930 2400 4384 2093 
L 4608 2451 
z
" transform="scale(0.015625)"/>
     <path id="03-read-16-HiraginoSansGB-W3-968f" d="M 4058 3642 
C 4173 3853 4224 3955 4365 4269 
L 5466 4269 
C 5734 4269 5939 4262 6131 4237 
L 6131 4659 
C 5965 4634 5760 4621 5472 4621 
L 4499 4621 
C 4531 4717 4589 4870 4627 4998 
C 4666 5120 4666 5120 4678 5146 
C 4704 5210 4704 5210 4717 5235 
L 4294 5318 
C 4269 5107 4230 4922 4134 4621 
L 3770 4621 
C 3437 4621 3270 4627 3098 4659 
L 3098 4237 
C 3277 4262 3456 4269 3770 4269 
L 4000 4269 
C 3731 3642 3462 3264 2989 2842 
C 3117 2739 3174 2675 3245 2547 
C 3450 2778 3526 2867 3686 3085 
L 3686 1037 
C 3686 781 3674 582 3648 390 
L 4083 390 
C 4064 570 4051 781 4051 1024 
L 4051 1568 
L 5421 1568 
L 5421 966 
C 5421 851 5376 826 5197 826 
C 5043 826 4966 832 4710 870 
C 4762 736 4781 646 4800 493 
C 4947 493 5114 486 5235 486 
C 5658 486 5786 582 5786 902 
L 5786 3110 
C 5786 3334 5792 3514 5811 3674 
C 5645 3648 5472 3642 5235 3642 
L 4058 3642 
z
M 4051 1888 
L 4051 2445 
L 5421 2445 
L 5421 1888 
L 4051 1888 
z
M 4051 2765 
L 4051 3309 
L 5421 3309 
L 5421 2765 
L 4051 2765 
z
M 2637 736 
C 2528 691 2400 589 2112 333 
C 1965 198 1869 109 1670 -64 
L 1946 -358 
C 2394 154 2662 403 2758 403 
C 2790 403 2842 371 2880 326 
C 3072 96 3072 96 3117 58 
C 3270 -77 3488 -179 3706 -218 
C 3981 -262 4499 -288 5280 -288 
C 5555 -288 5741 -282 6125 -269 
C 6144 -192 6150 -160 6195 -32 
C 6214 0 6234 58 6259 134 
C 5702 90 5338 77 4934 77 
C 4486 77 4045 96 3859 122 
C 3622 154 3373 275 3226 435 
C 3027 653 3027 653 3002 678 
L 3002 2067 
C 3002 2362 3008 2534 3034 2720 
C 2944 2707 2867 2701 2720 2701 
L 2490 2701 
C 2374 2701 2259 2707 2138 2726 
L 2138 2336 
C 2291 2355 2342 2362 2451 2362 
L 2637 2362 
L 2637 736 
z
M 2189 4685 
C 2464 4243 2592 3974 2778 3482 
L 3098 3706 
C 2835 4301 2797 4371 2502 4883 
L 2189 4685 
z
M 570 243 
C 570 -32 557 -275 531 -448 
L 979 -448 
C 960 -294 954 -90 954 237 
L 954 4781 
L 1818 4781 
C 1594 4096 1427 3699 1158 3219 
C 1427 2925 1536 2765 1645 2496 
C 1734 2285 1773 2074 1773 1850 
C 1773 1504 1715 1421 1459 1421 
C 1357 1421 1248 1434 1114 1459 
C 1165 1318 1178 1229 1178 1069 
C 1286 1056 1338 1056 1408 1056 
C 1626 1056 1786 1082 1875 1120 
C 2042 1210 2138 1453 2138 1818 
C 2138 2048 2099 2278 2022 2490 
C 1933 2758 1830 2931 1568 3258 
C 1773 3642 1875 3872 2080 4410 
C 2221 4781 2221 4781 2272 4896 
L 2048 5158 
C 1965 5139 1856 5133 1683 5133 
L 979 5133 
C 832 5133 666 5139 544 5158 
C 563 5030 570 4832 570 4614 
L 570 243 
z
" transform="scale(0.015625)"/>
     <path id="03-read-16-HiraginoSansGB-W3-6570" d="M 6138 4115 
C 5965 4083 5837 4077 5542 4077 
L 4186 4077 
C 4294 4422 4314 4486 4397 4819 
C 4480 5133 4480 5133 4512 5222 
L 4077 5318 
C 4051 4915 3930 4410 3750 3942 
C 3526 3347 3296 2950 2963 2560 
C 3104 2483 3168 2438 3283 2310 
C 3501 2592 3597 2739 3750 3053 
C 3878 2285 4058 1760 4410 1171 
C 4019 634 3507 230 2752 -134 
C 2861 -243 2938 -339 3014 -493 
C 3731 -102 4211 288 4640 826 
C 5018 307 5478 -102 6061 -442 
C 6144 -262 6195 -173 6298 -45 
C 5690 275 5242 659 4877 1165 
C 5318 1875 5530 2618 5594 3712 
C 5888 3712 5997 3706 6138 3680 
L 6138 4115 
z
M 5197 3712 
C 5165 3136 5126 2867 5005 2451 
C 4909 2099 4813 1850 4640 1536 
C 4448 1875 4314 2195 4186 2643 
C 4096 2957 4096 2957 3994 3571 
C 4000 3590 4006 3597 4026 3642 
C 4032 3661 4038 3680 4051 3712 
L 5197 3712 
z
M 3014 314 
C 2765 474 2714 499 2445 640 
C 2803 1101 2957 1389 3162 2029 
C 2880 2016 2726 2010 2694 2010 
L 1798 2010 
C 1843 2099 1875 2176 1978 2426 
L 1587 2515 
C 1510 2259 1466 2150 1395 2010 
L 832 2010 
C 602 2010 486 2016 371 2042 
L 371 1638 
C 493 1664 614 1670 832 1670 
L 1222 1670 
C 1056 1382 986 1274 762 979 
C 1331 768 1498 698 1818 544 
C 1434 262 966 58 333 -96 
C 429 -218 480 -307 538 -448 
C 1350 -186 1830 51 2176 358 
C 2419 224 2515 160 2790 -38 
L 3014 314 
z
M 2650 1670 
C 2547 1389 2349 1075 2106 806 
C 1786 954 1670 1005 1299 1146 
C 1485 1427 1542 1517 1626 1670 
L 2650 1670 
z
M 2112 4019 
L 2112 4877 
C 2112 5056 2118 5165 2144 5306 
L 1715 5306 
C 1741 5158 1747 5043 1747 4877 
L 1747 4019 
L 915 4019 
C 666 4019 531 4026 397 4051 
L 397 3654 
C 531 3680 666 3686 902 3686 
L 1491 3686 
C 1338 3469 1325 3450 1235 3354 
C 979 3072 736 2893 288 2624 
C 410 2528 480 2451 570 2317 
C 1197 2765 1446 3021 1773 3578 
C 1754 3398 1747 3328 1747 3206 
L 1747 2848 
C 1747 2758 1741 2669 1722 2586 
L 2138 2586 
C 2118 2669 2112 2758 2112 2854 
L 2112 3245 
C 2157 3219 2182 3206 2240 3174 
C 2547 2995 2618 2944 2899 2701 
L 3117 3014 
C 2662 3302 2662 3302 2112 3565 
L 2112 3686 
L 2950 3686 
C 3168 3686 3270 3680 3386 3661 
L 3386 4045 
C 3277 4026 3181 4019 2957 4019 
L 2112 4019 
z
M 589 5030 
C 800 4742 915 4538 1050 4192 
L 1395 4333 
C 1248 4666 1133 4864 915 5178 
L 589 5030 
z
M 2931 5197 
C 2790 4832 2650 4589 2400 4282 
L 2720 4122 
C 2944 4410 3059 4608 3277 5037 
L 2931 5197 
z
" transform="scale(0.015625)"/>
     <path id="03-read-16-HiraginoSansGB-W3-91cf" d="M 3360 1075 
L 4666 1075 
C 4883 1075 5050 1069 5280 1050 
C 5267 1152 5261 1331 5261 1562 
L 5261 2266 
C 5261 2445 5267 2566 5280 2688 
C 5094 2669 4928 2662 4685 2662 
L 1754 2662 
C 1466 2662 1274 2669 1146 2688 
C 1158 2541 1165 2419 1165 2246 
L 1165 1568 
C 1165 1293 1165 1222 1146 1050 
C 1331 1069 1498 1075 1760 1075 
L 2989 1075 
L 2989 710 
L 1408 710 
C 1146 710 1005 717 864 742 
L 864 378 
C 1030 403 1165 410 1421 410 
L 2989 410 
L 2989 32 
L 1018 32 
C 723 32 557 38 365 70 
L 365 -314 
C 550 -288 749 -275 1018 -275 
L 5389 -275 
C 5658 -275 5850 -288 6042 -314 
L 6042 70 
C 5850 38 5683 32 5389 32 
L 3360 32 
L 3360 410 
L 5037 410 
C 5286 410 5421 403 5587 378 
L 5587 742 
C 5440 717 5312 710 5050 710 
L 3360 710 
L 3360 1075 
z
M 2989 2010 
L 1530 2010 
L 1530 2374 
L 2989 2374 
L 2989 2010 
z
M 3360 2010 
L 3360 2374 
L 4896 2374 
L 4896 2010 
L 3360 2010 
z
M 2989 1363 
L 1530 1363 
L 1530 1734 
L 2989 1734 
L 2989 1363 
z
M 3360 1363 
L 3360 1734 
L 4896 1734 
L 4896 1363 
L 3360 1363 
z
M 4742 3629 
C 5114 3629 5222 3622 5382 3610 
C 5363 3776 5357 3910 5357 4096 
L 5357 4678 
C 5357 4877 5363 4998 5382 5139 
C 5203 5120 5043 5114 4762 5114 
L 1664 5114 
C 1389 5114 1216 5120 1030 5139 
C 1050 4992 1056 4870 1056 4678 
L 1056 4096 
C 1056 3872 1050 3750 1030 3610 
C 1184 3622 1306 3629 1664 3629 
L 4742 3629 
z
M 1421 4826 
L 4992 4826 
L 4992 4512 
L 1421 4512 
L 1421 4826 
z
M 1421 4237 
L 4986 4237 
L 4986 3910 
L 1421 3910 
L 1421 4237 
z
M 365 2963 
C 531 2989 685 2995 998 2995 
L 5414 2995 
C 5734 2995 5875 2989 6042 2963 
L 6042 3341 
C 5869 3315 5677 3309 5408 3309 
L 998 3309 
C 730 3309 538 3315 365 3341 
L 365 2963 
z
" transform="scale(0.015625)"/>
     <path id="03-read-16-HiraginoSansGB-W3-589e" d="M 1414 3578 
L 1414 4653 
C 1414 4915 1427 5069 1453 5254 
L 979 5254 
C 1005 5062 1018 4902 1018 4646 
L 1018 3578 
L 838 3578 
C 589 3578 461 3584 301 3616 
L 301 3174 
C 454 3206 582 3213 838 3213 
L 1018 3213 
L 1018 1101 
C 602 954 397 896 250 870 
L 346 429 
C 397 454 416 461 480 486 
C 691 576 691 576 736 589 
C 1037 704 1037 704 1146 749 
C 1574 922 1837 1037 2144 1197 
L 2157 1581 
C 1734 1382 1632 1331 1414 1248 
L 1414 3213 
L 1555 3213 
C 1786 3213 1901 3206 2074 3187 
L 2074 3616 
C 1946 3584 1818 3578 1562 3578 
L 1414 3578 
z
M 3597 3021 
C 3424 3462 3328 3648 3117 3981 
L 2803 3859 
C 3021 3533 3091 3392 3277 2867 
L 3597 3021 
z
M 4710 2880 
C 4934 3200 5056 3430 5229 3853 
L 4909 4000 
C 4762 3584 4646 3354 4429 3027 
L 4710 2880 
z
M 3674 4512 
C 3539 4826 3418 5024 3181 5363 
L 2810 5216 
C 3040 4947 3136 4794 3264 4512 
L 2810 4512 
C 2534 4512 2374 4525 2259 4550 
C 2285 4358 2291 4134 2291 3834 
L 2291 2970 
C 2291 2714 2285 2515 2259 2323 
C 2419 2349 2586 2355 2797 2355 
L 5254 2355 
C 5453 2355 5594 2349 5798 2330 
C 5786 2496 5779 2688 5779 2995 
L 5779 3827 
C 5779 4102 5786 4314 5798 4544 
C 5702 4518 5542 4512 5229 4512 
L 4742 4512 
C 4890 4698 4979 4838 5190 5229 
L 4806 5382 
C 4640 4992 4525 4781 4326 4512 
L 3674 4512 
z
M 2637 2682 
L 2637 4186 
L 3821 4186 
L 3821 2682 
L 2637 2682 
z
M 4160 2682 
L 4160 4186 
L 5427 4186 
L 5427 2682 
L 4160 2682 
z
M 2547 122 
C 2547 -109 2541 -282 2515 -493 
L 2912 -493 
L 2912 -218 
L 5082 -218 
L 5082 -512 
L 5485 -512 
C 5459 -320 5446 -134 5446 128 
L 5446 1331 
C 5446 1626 5453 1805 5472 1958 
C 5344 1939 5184 1933 4896 1933 
L 3098 1933 
C 2861 1933 2656 1939 2528 1952 
C 2541 1760 2547 1555 2547 1293 
L 2547 122 
z
M 2912 1594 
L 5082 1594 
L 5082 1075 
L 2912 1075 
L 2912 1594 
z
M 2912 749 
L 5082 749 
L 5082 122 
L 2912 122 
L 2912 749 
z
" transform="scale(0.015625)"/>
     <path id="03-read-16-HiraginoSansGB-W3-52a0" d="M 2765 3731 
C 2758 3046 2746 2688 2714 2080 
C 2675 1408 2554 570 2470 358 
C 2426 243 2342 205 2170 205 
C 1978 205 1722 243 1421 307 
C 1466 122 1478 70 1485 -115 
C 1786 -160 2061 -186 2259 -186 
C 2714 -186 2842 -38 2957 602 
C 3072 1267 3149 2272 3168 3328 
C 3174 3955 3174 3955 3200 4141 
C 3008 4122 2835 4115 2534 4115 
L 1843 4115 
C 1869 5158 1869 5158 1901 5325 
L 1414 5325 
C 1453 5146 1459 5024 1459 4634 
C 1459 4442 1459 4320 1446 4115 
L 1075 4115 
C 755 4115 525 4128 371 4160 
L 371 3686 
C 544 3718 762 3731 1043 3731 
L 1427 3731 
C 1357 2573 1190 1766 864 1069 
C 666 646 506 416 147 6 
C 288 -109 371 -198 474 -352 
C 800 58 934 262 1120 627 
C 1331 1037 1472 1421 1587 1914 
C 1722 2470 1779 2925 1830 3731 
L 2765 3731 
z
M 4154 262 
L 5478 262 
L 5478 -198 
L 5920 -198 
C 5894 -6 5882 186 5882 429 
L 5882 3872 
C 5882 4179 5888 4346 5914 4544 
C 5709 4518 5606 4512 5357 4512 
L 4282 4512 
C 4038 4512 3904 4518 3718 4544 
C 3744 4346 3750 4166 3750 3872 
L 3750 480 
C 3750 218 3738 -70 3712 -250 
L 4154 -250 
L 4154 262 
z
M 4154 4141 
L 5478 4141 
L 5478 634 
L 4154 634 
L 4154 4141 
z
" transform="scale(0.015625)"/>
    </defs>
    <use xlink:href="#03-read-16-HiraginoSansGB-W3-44"/>
    <use xlink:href="#03-read-16-HiraginoSansGB-W3-20" x="75.799988"/>
    <use xlink:href="#03-read-16-HiraginoSansGB-W3-b7" x="109.099976"/>
    <use xlink:href="#03-read-16-HiraginoSansGB-W3-20" x="209.09996"/>
    <use xlink:href="#03-read-16-HiraginoSansGB-W3-31" x="242.399948"/>
    <use xlink:href="#03-read-16-HiraginoSansGB-W3-36" x="308.099945"/>
    <use xlink:href="#03-read-16-HiraginoSansGB-W3-20" x="373.799942"/>
    <use xlink:href="#03-read-16-HiraginoSansGB-W3-8bf7" x="407.09993"/>
    <use xlink:href="#03-read-16-HiraginoSansGB-W3-6c42" x="507.099915"/>
    <use xlink:href="#03-read-16-HiraginoSansGB-W3-ff1a" x="607.099899"/>
    <use xlink:href="#03-read-16-HiraginoSansGB-W3-4e24" x="707.099884"/>
    <use xlink:href="#03-read-16-HiraginoSansGB-W3-79cd" x="807.099869"/>
    <use xlink:href="#03-read-16-HiraginoSansGB-W3-6c60" x="907.099854"/>
    <use xlink:href="#03-read-16-HiraginoSansGB-W3-6a21" x="1007.099838"/>
    <use xlink:href="#03-read-16-HiraginoSansGB-W3-5f0f" x="1107.099823"/>
    <use xlink:href="#03-read-16-HiraginoSansGB-W3-7684" x="1207.099808"/>
    <use xlink:href="#03-read-16-HiraginoSansGB-W3-8bfb" x="1307.099792"/>
    <use xlink:href="#03-read-16-HiraginoSansGB-W3-53d6" x="1407.099777"/>
    <use xlink:href="#03-read-16-HiraginoSansGB-W3-8017" x="1507.099762"/>
    <use xlink:href="#03-read-16-HiraginoSansGB-W3-65f6" x="1607.099747"/>
    <use xlink:href="#03-read-16-HiraginoSansGB-W3-5747" x="1707.099731"/>
    <use xlink:href="#03-read-16-HiraginoSansGB-W3-968f" x="1807.099716"/>
    <use xlink:href="#03-read-16-HiraginoSansGB-W3-20" x="1907.099701"/>
    <use xlink:href="#03-read-16-HiraginoSansGB-W3-54" x="1940.399689"/>
    <use xlink:href="#03-read-16-HiraginoSansGB-W3-50" x="2005.299683"/>
    <use xlink:href="#03-read-16-HiraginoSansGB-W3-20" x="2071.49968"/>
    <use xlink:href="#03-read-16-HiraginoSansGB-W3-6570" x="2104.799667"/>
    <use xlink:href="#03-read-16-HiraginoSansGB-W3-91cf" x="2204.799652"/>
    <use xlink:href="#03-read-16-HiraginoSansGB-W3-589e" x="2304.799637"/>
    <use xlink:href="#03-read-16-HiraginoSansGB-W3-52a0" x="2404.799622"/>
   </g>
  </g>
 </g>
 <defs>
  <clipPath id="03-read-16-p1bff6ad17d">
   <rect x="120.68" y="48.149543" width="387.411385" height="548.279194"/>
  </clipPath>
  <clipPath id="03-read-16-p8379172ea5">
   <rect x="641.26376" y="48.149543" width="387.411385" height="548.279194"/>
  </clipPath>
 </defs>
 <defs>
  <pattern id="03-read-16-hc447d1c890" patternUnits="userSpaceOnUse" x="0" y="0" width="72" height="72">
   <rect x="0" y="0" width="73" height="73" fill="#c8daed"/>
   <path d="M -36 36 
L 36 -36 
M -32 40 
L 40 -32 
M -28 44 
L 44 -28 
M -24 48 
L 48 -24 
M -20 52 
L 52 -20 
M -16 56 
L 56 -16 
M -12 60 
L 60 -12 
M -8 64 
L 64 -8 
M -4 68 
L 68 -4 
M 0 72 
L 72 0 
M 4 76 
L 76 4 
M 8 80 
L 80 8 
M 12 84 
L 84 12 
M 16 88 
L 88 16 
M 20 92 
L 92 20 
M 24 96 
L 96 24 
M 28 100 
L 100 28 
M 32 104 
L 104 32 
M 36 108 
L 108 36 
" style="fill: #467bb5; stroke: #467bb5; stroke-width: 1.0; stroke-linecap: butt; stroke-linejoin: miter"/>
  </pattern>
  <pattern id="03-read-16-hdea91fd583" patternUnits="userSpaceOnUse" x="0" y="0" width="72" height="72">
   <rect x="0" y="0" width="73" height="73" fill="#b5dcd4"/>
   <path d="M -36 36 
L 36 -36 
M -32 40 
L 40 -32 
M -28 44 
L 44 -28 
M -24 48 
L 48 -24 
M -20 52 
L 52 -20 
M -16 56 
L 56 -16 
M -12 60 
L 60 -12 
M -8 64 
L 64 -8 
M -4 68 
L 68 -4 
M 0 72 
L 72 0 
M 4 76 
L 76 4 
M 8 80 
L 80 8 
M 12 84 
L 84 12 
M 16 88 
L 88 16 
M 20 92 
L 92 20 
M 24 96 
L 96 24 
M 28 100 
L 100 28 
M 32 104 
L 104 32 
M 36 108 
L 108 36 
" style="fill: #008477; stroke: #008477; stroke-width: 1.0; stroke-linecap: butt; stroke-linejoin: miter"/>
  </pattern>
 </defs>
</svg>
</figure>
```
**16 请求：共享 TP4 较 TP2 变化为 +42.4%～+91.6%。**

**下面用独立池检验：共享模式本身是否明显变慢。**

| 比较范围 | TP2 共享较独立 | TP4 共享较独立 |
| --- | ---: | ---: |
| 1 请求；P/D 合计 12 个对照 | -0.7%～+1.0% | -1.6%～+0.5% |
| 16 请求；P/D 合计 12 个对照 | -1.3%～+3.3% | -1.2%～+1.3% |

TP4 中，共享与独立的读取耗时仍很接近。
这批数据没有显示明显的共享池专属读延迟惩罚。
reader 数量和合计工作量增加，与容量变化同时发生。
仅凭这些 P50，不能定位具体的带宽或调度瓶颈。

**读取完整对照表。** 同名用例、同一消费者逐项比较。

| 消费者 / 用例 | TP2 独立 ms | TP2 共享 ms | TP4 独立 ms | TP4 共享 ms | 共享 TP4 较 TP2 |
| --- | ---: | ---: | ---: | ---: | ---: |
| P / `read-p-contiguous-1` | 0.3532 | 0.3542 | 0.3535 | 0.3542 | +0.0% |
| D / `read-p-contiguous-1` | 0.8383 | 0.8416 | 0.8377 | 0.8396 | -0.2% |
| P / `read-p-contiguous-16` | 0.7764 | 0.7800 | 1.3687 | 1.3691 | +75.5% |
| D / `read-p-contiguous-16` | 0.8722 | 0.8736 | 1.3943 | 1.4005 | +60.3% |
| P / `read-p-scattered-1` | 0.3550 | 0.3586 | 0.3546 | 0.3539 | -1.3% |
| D / `read-p-scattered-1` | 0.8416 | 0.8354 | 0.8366 | 0.8365 | +0.1% |
| P / `read-p-scattered-16` | 0.7186 | 0.7247 | 1.3832 | 1.3885 | +91.6% |
| D / `read-p-scattered-16` | 0.8794 | 0.8701 | 1.3695 | 1.3657 | +57.0% |
| P / `read-d-contiguous-1` | 0.8374 | 0.8335 | 0.8396 | 0.8394 | +0.7% |
| D / `read-d-contiguous-1` | 0.3511 | 0.3519 | 0.3592 | 0.3591 | +2.0% |
| P / `read-d-contiguous-16` | 0.8694 | 0.8813 | 1.3999 | 1.4184 | +60.9% |
| D / `read-d-contiguous-16` | 0.7833 | 0.7734 | 1.3670 | 1.3683 | +76.9% |
| P / `read-d-scattered-1` | 0.8378 | 0.8429 | 0.8416 | 0.8374 | -0.7% |
| D / `read-d-scattered-1` | 0.3549 | 0.3567 | 0.3578 | 0.3523 | -1.2% |
| P / `read-d-scattered-16` | 0.8701 | 0.8712 | 1.3640 | 1.3789 | +58.3% |
| D / `read-d-scattered-16` | 0.7138 | 0.7179 | 1.3627 | 1.3632 | +89.9% |
| P / `read-mixed-contiguous-1` | 0.5965 | 0.6007 | 0.5956 | 0.5945 | -1.0% |
| D / `read-mixed-contiguous-1` | 0.5968 | 0.5935 | 0.5937 | 0.5966 | +0.5% |
| P / `read-mixed-contiguous-16` | 0.8005 | 0.8270 | 1.1799 | 1.1920 | +44.1% |
| D / `read-mixed-contiguous-16` | 0.8349 | 0.8263 | 1.1760 | 1.1769 | +42.4% |
| P / `read-mixed-scattered-1` | 0.5951 | 0.5922 | 0.5965 | 0.5934 | +0.2% |
| D / `read-mixed-scattered-1` | 0.5945 | 0.5948 | 0.5938 | 0.5927 | -0.4% |
| P / `read-mixed-scattered-16` | 0.8108 | 0.8139 | 1.3819 | 1.3989 | +71.9% |
| D / `read-mixed-scattered-16` | 0.8132 | 0.8188 | 1.3352 | 1.3190 | +61.1% |

**补充：只有一个 TP 发起读写的已有用例。**
这些 single 用例只启用指定成员，其他成员不执行该项读写。
下表使用独立池结果，即指定 TP 访问其独占的 mempool。
它仍处于 TP2 / TP4 测试组中，不是独立部署的 TP1 实验。

单 writer 用例只写 **17 行**。
单 reader 用例读 **1 请求、2048 行、混合来源、离散索引**。
不能把它们冒充 2049 行大块单 writer 或 16 请求单 reader 结果。
计时仍是组包络，不是指定成员的纯设备耗时。

| 测试组 / 指定成员 | P 单 writer ms | D 单 writer ms | P 单 reader ms | D 单 reader ms |
| --- | ---: | ---: | ---: | ---: |
| TP2 / member-0 | 0.0339 | 0.0379 | 0.5959 | 0.5913 |
| TP2 / member-1 | 0.0328 | 0.0342 | 0.5926 | 0.5918 |
| TP4 / member-0 | 0.0407 | 0.0397 | 0.5947 | 0.5905 |
| TP4 / member-1 | 0.0400 | 0.0391 | 0.5981 | 0.5913 |
| TP4 / member-2 | 0.0387 | 0.0354 | 0.5949 | 0.5980 |
| TP4 / member-3 | 0.0372 | 0.0385 | 0.5914 | 0.5892 |

## E 结果边界与当前建议
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

**TP2：每侧 43 项。**


| 用例 | P 独立 ms | P 共享 ms | P 比值 | D 独立 ms | D 共享 ms | D 比值 |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| `logical-1-token` | 0.0339 | 0.0348 | 1.026 | 0.0333 | 0.0323 | 0.969 |
| `logical-1-feature` | 0.0342 | 0.0330 | 0.965 | 0.0342 | 0.0322 | 0.942 |
| `logical-1-layer` | 0.0398 | 0.0325 | 0.815 | 0.0347 | 0.0349 | 1.005 |
| `logical-17-token` | 0.0345 | 0.0373 | 1.083 | 0.0343 | 0.0326 | 0.952 |
| `logical-17-feature` | 0.0331 | 0.0339 | 1.024 | 0.0334 | 0.0331 | 0.988 |
| `logical-17-layer` | 0.0354 | 0.0337 | 0.951 | 0.0335 | 0.0326 | 0.972 |
| `logical-2049-token` | 0.0784 | 0.0459 | 0.585 | 0.0783 | 0.0454 | 0.580 |
| `logical-2049-feature` | 0.0785 | 0.0493 | 0.628 | 0.0784 | 0.0489 | 0.624 |
| `logical-2049-layer` | 0.0787 | 0.0486 | 0.618 | 0.0784 | 0.0488 | 0.622 |
| `equal_bytes-1-token` | 0.0346 | 0.0331 | 0.955 | 0.0360 | 0.0349 | 0.970 |
| `equal_bytes-17-token` | 0.0332 | 0.0340 | 1.025 | 0.0359 | 0.0401 | 1.118 |
| `equal_bytes-2049-token` | 0.0782 | 0.0785 | 1.005 | 0.0781 | 0.0786 | 1.006 |
| `logical-1-layer1` | 0.0330 | 0.0343 | 1.039 | 0.0354 | 0.0328 | 0.929 |
| `logical-17-layer1` | 0.0329 | 0.0340 | 1.031 | 0.0345 | 0.0337 | 0.975 |
| `logical-2049-layer1` | 0.0787 | 0.0490 | 0.623 | 0.0780 | 0.0484 | 0.621 |
| `read-p-contiguous-1` | 0.3532 | 0.3542 | 1.003 | 0.8383 | 0.8416 | 1.004 |
| `read-p-contiguous-16` | 0.7764 | 0.7800 | 1.005 | 0.8722 | 0.8736 | 1.002 |
| `read-p-scattered-1` | 0.3550 | 0.3586 | 1.010 | 0.8416 | 0.8354 | 0.993 |
| `read-p-scattered-16` | 0.7186 | 0.7247 | 1.009 | 0.8794 | 0.8701 | 0.989 |
| `read-d-contiguous-1` | 0.8374 | 0.8335 | 0.995 | 0.3511 | 0.3519 | 1.002 |
| `read-d-contiguous-16` | 0.8694 | 0.8813 | 1.014 | 0.7833 | 0.7734 | 0.987 |
| `read-d-scattered-1` | 0.8378 | 0.8429 | 1.006 | 0.3549 | 0.3567 | 1.005 |
| `read-d-scattered-16` | 0.8701 | 0.8712 | 1.001 | 0.7138 | 0.7179 | 1.006 |
| `read-mixed-contiguous-1` | 0.5965 | 0.6007 | 1.007 | 0.5968 | 0.5935 | 0.994 |
| `read-mixed-contiguous-16` | 0.8005 | 0.8270 | 1.033 | 0.8349 | 0.8263 | 0.990 |
| `read-mixed-scattered-1` | 0.5951 | 0.5922 | 0.995 | 0.5945 | 0.5948 | 1.000 |
| `read-mixed-scattered-16` | 0.8108 | 0.8139 | 1.004 | 0.8132 | 0.8188 | 1.007 |
| `logical-single-0` | 0.0339 | 0.0335 | 0.989 | 0.0379 | 0.0343 | 0.904 |
| `logical-single-1` | 0.0328 | 0.0332 | 1.012 | 0.0342 | 0.0334 | 0.976 |
| `read-single-0` | 0.5959 | 0.5917 | 0.993 | 0.5913 | 0.5924 | 1.002 |
| `read-single-1` | 0.5926 | 0.6004 | 1.013 | 0.5918 | 0.5910 | 0.999 |
| `write_then_read-1` | 0.7022 | 0.6987 | 0.995 | 0.6977 | 0.6995 | 1.003 |
| `write_then_read-16` | 0.9368 | 0.9345 | 0.997 | 0.9361 | 0.9293 | 0.993 |
| `write_with_history-1` | 0.6268 | 0.6276 | 1.001 | 0.6276 | 0.6258 | 0.997 |
| `write_with_history-16` | 0.7677 | 0.7590 | 0.989 | 0.8120 | 0.8420 | 1.037 |
| `current_serial-1` | 0.6264 | 0.6276 | 1.002 | 0.6286 | 0.6272 | 0.998 |
| `current_serial-16` | 0.7905 | 0.8026 | 1.015 | 0.7912 | 0.7435 | 0.940 |
| `current_parallel-1` | 0.7795 | 0.7879 | 1.011 | 1.0209 | 0.9876 | 0.967 |
| `current_parallel-16` | 0.9474 | 1.0898 | 1.150 | 1.1188 | 1.1154 | 0.997 |
| `materialize-0` | 1.4230 | 1.4228 | 1.000 | 1.6885 | 1.7078 | 1.011 |
| `materialize-50` | 1.3424 | 1.3541 | 1.009 | 1.5415 | 1.5508 | 1.006 |
| `materialize-100` | 0.8453 | 0.8578 | 1.015 | 0.8562 | 0.8465 | 0.989 |
| `zero-valid` | 0.0346 | 0.0336 | 0.971 | 0.0340 | 0.0330 | 0.970 |


**TP4：每侧 53 项。**


| 用例 | P 独立 ms | P 共享 ms | P 比值 | D 独立 ms | D 共享 ms | D 比值 |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| `logical-1-token` | 0.0368 | 0.0456 | 1.240 | 0.0401 | 0.0432 | 1.077 |
| `logical-1-feature` | 0.0440 | 0.0410 | 0.933 | 0.0455 | 0.0429 | 0.943 |
| `logical-1-layer` | 0.0379 | 0.0451 | 1.190 | 0.0417 | 0.0408 | 0.979 |
| `logical-17-token` | 0.0396 | 0.0412 | 1.040 | 0.0397 | 0.0389 | 0.982 |
| `logical-17-feature` | 0.0394 | 0.0406 | 1.031 | 0.0490 | 0.0420 | 0.856 |
| `logical-17-layer` | 0.0384 | 0.0386 | 1.005 | 0.0338 | 0.0416 | 1.229 |
| `logical-2049-token` | 0.1406 | 0.0447 | 0.318 | 0.1400 | 0.0442 | 0.316 |
| `logical-2049-feature` | 0.1405 | 0.0539 | 0.383 | 0.1408 | 0.0545 | 0.387 |
| `logical-2049-layer` | 0.1409 | 0.0485 | 0.344 | 0.1404 | 0.0486 | 0.346 |
| `equal_bytes-1-token` | 0.0374 | 0.0395 | 1.056 | 0.0438 | 0.0441 | 1.007 |
| `equal_bytes-17-token` | 0.0383 | 0.0372 | 0.971 | 0.0416 | 0.0422 | 1.013 |
| `equal_bytes-2049-token` | 0.1401 | 0.1391 | 0.993 | 0.1405 | 0.1410 | 1.003 |
| `logical-1-layer1` | 0.0385 | 0.0422 | 1.097 | 0.0412 | 0.0389 | 0.943 |
| `logical-17-layer1` | 0.0354 | 0.0402 | 1.135 | 0.0415 | 0.0442 | 1.064 |
| `logical-2049-layer1` | 0.1402 | 0.0492 | 0.351 | 0.1406 | 0.0486 | 0.346 |
| `logical-1-layer2` | 0.0373 | 0.0377 | 1.011 | 0.0361 | 0.0381 | 1.053 |
| `logical-17-layer2` | 0.0408 | 0.0386 | 0.947 | 0.0354 | 0.0401 | 1.134 |
| `logical-2049-layer2` | 0.1409 | 0.0510 | 0.362 | 0.1410 | 0.0501 | 0.356 |
| `logical-1-layer3` | 0.0404 | 0.0382 | 0.947 | 0.0436 | 0.0441 | 1.013 |
| `logical-17-layer3` | 0.0371 | 0.0441 | 1.188 | 0.0408 | 0.0370 | 0.908 |
| `logical-2049-layer3` | 0.1416 | 0.0482 | 0.341 | 0.1399 | 0.0495 | 0.354 |
| `read-p-contiguous-1` | 0.3535 | 0.3542 | 1.002 | 0.8377 | 0.8396 | 1.002 |
| `read-p-contiguous-16` | 1.3687 | 1.3691 | 1.000 | 1.3943 | 1.4005 | 1.004 |
| `read-p-scattered-1` | 0.3546 | 0.3539 | 0.998 | 0.8366 | 0.8365 | 1.000 |
| `read-p-scattered-16` | 1.3832 | 1.3885 | 1.004 | 1.3695 | 1.3657 | 0.997 |
| `read-d-contiguous-1` | 0.8396 | 0.8394 | 1.000 | 0.3592 | 0.3591 | 1.000 |
| `read-d-contiguous-16` | 1.3999 | 1.4184 | 1.013 | 1.3670 | 1.3683 | 1.001 |
| `read-d-scattered-1` | 0.8416 | 0.8374 | 0.995 | 0.3578 | 0.3523 | 0.984 |
| `read-d-scattered-16` | 1.3640 | 1.3789 | 1.011 | 1.3627 | 1.3632 | 1.000 |
| `read-mixed-contiguous-1` | 0.5956 | 0.5945 | 0.998 | 0.5937 | 0.5966 | 1.005 |
| `read-mixed-contiguous-16` | 1.1799 | 1.1920 | 1.010 | 1.1760 | 1.1769 | 1.001 |
| `read-mixed-scattered-1` | 0.5965 | 0.5934 | 0.995 | 0.5938 | 0.5927 | 0.998 |
| `read-mixed-scattered-16` | 1.3819 | 1.3989 | 1.012 | 1.3352 | 1.3190 | 0.988 |
| `logical-single-0` | 0.0407 | 0.0403 | 0.992 | 0.0397 | 0.0452 | 1.137 |
| `logical-single-1` | 0.0400 | 0.0432 | 1.080 | 0.0391 | 0.0339 | 0.866 |
| `logical-single-2` | 0.0387 | 0.0362 | 0.936 | 0.0354 | 0.0374 | 1.057 |
| `logical-single-3` | 0.0372 | 0.0355 | 0.952 | 0.0385 | 0.0376 | 0.978 |
| `read-single-0` | 0.5947 | 0.5932 | 0.998 | 0.5905 | 0.5887 | 0.997 |
| `read-single-1` | 0.5981 | 0.5931 | 0.992 | 0.5913 | 0.5947 | 1.006 |
| `read-single-2` | 0.5949 | 0.5956 | 1.001 | 0.5980 | 0.5885 | 0.984 |
| `read-single-3` | 0.5914 | 0.5881 | 0.994 | 0.5892 | 0.5830 | 0.989 |
| `write_then_read-1` | 0.6770 | 0.6719 | 0.992 | 0.6730 | 0.6714 | 0.998 |
| `write_then_read-16` | 1.5940 | 1.6149 | 1.013 | 1.5865 | 1.5936 | 1.004 |
| `write_with_history-1` | 0.6269 | 0.6252 | 0.997 | 0.6280 | 0.6244 | 0.994 |
| `write_with_history-16` | 1.1080 | 1.4264 | 1.287 | 1.2796 | 1.3316 | 1.041 |
| `current_serial-1` | 0.6276 | 0.6261 | 0.998 | 0.6278 | 0.6250 | 0.996 |
| `current_serial-16` | 1.0414 | 1.4199 | 1.363 | 1.3249 | 1.3313 | 1.005 |
| `current_parallel-1` | 0.7857 | 0.7819 | 0.995 | 1.0174 | 1.0099 | 0.993 |
| `current_parallel-16` | 1.1173 | 1.1042 | 0.988 | 1.2475 | 1.2778 | 1.024 |
| `materialize-0` | 1.6756 | 1.6494 | 0.984 | 1.8001 | 1.7690 | 0.983 |
| `materialize-50` | 1.3812 | 1.3784 | 0.998 | 1.5634 | 1.5645 | 1.001 |
| `materialize-100` | 0.8648 | 0.8574 | 0.991 | 0.8590 | 0.8625 | 1.004 |
| `zero-valid` | 0.0407 | 0.0385 | 0.946 | 0.0406 | 0.0336 | 0.828 |

