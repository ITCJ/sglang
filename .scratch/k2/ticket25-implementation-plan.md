# Ticket 25 实施计划

基点：46af27553847c013f07bad5c2428b1c5460a082b，当前分支实施。

用户已要求开始实施，并缩小测试边界；新增测试集中在现有 router/SR
服务入口，覆盖冷请求、命中请求和释放。用户明确“双机的on不要作”，
因此不安排双机 on 组；远端写可见性、真实 BM 内容与计算重叠仍未验证。
CPU 通过不能关闭本票。

1. RouterConfig 默认关闭双写。请求开始预留 D 新算页、生成页及 P 候选页。
   候选容量不足时整次不存；命中页保留 prefix 引用直到 D drain。
2. SR 分配候选页并传递页路由；P alive 不分配。load_back 保留、fill 取消。
3. NPU 写回在原 stream 写 D 副本和候选页；Index K 只写候选页。
   D 读命中 prefix 和本地新算页，当前 token 仍走 HBM。
4. admit 只 publish，返回重复候选页；释放沿用原父进程和请求清理路径。
5. 验证、Standards/Spec 两轴评审；独立 commit，票保持 open。

预计功能修改仅在 disaggregation/ascend 与 hardware_backend/npu。
D 的物理 alive 容量需覆盖 prompt 与生成页，启动预算显式计费。
不增加通用 shutdown 或逐 rank 退出回执。
