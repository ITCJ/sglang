# K1-ticket03 NPU003：私有prefix的CPU比较段错误

用户在31/32运行`split-read-acceptance`，run-id为`k1-ticket03-003`。
配置为`/home/cryang/mempool-min-ticket01-3132.json`，startup_timeout=7200、
operation_timeout=600；日志保存在`/home/cryang/log/mempool-min/k1-ticket03-003/`。
本次修复基点`0b94af3ceefc4c898a1e58be2a3c451d27fbe377`。
用户未回传实机版本文件，不把基点直接记成实机SHA。

## 已确认的故障位置

- A（`...-1`）完成请求、prefix保存与释放；13:22:26.101返回HTTP 200。
- B（`...-2`）13:22:26被alloc/dispatch；16 rank均打印32槽位、
  main=4,718,592B、index=1,048,576B、data_buffers=2。
- prefill.log第2190–2197行的首个完整当前线程栈指向
  `prefix.py:538 _submit_load_back → torch.equal(actual[slot], private[...])`。
  第4478行起的native栈为`THPVariable_equal → at::_ops::equal → cpu_equal`。
- 13:22:51 scheduler_12以-11退出；父进程随后按默认路径清理。
  Main在13:22:58.670报告Host unreachable。TBE、py-spy、coredump等待是后续现象。

本次故障发生在第一处私有prefix比较，散开调用排在其后。
不能据此声称all_gather内容、scatter、B/C恢复或整票NPU验收已经通过。

## 假设与证据

1. 首要假设：`LOCAL_HOST`的返回值没有CPU可读映射。
   `local_cpu_view`原来用ctypes.from_address加torch.frombuffer直接包装SDK地址；
   BM创建只有NUMA/performance位，没有DRAM_MAP_HOST_VA。
   本地SDK的VMM实现把同一slice地址登记为DVA/HVA，是否映射CPU由bit9决定。
   因此地址翻译非零不等于CPU可解引用，符合本次cpu_equal崩溃边界。
2. 次要假设：请求释放导致底层pool失效。
   A释放的是请求槽，私有prefix仍被保留；manager在worker生命周期内持有BM，
   正常请求free不会销毁handle。所给栈和代码不支持把此处归为shutdown问题。
3. 次要假设：页偏移越界。
   B命中32页、page128，配置prefix40页；恢复入口验证ready页且禁止重复。
   现有CPU内容/非连续页检查通过；这不能证明NPU指针可由CPU访问。

SDK依据来自本地`gitcode/memfabric_hybrid`（HEAD `29fbd1ae`）：
`src/smem/include/host/smem_bm_def.h`定义bit9，
`src/hybm/csrc/mm/hybm_vmm_based_segment.cpp`的AllocLocalMemory、
MapMemByImport/MapMemBySetAccess分别决定设备映射与host映射加设备访问。
另一份本地SDK（HEAD `004d9317`）也有同一bit9语义。
尚未读取实机SDK二进制或其准确源码；SDK路径判断及修复效果仍待NPU复测确认。

## 修复与复现边界

P侧仅在prefix开启且verify_prefix=true时，初始化session传入cpu_access=true，
BM创建将bit9与既有NUMA位合并。CPU视图继续借用真实私有副本，由manager持有底层池。
普通运行和D侧采用原flags。两块汇总HBM缓冲、共享槽位表、三步恢复不增加数据缓冲。
没有改用G2H：本地SDK该接口内部申请临时HBM，会影响本票缓冲约束。

测试的SDK边界现在拒绝未请求host映射的CPU读取，避免在测试进程中触发真实SIGSEGV。
旧实现运行2项定向检查时，正常恢复对照通过，私有副本检查在该边界失败；见red.log。
这复现的是缺失映射合同，不是Mac上运行真实CANN/驱动的段错误。
修复后12项定向检查通过，含两种分工、1/31/32页、内容损坏反例和session的NUMA位保留。
新增session检查验证host映射显式启用、CPU视图确实指向所分配存储；普通flags保持原值。

## 本次净改动阅读顺序

Ascend/NPU功能源码：

1. `python/sglang/srt/hardware_backend/npu/mempool/runtime.py`：由P侧prefix验收配置请求host映射。
2. `python/sglang/srt/hardware_backend/npu/mempool/manager.py`：传递cpu_access，在创建时组合SDK标志。

通用主线功能源码：无。

测试：`test_prefix.py`修正SDK替身的host地址合同；`test_pool_session.py`验证创建、NUMA位和CPU读回。
说明：本记录、ticket03 Comments、交付/运行说明及target-alignment D节。
baselinetest分支未修改；工程验收接线仍按用户约定在NPU确认后删除。

## 验证状态

agent在Mac执行：

- `red.log`：2项定向检查，正常恢复对照通过，私有副本读取在未映射SDK边界失败。
- `focused-cpu.log`：修复后12项通过（0.925秒）。随后将已有非连续恢复用例开启V2，
  进一步覆盖主KV、Index K的私有副本与原生内容正向比较。
- `full-cpu.log`：首轮完整495项，1项新开启V2的测试缺少verify_alive准备；47.489秒。
- `final-cpu.log`：补齐测试前置步骤后，最终495项全部通过；47.713秒。
  本次完整套件仅两轮，未裁减现有覆盖。
- `static.log`：4模块strict mypy通过；4个修改Python文件的ruff、isort、格式、
  Python3.9语法通过。最终补齐verify_alive后再次核对该测试文件的ruff和格式。

完整CPU命令：

```bash
PYTHONPATH=ascend-mempool-test/src:ascend-mempool-test/scripts:ascend-mempool-test/tests/unit \
  /private/tmp/sglang-pr43579-review-venv/bin/python -m unittest discover \
  -s ascend-mempool-test/tests/unit -v
```

## Standards

0项硬标准违规，0项需处理异味。功能变化在NPU目录，采用既有初始化接口，
按已暴露的映射问题修复，没有新增防御层或改变通用退出流程。
测试复用存储与SDK边界，保留NUMA位及普通模式行为；文档明确NPU待验。

## Spec

0项发现。本地SDK的bit9路径支持host映射及device access，修复与故障边界一致。
真实私有副本oracle、两块HBM缓冲、三步恢复和完成门控保留，未增加数据复制阶段。
两轴由独立agent只读评审，未额外运行测试。

NPU003失败；NPU004待用户运行，ticket保持open，验收项不勾选。
CPU检查不能验证真实驱动页表、SDK二进制、Triton-Ascend或HCCL行为。
双机完整自运行命令见[运行说明](../../../../ascend-mempool-test/K1_TICKET03.md)。
