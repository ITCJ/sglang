# Ticket11 demo-001：用户回传launcher结果

2026-11-02，来源为用户在会话中粘贴的31/32 launcher.json；未访问远端、未重跑。
交付候选为746ec2d54c；launcher不含commit，实际运行版本待all-ranks.json核实。

| 侧 | exit_code | children_reaped | 端口重绑 |
|---|---|---|---|
| P，10.120.72.31 | 0 | true | 29227、29209、29218、29200、29100、29101全部passed |
| D，10.120.72.32 | 0 | true | 29213、29222、29231、29204全部passed |

两侧interface_ready=false、user_accepted=false均保留。
按交付launcher的判据，exit_code=0要求本侧全部rank报告passed且端口检查通过。
结合该版本worker流程，可据此推断设备内容及全局块数/字节检查完成；尚未独立核对原始逐rank报告。
GIL实际推进、submit/wait耗时、SDK安装版本与实际设备信息待提取已有all-ranks.json。
interface_ready是入口固定保留标记，不是本次观测到接口失败；硬件可见性契约和SDK子提交错误传播仍待核对。
本记录不构成用户确认关闭ticket11，不启动服务接入；11 open、09阻塞。

## 逐rank摘要已回传

用户随后回传全部8rank摘要，见user-rank-summary.json；此文件规范化会话数据，不冒充原始all-ranks.json。
全部rank为746ec2d54c5fa015a891e5e9b94fc2d47251d783、status=passed。
P admit：rank0/2各8块、48,758,784B，共16块、97,517,568B；rank1/3无工作。
P fill：rank0为12块、51,511,296B；rank1/2/3各4块、46,006,272B，共24块、189,530,112B。
实际搬运的submit/wait均返回0；各P rank在两op复用同一执行线程TID。
admit wait期间rank0/2各观察10次tick；fill四rank分别15/12/11/13次。
同进程demo控制循环在真实wait期间推进已有证据；不等同于真实服务scheduler时延已验证。
D四rank不执行池间搬运，其passed覆盖脚本中的设备索引读取P alive内容校验；空任务无wait样本正常。
按该版本代码，P admit后也完成prefix设备索引内容校验。源码完成语义/fence保证不能仅由功能观测推出。

耗时只用于此次证据：admit提交0.376–1.068ms、wait1.518–1.531ms；fill提交0.084–0.943ms、wait1.778–2.266ms。
未从不同rank的并行时间求和得出总耗时，未据此承诺带宽/性能提升。
mf_version全部unknown，实际安装构建尚未确认；SDK子提交错误传播与完整硬件可见性契约仍待核对。
两组功能及wait期间控制推进通过；不重跑、不开展八组demo、不关闭11、不自动接入生产。

## 实际包版本与对应tag核对

用户确认两侧distribution：memfabric_hybrid=1.1.4，memfabric_zbal=1.1.2。
原报告unknown是模块__version__缺失，并非安装版本不一致；保留原始摘要不改写。
本地GitCode有v1.1.4，commit c01f3ad842b9ff7412681a44b67141ce7a124c6d；本次使用git show只读核对，未checkout/升级/修改SDK。

- 普通copy_data_batch调用smem_bm_copy_batch→DataCopyBatch，不自动走concurrent接口。
- v1.1.4的HostDataOpSDMA::BatchCopyG2G仍将子提交错误记录在ret，最后却返回InnerWait(options,0)。异步InnerWait返回成功，此错误传播缺口在对应tag源码也存在。
- Python batch/wait释放GIL、wait按调用线程取得内部SDMA stream，与demo方式一致。
- v1.1.4另外提供copy_data_batch_partial_succeed；不能直接替换。平均块大于4MiB且多条目时，它走库线程池逐项hybm_data_copy，async模式下外层调用线程的wait不自动覆盖这些线程的流。小块分支仍回普通batch。
- 包版本不证明部署动态库与tag逐字一致；此处是对应发布tag源码缺口，未声称已在实机注入失败复现。

demo正常功能和控制推进结论保留。下一步应针对当前部署1.1.4评估最小错误传播修复/接口方案；不建议为此盲目升级到同样含缺口的本地1.3.0。不自动修改SDK、不重复正常demo。
