"""阶段 C1 冒烟：replay PG profile（evals/replay.py）装配链 + golden_v1 在 PG 真实跑通。

前置：隔离测试库 OPSPILOT_TEST_DATABASE_URL（opspilot_test_*，schema=0005）；
不可达/版本不符整模块 skip（绝不回退 DATABASE_URL 指向的共享 opspilot 主库）。
覆盖（真实行为，非字符串级）：
- PgReplayProfile 装配：require_postgres_ready 门禁、每用例经 guard 重建 schema
  （reset_schema 仅允许 opspilot_test_* 隔离库，fail-closed）、
  订单/政策 seed 等价层（orders/order_items/policies 行）、PgCommandAdapter 后端、
  SQLite 持久 checkpoint、WorkflowRunner 强制 workflow_threads DB 租约；
- run_case(case, profile=...) 真实驱动 g01（正常退款闭环）通过，且
  PG 事实源落库（refund_operations.status='executed'、approval_decisions 1 行）。

说明：不做完整 11 条断言（evals/replay.py --profile pg 本身即验收命令）；
冒烟只固化装配与至少一条真实路径，防止装配链/seed 等价层被意外破坏。
"""
import sys
from pathlib import Path

import pytest
from sqlalchemy import create_engine, text

from tests.pg_live import live_test_db_url, pg_reachable

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.api.runtime import require_postgres_ready  # noqa: E402

TEST_DB_URL = live_test_db_url()


def _ready() -> bool:
    """隔离测试库就绪：可达且 alembic schema=0005（保留原 require_postgres_ready 门禁语义）。"""
    if TEST_DB_URL is None or not pg_reachable(TEST_DB_URL):
        return False
    try:
        require_postgres_ready(TEST_DB_URL)
        return True
    except Exception:  # noqa: BLE001
        return False


pytestmark = pytest.mark.skipif(
    not _ready(),
    reason="OPSPILOT_TEST_DATABASE_URL 未设置/不可达/schema≠0005："
           "PG profile 回放未实测（未使用隔离测试库）")


def _g01() -> dict:
    import json
    cases = json.loads(
        (ROOT / "evals" / "golden" / "golden_v1.json").read_text(encoding="utf-8"))
    return next(c for c in cases if c["id"] == "g01-refund-happy")


@pytest.fixture
def profile(tmp_path):
    """pg profile 评测环境（构造即门禁 + guard 重建 schema；每测试独立重建）。"""
    from evals.replay import PgReplayProfile
    p = PgReplayProfile(TEST_DB_URL, checkpoint_path=str(tmp_path / "ck.sqlite"))
    yield p
    p.close()


def test_replay_pg_profile_assembles_runner_with_lease_and_seed(profile):
    """装配真实：runner 强制 DB 租约、checkpoint 持久文件、seed 等价层行落库。"""
    from evals.replay import PgReplayProfile
    from src.domain.after_sales.adapters import PgCommandAdapter

    assert isinstance(profile, PgReplayProfile)
    assert isinstance(profile.backend, PgCommandAdapter)
    assert Path(profile.cp_path).exists()
    runner = profile.new_runner()
    assert runner._lease_repo is profile.repo       # D9 workflow_threads 租约强制
    assert runner._owner_id == "replay-pg"

    # seed 等价层：orders + order_items 明细 + policies（effective_from/version 固定）
    profile.seed_case(_g01())
    engine = create_engine(TEST_DB_URL)
    with engine.connect() as conn:
        order = conn.execute(text(
            "SELECT tenant_id, order_id, customer_id, status, paid_amount, "
            "days_since_sign FROM orders WHERE order_id='ORD-1'")).fetchone()
        item = conn.execute(text(
            "SELECT sku, quantity, unit_price FROM order_items "
            "WHERE tenant_id='T1' AND order_id='ORD-1'")).fetchone()
        pol = conn.execute(text(
            "SELECT policy_id, request_type, reason_tags, window_days, refund_ratio, "
            "effective_from, version FROM policies WHERE tenant_id='T1'")).fetchall()
    engine.dispose()
    assert order is not None
    assert (order[0], order[3]) == ("T1", "delivered")
    assert order[4] == 100  # paid_amount 100.00（SQLAlchemy numeric → Decimal/int 比较宽松）
    assert item is not None and item[0] == "SKU-1"
    assert {p[0] for p in pol} == {"P-DAMAGED-FULL"}
    assert all(p[5].isoformat() == "2020-01-01" and p[6] == 1 for p in pol)


def test_replay_pg_golden_g01_passes_with_pg_facts(profile):
    """g01 正常退款闭环在 PG 事实源真实跑通：11 条中的代表路径 + SQL 事实断言。"""
    from evals.replay import run_case
    case = _g01()
    result = run_case(case, profile=profile)
    assert result["pass"], result.get("detail")
    assert result["outcome"] == "refunded"
    assert result["refunded"] == "100.00"

    engine = create_engine(TEST_DB_URL)
    with engine.connect() as conn:
        op = conn.execute(text(
            "SELECT status, executed, amount FROM refund_operations "
            "WHERE tenant_id='T1' AND order_id='ORD-1'")).fetchone()
        n_approval = conn.execute(text(
            "SELECT COUNT(*) FROM approval_decisions WHERE tenant_id='T1'")).scalar()
        ticket_status = conn.execute(text(
            "SELECT status FROM tickets WHERE tenant_id='T1'")).scalar()
    engine.dispose()
    assert op is not None and op[0] == "executed" and op[1] is True
    assert n_approval == 1
    assert ticket_status == "closed"
