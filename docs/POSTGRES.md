# PostgreSQL 业务事实源

PostgreSQL 保存订单、工单、退款操作、审批决定、幂等记录、审计事件、政策和工作流租约，是 V1 唯一业务事实源。MemoryAdapter 仅用于测试和合成演示；SQLite 只保存 LangGraph checkpoint。

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
# 一次性准备隔离测试库（含主库 opspilot 各一次 alembic upgrade head 后）
$env:OPSPILOT_TEST_DATABASE_URL = 'postgresql+psycopg2://opspilot:opspilot@127.0.0.1:5433/opspilot_test_v1'
.\.venv\Scripts\python.exe -m pytest tests/ -q -p no:cacheprovider
```

`scripts/run_pg_tests_isolated.ps1` 每次自动创建带随机后缀的 `opspilot_test_a_<run>` /
`opspilot_test_b_<run>` 双隔离库，建库失败立即终止；`evals/replay.py --profile pg` 的 `reset_schema` 同样受 guard 保护（指向
`opspilot_test_*` 才允许重建）。

`PgCommandService` 在事务中校验租户、角色、金额、政策版本、状态和 expected version，再写入业务行、审批、审计与幂等记录并重读返回。退款执行使用订单容量和锁/CAS；未知外部结果只能按原 `operation_id` 对账。`workflow_threads` 只保存 Runner 租约，不能替代业务事实。

已删除 `PgBackedSession` 整库清空重插和快照恢复原型。生产部署、生产备份、支付或 CRM 接入未实现。
