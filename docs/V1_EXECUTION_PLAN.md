# V1 执行方案

## 最终目标

用一条破损退款闭环证明：Agent 组织只读证据和流程恢复，但没有权限决定金额、审批或副作用；确定性领域服务与 PostgreSQL 保证事实、安全和审计。

## 审查结论

当前 V1 已有完整的单 Agent 闭环、确定性领域服务、确定性证据检索基线（本地 RAG）、PostgreSQL 命令路径、
> **历史快照（2026-09-07，已被 V1.2 收口轮取代）**：本文保留当时的问题定位与实施顺序，
> 其中的状态判断与测试数字**不是当前事实**。当前事实请以 `README.md`、`docs/ARCHITECTURE.md`、
> `docs/STATUS_AND_RISKS.md`、`docs/TESTING_BASELINE.md`、`docs/INTERVIEW_OVERVIEW.md` 为准。
> 已被取代的两点：① FastAPI 工厂**已接入** `WorkflowRunner`（`create_app(agent_runner=...)`，
> `/api/v1/agent/*` 四个接口，仅内部坐席）；② 本文中的测试数字（399/44、443/0 等）属历史轮次，
> 当前基线为离线 490 passed / 49 skipped、隔离 PG 539 passed / 0 skipped。

审批恢复、unknown 原操作对账、领域 API、黄金集和五场景演示。Agent 运行器在本文写作时由脚本/回放
驱动、FastAPI 工厂尚未接入它（**该状态已在 V1.2 收口轮改变**）。`SupervisorRunner` 只作为
A/B 实验保留；模型模块只提供离线规则基线和受控影子入口；没有实际微调。这些边界与面试目标一致。
PG destructive-operation guard 已实现并有拒绝路径回归测试（`src/platform/pg_test_guard.py`
fail-closed：仅 `opspilot_test_*`@localhost 可被 DROP/重建，非法 URL 在连接探测前失败）。
**历史数字（2026-09-07 实测，仅作留档）**：未配置隔离 PG → 399 passed、44 skipped；
配置 `OPSPILOT_TEST_DATABASE_URL`（opspilot_test_* 隔离库）→ 443 passed、0 skipped。
当前基线见 `docs/TESTING_BASELINE.md`；未配置隔离库时的 skip 仅是该运行模式的纪律性跳过，
不能替代 PG 验证。

继续增加 RAG 层级、多 Agent、向量库、微调、生产前端或外部集成只会增加解释成本，不能提高当前
主张的可信度。下一阶段应进入 **V1 发布候选验收**，把现有能力变成可复现、可讲解的证据。

## 后续顺序

1. 冻结范围：只接受可复现缺陷修复、安全回归、验收脚本和当前事实的文档同步；不新增业务能力。
2. 复核 PG 验收隔离：确认任何会 `DROP` 表或重建 schema 的回放、集成测试和脚本都经过显式的本地隔离测试库守卫，拒绝通用 `DATABASE_URL`、生产/共享地址和非测试库名；建库失败必须 fail-closed，不能吞错后继续复用旧库。
3. 再决定 API 边界：优先保留当前领域 API 口径；只有补齐 `start / clarify-resume / state` 的 HTTP e2e 后，才把 WorkflowRunner 写进 API 主架构。否则同步文档，不做半成品接入。
4. 在 D 盘环境运行全量测试、黄金集回放、单 Agent/Supervisor 对照、离线影子评测、五场景演示和现有 API e2e；在结果旁记录提交版本、日期和运行模式。
5. PG guard 通过后，才使用唯一命名的隔离数据库复跑迁移和 PG 集成；不可用则明确列为未验证，不能用 memory 结果代替。
6. 以 `INTERVIEW_OVERVIEW.md` 为唯一讲解稿，准备主流程、信任边界、单 Agent 取舍三张图和五分钟现场演示；只在实际结果变化时更新 `TESTING_BASELINE.md` 与报告。

## 不做

- 不让 RAG 裁决金额、资格或状态；
- 不把 Supervisor 变成默认运行时；
- 没有版本化 chosen/rejected 数据和同一评测集，不做 LoRA/QLoRA/SFT/DPO；
- 不接真实支付、CRM、企业微信、MuleSoft、MCP；
- 不把本地演示工作台包装成生产前端；不向 C 盘安装、下载、缓存或写运行时文件。

## 给下一位 Agent 的提示词

```text
你负责 OpsPilot V1 发布候选验收。先读 AGENTS.md、README.md、docs/ARCHITECTURE.md、docs/POSTGRES.md、docs/TESTING_BASELINE.md、docs/INTERVIEW_OVERVIEW.md 和本文件。先执行 `git status`，工作区可能已有待提交清理；保留并理解这些改动，禁止 checkout、reset 或恢复已删除的旧模块。

目标：交付单 Agent 售后退款可靠性项目。Agent 只做意图、澄清、只读证据和解释；领域服务与 PostgreSQL 裁决金额、资格、状态、幂等、审批和审计；RAG 只提供可追溯政策 citation。

1. 先执行 `. .\scripts\init_d_env.ps1`。所有临时文件、下载、依赖缓存、checkpoint 和测试目录必须在项目 D 盘 `.runtime/` 或 `.cache/`。不得向 C 盘写入或清理文件。
2. 扫描旧退款服务、Mule Bridge、SQLite 快照恢复和 PgBackedSession 的导入、测试、死链接和文档残留；确认删除项没有默认运行时依赖。只删除确认无引用的历史产物。
3. 复核并测试现有 PG destructive-operation guard：回放、PG 集成夹具和隔离脚本必须只读取 `OPSPILOT_TEST_DATABASE_URL` 或由脚本创建的唯一 `opspilot_test_*` 本地库。禁止把 `DATABASE_URL` 或任意 `--pg-url` 直接用于 schema reset；拒绝非 localhost 地址、非测试库名和建库失败。守卫必须在任何连通性探测前执行，拒绝路径必须有测试。
4. 复核 RAG 指标：正向 citation 命中、旧版本/不适用证据的安全拒绝、注入拒绝分别报告；不能把“正确拒绝旧版本”写成引用正确率下降。同步黄金集报告生成器和针对性测试。
5. 运行 `.venv\Scripts\python.exe -m pytest tests/ -q`、memory profile 的 `evals\replay.py`、`evals\compare_agents.py`、`evals\run_model_shadow_eval.py --mode offline`、`scripts\demo_interview.py` 和 `git diff --check`。同时执行 API e2e 测试；它必须通过 HTTP 接口验证审批和权限边界，不能直接调用领域服务替代。
6. PG guard 通过后，才执行 `.\scripts\run_pg_tests_isolated.ps1` 和 PG profile 回放；不可用时明确列出未验证项，不伪造通过。运行前确认脚本已加载 D 盘环境。
7. 只有失败时才修复最小根因并补针对性回归。不要新增多 Agent、微调、向量库、长期记忆、生产前端、真实 LLM、真实支付/CRM/MCP/Mule 集成；现有本地演示工作台只做口径和回归维护。
8. 模型模块只能作为“未接入 V1 的离线安全基线”保留；若没有人会演示它，就连同对应测试和文档引用一起删除，不能留下孤立实验代码。RAG 只保留可追溯安全测试，不扩展为向量平台。
9. 只在实际结果变化时同步 README、ARCHITECTURE、POSTGRES、TESTING_BASELINE、STATUS_AND_RISKS 和 INTERVIEW_OVERVIEW；旧的 439/450 项数字不能作为当前基线，也不声称多 Agent 的性能收益。
10. 最终报告列出保留/删除范围、D 盘路径、实际命令与结果、隔离 PG 目标（脱敏）、未验证项，以及 90 秒和 5 分钟面试讲解。除非用户另行要求，不提交 Git、不接外部系统、不安装到 C 盘。
```

## 90 秒讲解

“我把项目设计成受控的售后工单工作流。Agent 只做意图识别、澄清和证据检索，金额、资格、状态和审批由确定性领域服务处理。退款先生成草稿，授权人员提交带版本的审批决定后，工作流才恢复；checkpoint 不保存业务真相，状态都从 PostgreSQL 重读。幂等键防止重复副作用，外部超时只允许按原操作对账。RAG 只给政策 citation，不能决定金额。我用同一黄金集对照过 Supervisor，结果没有收益，所以默认保持单 Agent；没有对照数据也不做微调。”
