# 运行模式 A/B 对照报告（V1.1：单 Agent vs three-agent vs four-role）

- 数据集版本：`golden-v1`（黄金集 11 条，固定种子合成数据）
- 模型版本：offline / rules-v1（三模式均为确定性规则实现，未连接真实 LLM）
- Prompt 版本：N/A（无 LLM 无 Prompt）
- online_llm：not_configured（未配 Key，不把离线当真实模型）
- 合成数据边界：全部为固定种子合成数据，不代表真实企业收益
- 运行方式：同一用例分别驱动 WorkflowRunner（single，policy_store 注入同检索面）、SupervisorRunner three-agent 与 four-role；审批/幂等/对账语义一致，金额/资格由领域服务裁决

## 结果

| 指标 | single（默认） | three-agent（历史兼容） | four-role（当前实验） |
| --- | --- | --- | --- |
| 任务完成率 | 1.0（11/11） | 1.0（11/11） | 1.0（11/11） |
| 平均耗时 / P50 / P95 | 仅 stdout（见运行输出） | 仅 stdout（见运行输出） | 仅 stdout（见运行输出） |
| Token / 成本 | N/A（无 LLM） | N/A（无 LLM） | N/A（无 LLM） |
| 越权成功 | 0 | 0 | 0 |
| 重复副作用 | 0 | 0 | 0 |
| unknown 换键重试 | 0 | 0 | 0 |
| 模型错误数 | 0 | 0 | 0 |
| 工具错误数 | 0 | 0 | 0 |

- outcome 三模式一致：11/11
- 退款金额三模式一致：11/11
- 意图一致（命中期望且互相同）：10/10
- 澄清一致（等待澄清零副作用）：1/11
- 转人工（escalated）计数：single=5，three=5，four=5
- 安全拒绝率：1.0（确定性检索同口径：旧版/不适用证据被正确拒绝，未当现行采信）
- 注入拦截：已拦截（确定性检索 INJECTION_DETECTED 拒绝；见既有 rag_checks）

## 引用一致性探针（确定性）

- g01-refund-happy：policy_citations 三模式一致=True（['P-DAMAGED-FULL@1#0', 'P-MISSING-FULL@1#0']），evidence_refs 一致=True
- g05-escalate-no-policy：三模式均无草稿、错误码一致（{'single': 'AFTER_SALES_POLICY_NOT_FOUND', 'three': 'AFTER_SALES_POLICY_NOT_FOUND', 'four': 'AFTER_SALES_POLICY_NOT_FOUND'}）

## 结论（ADR-002 回退条款）

无量化业务收益（三模式确定性通过率持平，同一领域裁决）：按 ADR-002 失败回退条款默认路径维持单 Agent；three-agent/four-role 保留为可选实验运行时（并行只读证据与四角色轨迹观测；多 Agent 增加角色串行耗时与复杂度）。

## 逐条明细

| case | single | three | four | single退款 | four退款 | outcome 一致 |
| --- | --- | --- | --- | --- | --- | --- |
| g01-refund-happy | refunded | refunded | refunded | 100.00 | 100.00 | 是 |
| g02-refund-rejected | rejected | rejected | rejected | 0.00 | 0.00 | 是 |
| g03-clarify-missing-order | clarify | clarify | clarify | 0.00 | 0.00 | 是 |
| g04-escalate-order-not-found | escalated | escalated | escalated | 0.00 | 0.00 | 是 |
| g05-escalate-no-policy | escalated | escalated | escalated | 0.00 | 0.00 | 是 |
| g06-escalate-conflict-policy | escalated | escalated | escalated | 0.00 | 0.00 | 是 |
| g07-escalate-unsupported-intent | escalated | escalated | escalated | 0.00 | 0.00 | 是 |
| g08-escalate-unknown-intent | escalated | escalated | escalated | 0.00 | 0.00 | 是 |
| g09-operation-unknown-recovery | operation_unknown | operation_unknown | operation_unknown | 100.00 | 100.00 | 是 |
| g10-repeat-request-idempotent | refunded | refunded | refunded | 100.00 | 100.00 | 是 |
| g11-forged-resume-safe | refunded | refunded | refunded | 100.00 | 100.00 | 是 |

> 诚实边界：三模式在确定性规则下正确路径一致（金额/资格/状态由领域服务裁决）；four-role 的模块化/轨迹观测价值不构成量化业务收益，默认路径按 ADR-002 维持单 Agent。
> 本报告为确定性产物（不含逐 run 耗时，跨运行零 diff；耗时仅输出 stdout/日志，不作为对照结论依据）。