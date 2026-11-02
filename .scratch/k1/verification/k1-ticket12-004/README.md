# K1-ticket12-004：双机自动验收通过，待用户确认

证据来源：用户在会话粘贴31的prefill/summary.json、prefill/page-first-acceptance.json及32的decode/summary.json全文。
本文件与[字段摘录](extracted-facts.json)为核对记录，不冒充原始文件；未远程访问NPU，未重新执行模型。

- 31/32均为c2ed808092704689f22d676f20dc254836db8fe5；tracked diff为空、untracked清单为空；两侧identity_check均passed、differences为空。
- 模式page-first-acceptance，模型/data_lib/data/models/GLM-5.2-w4a8，配置/home/cryang/mempool-min-ticket01-3132.json，run-id k1-ticket12-004。
- 两侧总状态passed，page-first-copy、fetch、target、restart均完成；两轮各6请求通过。
- ready事件为15/14，均reader_counts_zero=true；两轮账本、共享检查和注册均passed。
- 五类区域均page_first，B=128；主KV page_bytes=11,501,568，Index K page_bytes=688,128；两轮启动签名均与区域匹配。
- 副本8/8/1/1/1，prefix八段，每段5页；NUMA0..7各一份P/D主KV。P每节点12 GiB、合计96 GiB；D每节点1 GiB、合计8 GiB。P源快照预算32 GiB单列。
- A/B/C/E命中0/4096/3968/0，长生成与EOS为冷请求。A按段交错保存32页，B从段1追加8页。
- B 32页：主KV load_back=368,050,176 B，fill=2,944,401,408 B；Index K两者均22,020,096 B。
- C 31页：主KV load_back=356,548,608 B，fill=2,852,388,864 B；Index K两者均21,331,968 B。
- C主KV/index读取行数均为前7段512、末段384；主KV fill按段为前7段4096、末段3072。两轮一致，padding不计主存读取。
- 上述字节已按规格独立算术核对，且与08已接受口径相同；fill主KV为8份，Index K单份。
- 超时注入通过：SR heartbeat timeout、16 rank资源和全部配额保留，无自动free；worker_exit=137。默认进程清理与同端口restart通过。

外层coverage为K1-12；内层仍显示K1-08，是复用verify_mempool_session汇总器的标签。
执行模式、独立page-first-copy阶段、两轮page_first证据均为本票，不能把旧标签解释为未测page first。
依据已核对的汇总入口，passed要求两轮答案、内容、生命周期、注册、容量重放、端口及进程清理等前置报告通过。
本轮未单独回传逐rank原始日志、独立copy报告或各端口等待JSON；不推断004具体哪些端口等待了多久。

## 保留边界

沿用08批准的精简验收范围：源快照、正常恢复后的HBM和写读记录；无新增直接alive/prefix/fill DRAM读回。
物理NUMA驻留physical_placement_verified=false，性能未验收；V3是独立答案，V8是实际注册CPU重放。
不宣称完整原始V1–V10覆盖，不证明显式BM leave/uninitialize或退出后物理页释放。
002退出析构崩溃的历史保留；本次清理/restart通过不证明底层析构缺陷已修复。002/003原始failed不改写。
本地旧注册测试夹具修正后未第三次运行的CPU记录仍保留，不借NPU通过改写旧测试结果。

日志位置：/home/cryang/log/mempool-min/k1-ticket12-004/{prefill,decode}/。
当前user_accepted=false；报告核对完成，等待用户接受上述范围并确认关闭12，11仍阻塞。

## 用户确认

2026-11-02，用户核对两侧报告及保留范围后明确回复“关闭”。人工确认user_accepted=true，接受代码c2ed808092704689f22d676f20dc254836db8fe5的004精简功能验收。
原始报告及字段摘录中的user_accepted=false保留。12已closed，11已解锁但未实施，09仍等待11。
物理NUMA、性能、直接DRAM比较、显式BM析构及此前析构异常的限制继续保留。
