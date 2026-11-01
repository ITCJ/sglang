# Ticket12本机检查

基点`b4a925a3a3f08272ac4fd7b4a3226f60fefa7c56`。NPU未运行，ticket12仍open。

- 两项新布局测试先因缺少page_size接口失败：[red记录](../ticket12-copy-contract/layout-red.log)。
- [完整CPU](full-cpu.log)只运行一次：539项，52.945秒。13个方法失败（含31/32页等subtest），主要为旧连续层夹具、旧行计数口径，以及96 GiB组件槽超过4 GiB的限制。
- 已修正：大槽按完整页分窗；旧fake copy按独立张量排列解释物理行；协议快照及注册夹具更新。
- [定向补跑](retry-cpu.log)37项，2.066秒。35项通过；余下为一个新增注册测试run-id错误、一个旧注册夹具尝试修改不可变PoolLayout。
- 新注册测试修正run-id后，第二轮[通过](signature-green.log)。旧注册夹具改为重建完整PoolLayout，并经只读评审确认；该方法已运行两轮，按用户上限未第三次执行，**不计为通过**。
- 新增2个方法后合并541项：540项最终通过，1项最后夹具修正后未复跑。没有重复运行完整套件。
- [静态记录](static.log)：37个修改Python文件通过ruff、format、isort和Python 3.9 AST；21个功能/类型依赖模块通过strict mypy。未纳入未修改且依赖NPU Triton的`restore_kernel.py`。
- [Standards/Spec评审](review.md)各0项需修改问题；后续只读复核确认最后夹具修正，未扩大测试轮次。

未复跑方法：
`test_mempool_min_entry.TestMinRunner.test_worker_checks_actual_pool_allocator_transfer_and_dram_before_register`。
其普通注册和页大小拒绝检查在第二轮已执行，随后在容量注入夹具的不可变对象赋值处中断。
最终改为重建含prefix的真实PoolLayout；容量注入分支在本票最后版本不宣称通过。
本票独立的page-first注册、TP/P-D签名拒绝及入口describe检查已通过。

完整套件命令：

```bash
PYTHONPATH=ascend-mempool-test/src:ascend-mempool-test/scripts:ascend-mempool-test/tests/unit \
  /private/tmp/k1-ticket05-fix-venv/bin/python -m unittest discover \
  -s ascend-mempool-test/tests/unit -v
```

定向选择器见[retry-selection.json](retry-selection.json)。测试使用本机HTTP/TCP，在沙箱外运行，未连接NPU主机。
失败的原始日志保持原状；本机结果不证明NPU宽地址、Graph、真实BM数据或物理NUMA驻留正确。
源码与文档的`git diff --cached --check`通过；原始`full-cpu.log`第610行保留unittest输出的尾部空格，未清洗证据。
硬件运行与回传见[K1_TICKET12.md](../../../../ascend-mempool-test/K1_TICKET12.md)。
