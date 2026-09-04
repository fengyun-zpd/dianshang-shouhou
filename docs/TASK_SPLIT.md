# 第一版模块任务拆分与任务卡

> 归属：电商售后多智能体工单系统（目标远程 `fengyun-zpd/dianshang-shouhou`，本地工作区 `D:\workplace\PyCharmMiscProject\私域`）
> 版本：v0.1。本文档为"任务分配与文件所有权"的事实源；实现状态以 `docs/STATUS_AND_RISKS.md` 与测试输出为准。

## 1. 协作规则（每个执行者必须遵守）

1. 一个任务卡只由一个 Agent 负责一个模块目录；先声明负责目录、禁改文件、验收命令。
2. 不允许多个 Agent 同时修改同一个事实源文件（见 §4 所有权矩阵）。
3. 先写最小失败测试，再修改实现（RED → GREEN）。
4. 不覆盖工作区已有改动；不使用破坏性 Git 命令。
5. 每个任务完成后提交：修改文件、测试结果、评测变化、风险、未完成项、下一步（中文）。
6. 修改规范性行为时同步更新 README、需求、架构、ADR 与测试（当前 README 更新列为决策项 D2，未确认前不得擅改）。

## 2. 目标目录规划（新项目仓库内）

```text
私域/
├── AGENTS.md                  # 宪法（事实源，只读）
├── README.md                  # 项目说明（更新待 D2）
├── pytest.ini
├── pyproject.toml             # 规划中（阶段 0 尾部）
├── src/
│   ├── domain/                # 现有退款最小闭环（保留，只读）
│   │   └── after_sales/       # 阶段 1 售后领域插件（任务卡 A）
│   ├── platform/              # 规划：配置/日志脱敏/幂等/审计/评测端口
│   ├── agents/                # 规划（阶段 2+，任务卡 B）
│   ├── tools/  rag/  api/     # 规划（阶段 3+）
├── tests/
│   ├── test_refund_service.py # 现有基线（保留，只读）
│   ├── conftest.py            # 任务卡 C
│   └── unit/domain/after_sales/  # 任务卡 A
├── evals/                     # 规划（阶段 4，黄金集）
├── scripts/                   # 任务卡 C（run_tests / 回归模板）
├── docs/                      # 本目录：状态、拆分、架构、回归模板
└── 仓储 → 外部参考基线，禁止写入
```

## 3. 阶段 × 模块任务拆分矩阵

| 阶段 | 模块 | 交付物 | 状态 |
| --- | --- | --- | --- |
| 0 | 项目基线 | 目录/配置/日志/错误码/迁移/夹具/CI + README/架构/启动/验收清单 | 规划中（本文档与 `docs/ARCHITECTURE.md` 已落地一部分） |
| 1 | 售后领域插件 | 实体、订单核验、资格、退款上限、工单状态机、幂等命令、政策 V1、测试 | 规划中 → 任务卡 A（进行中） |
| 2 | 单 Agent 闭环 | LangGraph 单图、澄清、只读工具、草稿、审批 interrupt/resume | 规划中 → 任务卡 B（待 A 验收后启动） |
| 3 | 工具与 RAG | JSON Schema 工具、TenantContext、注入防护、检索+引用 | 规划中 |
| 4 | 可靠性与评测 | 重试/熔断/降级/`operation_unknown`、黄金集回放 | 规划中 |
| 5 | 多 Agent/模型优化 | Supervisor+子 Agent、CrewAI 可选、Graphiti、微调对照 | 规划中（ADR-002/004/005） |
| 6 | 外部 Agent 网络 | Mule Agent Bridge 适配器 | 规划中（ADR-003） |

实施顺序固定：0 → 1 → 2 → 3 → 4 → 5 → 6（用户指令与 ADR-002 一致；先单 Agent 稳定再谈多 Agent）。

## 4. 优先任务卡

### 任务卡 A：售后领域插件（阶段 1，✅ 已完成）

- 状态：阶段 0/1 已交付（提交 `aee6fbb`）；阶段 2 追加只读查询与确定性退款计划（`get_order_by_id` / `list_customer_tickets` / `compute_refund_plan` + `RefundPlan`），`tests/unit/domain/after_sales` 共 **40 项全绿**。
- 验收命令：`.venv\Scripts\python.exe -m pytest tests/unit/domain/after_sales -v` 全绿，且全量回归通过。

### 任务卡 B：单 Agent 闭环（阶段 2，✅ 已完成）

- 状态：已交付（用户 D3 指令启动）。LangGraph **1.2.11**（D 盘 `.venv`）。
- 负责目录：`src/agents/`（state / intent / ports / nodes / graph / runner）+ `tests/unit/agents/`（33 项全绿）。
- 交付：状态 Schema（13 必含字段）；规则化意图识别与缺参澄清（interrupt）；只读证据编排（订单/历史工单/物流未接入显式标注）；确定性退款计划（Agent 不决定金额）；草稿落库后审批 interrupt（approval_id 与 operation_id 独立）；恢复时重读领域事实源；拒绝零副作用；重复 resume / 重复请求幂等；operation_unknown 仅原 operation_id 查询与对账；审计事件 id 收集；伪造审批与非法恢复被拒。
- 验收命令：`.venv\Scripts\python.exe -m pytest tests/unit/agents -v`（33 通过）；`.venv\Scripts\python.exe -m pytest tests/ -v`（95 通过）；演示：`.venv\Scripts\python.exe scripts\demo_interrupt_resume.py`。
- 后续接黄金集（阶段 4）。

### 任务卡 C：测试基线工具链（阶段 0 尾部 / 横向，✅ 已完成）

- 状态：已交付（`scripts/run_tests.py`、`docs/TESTING_BASELINE.md`、`tests/conftest.py`、pytest.ini markers）。
- 验收命令：`.venv\Scripts\python.exe scripts\run_tests.py` → `REGRESSION PASS`。

### 任务卡 D：工具契约 + TenantContext + RAG（阶段 3，✅ 已完成）

- 交付：`src/platform/tooling.py`（pydantic 入/出参模型 → 严格 JSON Schema；ToolRegistry 调用链：角色权限 → 防跨租户参数 → 入参校验 → 超时执行 → 出参校验 → 审计）；`src/rag/`（PolicyDocument 分块、关键词倒排 + 可插拔 VectorBackend（进程内字典余弦）、RRF 混合检索、引用校验、提示注入双向防护、无证据语义）；`src/agents/toolkit.py`（只读工具 get_order/get_ticket/list_customer_tickets/retrieve_policy，租户来自 TenantContext 强绑定，领域错误原样透传，RAG 无证据 → NO_EVIDENCE、注入 → INJECTION_DETECTED）。
- 测试：`tests/unit/platform/test_tooling.py`（12）、`tests/unit/rag/test_rag_store.py`（14）、`tests/unit/agents/test_toolkit.py`（9）。
- 验收命令：`.venv\Scripts\python.exe -m pytest tests/unit/platform/test_tooling.py tests/unit/rag tests/unit/agents/test_toolkit.py -v`（全绿）；全量 139 passed。
- 边界：MCP 跨服务协议为规划接口（ADR-003）；外部向量库经 VectorBackend 协议接入（规划）。

### 任务卡 E：可靠性与评测（阶段 4，✅ 已完成）

- 交付：`src/platform/reliability.py`（有限重试 / 熔断 closed-open-half_open / 只读降级 fallback / fail-closed / 人工接管标记 AuditMarkers）；黄金集 `evals/golden/golden_v1.json`（11 条固定种子用例：正常/拒绝/澄清/转人工×4/unknown 对账/重复请求/伪造审批）；回放器 `evals/replay.py`（驱动 WorkflowRunner interrupt/resume 自动审批 → 确定性断言 → 报告 `evals/reports/golden_v1_report.md`：任务完成率/意图准确率/引用正确率/必要澄清率/P50-P95/安全不变量）。
- 测试：`tests/unit/platform/test_reliability.py`（8）、`tests/unit/evals/test_replay_smoke.py`（5）。
- 验收命令：`.venv\Scripts\python.exe evals\replay.py` → 11/11 通过；报告写入 evals/reports/。
- 评测结果（2026-09-04，golden-v1）：任务完成率 1.0、意图准确率 1.0、引用正确率 1.0、注入拦截通过、安全不变量 0。

### 任务卡 F：受控 LLM 运行时 + 能力矩阵 + 影子评测（阶段 5A，✅ 已完成）

- 交付：`src/models/`（config/base/schemas/prompts/offline/openai_compatible/router + `__init__`）；能力矩阵六项（tool_calling/high_risk_draft 高风险默认禁用）；`ModelGateway` 降级链（主模型失败 → 离线规则，degraded=True）；内容安全守卫（金额/审批/状态/执行指令拒绝）；发送前 PII 脱敏 + 证据块注入拒绝发送；影子入口 `evals/run_model_shadow_eval.py --mode offline|candidate`；报告 `evals/reports/shadow_eval_<mode>.md`；`docs/MODEL_EVALUATION.md`。
- 测试：`tests/unit/models/`（config 6 / offline 5 / openai_compatible 14 / router 5 / shadow_script 3 = 32 项）。
- 验收命令：`.venv\Scripts\python.exe -m pytest tests/ -v`（171 passed）；`.venv\Scripts\python.exe evals\replay.py`（11/11）；`.venv\Scripts\python.exe evals\run_model_shadow_eval.py --mode offline`（意图准确率 1.0）；`--mode candidate` 未配置 Key → 安全降级、标注"未实测"、未联网。
- 诚实边界：真实模型候选未实测（本环境无 Key）；微调/LoRA/DPO 需对照数据后另行门禁。

### 任务卡 G：Supervisor 多 Agent 拆分实验（阶段 5B，✅ 已完成，回退结论）

- 交付：`src/agents/subagents.py`（`ReadOnlySubAgent` + order/history/policy 白名单；构造时断言全只读）、`supervisor.py`（`SupervisorRunner`：API/状态/审批语义与单 Agent 一致，仅证据节点替换为并行子 Agent 编排）、`graph.py` 支持 `evidence_node` 切换；A/B 对照 `evals/compare_agents.py`；`docs/MULTI_AGENT_EXPERIMENT.md`。
- 测试：`tests/unit/agents/test_supervisor.py`（11 项：只读边界/无写工具/跨租户/正确性一致（approved+rejected）/政策引用/审批 resume/拒绝/重复 resume 幂等/unknown 原键对账/伪造忽略/零副作用）。
- 实验结论（ADR-002）：两模式黄金集均 11/11、outcome 与退款 100% 一致 → **默认路径维持单 Agent**；Supervisor 保留为可选实验运行时（并行只读证据、子 Agent 模块化、未来多模型挂载点）。
- 验收命令：`.venv\Scripts\python.exe -m pytest tests/ -v`（182 passed）；`.venv\Scripts\python.exe evals\compare_agents.py`；`.venv\Scripts\python.exe evals\replay.py`（11/11）；`git diff --check`。

### 任务卡 H：Mule Agent Bridge（阶段 6，✅ 已完成）

- 交付：`src/bridge/models.py`（BridgeIdentity/IdentityRegistry、BridgeAction 白名单、入/出站 Schema、BridgeLogEntry、FORBIDDEN_ACTIONS）+ `src/bridge/bridge.py`（`MuleAgentBridge.invoke`：身份→白名单→租户注入→Schema→熔断→执行→出站校验→审计；超时 3s；注入拒绝）；`docs/MULE_BRIDGE.md`。
- 安全边界：白名单仅只读查询 + `submit_after_sales_request`（客服入口语义，AGENT 草稿 + 人工审批）；approve/reject/execute/close_ticket/change_address/high_risk_draft/refund_now 在协议中**不存在**（测试断言不可达）；跨租户注入拒绝；审计无 PII/请求体。
- 测试：`tests/unit/bridge/test_bridge.py`（12 项）。全量 194 passed。
- 验收命令：`.venv\Scripts\python.exe -m pytest tests/ -v`；`.venv\Scripts\python.exe evals\replay.py`（11/11）；`git diff --check`。
- 边界：未接真实 MuleSoft/MCP server（规划）；身份映射为内存配置。

## 5. 修订记录

- v0.1（本会话）—— 建立模块任务拆分、目录规划、所有权矩阵与任务卡 A/B/C。
- v0.2（2026-09-04）—— 任务卡 A/C 完成（阶段 0/1）；任务卡 B（阶段 2）完成：LangGraph 1.2.11 单 Agent 工作流，全量 95 passed。
- v0.3（2026-09-04）—— 任务卡 D（阶段 3 工具契约 + TenantContext + RAG）与任务卡 E（阶段 4 可靠性与评测）完成：全量 139 passed；黄金集 11/11。
- v0.4（2026-09-04）—— 任务卡 F（阶段 5A 受控 LLM 运行时/能力矩阵/影子评测）完成：全量 171 passed；离线影子意图准确率 1.0，候选模型"未实测"（未配 Key、未联网）。
- v0.5（2026-09-04）—— 任务卡 G（阶段 5B Supervisor 实验）完成：全量 182 passed；A/B 对照两模式均 11/11 → 默认维持单 Agent（ADR-002 回退条款），Supervisor 保留可选运行时。
- v0.6（2026-09-04）—— 任务卡 H（阶段 6 Mule Agent Bridge）完成：全量 194 passed；桥接仅只读 + 发起请求（无审批/执行）。阶段 0–6 主线全部完成。
