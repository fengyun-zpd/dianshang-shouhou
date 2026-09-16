# 电商售后业务测试与验证基线

> 文档更新：2026-09-16。业务测试数字保留各自实际运行日期，本次文档整理没有重新执行全量业务回归。
> 数据为固定种子合成样本；真实企业效益、真实模型表现及生产容量未实测。

## 最近已记录的完整回归（2026-09-13）

环境：D 盘 `.venv`，基础解释器 `D:\Anaconda`，Python 3.12.4。

| 运行 | 结果 | 说明 |
| --- | --- | --- |
| 离线全量 | **502 passed / 55 skipped / 1 warning，11.20 s** | 未启用隔离 PG；54 项 PG live 跳过，另 1 项运行时检查因默认库仍为旧迁移而跳过 |
| 隔离 PostgreSQL 全量 | **557 passed / 0 skipped / 1 warning，37.08 s** | 已有隔离库升级至 `0006` 后复跑，不表述为本轮空库首迁验证 |
| PG HTTP 接入定点回归 | **7 passed** | 在既有 4 项上新增 3 项：脱敏、审计隔离、未知结果对账后收尾 |
| 审计、行编解码与 PG 接入组合 | **18 passed** | 覆盖持久审计标识与相关业务路径 |

警告来自 Starlette/AnyIO 弃用提示。跳过数量取决于环境，不能固定认为所有 skip 都是同一种原因。
以上新增审计标识复跑对应代码提交 `091f94c`；详细历史证据见[可靠性审查记录](./RELIABILITY_REVIEW_2026-09-13.md)。

## 文档维护验证（2026-09-16）

本次统一项目说明、修正引用，并将七场景业务入口更名为 `scripts/verify_after_sales.py`。
验证范围与完整业务回归分开记录：

- 20 份 Markdown 文档完成用语、相对链接与引用路径检查；47 个相对链接均有效。
- 收集现有测试共 557 项，只核对数量，不将收集结果当作通过。
- 报告生成相关已有回归 **11 passed**，覆盖影子报告、模式对照与回放。
- 更名后的业务验证入口 **7 个合成场景通过**，保留正常、澄清、证据不足、拒绝、未知结果、跨租户及幂等断言。
- 6 份既有 Markdown 评测报告的指标和历史元数据保持原值；仅调整其中 5 份的标题或模式说明。

本次未重跑完整 PG 集成、真实模型或性能测试；上方完整回归数字仍为 2026-09-13 的记录。

## 历史验证分开记录

| 轮次 | 离线结果 | 隔离 PG 结果 | 其他记录 |
| --- | --- | --- | --- |
| 修复前审查，基线 `d5ed51f` | 490 passed / 49 skipped | 538 passed / 1 failed | 边界复现 7 FAIL；保留首次失败 |
| 首轮整改后 | 494 passed / 49 skipped | 543 passed / 0 skipped | 边界验证通过 |
| 补充独立复验，迁移 `0005` | 503 passed / 51 skipped | 554 passed / 0 skipped | memory 6 项 + PG 1 项边界通过；双隔离库各 25 passed |
| 审计标识迁移 `0006` 后 | 502 passed / 55 skipped | 557 passed / 0 skipped | 增加 3 项 PG 接入回归，另有环境导致的跳过差异 |

不同轮次对应不同代码和环境，不能把后一轮数字覆盖进早期记录。
双隔离库各 25 项、黄金集 11/11、三模式持平与边界脚本结果是对应运行的历史证据，不代表本次重新测量。

## 复跑前提与命令

离线模式在专用 PowerShell 会话中清除隔离测试库变量；本地 PostgreSQL 可用时，再使用项目脚本准备隔离库。
脚本有建库权限要求，会创建随机后缀测试库并迁移，不清空共享业务库。

```powershell
. .\scripts\init_d_env.ps1
Remove-Item Env:OPSPILOT_TEST_DATABASE_URL -ErrorAction SilentlyContinue
.venv\Scripts\python.exe -m pytest tests -q

# PG：先确认本地服务可用，且测试账号有建库权限。
.\scripts\run_pg_tests_isolated.ps1
if ($LASTEXITCODE -ne 0) { throw '隔离准备或回归失败' }
# 脚本已将两种数据库变量指向最后一套隔离库，后续全量在该库执行。
.venv\Scripts\python.exe -m pytest tests -q
```

每次以实际输出为准，不将文档中的历史数字当成固定断言。
隔离规则与数据库前提见[PostgreSQL 使用说明](./POSTGRES.md)。

## 重点回归覆盖（2026-09-16 收集核对）

以下数量来自用例收集，未重新执行测试。完整收集共 557 项，退出码 0。

| 文件 | 项数 | 覆盖 |
| --- | --- | --- |
| `tests/e2e/test_agent_http_lifecycle.py` | 31 | start/clarify/decision/state 正常与失败路径；身份边界（客户 403）；`extra="forbid"`（tenant_id/金额/角色/审批/外部结果 → 422）；跨租户统一 404 且不泄露租户；冒号碰撞 HTTP 隔离且零业务写入；**脱敏后视图相同≠同请求（指纹仍区分原始文本）**；clarify/decision/冲突路径与日志无 PII；SYSTEM 写 unknown 后 decision 重读；伪造 decision 422；线程冲突 409；reset profile 边界；503；循环错误码 |
| `tests/integration/test_agent_restart_recovery_live.py` | 5（PG live） | 两个独立 Python 进程 A/B 中断、租约按数据库时间到期、同一 PG + 固定 checkpoint 恢复；错误租户 404；同名线程跨租户；租约未过期时拒绝推进；**R1 PG 冒号碰撞（先 start 后 GET）隔离且零业务写入**；**循环终态跨实例存活且不得重启执行** |
| `tests/integration/test_run_api_pg_backend_live.py` | 7（PG live） | PG profile 使用真实 PG runner + D9 租约；不注册 memory reset；start→审批事实→decision→state 全链路以 `refund_operations` 行验收；伪造 decision body 422；公共视图脱敏、线程审计隔离、未知结果对账后收尾 |
| `tests/unit/agents/test_audit_association.py` | 5 | 审计按租户/线程/实体关联：T1→T2→T1 不串；同租户两线程交错审批不混入；重复 state/decision 不重复累计；新实例重建一致；编号可由领域审计事实逐条重建 |
| `tests/unit/agents/test_loop_protection.py` | 15 | step_count 增长；超限安全停止；重复澄清有界；空澄清载荷 no-op；工具去重账本；事实重读不缓存；recursion limit 第二道防线；**循环终态入持久 checkpoint（新实例可读）**；安全停止后再 resume 不产生新写入；unknown 不换键重试 |
| `tests/unit/agents/test_thread_tenant_namespace.py` | 5 | v2 长度编码命名空间；冒号碰撞；旧键精确元数据兼容与歧义拒绝；错误租户与不存在线程的通用 404；新实例显式租户恢复 |
| `tests/unit/agents/test_resume_idempotency_and_unknown.py` | 5 | 重复 resume 幂等；unknown 原键对账；对账失败保持开放；外部已执行先于恢复时不重复 execute；**关单失败保留原错误码与人工信息、重试后收尾且不重复执行** |
| `tests/unit/models/test_openai_compatible.py` | 18 | 请求构造/降级/脱敏/注入/预算；**证据块脱敏后才计数与发送（断言请求体无手机号/邮箱/身份证）**；日志无 PII；注入检查仍先于脱敏 |
| `tests/unit/models/test_cost_tracking.py` | 21 | 新/旧价格变量、双向成本、缺价 N/A、非法价格安全回退、报告成本列、Key 不入报告 |
| `tests/unit/models/test_shadow_zero_side_effect.py` | 5 | 不创建领域/数据库对象、零网络、高风险任务不进模型、预测字段不含金额/审批（全部写 `tmp_path`） |
| `tests/unit/evals/test_llm_judge.py` | 19 | rubric 加载/失败、PII 脱敏、不触发领域写、无 Key 不联网、业务错误不被高分掩盖、真实裁判 mock 路径 |

### 回归有效性的变异验证（2026-09-13 历史记录）

新回归不是复述实现细节：临时回退对应修复后，测试确实失败，随后恢复源码（工作树确认干净）：

```text
回退 collect_audit_events（改回全局水位的旧实现）→ test_audit_association.py：2 failed, 3 passed
回退 _checkpoint_thread_key（改回 "tenant:thread"） → test_thread_tenant_namespace.py：3 failed, 2 passed
同一回退下 PG live：test_pg_colon_collision_start_then_read_stays_isolated → 1 failed
  （症状与审查记录一致：409 AGENT_THREAD_CONFLICT）
```

## 分层复跑命令

```powershell
. .\scripts\init_d_env.ps1
.venv\Scripts\python.exe -m pytest tests/unit/api -q
.venv\Scripts\python.exe -m pytest tests/unit/agents -q
.venv\Scripts\python.exe -m pytest tests/unit/models -q
.venv\Scripts\python.exe -m pytest tests/unit/evals -q
.venv\Scripts\python.exe -m pytest tests/e2e/test_agent_http_lifecycle.py -q
.venv\Scripts\python.exe scripts\run_tests.py --quick
```

## 报告落盘约定（防污染）

- canonical 报告写入 `evals/reports/`：`shadow_eval_offline.md`、`judge_offline.md`、
  `golden_v1_report.md`、`golden_v2_report.md`、`mode_compare.md`、`agent_compare.md`、
  `rag_metrics.json`；
- **未实测的候选模式报告写入 `.runtime/reports/`**（D 盘运行时目录，不入库），
  避免把离线降级写成真实候选模型成绩；
- **单元测试一律使用 `tmp_path`，不得改写 `evals/reports/`**；
  可在跑完整套件前后比对报告文件哈希验证（此前运行已验证：哈希不变）。

PG 不可用时必须报告跳过，不能把内存结果写成 PG 实测。任一越权成功、重复副作用、未知状态
换键重试或非法迁移都是阻断问题。破坏性 PG 集成只允许指向 `OPSPILOT_TEST_DATABASE_URL`
（localhost + `opspilot_test_*` 前缀）；共享主库 `opspilot` 与通用 `DATABASE_URL` 永不被
DROP（`src/platform/pg_test_guard.py` fail-closed：非法 URL 在任何连接探测前直接失败）。
