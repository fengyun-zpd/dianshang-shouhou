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

## 3. 运行

```powershell
.venv\Scripts\python.exe scripts\demo_persistence.py
.venv\Scripts\python.exe -m pytest tests/unit/persistence -v   # 6 项
```

全量回归：`pytest tests/` → 200 passed（194 + 6）。

## 4. 限制与下一步（诚实边界）

- 本原型是**内存领域服务 + 全量快照落库**，非真正的事务型持久化：
  单命令粒度原子性、并发与行级锁由未来 PostgreSQL 实现；
- 快照为全量（非增量 WAL）；幂等唯一约束仍在应用层（PostgreSQL 唯一约束为规划）；
- 生产路线：SQLAlchemy/Alembic + PostgreSQL（含 pgvector），把本层接口作为迁移契约；
- 未改变任何既有领域规则与安全不变量。

## 5. 修订记录

- v1.0（2026-09-04）—— 首版：导出/恢复接口、SQLite append-only 存储、恢复会话与演示。
