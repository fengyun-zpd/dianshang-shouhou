# 电商售后多角色编排实验记录（ADR-002）

> 归属：个人开发的电商售后数字化工具 OpsPilot。文档更新：2026-09-16；下方实验记录日期：2026-09-04。
> 目的：按 ADR-002 的方法验证"Supervisor + 只读子 Agent"是否带来可量化收益；无收益则回退单 Agent。

## 1. 架构差异（Supervisor vs 单 Agent）

| 维度 | 单 Agent（默认路径） | Supervisor（实验运行时） |
| --- | --- | --- |
| 顶层状态机 | 同一 LangGraph 图（AgentState 13 必含字段、审批 interrupt/resume、checkpoint） | **相同**（图只替换一个证据节点） |
| 证据编排 | `gather_evidence` 单节点直接查询 | `supervise_evidence`：Supervisor 并行调度 **order-agent / history-agent / policy-agent** 三个只读子 Agent |
| 子 Agent | — | 各自**只读工具白名单**（`ReadOnlySubAgent`），带 TenantContext，经 ToolRegistry 权限/Schema/超时/审计 |
| 写路径 | 领域服务 + 人工审批 | **仅 Supervisor 顶层**（子 Agent 无任何写工具，注册表断言全只读） |
| 业务真相 | 领域服务 | 领域服务（checkpoint 仅流程状态） |

子 Agent 定义：`src/agents/subagents.py`（`ORDER_AGENT_SPEC` / `HISTORY_AGENT_SPEC` / `POLICY_AGENT_SPEC`）；Supervisor 运行器：`src/agents/supervisor.py`（`SupervisorRunner`，API 与 `WorkflowRunner` 相同）。

## 2. 安全边界（子 Agent 不得越权）

- 白名单外工具调用 → `PERMISSION_DENIED`（测试断言）；
- 注册表内**不允许**存在非只读工具（构造即校验，测试断言）；
- 跨租户订单 → 域错误透传 → 转人工，零副作用（测试断言）；
- 审批 interrupt / 拒绝 / 重复 resume 幂等 / `operation_unknown` 原键对账 / 伪造恢复被拒：与单 Agent 语义一致（测试断言 11 项）。

## 3. 对照实验方法

`evals/compare_agents.py`：同一黄金集（golden-v1，11 条）逐条分别驱动
单 Agent（`WorkflowRunner`）与 Supervisor（`SupervisorRunner` + 政策文档存储），
自动提交用例审批决定并 resume；比较 outcome、退款金额和通过率。

```powershell
.venv\Scripts\python.exe evals\compare_agents.py
```

报告：`evals/reports/agent_compare.md`。

## 4. 结果（2026-09-04，本机实测）

| 指标 | 单 Agent | Supervisor |
| --- | --- | --- |
| 通过率 | 11/11 | 11/11 |
| outcome 一致 | — | 11/11 |
| 退款金额一致 | — | 11/11 |

## 5. 结论（ADR-002 回退条款）

确定性规则下，Supervisor 与单 Agent **正确率持平**，未带来可量化的
业务收益（完成率/正确性/金额均一致）。按 ADR-002："拆分失败时回退到 supervisor
的单 Agent 路径"：

- **默认路径维持单 Agent**（业务闭环、审批、评测均不受影响）；
- **Supervisor 保留为可选实验运行时**：提供并行只读证据编排、子 Agent 模块化与
  未来多模型/子 Agent 扩展接口，供有明确目标的业务更新尝试使用；
- 后续若引入真实 LLM 子 Agent 路由并出现指标差异，重跑本实验；有正收益再切换默认
  并提交 ADR 更新。

## 6. 诚实边界与修订

- 两种模式均为确定性规则实现（无 LLM）；"收益"仅指本对照可量化的业务指标；
- 对照报告不将运行耗时作为结论指标，不能声明性能持平或改善；
- （2026-09-04）—— 首版：架构差异、安全边界、对照方法、结果与回退结论。
- （2026-09-05）—— 删除未被确定性对照报告支持的耗时结论。
