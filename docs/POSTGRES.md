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

PG profile 下 `/api/v1/agent/*`（Agent 生命周期 HTTP，仅内部坐席）由同一个 `build_pg_backend`
产出的 `WorkflowRunner` 驱动：真实 `PgCommandAdapter` + **固定** SQLite checkpoint
（`.runtime/checkpoints/opspilot-agent.sqlite`，可用 `--checkpoint` 覆盖；不使用 PID 临时文件）
+ `workflow_threads` 租约。审批事实从 PG 读取，`decision` 接口只触发重读；
`/api/demo/reset` 在 pg profile 下**不注册**（该路由只属于 memory 合成演示后端）。

线程唯一键是 `(tenant_id, thread_id)`：**绑定事实源是租户限定的 `workflow_threads` 行 + 持久
checkpoint**，进程内字典只是便利缓存。因此实例 A 中断后进程退出（不释放租约）、租约过期，实例 B
用同一 PG 与同一 checkpoint 即可接管并继续执行；同名线程在不同租户下互不冲突；错误租户读取与
"线程不存在"返回**同一个** 404，消息不含所属租户。租约语义不变：他人持约未过期时推进被拒绝。
checkpoint 键使用带版本的长度编码 `v2|租户长度|租户|线程长度|线程`，不受标识符中冒号影响；
旧键只有在 checkpoint 内嵌租户/线程与请求精确一致时才兼容，歧义旧键直接拒绝。

实测（隔离库 live）：

```powershell
.venv\Scripts\python.exe -m pytest tests/integration/test_run_api_pg_backend_live.py -q
# → 4 passed（start→审批事实→decision→state 全链路，以 refund_operations 行金额验收；伪造 decision body 422）
.venv\Scripts\python.exe -m pytest tests/integration/test_agent_restart_recovery_live.py -q
# → 3 passed（独立进程 A/B；实例 A 中断 → 按 PostgreSQL 时间等待租约到期 → 实例 B 恢复；错误租户 404；同名线程；租约仍生效）
```

2026-09-13 整改验收全量结果：离线 `494 passed / 49 skipped`；隔离 PG `543 passed / 0 skipped`。
这两个数字来自固定种子合成数据和本机单次运行，不代表生产吞吐或真实企业收益。

已删除 `PgBackedSession` 整库清空重插和快照恢复原型。生产部署、生产备份、支付或 CRM 接入未实现。
固定 checkpoint 默认面向单实例；多实例部署应各自指定 `--checkpoint` 或改用服务端 checkpointer
（当前**未验证**多实例并行）。
