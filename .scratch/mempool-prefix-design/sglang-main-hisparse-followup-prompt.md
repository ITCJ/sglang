请在此前 SGLang 主线 GPU 的 prefix cache、radix tree、HiCache、Mooncake、router / scheduler 和 layer pipeline 讲解基础上，继续完善已经生成的中文 HTML，补充 HiSparse 的设计与 prefix 命中流程，并与原有章节互相链接。仍以当前 checkout 的真实代码为依据，报告 commit，关键结论附文件、符号和行号；官方文档只作交叉核对。

请重点讲清：

1. 区分三种“命中”：P 侧跨请求 prefix 命中、D 侧跨请求 prefix 复用、HiSparse 中同一请求的 GPU 热 KV 命中。核实 HiSparse 在哪一侧启用、哪一侧要求关闭 radix cache，以及 P 是否仍能使用原生 prefix cache / HiCache。若代码仅限制某种组合，不要扩大成整个 P/D 系统都不支持 prefix。
2. 用请求 B 的 prompt 长 8192、P 命中前 6144 token 的示例，追踪匹配、HBM 恢复、剩余 prefill、P/D 传输、D 的 host 接收和首个 decode。分别标明少算多少、仍需传哪些数据；沿 `decode_prefix_len`、发送范围和请求页映射证明 P 命中是否减少 P→D 的传输。示例长度可按当前模型约束调整。
3. 以受支持的一个 DSA 模型为主线，画清主 KV、Index K 和必要 scale 在 P HBM、D host memory、D HBM 中的位置与生命周期。核实 direct-to-host 的范围、哪些状态仍进入 D HBM、D 是否为每个请求分配独立 host pages，以及结束时哪些数据被回收或保留。
4. 解释每步 sparse decode 的 top-k 选择、设备热数据查找、缺失 KV 换入、attention 和新 KV backup；画出 token position、逻辑索引、host page 与设备 buffer slot 的映射。比较 HiSparse 的 host pool 与 HiCache L2 的用途、索引、共享范围和回收条件。
5. 核实 HiSparse 与 HiCache/L3、D 侧 prefix cache、异步输出备份能否实际组合；不能仅凭存在配置参数就认定支持。特别说明 A 正在 decode 或已经结束时，B 能否直接复用 A 的 host KV，以及生成部分能否供下一轮对话命中。没有接通的路径明确列为限制。
6. 若存在 shared-index prefetch，解释它如何复用 top-k 结果并提前换入后续层所需 KV，再与 HiCache 的 prefix layer pipeline 对照，标出各自的数据范围、触发者和事件依赖。区分已有实现和可能的扩展。

在原 HTML 中补充一张 P/D 与内存位置图、一条上述 B 请求的时间线，以及 HiSparse / HiCache 的对照表。可用切换或逐步播放展示 P 命中与未命中；图中分别标注“省计算”“需要传输”“热数据命中”。从现有章节链接到新内容，修正任何把三种命中或两种 host pool 混同的表述。交付前检查布局、链接和交互，返回更新后的 HTML 路径与当前版本的关键限制。
