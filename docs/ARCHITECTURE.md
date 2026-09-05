# 目标架构、启动方式与第一版验收清单

> 归属：电商售后多智能体工单系统（目标远程 `fengyun-zpd/dianshang-shouhou`，本地工作区 `D:\workplace\PyCharmMiscProject\私域`）
> 版本：v0.2。本文档整合本地工作区已确认事实与当前 V1 实现的"架构图 / 启动方式 / 验收清单"。规划能力不写成已实现。

## 1. 业务闭环（目标）

```
订单与租户核验 → 政策证据检索 → 售后意图识别 → 缺参澄清
→ 售后方案草稿 → 人工审批 → 受控工具执行 → 外部结果确认 → 工单关闭与审计
```

红线（宪法第三、四、六条）：

- Agent 只负责理解 / 澄清 / 检索 / 编排；订单资格、退款金额、状态迁移、权限、幂等由**确定性领域服务**完成。
- LLM 不得直接执行退款、改址、关闭工单等高危副作用。
- 写操作必须过：权限 → 人工审批 → 幂等键 → 审计 → 状态机。
- 外部调用超时或结果不明 → `operation_unknown`，只能以**原幂等键**查询，禁止换键重试。
- 用户输入、RAG 文档、工具返回均为不可信数据，须防提示注入。

## 2. 分层架构（目标）

```text
接入层    FastAPI（✅ 已实现：认证/角色/审计路由）+ 审批工作台界面（规划，未实现）
编排层    LangGraph 状态机（interrupt/resume）（✅ 阶段 2：src/agents）
决策层    Agent：意图/澄清/检索/草稿/话术      （✅ 规则化 V1 + 受控 LLM 适配层 src/models；离线基线实测，真实模型未实测）
领域层    确定性服务：权限/金额/状态/幂等/审计  （✅ 阶段 1：src/domain/after_sales）
数据层    业务事实源 = PostgreSQL Repository/schema/Alembic（✅ 已实现，本地 PG 实测 9/9）；当前运行时领域服务为内存 + SQLite 恢复原型（领域状态机整体 SQL 化规划中）
横切     日志脱敏 · 评测黄金集 · 安全不变量
```

当前已实现：领域层售后插件、LangGraph 单 Agent 工作流、工具/RAG、可靠性评测、受控 LLM 适配、Supervisor 实验、Mule Bridge、SQLite 可恢复原型、FastAPI、PostgreSQL Repository/schema/Alembic（本地 PG 集成实测 9/9）；checkpoint 仅存流程状态，业务事实一律重读领域服务。真实外部网络端点（MuleSoft/MCP server 实接）、生产部署与审批工作台界面仍为规划。

## 3. 本地目录职责（现状与目标）

见 `docs/TASK_SPLIT.md` §2。要点：`src/domain/` 现有退款最小闭环保留不动；阶段 1 新建 `src/domain/after_sales/`；`仓储` 为外部只读基线。

## 4. 启动方式

V1 当前验证（依赖装在 D 盘 `.venv`）：

```powershell
cd D:\workplace\PyCharmMiscProject\私域
.venv\Scripts\python.exe -m pytest tests/ -v                 # 全量回归（当前 310 项，2026-09-04 实测；PG 容器运行时集成 9/9）
.venv\Scripts\python.exe -m pytest tests/unit/agents -v     # 单 Agent 工作流
.venv\Scripts\python.exe -m pytest tests/unit/domain/after_sales -v   # 售后插件（任务卡 A）
.venv\Scripts\python.exe scripts\run_tests.py                # 分层回归脚本
.venv\Scripts\python.exe scripts\demo_interrupt_resume.py    # interrupt/resume 演示
```

依赖：`langgraph==1.2.11` + `pytest==9.1.1`（`requirements.txt`；PyPI 走清华镜像）。数据层：当前运行时领域服务为内存 + SQLite 恢复原型；PostgreSQL Repository/schema/Alembic 已实现并在本地 PG 集成实测（`docs/POSTGRES.md`），领域状态机整体 SQL 化与生产部署为规划。

## 5. 第一版验收清单（阶段 0 + 阶段 1）

- [ ] 目录与模块拆分文档（本文档与 `docs/TASK_SPLIT.md`）就位
- [ ] 状态与风险报告（`docs/STATUS_AND_RISKS.md`）就位，已实现/实验中/规划中严格区分
- [ ] 原退款最小闭环 14 项测试持续全绿
- [ ] 售后领域插件：订单核验、资格、退款上限、工单状态机、幂等命令实现并有测试
- [ ] 插件测试覆盖：正常 / 缺参 / 冲突政策 / 越权 / 重复请求 / 非法状态迁移 / 未知操作只查原键
- [ ] 测试基线脚本与回归模板可运行（任务卡 C）
- [ ] README / 架构图 / 启动方式 / 验收清单回填（依赖决策项 D2）
- [ ] Git 本地化与远程关联（依赖决策项 D1/D4）

## 6. 决策项索引

D1 git init 与 remote；D2 README 更新；D3 单 Agent 闭环启动时机；D4 GitHub 网络方式。详见 `docs/STATUS_AND_RISKS.md` §5。

## 7. 修订记录

- v0.1（本会话）—— 建立目标架构、启动方式与第一版验收清单。
- v0.2（2026-09-04）—— 分层图与数据层状态同步实现：接入层 FastAPI 已实现（界面规划）、决策层受控 LLM 适配已实现（真实模型未实测）、数据层 PostgreSQL Repository 已实现并本地实测（集成 9/9）、领域状态机整体 SQL 化与真实网络端点/控制台仍为规划；全量基线 310 passed。
