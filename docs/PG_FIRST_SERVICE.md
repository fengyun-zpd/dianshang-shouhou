# PG-first 命令服务设计（第三阶段）

> 版本：v0.1（2026-09-04，设计基线，尚未实现——不写成已实现）。
> 目标：把售后工单命令（create_ticket/create_refund_draft/submit/approve/reject/execute/
> reconcile/close_ticket）从"内存裁决 + 命令后全量镜像写（PgBackedSession）"升级为
> "命令即单数据库事务"：事务内读最新事实 → 租户/版本条件 → 订单行锁容量 → 写业务状态 →
> 幂等记录 → 追加审计 → 提交后返回；失败整体回滚并返回稳定错误码。
> 关联：缺陷台账 D7（废弃全量 clear/reinsert）、D8（政策/明细持久化）在此收编；
> 现状基线 `67f1687`：全量 342 passed / 3 xfailed（D7/D8/D12），PG 集成 18/18。

## 1. 非目标与本阶段边界

- **默认运行时切换**不在本设计内一步完成：内存 `AfterSalesService` 继续作为单测/演示/评测
  后端与规则参考实现；PG-first 以"可验证命令路径 + 集成证据"交付，切换默认运行时作为
  单独收口（需 golden/工作流/API 全部指向 PG 后端并通过）。
- 外部真实副作用（支付/退款接口）不在本阶段（见阶段五）。
- 不引入新规则；规则语义与错误码与内存 service 完全一致（错误码不可改写）。

## 2. 规则复用（避免漂移）

当前领域校验耦合内存 dict。PG-first 前先做一次**纯规则抽取**（行为不变，内存 service 改用之）：
- `src/domain/after_sales/rules.py`（纯函数，不触存储）：
  - `check_create_ticket_permission(actor)`、`check_create_refund_permission(actor)` 等权限；
  - 状态机迁移 `can_transition(status, target)`（表驱动 OPERATION_TRANSITIONS/TICKET_TRANSITIONS）；
  - 金额 `parse_money`/`amount_positive`/`capacity_ok(paid, executed_sum, amount)`；
  - 政策匹配 `match_policies/detect_conflict`（已纯，迁入）；
  - 版本校验 `decision_version_ok(current, expected)`；
  - 订单 eligibility（status != closed 等）。
- 内存 `AfterSalesService` 与 PG 命令服务共用 rules.py → 行为一致性由"同一纯函数 + 双后端
  契约测试"保证。

## 3. 数据面扩展（Alembic 0004 起，逐命令按需）

现有：orders / tickets / refund_operations / approval_decisions / idempotency_records /
audit_events（均 (tenant, id) 作用域；金额 NUMERIC(12,2)；状态/金额 CHECK）。

需新增（收编 D8，供命令内政策校验与订单明细恢复）：
1. `policies`：tenant_id, policy_id, request_type, reason_tags(TEXT JSON), window_days,
   refund_ratio NUMERIC(5,4), effective_from DATE, version INT, 主键 (tenant_id, policy_id)。
   生效期/版本列供后续 RAG 引用闭环（第八阶段）。
2. `order_items`：tenant_id, order_id, sku, name, quantity, unit_price NUMERIC(12,2)，
   外键 (tenant_id, order_id)→orders，主键 (tenant_id, order_id, sku)。订单明细持久化（D8）。
3. `entity_seq`（或 PG SEQUENCE）：`tenant_id, kind('ticket'|'operation'), next_seq INT`，
   主键 (tenant_id, kind)——以 (tenant) 隔离自增，替代内存全局 `_seq`（跨进程安全）。

## 4. Repository 命令面扩展（interfaces/memory/postgres 同步）

在既有表级 CRUD/行锁/unit_of_work 之上新增：
- policies：`insert_policy/list_policies`（命令内 policy 命中按 tenant+request_type+tags 过滤，
  可加 `list_policies(tenant_id, request_type)`）。
- order_items：`insert_order_item/list_order_items(tenant_id, order_id)`；orders 装载含明细
  （load 完整订单供 evidence/展示）。
- seq：`next_seq(tenant_id, kind) -> int`（事务内原子递增；内存实现锁+计数）。
- 操作状态迁移 CAS：现有 `update_operation_versioned(row, expected_version)` 仅支持
  status/executed；扩展以支持 version、decision_version、idempotency_key 各写路径所需列
  （或新增 `update_operation_state` WHERE 版本+状态条件），submit/approve/reject/
  execute(未知)/reconcile 各自 CAS。
- 工单状态迁移 CAS：`update_ticket_versioned(row, expected_version)`（close/resolve 用）。
- 幂等：现有 `insert_idem`(唯一约束)/`get_idem`；命令事务内完成 get-or-reserve 语义时用
  `insert_idem` 捕获 UniqueViolation → IDEMPOTENCY_CONFLICT（跨进程语义与内存 per-key 锁对齐）。

## 5. 命令事务模板（每个命令一致）

```
with repo.unit_of_work():                       # 单事务；异常整体回滚
    order = repo.lock_order_for_update(tenant, order_id)   # 订单行锁（金额/容量路径）
    row = repo.get_* 最新行
    rules.check_*(...) → 抛 AfterSalesError（稳定错误码，事务回滚）
    versioned CAS 更新（WHERE version=expected → OptimisticLockError → 稳定错误码）
    或 repo.insert_*(业务行)
    幂等：insert_idem 唯一冲突 → IDEMPOTENCY_CONFLICT
    repo.insert_audit(...)                       # 追加审计与业务写同事务
# 提交后返回构造的领域对象（读已提交行或返回写入参数快照）
```

各命令要点：
- create_ticket：权限 → 幂等(get_idem 命中同载荷返回原工单) → 订单 eligibility →
  政策命中（从 policies 表）→ next_seq → insert ticket + insert_idem + insert_audit。
- create_refund_draft：权限 → 工单状态 → 订单行锁 → 订单级 unknown 守卫 →
  容量（executed_sum+amount≤paid）→ next_seq → insert operation(draft) + 幂等 + 审计。
- submit：CAS pending_approval；approve/reject：expected_version CAS + 推进 version；
  execute：订单行锁 + 仅 approved + 容量 + executed；timeout→unknown；reconcile：
  unknown 仅原 op + 容量；close_ticket：订单行锁 + 无未决操作 + 定性 + 迁移 CAS。
- 失败均抛 `AfterSalesError(code, …)`，事务回滚零残留；repo/DB 级错误映射为稳定码。

## 6. 测试矩阵（新增）

- unit：`rules.py` 纯函数（权限/状态机/容量/版本）单测（无存储）。
- repo contract（memory+PG）：新增命令面方法契约测试。
- integration（PG live）：逐命令事务语义——成功提交可见、中途异常整单位回滚（无部分
  提交：业务行/幂等/审计零残留）、同订单并发 execute 单成功、同键并发单成功、跨租户拒绝、
  unknown 原键、approve/reject 同版本并发单成功（DB CAS）。
- e2e：完整链路（create→submit→approve→execute→close）在 PG-first 后端跑通并 SQL 断言行。
- 回归关联：D7 XFAIL 改写——PG-first 命令失败回滚后重读 DB 一致（不再存在镜像分叉）；
  D8 XFAIL 改写——policies/order_items 装载完整恢复；PgBackedSession 标废弃（仅测试兼容，
  文档声明不再作生产路径）。
- golden（v1/v2）/影子评测默认仍走内存后端（规则一致由 contract+同 rules.py 保证）；
  完成后追加"golden 在 PG-first 后端"对照轮（阶段七统一纳入）。

## 7. 里程碑切分（逐命令可验证落地）

1. rules.py 抽取 + 内存 service 改用（全量回归保持 342+）。
2. schema 0004（policies/order_items/entity_seq）+ repo 方法 + 契约测试。
3. create_ticket PG-first + 集成测试（D8 收编第一步）。
4. create_refund_draft/submit/approve/reject（版本 CAS）。
5. execute/reconcile/close_ticket（订单行锁容量/unknown 原键）。
6. 全命令 e2e + D7/D8 XFAIL 转 PASS + PgBackedSession 废弃标注 + 文档/STATUS 收口。

## 8. 风险与决策记录

- 规则漂移：以 rules.py 单一事实源 + 双后端契约测试缓解。
- 锁顺序：订单行锁 → 幂等唯一插入（与内存"订单锁→幂等锁"一致），避免死锁。
- 性能：每命令单事务 + 行锁粒度订单级；审计/幂等同事务保证无部分提交。
- 内存后端保留：单测/演示/评测（宪法允许"内存仅单元测试"；默认运行时切换单独收口）。
