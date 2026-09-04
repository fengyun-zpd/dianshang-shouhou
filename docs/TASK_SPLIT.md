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

## 5. 修订记录

- v0.1（本会话）—— 建立模块任务拆分、目录规划、所有权矩阵与任务卡 A/B/C。
- v0.2（2026-09-04）—— 任务卡 A/C 完成（阶段 0/1）；任务卡 B（阶段 2）完成：LangGraph 1.2.11 单 Agent 工作流，全量 95 passed。
