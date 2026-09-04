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


def test_rag_checks_block_injection_and_cite_distinguishable():
    """引用指标必须能区分错误（版本探针 miss），不能恒为 1.0。"""
    rag = replay.rag_checks()
    assert rag["injection_blocked"] is True
    assert rag["checked_citations"] == 3
    assert 0.0 < rag["citation_accuracy"] < 1.0     # 含版本探针 miss → 指标可区分
    assert rag["correct"] >= 1                       # 至少一个正确命中
    assert any("version" in d for d in rag["detail"])  # 错误版本被识别并列出


def test_golden_file_loads_and_all_cases_have_expected():
    cases = replay.json.loads((ROOT / "evals" / "golden" / "golden_v1.json").read_text(encoding="utf-8"))
    assert len(cases) >= 10
    for c in cases:
        assert c["id"] and "expected" in c
