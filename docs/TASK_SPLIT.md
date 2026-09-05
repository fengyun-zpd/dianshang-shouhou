# 第一版模块任务拆分与任务卡

> 归属：电商售后多智能体工单系统（目标远程 `fengyun-zpd/dianshang-shouhou`，本地工作区 `D:\workplace\PyCharmMiscProject\私域`）
> 版本：v0.2。本文档为"任务分配与文件所有权"的事实源；实现状态以 `docs/STATUS_AND_RISKS.md` 与测试输出为准。

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
├── evals/                     # 黄金集与模型评测（阶段 4/5，已实现）
├── scripts/                   # 任务卡 C（run_tests / 回归模板）
├── docs/                      # 本目录：状态、拆分、架构、回归模板
└── 仓储 → 外部参考基线，禁止写入
```

## 3. 阶段 × 模块任务拆分矩阵

| 阶段 | 模块 | 交付物 | 状态 |
| --- | --- | --- | --- |
| 0 | 项目基线 | 目录/配置/日志/错误码/迁移/夹具/CI + README/架构/启动/验收清单 | ✅ 已完成 |
| 1 | 售后领域插件 | 实体、订单核验、资格、退款上限、工单状态机、幂等命令、政策 V1、测试 | ✅ 已完成 |
| 2 | 单 Agent 闭环 | LangGraph 单图、澄清、只读工具、草稿、审批 interrupt/resume | ✅ 已完成 |
| 3 | 工具与 RAG | JSON Schema 工具、TenantContext、注入防护、检索+引用 | ✅ 已完成 |
| 4 | 可靠性与评测 | 重试/熔断/降级/`operation_unknown`、黄金集回放 | ✅ 已完成 |
| 5 | 多 Agent/模型优化 | Supervisor+子 Agent、离线/兼容 LLM 适配、微调对照路线 | 🧪 Supervisor 已完成；微调规划 |
| 6 | 外部 Agent 网络 | Mule Agent Bridge 适配器 | ✅ 已完成（MCP/A2A 实接规划） |

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
- 验收命令：`.venv\Scripts\python.exe -m pytest tests/ -v`（当前全量见 STATUS 基线：310 passed）；`.venv\Scripts\python.exe evals\compare_agents.py`；`.venv\Scripts\python.exe evals\replay.py`（11/11）；`git diff --check`。

### 任务卡 H：Mule Agent Bridge（阶段 6，✅ 已完成）

- 交付：`src/bridge/models.py`（BridgeIdentity/IdentityRegistry、BridgeAction 白名单、入/出站 Schema、BridgeLogEntry、FORBIDDEN_ACTIONS）+ `src/bridge/bridge.py`（`MuleAgentBridge.invoke`：身份→白名单→租户注入→Schema→熔断→执行→出站校验→审计；超时 3s；注入拒绝）；`docs/MULE_BRIDGE.md`。
- 安全边界：白名单仅只读查询 + `submit_after_sales_request`（客服入口语义，AGENT 草稿 + 人工审批）；approve/reject/execute/close_ticket/change_address/high_risk_draft/refund_now 在协议中**不存在**（测试断言不可达）；跨租户注入拒绝；审计无 PII/请求体。
- 测试：`tests/unit/bridge/test_bridge.py`（14 项，含角色矩阵与超时回归）。当前全量见 STATUS 基线（310 passed）。
- 验收命令：`.venv\Scripts\python.exe -m pytest tests/ -v`；`.venv\Scripts\python.exe evals\replay.py`（11/11）；`git diff --check`。
- 边界：未接真实 MuleSoft/MCP server（规划）；身份映射为内存配置。

### 任务卡 I：可恢复持久化原型（SQLite，✅ 已完成）

- 交付：`service.export_state/restore_state` 与 `idempotency.export/import_records`（只读/恢复增量，不改规则）；`src/persistence/{codec,store,session}.py`；`RecoverableSession`；演示 `scripts/demo_persistence.py`；`docs/PERSISTENCE.md`。
- 测试：`tests/unit/persistence/test_snapshot.py`（6 项：roundtrip 保真+续跑 / 幂等恢复 / JSON 可序列化 / 损坏拒绝 fail-closed / checksum 篡改检测 / 跨库隔离）。当前全量见 STATUS 基线（310 passed）。
- 验收命令：`.venv\Scripts\python.exe scripts\demo_persistence.py`；`.venv\Scripts\python.exe -m pytest tests/ -v`。
- 限制：全量快照（非 WAL/事务型）。生产路线：PostgreSQL Repository/schema/Alembic 已实现并本地实测（`docs/POSTGRES.md`，PG 容器运行时集成 9/9）；领域状态机整体 SQL 化未实现（如实声明）。

### 任务卡 J：一致性与恢复安全（✅ 已完成）

- 范围：`src/agents/runner.py`、`src/persistence/`、`src/domain/idempotency.py`、`src/domain/after_sales/service.py` 与对应测试/文档；不动 Mule/RAG/LLM/AGENTS.md。
- 交付：
  - 线程生命周期：请求指纹（tenant + 规范化请求哈希）写入 checkpoint；同一 thread 只能继续原请求；结束线程提交不同请求 → `ThreadConflictError`；同请求重复提交返回**原结果**（不重放）；禁止新 order 与旧 ticket 混合；租户绑定不可变（租户命名空间，`THREAD_TENANT_CONFLICT`）。
  - 快照安全：`src/persistence/validate.py` 严格校验（顶层键集合/引用关系/租户一致/金额有限非负/refunded==已执行求和且≤实付/seq 单调/状态组合/审计与幂等引用），所有解码与恢复错误统一 `SnapshotCorruptionError`。
  - 原子恢复：`RecoverableSession.restore_into(svc, snapshot)` 先解码+校验、失败时原服务完全不变、通过后 `restore_state` 一次性替换。
  - 原子幂等：`IdempotencyStore.lock_for`(per-key)/`get_or_reserve`/`commit`/`release`；`create_ticket`/`create_refund` 在 per-key 锁内 check-then-act（CAS），创建失败释放占位；并发 N 同请求只产生一个业务对象、同键异载荷恰一个成功。
- 新增测试 26 项：`test_thread_lifecycle.py`(5)、`test_snapshot_validation.py`(12)、`test_atomic_restore.py`(4)、`test_idempotency_concurrency.py`(5)；收敛既有重复提交语义测试为“返回原结果”。
- 验收：`.venv\Scripts\python.exe -m pytest tests/ -v`（交付时 235 passed；当前全量见 STATUS）；`scripts/run_tests.py`；`evals/replay.py`（11/11）；`git diff --check`。

### 任务卡 K1：订单级退款并发一致性（✅ 已完成）

- 交付：订单级锁 `(tenant_id, order_id)` 覆盖 `create_refund`/`execute`/`reconcile`/`close_ticket`；执行与对账成功原子容量校验（已执行累计+本次 ≤ 实付，超额抛 `AMOUNT_EXCEEDS_REMAINING` 且不迁移不累计）；unknown 订单级守卫（换键拒 `OPERATION_UNKNOWN_CONFLICT`）；幂等命中先于金额/unknown 守卫；锁顺序订单锁→幂等键锁；设计说明 `docs/TASK_K1_ORDER_REFUND_CONCURRENCY.md`。
- 测试：`tests/unit/domain/after_sales/test_order_refund_concurrency.py`（7 项：顺序超退拒绝 / 并发 execute 单成功 / unknown 对账 vs 执行竞争 / 换键拒绝 / 同键重复零副作用 / 超额执行不累计 / timeout 不累计）。全量 **242 passed**。
- 验收命令：`.venv\Scripts\python.exe -m pytest tests/ -v`；`scripts/run_tests.py`；`evals/replay.py`（11/11）。
- 边界：内存锁为进程内单实例语义；跨进程行锁已由 PostgreSQL Repository 提供（`FOR UPDATE` / `try_execute_refund`，集成实测 9/9，见 `docs/POSTGRES.md`）；领域编排整体 SQL 化未实现/未声称。

## 5. 修订记录

- v0.1（本会话）—— 建立模块任务拆分、目录规划、所有权矩阵与任务卡 A/B/C。
- v0.2（2026-09-04）—— 任务卡 A/C 完成（阶段 0/1）；任务卡 B（阶段 2）完成：LangGraph 1.2.11 单 Agent 工作流，全量 95 passed。
- v0.3（2026-09-04）—— 任务卡 D（阶段 3 工具契约 + TenantContext + RAG）与任务卡 E（阶段 4 可靠性与评测）完成：全量 139 passed；黄金集 11/11。
- v0.4（2026-09-04）—— 任务卡 F（阶段 5A 受控 LLM 运行时/能力矩阵/影子评测）完成：全量 171 passed；离线影子意图准确率 1.0，候选模型"未实测"（未配 Key、未联网）。
- v0.5（2026-09-04）—— 任务卡 G（阶段 5B Supervisor 实验）完成：全量 182 passed；A/B 对照两模式均 11/11 → 默认维持单 Agent（ADR-002 回退条款），Supervisor 保留可选运行时。
- v0.6（2026-09-04）—— 任务卡 H（阶段 6 Mule Agent Bridge）完成：全量 194 passed；桥接仅只读 + 发起请求（无审批/执行）。阶段 0–6 主线全部完成。
- v0.7（2026-09-04）—— 任务卡 I（可恢复持久化原型 SQLite）完成：全量 200 passed；恢复保真/续跑/损坏拒绝测试 6 项 + 演示。
- v0.8（2026-09-04）—— 任务卡 J（一致性与恢复安全）完成：全量 235 passed；线程指纹/冲突拒绝/重复返回原结果、快照严格校验 + 原子恢复、幂等 per-key CAS 并发单飞；26 项新测试。
- v0.9（2026-09-04）—— Task K1（订单级退款并发一致性）完成：全量 242 passed；订单级锁 + 执行阶段原子容量校验修复同订单不同键并发超退；7 项新测试。
- v0.10（2026-09-04）—— K2 评测与文档收敛：g10 重复请求契约同步（重复返回原结果），黄金集与 Agent/Supervisor 对照恢复 11/11（真实运行）；citation 指标可区分错误版本/适用范围（探针准确率 0.6667，非恒一）；删除恒真断言；文档清理过时数字（当前全量 248 passed）。
- v0.11（2026-09-04）—— 与 STATUS v0.18 对齐：任务卡 G/H/I 的"当前全量"指针统一为 310 passed（PG 集成 9/9）；任务卡 I/K1 的 PostgreSQL 边界更新为"Repository/schema/Alembic 已实现并本地实测、领域状态机整体 SQL 化未实现"。
