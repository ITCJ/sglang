# K1-ticket08-001：用户回传NPU自动验收通过

证据来源：用户在本会话粘贴31上的 `prefill/summary.json` 与 `prefill/prefix-sharing-acceptance.json` 全文。此文件为核对摘要，不冒充原始JSON；未远程访问机器。

- 运行：31（10.120.72.31）/32（10.120.72.32），run-id `k1-ticket08-001`。
- 代码：`4441eb9015aa16f4d94f3b3d23c537e99a731d6e`；报告显示tracked diff为空，untracked清单为空，双侧identity检查通过。
- 模型：`/data_lib/data/models/GLM-5.2-w4a8`；配置 `/home/cryang/mempool-min-ticket01-3132.json`。
- 模式：`prefix-sharing-acceptance`，owners admit、按请求轮转起始段、auto解析为by_segment、Index K写者rank0。
- 总状态passed；fetch、target、restart完成。两轮各6请求均passed，ready/free账本检查通过，reader_counts_zero=true。
- 两轮注册均applied：P/D主KV各8份、alive Index K及两类prefix各1份；八段owner为0/2/4/6/8/10/12/14。
- 实际BM贡献匹配注册：P每NUMA12 GiB、合计96 GiB；D每NUMA1 GiB、合计8 GiB。P验收快照预算32 GiB单列。
- A保存32页，起始段0，页序列0/5/10/.../35、1/6/...，main 368050176 bytes，index 22020096 bytes。
- B命中4096，起始段1追加8页：9/14/19/24/29/34/39/4。load_back主KV368050176 bytes；fill主KV2944401408 bytes，恰为8倍；Index K两者均22020096 bytes。
- C命中3968（31页），前7段各512行、末段384行；fill主KV按段为4096×7+3072行，正确保留不均匀分布，不把通信padding计入DRAM读取。
- A/B/C/E命中0/4096/3968/0；其余长生成与EOS为冷请求。两轮报告一致。
- 超时注入为预期fail-stop：SR heartbeat timeout，16 rank原生资源和全部额度保留，无自动free；worker_exit=137，进程清理通过。
- paired_process_cleanup通过；同端口restart完成。

自动验收覆盖为 `K1-08 reduced functional acceptance`。依据已核对的汇总程序，passed要求六例答案、内容/生命周期、注册、容量重放、清理与端口释放等前置报告通过；本轮未单独读取远端原始逐rank日志。

仍保留的边界：physical_placement_verified=false；没有直接读回alive/prefix/fill DRAM；未验收性能；V3采用独立逻辑答案，V8重放真实注册到CPU；不宣称完整原始V1–V10全覆盖。single_rank admit和双Index K写者未在本次默认NPU配置运行。

原始材料在 `/home/cryang/log/mempool-min/k1-ticket08-001/{prefill,decode}/`。
用户回传自动报告不等于明确关闭确认；`user_accepted=false`，08保持open，11未解锁。

## 用户确认

2026-11-01，用户在核对上述报告和未验证边界后回复“可以”，明确接受本次精简验收范围、关闭08并解锁11。
人工结论 `user_accepted=true`；原始自动报告的false保留，不改写原始结果。08已closed，11已解锁但尚未实施。
