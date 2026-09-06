# 单 Agent vs Supervisor 对照实验报告（阶段 5B / ADR-002）

- 数据集：`golden-v1`（黄金集 11 条，固定种子合成数据）
- 运行方式：同一用例分别驱动单 Agent（WorkflowRunner）与 Supervisor（SupervisorRunner，并行只读子 Agent：order/history/policy）

## 结果

| 指标 | 单 Agent（默认） | Supervisor |
| --- | --- | --- |
| 任务完成率 | 1.0（11/11） | 1.0（11/11） |
| 澄清一致（等待澄清且零副作用） | 1/11 | 1/11 |
| citation 一致（pass 对齐） | 11/11 | 11/11 |
| 旧版安全拒绝率 | 1.0 | 1.0 |
| 平均耗时 / P95 | 仅 stdout（见运行输出） | 仅 stdout（见运行输出） |
| Token / 成本 | N/A（无 LLM） | N/A（无 LLM） |
| outcome 一致 | 11/11 | — |
| 退款金额一致 | 11/11 | — |
| 越权成功 | 0 | 0 |
| 重复副作用 | 0 | 0 |
| unknown 换键重试 | 0 | 0 |

- outcome 一致性：11/11
- 退款金额一致性：11/11

## 结论（ADR-002 回退条款）

无明确业务收益（通过率与单 Agent 持平或更低），按 ADR-002 失败回退条款：默认路径维持单 Agent；Supervisor 保留为可选实验运行时（并行只读证据与未来多模型挂载点）。

## 逐条明细

| case | 单outcome | Sup outcome | 单退款 | Sup退款 |
| --- | --- | --- | --- | --- |
| g01-refund-happy | refunded | refunded | 100.00 | 100.00 |
| g02-refund-rejected | rejected | rejected | 0.00 | 0.00 |
| g03-clarify-missing-order | clarify | clarify | 0.00 | 0.00 |
| g04-escalate-order-not-found | escalated | escalated | 0.00 | 0.00 |
| g05-escalate-no-policy | escalated | escalated | 0.00 | 0.00 |
| g06-escalate-conflict-policy | escalated | escalated | 0.00 | 0.00 |
| g07-escalate-unsupported-intent | escalated | escalated | 0.00 | 0.00 |
| g08-escalate-unknown-intent | escalated | escalated | 0.00 | 0.00 |
| g09-operation-unknown-recovery | operation_unknown | operation_unknown | 100.00 | 100.00 |
| g10-repeat-request-idempotent | refunded | refunded | 100.00 | 100.00 |
| g11-forged-resume-safe | refunded | refunded | 100.00 | 100.00 |

> 诚实边界：确定性规则下两种模式的正确路径一致；Supervisor 的并行证据与模块化价值不构成此对比中的量化业务收益，默认路径按 ADR-002 维持单 Agent。
> 本报告为确定性产物（不含逐 run 耗时，跨运行零 diff；耗时仅输出到 stdout/日志，不作为对照结论依据）。