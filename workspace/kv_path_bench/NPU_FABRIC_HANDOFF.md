# A3 Fabric NPU 接收内存分配：调查交接

状态：2026-09-14，基于用户远端日志和代码的初步判断；尚无验证通过的 NPU 分配方案。公开接口是否足够、是否需要专用扩展或版本升级，都未确认。

## 目标与边界

让 `feasibility_check.py` 的 `L3->NPU staging->L1` 路径通过 small，再通过 max。网络接收目标必须实际位于 NPU HBM，不能改成 Host 中转后宣称直达成功。保留 BF16 MLA 的 61 层、128-token page、512 维压缩 KV + 64 维 RoPE；L1 保持两个分离缓冲区。历史可行性脚本使用有限暂存区循环复用（不作为性能测试要求）；max 是完整 128K L1、非连续 page 映射。

## 已知事实

- 安装脚本固定 `mooncake-transfer-engine-npu==0.3.12.post1`；远端实际包版本、CANN、驱动、torch_npu 版本仍需确认。
- master RPC / 管理端口为 19271 / 19273；Store 返回 S0，保持运行是预期行为。
- `setup()` 内部 1 GiB Host 缓冲区经 ADXL MallocMem 分配并注册成功。
- 外部 MooncakeHostMemAllocator Host 注册曾失败；提交 `75a5a56e2` 改用 `mooncake.store.BufferPool` 借用内部缓冲区后，客户端已走到 NPU 注册。但这本身不证明 Host 传输已通过。
- NPU 暂存由 `torch.empty(..., device="npu")` 分配；`register_buffer(ptr, size)` 返回 -600，底层 `aclrtMemRetainAllocationHandle` / `halMemRetainAllocationHandle` 失败，runtime result 507899，drvRetCode 11，日志 mem type 为 0。small 注册大小为 8994816 bytes。
- 不能从这份日志断定 Fabric 不支持 NPU；可能涉及分配方式、地址/粒度、运行时版本或驱动约束。

## 请调查并交付

1. 核对上述精确版本对 NPU 注册的要求：普通 torch_npu 缓存分配、VMM/可扩展段、ADXL device allocation，哪一种受支持；提供对应版本的官方源码或文档依据，不猜私有符号或结构体。
2. 优先寻找公开接口或已安装组件；确认 CPU/Host 与 NPU/HBM 内存类型，不能以 Host 分配器替代 NPU。
3. 实现最小 NPU 暂存分配与释放，能够作为 Torch NPU tensor 使用，支持现有切片、copy_、同步，生命周期覆盖传输和重排。先申请、注册 small 所需内存，再验证真实远端读和逐页内容。
4. 若受版本或未公开接口限制，明确缺少什么、能否通过已有公开配置解决，不虚构可行结论。不要默认重建整套环境。

## 代码与协作

关注 `feasibility_check.py` 中 `NPU staging allocation` / `NPU staging registration` 两处；Host 路径已改为先完成，`--host-only` 可单独验证它。不要覆盖 Host 修复，不扩展正式性能脚本。参考官方 Mooncake `v0.3.12.post1` 的 `mooncake-integration/store/store_py.cpp`、`mooncake-transfer-engine/src/transport/ascend_transport/` 和当前 CANN/ADXL 头文件。

远端由用户手动操作，命令不能复制，尽量封装脚本，结果使用短码。读取 `workspace/Agent.md`，不提交真实地址或凭据。失败日志为 `/tmp/a3-kv-feasibility-client-small.log`。Host 成功为 H0/H1，两条路径均通过为 P0/P1；F4 为 NPU 暂存分配或注册失败。物理 UB 链路仍需另外确认。
