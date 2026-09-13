# OpsPilot

> 企业售后工单处置 Agent 与可靠性评测项目。
> 版本：V1.2 Release Candidate（2026-09-13）。

## 项目使命

构建可面试展示、可复现的**单 Agent 售后退款可靠性工作流**：证明 Agent 能在证据不足、越权、
重复请求、工具失败和外部结果未知时安全停止、等待或转人工；金额、资格、状态、审批与最终
事实永远由确定性领域服务 + PostgreSQL 裁决。

## V1.2 主张

OpsPilot 证明 Agent 可以在受控权限内、经 **HTTP 生命周期接口**组织一条退款工单流程，并在
证据不足、越权、重复请求、工具失败、外部结果未知和自身反复循环时安全停止、等待或转人工。

- 单 Agent 负责意图、澄清、只读证据检索和方案解释。
- 确定性领域服务负责金额、资格、状态、权限、幂等、并发与审计。
- 高风险动作先生成草稿，只有授权人员提交审批决定后才可执行。
- **审批结论必须先写入领域事实源；HTTP `decision` 接口只触发恢复，`apply_decision` 重读带版本的审批事实。**
- PostgreSQL 是业务事实源；SQLite/内存 checkpoint 只保存流程恢复位置。
- **确定性证据检索基线**（本地 PolicyStore 检索：租户隔离、版本有效、引用定位、注入拒绝、无证据转人工）在 Agent 回放中提供政策 citation；不能裁决金额、资格或状态，也不能替代 PostgreSQL 事实。

## HTTP Agent 生命周期（V1.2 新增，已实现/已测试/已验证）

Agent 启动、澄清与恢复不再是"另一套入口"：`create_app(..., agent_runner=...)` 把当前装配的
`WorkflowRunner` 暴露为 HTTP。`scripts/run_api.py --backend memory` 创建内存运行器（**进程内
合成演示**，重启即重置）；`--backend pg` 复用同一 PG 运行器（真实 PostgreSQL 后端 +
`.runtime/checkpoints/opspilot-agent.sqlite` 固定 checkpoint + D9 线程租约，**跨进程可恢复**）。
未装配运行器时接口返回 `503 AGENT_RUNNER_UNAVAILABLE`，不静默降级、不伪造结果。

```text
POST /api/v1/agent/start               启动（仅内部坐席 AGENT）
POST /api/v1/agent/{thread_id}/clarify 澄清补参（仅澄清中断状态）
POST /api/v1/agent/{thread_id}/decision 触发恢复（仅 APPROVER / SYSTEM，body 可为 {}）
GET  /api/v1/agent/{thread_id}/state   只读线程视图（AGENT / APPROVER / SYSTEM）
```

| 接口 | 角色 | 请求要点 | 关键边界 | 失败路径 |
| --- | --- | --- | --- | --- |
| `start` | **仅 AGENT** | `message` / `thread_id?` / `order_id_hint?` | `extra="forbid"`：租户、金额、角色、审批结果、外部结果字段 → 422；租户只取认证身份 | 同 (租户, thread) 异请求 → 409 `AGENT_THREAD_CONFLICT` |
| `clarify` | **仅 AGENT** | `order_id` / `description` / `message` 至少一个 | `extra="forbid"`：不能借澄清改租户/金额/审批状态/权限；非澄清状态 → 409 | 空载荷 → 422 |
| `decision` | APPROVER / SYSTEM | body 可为 `{}` | **不携带** `approved`/`rejected`/`decision`（携带 → 422）；只触发 `resume("_continue_")` | 非审批等待状态 → 409 `AGENT_NOT_WAITING_APPROVAL`；Agent 调用 → 403 |
| `state` | AGENT / APPROVER / SYSTEM | 无请求体 | 线程唯一键 = `(tenant_id, thread_id)` | 不存在**或**错误租户 → 统一 404 `AGENT_THREAD_NOT_FOUND`（不暴露所属租户） |

V1 的 Agent 生命周期**只服务内部坐席**：同租户客户调用上述任一接口都会得到 403
（`AFTER_SALES_PERMISSION_DENIED`）。**客户自助入口属规划能力，当前不开放**。

外部执行结果**不由 HTTP 调用者指定**：`start` 不接受 `simulate_external`/`external_result`；
未知状态演示必须由授权 SYSTEM 调用 `/api/operations/{id}/execute` 写入 `timeout`（领域事实变
`unknown`），再由 `decision` 重读领域事实。runner 内部仍保留 `simulate_external` 参数，
**仅用于测试与合成演示**（HTTP 层不可达）。

调用顺序（正常闭环）：

```text
1) POST /api/v1/agent/start                    → waiting_approval=true，返回 operation_id
2) POST /api/operations/{operation_id}/approve → 授权人把审批事实写入领域事实源（带版本）
3) POST /api/v1/agent/{thread_id}/decision     → body {} → apply_decision 重读事实 → 执行
4) GET  /api/v1/agent/{thread_id}/state        → 只读视图（outcome / audit_event_ids / step_count）
```

> 面试讲解要点：Agent 只负责理解请求、澄清信息、检索证据和组织流程。审批结果必须先写入领域
> 事实源，decision HTTP 接口只触发工作流恢复，apply_decision 会重新读取带版本的审批事实。
> 金额、资格、状态、权限、幂等和审计仍由确定性领域服务负责。

### 跨进程恢复（PG profile）

线程绑定的事实源是**租户限定的 `workflow_threads` 行 + 持久 checkpoint**，进程内字典只是
便利缓存。因此实例 A 中断后进程退出、租约过期，实例 B 用同一 PostgreSQL 与同一 checkpoint
即可按 `(tenant_id, thread_id)` 读 `state`、恢复 `decision` 并继续执行；同名线程在不同租户下
互不冲突（checkpoint 键空间为 `tenant:thread`）。实测见
`tests/integration/test_agent_restart_recovery_live.py`（PG live，3 项）。内存 profile 不声称
持久恢复。

### 确定性保护（V1.2 新增）

- **步数上限**：每个节点进入时 `step_count` 递增，默认上限 32（可构造参数覆盖）。超出即抛
  `AgentLoopDetected` → `error_code=AGENT_LOOP_DETECTED`、`outcome=escalated`、回复明确说明
  已转人工；**触发后无新增领域写入、无退款执行**（被拦截节点体不执行）。终态标记写入
  **持久 checkpoint**，进程重启后的新实例仍能读到 `AGENT_LOOP_DETECTED` 与人工接管原因；
  检测前已形成的草稿与审批事实原样保留。
- **第二道防线**：LangGraph `recursion_limit`（`max_steps*2+10`）兜住单次 invoke 内的超级步失控。
- **工具去重账本**：键 =（工具名, 租户, thread_id, 参数摘要）。同线程同工具同参数重复调用复用
  首次结果；`get_operation`（审批事实重读）**永不缓存**。领域幂等键仍是重复副作用的最终兜底。
- **不依赖模型自觉**：以上全部是确定性代码路径，与 LLM 是否启用无关。

## V1.2 范围

保留单 Agent LangGraph（默认路径）、确定性证据检索基线、PostgreSQL 命令路径、黄金集、回放与演示。
Supervisor 只保留 A/B 实验结论：与单 Agent 无量化业务收益，因此默认不用；V1.2 提供**四角色只读
多 Agent 可选编排**（Triage/Evidence/Resolution/RiskReview，`SupervisorRunner(orchestration="four-role")`），
每角色轨迹（trace_id/输入输出摘要/耗时/工具调用/citation/拒绝原因）经
`scripts/demo_supervisor_trace.py` 可复现查看；`evals/compare_modes.py` 的当前结论看 single/four-role。
运行模式开关见 `src/agents/modes.py`：`single_agent`（**默认**）/`multi_agent`（四角色实验）/
`offline_rule`（无 LLM Key 自动离线规则）/`llm`（仅显式配置安全 Key + 白名单 Base URL 才可用，
未配置安全回落 `offline_rule`）。

项目内的 `src/api/ui/` 是本地合成数据演示工作台，用于展示领域闭环、Agent Lab 轨迹和评测边界；
它不是生产运营后台，也没有接入真实身份系统或真实外部副作用。

不纳入 V1.2：真实 LLM 指标（未实测）、真实微调效果（仅实验入口/合成样本，无 GPU 未运行）、
真实 Mule/MCP、生产级前端身份与部署、pgvector、长期记忆和生产部署。

```text
请求 -> 订单/租户核验 -> 政策证据与澄清 -> 规则计算 -> 动作草稿
     -> 人工审批 interrupt -> 重读事实 resume -> 执行或 operation_unknown
     -> 原 operation_id 对账 -> 关单与审计
```

## 能力状态（严格区分口径）

| 能力 | 已实现 | 已测试 | 已验证（本机实测） | 未实测 | 可选 | 规划 |
| --- | --- | --- | --- | --- | --- | --- |
| 单 Agent 退款闭环（澄清/证据/审批恢复/unknown 对账） | ✅ | ✅ | ✅ memory 黄金集 11/11 | — | — | — |
| Agent 生命周期 HTTP（start/clarify/decision/state） | ✅ | ✅ 29 项 e2e + 5 项 PG live | ✅ memory 与 PG profile | — | — | — |
| 跨进程重启恢复（PG + 固定 checkpoint） | ✅ | ✅ 3 项 PG live | ✅ 隔离库实测 | — | — | — |
| 死循环与工具去重保护（终态入 checkpoint） | ✅ | ✅ 16 项 | ✅ memory | — | — | — |
| 确定性领域服务（金额/资格/状态/幂等/审计） | ✅ | ✅ | ✅ | — | — | — |
| PostgreSQL profile（命令事务/审批事实/租约/API 装配/Agent 主链路） | ✅ | ✅ | ✅ 539 passed 全量（隔离库） | — | — | — |
| 确定性证据检索基线（本地 RAG） | ✅ | ✅ | ✅ `demo_rag_policy` | — | — | — |
| LLM 成本记账（输入/输出双向 + N/A 语义） | ✅ | ✅ 21 项 | ✅ 离线路径 | 真实模型成本 | — | — |
| 真实 LLM 候选模式（白名单/显式模型名/降级） | ✅ 实现完成 | ✅ | 离线验证 | ✅ **真实模型未实测**（无安全 Key，零网络请求） | ✅ | — |
| LLM-as-Judge 评测（话术质量，不参与业务裁决） | ✅ 实现完成 | ✅ 19 项 | 离线验证（规则裁判） | ✅ **真实裁判未实测** | ✅ | — |
| 四角色只读多 Agent | ✅ | ✅ | ✅ A/B 无收益 | 真实 LLM 子 Agent | ✅ 实验 | — |
| **客户端自助入口** | — | — | — | — | — | ✅ 规划能力（当前仅内部坐席） |
| 微调（LoRA/QLoRA/DPO） | 仅样本生成入口 | — | — | ✅ 未训练 | — | ✅ |
| 生产部署 / 真实支付 / CRM / 企业微信 | — | — | — | — | — | ✅ 不属于本项目 |

## 当前验证

2026-09-13，在 D 盘运行时环境实测（`.venv` home=D:\Anaconda，Python 3.12.4）：

```powershell
. .\scripts\init_d_env.ps1
# 运行模式 A（离线，不设置 OPSPILOT_TEST_DATABASE_URL）：PG live 破坏性集成按纪律 skip
.venv\Scripts\python.exe -m pytest tests -q
# 490 passed, 49 skipped, 1 warning（本次实测，10.6 s）

# 运行模式 B（隔离 PG）：空库迁移至 0005 后跑全量，PG live 全部实测
$env:OPSPILOT_TEST_DATABASE_URL = 'postgresql+psycopg2://opspilot:opspilot@127.0.0.1:5433/opspilot_test_final_6003af47'
.venv\Scripts\python.exe -m pytest tests -q
# 539 passed, 0 skipped, 1 warning（本次实测，30.1 s）

# 隔离双跑脚本（自动创建两套带随机后缀的测试库）
.\scripts\run_pg_tests_isolated.ps1
# opspilot_test_a_74ae988e / opspilot_test_b_74ae988e 各 25 passed；ISOLATED DOUBLE-RUN PASS

.venv\Scripts\python.exe scripts\demo_agent_http.py             # HTTP 生命周期九步（含 404/422/循环）
.venv\Scripts\python.exe scripts\demo_interview.py              # 七场景（五核心 + 跨租户/重复请求）
.venv\Scripts\python.exe scripts\demo_supervisor_trace.py --mode multi_agent   # 四角色轨迹 8/8
.venv\Scripts\python.exe evals\replay.py --dataset golden_v1     # 11/11
.venv\Scripts\python.exe evals\compare_modes.py                  # single/three-agent/four-role 11/11 持平
.venv\Scripts\python.exe evals\run_model_shadow_eval.py --mode offline    # tokens=0, cost=N/A
.venv\Scripts\python.exe evals\run_model_shadow_eval.py --mode candidate  # 未实测；零网络请求
.venv\Scripts\python.exe evals\run_llm_judge.py --mode offline            # 离线规则裁判（真实裁判未实测）
.venv\Scripts\python.exe evals\run_llm_judge.py --mode judge              # 未配置安全环境 → 安全降级，零网络
.venv\Scripts\python.exe scripts\run_api.py --backend memory --port 8080  # 工作台六条路径人工验证
node --check src/api/ui/workspace.js                             # 通过
git diff --check                                                 # 通过
```

未设置隔离库时的 49 个跳过项只表示该运行模式未启用 PG live 测试，不能取代模式 B 的 PG 验证。
唯一警告来自 Starlette/AnyIO 的第三方弃用提示，不影响通过结论。数据为固定种子合成数据，
不代表真实企业收益；**真实模型、微调效果、生产连接和性能均未实测**。破坏性 PG 集成只允许
`opspilot_test_*`@localhost（`OPSPILOT_TEST_DATABASE_URL`），共享主库永不被 DROP
（见 `src/platform/pg_test_guard.py`）。

> 报告落盘约定：canonical 报告（`shadow_eval_offline.md`、`judge_offline.md`、黄金集/对照报告）
> 写入 `evals/reports/`；**未实测的候选模式报告写入 `.runtime/reports/`**（D 盘运行时目录，
> 不入库），避免把离线降级写成真实候选模型成绩。单元测试一律使用 `tmp_path`，不得改写
> `evals/reports/`。

## D 盘约束

`.venv` 和基础解释器位于 D 盘。运行前执行 `scripts/init_d_env.ps1`，它只为当前 PowerShell 设置
`TEMP`、`TMP`、pytest 临时目录、pip 缓存和 Python 字节码缓存，统一写入项目 `.runtime/` 与
`.cache/`。禁止向 C 盘安装、下载、缓存或写项目运行时文件。

## 演示与可复现命令

```powershell
. .\scripts\init_d_env.ps1
.venv\Scripts\python.exe scripts\demo_interview.py      # 七场景（五核心 + 跨租户/重复请求）
.venv\Scripts\python.exe scripts\demo_agent_http.py     # HTTP 生命周期九场景
.venv\Scripts\python.exe scripts\demo_rag_policy.py     # 确定性证据检索基线展示
.venv\Scripts\python.exe scripts\demo_modes.py          # 运行模式开关冒烟（含 tokens/cost 记账）
```

启动本地中文工作台：

```powershell
.venv\Scripts\python.exe scripts\run_api.py --host 127.0.0.1 --port 8080
# 浏览器打开 http://127.0.0.1:8080/
```

案件处置页的"安全剧本"会调用真实受保护 API，提供四个可重复的面试演示：审批拒绝、外部
结果未知后按原操作对账、同幂等键重复请求、Agent 越权审批拒绝。每次剧本开始前仅对 memory
合成后端重置演示数据；该 reset 路由**在 PostgreSQL profile 下不注册**，也不触碰真实业务数据。

切换到 Agent Lab 后，"Agent 何时必须安全停止"提供四个隔离边界剧本：信息不足时澄清、政策
不匹配时转人工、跨租户订单拒绝、手机号/邮箱/身份证脱敏。结果保留真实工作流 outcome 或领域
错误码，并明确显示零退款副作用；页面的检索区还可演示引用校验与提示注入拒绝。

七场景（真实断言）：① 正常破损退款闭环；② 缺订单号 → 澄清；③ 无政策证据 → 转人工不猜测；
④ 审批拒绝 → 无退款执行；⑤ 外部 unknown → 仅原键对账；⑥ 跨租户拒绝；⑦ 重复请求幂等返回原结果。
HTTP 九步（`demo_agent_http.py`，真实断言）：start / clarify / 写入审批事实 / decision 空 body /
state / 审批拒绝 / 未知状态（SYSTEM 写 timeout → decision 重读）/ 身份与租户边界（客户 403、
越权字段 422、跨租户 404）/ 循环保护。

工作台「案件处置 → Agent 全链路」面板真实调用上述四个接口，逐项展示 thread_id、是否等待审批/
澄清、next_action、operation_id、ticket_id、审批草稿金额、outcome、error_code、step_count、
证据引用与审计编号；并提供「未知状态」「循环保护」「跨租户拒绝」三个独立入口。

评测：`evals\replay.py --dataset golden_v1`（11/11）、`evals\compare_modes.py`（单 Agent vs
四角色 A/B，无收益维持单 Agent）、`evals\rag_metrics.py`、
`evals\run_model_shadow_eval.py --mode offline|candidate`（含 token 与成本列）、
`evals\run_llm_judge.py --mode offline|judge`（模糊质量 Judge，不替代确定性验收）。

## 面试版项目介绍（90 秒）

> "OpsPilot 是企业售后的确定性可靠性 Agent：单 Agent 只做意图、澄清与只读证据检索；
> 金额、资格、状态、审批、幂等与审计由确定性领域服务处理，业务事实落在 PostgreSQL。
> 退款先生成草稿、授权人带版本审批后才恢复执行，checkpoint 只存流程状态；幂等防重复、
> 外部 unknown 只按原操作对账。V1.2 把 Agent 生命周期接进 HTTP 主链路：start/clarify/
> decision/state 四个接口只服务内部坐席，其中 decision 不携带审批结论——审批必须先写入
> 领域事实源，apply_decision 每次重读带版本的事实；外部执行结果也只能由 SYSTEM 写领域事实。
> 线程以 (租户, 线程) 为唯一键，PG 下靠 workflow_threads + 固定 checkpoint 实现跨进程恢复；
> 死循环用确定性步数上限收口，被拦截节点不执行、终态写入 checkpoint。多 Agent 同黄金集
> A/B 无收益，故默认单 Agent；真实 LLM 与 Judge 都是**实现完成、离线验证、真实模型未实测**。"

---

## 范围冻结（V1.2 收口）

V1.2 之后**不再新增功能名词**：不新增默认多 Agent、微调/DPO、pgvector、长期记忆、缓存平台、
真实支付、CRM 或生产部署；Judge 保持独立、只评话术质量，不扩建采样与校准平台。
后续只做缺陷修复、可复现验证与文档事实对齐。

## 文档

- [工程宪法](./AGENTS.md)
- [架构](./docs/ARCHITECTURE.md)
- [PostgreSQL 事实源](./docs/POSTGRES.md)
- [测试基线](./docs/TESTING_BASELINE.md)
- [面试讲解](./docs/INTERVIEW_OVERVIEW.md)
- [状态与风险](./docs/STATUS_AND_RISKS.md)
- [模型与评测](./docs/MODEL_EVALUATION.md)
- [V1 执行方案与 Agent 提示词](./docs/V1_EXECUTION_PLAN.md)
- [Supervisor 实验](./docs/MULTI_AGENT_EXPERIMENT.md)
