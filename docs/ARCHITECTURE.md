# OpsPilot 电商售后数字化工具架构

> 文档更新：2026-09-16。面向个人开发中的退款工单处理与可靠性验证；数据为合成样本。

架构围绕订单核验、政策证据、草稿、人工审批、执行结果与审计划分职责。
退款为当前已实现主线，企业业务拓展需在既有权限与事实源边界下另行设计。

```text
FastAPI（HTTP 主链路：领域服务接口 + Agent 生命周期接口）
  ├── 领域服务接口（已实现/已测试/已验证）
  │     -> AfterSalesApplicationPort
  │          |-- MemoryAdapter（测试/业务验证）
  │          `-- PgCommandAdapter（PG profile）
  │                -> PgCommandService（权限/金额/状态/幂等/审批/审计/租约）
  │                -> PostgreSQL（业务事实源）
  └── Agent 生命周期接口（已实现/已测试/已验证，仅内部坐席）
        POST /api/v1/agent/start               -> WorkflowRunner.start()（仅 AGENT）
        POST /api/v1/agent/{id}/clarify        -> WorkflowRunner.resume()（仅澄清中断；仅 AGENT）
        POST /api/v1/agent/{id}/decision       -> WorkflowRunner.resume("_continue_"；不携带审批结论)
        GET  /api/v1/agent/{id}/state          -> WorkflowRunner.get_state()（只读；(租户,线程) 作用域）
              -> 单 Agent 工作流（只读证据、确定性证据检索基线、澄清与审批 interrupt/resume）
              -> 节点步数上限 + 工具去重账本（确定性保护）
线程绑定事实源（PG profile）：workflow_threads（租户限定）+ 固定 SQLite checkpoint
                            .runtime/checkpoints/opspilot-agent.sqlite（仅流程状态）

```

本地工作台（`src/api/ui/`）通过受保护 API 提供领域操作、Agent 全链路面板、隔离 Agent Lab 和评测信息；当前使用合成数据，生产运营环境尚未接入。

`src/domain/after_sales/` 是唯一业务主实现。早期退款服务、Mule Bridge 和整库镜像持久化原型已删除，不参与默认运行时。

| 组件 | 负责 | 不负责 |
| --- | --- | --- |
| 单 Agent | 意图、缺参澄清、只读证据、解释 | 金额、资格、状态、审批、执行放行 |
| 确定性证据检索基线（本地 RAG） | 合成政策 citation 与注入信号；租户/版本/引用约束、无证据转人工 | 金额、资格、状态或事实源版本裁决 |
| 领域服务 | 权限、金额、状态、幂等、并发、审计 | 自然语言理解 |
| 授权人员 | 带版本 approve/reject 决定 | 直接改写业务事实 |
| checkpoint | 流程挂起与恢复 | 订单、金额、审批、执行结果 |

## Agent 生命周期 HTTP 主链路

`create_app(..., agent_runner=...)` 把当前已装配的 `WorkflowRunner` 暴露为 HTTP：
`scripts/run_api.py --backend memory` 创建内存运行器（**进程内合成业务验证**，重启即重置）；
`--backend pg` 复用同一 PG 运行器（真实 PostgreSQL 后端 +
`.runtime/checkpoints/opspilot-agent.sqlite` 固定 checkpoint + D9 租约）。未装配运行器时接口
返回 `503 AGENT_RUNNER_UNAVAILABLE`，**不静默降级**、不伪造结果。

| 接口 | 角色 | 语义 | 关键边界 |
| --- | --- | --- | --- |
| `POST /api/v1/agent/start` | **仅 AGENT** | 调用 `WorkflowRunner.start()` | 租户只取认证身份；`extra="forbid"`（tenant_id/金额/角色/审批结果/外部结果 → 422）；同 (租户, 线程) 异请求 → 409 |
| `POST /api/v1/agent/{thread_id}/clarify` | **仅 AGENT** | 澄清补参后回到原线程 | 仅澄清中断状态可用；`extra="forbid"`；空载荷 422 |
| `POST /api/v1/agent/{thread_id}/decision` | APPROVER / SYSTEM | **只触发** `resume(payload="_continue_")`；待对账/待收尾的既有线程同样由此显式恢复 | 不携带 `approved/rejected`（携带 → 422）；`apply_decision` 重读带版本的领域审批事实；已安全停止的循环终态不接受推进（409） |
| `GET /api/v1/agent/{thread_id}/state` | AGENT / APPROVER / SYSTEM | 只读线程视图 | 线程唯一键 `(tenant_id, thread_id)`；不存在或错误租户统一 404（不暴露归属）；响应只含公共字段并统一脱敏 |

只服务内部坐席：**同租户客户调用任一接口 → 403**；客户端自助入口属规划能力，当前不开放。

HTTP 对外视图（R2）：`_agent_view` 只暴露工作流所需字段（thread/ticket/operation/金额/证据/
错误码/步数/审计编号），**不整体序列化内部 checkpoint**；`user_request`、
`thread_request_fingerprint`、`simulate_external` 等内部字段不出现在响应中；错误响应与
422 校验回显同样脱敏。请求指纹仍按**原始**请求文本计算，脱敏不改变幂等与冲突判定。

收尾恢复（R4）：`decision` 除审批等待外，也允许对 `next_action ∈ {reconcile_required, finished}`
的既有线程显式恢复；runner 从领域事实重读操作状态，只在 **EXECUTED/REJECTED** 时调用既有
关单命令收尾（**绝不再次 execute**），`FAILED` 保持工单开放并转人工；unknown 未对账前不关单、
不换键。收尾失败保留原领域错误码与人工处理信息，不改写为成功。

审计关联（R3）：`collect_audit_events(tenant, thread, (ticket_id, operation_id))` 从领域审计事实
按租户 + 线程绑定实体过滤，进程内集合只是去重视图；交错线程、重复读取与新实例重建已有回归。
新事件使用持久 `event_id`；无 ID 的旧对象仍按列表位置兼容生成标识，不能将兼容路径视为全局唯一身份保证。

外部执行结果不由 HTTP 调用者指定：`start` 不接受 `simulate_external`；未知状态必须由 SYSTEM
调用 `/api/operations/{id}/execute` 写入 `timeout`（领域事实 `unknown`），再由 `decision` 重读。
runner 内部的 `simulate_external` 参数仅用于测试与合成业务验证（HTTP 层不可达）。

`/decision` 的存在意义是**把"触发"与"决定"分开**：审批结论必须先经
`/api/operations/{operation_id}/approve|reject` 写入领域事实源；HTTP body、checkpoint 与
模型输出都不能作为审批依据。领域事实仍为 `PENDING_APPROVAL` 时，工作流继续等待；
`unknown` 时返回 `operation_unknown` 且只允许以原 `operation_id` 对账。

### 跨进程恢复（PG profile）

线程绑定的事实源 = **租户限定的 `workflow_threads` 行 + 持久 checkpoint**；进程内字典只是
便利缓存。因此：

- 实例 A 中断 → 进程退出（不释放租约）→ 租约过期后实例 B 用同一 PG + 同一 fixed checkpoint
  接管，可按 `(tenant_id, thread_id)` 读 `state`、恢复 `decision` 并继续执行；
- 同名线程在不同租户下互不冲突（checkpoint 键空间为带版本的长度编码 `v2|租户长度|租户|线程长度|线程`）；
- 错误租户读取与"线程不存在"返回**同一个** 404 与同样结构的消息（不可区分、不含所属租户）；
- 内存 profile 不声称持久恢复（进程内业务验证）。

实测：`tests/integration/test_agent_restart_recovery_live.py`（5 项 PG live，含独立进程重启、
租约仍生效、冒号碰撞隔离、循环终态跨实例存活）。

## 确定性保护边界

| 保护 | 机制 | 触发后的行为 |
| --- | --- | --- |
| 死循环 | 节点包装器递增 `step_count`（默认上限 32，可构造参数覆盖） | 抛 `AgentLoopDetected` → `AGENT_LOOP_DETECTED` / `outcome=escalated` / 转人工；**触发后无新增领域写入、无退款执行** |
| 单次 invoke 超级步失控 | LangGraph `recursion_limit`（`max_steps*2+10`） | `GraphRecursionError` → 同一错误码收口 |
| 重复工具调用 | 工具去重账本：键 =（工具名, 租户, thread_id, 参数摘要） | 复用首次结果，不发起第二次领域调用；领域幂等键仍是最终兜底 |
| 审批事实过期 | `get_operation` 明确列入"永不缓存"清单 | 每次恢复都读事实源最新版本 |

循环终态**写入持久 checkpoint**（流程状态，非业务事实）：新实例 `get_state` 仍返回
`AGENT_LOOP_DETECTED` / `escalated` / 人工接管原因；检测前已形成的草稿与审批事实保留在领域
事实源中，不受影响。

限制（如实）：`step_count` 只在节点**正常返回**时写回 checkpoint——LangGraph 的 `interrupt()`
会丢弃该节点的写入，因此持久化的计数不含"进入即挂起"的节点；但上限判定按**节点进入次数**
计算，两类计数都能阻止无界循环（`tests/unit/agents/test_loop_protection.py`）。

默认验收路径是确定性领域 API 加**已并入 HTTP 的** Agent 生命周期。Supervisor 只作 A/B 实验；
同一黄金集下没有收益，因此不进入默认路径。真实 LLM 适配入口已实现，真实调用效果未实测；微调尚未运行。

```powershell
cd D:\workplace\PyCharmMiscProject\私域
. .\scripts\init_d_env.ps1
.venv\Scripts\python.exe -m pytest tests/ -q
.venv\Scripts\python.exe scripts\verify_after_sales.py
.venv\Scripts\python.exe scripts\demo_agent_http.py      # HTTP 生命周期（9 场景）
.venv\Scripts\python.exe scripts\run_api.py --backend memory
```
