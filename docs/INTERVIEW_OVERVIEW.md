# OpsPilot 面试交付物（单 Agent 可靠性工作流 + HTTP 生命周期）

> 面向面试的讲解材料：请求时序图、架构取舍、安全不变量报告与 5 分钟演示。
> 当前测试基线以 `TESTING_BASELINE.md` 为准；未实测内容如实标注"未实测/未实现"。
> 演示脚本：`scripts/demo_interview.py`（七场景）、`scripts/demo_agent_http.py`（HTTP 九场景），
> 两者均为真实执行，任意失败抛 AssertionError。

## 0. 一句话主张

> OpsPilot 证明 Agent 能在**证据不足、越权、重复请求、工具失败、外部结果未知和自身反复循环**时
> 安全停止、等待或转人工——金额、资格、状态迁移与审批永远由确定性领域服务裁决。

## 0.5 核心边界一句话（本版面试必讲）

> Agent 只负责理解请求、澄清信息、检索证据和组织流程。审批结果必须先写入领域事实源，
> decision HTTP 接口只触发工作流恢复，apply_decision 会重新读取带版本的审批事实。
> 金额、资格、状态、权限、幂等和审计仍由确定性领域服务负责。

对应实现：`POST /api/v1/agent/{thread_id}/decision` 的请求体**不允许**出现
`approved`/`rejected`/`decision`（出现即 422），服务端只提交占位恢复值 `"_continue_"`；
`apply_decision` 节点调用 `gateway.get_operation()` 读领域事实，按 `APPROVED / REJECTED /
EXECUTED / FAILED / UNKNOWN / PENDING` 分支；`PENDING` 时忽略本次恢复、路由回
`request_approval` 继续等待。`get_operation` 明确列入"永不去重/永不缓存"清单。

## 1. 请求时序图（主线：破损退款 + 人工审批，经 HTTP）

```text
调用方(HTTP)        单Agent(LangGraph)      只读工具/政策RAG     确定性领域服务      审批人      PostgreSQL事实源
 │ POST /agent/start │                       │                    │                  │            │
 │──────────────────>│ 意图识别(refund)      │                    │                  │            │
 │                   │ 缺订单号? → clarify interrupt（HTTP 返回 waiting_clarify）    │            │
 │ POST /clarify     │                       │                    │                  │            │
 │──────────────────>│ 补参后回到原线程       │                    │                  │            │
 │                   │ get_order / 历史工单  │──只读─────────────>│                  │            │
 │                   │ 政策检索(带citation)  │──RAG(search+注入检测)─>│(仅证据引用)    │            │
 │                   │ 领域金额计划          │───────────────────>│ 金额唯一来源      │            │
 │                   │ 建单+动作草稿+提交     │───────────────────>│ 写:权限/幂等/审计 │            │
 │                   │ approval interrupt    │                    │─────────────────>│ 草稿/PENDING│
 │                   │ （HTTP 返回 waiting_approval=true + operation_id）             │            │
 │                   │                       │                    │<──人工审批────────│ approve(带版本)
 │ POST /operations/{op}/approve（领域接口，Approver 身份）        │                  │            │
 │ POST /agent/{id}/decision（body {}，只触发恢复）│               │                  │            │
 │──────────────────>│ apply_decision 重读领域决定（非 body/resume 参数）             │            │
 │                   │ 领域事实为 PENDING → 忽略本次恢复，继续等待│                  │            │
 │                   │ execute(模拟外部)     │───────────────────>│ 容量CAS→executed  │            │
 │                   │ 外部超时→unknown      │───────────────────>│ 仅原operation对账 │            │
 │                   │ 关单+审计             │───────────────────>│                  │            │
 │ GET /agent/{id}/state（只读视图）                                 │                  │            │
 │<── refunded/closed + audit_event_ids + step_count ───────────────────────────────│ 事实已落库 │
```

关键语义：
- **checkpoint（SQLite/内存）只存流程恢复状态**；业务事实（工单/操作/审批/执行/审计）一律重读领域服务/PG。
- **审批决定是已提交且带版本号的领域事实**，Agent 不携带"通过/拒绝"语义，decision 只触发重读。
- **确定性证据检索基线（本地 RAG）只提供政策证据与解释（policy_id+version+citation）**；资格/金额由领域 `compute_refund_plan` 裁决。
- 外部结果未知 → `operation_unknown`：**只能以原 operation_id 对账**，禁止换键重试。
- 反复循环 → 步数上限触发 `AGENT_LOOP_DETECTED`：**转人工；触发后无新增领域写入、无退款执行**，
  终态写入持久 checkpoint（新实例仍可读），不依赖模型自觉。

## 2. 架构取舍说明

| 决策 | 为什么（取舍） |
| --- | --- |
| **默认单 Agent** | A/B 对照（`evals/compare_modes.py`，黄金集 11 条）single / three-agent / four-role 的通过率、outcome 与退款金额均 100% 一致，没有可证明的业务收益（ADR-002 回退条款）。多 Agent 只增加协调复杂度与故障面，不解决"确定性裁决"这一核心可靠性问题。 |
| **审批结论不进 HTTP body** | 若 decision 接口接受 `approved`，攻击面就变成"谁能调这个接口谁就能放行"。把"触发"与"决定"分开后，唯一能改变审批事实的路径是领域审批接口（APPROVER 身份 + 带版本 CAS），HTTP body、checkpoint、模型输出都无法放行。 |
| **Agent 生命周期并入 HTTP，而不是再包一层** | 接口全部转发到同一个 `WorkflowRunner`（`create_app(agent_runner=...)`），不复制任何业务逻辑；memory/pg 只替换后端与 runner。未装配 runner 时返回 503 而不是静默降级到领域接口——避免出现"第二套业务实现"。 |
| **金额/资格不交给 LLM** | 模型会幻觉：金额、政策资格、库存、状态是业务事实，幻觉不可接受。确定性领域服务用规则+数据库裁决，LLM 只做意图/澄清/话术等低风险语言任务（能力矩阵默认禁用 tool_calling/high_risk_draft）。 |
| **死循环保护用确定性计数，不用模型判断** | "让模型自己决定何时停"在退化时正是最不可靠的环节。节点包装器把 `step_count` 写进状态并对上限做硬判定，LangGraph `recursion_limit` 作第二道防线，两层都不经过任何模型。 |
| **工具去重账本只覆盖写工具** | 受控写按（工具名, 租户, thread_id, 参数摘要）复用首次结果；`get_operation` 这类事实重读明确豁免——缓存审批事实会直接破坏"以领域事实为准"的边界。领域幂等键仍是重复副作用的最终兜底。 |
| **RAG 不负责最终裁决** | 政策文档是"证据与解释"，可能过期/版本错/被注入；真实资格来自版本化、启用、tenant 内、生效期正确的 PolicyRule/PG 政策行。检索结果只进 `evidence_refs`/`order_summary.policy_citations`。 |
| **成本未配置就显示 N/A** | 用 0 记账会把"没配置价格"伪装成"免费"。输入/输出单价分别配置、分别计算，缺任一项则该项与总成本都是 `None`（报告 `N/A`）；离线规则 `tokens=0` 并显式说明不代表 0 成本。 |
| **暂不微调（LoRA/QLoRA/DPO）** | 微调需要高质量数据集与可量化基线；当前真实 LLM 未接入（无安全 Key），离线规则基线的可改进空间与失败样本集尚未系统建立。无 chosen/rejected 数据不做 DPO。 |
| **PG profile 强制 DB 租约** | 跨进程同时 resume 同线程会造成重复副作用。`workflow_threads` 表用原子 claim（lease_owner/lease_until/fingerprint）保证只有持约者能读 checkpoint/invoke；失约者抛 `ThreadLeaseError` 零副作用（测试断言：实例 A 持约未过期时实例 B 拒绝推进）。 |
| **线程唯一键是 (租户, 线程)** | 线程绑定的**事实源**是租户限定的 `workflow_threads` 行 + 持久 checkpoint，进程内字典只是便利缓存。这样进程重启后新实例能按同一键接管；同名线程在不同租户下天然隔离；错误租户与"不存在"返回**同一个** 404，不泄露存在性。 |
| **Agent 生命周期只服务内部坐席** | 客户自助入口意味着另一套身份、授权与话术边界。V1 先只做内部坐席（start/clarify 仅 AGENT、decision 仅 APPROVER/SYSTEM）；同租户客户调用一律 403。客户入口作为**规划能力**明确标注，不假装已实现。 |
| **外部执行结果不由调用者指定** | 如果 `start` 能带 `simulate_external`，调用者就能自己制造"外部成功/超时"。现在未知状态必须由 SYSTEM 角色的领域执行接口写入事实源，工作流只重读——HTTP 层无法伪造外部结果。 |
| **证据块先脱敏再计数与发送** | 注入检测通过≠可以原文外发。每个证据块先 `redact_pii`，再参与 token 预算计数与请求拼接，测试直接断言请求体与日志中不出现完整手机号/邮箱/身份证号。 |

## 3. 安全不变量报告（实测 0 违例）

> 当前依据：D 盘环境中的全量 `pytest tests/` 基线见 `TESTING_BASELINE.md`（本轮隔离 PG：
> 当前验收基线：PG 543 passed / 0 skipped；离线 494 passed / 49 skipped。更早的数字和历史 PG 黄金集报告
> `evals/reports/golden_v1_report.md` 只作为已有证据；面试前必须按当前提交重新运行。

| 不变量 | 要求 | 实测 | 验证位置 |
| --- | --- | --- | --- |
| 越权成功数 | 0 | **0** | 跨租户 tenant-first 门禁；Agent HTTP 跨租户 → **统一 404**（不泄露归属）；同租户客户调用 Agent 接口 → 403 |
| 跨租户存在性泄露 | 0 | **0** | 错误租户与"不存在"返回同一错误码，消息除线程名外逐字一致；响应体不含所属租户（PG live + e2e 断言） |
| 调用者伪造副作用参数 | 0 | **0** | `start`/`clarify`/`decision` 一律 `extra="forbid"`（tenant_id/金额/角色/审批结果/外部结果 → 422） |
| 重复副作用数 | 0 | **0** | 幂等三元组 (tenant,command_type,raw_key) 唯一；工具去重账本；`claim_thread` 指纹不可变 |
| 未知状态换键重试数 | 0 | **0** | `operation_unknown` 只以原 operation_id 对账；换新键被领域拒绝；循环压力下也不自动换键 |
| 非法状态迁移数 | 0 | **0** | rules 状态机 + DB CHECK + 版本 CAS |
| 审批伪造数 | 0 | **0** | decision 接口禁带审批字段（422）；`apply_decision` 只读领域事实；Agent 无 approver 写通道 |
| 死循环导致的副作用数 | 0 | **0** | 步数上限在节点体执行前判定；触发后**无新增领域写入、无退款执行**（审计与退款金额不变），终态入持久 checkpoint |
| 未配置成本被伪造成 0 | 0 | **0** | 单价缺失 → `None`；离线规则 `tokens=0 / cost=N/A` |
| 无 Key 时的联网请求 | 0 | **0** | `assert_safe_network` 构造即失败；影子评测与 Judge 断言网络替身不被调用 |
| 模型请求/日志中的完整 PII | 0 | **0** | 证据块脱敏后才计数与发送；测试直接断言请求体与日志无手机号/邮箱/身份证号 |
| （补充）提示注入放行 | 0 | **0** | 查询注入 → `POLICY_INJECTION_DETECTED` 拒绝转人工；文档投毒块排除不进 evidence |

## 3b. 跨进程恢复怎么讲（1 分钟）

> "线程不是内存里的字典，而是 `(tenant_id, thread_id)`：PG profile 下绑定事实来自租户限定的
> `workflow_threads` 行，流程状态来自固定路径的 SQLite checkpoint（`.runtime/checkpoints/
> opspilot-agent.sqlite`，不是 PID 临时文件）。所以实例 A 崩溃、进程退出、租约过期之后，
> 实例 B 用同一张库和同一个 checkpoint 就能读到 state、按领域事实恢复 decision 并继续执行。
> 同名线程在不同租户下互不冲突；错误租户读不到，而且返回的是与'线程不存在'完全一样的 404——
> 连存在性都不泄露。租约语义没有放松：A 持约未过期时，B 推进会被 409 拒绝。"

## 4. 5 分钟演示脚本

```powershell
. .\scripts\init_d_env.ps1
.venv\Scripts\python.exe scripts\demo_agent_http.py    # 先跑这个：HTTP 生命周期九场景
.venv\Scripts\python.exe scripts\demo_interview.py     # 再跑这个：领域闭环七场景
```

`demo_agent_http.py`（九步，逐步断言，输出中文状态面板）：
① `start` 进入审批等待；② 缺订单号 → `clarify` → 补参回原线程；③ APPROVER 经领域接口写入审批事实；
④ `decision` 空 body → `apply_decision` 重读 → `refunded`；⑤ `state` 只读视图；
⑥ 审批拒绝 → 无退款执行；⑦ 未知状态：SYSTEM 经领域执行接口写入 `timeout` → `decision` 重读
→ `operation_unknown`（仅原 `operation_id` 对账）；⑧ 身份与租户边界（客户 403、越权/额外字段
422、跨租户统一 404）；⑨ 反复恢复超步数上限 → `AGENT_LOOP_DETECTED`、触发后无新增写入。
每个场景都打印：thread_id / 当前状态 / 是否等待审批 / 是否等待澄清 / operation_id / outcome /
error_code / 是否新增退款副作用 / 审计事件数量。

`demo_interview.py`（七场景）：① 正常闭环；② 信息不足澄清；③ 无政策证据转人工；④ 审批拒绝
（无退款执行）；⑤ 外部 unknown 原键对账；⑥ 跨租户拒绝；⑦ 重复请求幂等。

## 5. 当前主架构（一张图）

```text
FastAPI  =  领域服务接口  +  Agent 生命周期接口（start / clarify / decision / state，仅内部坐席）
   │                              │
   │                     WorkflowRunner（单 Agent, policy_store 可选,
   │                       步数上限 + 工具去重账本；线程键 =(租户, 线程)）
   │                              │
   └── AfterSalesApplicationPort ←┴── MemoryAdapter(测试/演示) | PgCommandAdapter(pg profile)
             │                            │
       确定性领域规则(rules)         PgCommandService 八命令单事务(权限/金额/状态/幂等/审批/审计)
             │                            │
        PostgreSQL 业务事实源 ←───────────┘   线程绑定：workflow_threads(租户限定，PG)
                                              + 固定 SQLite checkpoint(仅流程状态)
```

工作台（`src/api/ui/`）的「案件处置 → Agent 全链路」面板真实调用这四个接口，逐项展示
thread_id / 是否等待审批 / 是否等待澄清 / next_action / operation_id / ticket_id / 审批草稿金额 /
outcome / error_code / step_count / 证据引用 / 审计编号，并提供「未知状态」「循环保护」
「跨租户拒绝」三个独立入口；页面只渲染真实返回值，不硬编码成功。

### 可选与实验能力（不改变默认单 Agent 主链路）

- **四角色只读多 Agent**（`src/agents/multiagent.py`）：`SupervisorRunner(orchestration="four-role")`
  = Triage → Evidence → Resolution → RiskReview，各角色独立 Pydantic io schema/role/工具白名单/
  trace_id，全部只读；Resolution 无金额字段（金额唯一来自领域 `compute_refund_plan`）；
  RiskReviewer 阻断跨租户引用/无效 citation/金额非领域来源。与单 Agent 同黄金集 A/B 一致（无收益）。
- **确定性证据检索基线展示**：`scripts/demo_rag_policy.py`。
- **真实 LLM 可选适配器**（`src/models`，仅环境变量配置）：**实现完成、离线验证、真实模型未实测**
  （需用户提供安全 Key + 白名单 Base URL）；无 Key 自动离线规则模式、零网络；输入/输出双向成本
  记账，未配置价格显示 `N/A`；内容守卫拦截金额/审批/状态/执行指令；证据块先脱敏再计数与发送。
- **LLM-as-Judge 评测**（`evals/llm_judge.py`）：**实现完成、离线验证、真实模型未实测**。只评估
  解释准确性/完整性/澄清话术/语气/引用一致性/边界说明等模糊质量，**不评估也不替代**金额、权限、
  审批、状态迁移、幂等与副作用（rubric 的 `out_of_scope` 显式列出）。默认离线规则裁判；真实裁判
  仅在「Key + 白名单 + 显式模型名」齐备时启用，走同一内容守卫与 PII 脱敏，`http_post` 可注入
  → 路径已用 mock 单测；只要没有未降级的真实调用结果，报告一律按"未实测"输出。
- **微调实验入口**（`scripts/gen_sft_samples.py`）：固定种子合成样本生成；无 GPU/Key 未训练，
  不声称任何效果（门禁见 `docs/MODEL_EVALUATION.md`）。

## 6. 是否建议继续增加功能

**不建议**。V1.2 收口后范围已冻结：不新增默认多 Agent、微调/DPO、pgvector、长期记忆、缓存平台、
真实支付/CRM 或生产部署；Judge 保持独立、只评话术质量，不扩建采样与校准平台。
面试时可以明确说"领域接口与 Agent 生命周期在同一个 FastAPI 应用里，接口只做转发、不复制业务逻辑；
线程以 (租户, 线程) 为唯一键，PG 下靠 workflow_threads + 固定 checkpoint 跨进程恢复"。
若继续，建议按价值排序：① 在获得安全 Key 后跑真实候选模型与真实 Judge 并回填指标；
② 多实例部署（各自 checkpoint 或服务端 checkpointer）与审计检索/限流等运营增量；
③ 为演示工作台补浏览器 e2e。任何新增都须先有 RED 测试与真实评测。
