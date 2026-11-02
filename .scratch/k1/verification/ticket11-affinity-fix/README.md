# Ticket11 001失败与绑核修复

2026-11-02；工程基线d80ec2b2e1。用户确认31/32的Docker内均已安装SDK补丁。

## 用户回传事实

001 P服务ready成功；首请求完成P ready、native_freed和D finished，随后在18:01:17报：

```text
mempool_router fail-stop: SR prefill: pool copy FAILED: RuntimeError("OSError(22, 'Invalid argument')")
POST /generate HTTP/1.1 500
```

HTTP500是SR fail-stop的外层结果。执行器仅在setup异常时保存repr再包装RuntimeError，
故此异常形式指向执行线程初始化；当前日志未保留原始setup栈，不能证明是哪条系统调用或现场CPU mask。
用户未回传SDK搬运成功证据，001不计为验收通过；11仍open，09阻塞。

## 本地复现与修复

旧代码把目标NUMA cpulist与sched_getaffinity(0)求交，后者是当前线程mask，不是容器cpuset上限。
SGLang scheduler已有CPU/NUMA绑核入口，执行器继承该mask；目标节点与旧mask不交时会传空集而EINVAL。
Linux对sched_setaffinity请求自行按cpuset过滤，参见[内核文档](https://cdn.kernel.org/doc/html/latest/admin-guide/cgroup-v1/cpusets.html)。

公共Executor回归模拟容器CPU{0,1,4,5}、继承mask{0,1}、目标NUMA{4,5,6,7}。
未修复时得到与用户同形的FAILED RuntimeError("OSError(22, 'Invalid argument')")；
修复后READY，实际绑定{4,5}，batch调用一次。OS/NPU边界为mock，未声称真实Docker/NPU复现。
功能修复仅pool_copy.py：直接setaffinity(0,目标NUMA集合)，由内核施加cpuset限制；
无静默回退，目标节点真正不可用仍失败。setup异常增加原始traceback日志以区分绑核与set_device失败。
通用主线源码改动0。测试文件test_pool_copy.py增加上述回归。

## 实际检查

第一轮（修复前）只跑新增回归：1项失败，断言READY却得到FAILED；异常同上。
第二轮（修复后）跑test_pool_copy整文件：3项全部通过，见green.log；未第三次运行或重跑全套。
ruff与pool_copy strict mypy通过。Standards/Spec独立复核无阻断。

```bash
PYTHONPATH=ascend-mempool-test/src:ascend-mempool-test/tests/unit python -m unittest test_pool_copy.TestPoolCopy.test_setup_rebinds_inherited_affinity_to_pool_numa
PYTHONPATH=ascend-mempool-test/src:ascend-mempool-test/tests/unit python -m unittest test_pool_copy
```

现场根因尚待002重测确认，尤其需区分Docker确实禁止目标节点CPU的情况。
沿用用户已装的修复SDK，不需重编SDK；仅更新本工程Python代码，使用run-id k1-ticket11-002。
本次是001后的第二轮尝试；若仍失败，先看新的原始setup栈，不自动安排第三轮服务重跑。
