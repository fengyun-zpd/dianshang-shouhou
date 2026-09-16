# OpsPilot 可靠性审查记录（2026-09-13）

本文记录电商售后工单的可靠性审查、整改及后续复验，使用本地合成样本。
文档整理日期：2026-09-16；下方业务执行证据均保留原运行日期。

**当前状态：R1–R5 已完成整改，审计事件持久标识后续已补充。** 最近记录的全量结果为
离线 502 passed / 55 skipped，隔离 PG 557 passed / 0 skipped，见文末审计迁移记录。

修复前审查基线为 `d5ed51f`，当时结论为未通过验收。R1–R6 中的代码位置与“现有”描述均指该历史基线，
不代表当前缺陷仍未修复；文件重命名后的行号也不能直接对应历史位置。
七场景脚本在本次整理中更名为 `scripts/verify_after_sales.py`，历史记录中的引用统一指向当前入口。
审查涉及评测与文档，没有扩大业务范围、更换事实源或连接真实副作用系统。

## 修复前实测与范围（2026-09-13）

| 检查 | 实际结果 | 含义 |
| --- | --- | --- |
| 离线全量 `python -m pytest tests -q` | **490 passed / 49 skipped / 1 warning，10.67 s** | 49 项 PG live 未在此模式启用 |
| 新建隔离 PG 库，Alembic 从空库迁移到 0005 | 成功 | 目标为 `opspilot_test_review_d2f85679`@127.0.0.1:5433 |
| 隔离 PG 全量，同一 pytest 命令 | **538 passed / 1 failed / 0 skipped / 1 warning，30.41 s** | 失败：`test_restart_recovers_thread_and_continues`，预期 200、实得 409 租约冲突 |
| 单独重跑上述失败项 | **1 passed / 1 warning，2.76 s** | 没有代码修复；不能把单项重跑覆盖为全量通过；根因未确认 |
| 新增补充检查 `scripts/review_v12_boundaries.py --pg` | **7 FAIL / 0 ERROR，退出码 1** | 4 类实现缺口，其中命名空间问题同时在 memory 与 PG 复现 |

测试只使用本地合成数据。模型：`offline_rule`；Prompt 版本：N/A（确定性流程）；补充数据集：
`review_v12_v1`。耗时为本机单次测试观测，不代表服务性能。没有调用真实 LLM、Judge 或外部系统。
没有重跑浏览器交互、黄金集、模型对照或远程 CI；不把以前的记录称为本轮验证。

本地原始证据在 `.runtime/review-20260913/pytest.log`、`migration.log`、
`pg-collision.json`、`boundaries-pg.json`。报告不保存连接密码或原始联系方式。
新建隔离库保留供复核，没有清空共享业务库。当时未提交的开发手册保持原样；本文档整理时已按当前能力重新编排手册。

## R1 — P1：checkpoint 键碰撞可绕过租户隔离

定位：`src/agents/runner.py:453`（`_cfg_for`）、`:188`（`_thread_exists`）、
`:338`（`get_state`）；`src/api/schemas.py`、`src/api/deps.py` 没有限制标识中的冒号。

当前用 `f"{tenant_id}:{thread_id}"` 作为 checkpoint 键。合法标识组合
`(T1:dept, victim)` 和 `(T1, dept:victim)` 会映射到同一个值。

- memory：前一租户创建澄清线程后，后一租户直接 GET `dept:victim/state`，返回 **200**，
  `state.tenant_id` 为 **T1:dept**；预期统一 404。
- PG：后一租户先 start `dept:victim`，创建自己的 `workflow_threads` 行后因请求指纹冲突
  返回 **409**；随后 GET 却返回 **200** 和前一租户的状态。数据库线程行存在不证明 checkpoint 归属。

前提是配置中存在含冒号的租户标识；当前接口与身份类型允许该标识。默认 T1/T2 业务验证没有触发
该组合，不能据此宣称所有标识下隔离成立。此次证实跨租户读取，未证明越权退款。

修复验收：无歧义、带版本的键编码；读取与推进前校验 checkpoint 内租户和线程；旧键兼容必须
核验归属，禁止盲目 fallback；错误租户继续统一 404。补 memory、SQLite 新实例与 PG HTTP 回归。

## R2 — P1：HTTP 流程视图原样暴露联系方式

定位：`src/api/app.py:313`、`:333`。`_agent_view` 将完整内部 state 放入 HTTP 响应，
`state.user_request` 保留请求原文，没有应用现有 PII 脱敏。

以固定合成手机号提交破损退款后，start 返回 200，原手机号出现在响应；审批人随后调用 state，
仍获得原手机号。独立 Agent Lab 的脱敏展示与模型证据脱敏不能覆盖这条真实生命周期接口。

修复验收：定义可公开的响应字段，统一脱敏 start/clarify/decision/state 中的用户文本、回复、
interrupt 与嵌套展示字段。核查 422 错误回显（`src/api/errors.py:72`）与日志的同类风险；
此处尚未宣称已复现所有错误/日志路径。保留业务定位标识与金额精度，不可把公共脱敏文本用作
幂等指纹输入，避免不同原始请求被掩码合并。采用合成手机号、邮箱、身份证做回归，报告只存布尔结果。

## R3 — P2：审计引用遗漏或混入另一工单

定位：`src/agents/ports.py:197`。运行器共用一个 `_audit_watermark`，却把它应用到各自独立的
租户审计列表，并且收集时不按本线程的工单与操作过滤。

- 先启动 T1、再启动 T2：T1 返回 3 个审计编号；T2 领域存在 3 条事件，线程返回 **0 个**。
- 同租户两线程 A/B 均建草稿，先后审批，再恢复 A：A 的 `audit_event_ids` 中出现 **B 的审批事件**。

这不是数据库审计被删掉，而是 HTTP/流程中的证据关联不可靠。按租户分水位只能解决第一个问题。

修复验收：从领域审计按租户及线程绑定实体重建关联，使用真实事件的稳定身份或可靠复合标识；
同一事件不会因轮询或进程重启重复，其他线程事件不会串入。本次未验证 PG 审计混入路径，修复时补齐。

## R4 — P1：对账与已执行恢复缺少关单收尾

定位：`src/agents/nodes.py:273`、`:277`、`:281`，`src/agents/runner.py:338`，
`src/api/app.py:375`。

两个独立 HTTP 场景均可复现：

1. start → 人工审批 → SYSTEM execute(timeout) → decision → 原 operation reconcile(success)。
   领域操作已经 `executed`，退款累计 **100.00**；再次 decision 返回 **409**，state 仍为
   `operation_unknown`，工单仍为 **open**。
2. start → 人工审批 → SYSTEM execute(success) → decision。`apply_decision` 看到已执行就直接
   结束，返回 `already_executed`、`finished=true`，工单仍为 **open**；返回审计还缺少 execute。

现有 unknown 单测只核对操作状态与退款金额，没有核对关单和工作流后续恢复。
现有 state 被定义为流程快照，所以不能将旧 outcome 本身误称为数据库状态错误；实际缺口是
流程缺少授权恢复到关单的路径，且界面没有明确反映业务已完成对账。

修复验收：沿用原 operation_id 和带版本审批事实，显式恢复只执行领域允许的收尾；已执行
禁止再次 execute，unknown 仍未确认时禁止关单。GET state 保持只读。覆盖对账 success/failed、
重复触发、关单失败、领域已执行但 checkpoint 未更新、新实例恢复；最终业务状态、金额、审计均核对。

## R5 — P2：重启验收出现一次失败，且现有测试不是真正进程重启

定位：`tests/integration/test_agent_restart_recovery_live.py:81`，特别是固定 `time.sleep(1.2)`
及“完全新进程”的注释。该测试实际在同一 Python 进程内先后构造两个 runner/checkpointer。

本轮全量中恢复请求报 `AGENT_THREAD_LEASE_HELD`，单独重跑通过；**原因未定位，不能直接归因于
实现租约缺陷或时钟**。应保留首次失败，检查数据库时钟、租约截止、测试隔离及调用顺序。
新增独立子进程 A/B 的 HTTP 复现，证明真正退出后能使用同一 PG 与同一 checkpoint 恢复；
使用数据库时间判断租约到期并设置有限等待，不靠扩大固定 sleep 或降低断言掩盖失败。

## R6 — P2：旧开发说明与执行指引偏离当前实现

修复前的开发手册仍把 399/443 作为当前测试数字，并描述 HTTP 生命周期尚未接入；
旧维护任务还包含可能误删模型模块的建议。以上为当时文档问题，不能继续指导当前开发。
远程 CI 成功说法在该轮未核验。

2026-09-16 已按个人开发与电商业务设计重写项目首页、开发手册、业务概览、开发计划与维护指引。
手册保留原有 15 个主题，修正 HTTP 已接通、用例数量和 CI 配置事实；原始测试日期与失败结果继续保留。
当前维护入口见[维护任务指引](./MAINTENANCE_TASK_GUIDE.md)。

## 当时的整改顺序与复现方式

当时按 R1/R2/R4 → R3/R5 → R6 逐项复现、修复、验证；既有全量通过项不能抵消补充检查失败。
以下注释记录修复前结果，当前执行应得到修复后的结果。

可重复运行的补充检查：

```powershell
. .\scripts\init_d_env.ps1
.venv\Scripts\python.exe scripts\review_v12_boundaries.py
# 修复前：6 FAIL，退出码 1；仅 memory，无数据库写入。

# 显式设置 OPSPILOT_TEST_DATABASE_URL 指向已迁移的本地 opspilot_test_* 隔离库后：
.venv\Scripts\python.exe scripts\review_v12_boundaries.py --pg
# 修复前：7 FAIL，退出码 1；额外仅创建合成 workflow_threads/checkpoint，不重置表。
```

该脚本是本次定点复现集，不替代全面安全审计或下一轮针对性回归；不得通过删除检查来获得退出码 0。

## 整改后复核结论（2026-09-13）

本节追加记录修复后的结果，前文的 6/7 FAIL、租约 409 和旧测试数字作为历史证据保留。

| 编号 | 修复 | 验证证据 | 结果 |
| --- | --- | --- | --- |
| R1 | checkpoint 改为 v2 长度编码；旧键仅在存储元数据与请求租户/线程精确匹配时兼容；歧义旧键 fail-closed | `test_thread_tenant_namespace.py`、边界脚本 `R1-memory-namespace` | PASS |
| R2 | HTTP 公共视图白名单；用户文本、回复、interrupt、嵌套值和验证错误统一脱敏 | `test_agent_http_lifecycle.py::test_public_agent_views_and_validation_errors_redact_pii` 及错误路径回归 | PASS |
| R3 | 审计按租户、线程和当前 ticket/operation 实体过滤；稳定事件 ID 与线程级去重 | Agent 审计单测、PG 全量、边界脚本 `R3-audit-*` | PASS |
| R4 | `decision` 可恢复 `reconcile_required`/终态 checkpoint；按领域事实收口 executed/rejected/failed，禁止 unknown 未确认时关单 | `test_resume_idempotency_and_unknown.py`、边界脚本 `R4-*` | PASS |
| R5 | 重启测试改为独立子进程；以 PostgreSQL `now()` 判定租约过期，不依赖固定 sleep | `test_restart_recovers_thread_and_continues`（独立进程）及 PG 全量 | PASS |

### 首轮整改后实测

- 离线：`494 passed / 49 skipped / 1 warning`。
- 隔离 PostgreSQL：`543 passed / 0 skipped / 1 warning`。
- `scripts/review_v12_boundaries.py`：R1–R4 全部 PASS（memory 与 PG 均运行）。
- `scripts/run_pg_tests_isolated.ps1`：两套随机后缀隔离库各 `25 passed`，双跑 PASS。

仍未实测的内容保持原口径：真实 LLM 指标、真实 Judge 分数、微调效果、性能吞吐、多实例部署和生产外部系统。所有数据均为固定种子合成数据；本报告不代表生产收益或生产部署证明。

## 独立复验（2026-09-13，审计迁移前）

本节数量已按提交 `67d6293` 与本地原始日志校正，后续迁移结果另列，不覆盖本轮。

上表由整改实施方记录；本节由复验方在同一工作树、当前 HEAD 上独立重跑，并补足此前缺少的
定点回归。历史 FAIL 数字与前节结果均保留，不覆盖。

| 复验项 | 命令 | 实际结果 |
| --- | --- | --- |
| 边界脚本（memory） | `scripts/review_v12_boundaries.py` | **6 PASS / 0 FAIL / 0 ERROR，退出码 0** |
| 边界脚本（隔离 PG） | `scripts/review_v12_boundaries.py --pg` | **7 PASS / 0 FAIL / 0 ERROR，退出码 0**（含 R1-pg-namespace） |
| 离线全量 | `pytest tests -q` | **503 passed / 51 skipped / 1 warning，11.0 s** |
| 隔离 PG 全量 | 新建并迁移唯一命名隔离库至 0005 后 `pytest tests -q` | **554 passed / 0 skipped / 1 warning，33.8 s** |
| 隔离双跑 | `scripts/run_pg_tests_isolated.ps1` | `opspilot_test_a_8c9dfc6f` / `b_8c9dfc6f` 各 **25 passed**，`ISOLATED DOUBLE-RUN PASS` |
| 业务验证与评测 | `demo_agent_http.py`、`verify_after_sales.py`、`replay --dataset golden_v1`、`compare_modes.py`、影子评测 offline、Judge offline | 全部退出码 0；黄金集 11/11；三模式 11/11 持平；影子 `tokens=0 / cost=N/A`；Judge 未实测 |
| 静态检查 | `node --check src/api/ui/workspace.js`、`git diff --check` | 均退出码 0 |

本轮补充的定点回归（此前无覆盖，先做变异验证再留档）：

- `tests/unit/agents/test_audit_association.py`（5 项）：跨租户交错、同租户两线程交错审批、
  重复 state/decision 不重复累计、新实例重建一致、编号可由领域审计事实逐条重建；
- `tests/e2e/test_agent_http_lifecycle.py`（+2 项）：clarify/decision/冲突错误路径与**日志**无 PII；
  脱敏后公共视图相同但原始文本不同的请求仍产生 409 冲突（指纹不被掩码合并）；
- `tests/unit/agents/test_resume_idempotency_and_unknown.py`（+1 项）：收尾关单失败保留原错误码
  与人工处理信息，重试后收尾且不重复执行、不重新建单；
- `tests/integration/test_agent_restart_recovery_live.py`（+2 项 PG live）：R1 冒号碰撞在 PG 中
  "先 start 后 GET" 仍隔离且零业务写入；循环保护终态跨实例存活且新实例推进被拒（无退款）。

回归有效性（变异验证，随后已恢复源码、工作树确认干净）：

```text
回退 collect_audit_events → tests/unit/agents/test_audit_association.py：2 failed, 3 passed
回退 _checkpoint_thread_key → tests/unit/agents/test_thread_tenant_namespace.py：3 failed, 2 passed
同一回退下 PG live collision 用例：1 failed（409 AGENT_THREAD_CONFLICT，与审查记录同症状）
```

复验方结论：R1–R5 的整改与本轮新增回归一致通过；剩余未验证项与前节相同（真实 LLM/Judge、
微调、性能、多实例部署、生产外部系统）。复验期间 `evals/reports/` 的 canonical 报告仅因重跑
产生时间戳差异，已还原为原提交内容，未作为新一轮证据。

## 审计持久标识补充验证（2026-09-13）

后续代码提交 `091f94c` 增加 `event_id` 持久化及迁移 `0006`，新事件生成标识，历史行按数据库 ID 回填。
PG HTTP 文件在既有 4 项上新增 3 项：公共视图脱敏、线程审计隔离、未知结果对账后收尾，合计 7 项。
无 ID 的旧领域对象仍保留兼容回退，不能表述为全部审计来源均已取消位置标识。

| 验证 | 记录结果 | 环境说明 |
| --- | --- | --- |
| 离线全量 | 502 passed / 55 skipped / 1 warning，11.20 s | 54 项 PG live 未启用；另 1 项运行时检查因默认库迁移条件不满足而跳过 |
| 隔离 PG 全量 | 557 passed / 0 skipped / 1 warning，37.08 s | 在已有隔离库升级至 0006 后复跑，不是新建空库迁移记录 |
| PG HTTP 定点回归 | 7 passed | 含新增 3 项业务边界回归 |
| 审计、行编解码及 PG 接入组合 | 18 passed | 持久 ID 与相关业务语义 |

该轮未取得真实 LLM、真实支付、生产吞吐或多实例并行的实测证据。
2026-09-16 只更新文档、说明文字与报告标题，并用收集检查核对现有用例，未重新执行完整业务回归。

### 当前可复现核查顺序

先按[测试基线](./TESTING_BASELINE.md)准备离线与隔离 PG 环境。
数据库服务、建库权限与迁移前提见[PostgreSQL 使用说明](./POSTGRES.md)，不得用共享库替代隔离库。

```powershell
. .\scripts\init_d_env.ps1
.venv\Scripts\python.exe scripts\review_v12_boundaries.py
# PG 环境在专用会话中准备，脚本将数据库变量指向最后一套隔离库。
.\scripts\run_pg_tests_isolated.ps1
if ($LASTEXITCODE -ne 0) { throw '隔离验证失败' }
.venv\Scripts\python.exe scripts\review_v12_boundaries.py --pg
.venv\Scripts\python.exe -m pytest tests -q
.venv\Scripts\python.exe scripts\demo_agent_http.py
.venv\Scripts\python.exe scripts\verify_after_sales.py
.venv\Scripts\python.exe evals\replay.py --dataset golden_v1
.venv\Scripts\python.exe evals\compare_modes.py
node --check src/api/ui/workspace.js
git diff --check
```

完整输出按当次日期与提交记录，不能将本文件的历史成功数字直接复制为新的运行结果。
工作台六条路径此前使用页面相同 API 序列验证，浏览器自动化仍未完成。

审计迁移前独立复验的原始证据（未提交，留在 D 盘运行时目录）：`.runtime/verify-v12-20260913-2209/`
（`boundaries-memory*.txt`、`boundaries-pg*.txt`、`offline-*.txt`、`pg-final.txt`、
`pg-isolated-final.txt`、`ui-paths.txt`、`acceptance-cli.txt`、`pg-restart.txt`），
上一轮审查证据备份在 `.runtime/review-20260913/pre-verify-20260913-2209/`。
