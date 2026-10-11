# MF 启动问题：阶段记录

更新日期：2026-10-11。这些启动问题未单独建新ticket；它们与09的NUMA分配问题分阶段记录。
本文件保存会话取证和当前处置，供再次复现时使用。

## 当前处置

| 问题 | 最新状态 | 证据边界 |
| --- | --- | --- |
| Transfer Join失败/超时 | 用户2026-10-11确认已解决 | 最终原因、操作、MF版本未提供；不归因到某个提交或protocol切换 |
| TransferEngine URL parser失败 | 当前可访问机器未复现，暂停排查 | 未证明旧环境已修复，也未证明MF升级是原因 |

继续开发时两项均不作为阻塞。若复发，先确认失败阶段和部署版本，再恢复针对性取证。

## A：历史 Join 失败

用户在BIOS调整后启动双机TP16服务，曾启用 `ASCEND_MF_TRANSFER_PROTOCOL=device_rdma`。
日志包括 `GroupJoin ... signal_ret:-2004 op_ret:0`、`GroupOpBarrier ... -2013`、
`Join timeout ... elapsed:603s`。P退出后出现的D store连接中断是后续事件，不能直接当根因。
BM mapping ready或D Graph capture完成也不代表TransferEngine/control已全部ready。

阶段位置：scheduler初始化PD → KV manager → AscendTransferEngine.initialize →
MF创建trans entry / 初始化 / Join。BM是每TP一对P/D的池；原TransferEngine仍用于Index K等传输。

历史曾反馈unset protocol后正常，随后又报告普通PD失败；后者截图实际是下节的解析阶段失败，
不能视作同一Join故障的对照复现。最终以2026-10-11“join已解决”为最新状态。

旧版源码研究区分了内部group等待与外层join总超时；曾看到内部默认120秒、外层600秒，
新代码还存在不同默认值。`SGLANG_DISAGGREGATION_BOOTSTRAP_TIMEOUT`、mempool timeout、
MF group timeout不是同一参数。复发时按安装版确认，不能统一套用600秒。

## B：历史 URL parser 失败

组合：`cryang/pr/sparsity-pd-disagg` + `glm51dis.sh`，P/D均报错；
用户当时反馈`cryang/dev/mempool` + `glm51mempool.sh`能launch。
截图显示store为`tcp://61.28.30.27:24670`，protocol为`sdma`。

```text
smem_net_common.cpp:117 ExtractIpPortFromUrl: Assert parser != nullptr
smem_trans_entry_manager.cpp:36 CreateEntryByName: parse store url failed: -1
smem_trans_entry.cpp:48 Create: create trans entry failed, probably out of memory
smem_trans.cpp:68 smem_trans_create: create entity happen error.
pytransfer.cpp:76 Initialize: smem trans create failed.
```

已核对的源码事实：

- 失败发生在创建store连接及Join之前；`probably out of memory`是上层通用提示，不证明OOM。
- 当时两个SGLang分支的`disaggregation/ascend/transfer_engine.py`相同，直接传入store URL。
- `SocketAddressParserMgr::CreateParser(url)`会创建解析器、执行`ParseUrl`/`ResolveAddress`，
  不要求先注册。`smem_init()`没有注册parser动作；“缺少mf.initialize导致未注册”假设不成立于所读源码。
- mempool确实较早运行MF/BM初始化；它可能改变启动顺序，但尚无证据证明这就是成功原因。
- 本地旧MF checkout HEAD `004d9317`与gitcode MF HEAD `29fbd1ae`都使用真实头文件编译了小型C++探针。
  `tcp://61.28.30.27:24670`、`.28:24670`均解析成功；host前人为插入空格的负例返回NULL。
  这是Mac解析器组件验证，不是NPU安装版或完整故障复现；并未证明用户URL含空格。
- `29fbd1ae`仅补空host访问保护；`ecd6e0d8`修正listener按端口缓存取错IP的问题。
  两者均不能据此认定修复本次`ExtractIpPortFromUrl`失败。
- 截图行号/报错文字与本地较旧MF源码吻合，gitcode HEAD接口和日志已有变化；行号相同不足以确认安装commit。

原建议的下一步是实际TP worker调用engine.initialize之前采集URL的repr/hex、session_id、
device/role、Python MF/adapter路径、`/proc/self/maps`内`libmf_*`/`_pymf_*`/`libhcom`路径，
两种启动组合对照文件哈希。若参数与二进制一致，再进入C++解析失败处采样。
临时`[MF_INIT_DIAG]`代码只在会话中建议，没有写入生产代码；用户未回传该诊断结果。
当前未复现，故无需为此重新加载模型或自动加入常驻诊断。

## 环境与原始证据位置

- 旧机器：10.120.72.31/32；后续测试机器：61.28.30.27/28。机器及环境不能跨轮视作相同。
- 截图：`/Users/hibikid/Desktop/截屏2026-10-09 19.46.06.png`（左P右D，URL解析失败）。
- 其他Join截图及完整日志以旧会话用户上传内容为来源；没有把远端原始日志下载到本仓库。
- 本地MF研究路径：`../gitcode/memfabric_hybrid`（当时HEAD `29fbd1ae14e7862c2335dc643bdf8084f0253393`），
  `../memfabric_hybrid`（当时HEAD `004d9317289fe99bd6bf13def0500b3fa3795ccc`）。
- 相关源码：`src/smem/csrc/net/smem_net_common.cpp`、`src/util/csrc/mf_ipv4_validator.h`、
  `src/smem/csrc/smem_trans/smem_trans_entry_manager.cpp`、`smem_trans_entry.cpp`和Python transfer adapter。

本轮只保存结论，没有修改MF/SGLang生产实现，没有宣称已做远端复验。
