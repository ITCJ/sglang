# A/C 语义客户端 review

脚本仓库基线：42a1511；候选只含 pd-disaggregation 内的 7 个文件。
Standards：硬规则 0，判断项 0。Spec：0 项发现。
用户明确要求 ticket01 式独立启动，所以三个服务由用户拥有、手动关闭。
客户端自动等待 ready、编码、发送 A/C、核对语义/命中/释放和保存原文。
无 SGLang 功能代码改动；不放宽 ticket03 全输出验收。
真实 tokenizer 和 NPU 输出仍待用户运行。
