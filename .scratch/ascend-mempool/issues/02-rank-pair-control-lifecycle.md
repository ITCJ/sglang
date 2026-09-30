# 02: 16 对 rank 的正常控制闭环

**What to build:** 启动固定 P_i/D_i 的 16 个独立 pool，复用现有 PD bootstrap/ZMQ，
用真实 GLM-5.1 server 的 shadow 双写走通 acquire、binding、ready、drain 与释放。
每个请求在一侧的全部 ranks acquired 后才能推进，控制所有权持续到 release confirmation。

**Parent:** [Ascend mempool spec](../spec.md)

**Blocked by:** [01: 双机 mempool KV view 与 Graph 验证](01-mempool-kv-view-graph.md).

**Status:** ready-for-agent

**State:** open

## ③ Implementation plan（2026-09-30 用户确认安排）

本节是③的当前实施入口，优先于较早的③任务安排。2026-09-30 已完成③代码与
Mac 检查，等待用户核对及两机 NPU gate。S1–S4 勾选仅表示代码/本地检查完成，
不表示硬件验收；完整服务及本票 Acceptance criteria 仍待④。
范围：backend mempool runtime、P/D temporary compact KV shadow 双写、Graph 所需
metadata、Mac 检查和代码核对。不含④的 tick、准入 gate、drain、配置、BM 启动和
真实服务运行。③提供 binding 安装/清除与写入完成接口，不依赖 D1/D4/D5 已实现。

用户确认两点：行推导先在 mempool 内复制、单独测试并留档，以后再考虑合并；
③整体完成后一起交付 NPU 测试，不单独提前交付 writer gate。两机 gate 驱动 runtime
本身，与 backend hook 共用写入路径，不能只测试底层 offloader。③通过仍需④才能
进行服务级验收，ticket02 保持 open。

### 已核对的实现基础

- 现有 `mempool/offload.py` 要求 values 为 `(inputs.rows, heads, dim)`，rows 在
  构造时固定；P eager prefill 的 chunk 行数变化，因此需要扩展。
- `sparsity_driven_kv_offload/manager.py::offload_v2`（当前约656–870行）在每层
  从设备张量推导请求、token position、valid，包含 decode、compact ragged prefill、
  graph 静态 prefill、MoE 尾部 padding。decode 还检查 `seq_lens != 1` 和
  `out_cache_loc >= 0`；原函数尚无直接单元测试保护。
- hook 位于 `AscendAttnBackend.forward_extend` / `forward_decode` 的
  `topk_indices is not None` 分支内、sparse 子分支之前。P 随后走原生
  `forward_sparse`，D 走 `forward_sparsity_driven_kv_offload`。demo 不启用
  MLAPO，此处传入 compact k/k_rope；writer 触发不依赖 save_kv_cache。
- Mac 的 `ascend-mempool-test/src/ascend_mempool/__init__.py` 将 runtime
  `hardware_backend/npu/mempool` 暴露为 `ascend_mempool`，新增模块可用 CPU torch
  测试。backend/attention 依赖 torch_npu，Mac 只做静态检查，hook 必须薄。
- P 脚本使用 `--disable-cuda-graph`；固定 Graph metadata 只要求 D，P 走 eager。

### S1：复制行推导并建立测试

- [x] 新增 `python/sglang/srt/hardware_backend/npu/mempool/rows.py`，纯函数输入
  普通字段/张量：forward mode、req_pool_indices、seq_lens、extend_seq_lens、
  extend_prefix_lens、CPU 侧长度、out_cache_loc 等；不依赖 ForwardBatch 类型。
- [x] 忠实移植 offload_v2 四种布局、有效性条件及错误检查，返回
  `(req_ids, token_pos, valid)`。保留 decode 的 `seq_lens != 1`、cache loc 检查。
- [x] 文件头标明来源函数/对应逻辑，并指向本 ticket 的“已知重复”记录。
- [x] 手算期望值测试四种布局和 padding，不仅用实现自身生成期望值。

### S2：扩展 writer

- [x] `MempoolKVOffload.write` 支持可变行数，接受行推导后得到的
  slots/positions/valid，保持零有效行也 launch。
- [x] P eager 支持实际 chunk 行数；D 使用固定 binding 表，capture/replay 实测待 S5。

### S3：backend mempool runtime

- [x] 新增 `mempool/runtime.py`，按 `layer_id - start_layer` 定位 per-layer offloader。
- [x] 固定地址设备 binding 表 `row_slot`、`row_prompt_len`，大小对应 req_to_token_pool
  行数；第0行作为 graph padding 保持 invalid（-1）。
- [x] 提供 bind/unbind，仅由 tick 批准后调用；③测试用假调用。约束为 bind 对应尚未
  入 batch 的请求、unbind 在 drain 后，操作行不属于任何在飞行 batch。以此作为
  不需要额外 WAR barrier 的设计前提，写成不变量检查，不将推理视为硬件证据。
- [x] `write_layer(layer_id, k, k_rope, forward_batch)`：拼接 compact KV → S1 行推导
  → binding/角色映射 → writer。P local position=pos，要求 pos<prompt_len；
  D local position=pos-prompt_len，要求 local>=0。未绑定行 invalid，仍 launch。
- [x] 同一 stream 写入，与 offload_v2 一致，避免新引入源 tensor 跨流生命周期问题。
  shadow 阶段接受写入位于关键路径，侧流优化留待后续。
- [x] forward 结束时记录完成事件，提供 tick 查询；runtime 不发送 READY/DONE。
- [x] host 侧记账用于首个无服务内 readback gate：每个 forward 每层恰好调用一次，
  总调用数等于本 rank 层数；按期望行数（P extend_seq_lens 之和、D 有效请求数）
  累计 per-slot 写入进度，KV_READY 前与 prompt_len 核对，不一致报错。
- [x] `assert_bound(reqs)`：真实请求必须有 binding，fake 通过既有标识跳过；④在
  run_batch 前调用。底层 invalid mask 不能成为真实请求未绑定时静默通过的理由。
- [x] 构造时防御性拒绝 MLAPO 同开；正式启动配置校验归④。

实现补充：eager/capture 检查 Python layer 覆盖；replay 不执行 Python hook，因此
`begin_forward(..., replay=True)` / `end_forward()` 必须由④在每次真实 replay 外调用。
设备计数按 layer/slot 累计有效行，forward 末尾 clone 快照并记录完成事件；
`poll_completed()` 在事件完成后核对 host 期望值，再更新 completed KV rows。
快照避免 overlap 下早一轮完成事实读到后一轮的计数；计数不代替实际数据 readback。
capture 只允许无 live binding、无 pending completion 的 dummy 状态。
bind/unbind 禁止 open forward，bind 更新在同一 scheduler stream 上提交，下一次
forward stream 等待最新安装事件；unbind 仍需调用方证明远端 drain。

### S4：薄 backend hook

- [x] 在上述 extend/decode 的 topk 分支中调用
  `self.mempool_runtime.write_layer(...)`，不以 save_kv_cache 控制写入。
- [x] `AscendAttnBackend` 提供 attach 接口，默认 runtime 为 None，实际 attach 归④。
- [x] 不修改 sparse manager 的行推导；mempool 不依赖该类的生命周期。
  每个新增类/函数提供简短功能介绍，核心逻辑放入 Mac 可测模块。

### S5：③完成后统一交付两机 NPU 测试包

- [x] 在 `ascend-mempool-test/scripts/` 增加 gate 脚本并更新 README，沿用 run_gate.sh
  风格和 `ALL_CHECKS_PASSED` 判据。gate 驱动 runtime.write_layer，使用合成 batch。
- [x] gate 实现 P 写/D 读的 ragged prefill、chunk 偏移、同 slot 改写和 padding/unbound。
  随后 D 写/P 读 decode capture/replay，改变 slot、prompt length 和位置；使用真实
  BM rank1/runtime D 角色规则，不在 P pool 模拟 decode 相对位置。
- [x] 两侧读端用01读取路径逐元素比较 owner 全部逻辑 KV，覆盖未改动 slot 的哨兵。
  测试脚本已交付；实际双机运行与 Graph/远端可见性证据尚未取得。
- [ ] 按 verification.md 先向用户核对实现、正常路径、ownership、同步/释放前提和
  代码位置，再交付具体双机命令。③与 writer gate 同轮交付，不提前单独验收 writer。
- [ ] 用户执行两机 writer gate，双方20条 checks、其中10条 decode replay，
  `ALL_CHECKS_PASSED` 且报告 `status=passed`；回传日志后核对验收。

### S6：记录与重复代码管理

- [x] ticket Comments 记录实际实现/检查结果；没运行的检查明确写“未运行”。
- [x] ③及两机 writer gate 合并为同一轮 NPU 测试，未验证里程碑不勾选。
- [x] 已知重复：`mempool/rows.py` 已复制自 offload_v2 行推导。修改 padding、
  `seq_lens != 1` 等逻辑时需核对并同步两处；在文件头、ticket Comments 和
  design.md ③一节留档。后续先补特征测试再抽共享纯函数，此次不改无测试保护的
  sparse manager。若发现原逻辑缺陷，先明确差异，不能无记录地令两份代码分歧。

### 检查与交付

按 S1–S4 顺序逐片红→绿，在 Mac CPU torch 环境测试。

- Mac：现有56项 CPU suite 加新增测试；按 ascend-mempool-test/pyproject.toml
  严格 mypy；Ruff F/UP037、format、isort、git diff --check。
- 新增覆盖：四种行布局、P chunk 偏移、D 首次 decode local position=0、
  M==S_D 边界、padding 不写、未绑定不写且记账不一致报错、bind/unbind 后表地址
  不变、可变行数、capture 全 invalid 仍调用 kernel。
- NPU：S5 双机 gate，由用户在③整体完成后手动运行；Graph writer 和远端可见性
  在此之前均没有硬件证据。完整 server 验收仍依赖④。
- 不执行 git add/commit，除非用户另行明确要求。

### 风险与实现核对点

- 复制行推导可能漂移；通过忠实移植、手算测试和留档管理。原 manager 依赖
  torch_npu，Mac 不直接导入对照两份函数。
- backend/attention 的动态路径需要 NPU 验证，Mac 静态检查不代表运行通过。
- bind/unbind 不触及在飞行行是调用方必须兑现的前提，不因同一 stream 写入自动成立。
- Graph replay 不重新执行 capture 时的 Python write_layer；host 记账和完成事件
  必须有每次真实 forward/replay 的调用边界接口，④负责服务接线，③测试显式驱动。
  host 期望行数不证明实际 BM 数值正确，仍须 S5 与后续服务 readback。


## Acceptance criteria

- [ ] `SGLANG_NPU_ENABLE_MEMPOOL=1` 要求同时开启 sparse KV offload，并校验 NPU、
  Ascend PD backend、P/D role、TP=16、PP=1 与 peer 配置；无效组合清晰报错。
  P 的 `PD_PREFILL_NATIVE` 状态不会因 host-offload 属性为 false 而错误禁用 mempool。
- [ ] `B_slots=16`；`S_P` / `S_D` 可通过 server args 配置，默认均为 16384。
  每个 P_i 启动 store，D_i 连接 `base_port+i`；全部 ranks 的 BM
  映射与固定 Graph buffer 在 D capture 前就绪；协议、role/rank、session、
  dtype/layout、容量与 stride 的 peer 兼容握手在首个请求准入前完成。
- [ ] 在已有 PD receive path 处理 tagged mempool 消息，保持每个 PULL socket 一个 receiver。
  接收线程解析并排队，由顺序一致的 scheduler 协调执行 acquire、collective 和 release。
- [ ] D 先独立 acquire，再请求 P acquire；每一侧 16 ranks 对同一请求取得相同 slot，
  P/D slot 可不同。P 全 ranks acquired 且收到匹配的 `BOUND_ACK` 后才允许 prefill。
- [ ] 用 shared bootstrap room、pool session/epoch、request attempt 和 slot generation
  确认 binding；正常重复消息幂等，persistent ownership 不依赖 transient sender/receiver。
- [ ] 真实 GLM-5.1 P/D server 保留现有 sparse PD 路径，同时把 temporary KV 写入 P/D
  mempool，走通 `ACQUIRE`、`ACQUIRED`、`BOUND_ACK`、`KV_READY`、`DONE`、`RELEASE_ACK`。
  P mempool slot 的 ownership 持续到 D drain/DONE，普通 handoff cleanup 不释放它。
- [ ] 先由用户确认无 mempool 读取的 shadow 服务运行，再添加独立 UniDexCopy readback
  校验实际 top-k KV。校验数据不作为 attention 输入；有效内容正确且 decode 输出正常。
- [ ] D 的最后一次读取排空后释放 D slot 并发 `DONE`；P 确认该 attempt 的 D drain
  和自己的写入完成后释放 P slot 并回复 `RELEASE_ACK`。可从日志确认先后条件与最终可用 slot。
- [ ] mempool 与 MLAPO 同开启动报错；P/D runtime 均从 attention backend 接入，
  不依赖 sparse manager 生命周期。覆盖 skip_topk 层，避免 forward 漏写/重复写。
- [ ] 保留 D1 snapshot all-gather/preflight/commit，空输入仍同步；所有 ownership
  变化由 tick 批准，一致 preflight 后意外部分 acquire 失败报错终止。
- [ ] P 在 finalize_bootstrap 副作用前检查 tick-approved binding，未就绪返回 False；
  optimistic prefill 关闭，其他入口不能绕过；hook 不自行 acquire/release。
- [ ] D 在原 metadata/staging 两条 poll 路径均应用联合 readiness；原 staging
  继续推进，原 transfer 完成事实独立提交 tick，不与最终放行形成循环等待。
  覆盖 READY 先到/后到和原 transfer failure，失败不得隐藏为 waiting。
- [ ] D4 独立 pending release 管理纳入 idle/leak/sleep 判断；同轮释放合并排空，
  不沿用旧 deferred release 超时强制 free。涵盖 delayed sampling 和零 decode。
- [ ] 基础 fatal fault 明确报错退出，不伪造 DONE 或复用未确认安全的 slot；
  完整故障注入矩阵留给07，不能推迟正常服务依赖的 fault 接线。
- [ ] 按[阶段交付流程](../verification.md)完成实现核对、测试脚本交付和用户 NPU 验收，
  在 `Comments` 中记录实际证据。

## Verification

Mac 上通过消息/操作边界检查合法状态推进、身份匹配与正常重复消息。
NPU 上由用户启动全部 16 对 rank，验证启动兼容性检查、正常 binding、Graph 写入后 drain
和 release acknowledgement；日志应关联 bootstrap room、attempt、P/D slot 与 generation。
02 包含可安全运行的 waiting/timeout/cancel/fatal 基础接线；容量压力和部分 acquire
故障注入的系统验证归06，active cancel/peer fault 完整矩阵归07。

## Comments

### 2026-09-30：③实现交付，等待用户核对与 NPU gate

Codex / GPT-6：按用户要求先提交原工作区内容，提交为 `6dab4b6258`
（Document Ascend mempool implementation plan and verification gates）。
其后③新增/修改保持 unstaged/untracked，没有再次 git add 或 commit。

- `mempool/rows.py` 独立纯张量行推导；`offload.py` 接受可变行数/显式 metadata，
  全 invalid 仍 launch。保留固定 inputs 的已有调用方式。
- `mempool/runtime.py` 提供 `KVWriteExpectation`、bind/unbind/assert_bound、
  begin/write/end/poll 与 writes_done/prompt_ready。固定 request-row 表映射到本侧 slot，
  P 写全 prompt position，D 写相对 decode position；本地完成与协议 ownership 分开。
- eager/capture 验证每层写入一次；replay 通过外部 forward 边界记账。设备有效行计数
  及事件后的快照核对避免全 invalid 静默通过，并覆盖 overlap 的完成顺序。
- backend 增加默认 None 的 runtime 和 attach 防御性校验，extend/decode topk 分支
  在现有 sparse 路径前 shadow 写入；不依赖 save_kv_cache，不改 sparse manager。
  ④仍负责 runtime 创建/attach、scheduler 边界调用、fake marker 和 ownership。
- 新增 `verify_writer.py`、`run_writer_gate.sh`、`writer_cases.py` 及 README。
  gate 与 hook 使用相同 runtime；P prompt 写/D 远端读后，D decode 写/P 远端读。
  这个双向安排替代原 S5 的固定单向机器职责，以实际验证 rank1 的 D 相对位置。
  每个24/48-core阶段都包含 eager、16-row decode capture/replay、rebind、边界和哨兵。
- 双向 gate 收到双方 `WRITER_GATE_DRAINED` 后才关闭 pool；失败缺少 peer drain 时
  保留存储，Ctrl+C 不授权双向 retained pool 的 BM close。01单向 gate 保留原流程。
- 已知重复：rows.py 有意复制 offload_v2，来源/同步维护要求已在文件头、此票和
  design.md 记录；空 batch 保留静态 source extent 为全 invalid，额外 shape 检查
  使 malformed 输入直接报错。后续共享抽取需先补原 manager 特征测试。

Mac 实际执行：完整 CPU suite **77项通过**；严格 mypy 检查 **16个源码文件通过**；
Ruff F/UP037、format、isort、git diff whitespace、两份 gate runner 的 bash syntax 检查通过；
writer `--describe --kv-dim 576` 通过（P/D 各贡献1 GiB，stride=1 GiB）。
backend 的 import/runtime 路径受 torch_npu 限制，Mac 只做静态检查。
新增21项测试覆盖四种布局、可变行数、chunk/local position、容量边界、绑定地址、
安装事件等待、漏/重复layer、计数错误、capture live-binding 拒绝及完整 gate CPU参考。
CPU测试使用 fake SDK/kernel/event 边界，不是实际 BM/NPU 执行。

Standards review 的事件边界/测试 fixture 可读性/嵌套函数介绍已修正；
Spec review 的 live-binding capture 漏记完成和双向失败时 D 提前关闭缺陷已修正。
两项 review 最后定向复核均无剩余发现；Spec review 的 CPU 模拟也确认双向 callback
失败/timeout 时不关闭 BM，以及双向 retention 不因 Ctrl+C 执行 close。
实际 NPU Graph、远端可见性、真实 GLM5.1 server：**未运行**。
③代码与 gate 同轮交付；用户核对、两机 writer gate、④以及服务 readback 都待完成，
ticket02 保持 open，不解锁依赖本票硬件验收的工作。

2026-09-29：用户确认②代码review无疑问并授权commit，已提交
`8d9bdd75b2`（Add Ascend mempool PD control and safe slot retirement），共9个文件，
包括控制实现、conn接入入口、本地测试、README及pool ID修正。本轮未修改代码，
沿用上一轮56项Mac测试与类型/lint结果，提交前staged whitespace检查通过。
用户提供的Claude复核意见已阅读，TP tick/drain/fault及阶段拆分仍需后续设计确认；
②代码核对通过不等于02整票硬件验收完成，ticket保持open，③④待接线。

2026-09-28，Codex / GPT-6，用户确认后的retirement历史修改（未stage/commit）：

- RELEASE_ACK统一确认精确allocation已不再占用P资源，覆盖DONE及安全rollback。
- `_release_slot`在实际归还ownership时更新每slot的`_retired_generation`；
  P/D使用同一退休边界机制，保留session、签名proof、generation和owner校验。
- 移除永久`_released_proofs`集合、`max_release_history`参数及累计65,536次限制。
  近期request records继续受`max_records`约束；仅按slot保存长期退休边界。
- 旧rollback的DONE在CANCELLED记录保留/回收后均返回一致ACK，迟到BOUND_ACK不复活该请求。
  普通unbound CANCEL仍无额外ACK往返，bound取消仍须drain/DONE及P写入排空。
- Mac完整56项测试通过，覆盖65,537次连续释放、有界records、旧rollback与新owner隔离、
  未退休allocation和伪造request/session/slot/proof拒绝。严格mypy检查7个源码文件通过，
  指定Ruff规则、format和diff whitespace检查通过。没有执行NPU测试。
- 同步spec、review和独立测试README；桌面`ascend-mempool-request-lifecycle.md`已更新。
  C4已修复。以下较早记录中“ACK待确认/65,536上限”均为历史状态。③④及硬件验收仍待完成。

2026-09-28，Codex / GPT-6，review缺陷修复（未stage/commit）：

- `_cancel_record`保留DRAINING，重复本地/远端CANCEL不会撤销drain。
- 取消、排空、等待释放ACK或CLOSED时，匹配binding的迟到KV_READY安全忽略；
  不复活请求，冲突slot generation仍拒绝。
- `acquire_decode`在reserve前校验长度类型/容量和reply endpoint，错误输入不占slot/room。
- P收到DONE但BM writes尚未结束时进入CANCELLING，继续保留slot直到写入排空；
  不再把已结束请求对外表示为正常PREFILLING。
- BM startup与PoolPeer统一支持0..255，包含已实测gate的101/102；
  范围依据本地MF release/1.1的TransferEngine entity从256开始，不宣称这是SDK通用上限。

Mac回归先复现原缺陷，再验证修复：完整CPU suite为56项通过，
包含重复CANCEL、迟到KV_READY与新slot owner隔离、非法输入无占用副作用、
pending-write DONE和pool ID边界。严格mypy按`ascend-mempool-test/pyproject.toml`
检查7个源码文件通过；Ruff按仓库pre-commit的F401/F821/UP037规则及format检查通过。
首次未指定配置的广泛Ruff/隔离mypy调用报告旧规则问题与跳过依赖导致的Any返回；
改用仓库/本任务已有检查配置后通过，未为这些无关检查修改代码。

用户明确本轮仅进行Mac回归，不要求NPU测试。未启动NPU/MF/server；ticket保持open。
C4累计release历史上限待用户确认ACK语义；③④接线及其硬件验收仍待完成。

2026-09-28 review: ①存储与②控制已经进行只读复核，现有51项CPU测试通过，
但额外复现发现重复CANCEL导致DRAINING倒退、取消后的迟到KV_READY报错、
无效acquire输入留下slot占用；另有累计release历史上限。
详见[整体方案与代码复核](../design-review-2026-09-28.md)。本轮没有修改实现；
③④接线前应处理这些缺陷并确认drain、故障策略及历史回收合同。Ticket保持open。

任务已建立；第一部分 storage 已实现，其余集成与 NPU 验收尚未完成。

2026-09-27：依赖的 [01](01-mempool-kv-view-graph.md) 已由用户确认验收并关闭；
本 ticket 的 blocker 已解除，可按 `/implement 02` 开始开发。

### 2026-09-27: confirmed delivery scope

- 用户确认按四部分实施：NPU `mempool/` storage；Ascend 控制；attention/稀疏 manager
  双写；conn/scheduler/环境变量与参数接入。
- 第一轮使用真实 GLM-5.1 server，保留原有 main KV transfer、D staging/hostSHM 和
  attention 消费路径。P/D 额外写入 BM；P native HBM cache 保留以服务 prefill。
- 测试可设置 `S_P=S_D=8192` 与较小 context，降低额外 DRAM 需求；产品容量默认仍为
  16384。Graph capture/warmup 的未绑定行保持 invalid，不能占用/访问真实 slot。
- 先交付选项 1 的 shadow 服务运行，由用户在 NPU 验证；随后加入选项 2 的 mempool
  top-k readback 比对，完成整个 02。01 的 fetch Graph 证据不能替代新写入路径的验证。
- 第一部分（Codex / GPT-6）：迁入 01 的 layout 与 BM manager/view，增加 model-derived
  `MempoolConfig` 和 `MempoolKVOffload`。尚未接入 server，不代表 02 控制闭环完成。
  Mac 实际检查：完整 mempool CPU suite 18 项通过；严格 mypy 检查 11 个源码文件通过；
  ruff lint/format、isort、shell syntax 与 diff whitespace 检查通过。默认/不等容量
  `--describe` 通过，独立 import 确认为 runtime 文件且不初始化 SGLang/Torch/MF。
  Standards review / Spec review 均无发现；代码提交为 `a02bdc234e`
  (`Add Ascend mempool storage and graph-compatible KV offload`)，branch 为
  `cryang/dev/mempool`。本地 spec/ticket 保留在 `.scratch/ascend-mempool/`。
  NPU raw-destination offload、服务 capture/replay、shadow 请求和 readback 尚未执行。

### 2026-09-28: rank-pair BM startup helper (unstaged)

按用户确认，在 `MempoolKVManager.initialize_rank_pair()` 内实现 P_i 启动
`base_port+i` store、D_i 连接该 store。两侧分别取 BM rank 0/1，world_size=2；
方法初始化 BM 并 join/验证映射，失败时清理本次上下文。`mf.initialize()` 仍由服务
生命周期拥有，避免 manager 在 TransferEngine 活跃时关闭共享 MF。当前调用方/服务接入
留到第④部分，`POOL_HELLO/READY` 身份校验留到第②部分。
新增 CPU SDK boundary 测试覆盖 16 对 URL/rank 与失败路径；用户要求此次修改保持
unstaged，明确指示后才执行 `git add`。本次无 NPU 执行结果。
复核 MF 1.1 源码发现，BM 尚未初始化时 `bm_rank_id()` 可能返回默认 rank 0，
因此不能用于预检查。已改为 manager 内部跟踪自身创建的活动 BM context，
保留初始化后的 rank 校验；外部 BM context 须由第④部分的启动顺序保证不存在。
Mac 实际检查：新增 5 个 startup 测试及完整 CPU suite 共 23 项通过；严格 mypy
11 个源码文件通过；Ruff lint/format、isort、`git diff --check` 通过。
真实 MF 1.1 的 16 对启动、服务接入和 Graph 路径尚未执行，等待后续阶段的用户 NPU 验收。

### 2026-09-28: KV element width follows declared dtype (unstaged)

Codex / GPT-6：按用户指出的布局可读性问题，`KVLayout` 使用声明的 `dtype` 推导
`element_bytes`，`row_bytes` 与 `byte_offset` 共享该宽度；当前仍仅支持 BF16，
运行时写入继续校验实际 tensor 为 BF16。Mac 实际检查：完整 CPU suite 23 项通过、
严格 mypy 5 个 mempool 源码文件通过、Ruff lint/format 与 `git diff --check` 通过。
没有执行 `git add`，NPU 验证状态不变。

### 2026-09-28: Ascend PD control protocol, phase ② (unstaged)

Codex / GPT-6：新增 `mempool_protocol.py` 的 tagged multipart 消息和严格 schema、
P/D peer 兼容检查；新增 `mempool_control.py` 的单 rank persistent slot/binding 状态、
`KV_READY` 与原 transfer success 双条件、D drain -> `DONE` -> P `RELEASE_ACK`。
`AscendKVManager` 复用现有 PD PULL/PUSH socket；接收线程只排队控制事件，
scheduler 后续处理状态转换。早于 control attach 到达的帧由有界 router 暂存，
坏帧使 mempool 控制进入明确 fault，普通 PD 接收线程继续运行。
取消逻辑区分 D 已绑定与未绑定，P 不会因 `BOUND_ACK` 迟到而提前复用 prompt slot。
近期终态记录有界保留；D slot generation 拒绝回收记录后的迟到 `ACQUIRE`。
P/D slot proof 绑定 request 与两个 lease，结合已释放 generation 校验迟到的
`DONE`/`RELEASE_ACK` 等消息，不触碰新 slot owner。P 还只在实际 `DONE` 释放后
保存精确 release proof；此前经 unbound `CANCEL` 回滚的 binding 不能借后续
slot generation 获得假 `RELEASE_ACK`。release proof 历史上限为 65536，
达到上限时拒绝新 P slot acquire，后续需设计安全的 session rollover/回收。
协议 fault 会阻止新的 P prefill/D decode 工作。

Mac 实际检查：28 项控制单元测试通过；使用隔离 CPU PyTorch 环境运行完整 suite
共 51 项通过。新增两个 runtime 模块严格 mypy、Ruff 与 `git diff --check` 通过。
`conn.py` 在隔离 mypy 配置下仍有 17 项原有的外部模块/旧代码类型错误，
此次新增的 `no-any-return` 已修复。无 NPU 硬件执行。
② 尚无 scheduler/server 接线，③ 双写和④接入完成前，
不能进行真实 16 对 rank 的控制闭环与 Graph 服务验收。未 `git add` 或提交。

### 2026-09-29：D1 设计确认

用户确认同侧统一 acquire/release、一致 preflight 后意外部分 acquire 失败采用
fail-stop，以及首版每 scheduler tick 的 CPU 元数据同步成本。tick 推进持久状态，
不是定时重置。已更新父 spec 和 [接线设计](../d1-d4-d5-design.md)。
D4 drain 和 D5 fault 的具体接入仍待确认；本次仅修改文档，未运行测试，未修改实现。
此决定取代早期“partial acquire rollback 后继续等待”的方案；普通 cancel 的安全
rollback 保留。后续容量/取消 ticket 应遵循父 spec 的新合同。

### 2026-09-29：D4/D5 策略确认

用户接受 D4 首版在 D 侧统一暂停新 batch 提交、排空 overlap 和设备工作、释放结束
请求后恢复其余请求；D5 不可恢复错误直接报错终止，不做同进程恢复。
已同步父 spec 和接线设计。报错不等于 DONE，不允许据此复用未确认安全的 P slot
或销毁 BM。具体 flush、故障传播/timeout 与退出 hooks 仍待③④实现及验证。
本次只更新文档，未修改运行代码或运行测试；未 add/commit。

### 2026-09-29：③④ shadow 接线方案更新

P/D mempool runtime 均从 attention backend 接入，不依赖 sparse manager 的生命周期。
参考现有 sparse UniDexCopy 的设备索引和 Graph 调用，适配 BM slot/position。
③ backend 接线单独完成不等于真实服务 gate；目标 gate 需要④必要启动/control/tick/
drain 接线共同完成，再由用户执行 NPU 验证。服务 readback 仍在首轮运行确认后添加。
详见 [当前实现方案与 review 检查点](../design.md)；已同步父 spec。
本次仅文档更新，没有代码变更、测试执行或 add/commit。

### 2026-09-30：准入 gate 确认与子里程碑

P 在 finalize_bootstrap 副作用前检查 tick 批准结果；D 联合 readiness 覆盖 metadata
和 staging，原 transfer 完成事实独立提交 tick。D1 保留原方案，采纳 D4 idle/pending
release 补充及 MLAPO 禁用约束。父 spec、design、D1/D4/D5 与03/04/07同步更新。
容量由用户 launch 时设置；除用户询问或出现明显相关问题外不反复提醒。

- [x] ① layout/BM manager/view/writer 基础代码已提交（不代表 raw writer NPU 验收）。
- [x] ② 单 rank 协议/control 与 conn 接口已提交（不代表16对服务闭环完成）。
- [ ] ③ backend runtime、P/D temporary KV 双写与 Graph metadata；Mac 检查及代码核对；
  ③整体完成后与下一项 writer gate 同轮交付 NPU 测试。
- [ ] 两机 runtime writer gate：P 写/D 远端读、同 slot 内容更新、capture/replay 内容验证；
  与③同轮 NPU 测试，不单独提前交付。
- [ ] ④ 配置/BM startup/conn/tick/准入/drain/fault 接线；16对启动与绑定。
- [ ] 用户确认无服务内 readback 的真实 GLM5.1 shadow warmup/capture/replay/请求与释放。
- [ ] 加入 top-k 独立 BM readback 对照；用户 NPU 验收并记录证据，才可关闭02。

本次仅更新文档，未修改运行代码、执行测试或 add/commit。

### 2026-09-30：③ S1–S6 实施安排（用户提供）

已将用户安排置于本票前部作为③实施入口：行推导复制到 mempool/rows.py 独立测试，
不改 sparse manager；③整体完成后一次交付 runtime 双机 NPU gate。
已知重复需在两处行推导修改时同步核对，后续补特征测试后再考虑共享抽取。
本次只更新 ticket 和 design 留档，尚未实现③；Mac/NPU 检查均未运行，未 add/commit。
