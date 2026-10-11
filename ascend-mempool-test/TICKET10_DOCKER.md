# Ticket10：容器内自动验收

两台机器各在现有 NPU 容器中执行一次命令。P 负责请求与汇总，D 自动配合，
不需要容器间 SSH。容器沿用已跑通模型的镜像、设备和权重挂载，使用 host 网络，
并将 `/home/cryang/log` 挂载到宿主机以保留报告。

## 前置条件

- 两侧各16张NPU，使用同一SGLang提交、Python依赖和模型/tokenizer文件；代码工作区干净。
- 停止此前的模型、router、gate，释放测试NPU和端口。脚本只停止自己启动的进程组。
- 网络开放已有服务/MF端口；新增协调端口为P的TCP18890，可用 `--control-port` 修改。
- wrapper自动加载 `/usr/local/Ascend/ascend-toolkit/set_env.sh` 和
  `/usr/local/Ascend/nnal/atb/set_env.sh`。环境布局不同可自行source后直接运行同名 `.py`。
- 需要 `python3`、`bash`、`git`、`ps`、`npu-smi` 及已有SGLang/NPU/router依赖。

## 每机一条命令

先在两侧同步 `cryang/dev/mempool`，进入 `/home/cryang/sglang`。
示例IP及bond4按实际修改；两侧 `--run-id` 必须相同，每次重测换新值，已有目录会被拒绝。

P容器先执行：

```bash
cd /home/cryang/sglang
HCCL_SOCKET_IFNAME=bond4 GLOO_SOCKET_IFNAME=bond4 \
  bash ascend-mempool-test/scripts/run_ticket10.sh \
  --role prefill --prefill-host 10.120.72.31 --decode-host 10.120.72.32 \
  --run-id glm52-r1 --model-path /data_lib/data/models/GLM-5.2-w4a8
```

D容器随即执行：

```bash
cd /home/cryang/sglang
HCCL_SOCKET_IFNAME=bond4 GLOO_SOCKET_IFNAME=bond4 \
  bash ascend-mempool-test/scripts/run_ticket10.sh \
  --role decode --prefill-host 10.120.72.31 --decode-host 10.120.72.32 \
  --run-id glm52-r1 --model-path /data_lib/data/models/GLM-5.2-w4a8
```

也可在各宿主机用 `docker exec` 进入已有容器执行上述命令，例如：

```bash
docker exec -it -w /home/cryang/sglang \
  -e HCCL_SOCKET_IFNAME=bond4 -e GLOO_SOCKET_IFNAME=bond4 \
  <容器名> bash ascend-mempool-test/scripts/run_ticket10.sh \
  --role prefill --prefill-host 10.120.72.31 --decode-host 10.120.72.32 \
  --run-id glm52-r1 --model-path /data_lib/data/models/GLM-5.2-w4a8
```

D宿主机使用自己的容器名及 `--role decode`。脚本不创建容器、不安装依赖、不修改sysctl。

## 自动执行内容

1. 比对SHA、依赖版本、模型配置及tokenizer摘要，校验GLM-5.2的78层/21producer/2048topk。
   摘要不扫描全部权重分片；仍需确认两机挂载相同权重。
2. Index K-only transfer组件，以及78层独立KV数值/Graph组件。
3. 固定chat输入；依次执行native/eager、native/graph、formal/eager、formal/graph。
   自动启动P/D、等待ready、启动router、发送10个请求。
4. formal两组自动收集D日志，等待生命周期checker通过，再停止本组服务。
   对照三组与native/eager的输出token，保存首个差异。

模型服务采用context8192、P/D容量各4096、max-total-tokens8192、Graph bucket16、
BF16 KV和普通非投机decode。P/D每rank BM各6GiB，每机96GiB；模型输入最长3072token，
输出最多128token。容量和测试内容含义见 [GLM52说明](GLM52.md)。

两端任一阶段失败会终止流程并清理本入口启动的进程，报告保持failed，不自动跳过或重试。
每阶段最长默认7200秒，可用 `--timeout` 调整。组件内部等待最长600秒。
模型启动时间较长，控制台打印当前阶段及详细日志路径，可以用另一个终端 `tail -f`。

## 可选参数与结果

### GLM-5.1快速自主回归

两侧同步代码、释放旧服务后，P先执行，D随即执行（各自在仓库根目录）：

```bash
# P容器
bash ascend-mempool-test/scripts/run_ticket10.sh \
  --role prefill --prefill-host 10.120.72.31 --decode-host 10.120.72.32 \
  --run-id glm51-quick-r1 --suite glm51-quick

# D容器
bash ascend-mempool-test/scripts/run_ticket10.sh \
  --role decode --prefill-host 10.120.72.31 --decode-host 10.120.72.32 \
  --run-id glm51-quick-r1 --suite glm51-quick
```

默认权重为`/data_lib/data/models/GLM-5.1-w4a8`，可用`--glm51-model-path`覆盖。
只运行GLM-5.1正式mempool + Graph一次启动，自动处理router、请求、日志及进程清理。
短题17+25、9×7、100−16各跑两遍，加两个zero-decode，共8个请求；输入最多512 tokens，
输出最多128 tokens。服务context1024、P/D容量512、Graph bucket16、非投机decode。
检查答案42/63/84、78层Index K及KV、全rank Graph/生命周期和slot复用。
该模式不运行组件、GLM-5.2或native/eager对照；不宣称覆盖长请求或完整精度回归。
成功时两侧summary的`glm51_regression`为`quick_smoke_passed`；P侧另存
`glm51/formal-graph/tokens.json`和`service.json`。日志仍在统一的run-id目录下。

### 原有完整流程

- 只跑组件：两边加 `--suite components`。
- 组件已有通过证据，只跑四组模型：两边加 `--suite models`。
- 连同GLM-5.1回归：两边加 `--glm51-model-path /data_lib/data/models/GLM-5.1-w4a8`。
  同样执行其四组模型对照；GLM-5.1配置应为78个full层。
- 只查看计划：加 `--describe`，不加载NPU库、不监听端口、不启动进程。

默认目录：`/home/cryang/log/ticket10/<run-id>/<prefill|decode>/`。
每侧有 `summary.json`、`identity.json` 和组件JSON/log；每个模型目录下有各模式的
启动命令、server环境及模型配置、日志。P侧另有cases、token响应、token比较日志、
formal的D日志副本及service.json。

预期两侧最终输出 `AUTOMATED_CHECKS_PASSED`，summary标记`automated_checks_passed`。
这只代表选择的自动检查通过；未选GLM-5.1时报告明确标记`not run`。
保留两侧目录并回传summary及失败阶段日志。最终还需人工核对baseline文本合理、
实际量化/后端与性能观测，再按verification.md确认关闭ticket10。
GLM-5.2完整入口已收到用户glm52-r2双机通过报告；GLM-5.1快速模式的实机结果待用户回传。
