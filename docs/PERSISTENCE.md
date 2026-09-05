# 可恢复唯一事实源原型（SQLite，阶段主线后工程）

> 归属：电商售后多智能体工单系统（OpsPilot）。版本：v1.0（2026-09-04）。
> 目的：在不改动领域核心规则的前提下，验证"领域服务状态 + 审计可落库、重启可恢复、
> 恢复后可持续执行"的唯一事实源模式；为未来 PostgreSQL + alembic 提供先例与测试。

## 1. 结构

| 模块 | 职责 |
| --- | --- |
| `src/domain/after_sales/service.py` | 新增 `export_state()` / `restore_state()`：领域状态（订单/政策/工单/操作/累计退款/幂等记录/审计/序号）整体导出与恢复；**不改任何规则/状态机/权限/幂等语义** |
| `src/domain/idempotency.py` | 新增 `export_records()` / `import_records()` |
| `src/persistence/codec.py` | dataclass/Enum/Decimal/tuple ↔ JSON 安全编解码（str-mixin 枚举优先于 str 判断，防止退化） |
| `src/persistence/store.py` | SQLite **append-only** journal：`save_snapshot` / `latest_snapshot`（checksum 校验）/ `history` / `clear`；损坏或篡改 → `SnapshotCorruptionError`（fail-closed） |
| `src/persistence/session.py` | `RecoverableSession(db, build_service)`：`load()`（有快照则校验恢复，否则全新）、`persist(svc)` |
| `scripts/demo_persistence.py` | 一键演示：运行→落库→“重启”→恢复→审批执行→再落库→再恢复核对 |

## 2. 恢复保真与安全

- 状态一致：恢复后 `export_state()` 与落库前逐字段相等（测试断言）；
- 幂等水位恢复：恢复后同幂等键同载荷返回原操作（不重复建单/退款）；
- 恢复后可续：恢复实例上可继续 审批 → 执行 → 关单，审计追加且不丢历史；
- 损坏/篡改（payload 改坏、checksum 不匹配、JSON 非法）→ 拒绝恢复（fail-closed），绝不半恢复；
- 跨数据库文件隔离。

## 3. 严格校验与原子恢复（任务卡 J）

- `src/persistence/validate.py`：快照在恢复前经历**完整结构/语义校验**——顶层键集合精确、
  引用关系（工单→订单、操作→工单/订单一致）、租户一致、金额为有限 Decimal 且非负/为正、
  refunded == 已执行求和且 ≤ 实付、seq 单调、`executed` 与状态一致、工单关闭时操作均终态、
  审计与幂等记录引用存在、无 `<pending>` 占位；
- 任何解码/校验/恢复错误统一为 `SnapshotCorruptionError`（fail-closed）；
- `RecoverableSession.restore_into(svc, snapshot)`：先解码+校验，失败时**原服务状态完全不变**，
  通过后 `restore_state` 一次性替换（原子恢复）；
- 幂等记录支持 `lock_for`（per-key）/`get_or_reserve`/`commit`/`release`，
  `create_ticket`/`create_refund` 在 per-key 锁内 check-then-act（原子幂等，并发同键只产生一个对象）。

## 4. 运行

```powershell
.venv\Scripts\python.exe scripts\demo_persistence.py
.venv\Scripts\python.exe -m pytest tests/unit/persistence -v   # 37 项（原型 6 + 校验 12 + 原子恢复 4 + K3 严格类型/恢复安全 15）
```

全量回归：`.venv\Scripts\python.exe -m pytest tests/` → 334 passed（2026-09-04 实测；PG 容器运行时集成 18/18）。

## 5. 限制与下一步（诚实边界）

- **SQLite 仅为恢复原型，不是生产唯一事实源**：本文档描述的内存领域服务 + 全量快照落库，
  非事务型持久化，仅用于验证"落库-恢复-续跑"模式；
- **PostgreSQL 唯一事实源已落地**（任务卡 K4，`docs/POSTGRES.md`）：Repository 接口 + 内存/PG 实现、
  schema/Alembic 0001+0003（含 DB 唯一约束与 CHECK）、`FOR UPDATE` 行锁与 `try_execute_refund`
  原子容量执行、`unit_of_work` 命令级原子写、`PgBackedSession` 整库镜像写/装载重建
  （`src/persistence/pg_backed.py`，事实确在 PG 行表），已在本地 PG 集成实测 18/18（无 PG 自动跳过）；
- 领域状态机（create/approve/execute/reconcile/close 的**增量 SQL 化编排**）仍未实现：
  当前领域命令仍内存裁决（PG-backed 会话为命令后全量镜像写、单实例原型），跨进程并发一致性
  与"命令即单事务"的整体 SQL 迁移是后续工程；
- 快照为全量（非增量 WAL）；
- 未改变任何既有领域规则与安全不变量。

## 6. 修订记录

- v1.0（2026-09-04）—— 首版：导出/恢复接口、SQLite append-only 存储、恢复会话与演示。
- v1.1（2026-09-04）—— 任务卡 J：快照严格校验、原子恢复（restore_into）、幂等 per-key 原子语义。
- v1.2（2026-09-04）—— K3：顶层字段类型严格（schema_version/seq 仅合法 int，拒小数截断/str/bool；集合类型校验）；reason_tags/嵌套结构与跨租户校验；`restore_state` 原子（先构造后替换，失败零部分变更）；新增损坏/伪造 checkpoint（指纹篡改）/跨租户/重复/并发恢复测试；明确 SQLite 仅为恢复原型。
- v1.3（2026-09-04）—— 与 K4 现状对齐：删除"PostgreSQL 为规划"的过时表述，改为指向 `docs/POSTGRES.md`（已实现并本地实测 9/9）；保留"领域状态机整体 SQL 化未实现"诚实边界；全量基线 310 passed。
