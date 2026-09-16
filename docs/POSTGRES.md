# PostgreSQL 业务事实源

> 电商售后订单、退款、审批与审计的持久化设计。文档更新：2026-09-16。

PostgreSQL 保存订单、工单、退款操作、审批决定、幂等记录、审计事件、政策和工作流租约，是持久化运行模式的唯一业务事实源。MemoryAdapter 仅用于测试和合成业务验证；SQLite 只保存 LangGraph checkpoint。

## 本地开发

```powershell
docker run -d --name opspilot-pg -e POSTGRES_PASSWORD=opspilot -e POSTGRES_DB=opspilot `
  -e POSTGRES_USER=opspilot -p 5433:5432 postgres:16-alpine
$env:DATABASE_URL = 'postgresql+psycopg2://opspilot:opspilot@127.0.0.1:5433/opspilot'
. .\scripts\init_d_env.ps1
.venv\Scripts\python.exe -m alembic -c alembic.ini upgrade head
```

本地容器不连接真实企业系统。

## 验收隔离

PG 回放和破坏性集成测试会重建 schema，因此不能把通用 `DATABASE_URL`、开发库或共享库直接
传给它们。已实现隔离数据库 guard（`src/platform/pg_test_guard.py`，fail-closed）：只允许
host ∈ {localhost, 127.0.0.1} 且库名以 `opspilot_test_*` 开头的显式测试库，非本机地址、
非测试库名、共享/系统库名（opspilot/postgres/template*）或畸形 URL 一律拒绝且不执行任何
SQL。破坏性 live 测试的唯一目标是环境变量 `OPSPILOT_TEST_DATABASE_URL`（**不回退**
`DATABASE_URL`）；未设置或不可达时这些测试如实 skip。

```powershell
# 前提：本地 PostgreSQL 可用，且测试账号可创建数据库。
# 在专用 PowerShell 会话运行；脚本创建两套随机命名隔离库并迁移到 head。
. .\scripts\init_d_env.ps1
.\scripts\run_pg_tests_isolated.ps1
if ($LASTEXITCODE -ne 0) { throw '隔离数据库准备或验证失败' }
# 脚本将 DATABASE_URL 和 OPSPILOT_TEST_DATABASE_URL 留在最后一套隔离库。
.venv\Scripts\python.exe -m pytest tests/ -q -p no:cacheprovider
```

`scripts/run_pg_tests_isolated.ps1` 每次自动创建带随机后缀的 `opspilot_test_a_<run>` /
`opspilot_test_b_<run>` 双隔离库，建库失败立即终止；`evals/replay.py --profile pg` 的 `reset_schema` 同样受 guard 保护（指向
`opspilot_test_*` 才允许重建）。

`PgCommandService` 在事务中校验租户、角色、金额、政策版本、状态和 expected version，再写入业务行、审批、审计与幂等记录并重读返回。退款执行使用订单容量和锁/CAS；未知外部结果只能按原 `operation_id` 对账。`workflow_threads` 只保存 Runner 租约，不能替代业务事实。

PG profile 下 `/api/v1/agent/*`（Agent 生命周期 HTTP，仅内部坐席）由同一个 `build_pg_backend`
产出的 `WorkflowRunner` 驱动：真实 `PgCommandAdapter` + **固定** SQLite checkpoint
（`.runtime/checkpoints/opspilot-agent.sqlite`，可用 `--checkpoint` 覆盖；不使用 PID 临时文件）
+ `workflow_threads` 租约。审批事实从 PG 读取，`decision` 接口只触发重读；
`/api/demo/reset` 在 pg profile 下**不注册**（该路由只属于 memory 合成业务验证后端）。

线程唯一键是 `(tenant_id, thread_id)`：**绑定事实源是租户限定的 `workflow_threads` 行 + 持久
checkpoint**，进程内字典只是便利缓存。因此实例 A 中断后进程退出（不释放租约）、租约过期，实例 B
用同一 PG 与同一 checkpoint 即可接管并继续执行；同名线程在不同租户下互不冲突；错误租户读取与
"线程不存在"返回**同一个** 404，消息不含所属租户。租约语义不变：他人持约未过期时推进被拒绝。
checkpoint 键使用带版本的长度编码 `v2|租户长度|租户|线程长度|线程`，不受标识符中冒号影响；
旧键只有在 checkpoint 内嵌租户/线程与请求精确一致时才兼容，歧义旧键直接拒绝。
审计事件使用 Alembic `0006` 增加的持久化 `event_id`，并有非空值唯一索引；历史行按数据库 ID 回填。
字段目前允许空值，无 ID 的旧领域对象仍有基于列表位置的兼容回退；新事件优先引用持久标识。

收尾恢复（R4）与审计关联（R3）同样以 PG 事实为准：`decision` 恢复时从 `refund_operations`
重读状态，只在 `executed`/`rejected` 时调用既有领域关单命令收尾（绝不再次执行），`failed`
保持工单开放转人工，`unknown` 未用原 `operation_id` 对账前不关单；`audit_event_ids` 由
`audit_events`（`ORDER BY id`）按租户 + 本线程 ticket/operation 过滤重建，稳定且可去重。

最近已记录的实测命令（2026-09-13，隔离库 live；执行前准备上方测试环境）：

```powershell
.venv\Scripts\python.exe -m pytest tests/integration/test_run_api_pg_backend_live.py -q
# → 最近记录为 7 passed；包含领域行金额验收、伪造审批拒绝、PII 脱敏、审计隔离及未知结果对账收尾
.venv\Scripts\python.exe -m pytest tests/integration/test_agent_restart_recovery_live.py -q
# → 5 passed（独立进程 A/B；实例 A 中断 → 按 PostgreSQL 时间等待租约到期 → 实例 B 恢复；错误租户 404；
#   同名线程；租约仍生效；R1 冒号碰撞先 start 后 GET 仍隔离；循环终态跨实例存活且推进被拒）
```

2026-09-13 0006 迁移后整改复验全量结果：离线 `502 passed / 55 skipped`；隔离 PG `557 passed /
0 skipped`；边界脚本 `scripts/review_v12_boundaries.py` memory 6 项 + PG 1 项全部 PASS（退出码 0）。
这些数字来自固定种子合成数据和本机单次运行，不代表生产吞吐或真实企业收益。

已删除 `PgBackedSession` 整库清空重插和快照恢复原型。生产部署、生产备份、支付或 CRM 接入未实现。
固定 checkpoint 默认面向单实例。多实例并行、共享恢复状态与高可用需要独立设计和验证，
不能仅通过分配不同 checkpoint 文件推定可接续同一线程。当前只验证顺序重启恢复。
