# Ticket12交付：主存page first布局

五类主存区域已改为`[物理页, 层, 页内token, head, feature]`。
副本仍为8/8/1/1/1，HBM原生布局、prefix分配、描述符、分工、控制操作及完成语义沿用。
本票实现和双轴评审已完成；[004双机自动验收](verification/k1-ticket12-004/README.md)通过并经用户确认。12已closed，11已解锁但未实施。

主KV和Index K分别形成连续页块，当前GLM-5.2每页为11,501,568 B和688,128 B。
布局集中处理逻辑token行到物理行的变换，view提供`page_block(physical_page)`，
返回池库GVA、设备地址和长度，供11后续使用。

现有UniDexCopy内核保留。小区域/实际prefix段一次提交；大区域按槽分段，
旧大容量组件池中超过4 GiB的槽进一步按完整页切分。每次调用固定完整64位基址，
索引和有效mask在设备上计算，图中保留零有效调用，换槽不新增主机读回或同步。
图重放的实际支持仍需用户NPU验证；分段增加提交次数，本票不承诺性能改善。

P写回、D特征分写及两源稀疏读复用同一拷贝适配层。
prefix三操作仍逐层索引拷贝，load_back保留两个汇总缓冲、all_gather及原生scatter。
只有目标完成后才汇总ready或publish，默认父进程清理路径沿用。
运行时注册和P/D启动签名携带布局及全部维度，Main核对TP内与P/D之间一致性。

## 功能源码阅读顺序

全部属于Ascend/NPU专用源码；通用主线功能源码净改动为0。

| 文件 | 用途 |
|---|---|
| [layout.py](../../python/sglang/srt/hardware_backend/npu/mempool/layout.py) | 页几何、行变换、页块偏移及签名字段；逻辑层字节不再代表连续跨度 |
| [config.py](../../python/sglang/srt/hardware_backend/npu/mempool/config.py) | 将统一B和模型几何传入五类区域 |
| [manager.py](../../python/sglang/srt/hardware_backend/npu/mempool/manager.py) | 实际段view、首行地址、页块接口、注册及跨页分段写入；沿用已有映射检查 |
| [indexed_copy.py](../../python/sglang/srt/hardware_backend/npu/mempool/indexed_copy.py) | 设备行索引转换和32位跨度内的固定调用窗口，保留原内核 |
| [offload.py](../../python/sglang/srt/hardware_backend/npu/mempool/offload.py) | 写回接入新寻址，保留mask与D特征分写 |
| [copy.py](../../python/sglang/srt/hardware_backend/npu/mempool/copy.py) | prompt/decode两源稀疏读取接入新寻址 |
| [prefix.py](../../python/sglang/srt/hardware_backend/npu/mempool/prefix.py) | admit、fill和逐层load_back适配；选页、分片和完成路径沿用 |
| [shared_probe.py](../../python/sglang/srt/hardware_backend/npu/mempool/shared_probe.py) | 适配既有启动探针地址，不新增BM读取场景 |
| [mempool_protocol.py](../../python/sglang/srt/disaggregation/ascend/mempool_protocol.py) | 在已有P/D启动描述中携带物理布局签名 |
| [mempool_service.py](../../python/sglang/srt/disaggregation/ascend/mempool_service.py) | 以实际PoolLayout生成启动签名 |
| [mempool_router_transport.py](../../python/sglang/srt/disaggregation/ascend/mempool_router_transport.py) | 注册实际布局事实与签名 |
| [mempool_router.py](../../python/sglang/srt/disaggregation/ascend/mempool_router.py) | 在既有注册比较中核对page first及各rank布局一致性 |

## 测试、脚本和说明

- [K1_TICKET12.md](../../ascend-mempool-test/K1_TICKET12.md)：双机各一条命令、资源需求、判据和证据限制。
- [run_mempool_min.py](../../ascend-mempool-test/scripts/run_mempool_min.py)：新增`page-first-acceptance`，复用两次模型启动和六例，协调独立NPU拷贝检查。
- [verify_page_first_copy.py](../../ascend-mempool-test/scripts/verify_page_first_copy.py)：小几何/真实P几何的NPU设备内存检查，不分配或读回BM；独立物理张量oracle与图重放。
- [verify_mempool_shared.py](../../ascend-mempool-test/scripts/verify_mempool_shared.py)：核对实际五页段、B、全部维度、页块字节及签名。
- [test_page_first.py](../../ascend-mempool-test/tests/unit/test_page_first.py)与[page_first_oracle.py](../../ascend-mempool-test/tests/unit/page_first_oracle.py)：独立物理排列、页块/行一致性、宽地址、特征分写和mask。
- 既有layout、pool、offload、runtime、fetch、shared decode/index/prefill、prefix、split_read、注册与协议测试适配新契约；CPU fake通过独立张量排列解释物理行。
- `ascend-mempool-test/src/ascend_mempool/sharing_copy.py`和`sharing_pipeline.py`：既有组件测试路径复用新地址适配，未恢复服务性能埋点。

## 验证和边界

[第一步接口证据](verification/ticket12-copy-contract/README.md)区分本地源码契约、CPU地址核对和待运行NPU证据。
完整CPU一次539项，52.945秒；定向补跑37项，新增签名用例第二轮通过。
合并541个方法中540个已验证通过；1个旧worker注册用例因夹具修改不可变PoolLayout失败，
现已改为重建布局并经只读评审确认。该方法已运行两轮，按用户上限不第三次执行，不能计为通过。
完整套件没有重复运行；新布局/页块、宽地址、物理内容oracle及签名用例均已通过。
37文件静态检查、21模块strict mypy通过；Standards与Spec各0项需修改的问题。
详见[本机检查与裁减范围](verification/ticket12-cpu-20261102/README.md)及[双轴评审](verification/ticket12-cpu-20261102/review.md)。

NPU验收采用源快照、正常load_back设备数据和读写记录；不新增直接BM主存读取。
物理NUMA驻留、直接alive/fill主存比较和性能不在本票；CPU不能代替NPU验收。
用户确认本票硬件结果后才关闭12并解锁11。
