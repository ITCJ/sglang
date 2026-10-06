# Ticket03 S6.3：三个 Standards 整改

实现基线：`912012f3d0`；分支：`cryang/dev/mempool`。用户2026-10-06授权按
删除统计 → 共用row推导 → control查询/TP同步顺序实施、分别验证和提交。
范围及约束见[S6计划](ticket-03-s6-plan.md)。本地检查不替代最终NPU验收，03保持open。

提交按三个独立部分组织：STD-03 `25bfbddfa3`，STD-01 `9ba89e46eb`，STD-02为
本交付文档所在的`refactor(ascend): bound control query and TP observation work`提交。
生产改动均在`hardware_backend/npu`或`disaggregation/ascend`内。

## 1. STD-03：删除fetch调试统计

生产runtime不再计算或累计selected KV、cache hit、P miss和D miss。
`_fetch_checks`由六列缩为三列，保留层覆盖、非法读取数量及错误位置；每forward独立
完成快照、事件顺序、binding、可读范围和故障禁止释放仍保留。

`completion_report()` / `mempool decode_completion`仅传递Graph、绑定、写入完成及
drain事实；服务checker同步改用新日志，不再要求hit/miss为正。独立fetch gate仍通过
独立pattern逐元素验证P/D miss、hit、padding及连续异步replay，保留实际copy覆盖。

验证边界是已有runtime完成、service安全释放及正式日志checker公开接口。
先迁移checker fixture，原实现报incomplete attempt；修改实现后27项针对性CPU测试通过
（service_gate、fetch、fetch_layers、pd_service）。严格mypy 33文件、Ruff规则和format、
diff空白检查通过。首次mypy命令漏传测试src目录导致导入失败，补齐标准调用后通过。

## 2. STD-01：共用row推导

将原`mempool/rows.py`移到纯NPU模块`npu/kv_rows.py`，删除普通host
`SparseKVCacheManager.offload_v2()`中重复的decode/ragged/static/tail推导。
两条路径共享request row、全序列position及native有效mask，BM仍追加绑定和P/D容量约束，
host仍构造自己的SHM地址。普通host空batch继续直接返回；helper仅依赖torch和标准库。

独立CPU adapter加载同一份helper，避免导入SGLang服务栈。已有逐值rows/writer测试复用，
新增普通host落地内容测试覆盖ragged、static缺前缀padding、MoE tail和空batch。
资源gate的最小ForwardBatch fixture补齐四个可选字段，值均为None。

43项针对性CPU测试通过（rows、offload、runtime、sparse_resources、materialize），
严格mypy 34源文件通过。普通host roundtrip最初暴露gate fixture缺可选字段，修正后通过。

## 3. STD-02：直接查询、活跃协议视图和TP同步

`MempoolPDControl`新增`get_request(identity)`、`get_room_request(room)`，分别读取
完整identity和当前room owner。活跃请求索引与终态留存分开，`has_pending_requests()`
包含`WAITING_RELEASE_ACK`，`has_retained_room()`通过有界索引保留fresh-room限制。
admission、终态、淘汰和preflight同时维护这些索引；完整`snapshot()`留作诊断。
service所有单请求查询、pending判断及tick日志不再复制全量历史。

`active_snapshot(include)`只收集协议活跃记录和本轮本地清理所需identity。TP载荷中
活跃状态必须一致，可选终态清理记录合并后用于清理及防止重复admission；native队列
不必同时移除Req。同步仅含逻辑attempt、slot/generation、绑定/完成/释放事实及消息
摘要，不传各pair的session、proof、完整peer layout或无关历史记录。原消息仍留本地，
原control的session、generation、retirement、binding proof和所有权检查继续执行。
同tick的不可变观察用于规划、预检及提交；观察/预检结果/提交结果三次collective保留，
所有rank预检通过才提交，全部提交成功后才发送outbox。

新增覆盖包括：256条终态加一个活跃请求时载荷不增长，slot/room复用后的直接查询，
WAITING_RELEASE_ACK、预检隔离新增索引、历史淘汰、部分rank淘汰后的旧/重复DONE和
迟到ACK，以及native队列异步移除终态Req。原pending DONE、取消和跨TP预检失败用例继续运行。

### preflight复制策略的独立评估

保留当前全量record复制。本轮未改为局部复制或写时复制，也不把查询/载荷改进表述为
整个tick成本与历史无关。复制对象除请求外，还包括room/slot owner、generation、
retirement及终态淘汰索引；局部复制必须覆盖这些相互关联的修改和异常回滚边界。

以下为开发Mac、Python3.9.6的微基准；每项31组取中位数，query/active snapshot每组
100次，其余每组一次。公开control接口生成指定终态数量后保留一个活跃请求；preflight
调用真实`cancel_local`预检并确认live状态未变。tick的gather边界以本地值复制16次，
不含真实跨进程collective、NPU或网络耗时。测量脚本保存在开发机
`/private/tmp/ticket03-s63-control-benchmark.py`，调用时设置`PYTHONPATH=ascend-mempool-test/src`。

| 终态数 | 单请求查询 µs | 当前room查询 µs | 活跃快照 µs | 完整快照 µs | TP观察pickle字节 | preflight取消 µs | 空计划tick µs |
| ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| 0 | 2.139 | 2.277 | 3.653 | 3.708 | 458 | 11.042 | 27.333 |
| 256 | 2.128 | 2.240 | 3.681 | 548.500 | 458 | 705.667 | 739.667 |
| 4094 | 2.213 | 2.297 | 3.730 | 9143.292 | 458 | 11549.083 | 11689.875 |

结论：查询和同步数据量已脱离无关历史；preflight仍随历史线性增长，在接近记录上限时
约11.5 ms，值得后续单独优化。这是CPU成本证据，不能推算SGLang端到端TPOT。
建议后续先评估空事务是否需要克隆，再评估仅复制事务涉及记录；任何方案须保留预检
隔离、淘汰/retirement处理及失败不提交的合同，本次不混入事务机制重写。

## 整改后复查

固定范围：`git diff 912012f3d0...HEAD`加第三部分工作区diff。按`code-review`技能并行
执行Standards/Spec两条审查；以当前工作区复查发现的修复，不替代S6.4全部01–03复查。

### Standards

未发现文档规范硬性违规或需要整改的新设计问题。control封装查询/索引，TP观察类型
具有明确用途，纯row模块不依赖BM，生产范围限于Ascend/NPU。

### Spec

发现并修正两个取消/清理回归，最终无未解决发现：

- P收到ACQUIRE/CANCEL后HTTP请求才到达：已无room owner，原直接查询会等到超时。
  现在P在track时根据终态room索引立即进入取消和清理，不把新Req绑定到旧identity。
- 各rank本地队列不同步移除终态Req：可选终态快照曾导致误报TP状态分歧。现在协议
  活跃状态单独比较，终态清理观察合并；已native_free的旧事实不能触发重新admission。

两项均先复现失败，再修复并执行行为回归。仍保留范围、绑定、层覆盖、完成事件、
drain以及全rank预检；调试统计删除、共用row和查询/同步边界均符合批准方案。

## 最终本地检查

- 独立CPU suite最终200项通过，命令为`PYTHONPATH=ascend-mempool-test/src
  /private/tmp/ascend-mempool-s1/bin/python -m unittest discover -s ascend-mempool-test/tests/unit -q`。
  首轮197项通过后，review补充三个取消/终态清理回归并修复，再执行最终200项。
  日志中的故障注入输出属于预期负向用例；开发机记录为`/private/tmp/ticket03-s63-unit.log`。
- 严格mypy 34个源文件通过，覆盖standalone src/scripts、纯row helper、runtime和PD
  control/protocol/tick/service。中途发现复用`key`变量导致tuple类型冲突，改名后通过。
- standalone完整Ruff、生产文件按仓库hook的`F401,F821,UP037`规则、64文件format及
  受影响文件isort通过，`git diff --check`通过。额外尝试的更宽生产Ruff配置有74项提示，
  未批量修改其覆盖的现存代码；本交付不声明那个更宽配置全部通过。
- Standards和Spec各自复查后无未解决增量发现；review期间暴露的两个真实回归已修复。

实际NPU gate未在Mac执行；SGLang registered测试仍需要完整Linux/NPU依赖环境，
不以独立CPU suite代替。

## 后续验证

最终版本使用[正式服务命令](../../ascend-mempool-test/FORMAL_SERVICE.md)执行fetch gate、
zero/decode/reuse请求及服务checker，普通模式按同页native命令回归。真实NPU未执行。
两端必须更新到同一最终提交并重启服务，用新日志运行checker；完成事件已改名为
`mempool decode_completion`，不能用清理前的`fetch_result`日志替代本次证据。
在已可运行的小容量内复验，不扩大ticket09的NUMA/长上下文范围；回传双端日志、
fetch报告、service-result.json、curl输出及性能结论后再记录硬件验收。
