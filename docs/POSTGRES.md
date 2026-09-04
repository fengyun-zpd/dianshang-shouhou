# PostgreSQL 唯一事实源（K4）

> 版本：v1.0（2026-09-04）。状态：Repository 契约与 PostgreSQL 实现在本地真实数据库上**实测通过**；
> 领域状态机的完整“搬迁到 PG”尚未实现（当前运行时仍为内存领域服务 + SQLite 恢复原型）。
> SQLite 仅恢复原型，**不能替代 PostgreSQL 作为生产唯一事实源**。

## 1. 启动本地 PostgreSQL（开发用）

```powershell
docker run -d --name opspilot-pg -e POSTGRES_PASSWORD=opspilot -e POSTGRES_DB=opspilot `
  -e POSTGRES_USER=opspilot -p 5433:5432 postgres:16-alpine
```

连接串：`postgresql+psycopg2://opspilot:opspilot@127.0.0.1:5433/opspilot`
（可用环境变量 `DATABASE_URL` 覆盖，alembic/集成测试均读取）。

## 2. Schema 与迁移

- `src/repo/schema.sql`：六张业务表（orders / tickets / refund_operations /
  approval_decisions / idempotency_records / audit_events）均带 `tenant_id`；
  金额 `NUMERIC(12,2)`；幂等键主键 `(tenant_id, idem_key)`（数据库唯一约束）；
  订单行锁/累计见文件尾注释。
- Alembic：`alembic.ini` + `alembic/env.py` + `alembic/versions/0001_init_after_sales.py`。
  已在本机 PG 实测：`python -m alembic -c alembic.ini upgrade head` → 版本 `0001`。

## 3. Repository 层（`src/repo/`）

| 文件 | 内容 |
| --- | --- |
| `interfaces.py` | `AfterSalesRepository` 契约：事务/行锁（FOR UPDATE）/乐观版本/唯一幂等/`try_execute_refund`（行锁内原子容量执行） |
| `memory.py` | 内存实现（契约参考/测试；线程安全模拟） |
| `postgres.py` | SQLAlchemy + psycopg2 真实实现 |

## 4. 测试结果（2026-09-04 本地 PG 实测）

- 单元契约（memory）：`tests/unit/repo/test_repo_contract.py` 6 项（CRUD/幂等唯一/乐观版本/顺序容量/租户隔离/并发容量单成功）✅
- 真实 PG 集成：`tests/integration/test_postgres_repository_live.py` 6 项
  （CRUD/幂等唯一 UniqueViolation/乐观冲突/顺序容量/**并发行锁**：同订单 60+60 两并发事务恰一个成功、累计 60 ✅）
- PG 不可达时集成测试自动 `skip` 并标注“数据库集成未实测”。

## 5. 覆盖与未覆盖（诚实边界）

**已覆盖**：表结构含 tenant、幂等唯一约束、精确金额类型、迁移、行锁原子容量执行、
乐观版本、审计/审批表记录、unknown 原键记录（幂等唯一约束支撑原键查询）。

**未覆盖/未实现**：领域状态机（create/approve/execute/reconcile/close 的全量 SQL 化编排）
仍留在 `src/domain/after_sales`（内存权威 + SQLite 快照）；把业务运行时整体切换到 PG
是后续工程；`orders` 行级版本由业务事务控制，Repository 已提供原语。

## 6. 修订记录

- v1.0（2026-09-04）—— schema、Alembic 迁移、Repository 接口/内存/PG 实现；本地 PG 集成实测通过。
