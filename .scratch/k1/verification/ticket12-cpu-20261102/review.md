# Ticket12双轴评审

基点`b4a925a3a3`；实现尚未提交时评审暂存变更：
`git diff --cached b4a925a3a3 -- python ascend-mempool-test`。
两位独立review agent只读检查，未运行测试、未修改文件。
规格为本地ticket12和K1执行文本第16节；标准为AGENTS及用户编码/验收约定。

## Standards

0项违规，0项必要的heuristic建议。

- 功能改动全部位于Ascend/NPU模块，未发现通用shutdown或worker清理路径改造。
- indexed_copy集中处理逻辑行转换和拷贝窗口，各写回、读取及prefix调用复用该入口。
- 新增几何和签名校验属于票12要求；未发现无关防御逻辑、布局开关或11的执行器抽象。
- 自运行入口保留失败非零、既有进程清理、两轮安排，以及用户确认NPU后才关闭票和解锁依赖的边界。

## Spec

0项需修改的问题。

已核对五类区域寻址、页块接口、实际prefix段几何；固定64位基址与小于4 GiB窗口；
设备换槽、mask、特征分写；两源路由、prefix三操作、两个汇总缓冲与既有完成语义；
TP内/P-D签名、注册维度；独立CPU oracle和复用两轮六例的新入口。
宽地址检查使用独立NPU arena，没有新增BM内容读回。

## 最后夹具修正复核

Standards reviewer确认：重建完整PoolLayout避免修改frozen字段，并提供注册所需signature和prefix几何；
新注册测试run-id与夹具一致。未发现新的接口错配，未运行测试。
旧注册方法修正后的结果按两轮上限标为未复跑，不能计作通过。

两轴结论均不代表NPU验收通过；真实宽地址、图捕获/重放和端到端仍由用户实测。
