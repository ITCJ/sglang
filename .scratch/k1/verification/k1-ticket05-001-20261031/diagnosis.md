# K1-ticket05 NPU001：释放后下一次分配的TP分歧

用户在31/32运行提交`cf2069abf51fac98a8e772d505474f7149bbe6e1`，两机身份检查通过，代码无差异。
P=10.120.72.31，D=10.120.72.32，GLM-5.2-w4a8，TP16，page128，prefix40页，P共享8份。
模式`p-sharing-acceptance`，run-id=`k1-ticket05-001`。
原始日志留在两机`/home/cryang/log/mempool-min/k1-ticket05-001/`；本文件记录用户粘贴的证据，未读取硬件环境。

## 用户NPU证据

- 两侧复制smoke通过；Router `/ready` 与 `/status` 返回200。
- 00:03:07，A完成生成、admit、双侧free_ack，`POST /generate`返回200。
- 第二个请求收到D alloc_ack。P日志显示rank1–15完成alloc，rank0缺失。
- P最早异常为`RuntimeError: mempool TP tick failed: TP mempool ownership or phase diverged`。
  调用链：scheduler → mempool_service.advance → mempool_tick.advance。
- 随后P退出期间出现MemFabric `HybmVaManager::RemoveOneVaInfo` / `Unmap` / `exit`崩溃栈。
  Router报告Host unreachable，D持续重连P失败，验收客户端最终超时。
- 析构崩溃只记录为后续现象；本修复不声称已验证MemFabric退出问题。

## 已复现的原因与修复

free清空request，却保留coverage与last_coverage。下一轮writer_rows从实际写计数变为全0，
只有rank0执行的统计日志断言访问空request，触发无消息AssertionError。
`results["error"] = str(exc)`得到空字符串，异常汇总将它当作无错误。
rank0跳过新alloc，其余15个rank分配成功，下一native TP tick检测到所有权分歧。

释放时同时清除coverage/last_coverage；捕获提交异常时用异常类型名补足空消息。
没有增加释放回执、修改通用shutdown或调大timeout。

## 最终源码阅读清单

Ascend专用功能源码仅1个：
`python/sglang/srt/disaggregation/ascend/mempool_subrouter.py`——请求统计随free退休，空异常传播到全组fail-stop。
通用主线功能源码：无。

测试另列：`ascend-mempool-test/tests/unit/test_mempool_subrouter.py`。
包含零写计数对照、非零写计数的free→下一alloc→native TP tick，以及空异常阻止注册确认。
说明另列：本文件、ticket05 Comments、target-alignment D节及其HTML。

## 本机验证

旧临时venv缺失torch文件，首次命令在导入前失败（未执行用例）。新环境：
`/private/tmp/k1-ticket05-fix-venv/bin/python`，Python3.12，CPU torch2.15.0，msgspec0.22.0。

共同环境：`PYTHONPATH=ascend-mempool-test/src:ascend-mempool-test/scripts:ascend-mempool-test/tests/unit`。

1. 零写计数对照单独执行1次，通过；它不触发真实NPU问题。
2. `-m unittest test_mempool_subrouter.TestSubrouter.test_shared_write_counts_retire_before_the_next_allocation -v`
   修复前失败，精确复现`mempool TP tick failed: TP mempool ownership or phase diverged`。
3. `-m unittest test_mempool_subrouter.TestSubrouter.test_empty_commit_exception_stops_every_rank_before_registration_ack -v`
   修复前失败：`RuntimeError not raised`。
4. 修复后执行`-m unittest test_mempool_subrouter test_pd_tick test_pd_service -v`，40项全部通过（1.248秒）。
   两项失败用例各完成红绿两轮；零计数对照共两次；其余用例单轮。
   未重跑整套502项：本轮覆盖SR生命周期、TP一致性和服务入口，未重复设备数据/完整验收脚本测试。

ruff lint/format、isort、Python3.9语法、功能文件strict mypy与git diff --check通过。
Standards与Spec只读复审均0项未解决问题；未重复运行测试。

## 待用户重验

两机更新到修复提交后沿用原配置、先P后D，使用新run-id `k1-ticket05-002`。
001保留，不覆盖；入口仍自动完成两轮精简功能验收。
本地CPU通过不代表NPU通过；本票仍open，08不解锁。
