# Ticket03 S6.2：删除 shadow 和旧 READBACK

日期：2026-10-06。实现基线：`a22f36db0b`，当前分支`cryang/dev/mempool`。
范围来自用户批准的[S6计划](ticket-03-s6-plan.md)第2步；STD-01/02/03留在S6.3。
本阶段删除旧入口、修正SPEC-01/02并迁移有效测试。Ticket03保持open，等待最终NPU复验。

## 修改与执行路径

| 代码位置（生产文件相对`python/sglang/srt/`） | 改动与作用 |
| --- | --- |
| `hardware_backend/npu/sparsity_driven_kv_offload/config.py` | 删除P/D shadow枚举及能力分支、旧READBACK验证方法和调用。MEMPOOL开启选择正式P/D，关闭恢复普通模式，拓扑、模型与容量检查仍在分配前执行。 |
| `hardware_backend/npu/attention/ascend_backend.py` | 删除shadow枚举引用与已失去用途的runtime支持检查调用。普通prefill仍保留native KV。 |
| `hardware_backend/npu/mempool/readback.py` | 删除旧host selected KV与BM双路径比较、比较错误类型及统计实现。 |
| `hardware_backend/npu/mempool/runtime.py` | 删除READBACK参数、对象、层记录、快照、比较、报告和完成消费。保留每forward写入/fetch快照、完成事件、绑定/可读范围与故障阻止释放。 |
| `hardware_backend/npu/sparsity_driven_kv_offload/attention.py` | 删除旧selected KV生成后的额外比较调用。正式BM miss和普通host miss继续按模式执行。 |
| `disaggregation/ascend/mempool_service.py` | 删除READBACK报告及专用错误包装，直接消费runtime完成；错误仍进入同侧TP fault共识。 |
| `environ.py` | 删除全局`SGLANG_NPU_MEMPOOL_READBACK`定义。这是唯一共享生产代码改动：仅删NPU引用会留下失效的全局配置入口，因此必须同步删除定义。 |
| `ascend-mempool-test/scripts/verify_resources.py` | SPEC-01：移除正式模式必须报“S5拒绝”的过期断言；P继续检查native/Index K及allocator回收，D继续检查零host/staging、alloc/free/clear和slot map复用。CLI去掉shadow选项。 |
| `test/registered/unit/npu/test_sparsity_driven_kv_offload_config.py` | SPEC-02：启动测试分别期待正式P/D，删除shadow/READBACK矩阵；保留普通模式、非法TP及超范围容量拒绝检查。 |
| `ascend-mempool-test/scripts/verify_service.py` | 将旧READBACK失败标记替换为实际`mempool KV fetch invalid selection`；保留全rank、Graph、binding、完成、释放与复用合同。 |

正常路径为：startup mode → native/sparse资源 → Graph前BM/runtime映射 → P writer与
Index K handoff → D cache hit或P/D BM miss → 完成事件 → drain → detach/native free →
DONE/RELEASE_ACK。P prompt slot一直保留到D确认最终读取完成；删除比较功能不改变所有权。
普通模式继续使用原host/staging/native-KV传输，关闭mempool不会新增BM依赖。

本阶段保留正式fetch的范围/覆盖检查及调试计数；调试统计删除按用户决定在S6.3完成。
独立`verify_graph.py`、`verify_writer.py`、`verify_fetch.py`的已知pattern数值验证保留。

## 测试迁移

| 旧覆盖 | 最终覆盖 |
| --- | --- |
| READBACK中两层P/D边界、D offset 0、padding、row/slot复用 | `test_fetch_layers.py`对正式fetch的输出逐元素核对独立值，使用16×2048形状及不同P/D slots；事件完成前不能detach。 |
| READBACK绑定、未写正索引、前一个失败不能被后一次覆盖 | 已有`test_fetch.py`正式范围/完成测试继续覆盖；不保留对旧host参考值的诊断比较。 |
| READBACK capture层覆盖 | `test_fetch_layers.py`验证zero-valid capture仍必须完成两层fetch，padding输出保持sentinel。 |
| 服务读回成功报告与失败禁止释放 | `test_pd_service.py`改用正式fetch；批准的P/D slots能取到独立预期数据，非法选择故障不能free或归还D slot。 |
| shadow日志gate全rank/replay/层检查 | 迁入`test_service_gate.py`正式日志入口；缺单rank ACK、replay、完成报告或层检查会失败。 |
| 普通/shadow资源与传输矩阵 | 保留普通local/PD host读写、staging和native K/V+Index K传输；删除shadow行。 |

删除`verify_shadow_service.py`、`test_shadow_service.py`、`test_readback.py`。
旧READBACK服务说明移至[历史归档](archive/ticket-02-readback-service.md)，历史票据链接随之更新。
当前README、资源/传输/正式服务说明只给可执行的现有入口。
`run_service.sh`删除READBACK导出、dry-run字段及环境清单项；外部
`ascend-sglang-script/pd-disaggregation/glm51mempool.sh`仅删除同一行export，保留原格式。
外部脚本仓库提交：`5cdc28e`（main）。

## 本地验证与审查

迁移遵循现有配置、资源、runtime和service公开接口。正式checker非法选择用例先复现
误放行，再修正日志标记后通过；这项发现随入口迁移修复。

Mac实际执行：

- 完整独立CPU suite：192项通过，输出`/tmp/ticket03-s6.2-unit.log`；删除专用测试后计数低于原202项。
- 严格mypy：33个源文件通过；18个改动Python文件AST、仓库hook Ruff规则和format通过。
- 按各目录配置的isort、独立测试目录Ruff检查通过；两份启动脚本bash语法、P/D的formal/native四种dry-run通过。
- 当前生产/测试代码无shadow枚举、READBACK环境变量、旧比较/报告入口引用。独立pattern读取数据仍保留。

### Standards review

独立审查未发现硬性仓库规范违例或本轮新增的实质代码气味。模式职责、request row与
slot区分、完成事件及安全释放条件保持清晰；STD-01/02/03仍按批准计划留待S6.3。
审查指出旧Shadow注释、S4文档开头和ticket当前状态说明未同步，三处均已修正。

### Spec review

独立审查未发现功能性缺陷或越界实现。SPEC-01/02已修正，有效覆盖已迁至正式
fetch/service；普通host/staging、独立数值验证、binding、可读范围、层覆盖、完成事件、
drain和所有权保护保留。发现两项P3文字遗漏：`environ.py`的旧Shadow注释和
`PD_TRANSFER.md`过期S5启动保护说明，均已修正；无未解决的本轮审查发现。
两路审查者已复查上述文字修正，确认本轮无遗留项。

Mac不能替代NPU数值和Graph验收。
完整registered启动测试需要受支持的Python及SGLang依赖，本机Python3.9在导入
`environ.py`的`str | None`注解时失败，未执行该测试体。

## 用户复验命令

前置：P=`10.120.72.31`、D=`10.120.72.32`，相同提交及已有CANN、torch_npu、
sgl_kernel_npu、MemFabric 1.1.4环境。组件gate使用空闲device0，实际server每侧16卡。
保持此前context1024、P/D各512的小容量服务配置；本次不扩大NUMA/长上下文容量。

1. 在任一端完整SGLang环境执行registered回归，不需要启动server：

```bash
cd /home/cryang/sglang
export PYTHONPATH="$PWD/python${PYTHONPATH:+:$PYTHONPATH}"
python3 test/registered/unit/npu/test_sparsity_driven_kv_offload_config.py
python3 test/registered/unit/model_executor/test_hisparse_pool_configurator.py
```

2. 在任一端空闲device0执行四种资源模式，每种使用独立进程：

```bash
bash -o pipefail <<'SH'
set -eu
cd /home/cryang/sglang
export PYTHONPATH="$PWD/python${PYTHONPATH:+:$PYTHONPATH}"
mkdir -p /tmp/ticket03-s6.2
git rev-parse HEAD > /tmp/ticket03-s6.2/version.txt
for mode in pd_prefill_mempool pd_decode_mempool local_offload pd_decode_offload; do
  python3 -u ascend-mempool-test/scripts/verify_resources.py \
    --mode "$mode" --device-id 0 --report "/tmp/ticket03-s6.2/resources-${mode}.json" \
    2>&1 | tee "/tmp/ticket03-s6.2/resources-${mode}.log"
done
SH
```

预期4条`RESOURCE_PASS`、4份`success=true`报告及全部退出0；正式模式不再中止于
“Expected rejection containing 'S5'”。D正式模式host/staging为0，普通D二者大于0。

3. 先P后D执行[正式fetch双机命令](../../ascend-mempool-test/FORMAL_SERVICE.md#1-连续异步-replay-组件-gate)。
该页已填两机IP，使用18873/18874、NIC24770；两侧均`ALL_CHECKS_PASSED`，D有30条
`FETCH_PASS`且`queued_forwards=5`。同时复跑[writer gate](../../ascend-mempool-test/README.md)
验证删除完成快照字段后P/D writer及Graph完成语义；两侧20条PASS及`ALL_CHECKS_PASSED`。

4. 组件进程退出后，按[正式服务说明](../../ascend-mempool-test/FORMAL_SERVICE.md#2-启动正式服务)
启动P、D、router，执行该页zero/decode/reuse三请求和`verify_service.py`。
预期`FORMAL_SERVICE_PASSED`，最终全rank free16；curl小题目与性能另由用户确认。
关闭MEMPOOL后按同页native模式命令复跑普通sparse PD，核对实际host/staging/native
传输和Graph输出。该说明包含权重路径、环境、端口、请求与回传文件。

任何异常、超时、缺rank、数值差异或资源未归还都不能判通过。保存本次SHA、完整P/D
日志和JSON，不能沿用S5清理前的结果。完整S6最终验收仍须在S6.3/S6.4完成后的同一版本执行。
