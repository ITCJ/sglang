# Ticket26：三版本各一次短采样

用户在023清点后允许baseline、phase1、phase2各一次同负载短采样。
每段只发一个4096-token输入、32-token输出请求；服务启动/Graph预热仍保留，HTTP额外warmup关闭。
逐段人工确认清理，不自动推进下一个版本，不重跑完整验收。

## 版本与范围

- baseline：迁移前提交aa5852624e15df139df7c76bb8d757341b049713，使用原kernel。
- phase1：同一基点叠加已归档phase1-source.tar.gz，记录归档SHA256。
- phase2：准备时当前HEAD的完整源码。后续每段准备均归档source-manifest.json。
- 三者统一使用准备时HEAD中的验收scripts/src；不覆盖所选版本的生产python/。
- 原工作目录不切分支；每段在新日志目录/source创建独立checkout。
- 三个历史版本还有后续修复差异，不能把耗时变化全部归因为kernel迁移。
- 使用015已保存cases.json第一例的相同input_ids。固定greedy、ignore_eos=true、max_new_tokens=32。
- config沿用017，只打开observe/observe_trace并设1800秒请求/导出时限。
- P eager、D Graph不变。trace包含捕获/预热，离线仅关联真实rid/forward标记。

这只是采集入口，输出sample_collected不等于迁移前后相等或计算重叠成立。
各版本需比较输入hash、模型/软件身份、输出IDs和对应选择/来源，差异保留。
单个短请求仅覆盖冷启动与后续decode，不覆盖全部prefix形状和长生成。
旧版不具备26的新双写生产路径，因此这次也不证明25双目标写能被真实计算隐藏。

## 024 baseline准备：每侧一个指令

31容器：

```bash
cd /home/cryang/sglang &&
git pull --ff-only &&
python3 ascend-mempool-test/scripts/prepare_migration_sample.py \
  --variant baseline \
  --root /home/cryang/log/k2/k2-ticket26-024-baseline \
  --config /home/cryang/log/k2/k2-ticket26-017-target/config.json \
  --cases /home/cryang/log/k2/k2-ticket26-015-reference/prefill/cases.json
```

32容器同命令，仅将cases路径的prefill改为decode。
本段不重新编译multibase，不在旧checkout执行git pull；准备失败不启动服务。

## 024运行：31先、32后

31容器：

```bash
cd /home/cryang/log/k2/k2-ticket26-024-baseline/source &&
python3 -u ascend-mempool-test/scripts/diagnose_target_startup.py \
  --role prefill \
  --config /home/cryang/log/k2/k2-ticket26-024-baseline/config.json \
  --run-id k2-ticket26-024-baseline \
  --sample-json /home/cryang/log/k2/k2-ticket26-024-baseline/request.json \
  --without-index-sync --legacy-kernel
```

32容器同命令，仅将role改为decode。
每侧成功标记MIGRATION_SAMPLE_COLLECTED；结束后用户确认资源清理，暂停在本版本。
回传两侧summary.json和sample-traces.json，31的sample-response.json。
原始JSONL和trace保留在角色目录/observe，后续离线分析；不将首组时间当作完整重叠证据。

## 实现与CPU检查

无Ascend/NPU或通用主线功能源码修改。
脚本prepare_migration_sample.py准备隔离源码、共享请求与身份清单。
脚本diagnose_target_startup.py复用原启动/就绪/进程组/端口清理，增加一次请求和原有flush协议。
测试test_migration_sample.py验证生产快照保留、输入一致、只请求一次、导出先于清理、缺trace拒绝。
5项定向CPU检查通过；ruff通过。CPU不替代NPU执行。
原本地系统Python缺少tarfile filter参数，导入检查未启动；切换既有测试venv重试。

既有测试venv下baseline/phase1旧源码导入新入口均通过；未启动模型或NPU。
固定基点d6357a7d43的Standards/Spec两轴评审各0项未解决。
