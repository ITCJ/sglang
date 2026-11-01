# K1-ticket07 本机验证

基点：1009c867881bdd7ed99b53278a9aa9ff7f2174b7。
解释器：`/private/tmp/k1-ticket05-fix-venv/bin/python`，在开发Mac执行，未连接硬件服务器。

## 结果

- [完整CPU日志](full-cpu.log)：单轮518项；其中21个测试因沙箱禁止回环socket或ps而阻断，产生25个错误/子用例错误。
- [权限补跑](cpu-permission-retry.log)：仅补跑上述21项，22.730秒，全部通过。
- [评审修正](review-fixes.log)：新增完整验收入口兼容性回归，以及D反向NUMA映射，2项通过。
- 新增回归令当前覆盖合计519项；没有第二次重复运行完整套件。
- [集成检查](integration.log)：此前SR、服务和NUMA规划31项通过。
- [最终静态检查](static-final.log)：24个Python文件ruff、isort、Python 3.9语法通过；17个功能模块strict mypy通过。
- [文件清单](python-files.txt)保存本票Python文件范围。

## 红绿验证与修正

池公开接口先因不支持Index K单份配置失败，实现后验证32参与者视图。
16个P映射者看到一份Index K；P/D主KV分组和prefix私有区域分别验证。
模型写回、admit、fill接口先因缺少Index K写者分工失败，实现后内容检查通过。
单写者与可选双写者分别核对实际行数，Index K延迟写者阻止ready。
CLI先拒绝新验收模式，接入后确认8/8/1/16/16与105/8 GiB预算。

逻辑实验新增3题：冷请求、改变答案的命中、完全命中。
CPU验证模板token边界、独立答案拒绝、真实Main状态机九请求证据及缺失回执拒绝。
这些测试没有运行模型，不能证明实机逻辑精度已经通过。

评审发现全局开启Index K会让旧full-acceptance误带3题；回归先出现9≠6，
修复后仅session验收入口追加实验，旧完整验收保留6请求。
P的NUMA 0约束不施加于D；D反向节点分配验证通过。

归档日志只去除行尾空白，不改变测试输出内容。

## 命令与次数

```bash
PYTHONPATH=ascend-mempool-test/src:ascend-mempool-test/scripts:ascend-mempool-test/tests/unit \
  /private/tmp/k1-ticket05-fix-venv/bin/python -m unittest discover \
  -s ascend-mempool-test/tests/unit -v
```

红绿用例按公开接口定向运行；失败修正后定向复测。
完整套件只运行一次，权限阻断只补对应测试，最终新回归不触发全套重跑。
两轮硬件入口尚未运行，仍由用户执行并确认；性能与物理页驻留未验收。
