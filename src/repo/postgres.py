"""PostgreSQL Repository（K4，真实实现）。

要求：PostgreSQL 运行中（本地开发见 docker：postgres:16-alpine，端口 5433；
可用环境变量 DATABASE_URL 覆盖，如 postgresql+psycopg2://user:pw@host:port/db）。
表结构见 src/repo/schema.sql（CREATE TABLE IF NOT EXISTS，可重复应用）。

并发语义：
- try_execute_refund：在单个事务内 SELECT … FOR UPDATE 锁订单行 →
  读取 executed 累计 → 容量校验 → 插入 executed 操作；行锁串行化同订单并发执行；
- idempotency_records 主键 (tenant_id, idem_key) → 数据库唯一约束；
- 乐观版本：update_*_versioned 以 WHERE version=expected 执行，rowcount=0 → 冲突；
- unit_of_work：命令级原子写作用域——进入后本实例全部读写方法复用同一连接与事务
  （thread-local），正常退出提交、异常回滚（无部分提交）；不允许嵌套；
  作用域内请勿使用独立开事务的行锁方法（lock_order_for_update / with_order_lock）。
"""
from __future__ import annotations

import threading
from contextlib import contextmanager
from decimal import Decimal
from typing import Iterator, Optional

from sqlalchemy import create_engine, text
from sqlalchemy.exc import IntegrityError

from .interfaces import (
    AfterSalesRepository,
    ApprovalRow,
    AuditRow,
    IdemRow,
    OperationRow,
    OptimisticLockError,
    OrderItemRow,
    OrderRow,
    PolicyRow,
    TicketRow,
    UniqueViolation,
)


class PostgresAfterSalesRepository(AfterSalesRepository):
    def __init__(self, url: str):
        self._engine = create_engine(url, pool_pre_ping=True)
        self._tl = threading.local()  # unit_of_work 作用域：conn（连接）与 active（嵌套检测）

    # ---------- 事务 ----------
    def transaction(self):
        return self._engine.begin()

    @contextmanager
    def unit_of_work(self) -> Iterator[None]:
        """命令级原子写作用域：作用域内所有读写复用同一连接与事务。
        正常退出 commit；异常 rollback（无部分提交）；嵌套直接报错。"""
        if getattr(self._tl, "active", False):
            raise RuntimeError("unit_of_work 不允许嵌套（请勿在作用域内再次进入）")
        conn = self._engine.connect()
        tx = conn.begin()
        self._tl.conn = conn
        self._tl.active = True
        try:
            yield
            tx.commit()
        except BaseException:
            tx.rollback()
            raise
        finally:
            self._tl.active = False
            self._tl.conn = None
            conn.close()

    @contextmanager
    def _tx(self) -> Iterator:
        """方法级连接上下文：作用域内返回共享连接（事务由外层 unit_of_work 收尾）；
        作用域外等价于自开事务（commit/rollback）。"""
        conn = getattr(self._tl, "conn", None)
        if conn is not None:
            yield conn
            return
        with self._engine.begin() as conn:
            yield conn

    def lock_order_for_update(self, tenant_id: str, order_id: str) -> Optional[OrderRow]:
        """以 FOR UPDATE 读订单行（单语句事务内持锁，随即释放）；
        长时间持锁的原子容量执行请使用 try_execute_refund / with_order_lock。"""
        with self._tx() as conn:
            row = conn.execute(text(
                "SELECT tenant_id, order_id, customer_id, status, paid_amount, "
                "days_since_sign, version FROM orders "
                "WHERE tenant_id=:t AND order_id=:o FOR UPDATE"
            ), {"t": tenant_id, "o": order_id}).fetchone()
            return self._order_from(row) if row else None

    @contextmanager
    def with_order_lock(self, tenant_id: str, order_id: str) -> Iterator[Optional[OrderRow]]:
        """持订单行锁直到业务代码（yield 体）执行完成：事务内 SELECT … FOR UPDATE 持锁，
        业务代码在未提交事务中运行（其他事务同订单写被阻塞），退出后统一 commit/rollback
        释放锁（供需要"锁内做多步"的编排，如第三阶段命令级事务）。

        注意：yield 体内请勿调用本仓库中会自开事务/新连接的写方法（会等待本锁而阻塞）；
        需要锁内多表写时请直接使用同一连接的事务编排（unit_of_work / PG-first 命令服务）。
        """
        conn = self._engine.connect()
        tx = conn.begin()
        try:
            row = conn.execute(text(
                "SELECT tenant_id, order_id, customer_id, status, paid_amount, "
                "days_since_sign, version FROM orders "
                "WHERE tenant_id=:t AND order_id=:o FOR UPDATE"
            ), {"t": tenant_id, "o": order_id}).fetchone()
            yield self._order_from(row) if row else None
            tx.commit()
        except BaseException:
            tx.rollback()
            raise
        finally:
            conn.close()

    # ---------- orders ----------
    def get_order(self, tenant_id: str, order_id: str) -> Optional[OrderRow]:
        with self._tx() as conn:
            row = conn.execute(text(
                "SELECT tenant_id, order_id, customer_id, status, paid_amount, "
                "days_since_sign, version FROM orders WHERE tenant_id=:t AND order_id=:o"
            ), {"t": tenant_id, "o": order_id}).fetchone()
            return self._order_from(row) if row else None

    def insert_order(self, row: OrderRow) -> None:
        with self._tx() as conn:
            conn.execute(text(
                "INSERT INTO orders (tenant_id, order_id, customer_id, status, "
                "paid_amount, days_since_sign, version) VALUES (:t,:o,:c,:s,:a,:d,:v)"
            ), self._order_params(row))

    def update_order_versioned(self, row: OrderRow, expected_version: int) -> None:
        with self._tx() as conn:
            res = conn.execute(text(
                "UPDATE orders SET status=:s, version=version+1 WHERE tenant_id=:t "
                "AND order_id=:o AND version=:ev"
            ), {"t": row.tenant_id, "o": row.order_id, "s": row.status, "ev": expected_version})
            if res.rowcount == 0:
                raise OptimisticLockError(f"订单 {row.order_id} 版本冲突（期望 {expected_version}）")

    # ---------- tickets ----------
    def insert_ticket(self, row: TicketRow) -> None:
        with self._tx() as conn:
            conn.execute(text(
                "INSERT INTO tickets (tenant_id, ticket_id, order_id, customer_id, "
                "request_type, reason, status, resolution, version, created_by, reason_tags) "
                "VALUES (:t,:tid,:o,:c,:rt,:r,:s,:res,:v,:cb,:rtags)"
            ), {**self._ticket_params(row)})

    def get_ticket(self, tenant_id: str, ticket_id: str) -> Optional[TicketRow]:
        with self._tx() as conn:
            row = conn.execute(text(
                "SELECT tenant_id, ticket_id, order_id, customer_id, request_type, "
                "reason, status, resolution, version, created_by, reason_tags FROM tickets "
                "WHERE tenant_id=:t AND ticket_id=:id"
            ), {"t": tenant_id, "id": ticket_id}).fetchone()
            return self._ticket_from(row) if row else None

    # ---------- refund_operations ----------
    def insert_operation(self, row: OperationRow) -> None:
        with self._tx() as conn:
            conn.execute(text(
                "INSERT INTO refund_operations (tenant_id, operation_id, ticket_id, order_id, "
                "op_type, amount, status, idempotency_key, created_by, version, "
                "decision_version, executed) VALUES "
                "(:t,:op,:tk,:o,:ot,:a,:s,:ik,:cb,:v,:dv,:ex)"
            ), self._operation_params(row))

    def get_operation(self, tenant_id: str, operation_id: str) -> Optional[OperationRow]:
        with self._tx() as conn:
            row = conn.execute(text(
                "SELECT tenant_id, operation_id, ticket_id, order_id, op_type, amount, "
                "status, idempotency_key, created_by, version, decision_version, executed "
                "FROM refund_operations WHERE tenant_id=:t AND operation_id=:op"
            ), {"t": tenant_id, "op": operation_id}).fetchone()
            return self._operation_from(row) if row else None

    def update_operation_versioned(self, row: OperationRow, expected_version: int) -> None:
        with self._tx() as conn:
            res = conn.execute(text(
                "UPDATE refund_operations SET status=:s, executed=:ex, version=version+1 "
                "WHERE tenant_id=:t AND operation_id=:op AND version=:ev"
            ), {"t": row.tenant_id, "op": row.operation_id, "s": row.status,
                "ex": row.executed, "ev": expected_version})
            if res.rowcount == 0:
                raise OptimisticLockError(f"操作 {row.operation_id} 版本冲突")

    def executed_sum_for_order(self, tenant_id: str, order_id: str) -> Decimal:
        with self._tx() as conn:
            value = conn.execute(text(
                "SELECT COALESCE(SUM(amount), 0) FROM refund_operations "
                "WHERE tenant_id=:t AND order_id=:o AND status='executed'"
            ), {"t": tenant_id, "o": order_id}).scalar()
            return Decimal(str(value or 0)).quantize(Decimal("0.01"))

    def try_execute_refund(self, tenant_id: str, order_id: str, operation: OperationRow) -> bool:
        """单事务：FOR UPDATE 锁订单 → 容量校验 → 插入 executed 操作。并发安全。"""
        with self._tx() as conn:
            order = conn.execute(text(
                "SELECT paid_amount FROM orders WHERE tenant_id=:t AND order_id=:o FOR UPDATE"
            ), {"t": tenant_id, "o": order_id}).fetchone()
            if order is None:
                return False
            current = conn.execute(text(
                "SELECT COALESCE(SUM(amount),0) FROM refund_operations "
                "WHERE tenant_id=:t AND order_id=:o AND status='executed'"
            ), {"t": tenant_id, "o": order_id}).scalar()
            total = Decimal(str(current or 0)) + (operation.amount or Decimal("0.00"))
            if total > Decimal(str(order[0])):
                return False
            conn.execute(text(
                "INSERT INTO refund_operations (tenant_id, operation_id, ticket_id, order_id, "
                "op_type, amount, status, idempotency_key, created_by, version, "
                "decision_version, executed) VALUES "
                "(:t,:op,:tk,:o,:ot,:a,:s,:ik,:cb,:v,:dv,:ex)"
            ), self._operation_params(operation, status_override="executed", executed_override=True))
            return True

    # ---------- approval_decisions ----------
    def insert_approval(self, row: ApprovalRow) -> None:
        with self._tx() as conn:
            conn.execute(text(
                "INSERT INTO approval_decisions (tenant_id, operation_id, decision, reason, "
                "decided_by, decided_version) VALUES (:t,:op,:d,:r,:by,:dv)"
            ), {"t": row.tenant_id, "op": row.operation_id, "d": row.decision,
                "r": row.reason, "by": row.decided_by, "dv": row.decided_version})

    def approvals_of(self, tenant_id: str, operation_id: str) -> list[ApprovalRow]:
        with self._tx() as conn:
            rows = conn.execute(text(
                "SELECT tenant_id, operation_id, decision, reason, decided_by, decided_version "
                "FROM approval_decisions WHERE tenant_id=:t AND operation_id=:op ORDER BY id"
            ), {"t": tenant_id, "op": operation_id}).fetchall()
            return [ApprovalRow(r[0], r[1], r[2], r[3], r[4], r[5]) for r in rows]

    # ---------- audit_events ----------
    def insert_audit(self, row: AuditRow) -> None:
        with self._tx() as conn:
            conn.execute(text(
                "INSERT INTO audit_events (tenant_id, action, entity_type, entity_id, actor, "
                "before_state, after_state, idempotency_key, note) VALUES "
                "(:t,:a,:et,:eid,:actor,:b,:af,:ik,:note)"
            ), {"t": row.tenant_id, "a": row.action, "et": row.entity_type,
                "eid": row.entity_id, "actor": row.actor, "b": row.before_state,
                "af": row.after_state, "ik": row.idempotency_key, "note": row.note})

    def audit_of(self, tenant_id: str, entity_type: str, entity_id: str) -> list[AuditRow]:
        with self._tx() as conn:
            rows = conn.execute(text(
                "SELECT tenant_id, action, entity_type, entity_id, actor, before_state, "
                "after_state, idempotency_key, note FROM audit_events "
                "WHERE tenant_id=:t AND entity_type=:et AND entity_id=:eid ORDER BY id"
            ), {"t": tenant_id, "et": entity_type, "eid": entity_id}).fetchall()
            return [AuditRow(r[0], r[1], r[2], r[3], r[4], r[5], r[6], r[7], r[8]) for r in rows]

    # ---------- idempotency_records ----------
    def insert_idem(self, row: IdemRow) -> None:
        try:
            with self._tx() as conn:
                conn.execute(text(
                    "INSERT INTO idempotency_records (tenant_id, idem_key, payload_hash, "
                    "refund_id) VALUES (:t,:k,:h,:rid)"
                ), {"t": row.tenant_id, "k": row.idem_key, "h": row.payload_hash,
                    "rid": row.refund_id})
        except IntegrityError as e:
            raise UniqueViolation(f"幂等键 {row.idem_key} 已存在（租户 {row.tenant_id}）") from e

    def get_idem(self, tenant_id: str, idem_key: str) -> Optional[IdemRow]:
        with self._tx() as conn:
            row = conn.execute(text(
                "SELECT tenant_id, idem_key, payload_hash, refund_id FROM idempotency_records "
                "WHERE tenant_id=:t AND idem_key=:k"
            ), {"t": tenant_id, "k": idem_key}).fetchone()
            return IdemRow(row[0], row[1], row[2], row[3]) if row else None

    # ---------- 恢复装载（全表列举，只读） ----------
    def list_orders(self) -> list[OrderRow]:
        with self._tx() as conn:
            rows = conn.execute(text(
                "SELECT tenant_id, order_id, customer_id, status, paid_amount, "
                "days_since_sign, version FROM orders ORDER BY tenant_id, order_id"
            )).fetchall()
            return [self._order_from(r) for r in rows]

    def list_tickets(self) -> list[TicketRow]:
        with self._tx() as conn:
            rows = conn.execute(text(
                "SELECT tenant_id, ticket_id, order_id, customer_id, request_type, "
                "reason, status, resolution, version, created_by, reason_tags "
                "FROM tickets ORDER BY tenant_id, ticket_id"
            )).fetchall()
            return [self._ticket_from(r) for r in rows]

    def list_operations(self) -> list[OperationRow]:
        with self._tx() as conn:
            rows = conn.execute(text(
                "SELECT tenant_id, operation_id, ticket_id, order_id, op_type, amount, "
                "status, idempotency_key, created_by, version, decision_version, executed "
                "FROM refund_operations ORDER BY tenant_id, operation_id"
            )).fetchall()
            return [self._operation_from(r) for r in rows]

    def list_audit(self) -> list[AuditRow]:
        with self._tx() as conn:
            rows = conn.execute(text(
                "SELECT tenant_id, action, entity_type, entity_id, actor, before_state, "
                "after_state, idempotency_key, note FROM audit_events ORDER BY id"
            )).fetchall()
            return [AuditRow(r[0], r[1], r[2], r[3], r[4], r[5], r[6], r[7], r[8]) for r in rows]

    def list_idem(self) -> list[IdemRow]:
        with self._tx() as conn:
            rows = conn.execute(text(
                "SELECT tenant_id, idem_key, payload_hash, refund_id "
                "FROM idempotency_records ORDER BY tenant_id, idem_key"
            )).fetchall()
            return [IdemRow(r[0], r[1], r[2], r[3]) for r in rows]

    def clear_all(self) -> None:
        """按 FK 依赖序清空全部镜像业务行（子表先于父表：refund_operations/tickets/order_items
        引用 orders，须先删；policy/entity_seq 无 FK 依赖随后）。entity_seq 虽由命令 id 分配使用，
        镜像模式下由内存导出重建，清空无一致性影响。"""
        with self._tx() as conn:
            for table in ("audit_events", "idempotency_records", "approval_decisions",
                          "refund_operations", "tickets", "order_items", "orders",
                          "policies", "entity_seq"):
                conn.execute(text(f"DELETE FROM {table}"))

    # ---------- 0004 命令数据面：政策/明细装载与租户自增序列 ----------
    def insert_policy(self, row: PolicyRow) -> None:
        try:
            with self._tx() as conn:
                conn.execute(text(
                    "INSERT INTO policies (tenant_id, policy_id, request_type, reason_tags,"
                    " window_days, refund_ratio, effective_from, version) VALUES "
                    "(:t,:pid,:rt,:tags,:w,:ratio,:eff,:v)"
                ), {"t": row.tenant_id, "pid": row.policy_id, "rt": row.request_type,
                    "tags": row.reason_tags, "w": row.window_days,
                    "ratio": str(row.refund_ratio), "eff": row.effective_from,
                    "v": row.version})
        except IntegrityError as e:
            raise UniqueViolation(
                f"政策 {row.policy_id} v{row.version} 已存在（租户 {row.tenant_id}）") from e

    def list_policies(self) -> list[PolicyRow]:
        with self._tx() as conn:
            rows = conn.execute(text(
                "SELECT tenant_id, policy_id, request_type, reason_tags, window_days,"
                " refund_ratio, effective_from, version FROM policies ORDER BY tenant_id, policy_id, version"
            )).fetchall()
            return [PolicyRow(r[0], r[1], r[2], r[3], int(r[4]),
                              Decimal(str(r[5])), str(r[6]), int(r[7])) for r in rows]

    def insert_order_item(self, row: OrderItemRow) -> None:
        with self._tx() as conn:
            conn.execute(text(
                "INSERT INTO order_items (tenant_id, order_id, sku, name, quantity, unit_price)"
                " VALUES (:t,:o,:sku,:name,:q,:price)"
            ), {"t": row.tenant_id, "o": row.order_id, "sku": row.sku, "name": row.name,
                "q": row.quantity, "price": str(row.unit_price)})

    def list_order_items(self) -> list[OrderItemRow]:
        with self._tx() as conn:
            rows = conn.execute(text(
                "SELECT tenant_id, order_id, sku, name, quantity, unit_price"
                " FROM order_items ORDER BY tenant_id, order_id, sku"
            )).fetchall()
            return [OrderItemRow(r[0], r[1], r[2], r[3], int(r[4]), Decimal(str(r[5])))
                    for r in rows]

    def next_seq(self, tenant_id: str, kind: str) -> int:
        """租户作用域原子递增：INSERT … ON CONFLICT DO UPDATE（跨连接安全，单语句）。"""
        with self._tx() as conn:
            value = conn.execute(text(
                "INSERT INTO entity_seq (tenant_id, kind, next_val) VALUES (:t,:k,1)"
                " ON CONFLICT (tenant_id, kind) DO UPDATE"
                " SET next_val = entity_seq.next_val + 1 RETURNING next_val"
            ), {"t": tenant_id, "k": kind}).scalar()
            return int(value)

    # ---------- helpers ----------
    @staticmethod
    def _order_from(r) -> OrderRow:
        return OrderRow(r[0], r[1], r[2], r[3], Decimal(str(r[4])), int(r[5]), int(r[6]))

    @staticmethod
    def _order_params(row: OrderRow) -> dict:
        return {"t": row.tenant_id, "o": row.order_id, "c": row.customer_id,
                "s": row.status, "a": str(row.paid_amount), "d": row.days_since_sign,
                "v": row.version}

    @staticmethod
    def _ticket_from(r) -> TicketRow:
        return TicketRow(r[0], r[1], r[2], r[3], r[4], r[5], r[6], r[7], int(r[8]),
                         str(r[9] or ""), r[10])

    @staticmethod
    def _ticket_params(row: TicketRow) -> dict:
        return {"t": row.tenant_id, "tid": row.ticket_id, "o": row.order_id,
                "c": row.customer_id, "rt": row.request_type, "r": row.reason,
                "s": row.status, "res": row.resolution, "v": row.version,
                "cb": row.created_by, "rtags": row.reason_tags}

    @staticmethod
    def _operation_from(r) -> OperationRow:
        amount = Decimal(str(r[5])) if r[5] is not None else None
        return OperationRow(r[0], r[1], r[2], r[3], r[4], amount, r[6], r[7], r[8],
                            int(r[9]), r[10], bool(r[11]))

    @staticmethod
    def _operation_params(row: OperationRow, status_override: Optional[str] = None,
                          executed_override: Optional[bool] = None) -> dict:
        return {"t": row.tenant_id, "op": row.operation_id, "tk": row.ticket_id,
                "o": row.order_id, "ot": row.op_type,
                "a": str(row.amount) if row.amount is not None else None,
                "s": status_override or row.status, "ik": row.idempotency_key,
                "cb": row.created_by, "v": row.version, "dv": row.decision_version,
                "ex": row.executed if executed_override is None else executed_override}
