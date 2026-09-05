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
  已在本机 PG 实测：`python -m alembic -c alembic.ini upgrade head` → 版本 `0002`
  （0001 六表 + 0002 DB 级 CHECK 约束：金额非负/退款金额为正、工单与操作状态枚举）。
- 可复现启动（无 PostgreSQL 时）：`docker run -d --name opspilot-pg -e POSTGRES_PASSWORD=opspilot
  -e POSTGRES_DB=opspilot -e POSTGRES_USER=opspilot -p 5433:5432 postgres:16-alpine`；
  daemon/容器不可达时 PG 集成测试自动 `skip`（数据库集成未实测），不伪造通过。

## 3. Repository 层（`src/repo/`）

| 文件 | 内容 |
| --- | --- |
| `interfaces.py` | `AfterSalesRepository` 契约：事务/行锁（FOR UPDATE）/乐观版本/唯一幂等/`try_execute_refund`（行锁内原子容量执行）/**`unit_of_work`（命令级原子写作用域：跨表同事务、无部分提交、禁嵌套）** |
| `memory.py` | 内存实现（契约参考/测试；线程安全模拟） |
| `postgres.py` | SQLAlchemy + psycopg2 真实实现 |

## 4. 测试结果（2026-09-04 本地 PG 实测）

- 单元契约（memory）：`tests/unit/repo/test_repo_contract.py` **10 项**（CRUD/幂等唯一/乐观版本/顺序容量/租户隔离/并发容量单成功/unit_of_work 提交可见·回滚无部分·保留先态·禁嵌套）✅
- 真实 PG 集成：`tests/integration/test_postgres_repository_live.py` **13 项** ✅
  （CRUD / 幂等唯一 UniqueViolation / 乐观版本冲突 / 顺序与并发行锁容量（60+60 恰一成功，累计 60）/
   unknown 原键记录 / **DB CHECK 拒非法金额与非法状态** / **并发同幂等键恰一成功** /
   **unit_of_work：跨表原子提交·中途异常整单位回滚（无部分提交）·作用域内读自身写·禁嵌套**）
- 全量：`.venv\Scripts\python.exe -m pytest tests/ -q` → 318 passed（PG 容器运行时；无 PG 集成自动跳过）
- PG 不可达时集成测试自动 `skip` 并标注“数据库集成未实测”。

## 5. 覆盖与未覆盖（诚实边界）

**已覆盖**：表结构含 tenant、幂等唯一约束、精确金额类型（NUMERIC(12,2)）、DB 级 CHECK
（金额/状态枚举——数据库可独立阻止非法业务状态）、Alembic 0001+0002、行锁原子容量执行、
乐观版本、审计/审批表记录、unknown 原键记录、`unit_of_work` 命令级原子写（PG thread-local
连接复用：单个命令的多表落库整体提交或整体回滚；嵌套拒绝；集成实测无部分提交）。

**未覆盖/未实现**：领域状态机（create/approve/execute/reconcile/close 的全量 SQL 化编排）
仍留在 `src/domain/after_sales`（内存权威 + SQLite 快照）；把业务运行时整体切换到 PG
是后续工程（`unit_of_work` 已为此提供跨表原子写地基）；checkpoint（LangGraph）仅存流程
恢复状态、不写业务真相，与 DB 层无覆盖关系。

## 6. 修订记录

- v1.0（2026-09-04）—— schema、Alembic 迁移、Repository 接口/内存/PG 实现；本地 PG 集成实测通过。
- v1.1（2026-09-04）—— DB 级 CHECK 约束（Alembic 0002）：非法金额/非法状态由数据库独立拒绝；集成测试 9 项（新增 DB 约束与并发幂等唯一用例）。
- v1.2（2026-09-04）—— Repository 增加 `unit_of_work` 命令级原子写作用域：PG 以 thread-local 共享连接/事务（正常提交、异常回滚=无部分提交、禁嵌套），内存以锁+快照回滚模拟；全部读写方法接入同一连接上下文；契约测试 10 项、PG 集成 13 项；全量 318 passed。领域状态机整体 SQL 化仍为后续工程（本能力是其地基）。
