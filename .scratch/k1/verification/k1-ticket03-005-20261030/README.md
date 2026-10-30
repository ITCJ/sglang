# K1-ticket03 NPU005：启动冲突与源快照验证

本次基点`6f50c4d54bef8774516da0a622aa094e0db8a645`，用户确认31上正在运行该版本。
用户反馈005仍返回137，并明确要求“整个prefix验证链不额外读BM”，
接受不再直接证明alive/prefix DRAM内容，生产恢复、fill/admit访问保留。
此要求替代票03原先“真实私有副本作oracle”的验收条款。

## 实机005失败依据

日志根目录：`/home/cryang/log/mempool-min/k1-ticket03-005/`。
用户回传`prefill/target/launcher.log`片段：

```text
15:43:00.727129 [SMEM smem_tcp_config_store.cpp:148 Startup] Failed to start config store server ret:-2009
15:43:00.727155 [SMEM smem_bm_entry_manager.cpp:63 Initialize] prepare store failed: -2009
15:43:00 TP3 [MEMPOOL_INIT] FAIL stage=bm.initialize pid=130631 ... store=tcp://10.120.72.31:19003 ... ret=-1
RuntimeError: BM initialize failed for tcp://10.120.72.31:19003: -1
15:43:00 Received sigquit from a child process.
15:43:00 kill_process_tree called: parent_pid=130395, include_parent=True, pid=130395
run_service.sh: line 149: 130395 Killed "${common[@]}"
```

其他rank开始create2，bm_flags=128–135，说明004的host映射标志已撤销。
005未进入prefix校验；137来自后续默认进程清理，不是BM内容比较的返回码。

本地两份SDK源码均将`-2009`定义为`SM_RESOURCE_IN_USE`，
`TcpConfigStoreServer::Startup`将`ACC_LINK_ADDRESS_IN_USE`转换成该值。
依据为`memfabric_hybrid/src/smem/csrc/common/smem_types.h`及
`config_store/tcp_store/smem_tcp_config_store_server.cpp`；对应本地SDK版本
`004d9317`和`29fbd1ae`，不是本次NPU重新采集的SDK身份。
因此当前证据指向19003启动时地址占用，不能据此推断具体占用进程。

容器没有ss；用户用psutil只读查询本地或远端端口19003，返回`matches: 0`。
当前没有可确认的连接或占用者，不发出kill命令，不更改默认退出或BM初始化。
验证改造不作为该启动冲突已修复的证据。

## 新验证链及覆盖边界

1. 计算源产生主KV/Index K时，现有capture_source保存独立CPU快照和覆盖位图。
2. admit事件完成后，将对应源快照按prefix页映射保留；不读取admit目标DRAM。
3. 每层all_gather完成后，把汇总HBM复制到CPU，按槽位与源快照逐token比较。
   补齐槽位跳过；之后执行原有scatter。
4. 全部恢复event完成后，按allocator索引读原生HBM并与同一源快照比较。
   没有私有副本读回、额外oracle索引或第三块HBM数据缓冲。
5. fill/admit和source release仍依赖原有设备完成门控。
   alive/suffix/fill/admit/retained只保留源覆盖、页绑定与生命周期证据。

`oracle=computed_source`、`oracle_read_bytes=0`；不再输出private_replica_verified。
prefix报告统一明确`bm_readback=false`，只有load_back的content_source为native_hbm，
其他阶段为computed_source。checker拒绝不匹配的来源类型和额外oracle读量。
不再声称逐页验证了alive、prefix及fill目标的DRAM内容。
被实际生产读取并恢复到HBM的数据仍接受精确核对；未读取副本的潜在损坏不在覆盖内。
V2的HBM→CPU拷贝与CPU比较仍影响计时，当前表不是纯生产性能。
当前prefix/reuse/session/full报告均使用这个边界；历史记录须用对应版本解释。

## 验证

复用真实PrefixStore与NPU复制边界替身：禁止任何诊断BM→HBM复制，
旧实现的alive校验立即失败；同时新oracle字段在旧实现不存在。见red.log。
这是用户要求的行为回归，不是对005原生SDK地址占用的CPU复现。

16项定向CPU通过（3.662秒），复用原有测试并覆盖：

- 整个源保存、admit、释放、fill与后缀验证没有额外BM读回。
- 恢复实际复制字节等于生产read_bytes，oracle_read_bytes为0，两块缓冲复用。
- 主KV/Index K的all_gather结果或原生HBM被篡改均拒绝。
- 源快照独立于原始tensor、页绑定/非连续恢复正确，报告不夸大DRAM覆盖。
- 旧私有副本oracle标记、错误来源标记及额外oracle流量均被报告检查拒绝。

13文件静态检查、8模块strict mypy及Python 3.9语法检查通过，见static.log。
完整CPU单轮通过494项，46.501秒，见full-cpu.log；Standards/Spec均0项发现。
未运行NPU，005失败，006待验，ticket仍open。

## Standards

0项硬标准违规，0项需处理异味。三个功能文件均在Ascend/NPU目录，
删除诊断BM读回并复用源快照与完成门控；默认退出路径未改。
测试沿用已有边界，报告与文档明确调整后的覆盖范围，005失败历史保留。

## Spec

0项发现。按用户最新要求，剩余BM拷贝仅为生产load_back、fill、admit。
源捕获写入独立CPU存储，admit后按prefix页clone保留；gather与native均核对实际HBM。
两块恢复缓冲、三步发射和event完成后回执保留，来源标记与零额外读取有严格检查。
用户已接受的DRAM内容覆盖缩减明确记录；没有宣称启动冲突已修复或NPU验收通过。

两轴均只读评审，未增加测试轮次；最严重问题：无。

## 本次改动阅读顺序

Ascend/NPU功能源码：

1. `prefix.py`：删除诊断BM读取；源快照、gather和native内容比较；诚实记录验证来源。
2. `runtime.py`：计算结束调用verify_source，验证源捕获覆盖。
3. `mempool_service.py`：fill完成后核对prefix/suffix组合的源覆盖。

通用主线功能源码：无。
脚本：verify_mempool_prefix/reuse/split_read/full/session，更新证据来源及覆盖描述。
测试：test_prefix、test_prefix_split_read、test_mempool_prefix_service、
test_mempool_prefix_evidence、test_mempool_split_read_evidence。
文档：K1票03/执行文本、运行与交付说明、本记录、target-alignment D节及生成HTML。
