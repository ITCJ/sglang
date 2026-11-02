# K1 ticket11：两组 SDMA demo

本入口不加载模型，不切换服务fill/admit。它调用服务后续复用的 `pool_copy.py`。
两侧各4个进程、两组，每组两个rank，共8参与者；使用生产SharingPlan/PoolSession/manager。

## 准备

两台主机/容器使用相同代码版本、Python环境，包含torch、torch_npu、msgspec、MemFabric及UniDexCopy。
使用P/D设备能互访的现有环境。复用当前驱动、CANN和库安装，不自动升级SDK。
将 `configs/k1-ticket11-demo.example.json` 复制为双方一致的映射文件，填入实际可见设备号和NUMA。
rank0/1必须同节点且在P的NUMA0，rank2/3同另一节点。示例设备号不是探测结论。

端口默认29100（gloo）、29101（BM store），BM NIC使用29200起由SharingPlan分配的实际端点。
两侧先确保无同端口旧服务。每次使用全新的报告目录。

## 运行（每台一条命令，P先启动后尽快启动D）

在仓库根目录，将P_IP、D_IP与映射路径替换为实际值。使用装有上述依赖的python。

P：

```bash
python ascend-mempool-test/scripts/verify_pool_sdma.py --role prefill --prefill-host P_IP --decode-host D_IP --mapping /home/cryang/k1-ticket11-map.json --report-dir /home/cryang/log/k1-ticket11-demo-001-p --timeout 120
```

D：

```bash
python ascend-mempool-test/scripts/verify_pool_sdma.py --role decode --prefill-host P_IP --decode-host D_IP --mapping /home/cryang/k1-ticket11-map.json --report-dir /home/cryang/log/k1-ticket11-demo-001-d --timeout 120
```

入口自动启动本侧4个worker；双侧通过gloo协调准备、copy、校验与汇总。
父进程终止并回收自身worker；任务失败不调用pool释放，沿用进程清理。
实际端点重绑检查复用已有TIME_WAIT有界等待。不会将显式BM析构认定为通过。
一侧异常时另一侧会在既定gloo/父进程超时后退出；保留双方目录。

## 执行内容与判据

8页真实几何：每页主KV 11,501,568 B，Index K 688,128 B。
admit由各段owner读本组alive与NUMA0 Index K；fill由组内两个rank按完整8页分工。
同一执行线程一次提交混合main/index清单，调用同线程BM wait，控制线程poll。
正常设备事件不参与判断库完成。计时含batch提交与wait，记录空闲tick、搬运tick和wait窗口内推进数。

两组demo的总块数为admit16、fill24（不是生产8组的fill72）；总字节分别97,517,568及189,530,112。
所有目标由独立确定性数据模式比较；admit后P设备索引读prefix，fill后P/D设备索引读P alive。
校验不使用被测BM batch作为读回接口。初始化和校验的设备算子不算池间搬运算子调用。

所有rank完成内容校验及全局计数，launcher清理/端口通过才返回0；失败非零。
`status=passed`只代表functional smoke。`interface_ready=false`与`user_accepted=false`保留。
wait区间没有tick样本时GIL实测证据不足，不能仅凭源码宣布测量通过。
不安排第二个8组demo；完整分片和服务B/C、V2在后续服务验收中覆盖，服务不增加目标读回。

## 接口事实和已知限制

依据本地GitCode 1.3.0/29fbd1ae14e7；部署库是否等同须用报告中的模块/库信息核对。

| 项目 | 当前结论 |
|---|---|
| 完成 | Python wait返回整数；底层等待调用线程的内部SDMA流，不是独立batch句柄 |
| 全部子任务 | 正常wait覆盖该流已提交任务；本地batch有子提交错误可能丢失的路径，尚未证明全部预期子任务必被提交 |
| 可见性 | demo检验真实设备读者；已读源码未见独立fence，底层完成的硬件可见性契约仍待确认 |
| GIL | Python batch/wait绑定释放GIL；实际进度以同进程ticks_during_wait为准 |
| 跨owner | fill清单包含另一owner的prefix段，运行后以比较结果确认 |
| 条目上限 | count为uint32，未发现更小公开上限；SDMA队列深度2048不是API条目上限。旧SoC单连续任务长度不超过UINT32_MAX；本demo远低于此值 |
| 错误身份 | 返回任务rid/op_id/kind及清单、阶段错误；SDK无逐子条目结构化返回，不伪造失败块号 |
| 超时 | 适配层从入队计时，poll超时为UNKNOWN；wait原生函数不可取消，失败/未知保留区域由父进程处理；迟到成功不会恢复UNKNOWN |

本次不修改SDK、不制造非法设备访问。CPU负测只能证明适配层错误传播，不能证明SDK不存在吞错。
接口契约未解决前不将functional smoke视作服务接入许可。

回传双方的 `launcher.json`、`all-ranks.json`、`rank-*.json`、`rank-*.log`。
同一测试最多2轮；先分析失败，不自动连续重跑。

## 功能源码阅读顺序

Ascend/NPU：`hardware_backend/npu/mempool/pool_copy.py`（任务/结果、SDMA适配与执行线程）；
`hardware_backend/npu/mempool/manager.py`（pool_transfer装配入口）。通用主线功能改动0。
测试/脚本：`scripts/verify_pool_sdma.py`、`tests/unit/test_pool_copy.py`、示例映射与本文。
