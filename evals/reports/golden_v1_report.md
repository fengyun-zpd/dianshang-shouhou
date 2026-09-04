# 黄金集评测报告

- 数据集版本：`golden-v1`
- 模型版本：N/A（当前无 LLM 运行时，确定性规则工作流）
- Prompt 版本：N/A
- 运行模式：本地内存仓储 + MemorySaver checkpoint + 模拟外部执行
- 合成数据边界：全部为固定随机种子合成数据，不代表真实企业收益

## 结果

- 用例总数：11｜通过：10｜失败：1
- 任务完成率：0.9091
- 意图准确率：1.0
- 必要澄清率：0.0909
- 引用正确率：1.0（校验 3 条查询，注入拦截=True）
- P50 耗时：15.0 ms｜P95 耗时：16.0 ms
- Token / 成本：N/A（无 LLM）

## 安全不变量（阻断问题，必须全 0）

| 不变量 | 违规数 |
| --- | --- |
| 越权成功 | 0 |
| 重复副作用 | 0 |
| 未知状态盲目重试 | 0 |
| 非法状态迁移 | 0 |

## 失败用例

- g10-repeat-request-idempotent（重复请求不重复副作用）：重复请求 outcome=refunded；repeat_outcome=refunded != 期望 already_executed

## RAG 抽查明细

- query='商品破损怎么处理' top_policy=P-DAMAGED-FULL expect=P-DAMAGED-FULL OK
- query='少件漏发怎么补' top_policy=P-MISSING-FULL expect=P-MISSING-FULL OK
- 注入查询拦截=OK
