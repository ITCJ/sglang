# 宿主机 RoCE 传输检查

只使用现有 `ib_write_bw`（perftest）、Bash、coreutils 和 awk。
运行时依赖宿主机 RDMA 驱动、libibverbs 和对应网卡 provider；不自动安装依赖。
不使用容器、Python、CANN、Mooncake 或 NPU，不执行数据内容校验。
两端应使用相同 perftest 版本。之前的 6.03 需在目标机器实际验证。

## 问题

指定的两台机器能否完成 Host 内存 RDMA WRITE？可选查看短时带宽。
通过 TCP 交换连接信息，不使用 RDMA CM (`-R`)；数据由 RDMA WRITE 传输。
smoke：4 KiB × 100 次、单 QP、发送深度 8。bw：4 KiB、3 秒。
**bw 不限速，即使时间短也可能打满链路；只允许小流量时仅运行 smoke。**
注册少量 Host 内存，测试时停止模型。默认超时 90 秒，禁止生成 core dump。

## 前置条件和两端命令

代码提交并推送后，两端在仓库根目录先执行：

```bash
git pull --ff-only
git log -1 --oneline
```

核对交付 commit。可把整个目录复制到相同系统的其他机器，无需编译。
设备、IB 端口和 GID 必须来自当前宿主机，不能照抄历史编号。
选择对应 RoCE 网口 IP 的 RoCE v2 GID；服务端 IP 不得替换为管理 IP。
可用已有的 `bash workspace/setup/check_gid.sh` 查看候选项（独立于本目录）。
两端需要互通控制端口 TCP 18515，以及相应 RoCE 数据通路。

服务端先运行（START 仅表示开始启动，不保证已监听）：

```bash
bash workspace/roce_test/run.sh server <SERVER_HCA> <SERVER_IB_PORT> <SERVER_GID> smoke
```

客户端随后在 90 秒内运行：

```bash
bash workspace/roce_test/run.sh client <CLIENT_HCA> <CLIENT_IB_PORT> <CLIENT_GID> smoke <SERVER_ROCE_IP>
```

两端完成后交换角色，再测一次反向。多台机器先选中心节点逐对执行，避免并发。
smoke 两端通过且允许短时高流量后，两端将 `smoke` 同时改为 `bw` 再运行。
带宽结果使用工具原始表头和单位；小报文短测试不是链路峰值带宽验收。
多网卡时 TCP 控制连接由系统路由选源地址，RDMA 设备与 GID 则由参数指定。

## 成功标志

两端均打印 `ROCE:PASS`。仅证明所选 RDMA WRITE 完成，不代表内容校验、
集群全互联或物理路径认证。bw 额外输出一行表头、一行带宽结果。
回报机器代号、方向、模式及这几行即可。

## 失败回报

失败立即停止依赖该结果的后续测试，回报 `ROCE:FAIL` 和 `HINT` 行。
只有没有 HINT 时，执行以下短日志命令，回报最多 3 行：

```bash
tail -n 3 <LOG_DIR>/perftest.log
```

完整日志和命令、版本保留在终端显示的 `/tmp/roce-*` 目录。
本测试不涉及镜像、CANN 或 Mooncake；首次记录两端 commit、内核/驱动版本、
perftest 版本、实际 RoCE IP、设备/GID 和日志目录，真实地址只保留在远端或本地事实记录中。

## 清理

两端进程正常退出或超时后无需清理服务。中途退出可在两端终端按 Ctrl-C。
保留日志供定位；确认不再需要后删除本次日志目录。不修改网络或驱动配置。
