# Ticket18：alive 按页分配交付

代码已实现；NPU 验收尚未运行，ticket18 保持 open。
基点为 4026e92136bfe1943f0c679c88ccb91c4f8aec64。

## 行为与所有权

MR 根据实际 prompt 页数和 ceil(max_new_tokens/page_size) 分别预留 P/D alive 额度。
总生成上限同时受 D 区域总页数、原生 HBM、context_length 约束；不再按 S_D 限制单请求。
SR 在有序 tick 内确定页集合，所有共享成员重放同一分配次序；每个物理副本只有一份页数据。
P main 与 Index K 同生命周期、同页号映射，各自仍有独立物理区域。

设备页表在 capture 前分配固定 Tensor；分配与绑定在图外更新，沿用 binding event 排序。
P 写回、D 写回和两源稀疏读取均通过页表寻址。prefix 的分配器不改，alive↔prefix 搬运使用实际物理页。
同区域一次单基址64位调用，P远端与D本地仍分两次。2026-11-04用户确认kernel已在Docker装载，Python接口不变；本票不修改kernel。

保留协议中的 slot 字段作为16个请求绑定身份，继续使用既有 generation、签名、drain/free_ack。
它不再代表固定存储范围，也不同于 SGLang req_pool_idx；本票仍单请求串行。
释放前仍等待既有读写与原生资源完成；只有free_ack后MR恢复额度。
绑定保存页表版本，forward前在主机核对版本；过期即fail-stop，不靠设备mask吞掉错误。

## 功能源码阅读顺序

全部功能改动均在 Ascend/NPU 目录；**通用主线功能源码：无**。
既有 Ascend hook 足够承接分配、prepare_batch 与 runtime 绑定，无需修改通用 Scheduler、native allocator 或 shutdown。

| 文件 | 用途 |
|---|---|
| [alive_pages.py](../../python/sglang/srt/hardware_backend/npu/mempool/alive_pages.py) | 区域空闲页、按请求reserve、散页复用、版本租约及归还 |
| [mempool_router.py](../../python/sglang/srt/disaggregation/ascend/mempool_router.py) | MR实际页额度、context准入、回执页数、历史needed记录 |
| [mempool_subrouter.py](../../python/sglang/srt/disaggregation/ascend/mempool_subrouter.py) | SR分配页集合、P/D映射交接、prefix源描述符、drain后归还 |
| [mempool_router_transport.py](../../python/sglang/srt/disaggregation/ascend/mempool_router_transport.py) | 将现有manager交给SR，复用同一映射所有者 |
| [mempool_control.py](../../python/sglang/srt/disaggregation/ascend/mempool_control.py) | 将D协议长度检查从旧槽容量扩大至区域容量，保留生命周期协议 |
| [mempool_service.py](../../python/sglang/srt/disaggregation/ascend/mempool_service.py) | 实际请求入口使用映射容量，避免MR接受后又按旧槽取消 |
| [manager.py](../../python/sglang/srt/hardware_backend/npu/mempool/manager.py) | 稳定设备页表、版本边界、逻辑行/标量地址到物理页转换 |
| [runtime.py](../../python/sglang/srt/hardware_backend/npu/mempool/runtime.py) | capture前分配表、绑定版本、forward前校验、请求页预算、观测真实页号 |
| [offload.py](../../python/sglang/srt/hardware_backend/npu/mempool/offload.py) | P/D及Index K写回使用映射后的逻辑容量 |
| [copy.py](../../python/sglang/srt/hardware_backend/npu/mempool/copy.py) | 两源稀疏读取使用请求映射跨度 |
| [indexed_copy.py](../../python/sglang/srt/hardware_backend/npu/mempool/indexed_copy.py) | 页表查址及单基址64位调用，删除32位窗口分拆 |
| [prefix.py](../../python/sglang/srt/hardware_backend/npu/mempool/prefix.py) | pool-copy与indexed-copy的alive源/目标改为散页地址 |

## 脚本、测试与资料

- `ascend-mempool-test/scripts/run_k1_full_acceptance.py` 增加 `--page-allocation`，沿用四配置双机协调、报告和清理。
- `run_mempool_min.py` 接通Graph gate及完整输出ID比较；`mempool_logical_cases.py` 在该模式下将末两例改为8K/1K，连同前一例4K构成连续混合长度。
- `verify_page_first_copy.py` 验证固定页表、散页、换页、零有效行、独立物理tensor内容；传入实际B=64/128，每个B在矩阵中执行两次，不采trace。
- `verify_mempool_min.py` 核对MR实际预算、SR逻辑/物理页及释放后账本；`verify_mempool_prefix.py` 核对新源描述符，同时保留历史报告读取。
- 新增 `tests/unit/test_alive_pages.py`、`test_device_alive_pages.py`；更新router/subrouter、page_first、runtime、observation及sharing_pipeline已有测试；`src/ascend_mempool_pd/__init__.py` 补CPU导入适配。
- [实施记录](ticket18-implementation-plan.md)、[检查记录](verification/ticket18-20261104/checks.json)、[目标对齐D节](../mempool-min/target-alignment.html)。

## CPU 检查与证据边界

完整套件只执行一次（584个测试入口）。首轮环境为Python3.9，缺少HTTP/transformers依赖且沙箱禁止socket/进程清理；不宣称全套通过。
修复测试替身与环境后，在Python3.12下定向运行43个入口：42通过，HTTP模块因缺requests仍未进入测试主体。
补齐requests后，真实HTTP启动/warmup测试首次执行通过。功能测试主体未安排超过两轮。
6个已知旧失败方法保留：旧UINT32上限预期、两个旧端口集合预期、三个preflight旧mock；见existing-failures.txt。
这些相关生产行为不是本票改动，未扩大为测试清理项目。

新页分配、CPU独立物理内容oracle、同地址重绑定、过期页表forward前fail-stop、MR超旧槽生成预算、超过4TiB单调用寻址均通过。
28个相关源码模块Linux目标strict mypy通过；未将NPU Triton模块纳入Mac类型检查。
格式/静态及入口describe结果见checks.json。
CPU不能证明BM内容、NPU Graph或完整模型输出；以下实机验收仍待用户执行。

## NPU 双机运行

前提：两端部署同一ticket18提交，NPU Docker装载用户确认的64位单基址kernel；沿用已验收的MemFabric SDK补丁。
配置文件使用现有完整验收JSON，填写两端可达主机地址和本地模型路径。两端同run-id、同SDK标记；P/D启动先后均可，由入口自动协调。

P（31）容器，仓库根目录：

```bash
python ascend-mempool-test/scripts/run_k1_full_acceptance.py \
  --role prefill --config /path/to/mempool-min.json \
  --run-id k2-ticket18-001 --sdk-build-id mf114-c01f3ad-ticket11-batchfix \
  --page-allocation
```

D（32）容器，仓库根目录：

```bash
python ascend-mempool-test/scripts/run_k1_full_acceptance.py \
  --role decode --config /path/to/mempool-min.json \
  --run-id k2-ticket18-001 --sdk-build-id mf114-c01f3ad-ticket11-batchfix \
  --page-allocation
```

入口自动完成B64/128×SD128/512四组、独立Graph gate、原生参考、目标请求、容量拒绝、timeout/fail-stop、端口与子进程清理。
采用已有默认父进程退出机制；不增加逐rank主动释放回执。trace强制关闭。`--describe`仅检查配置，不启动NPU。

这是完整验收入口；`--start-from`只补后续配置，报告会标明剩余覆盖，不能单独视为完整矩阵。
日志保存在 `/home/cryang/log/mempool-min/k2-ticket18-001/`。任一失败返回非零码。
回传两端 `matrix-prefill.json` / `matrix-decode.json`，以及各配置下的 `v1-v10.json`、`page-first-copy/`、`target/`、`capacity/`、`timeout/` 与launcher日志。
预期：矩阵passed、完整输出ID匹配、页预算无整槽取整、Graph换页内容正确、释放后额度恢复；user_accepted仍为false，等待用户确认关闭。

## Standards

初审发现4项正确性问题与1项重叠重复代码判断，均已修正；最终未解决硬性违规、正确性阻断、baseline smell均为0。

## Spec

初审发现context准入与replay版本校验两项问题，均已修正。
最终补充发现Graph探针固定B128，已改为传入实际B；未解决项为0。
评审不代替NPU验收。
