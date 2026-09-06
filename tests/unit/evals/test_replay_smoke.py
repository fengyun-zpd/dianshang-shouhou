"""黄金集回放器冒烟测试（评测工具自身可运行性）。"""
import importlib.util
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from evals import replay  # noqa: E402


HAPPY = {
    "id": "smoke-1", "scenario": "happy", "request": "订单 ORD-1 商品破损，要求退款",
    "tenant": "T1", "approval": "approved",
    "expected": {"outcome": "refunded", "refunded": "100.00", "intent": "refund"},
}
CLARIFY = {
    "id": "smoke-2", "scenario": "clarify", "request": "我要退款", "tenant": "T1",
    "expected": {"outcome": "clarify", "refunded": "0.00"},
}
REJECT = {
    "id": "smoke-3", "scenario": "reject", "request": "订单 ORD-1 商品破损，要求退款",
    "tenant": "T1", "approval": "rejected",
    "expected": {"outcome": "rejected", "refunded": "0.00", "intent": "refund"},
}


def test_run_case_happy_passes():
    result = replay.run_case(HAPPY)
    assert result["pass"] is True, result["detail"]
    assert result["outcome"] == "refunded"
    assert result["refunded"] == "100.00"


def test_run_case_clarify_passes_without_side_effects():
    result = replay.run_case(CLARIFY)
    assert result["pass"] is True, result["detail"]
    assert result["outcome"] == "clarify"
    assert result["refunded"] == "0.00"


def test_run_case_rejected_passes():
    result = replay.run_case(REJECT)
    assert result["pass"] is True, result["detail"]
    assert result["outcome"] == "rejected"


def test_rag_checks_three_way_classification():
    """三分类口径：正向 citation（分母仅期望命中类）、安全拒绝（旧版不计 accuracy）、
    注入拒绝（单独计数）。0.6667 旧口径（把正确拒绝旧政策算 accuracy miss）不再出现。"""
    rag = replay.rag_checks()
    # 1) 正向 citation：2 条期望命中现行版全部正确 → accuracy=1.0（拒绝行已剔除分母）
    assert rag["checked_citations"] == 2
    assert rag["citation_total"] == 2 and rag["citation_hits"] == 2
    assert rag["citation_accuracy"] == 1.0
    # 2) 安全拒绝：版本探针 1 条（词面旧版 v1）被正确拒绝，单独统计
    assert rag["safe_reject_total"] == 1 and rag["safe_reject_correct"] == 1
    assert rag["safe_reject_rate"] == 1.0
    assert any("safe_reject" in d for d in rag["detail"])
    # 3) 注入拒绝：单独口径（与旧 injection_blocked 命名统一）
    assert rag["injection_blocked"] is True
    assert rag["injection_rejected"] == 1 and rag["injection_total"] == 1
    assert rag["injection_rejection_rate"] == 1.0


def test_golden_file_loads_and_all_cases_have_expected():
    cases = replay.json.loads((ROOT / "evals" / "golden" / "golden_v1.json").read_text(encoding="utf-8"))
    assert len(cases) >= 10
    for c in cases:
        assert c["id"] and "expected" in c


def test_pg_profile_rejects_before_connectivity_probe(monkeypatch, tmp_path):
    """共享库 URL 必须在任何 PostgreSQL 探测前被隔离守卫拒绝。"""
    from src.platform.pg_test_guard import TestDbGuardError

    def _probe_must_not_run(*args, **kwargs):  # noqa: ANN002, ANN003
        raise AssertionError("隔离守卫之前不得探测 PostgreSQL")

    monkeypatch.setattr("src.api.runtime.require_postgres_ready", _probe_must_not_run)
    with pytest.raises(TestDbGuardError):
        replay.PgReplayProfile(
            "postgresql+psycopg2://opspilot:opspilot@127.0.0.1:5433/opspilot",
            checkpoint_path=str(tmp_path / "checkpoint.sqlite"),
        )
