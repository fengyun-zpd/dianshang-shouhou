# PostgreSQL 唯一事实源（K4）

> 版本：v1.3（2026-09-04）。状态：Repository 契约与 PostgreSQL 实现在本地真实数据库上**实测通过**；
> PG-backed 领域会话（`src/persistence/pg_backed.py`）已把领域业务事实（订单/工单/操作/审批流水/
> 幂等/审计）整库镜像写入 PostgreSQL 并可装载重建（重启不丢事实、不重复副作用）。
> 领域状态机的**增量 SQL 化编排**（跨进程并发下的命令级 DB 事务编排）尚未实现，仍为后续工程；
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
- Alembic：`alembic.ini` + `alembic/env.py` + `alembic/versions/0001..0004`。
  已在本机 PG 实测：`python -m alembic -c alembic.ini upgrade head` → 版本 `0004`
  （0001 六表 + 0002 DB 级 CHECK + 0003 tickets 增 created_by/reason_tags +
   0004 PG-first 数据面：policies（含版本/生效期，`(tenant, policy, version)` 唯一与 ratio/window
   CHECK）/order_items（`(tenant, order, sku)` PK + FK + quantity/price CHECK）/entity_seq
   （租户作用域自增 `(tenant, kind)`，kind ∈ ticket|operation））。
- 可复现启动（无 PostgreSQL 时）：`docker run -d --name opspilot-pg -e POSTGRES_PASSWORD=opspilot
  -e POSTGRES_DB=opspilot -e POSTGRES_USER=opspilot -p 5433:5432 postgres:16-alpine`；
  daemon/容器不可达时 PG 集成测试自动 `skip`（数据库集成未实测），不伪造通过。

## 3. Repository 层（`src/repo/`）

| 文件 | 内容 |
| --- | --- |
| `interfaces.py` | `AfterSalesRepository` 契约：事务/行锁（FOR UPDATE）/乐观版本/唯一幂等/`try_execute_refund`（行锁内原子容量执行）/**`unit_of_work`（命令级原子写作用域：跨表同事务、无部分提交、禁嵌套）** |
| `memory.py` | 内存实现（契约参考/测试；线程安全模拟） |
| `postgres.py` | SQLAlchemy + psycopg2 真实实现（含 `list_*` 全表列举 / `clear_all` 恢复装载） |

## 3.1 PG-backed 领域会话（`src/persistence/pg_backed.py`）

`PgBackedSession`：把 `AfterSalesService.export_state()` 在 `unit_of_work()` 内整库清空并重插
（订单/工单/操作 + 审计派生的审批流水 + 幂等记录 + 审计），异常整单位回滚（无部分提交）；
`load()` 从 Repository 全表读取并装配全新领域服务（同套规则），可继续审批/执行/对账/关单，
同幂等键同载荷在重建实例上仍返回原结果（重启不重复副作用）；演示 `scripts/demo_pg_backed.py`。

**诚实边界**：单实例原型级运行时（命令仍内存裁决、命令后全量镜像写）；订单明细 items 与
政策规则（seed_policy）不入表（items 为展示数据、政策由调用方在 load 后 seed）；跨进程
并发一致性与增量 SQL 化编排（领域状态机整体迁移）仍未实现；checkpoint 只存流程状态，
业务事实一律以 PG 为准并可由此重建。

## 4. 测试结果（2026-09-04 本地 PG 实测）

- 单元契约（memory）：`tests/unit/repo/test_repo_contract.py` **10 项**（CRUD/幂等唯一/乐观版本/顺序容量/租户隔离/并发容量单成功/unit_of_work 提交可见·回滚无部分·保留先态·禁嵌套）✅
- 真实 PG 集成：`tests/integration/test_postgres_repository_live.py` **13 项** ✅
  （CRUD / 幂等唯一 UniqueViolation / 乐观版本冲突 / 顺序与并发行锁容量（60+60 恰一成功，累计 60）/
   unknown 原键记录 / **DB CHECK 拒非法金额与非法状态** / **并发同幂等键恰一成功** /
   **unit_of_work：跨表原子提交·中途异常整单位回滚（无部分提交）·作用域内读自身写·禁嵌套**）
- PG-backed 会话集成：`tests/integration/test_pg_backed_live.py` **4 项** ✅
  （save 后事实以行表真实存在（SQL 直接断言：executed 状态/金额/decision_version/审批流水/幂等）/
   save→load 保真 / save 中途失败整单位回滚（先落镜像不被部分覆盖）/ 重启装载后重复请求无重复
   副作用并可继续关单、审计在重建实例上追加）
- 全量：`.venv\Scripts\python.exe -m pytest tests/ -q` → 439 passed, 0 xfailed（PG 容器运行时；无 PG 集成自动跳过）
- PG 不可达时集成测试自动 `skip` 并标注“数据库集成未实测”。

## 5. 覆盖与未覆盖（诚实边界）

**已覆盖**：表结构含 tenant、幂等唯一约束、精确金额类型（NUMERIC(12,2)）、DB 级 CHECK
（金额/状态枚举——数据库可独立阻止非法业务状态）、Alembic 0001+0003、行锁原子容量执行、
乐观版本、审计/审批表记录、unknown 原键记录、`unit_of_work` 命令级原子写（PG thread-local
连接复用：单个命令的多表落库整体提交或整体回滚；嵌套拒绝；集成实测无部分提交）、
`PgBackedSession` 整库镜像写/装载重建（事实确在 PG 行表，SQL 可查；重启不重复副作用）。

**未覆盖/未实现**：领域状态机（create/approve/execute/reconcile/close 的**增量 SQL 化编排**）
仍留在 `src/domain/after_sales`（内存裁决）；跨进程并发一致性与"命令即单事务"的整体 SQL
迁移是后续工程（`unit_of_work` + `PgBackedSession` 已提供原子写与事实重建基础）；订单明细
items 与政策规则不入表（展示/配置数据）；checkpoint（LangGraph）仅存流程恢复状态、不写业务
真相，与 DB 层无覆盖关系。

## 6. 修订记录

- v1.0（2026-09-04）—— schema、Alembic 迁移、Repository 接口/内存/PG 实现；本地 PG 集成实测通过。
- v1.1（2026-09-04）—— DB 级 CHECK 约束（Alembic 0002）：非法金额/非法状态由数据库独立拒绝；集成测试 9 项（新增 DB 约束与并发幂等唯一用例）。
- v1.2（2026-09-04）—— Repository 增加 `unit_of_work` 命令级原子写作用域：PG 以 thread-local 共享连接/事务（正常提交、异常回滚=无部分提交、禁嵌套），内存以锁+快照回滚模拟；全部读写方法接入同一连接上下文；契约测试 10 项、PG 集成 13 项；全量 318 passed。领域状态机整体 SQL 化仍为后续工程（本能力是其地基）。
- v1.3（2026-09-04）—— PG-backed 领域会话：`src/persistence/pg_backed.py`（整库镜像写/行表重建 + 保真 codec）、Alembic 0003（tickets 增 created_by/reason_tags）、Repository 恢复装载方法（`list_*`/`clear_all`）、演示脚本与集成测试 4 项（事实确在 PG/roundtrip/失败回滚/重启不重复副作用）；全量 330 passed（unit 281 / integration 17）。诚实边界：增量 SQL 化编排仍未实现。
