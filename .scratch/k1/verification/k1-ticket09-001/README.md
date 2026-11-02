# Ticket09 NPU001用户回传与端口修复

2026-11-02用户回传31/32终端摘要，指导版本04f5d9fc52（摘要未含git SHA）。

- b128-sd128：两侧matrix条目passed、exit_code=0；P的V1–V10、冷A/C稳定性、长生成、timeout、paired cleanup全部passed。冷A/C各17 tokens，IDs相等。
- b128-sd512：preflight失败，D提示10.120.72.32:10000 occupied；尚未启动本组fetch/model。P收到peer failed。
- b64-sd128、b64-sd512：not-run。
- 总体failed；09保持open，未取得原始逐rank报告及端口状态快照。

本地确认run_service.sh使用10000作dist-init-addr，但service_ports漏掉此端口。
回归通过公开service_ports接口先失败（10000 not found），加入后通过。
旧preflight使用无SO_REUSEADDR的单次bind，把所有绑定错误统一描述为occupied。
仅凭现场摘要不能断定是TIME_WAIT、残留listener或其他进程占用。

修复仅测试脚本：将10000纳入已有阶段释放检查；preflight复用released_port。
该实现仅在/proc观测到TIME_WAIT时等待，实际bind/listen成功才通过；listener继续失败。
记录preflight-port-10000.json及阶段port-release-10000.json；不杀未知进程、不修改生产shutdown。
本机两项回归通过（端口集合与既有TIME_WAIT/listener行为），未访问NPU。

矩阵新增--start-from，第二轮从b128-sd512继续，前组沿用001用户证据。
本次只改验收端口和续跑编排，未改KV功能；跨版本合核限此范围。
续跑报告明确remaining combinations only，不冒充本轮四组均运行。
若仍报真实监听，回传该JSON与占用进程信息，再定位进程；不自动进行第三轮。
