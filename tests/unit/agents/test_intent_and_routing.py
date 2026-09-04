"""意图识别、缺参判断与节点路由（纯函数）测试。"""
from src.agents.intent import clarify_questions, extract_intent
from src.agents.nodes import (
    route_after_decision,
    route_after_gather,
    route_after_parse,
    route_after_plan,
)


# ---------- 意图识别与缺参 ----------

def test_extract_refund_intent_with_order_and_tag():
    res = extract_intent("订单 ORD-123 商品破损，要求退款")
    assert res["intent"] == "refund"
    assert res["order_id"] == "ORD-123"
    assert res["reason_tags"] == ["damaged"]
    assert res["missing_fields"] == []


def test_extract_order_id_case_insensitive():
    res = extract_intent("我要退 ord-88 的货，少件了")
    assert res["order_id"] == "ORD-88"
    assert "missing_item" in res["reason_tags"]


def test_missing_order_id_and_reason():
    res = extract_intent("我要退款")  # 无订单号、无问题描述
    assert res["intent"] == "refund"
    assert res["order_id"] is None
    assert res["missing_fields"] == ["order_id", "reason"]


def test_missing_reason_only_when_order_present():
    res = extract_intent("订单 ORD-1 我要退款")
    assert res["order_id"] == "ORD-1"
    assert res["missing_fields"] == ["reason"]


def test_unknown_intent():
    res = extract_intent("你好呀")
    assert res["intent"] == "unknown"


def test_exchange_intent_recognized():
    assert extract_intent("我想换货")["intent"] == "exchange"


def test_clarify_questions_mapping():
    qs = clarify_questions(["order_id", "reason"])
    assert any("订单号" in q for q in qs)
    assert any("描述" in q for q in qs)


# ---------- 节点路由 ----------

def test_route_after_parse():
    assert route_after_parse({"missing_fields": ["order_id"]}) == "clarify"
    assert route_after_parse({"intent": "unknown"}) == "escalate"
    assert route_after_parse({"intent": "exchange"}) == "escalate"   # V1 不支持自动草稿
    assert route_after_parse({"intent": "refund", "missing_fields": []}) == "gather_evidence"


def test_route_after_gather_and_plan():
    assert route_after_gather({"error_code": "AFTER_SALES_ORDER_NOT_FOUND"}) == "escalate"
    assert route_after_gather({}) == "plan"
    assert route_after_plan({"error_code": "AFTER_SALES_POLICY_CONFLICT"}) == "escalate"
    assert route_after_plan({}) == "create_ticket_draft"


def test_route_after_decision():
    assert route_after_decision({"next_action": "execute_operation"}) == "execute_operation"
    assert route_after_decision({"next_action": "settle_rejected"}) == "settle_rejected"
    assert route_after_decision({"next_action": "finished"}) == "finished"
    assert route_after_decision({"next_action": "reconcile_required"}) == "reconcile_required"
    assert route_after_decision({}) == "escalate"
