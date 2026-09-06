# OpsPilot 面试交付物（单 Agent 可靠性工作流）

> 面向面试的讲解材料：请求时序图、架构取舍、安全不变量报告与 5 分钟演示。
> 当前测试基线以 `TESTING_BASELINE.md` 为准；未实测内容如实标注"未实测/未实现"。
> 演示脚本：`scripts/demo_interview.py`（真实执行，任意失败抛 AssertionError）。

## 0. 一句话主张

> OpsPilot 证明 Agent 能在**证据不足、越权、重复请求、工具失败和外部结果未知**时安全停止、等待或转人工——金额、资格、状态迁移与审批永远由确定性领域服务裁决。

## 1. 请求时序图（主线：破损退款 + 人工审批）

```text
用户          单 Agent(LangGraph)        只读工具/政策RAG         确定性领域服务         审批人        PostgreSQL事实源
 │   "订单破损退款"  │                        │                       │                  │               │
 │─────────────────>│                        │                       │                  │               │
 │                  │ 意图识别(refund)        │                       │                  │               │
 │                  │ 缺订单号?  → 澄清interrupt                        │               │               │
 │ 补订单号 resume  │                        │                       │                  │               │
 │─────────────────>│                        │                       │                  │               │
 │                  │ get_order / 历史工单   │──只读─────────────────>│                  │               │
 │                  │ 政策检索(带citation)   │──RAG(search+注入检测)──>│(仅证据引用)      │               │
 │                  │ compute_refund_plan    │──────────────────────>│ 金额唯一来源       │               │
 │                  │ 创建动作草稿+工单        │──────────────────────>│ (写: 权限/幂等/审计) │              │
 │                  │ 高风险 → 审批interrupt  │                       │──────────────────>│  草稿/PENDING  │
 │                  │                        │                       │<──人工审批──────────│  approve(带版本)
 │ 恢复 resume      │                        │                       │                  │               │
 │─────────────────>│ apply_decision 重读领域 决定（非 resume 参数）    │                  │               │
 │                  │ execute(模拟外部)       │──────────────────────>│ 容量CAS→executed    │              │
 │                  │ 外部超时→unknown        │──────────────────────>│ 仅原operation对账   │              │
 │                  │ 关单+审计              │──────────────────────>│                  │               │
 │<── refunded/closed───────────────────────────────────────────────────────────────│ 事实已落库      │
```

关键语义：
- **checkpoint（SQLite）只存流程恢复状态**；业务事实（工单/操作/审批/执行/审计）一律重读领域服务/PG。
- **审批决定是已提交且带版本号的领域事实**，Agent 不携带"通过/拒绝"语义，resume 只触发重读。
- **确定性证据检索基线（本地 RAG）只提供政策证据与解释（policy_id+version+citation）**；资格/金额由领域 `compute_refund_plan` 裁决。
- 外部结果未知 → `operation_unknown`：**只能以原 operation_id 对账**，禁止换键重试。

## 2. 架构取舍说明

| 决策 | 为什么（取舍） |
| --- | --- |
| **默认单 Agent** | A/B 对照（`evals/compare_agents.py`，黄金集 11 条）Supervisor 与单 Agent 的通过率、outcome 与退款金额均 100% 一致；对照不以耗时作结论，因此没有可证明的业务收益（ADR-002 回退条款）。多 Agent 只增加协调复杂度与故障面，不解决"确定性裁决"这一核心可靠性问题。 |
| **金额/资格不交给 LLM** | 模型会幻觉：金额、政策资格、库存、状态是业务事实，幻觉不可接受。确定性领域服务用规则+数据库裁决，LLM 只做意图/澄清/话术等低风险语言任务（能力矩阵默认禁用 tool_calling/high_risk_draft）。 |
| **RAG 不负责最终裁决** | 政策文档是"证据与解释"，可能过期/版本错/被注入；真实资格来自**版本化、启用、tenant 内、生效期正确的 PolicyRule/PG 政策行**。检索结果只进 `evidence_refs`/`order_summary.policy_citations`，金额永远来自 `compute_refund_plan`。 |
| **暂不微调（LoRA/QLoRA/DPO）** | 微调需要高质量数据集与可量化基线；当前真实 LLM 未接入（无安全 Key），离线规则基线的可改进空间与失败样本集尚未系统建立。无 chosen/rejected 数据不做 DPO；动态价格/政策/库存不能写进模型参数（宪法第八条）。 |
| **checkpoint 不保存业务真相** | checkpoint 可丢失/损坏/被伪造视图注入（有 fail-fast 与注入防护测试）。把金额/审批/执行放 checkpoint 会让"流程状态"成为第二事实源，违背 PG 唯一事实源。恢复一律重读领域服务：同幂等键同载荷仍返回原结果，不重复副作用。 |
| **PG profile 强制 DB 租约** | 跨进程同时 resume 同线程会造成重复副作用。`workflow_threads` 表用原子 claim（lease_owner/lease_until/fingerprint）保证只有持约者能读 checkpoint/invoke；失约者抛 `ThreadLeaseError` 零副作用。 |

## 3. 安全不变量报告（实测 0 违例）

> 当前依据：D 盘环境中的全量 `pytest tests/` 基线见 `TESTING_BASELINE.md`。历史 PG 黄金集报告
> `evals/reports/golden_v1_report.md` 只作为已有证据；面试前必须按当前提交重新运行，不能用旧报告替代本次实测。

| 不变量 | 要求 | 实测 | 验证位置 |
| --- | --- | --- | --- |
| 越权成功数 | 0 | **0** | 跨租户 tenant-first 门禁（memory 403 TENANT_MISMATCH / PG 404 NOT_FOUND）；D3 客户只能读自己工单；演示 3 跨租户 T2→T1 拒绝 |
| 重复副作用数 | 0 | **0** | 幂等三元组 (tenant,command_type,raw_key) 唯一；`claim_thread` fp 不可变；演示 4 重复请求 execute 审计仅 1 条 |
| 未知状态换键重试数 | 0 | **0** | `operation_unknown` 只以原 operation_id 对账；演示 5 原键对账 success 落账 |
| 非法状态迁移数 | 0 | **0** | rules 状态机 + DB CHECK + 版本 CAS（并发 approve/execute 恰一成功） |
| 审批伪造数 | 0 | **0** | approve/reject 的 `decided_by=认证 principal`；Agent 无 approver 写通道；伪造 resume 被忽略（g11） |
| （补充）提示注入放行 | 0 | **0** | 查询注入 → `POLICY_INJECTION_DETECTED` 拒绝转人工；文档投毒块排除不进 evidence（9 项单 Agent RAG 测试） |

## 4. 5 分钟演示脚本

```powershell
.venv\Scripts\python.exe scripts\demo_interview.py
```

固化五核心场景（真实执行、逐步断言，任意失败抛 AssertionError）：
① 正常闭环：破损退款 → 审批 approve → resume → refunded 100.00 / closed / 7 审计；
② 信息不足：缺订单号 → 澄清 interrupt（不猜测金额/政策）；
③ 无政策证据：无适用 PolicyRule → 领域 `AFTER_SALES_POLICY_NOT_FOUND` → 转人工，
   不虚构政策/金额、零副作用（无草稿/审计/退款）；
④ 审批拒绝：授权人 reject → resume → outcome=rejected、操作 rejected、退款 0.00、无 execute 审计；
⑤ 外部结果未知：外部超时 → operation_unknown（金额 0）→ 仅原 operation_id 对账 success → executed/100.00。
补充安全演示：⑥ 跨租户 T2 读 T1 → escalated 零副作用；⑦ 重复请求 → 原结果、execute 审计仅 1 条。
运行一次约数秒，输出含每步状态断言，适合面试现场演示或录屏讲解。

## 5. 当前主架构（一张图）

```text
FastAPI(memory/pg profile：领域服务接口)  +  WorkflowRunner(脚本/回放入口，单 Agent, policy_store 可选)
   │                              │
   └── AfterSalesApplicationPort ←┴── MemoryAdapter(测试/演示) | PgCommandAdapter(pg profile)
             │                            │
       确定性领域规则(rules)         PgCommandService 八命令单事务(权限/金额/状态/幂等/审批/审计)
             │                            │
        PostgreSQL 业务事实源 ←───────────┘        SQLite = LangGraph checkpoint(仅流程状态)
```

### V1.1 新增（实验性/可选，不改变默认单 Agent 主链路）

- **四角色只读多 Agent**（`src/agents/multiagent.py`）：`SupervisorRunner(orchestration="four-role")`
  = Triage → Evidence → Resolution → RiskReview，各角色独立 Pydantic io schema/role/工具白名单/
  trace_id，全部只读；Resolution 无金额字段（金额唯一来自领域 `compute_refund_plan`）；
  RiskReviewer 阻断跨租户引用/无效 citation/金额非领域来源；证据不足/角色失败/注入 → 转人工。
  与单 Agent 同黄金集子集 A/B 一致（无收益 → 默认仍单 Agent）。
- **确定性证据检索基线展示**：`scripts/demo_rag_policy.py`（导入/分块/启停/版本切换/注入拒绝/
  引用校验，真实执行）。
- **真实 LLM 可选适配器**（`src/models`，仅环境变量配置）：无 Key 自动离线规则模式；内容守卫
  拦截金额/审批/状态/执行指令；**真实模型未接入、未实测**（需用户提供安全 Key + 白名单
  Base URL）。
- **微调实验入口**（`scripts/gen_sft_samples.py`）：固定种子合成样本生成；无 GPU/Key 未训练，
  不声称任何效果（门禁见 `docs/MODEL_EVALUATION.md`）。

## 6. 是否建议继续增加功能

**不建议**在现有主线继续堆叠（RAG 增强、多 Agent、微调、长期记忆均不带来已证实的可靠性收益）。当前 API 与 Agent 运行器尚未合并为一条 HTTP 主链路，面试时必须按两个入口准确表述；除非先补齐 HTTP e2e，否则不要声称 API 已覆盖 Agent 恢复。
若继续，建议按价值排序：① 审批工作台前端（把已齐备的 API 面用起来，需浏览器 e2e）；
② 真实 LLM 影子评测（需用户提供安全 Key/允许 Base URL）以建立失败样本集；
③ 审计检索/限流/CORS/OpenAPI 示例等运营观测（低风险增量）。任何新增都须先有 RED 测试与真实评测，不引入未经证实的"技术名词"。
