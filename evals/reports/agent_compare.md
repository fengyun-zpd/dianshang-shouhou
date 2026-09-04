# 单 Agent vs Supervisor 对照实验报告（阶段 5B / ADR-002）

- 数据集：`golden-v1`（黄金集 11 条，固定种子合成数据）
- 运行方式：同一用例分别驱动单 Agent（WorkflowRunner）与 Supervisor（SupervisorRunner，并行只读子 Agent：order/history/policy）

## 结果

| 模式 | 通过 | 通过率 | P50 耗时(ms) | P95 耗时(ms) |
| --- | --- | --- | --- | --- |
| 单 Agent（默认） | 11 | 1.0 | 16.0 | 32.0 |
| Supervisor | 11 | 1.0 | 15.0 | 16.0 |

- outcome 一致性：11/11
- 退款金额一致性：11/11

## 结论（ADR-002 回退条款）

无明确业务收益（通过率与单 Agent 持平或更低，/或耗时相当），按 ADR-002 失败回退条款：默认路径维持单 Agent；Supervisor 保留为可选实验运行时（并行只读证据与未来多模型挂载点）。

## 逐条明细

| case | 单outcome | Sup outcome | 单退款 | Sup退款 | 单ms | Supms |
| --- | --- | --- | --- | --- | --- | --- |
| g01-refund-happy | refunded | refunded | 100.00 | 100.00 | 32.0 | 15.0 |
| g02-refund-rejected | rejected | rejected | 0.00 | 0.00 | 16.0 | 15.0 |
| g03-clarify-missing-order | clarify | clarify | 0.00 | 0.00 | 16.0 | 16.0 |
| g04-escalate-order-not-found | escalated | escalated | 0.00 | 0.00 | 15.0 | 16.0 |
| g05-escalate-no-policy | escalated | escalated | 0.00 | 0.00 | 16.0 | 15.0 |
| g06-escalate-conflict-policy | escalated | escalated | 0.00 | 0.00 | 16.0 | 15.0 |
| g07-escalate-unsupported-intent | escalated | escalated | 0.00 | 0.00 | 16.0 | 16.0 |
| g08-escalate-unknown-intent | escalated | escalated | 0.00 | 0.00 | 15.0 | 16.0 |
| g09-operation-unknown-recovery | operation_unknown | operation_unknown | 100.00 | 100.00 | 16.0 | 15.0 |
| g10-repeat-request-idempotent | refunded | refunded | 100.00 | 100.00 | 31.0 | 16.0 |
| g11-forged-resume-safe | refunded | refunded | 100.00 | 100.00 | 16.0 | 15.0 |

> 诚实边界：确定性规则下两种模式的正确路径一致；Supervisor 的并行证据与模块化价值不构成此对比中的量化业务收益，默认路径按 ADR-002 维持单 Agent。