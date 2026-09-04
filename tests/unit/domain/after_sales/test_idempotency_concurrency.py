"""原子幂等（任务卡 J）：get-or-reserve/CAS；同键同载荷只产生一个业务对象、异载荷拒绝；并发单飞。

- IdempotencyStore 提供按 key 的原子临界（lock_for/get_or_reserve 语义）；
- 并发 N 个相同请求 → 最终只有一个操作（先红：当前 check-then-act 无同步）。
"""
import threading
from decimal import Decimal

import pytest

from src.domain.after_sales import (
    AfterSalesError,
    AfterSalesErrorCode,
    CreateRefundCommand,
    CreateTicketCommand,
    RequestType,
    Role,
)
from src.domain.idempotency import IdempotencyStore
from tests.unit.domain.after_sales.helpers import service_with_policies

POL = (("P-DAMAGED-FULL", ("damaged",), "1.00", 30),)


# ---------- store 级 get-or-reserve ----------

def test_store_get_or_reserve_semantics():
    store = IdempotencyStore()
    assert store.get_or_reserve("key-1", "hash-a") is None          # 抢到占位（可创建）
    rec = store.get_or_reserve("key-1", "hash-a")
    assert rec is not None and rec.refund_id == "<pending>"         # 已被占用（含同载荷）
    assert store.get_or_reserve("key-1", "hash-b") is not None      # 异载荷同样被拒
    # 创建完成后提交真实指向；失败释放占位
    store.commit("key-1", "hash-a", "OP-1")
    committed = store.get("key-1")
    assert committed is not None and committed.refund_id == "OP-1"
    store.release("key-1")                                          # 已提交 → 不释放
    assert store.get("key-1") is not None


def test_store_release_only_pending():
    store = IdempotencyStore()
    assert store.get_or_reserve("k-rel", "h") is None
    store.release("k-rel")
    assert store.get("k-rel") is None                               # 占位被释放


def test_store_lock_is_per_key():
    store = IdempotencyStore()
    a, b = store.lock_for("k-a"), store.lock_for("k-b")
    assert a is not b
    assert store.lock_for("k-a") is a


# ---------- 并发单飞（同键同载荷 N 个请求只创建一个业务对象） ----------

def _create_ticket(svc, key: str):
    return svc.create_ticket(CreateTicketCommand(
        tenant_id="T1", order_id="ORD-1", customer_id="C1",
        request_type=RequestType.REFUND, reason="商品破损", reason_tags=("damaged",),
        actor=Role.AGENT, idempotency_key=key,
    ))


def test_concurrent_same_key_same_payload_single_operation():
    svc = service_with_policies(*POL)
    ticket = _create_ticket(svc, "tk-conc")
    n = 8
    barrier = threading.Barrier(n)
    results: list[str] = []
    errors: list[Exception] = []
    lock = threading.Lock()

    def worker(_i: int):
        try:
            barrier.wait()
            op = svc.create_refund(CreateRefundCommand(
                ticket_id=ticket.ticket_id, amount=Decimal("100.00"),
                reason_detail="破损", actor=Role.AGENT, idempotency_key="conc-same",
            ))
            with lock:
                results.append(op.operation_id)
        except Exception as e:  # noqa: BLE001
            with lock:
                errors.append(e)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert errors == [], f"全部应成功（同键同载荷），错误：{errors}"
    assert len(set(results)) == 1                 # 只产生一个业务对象
    assert len(svc.operations_of(ticket.ticket_id)) == 1
    assert [e.action for e in svc.audit_log()].count("create_refund") == 1


def test_concurrent_same_key_different_payload_one_wins():
    svc = service_with_policies(*POL)
    ticket = _create_ticket(svc, "tk-conc2")
    n = 8
    barrier = threading.Barrier(n)
    ok, conflicts = [], []

    def worker(i: int):
        try:
            barrier.wait()
            svc.create_refund(CreateRefundCommand(
                ticket_id=ticket.ticket_id, amount=Decimal(f"{10 + i}.00"),
                reason_detail=f"r{i}", actor=Role.AGENT, idempotency_key="conc-diff",
            ))
            ok.append(i)
        except AfterSalesError as e:
            if e.code == AfterSalesErrorCode.IDEMPOTENCY_CONFLICT:
                conflicts.append(i)
            else:
                raise

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert len(ok) == 1                          # 恰一个成功
    assert len(conflicts) == n - 1               # 其余同键异载荷被拒
    assert len(svc.operations_of(ticket.ticket_id)) == 1
