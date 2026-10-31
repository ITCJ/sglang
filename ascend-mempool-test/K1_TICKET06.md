# K1-ticket06：D alive 主 KV 共享与每步顺序

用户已确认NPU001精简功能验收，06已关闭。以下专用验收命令对应
`9b760a1addddec05ce2a01d2f2a554a7abe33f67`，仅用于在独立checkout复现历史验收。
清理后的版本已删除`d-sharing-acceptance`、`verify_decode`及工程哨兵/延迟探针。
[人工确认和证据边界](../.scratch/k1/verification/k1-ticket06-001-20261031/acceptance.md)。

当前正常服务使用`serve`，配置`decode_shared: true`；
通用`session-acceptance`也读取此配置，覆盖现有功能回归，报告标明不含D顺序探针。
它不能替代下面旧版本的逐步哨兵/负测试。
旧报告可用独立脚本`verify_mempool_decode.py`离线核对；该脚本不在worker中运行。

P、D各一台TP16，GLM-5.2，page128，P eager、D Graph。
本入口默认开启P/D共享：两侧主KV各8份；Index K和prefix保持既有布局。
同组D成员按特征维分写，当前token从本rank的HBM供给，历史token读取共享DRAM。

## 历史验收：每机一条命令

两机使用相同提交、权重和配置，复用前票的CANN、ATB、MemFabric环境。
配置可以沿用`/home/cryang/mempool-min-ticket01-3132.json`。
新环境使用`configs/mempool-min.example.json`，填入两机IP、各机权重路径和网卡。
先在P启动，再在D启动；后续阶段自动协调。

```bash
# P容器，仓库根目录
bash ascend-mempool-test/scripts/run_mempool_min.sh \
  --role prefill --mode d-sharing-acceptance \
  --config /home/cryang/mempool-min-ticket01-3132.json --run-id k1-ticket06-001
```

```bash
# D容器，仓库根目录
bash ascend-mempool-test/scripts/run_mempool_min.sh \
  --role decode --mode d-sharing-acceptance \
  --config /home/cryang/mempool-min-ticket01-3132.json --run-id k1-ticket06-001
```

两机run-id相同；失败后保留旧目录并使用新run-id。
加`--describe`只预览配置、容量与执行阶段。
BM分配预算为P120GiB、D8GiB；P的CPU源快照预算另计32GiB。
模型、原生HBM、设备验证缓冲及驱动虚拟地址预留不计入BM数字。

## 配置与生产路径

`serve`配置中的`decode_shared: true`开启D共享，可独立于P共享使用。
`decode_groups`指定组内成员顺序，首成员为owner；默认`{0,1}`至`{14,15}`。
同组成员必须在同一NUMA；P/D同时共享时使用相同会话分组。
`decode_write_assignment`支持`features`（默认）或`layers`。
前者均分576维，后者按本地层号轮转；其他成员仍保存本rank的完整当前KV。
旧验收版本的`verify_decode`只在验收时启用逐步读回和启动负测试。
清理后的配置不再包含此字段。

生产写回是当前计算流上的`unidex_copy`，可随decode graph捕获。
写回排在同层attention输出后的全员TP集合通信之前。
当前token的miss由本地HBM补齐，避免读取其他成员尚未写完的半行。
已有miss stream等待`copy_ready`，attention/refill等待原有完成事件。
请求释放沿用Main/SR，服务退出沿用SGLang父进程清理路径。

## 两轮自动检查

入口先检查双端版本、配置、依赖与容量，并运行一次复制smoke。
每次服务启动后，D在正式请求接入前运行Graph顺序探针：
最后一个写者在单独流上排入矩阵乘延迟，再提交实际分工写入。
同一依赖结构分别关闭、打开写回event等待。
关闭等待必须观察到早读错误，打开等待必须读到完整行；否则验收失败。
该探针使用末槽临时行，最终等待全部操作完成；不增加性能计时。

1. 运行A/B/C/E、长生成和提前EOS，随后执行超时fail-stop与双端清理。
2. 使用相同端口重启，重复功能请求，核对释放复用及端口释放。

只清理本入口创建的进程组；失败返回非零，不自动增加第三轮。
逐步哨兵在每层下一步写入前读回上一token整行，与本rank保留的HBM比较。
第1步必定核对`prompt_len`位置，不依赖top-k是否选中该token。
错误在设备完成后触发fail-stop，正常完成报告包含检查行数和实际分工写入字节数。

## 覆盖与回传

入口标为 **K1-06 reduced functional acceptance**。
保留前票的精简边界：V3使用独立题目答案；V8在CPU重放真实注册的容量拒绝。
prefix使用计算源快照与恢复后的native HBM核对，不额外读取prefix DRAM。
D侧逐步哨兵会读回真实共享DRAM，单独记录，不计成模型cache miss读取。
NUMA实际页驻留、性能基线不由本轮通过声明替代。
性能工作仍在baselinetest分支安排。

日志：`/home/cryang/log/mempool-min/k1-ticket06-001/{prefill,decode}/`。
回传P的`summary.json`、`d-sharing-acceptance.json`及两机终端结果。
失败时保留对应阶段的P/D日志。
`target/`与`restart/`中包含注册、请求、账本、内容、容量和清理证据。
注册的`shared_probe.decode_order`保存Graph负测试与正测试结果。
decode日志中的`decode_completion.shared_decode`保存每请求逐步检查和写字节数。
原报告中的`user_accepted: false`保留，用户确认另行记录；06现已closed。
