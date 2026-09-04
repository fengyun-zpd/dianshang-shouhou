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

### 任务卡 A：售后领域插件（阶段 1，执行中）

- 负责 Agent：A（领域插件工程师）
- 负责目录：`src/domain/after_sales/` 与 `tests/unit/domain/after_sales/`（新建）
- **禁改文件**：`README.md`、`AGENTS.md`、`pytest.ini`、`docs/`（只读引用）、`src/domain/{models,idempotency,refund_service}.py`、`tests/test_refund_service.py`、`tests/conftest.py`、`scripts/`、`仓储/**`
- 交付：售后核心实体最小建模；确定性服务（订单核验、售后资格、退款上限含累计部分退款、工单/操作状态机、幂等命令、审计）；政策 V1 用结构化规则（非 RAG）；测试覆盖正常/缺参/冲突政策/越权/重复请求（同键同载荷/异载荷）/非法状态迁移/未知操作只查原键
- 验收命令：`python -m pytest tests/unit/domain/after_sales -v` 全绿，且 `python -m pytest tests/ -v` 原有 14 项仍全绿
- 依赖：无第三方新依赖（仅标准库 + pytest）；金额用 Decimal 禁 float

### 任务卡 B：单 Agent 闭环（阶段 2，暂缓启动）

- 负责 Agent：B（编排工程师）—— 阶段 1 验收通过、决策项 D3 确认后启动
- 负责目录：`src/agents/`（规划）+ `tests/unit/agents/`
- 交付：LangGraph 或等价状态机单 Agent：意图识别 → 澄清 → 只读工具检索 → 动作草稿 → 审批 interrupt → 恢复 → 审计；checkpoint 仅存流程状态
- 验收命令（规划）：对应单测 + 一条端到端冒烟；随后接黄金集

### 任务卡 C：测试基线工具链（阶段 0 尾部 / 横向，执行中）

- 负责 Agent：C（测试基线工程师）
- 负责目录：`scripts/`、`docs/TESTING_BASELINE.md`、`tests/conftest.py`、`pytest.ini`（扩展 markers，保留现有内容）
- **禁改文件**：`README.md`、`AGENTS.md`、`src/**`（含 domain 现有与新插件）、`tests/test_refund_service.py`、`tests/unit/**`、`docs/{STATUS_AND_RISKS,TASK_SPLIT,ARCHITECTURE}.md`、`仓储/**`
- 交付：分层回归脚本（默认 `-p no:cacheprovider`，规避中文路径 cache 失败）；回归报告模板（模型/Prompt/数据/代码版本、通过/失败、安全不变量）；固定随机种子与合成数据辅助（conftest 最小化）；pytest markers（unit/integration/e2e/property/security）
- 验收命令：`python scripts/run_tests.py` 返回 0 且 14 项原有测试通过；`python -m pytest tests/ -v` 正常收集
- 依赖：无第三方新依赖

## 5. 修订记录

- v0.1（本会话）—— 建立模块任务拆分、目录规划、所有权矩阵与任务卡 A/B/C。
