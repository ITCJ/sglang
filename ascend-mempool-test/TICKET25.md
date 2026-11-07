# Ticket 25：prompt 双写

默认 `dual_write=false`，沿用原路径。开启项在现有 RouterConfig JSON 中；
支持共享 P/D alive、共享 prefix、`prefix_admit_mode=owners` 的配置。
无需新增环境变量或通用主线 hook。

## 正常路径

1. Main 在 route 时预留 D 的新算 prompt 页和生成页，P alive 预留为0。
   Prefix 候选页在请求开始预留；容量不足时整次不保存新增页，只写 D。
2. P SR 返回候选页，安装远端 D 页位置。D 安装固定页路由：命中页指向
   prefix 正本，新算页指向 D alive。命中页另外保留请求级引用直到 D drain。
3. P 的 load_back 保留，fill 不再发出。每层主 KV 从同一 HBM 源生成两组
   写任务：对应 P rank 对按页写 D 副本，prefix segment owner 写候选页。
   Index K 只写候选页；给 D 的 Index K 仍走原生传输。
4. 写入沿用原 stream，forward 完成 event 后核对远端实际 mask 和各层
   候选主 KV/Index K 计数，再推进 ready。日志 `dual_write_complete`
   分别给出远端主 KV、候选主 KV、候选 Index K 的有效行数与字节。
5. D 的 cache/current HBM 路径不变；缺失数据读取 prefix 正本或本地 D alive。
   Graph 使用固定容量设备路由表，复用时只改内容。观测中 prefix 与 D 分开计数。
6. D drain、P native cleanup 后，admit 只发布候选页，不搬运数据。
   Main 重查 hash，重复候选页归还；发布完成后释放引用和请求页。
   保留原 fail-stop、父进程退出和清理路径。

D 的物理池容量与生成上限分开：`decode_storage_capacity` 在开启时为
`prompt_capacity + decode_capacity`，关闭时等于原 `decode_capacity`。
每个池仍有16个绑定槽；实际请求按页分配。启动内存预算计入扩大的 D 区域。
P alive 区域仍注册/映射，只是不分配请求页，因此本票不声称回收其物理内存。

## 本次验证与限制

用户明确要求缩小测试，并指定“双机的on不要作”。因此本次不安排双机 on 组，
不提供把 on 当作已验证的验收结论；Ticket 25 保持 open，20/23不解锁。
真实 kernel、远端写完成后 D 立即读回、Graph 和计算重叠均未验证。
本地完成事件仅为实现所用的完成边界，不能替代这些硬件证据。

新增3项 CPU 用例通过：Main 冷请求预留；A/B/C/E 发布、命中引用、满池跳过保存和释放；
真实 TP16 SR 分配/dispatch。它们验证控制路径，不证明设备数据正确。

完整 CPU 集合运行1轮，652项；已跑过red/green的新增预留测试未安排第三轮。
24个受本机权限限制的方法只重跑1轮，同时完成新增SR用例的第2轮，共25项、24项通过。
尚有9个方法未通过：7个既有基线问题、1个子进程清理权限问题、1个旧性能对照脚本
因 manager 已改变而拒跑。保留该脚本的基线一致性检查，未启动新性能优化。
具体列表和原始日志见 `.scratch/k2/verification/ticket25-local/`。

4个主要新增/控制模块 strict mypy 通过；较宽检查仍有既有依赖/类型问题。
修改文件 Ruff、格式和语法检查通过。Standards 与 Spec 评审均无静态剩余发现。

## 双机 off 回归入口（由用户运行）

复用已有自运行入口。准备原31/32部署配置的副本，将 `dual_write` 设为 `false`；
模型、地址、网卡和端口沿用实际部署值。两侧使用相同的新 run-id。
该入口自动准备输入、启动、等待就绪、请求、核对结果、收集报告并清理自身进程；
失败非零。不访问 NPU 的开发机不要执行。

P 容器：

```bash
python ascend-mempool-test/scripts/run_mempool_min.py \
  --role prefill --mode page-first-acceptance \
  --config /path/to/ticket25-off.json --run-id ticket25-off-001 \
  --log-root /home/cryang/log/k2
```

D 容器：

```bash
python ascend-mempool-test/scripts/run_mempool_min.py \
  --role decode --mode page-first-acceptance \
  --config /path/to/ticket25-off.json --run-id ticket25-off-001 \
  --log-root /home/cryang/log/k2
```

报告在 `/home/cryang/log/k2/ticket25-off-001/{prefill,decode}/`。
这是关闭双写时的既有路径回归，不是25的完整验收。本次未实际执行。

## 功能源码阅读顺序

Ascend/NPU 专用（路径相对 `python/sglang/srt/`）：

1. `disaggregation/ascend/mempool_router_protocol.py`：开关、支持配置和物理容量。
2. `disaggregation/ascend/mempool_router.py`：提前预留、命中引用、无拷贝发布与账本。
3. `disaggregation/ascend/mempool_subrouter.py`：候选页分配、P/D路由安装及释放。
4. `disaggregation/ascend/mempool_router_transport.py`：启动配置和注册能力一致性。
5. `hardware_backend/npu/mempool/alive_pages.py`：P零页绑定仍保留版本与释放身份。
6. `hardware_backend/npu/mempool/manager.py`：持有路由；D生成段的页偏移。
7. `hardware_backend/npu/mempool/dual_write.py`：固定路由表、两目标写任务与候选计数。
8. `hardware_backend/npu/mempool/runtime.py`：写入接线、完成计数、保活和读源报告。
9. `hardware_backend/npu/mempool/copy.py`：prefix多段与D本地的融合物化寻址。
10. `hardware_backend/npu/mempool/prefix.py`：候选预留/发布与源内容摘要。
11. `hardware_backend/npu/mempool/observation_device.py`：按真实来源累计读取计数。

通用主线功能源码：无修改。

## 脚本、测试与文档

- `scripts/run_mempool_min.py`：物理D容量进入启动参数和内存预算。
- `tests/unit/test_dual_write_service.py`：上述三个公开服务边界用例。
- 本文件、`.scratch/k2/ticket25-implementation-plan.md`、本票与验证记录。
- `.scratch/mempool-min/target-alignment.md` 及生成HTML：代码/CPU/NPU状态分开记录。

## off 首轮反馈与修复（2026-11-07）

用户运行 ticket25-off-001，D fetch 因验收脚本访问已移除的
`SparseKVCopy.source_regions` 而失败。修正 `scripts/verify_fetch.py`，
用 `transfer.source_rows` 和 `source_table.ranges` 统计有效 P/D 行。
执行实际统计循环的 CPU 检查通过：左右边界正确，排除 cache/current HBM 和无效行。
未修改功能源码或放宽 Graph/数据检查。待第二轮 ticket25-off-002，票仍 open。

## 双写 on 功能 smoke（2026-11-07）

用户回传 ticket25-off-002 两轮各6请求通过，reader count归零。
随后明确授权 dual_write=true、observe=false。Ticket仍open，待on实机结果。
page-first-acceptance 在双写开启时只启动一次模型服务：组件copy/fetch，
六个ABCE/load/EOS请求，Graph，P零页/D扩容，双写行数与字节，
无fill，候选owner无拷贝发布，读源和引用/账本释放，进程及端口清理。
不跑restart或故障注入。报告dual-write-smoke.json，不代表完整验收。
性能、计算重叠、物理NUMA、独立远端可见性探针、完整DRAM读回均未覆盖。

本次仅修改验收脚本run_mempool_min、verify_mempool_min、verify_mempool_shared，
新增verify_dual_write及报告检查用例，无功能源码修改。
本地相关15项测试通过（一次导入失败补齐scripts搜索路径后通过），
CLI describe及页预算正反检查通过。未再次运行完整CPU套件或访问NPU。

两机从已有ticket25-off.json复制生成ticket25-on.json，设置：
`dual_write=true, observe=false, observe_trace=false, prefill_shared=true,
decode_shared=true, prefill_index_shared=true, prefix_shared=true,
prefix_admit_mode=owners, prefix_pages=5120/page_size`。
使用同一入口，31 role=prefill、32 role=decode，mode=page-first-acceptance，
config=/home/cryang/ticket25-on.json，run-id=ticket25-on-001，
log-root=/home/cryang/log/k2。先31后32，后续自动协调。
预期退出0且summary及dual-write-smoke status=passed；失败非零。
回传prefill报告及失败阶段日志，日志位于上述log-root/run-id下。
