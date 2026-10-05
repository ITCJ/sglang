# 09: NUMA 分配失败、启动长尾与 SDK 失败清理跟进

**Type:** bug / investigation

**What to build:** 汇总 BM 本地 DRAM/NUMA 排查证据与复现入口，后续定位指定节点
分配失败、大容量启动长尾及失败后的原生析构异常，并验证相应修复。

**Parent:** [Ascend mempool spec](../spec.md)

**Related:** [02: shadow 服务及原始诊断记录](02-rank-pair-control-lifecycle.md)、
[03: 正式数据路径切换](03-prefill-direct-offload.md)。

**Blocked by:** 无；后续正式服务的容量/性能对照应使用03移除旧hostSHM后的配置。
本票不构成02–08的blocking edge。

**Status:** needs-info

**State:** open

**Priority:** deferred（2026-10-06用户决定再次封存，等待明确恢复）

## 执行决定

2026-10-02首次延期，优先完成demo。2026-10-05用户因P/D各16K容量分配失败，
提前恢复本票，完成双机最小探针对照、plog取证及宿主机驱动源码/物理地址检查。
2026-10-06用户决定再次封存：保留本轮结论和复现入口，暂停进一步排查及驱动/SDK
修复。票据仍为`open`，不表示故障已修复，也不新增主线blocking edge。

本轮确认的关键进展是：31机器的奇数NUMA内存块与安装版驱动定义的四个Fabric窗口
无交集，偶数节点各有170GiB地址覆盖。该限制解释node1探针的快速失败；大容量
偶数节点分配长尾、SDK失败析构和硬件窗口是否可重配置，仍是独立的未解决项。
新增的只读交集脚本用于快速检查其他机器，不创建BM池，不恢复分配压力实验。
保留显式节点列表、诊断开关和既有日志；只有用户明确恢复时，才继续节点分配扫描、
SDK改造、分配串行化或大容量对照实验。真实KV、Graph和生命周期验收仍属主线。

`needs-info`表示恢复排查时尚需部署SDK源码/构建信息和完整硬件结果，不是当前需要
用户立即补材料。原始时间线继续保存在02的Comments；本票作为后续排查入口。

## 环境与配置

| 项目 | 已知信息 |
| --- | --- |
| P / D | npu1-31 / npu1-32，10.120.72.31 / 10.120.72.32 |
| 运行环境 | Docker内 `/home/cryang/sglang`，Python 3.11.15；NPU操作由用户执行 |
| 拓扑 | 每侧device0–15、TP16；16个独立P_i/D_i二rank池；P BM rank0、D BM rank1 |
| 本机NUMA | 节点0–7；已贴快照Mems_allowed_list=0-7，主机总DRAM约2TiB |
| BM用途 | HOST DRAM、HBM贡献0、SDMA；映射就绪后才安装runtime和capture |
| MemFabric | 1.1.4，commit `c01f3ad842b9ff7412681a44b67141ce7a124c6d`，2026-08-03构建 |
| 驱动 | 31宿主机`Version/package_version=26.1.1`、`ascendhal_version=7.35.23`、`Innerversion=V100R001C10SPC009B220`；两侧MF日志检测为该内部版本/V5 |
| 10-02大配置 | context16384；78层、16slots、P/D各8192tokens、1head、dim576、BF16 |
| 10-05失败容量 | 用户反馈P/D各16384tokens；TP16、16slots、78层、1head、dim576、BF16；未回传该轮完整分配失败日志 |
| 最新相关环境 | CPU_AFFINITY=1、NPU_USE_MULTI_STREAM=1、TASK_QUEUE_ENABLE=0、PYTORCH_NPU_ALLOC_CONF=expandable_segments:True |

上表最后一行的前两个变量全名分别为`SGLANG_SET_CPU_AFFINITY`、
`SGLANG_NPU_USE_MULTI_STREAM`。这些设置是观测事实，未验证各自与故障的因果关系。
容器中的free/NUMA信息不能代替宿主机完整cgroup额度；抓栈必须匹配容器PID或通过
NSpid找到宿主机PID。容器root不保证可以读取内核栈或ptrace。

## 当前可用规避与已交付代码

两侧服务启动前分别设置：

```bash
export SGLANG_NPU_MEMPOOL_LOCAL_NUMA_NODE=0,2,4,6
```

实际选择为`nodes[tp_rank % len(nodes)]`，使用真实TP rank；BM rank0/1不参与轮转。

| NUMA | TP ranks | BM flags | 1GiB/池时 | 11GiB/池时 | 22GiB/池时 |
| --- | --- | ---: | ---: | ---: | ---: |
| 0 | 0、4、8、12 | 128 | 4GiB | 44GiB | 88GiB |
| 2 | 1、5、9、13 | 130 | 4GiB | 44GiB | 88GiB |
| 4 | 2、6、10、14 | 132 | 4GiB | 44GiB | 88GiB |
| 6 | 3、7、11、15 | 134 | 4GiB | 44GiB | 88GiB |

- 节点列表代码已推送为`5067e7cb21`；旧`SGLANG_NPU_MEMPOOL_LOCAL_NUMA_NODE_COUNT`
  已取消。历史COUNT经历过按全部节点轮转及临时只用偶数节点两个版本，不能照搬旧命令。
- 未设置列表时用`flags=0`。列表为空、重复、格式非法、超出0..126、含不存在/未在线
  节点，或无法读取在线拓扑时，提示并整份回退到默认分配；不会只留下部分节点。
- 有效列表经`0x80 | node`传HAL。列表验证只确认节点在线，不保证驱动可分配该节点。
  127是MF自动亲和保留值。默认HAL日志numa:4294967295表示传入-1，不证明实际落点。
- 配置回退发生在create2前；已进入SDK并失败的create2没有同进程自动重试默认策略。
  页大小回退由SDK执行，仍保留所选NUMA节点。
- 当前版本相关CPU测试36项通过，静态检查通过；最新D日志确认列表进入HAL。
  新版本完整大容量P/D服务验收尚未收到。

代码入口：

- [manager.py](../../../python/sglang/srt/hardware_backend/npu/mempool/manager.py)：
  `_configured_numa_nodes`、`create`、`initialize_rank_pair`及映射检查。
- [runtime.py](../../../python/sglang/srt/hardware_backend/npu/mempool/runtime.py)：
  `initialize_for_model_runner`，记录布局、原hostSHM容量和配置环境。
- [diagnostics.py](../../../python/sglang/srt/hardware_backend/npu/mempool/diagnostics.py)：
  `SGLANG_NPU_MEMPOOL_DIAGNOSTICS=1`启用MF INFO和快照；BEGIN/END/FAIL、每15秒WAIT、
  首次WAIT的调用线程Python/内核栈、wchan、NUMA与可见cgroup信息。采样不调用BM/NPU，
  不给SDK阻塞增加可中断保证；`--mempool-timeout`也不保证取消原生create2。

## 已有实验与证据

| 时间 / 场景 | 结果与证据边界 |
| --- | --- |
| 10-01 21:23真实服务 | P大多数create2约108–112秒后完成；D观察79–87秒仍未返回，随后P被Ctrl-C停止。未证明永久死锁；P等D映射的错误不能代替D分配诊断。 |
| 10-01 D PID1169采样 | wchan为devmm分配路径，ps列有截断；未取得有效GDB输出。容器free约1.6TiB available不能排除cgroup或特定分配资源限制。 |
| 10-01 22:35/22:41单pair writer | 两侧均20条PASS，其中10条decode replay。22:41为72层、16slots、8192tokens、11GiB/rank；HAL约P1.17秒/D2.01秒。没有模型及旧hostSHM。 |
| 10-01独立16pair | 每侧16个11GiB池同时存活，两侧全部PASSED。P `/tmp/mempool-bm-startup-p/run.fv3xez`，D `/tmp/mempool-bm-startup-d/run.dKTy4b`，入口交付`927e01e4ef`；逐卡原始报告/耗时未独立核验。 |
| 10-02关闭CPU亲和性 | 用户反馈仍卡住；独立BM继续通过。一次服务与独立测试Shmem差约186.57GiB，接近当时旧hostSHM约186.56GiB/机，但采样并非受控基线，不能证明唯一根因。 |
| 10-02小容量真实服务 | context1024、P/D各512、1GiB BM/rank；两侧启动，D capture约96.94秒。单个请求全部16ranks实际replay、drain/DONE/ACK并恢复16个free slots。attention仍读旧路径，未做真实BM数值比对。 |
| 10-02 14:08全部8节点轮转 | P奇数节点两种页均HAL6，部分偶数节点成功；奇数节点MemFree当时小于1GiB。P退出后D连接store失败，本轮不能据此判断D节点能力。 |
| 10-02 14:59清缓存后 | 节点仍有约160–200GiB free，P/D仍出现奇数节点HAL6，D已进入自己的create2。清缓存没有消除现象，普通节点MemFree不足不能解释此轮。 |
| 10-02 15:24–15:26最小探针 | P device0/1分别在默认、node0成功，在node1两种页均HAL6；失败后均SIGABRT/134，详见下节。 |
| 10-02双机偶数节点16池 | 1GiB/池，P/D全部16池通过、各偶数节点4池、32个worker正常退出，包含64字节peer probe、同时持池及释放。交付`5b100c0001`，当时仍是旧COUNT偶数策略；用户确认“感觉没有问题”。 |
| 10-02 20:42–20:45大容量真实D | 新列表配置生效；11GiB/池，多rank由1GiB页回退2MiB页后成功，最长已完成169.444秒。片段末尾TP6/10仍在等待；未收到整轮完成结果。 |
| 10-05双机最小探针 | P `/tmp/bm-numa.nxa4l9`、D重跑 `/tmp/bm-numa.wezpF9`：device0/1在默认、node0均exit0，node1均exit134。P PID208036、D PID323005的完整node1日志均为两种页HAL6→create2失败→退出析构堆损坏。最早D `/tmp/bm-numa.xhFDVu` 全exit1不能作为节点能力证据，以重跑为准。 |
| 10-05 22:19 P node1重测 | `/tmp/bm-numa-node1-recheck.log`，PID208496，1GiB/2MiB页分别约48/17微秒返回6。宿主机实时dmesg及所查journal区间无记录；容器plog存在对应DRV失败，详见下节。 |
| 10-06安装版源码核对 | 31的26.1.1包内头文件和allocator确认四个窗口及NUMA交集筛选；与31内存块地址叠加后，node1及其他奇数节点交集为0。尚未修改驱动或执行修复后测试。 |

偶数节点双机实测文件：P `/tmp/bm-even-numa-p/run.aAPaAl`，
D `/tmp/bm-even-numa-d/run.EreEQi`；包含每卡`.log/.json`、`pids.tsv`、`exits.tsv`、
`even-numa-summary.json`。当前收到的是终端汇总，原始文件仍在用户服务器。
其他服务轮次完整路径/服务器HEAD并未全部提供，不能以本地交付commit替代部署证据。

## 问题A：指定node1立即分配失败

最小复现为P上的独立进程、world_size=1、1GiB HOST/SDMA，无模型、跨机join或Graph。
探针交付版本`0c49f55296`，原始目录 `/tmp/bm-numa.qDNtCX`。

| device | 默认flags0 | node0 flags128 | node1 flags129 |
| --- | --- | --- | --- |
| 0 | HAL0、exit0 | HAL0、exit0 | HAL6两次、create2失败、exit134 |
| 1 | HAL0、exit0 | HAL0、exit0 | HAL6两次、create2失败、exit134 |

node1两次调用耗时分别为device0的44/18微秒、device1的49/17微秒；成功分配约
101–126毫秒。探针总耗时包含导入、初始化和清理，不能当成HAL耗时。
以上为10-02历史记录。10-05已补齐D侧同样六组对照，两侧node1行为一致。
当前源码和地址证据见下节；没有对31的每个奇数节点逐一运行BM分配，也没有收到32的
完整物理地址表，不能把31的逐节点地址计算当成32的实测结果。

### 2026-10-05/06：node1与Fabric窗口无交集

31机器的`PAGESIZE=4096`、`Mems_allowed_list=0-7`、online nodes为0-7。
node1普通MemFree约147GiB，buddyinfo仍有大量2MiB/4MiB块；zoneinfo显示其主内存
属于Normal区，只有少量低地址内存位于DMA区。这些快照不能证明Fabric窗口内有空闲页，
也不支持把此次1GiB最小探针失败直接归为全节点内存不足或普通碎片化。

失败plog：`/root/ascend/log/run/plog/plog-208496_20261005221859163.log`，
125/129行的`devmm_vmm_mem_create`分别记录：

```text
ret=6; size=1073741824; side=2; devid=65; module_id=48; pg_type=2; mem_type=3
ret=6; size=1073741824; side=2; devid=65; module_id=48; pg_type=1; mem_type=3
```

对应HOST NUMA / P2P DDR路径，先1GiB页再2MiB页；`devid=65`是内部Host设备标识，
不能读成NUMA65。DRV错误已进入用户态plog；`dmesg/journalctl -k`无输出不表示调用
没有进入驱动，亦不能据此断言错误只发生在用户态。后续的glibc abort属于问题B。

用户在31宿主机直接提供了以下安装版源码，而非仅凭公开仓库推断：

- `/usr/local/Ascend/driver/kernel/svmdrv/pmaster/common/inc/devmm_common.h:68–123`
- `/usr/local/Ascend/driver/kernel/svmdrv/pmaster/comm/svm_master_phy_allocator.c:180–255`

非`EMU_ST`分支定义`DEVMM_S2S_HOST_NODE_NUM=4`、每个窗口
`DEVMM_S2S_HOST_NODE_MEM_SIZE=0xaa80000000`（682GiB）：

| Fabric窗口ID | 本机物理地址范围 `[start, end)` |
| --- | --- |
| 0 | `[0x29580000000, 0x34000000000)` |
| 1 | `[0xa9580000000, 0xb4000000000)` |
| 2 | `[0x129580000000, 0x134000000000)` |
| 3 | `[0x1a9580000000, 0x1b4000000000)` |

这四个ID是Fabric地址分段编号，不是Linux NUMA 0–3。用户读取的内存块大小为1GiB，
按node目录中的实际内存块合并相邻地址得到：

| Linux NUMA | 内存块覆盖范围 `[start, end)` | 覆盖GiB | 对应Fabric窗口 | 交集GiB |
| --- | --- | ---: | --- | ---: |
| 0 | `[0x28000000000, 0x2c000000000)` | 256 | 0 | 170 |
| 1 | `[0x40000000, 0x80000000)`；`[0x24080000000, 0x28000000000)` | 255 | 无 | 0 |
| 2 | `[0xa8000000000, 0xac000000000)` | 256 | 1 | 170 |
| 3 | `[0xa4000000000, 0xa8000000000)` | 256 | 无 | 0 |
| 4 | `[0x128000000000, 0x12c000000000)` | 256 | 2 | 170 |
| 5 | `[0x124000000000, 0x128000000000)` | 256 | 无 | 0 |
| 6 | `[0x1a8000000000, 0x1ac000000000)` | 256 | 3 | 170 |
| 7 | `[0x1a4000000000, 0x1a8000000000)` | 256 | 无 | 0 |

每个偶数节点起点记为B，其覆盖`[B, B+256GiB)`，匹配窗口为
`[B+86GiB, B+768GiB)`，交集为170GiB。总内存块覆盖2047GiB，交集680GiB。
此数值是地址覆盖上界，包含已使用/保留区域和块内可能的空洞；不是MemTotal或MemFree，
更不是保证可分配的BM容量。32需独立读取拓扑，不能只因两机故障相同就复用该表。

安装版`devmm_master_alloc_numa_large_pages()`对每个NUMA PFN范围与四个窗口取交集，
再按页大小对齐。交集为空时直接`continue`，不会调用`alloc_contig_range()`。
指定NUMA时循环末尾`break`，不会自动尝试其他节点。node1的地址范围不与任何窗口
相交，所以换成2MiB页仍无候选地址。这解释了两次HAL调用均迅速失败的现象。

**结论边界：** 已找到与node1实测一致的安装版驱动分配约束；尚未追到硬件/固件窗口
建立位置，未逐一验证安装源码与已加载模块的构建身份，也未执行任何驱动修复。
不能宣称奇数节点在所有平台或驱动版本上永久不可用。

### 能否移动窗口或利用奇数节点

普通Host内存可以使用奇数NUMA；当前MF的P2P DDR/Fabric分配路径受上述交集限制。
`numactl`、CPU亲和性和页面迁移只影响分配选择或内容落点，不会移动DRAM的物理地址。
仅合并/重命名NUMA节点也不会扩大物理地址与Fabric窗口的交集。固件或内存控制器
是否支持改变地址布局，必须按实际服务器平台确认，目前没有已验证的BIOS方案。

修改磁盘上的`devmm_common.h`不会改变已加载模块。重编译对应模块会改变分配筛选，
也会改变`devmm_get_s2s_host_global_addr()`的换算：

```text
global = 0x800000000000 + server_id * (682GiB * 4)
       + fabric_window_id * 682GiB + (host_PA - local_window_start)
```

公开源码的跨服务器Host内存导入路径
`devmm_get_cs_host_target_blk_info → devmm_s2s_host_addr_trans`使用此换算。
目前追到的是软件筛选/地址换算，没有发现由这些常量配置硬件窗口的调用；不能据此
认为改常量就会移动真实映射。若底层映射仍旧，分配成功也不能证明远端访问地址正确。
后续需厂商确认硬件映射约定、支持的配置方式及两侧配套组件，暂不改常量或加载修改版驱动。

尚未验证的其他方向：将普通模型加载/临时CPU内存放到奇数节点，给偶数节点留余量；
或让仅本机消费的D生成KV使用普通Host注册内存。后者需要存储/映射路径改造及真实
NPU copy、Graph验收，不能把现有MF flags或Host RDMA模式当成已验证的直接替代。

源码参考：公开`cann/driver`快照
`e374b0dfdb9955634f575210727b2389ae71ba71`（2026-09-30），910B/910_93构建使用的
custom版本为`26.1.0.b061`，不等于本机26.1.1二进制。
[窗口定义](https://gitcode.com/cann/driver/blob/e374b0dfdb9955634f575210727b2389ae71ba71/src/sdk_driver/svm/v2/common/inc/devmm_common.h)、
[分配器](https://gitcode.com/cann/driver/blob/e374b0dfdb9955634f575210727b2389ae71ba71/src/sdk_driver/svm/v2/master/comm/svm_master_phy_allocator.c)、
[跨服务器地址换算](https://gitcode.com/cann/driver/blob/e374b0dfdb9955634f575210727b2389ae71ba71/src/sdk_driver/svm/v2/master/comm/svm_master_mem_share.c)。
窗口和交集逻辑已由用户贴出的安装版源码确认；其他参考实现仍须按部署版本核对。

## 问题B：create2失败后的SDK退出异常

两份node1失败日志均显示：HAL6 → Python RuntimeError → BM/store uninitialize完成
→ Python RESULT → 约1–2秒后HYBM析构 → glibc abort。`cleanup_errors=[]`只说明
显式Python清理未捕获异常；`timeout: ... dumped core`此处报告子进程崩溃，并未到时限。

同一个预留地址`0x280040000000`先出现`HalMemAddressFree return:-8`，之后出现
`reserved space not found`和析构失败，最终`malloc_consolidate(): invalid chunk size`。
日志证明检测到了原生堆损坏，尚不能确定最早的越界写、UAF或double-free位置。

本地MF参考源码把-8定义为`BM_UNDER_API_UNLOAD`；HAL函数指针被清空后返回该值。
uninit卸载底层API后继续析构的时序与日志吻合。另有本地回滚修复提交
`2e47225066f0372c62e524008f9c79f41ea1ac8c`（rollback bm init state when failed）。
部署commit `c01f3ad...`不在本地对象库，尚未证明它缺此修复，也未验证该修复能解决本例。
修复清理与恢复node1可分配能力需要分别验收；不能用`os._exit`隐藏析构崩溃。

## 问题C：11GiB分配长尾与22GiB容量失败

10-02布局逻辑KV为10.96875GiB/rank，加64字节probe后按1GiB对齐为11GiB；
每机176GiB、每个偶数节点44GiB。22GiB GVA覆盖P/D两个贡献，不是本机每rank物理申请量。
D日志同时记录旧hostSHM为25,033,522,176 bytes/rank，按16worker估计约373.03GiB；
加BM约549.03GiB/机，尚不含其他内存。此处记录的是历史shadow配置；03正式模式已关闭
旧hostSHM，不能把这份历史重复存储容量当作10-05正式模式的实际占用。

已完成create2的代表值：TP13/14/12/11约1.4–1.7秒；TP1 node2为21.302秒，
TP5 node2为79.821秒，TP2 node4为153.213秒，TP9 node2为169.444秒。
后四者都出现1GiB页失败后2MiB页成功；随后join、映射、runtime均完成。
末尾TP6/10均node4，各约150秒的1GiB页尝试返回6后进入2MiB页路径，观察至约165秒
尚未返回。其他遗漏rank也不能补算成功；没有最终16rank/Graph/服务完成证据。

等待时内核栈出现`alloc_contig_range`、`lru_add_drain_all`、`__drain_all_pages`、
`devmm_master_alloc_numa_large_pages`或giant页分配中的
`devmm_master_free_giant_pages/devmm_master_free_one_page_by_size`。
等待定位在本地驱动物理页分配/整理/释放；碎片化、并发争用和驱动行为的贡献仍待区分。
已有慢rank最终成功，因此不能从WAIT次数推断永久死锁。D状态也不等于正在做磁盘I/O。

10-05用户反馈P/D长度各增至16K后失败。对应78层、16slots、BF16 MLA dim576：
`78 × 16 × 16384 × 1 × 576 × 2 = 23,555,211,264 bytes = 21.9375GiB/rank`。
加probe及贡献对齐为22GiB/rank，TP16每机352GiB，两机704GiB；四个偶数NUMA各承载
4个独立22GiB申请，合计88GiB。它们不要求一整块88GiB连续物理内存。
MF日志显示物理页尝试为1GiB→2MiB，而非16GiB→2MiB；SGLang贡献大小对齐与
驱动物理页分配粒度是两层概念。每个偶数节点170GiB只是地址上界，不能从全节点
130–180GiB free推出窗口内有88GiB可用，碎片化也尚未被确认为这轮失败的唯一原因。
本轮未取得完整服务分配日志，不能把小探针的node1根因直接套到偶数节点的大容量失败。

## 已排除的过度结论与源码边界

- HAL6在参考源码中是OOM类错误，但不能据此直接判为主机总内存不足。快照的可见
  cgroup failcnt=0、limit近似无限；某些失败轮次节点有大量free。node1区域约束现已定位，
  偶数节点窗口内的实际空闲页及大容量失败条件仍未知。
- `HugePages_Total/Free=0`、缺libhcom、未设置extend library也出现在成功rank，
  无法单独解释故障。没有依据要求静态HugeTLB预留或反复drop_caches。
- 源码路径 `memfabric_hybrid/src/hybm/csrc/mm/hybm_vmm_based_segment.cpp` 的
  `MallocFromHost/HalMemCreateAdapterFromHost`：MEM_HOST_NUMA_SIDE、MEM_P2P_DDR_TYPE，
  giant页OOM后在同节点换huge页；`spend time`单位为微秒。
  日志支持这条分配路径，但参考源码不是部署二进制的逐行证明。
- SDK日志中的numa确认传入参数；严格物理落点和跨NUMA性能尚未验收。
  CPU绑核、请求NUMA、BM rank和NPU device是不同概念。
- `MEMPOOL_INIT READY`是单worker BM/runtime就绪，之后仍有Graph和PD control握手；
  独立64字节probe或正常生成文本不能代替真实BM KV内容比对。

## 恢复排查时的入口与验收

完整命令、端口、约束及采集方式集中在
[BM_NUMA_DIAGNOSTIC.md](../../../ascend-mempool-test/BM_NUMA_DIAGNOSTIC.md)。
这些是保留的复现入口，当前不要求重跑：

- `scripts/probe_numa_fabric_overlap.py`：新增纯标准库只读工具，读取在线sysfs内存块
  和指定安装版头文件的非`EMU_ST`窗口，输出每节点地址交集和可选JSON；不申请BM，
  不核验硬件寄存器、当前空闲页或已加载模块。缺少/无法识别输入时明确失败，不套用31常量。
- `scripts/probe_bm_numa.py`：单device/node、world_size1，直接读取`--numa-node`；
  flags默认/显式对照、独立进程退出状态和GDB失败栈。
- `scripts/run_bm_startup_gate.sh --even-numa`：两侧各16个1GiB池及实际退出码，
  `check_bm_even_numa.py`核对节点分布；不带该选项的既有配置是11GiB/rank、72层，
  并非当前78层真实模型的完整替身。
- 实际服务：保留完整P/D日志、代码及SDK构建版本、容量/布局、前后NUMA/cgroup快照，
  对照03后无旧hostSHM的正式路径；先固定容量和环境再单独改变待验证因素。

- [ ] 对照部署SDK/驱动版本，确定node1失败条件；若属受支持的节点限制，明确记录
  可用配置及约束，不要求所有在线节点都必须分配成功。
- [ ] 失败case可以干净退出，保留原分配错误，无迟到析构、堆损坏或SIGABRT；
  默认/node0的成功与正常释放回归通过。
- [ ] 用正式服务配置验证P/D全部16池及最终服务状态，记录每rank分配耗时/页回退，
  区分成功但慢、明确失败与原生阻塞；性能目标须在恢复本票时另行确定。
- [ ] 验证所承诺的NUMA分配行为及容量压力边界，记录未解决限制和重测脚本。

## Comments

2026-10-02：按用户指示从02汇总NUMA/分配排查，保留历史原文与服务器文件位置，
建立独立延期ticket，不修改运行代码，不新增硬件通过声明。主线继续02真实KV读回、
03数据路径切换、04正式Graph/模型验收；本票不阻塞demo。
Mac文档检查：`git diff --check`通过，新增/更新文档的本地文件链接检查通过。
未运行CPU/NPU代码测试，未commit/push。

2026-10-06：按用户要求封存本轮排查，更新10-05双机探针、plog、26.1.1安装版源码、
31物理地址/Fabric交集与16K容量核算。保持`needs-info/open/deferred`；node1限制已有
解释，大容量分配、SDK失败清理和可否调整硬件窗口均未宣称修复。
新增`probe_numa_fabric_overlap.py`及诊断说明，交付为单机只读地址检查，不扩展本票验收。
Mac已执行5项CPU测试：用户31完整地址表复现、空洞/offline/窗口重叠、未知头文件、
跨节点共享内存块、热插拔/输入缺失错误处理；全部通过。31快照计算为偶数各170GiB、
奇数0、总交集680GiB。真实宿主机读取待用户运行，不新增NPU分配或修复通过声明。
新脚本mypy严格检查、两份新Python文件的ruff/isort及格式检查通过；本地公开驱动完整
头文件可解析出四个682GiB窗口。Markdown本地链接与`git diff --check`通过。
本轮未执行git add、commit或push。
