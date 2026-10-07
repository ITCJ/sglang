# Ticket03 S6：最终审查发现整改

日期：2026-10-07。用户授权处理STD-F01、SPEC-F01和T-01/T-02。
整改基线为`e838860ed0e4bfef540087d5cdd5ebbd1bcee992`；原始发现保留在
[固定版本的全量review](ticket-03-s6-final-review.md)。本次是该报告的整改记录，
不改变原报告的审查版本和历史结论。Ticket03仍为open，等待用户执行最终版本NPU验收。

代码提交：`90bcb1d805`处理STD-F01；`2de91045d9`处理SPEC-F01及T-01。
T-02及本次审查/交付记录随后单独提交。两次代码提交合起来对应下面的测试与复查结果。

## 修改结果

| 发现 | 处理 | 验证重点 |
| --- | --- | --- |
| STD-F01 | 9个生产文件的26个dataclass及两个standalone用例容器改为msgspec.Struct | 构造校验、不可变快照、身份/hash、隔离准备、协议及布局序列化 |
| SPEC-F01 | 删除普通staging与mempool并存的shadow分支及可选release参数；迁移测试 | 普通staging成功/失败释放；正式取消等待所有发送端ACK和本地drain |
| T-01 | conn.py第三方/项目import之间补空行 | 仓库isort检查 |
| T-02 | NUMA历史HTML清理128处行尾空白 | 从完整开发基线开始的git diff --check |

`mempool_protocol/control/tick/service.py`和NPU `mempool/`中的容器保持原有可变或
冻结属性。请求身份仍按字段比较并可作为字典key；`KVRowBinding`保留`eq=False`，
同坐标的新对象仍不能冒充已批准的attachment。`_Forward`的集合逐实例创建，
control准备仍复制当前record，正式提交前不会修改真实记录。

迁移时保留两个容易遗漏的语义：`msgspec.structs.replace()`不运行`__post_init__`，
因此`MempoolConfig.make_mla_layout()`分别调用P/D布局构造器，继续检查两侧容量。
`msgspec.structs.asdict()`只转换一层，因此协议编码及`PoolLayout.signature()`使用
`msgspec.to_builtins()`递归转换。v2协议仍用原JSON字段、排序和multipart tag；测试
按迁移前的完整握手字节核对，保留decoder的严格字段和构造校验。

`AscendKVManager.update_status()`和`AscendKVReceiver.abort()`中的staging逻辑现在
只承担普通sparse PD的清理。`SparsePDDecodeStagingPool.offload_room_to_host()`始终
在finally中释放staging slot。正式mempool不创建该pool；其Index K/aux目的资源仍由
service控制释放，`_send_abort_notification()`继续提前建立ACK计数。

迁移后的正式取消测试没有staging对象，使用真实Ascend receiver方法和common
ACK tracker：本地host/device drain各一次，一位发送端的重复ACK不足以释放；重复
ABORT通知不会重置已收到的ACK，所有发送端完成后native资源只释放一次。
普通staging测试使用`mempool_control=None`，覆盖正常offload、offload失败和abort。

## Mac实际验证

- 针对性回归：容器/协议/生命周期等78项通过；传输、service及资源等53项通过。
- 最终独立CPU suite：205项通过，4.608秒。故障注入日志是预期负向用例。
- 严格mypy：33个源文件通过。
- 从`4878a495d8`至当前工作区的全部增量：79个Python文件AST/format、按各自配置的
  Ruff/isort、4个shell脚本语法及全量diff空白检查通过。
- 日志：`/private/tmp/ticket03-final-fixes-unit.log`、
  `/private/tmp/ticket03-final-fixes-mypy.log`、`/private/tmp/ticket03-final-fixes-static.json`。

本机为Mac/Python3.9.6，msgspec0.20.0；未执行完整SGLang registered suite或NPU测试。
此前回传的正式checker、curl及三轮性能仍是历史证据，不标成本次修改的硬件通过。

## 整改复查

基于`e838860ed0`至本次修改，由两个独立reviewer按Standards和Spec分别复查。
原全量报告的规划函数维护提示仍为非阻塞建议，本轮没有扩展其重构范围。

### Standards

无新增违反。26个生产容器及两个standalone容器符合贡献指南的Struct要求；冻结、可变
记录、默认工厂及binding身份属性保留。共用native receiver fixture避免重复ACK搭建。
T-01/T-02由工具检查确认，不计入人工Standards发现数。

### Spec

无缺失、越界或实现错误发现。确认构造校验、协议字节、递归布局、prepare隔离保留；
不可达shadow组合已清理，正式abort/ACK和普通staging释放覆盖均在相应模式执行。
reviewer另核对值比较/hash/pickle、binding身份及独立可变默认集合。

Standards：0项新增发现；Spec：0项新增发现。硬件验收状态仍为待用户执行。

## 最终版本NPU复验

沿用已有S6.5验收，不扩大到NUMA/长上下文问题。两端更新到相同提交，使用原Python3.11、
torch_npu、sgl_kernel_npu、MemFabric和CANN环境；保留两侧`git rev-parse HEAD`、
`git status --short`及`python3 -c 'import msgspec; print(msgspec.__version__)'`输出。
所有命令从仓库根目录执行，先停止使用目标NPU的旧gate或服务。

1. 先P后D运行[正式服务说明第1节](../../ascend-mempool-test/FORMAL_SERVICE.md#1-连续异步-replay-组件-gate)
   的双机`verify_fetch.py`命令；P等待listener/BM时就启动D。应有两端
   `ALL_CHECKS_PASSED`、D侧30个`FETCH_PASS`及正常退出。命令中的IP替换为实际两机地址。
2. 本轮删除了普通staging的可选释放入口，在任意一台空闲device0上复跑资源gate：

   ```bash
   bash -o pipefail <<'SH'
   set -eu
   export PYTHONPATH="$PWD/python${PYTHONPATH:+:$PYTHONPATH}"
   mkdir -p /tmp/ticket03-s6-final-resources
   git rev-parse HEAD > /tmp/ticket03-s6-final-resources/version.txt
   for mode in pd_prefill_mempool pd_decode_mempool local_offload pd_decode_offload; do
     python3 -u ascend-mempool-test/scripts/verify_resources.py \
       --mode "$mode" --device-id 0 \
       --report "/tmp/ticket03-s6-final-resources/${mode}.json" \
       2>&1 | tee "/tmp/ticket03-s6-final-resources/${mode}.log"
   done
   SH
   ```

   应有4条`RESOURCE_PASS`、4份`success=true`报告及全部退出0。
3. 依照[正式服务说明第2–5节](../../ascend-mempool-test/FORMAL_SERVICE.md#2-启动正式服务)
   重启P、D及router，继续context1024、P/D各512、TP16、D Graph width16、NUMA
   `0,2,4,6`。新日志执行zero/decode/reuse及service checker，预期1/32/32 tokens、
   `FORMAL_SERVICE_PASSED`及全rank释放。另核对关闭thinking后的小题目完整回答与原负载性能。
   新LOG_DIR与checker路径保持一致，不将本次日志混入此前验收目录。
4. 服务drain后停止P/D/router，用同一启动脚本的`prefill native`、`decode native`
   重启普通模式，再启动router并核对输出、Graph、原main-KV传输和staging/host路径。
   普通模式不使用只接受正式资源合同的service checker。
5. 在完整SGLang环境执行既有registered回归：

   ```bash
   PYTHONPATH=python python3 -B -m unittest discover -s test/registered/unit/npu -p test_sparsity_driven_kv_offload_config.py -v
   PYTHONPATH=python python3 -B -m unittest discover -s test/registered/unit/model_executor -p test_hisparse_pool_configurator.py -v
   ```

   两项suite应全部通过；回传版本、各gate JSON/log、P/D日志、HTTP响应、性能原始数据及用户确认。

任意gate非零退出、断言失败、缺rank、资源未释放或输出异常均保留完整日志后定位。
CPU中的取消/ACK回归不等于新取得了真实NPU取消验收；本轮不扩展后续ticket的故障矩阵。
