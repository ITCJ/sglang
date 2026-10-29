# K1-ticket02 最终代码清单

按 `993ae38ed6`（ticket01结束）到当前恢复默认退出版本的净差异列出。
本票已推送的历史提交中曾包含退出扩展；下列清单反映撤回后的最终实现，不把已撤回代码计入。

## NPU 生产代码：3 个文件

| 阅读顺序 | 文件 | 本票改动 |
|---|---|---|
| 1 | [sharing_plan.py](../../python/sglang/srt/hardware_backend/npu/mempool/sharing_plan.py) | 新增PoolSession，由既有NUMA/分组规划生成参与者、owner、贡献量和端口参数 |
| 2 | [manager.py](../../python/sglang/srt/hardware_backend/npu/mempool/manager.py) | 多成员会话初始化、逻辑P/D到物理owner映射、副本注册；删除旧rank-pair包装，保留组件close接口 |
| 3 | [runtime.py](../../python/sglang/srt/hardware_backend/npu/mempool/runtime.py) | 真实模型capture前按SharingPlan/PoolSession创建池，复用既有NUMA功能 |

## Ascend 生产代码：5 个文件

| 阅读顺序 | 文件 | 本票改动 |
|---|---|---|
| 4 | [mempool_accounting.py](../../python/sglang/srt/disaggregation/ascend/mempool_accounting.py) | 新增按副本维护的prompt覆盖位图、旧计数器对照及读者计数 |
| 5 | [mempool_tick.py](../../python/sglang/srt/disaggregation/ascend/mempool_tick.py) | RequestObservation新增write_range，携带真实写回范围 |
| 6 | [mempool_service.py](../../python/sglang/srt/disaggregation/ascend/mempool_service.py) | 从活跃/已分离请求读取实际写回范围，传给SR |
| 7 | [mempool_subrouter.py](../../python/sglang/srt/disaggregation/ascend/mempool_subrouter.py) | 聚合页覆盖计算ready_tokens；读者归零参与free门控；记录ready/free证据 |
| 8 | [mempool_router_transport.py](../../python/sglang/srt/disaggregation/ascend/mempool_router_transport.py) | 注册新增replica_locations；接收消息类型注解，不新增退出协议 |

## 公共主线生产代码：0 个文件

`python/sglang/srt/managers/scheduler.py`、`disaggregation/prefill.py`、
`disaggregation/decode.py`已逐字节恢复至`993ae38ed6`。
`mempool_router.py`也恢复原样；quiesce、主动BM关闭hook和退出阶段日志均已撤回。

## 独立组件与验收工具：3 个文件

| 文件 | 用途 |
|---|---|
| [sharing_pool.py](../../ascend-mempool-test/src/ascend_mempool/sharing_pool.py) | 实验共享池复用公共manager，删除重复join/owner映射实现 |
| [run_mempool_min.py](../../ascend-mempool-test/scripts/run_mempool_min.py) | 两轮session-acceptance、精简复制smoke、故障/重启编排及进程/端口检查；退出使用已有Processes清理 |
| [verify_mempool_session.py](../../ascend-mempool-test/scripts/verify_mempool_session.py) | 副本注册、覆盖/计数器等价、零读者、容量拒绝重放和两轮汇总 |

`run_service.sh`已恢复原样，不属于最终净改动。

## 测试代码：5 个文件

| 文件 | 覆盖范围 |
|---|---|
| [test_pool_session.py](../../ascend-mempool-test/tests/unit/test_pool_session.py) | 会话参与者、NUMA、owner映射、组件关闭及重建 |
| [test_pair_startup.py](../../ascend-mempool-test/tests/unit/test_pair_startup.py) | 原启动测试改走实际规划会话接口，移除旧包装测试 |
| [test_mempool_subrouter.py](../../ascend-mempool-test/tests/unit/test_mempool_subrouter.py) | 页覆盖完成、最后副本、读者计数与原生命周期 |
| [test_mempool_min_entry.py](../../ascend-mempool-test/tests/unit/test_mempool_min_entry.py) | 既有入口fixture补齐副本注册接口 |
| [test_mempool_session_acceptance.py](../../ascend-mempool-test/tests/unit/test_mempool_session_acceptance.py) | 两轮CLI、证据缺失、真实注册容量重放和端口释放 |

记忆与交付文档不计入功能源码；退出偏好记录于仓库 [AGENTS.md](../../AGENTS.md)。
[本轮验证](verification/ticket02-default-exit-20261030/README.md)：483项CPU、6模块strict mypy、两轴审查通过；用户已确认NPU004验收通过，02已关闭。
