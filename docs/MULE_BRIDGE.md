# Mule Agent Bridge（阶段 6 / ADR-003）

> 归属：电商售后多智能体工单系统（OpsPilot）。版本：v1.0（2026-09-04）。
> 目标：把本系统能力以受控方式暴露给 MuleSoft Agent Fabric / 其他 Agent 网络。

## 1. 桥接层职责（只做协议/身份适配，不扩大本地权限）

| 环节 | 实现 |
| --- | --- |
| 身份映射 | `IdentityRegistry`：外部 principal → 本地租户/角色/动作白名单；未注册 → `AUTH_ERROR` 拒绝；每个动作还要通过本地角色矩阵（查询只读角色，发起请求允许客服/客户，审批与执行永不进入桥接层） |
| 租户注入 | 请求级 `tenant_id` 与身份映射不一致 → `TENANT_MISMATCH` 拒绝；一致则剥离（租户只来自映射） |
| Schema 校验 | 入站 `INBOUND_SCHEMAS` / 出站 `OUTBOUND_SCHEMAS`（Pydantic，非法 → `VALIDATION_ERROR` / `OUTPUT_SCHEMA_ERROR`） |
| 超时 | 执行默认 3s（`ThreadPoolExecutor` + future timeout）→ `BRIDGE_TIMEOUT`；无法确认副作用时只能按原 `operation_id` 对账，禁止换键重试 |
| 审计 | `BridgeLogEntry`：身份/动作/租户/状态/耗时/错误码；**不含 PII、密钥、请求体原文**（测试断言） |
| 断路 | 复用 `platform.reliability.CircuitBreaker`：打开时 fail-closed → `CIRCUIT_OPEN` |
| 注入防护 | 政策检索查询命中提示注入 → `INJECTION_DETECTED`（不返回文档） |

## 2. 动作白名单（外部 Agent 可调用集合）

- 只读：`query_order` / `query_ticket` / `list_customer_tickets` / `retrieve_policy`
- 发起：`submit_after_sales_request` —— 以客服入口语义启动内部售后流程（AGENT 草稿 + **人工审批**），
  返回回执（thread/待审批状态/操作号），**桥接层无任何审批/执行能力**。

`FORBIDDEN_ACTIONS = approve / reject / execute / close_ticket / change_address / high_risk_draft / refund_now`
——这些动作在桥接协议枚举中不存在（`BAD_ACTION`），白名单不可能包含它们（测试 `test_forbidden_actions_never_exist_on_bridge`）。

## 3. 一次典型调用（外部网络 → 本系统）

```
外部 Agent(mule-support-A)
  └─ POST query_order {order_id: "ORD-1"}          （身份映射 T1 / AGENT）
       └─ Bridge.invoke：身份→白名单→租户注入→Schema→熔断→执行（本地领域只读）→出站校验→审计
       └─ 返回 {ok, data:{order_id, status, ...}}   （不含敏感字段外泄）
外部 Agent 发起售后：submit_after_sales_request → 内部 WorkflowRunner.start
  → 工单草稿落库 → PENDING_APPROVAL（interrupt）→ 回执 waiting_approval=true
  → 审批只能由本地授权人员（approve 不进桥接层）
```

## 4. MCP 说明

MCP（Model Context Protocol）作为跨服务/跨 Agent 网络协议属**规划**；本桥接层实现是
进程内协议适配（函数式 invoke + Pydantic Schema），未来可将同一动作白名单与安全语义
包装为 MCP server / A2A 兼容端点，**不改变**本层安全边界（身份映射/租户注入/Schema/超时/审计/断路）。

## 5. 验证与诚实边界

- 测试：`tests/unit/bridge/test_bridge.py`（14 项）：身份拒绝/越权拒绝/高危动作不可达
  （FORBIDDEN_ACTIONS 协议中不存在）/未知动作/跨租户注入拒绝/租户一致容忍/Schema 校验/正常只读查询/
  政策注入拒绝/发起请求到审批（零执行、零退款）/熔断 fail-closed/审计无 PII 与请求体/
  角色矩阵（审批/执行/关单永不进入桥接层）/未知结果按原 `operation_id` 对账（禁止换键重试）。
- 全量回归：`.venv\Scripts\python.exe -m pytest tests/` → 334 passed（2026-09-04 实测；PostgreSQL 容器运行时集成 18/18，无 PG 自动跳过）。
- 未接入真实 MuleSoft / 网络端点（无凭据、不连外部）；身份映射为内存配置（生产可换 DB/配置）。
- 修订：v1.1（2026-09-04）补充角色矩阵与未知结果对账语义。
- 修订：v1.2（2026-09-04）测试计数与实际收集同步为 14 项（含角色矩阵与未知对账），全量基线 310 passed、PG 集成 9/9。
