# K1-ticket02-003：正常关闭日志核验失败

用户回传31终端：两轮服务均已启动，最终报：

```text
ValueError: prefill: normal shutdown used forced cleanup or crashed; see /home/cryang/log/mempool-min/k1-ticket02-003/prefill/restart/prefill.log
```

最近交付为 `bef317e497`；本次实际运行SHA尚未回传。
配置仍为 `/home/cryang/mempool-min-ticket01-3132.json`。agent未访问NPU。

## 当前可判断的范围

按当前入口实现，这条错误出自最终汇总的 `check_normal_close()`。
它表示P的restart日志中至少包含以下一项：
`Schedulers still alive`、`Scheduler hit an exception`、`SR fail-stop:`、`double free or corruption`。
仅凭汇总文本无法确定匹配项，也不能确定P的BM或后续清理完成到哪个阶段。

按该执行路径推断，已完成MR/SR停机确认与P/D主进程退出等待，且D日志已收到31的
`prefill/restart/decode.log`。这是代码路径推断，尚未拿到对应报告原文。
检查器先检查P，发现错误即停止，所以不能据此说D正常关闭已通过。

下一步读取31上现有的control-quiesced.json、两侧BM关闭回执、worker_exit阶段及日志末尾。
本次不需要重新加载模型；先区分主机资源释放、通信清理和Python/native进程收尾。
NPU003未通过，票据保持open，尚未修改功能代码或放宽判据。

## 用户补充证据：两侧已到 scheduler 最后阶段，仍有进程滞留

用户回传 `quiesced=True, fault=None`。
P/D各16个rank的 `session_closed`、`host_resources_released`、
`distributed_released` 均齐全。

- D：23:27:14完成这些阶段，23:27:27仍有16个scheduler，父进程强制清理。
- P：23:27:35完成这些阶段，23:27:47仍有8个scheduler，rank为1/2/4/7/8/11/13/15。
- 本次回传片段没有002的 `Host unreachable` 或 `double free`；不能据日志片段证明全日志不存在这些错误。

### `distributed_released` 的证据边界需要纠正

该标记在 `abort_distributed_environment()` 返回后输出。
实际函数在torch缺少私有 `_abort_process_group` 时直接返回；调用抛异常时只记录warning，也会返回。
因此标记只证明调用已经结束，不能证明HCCL/native communicator实际销毁。
此前交付将此标记称为“通信清理完成回执”过强。

agent在Mac执行了生产函数本体的隔离检查：缺少API、API抛异常、正常返回三种分支都能返回；
缺少API时底层abort调用次数为0。这只确认日志含义，不是NPU退出卡住的复现。
本机torch 2.14.1的私有API文档注明只支持NCCL；这不能代替31/32实际torch_npu版本的核对。

### 已缩小的范围与待证明事项

`run_scheduler_process()` 在该标记后没有其他显式业务步骤。
其返回涉及局部对象析构，multiprocessing还会执行退出finalizer和线程shutdown。
Ascend KVManager继承的transport teardown有线程/executor/socket释放，但目前仅在role_switch调用；
正常 `release_host_resources()` 未接该teardown。BM与全局MF/TransferEngine也不是同一生命周期。
这些是实际清理边界缺口/候选点，不能凭此认定哪个等待导致003。

父进程通过psutil子进程cmdline判断scheduler存活，并未核对这些阶段回执；
“killing them before they released”是固定文本，不能反推BM未释放。
Linux psutil的cmdline路径会识别zombie并抛错，而该collector会排除psutil错误；
简单的未回收zombie误判不是当前优先解释，实际进程状态尚未采集。

下一步应在最终停机窗口抓一个滞留rank的Python全线程栈与必要的native栈，
区分线程join、ZMQ/context或HCCL/MF析构；并核对NPU环境abort API的实际行为。
用户本轮要求先分析，未修改实现、增加新验收轮次或放宽15秒/正常关闭判据。
