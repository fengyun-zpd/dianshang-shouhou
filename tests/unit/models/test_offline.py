"""离线规则适配器与网关离线路径测试（与现有规则基线一致）。"""
from src.agents.intent import clarify_questions, extract_intent
from src.models import (
    EvidenceBoundExplanation,
    IntentExtraction,
    ModelContentPolicyError,
    ModelGateway,
    ModelTask,
    OfflineRuleClient,
)
from src.models.schemas import ClarificationDecision


def test_offline_intent_matches_rule_baseline():
    client = OfflineRuleClient()
    text = "订单 ORD-1 商品破损，要求退款"
    resp = client.invoke("intent_classification", "prompt-ignored",
                         IntentExtraction, {"text": text})
    expected = extract_intent(text)
    payload = resp.payload
    assert payload.intent.value == expected["intent"]
    assert payload.order_id == expected["order_id"]
    assert payload.reason_tags == expected["reason_tags"]
    assert payload.missing_fields == expected["missing_fields"]
    assert resp.metadata.provider == "offline-rule"
    assert resp.metadata.degraded is False


def test_offline_clarification_matches_rule():
    client = OfflineRuleClient()
    resp = client.invoke("clarification_copy", "p", ClarificationDecision,
                         {"text": "我要退款", "missing_fields": ["order_id", "reason"]})
    assert resp.payload.should_clarify is True
    assert resp.payload.questions == clarify_questions(["order_id", "reason"])


def test_gateway_offline_default_path():
    gw = ModelGateway()  # 未配置 settings → 无主模型，纯离线
    assert gw.has_primary_model is False
    resp = gw.analyze_intent("订单 ORD-1 商品破损，要求退款")
    assert resp.payload.intent.value == "refund"
    assert resp.metadata.degraded is False


def test_gateway_clarification_and_explanation():
    gw = ModelGateway()
    r = gw.decide_clarification("我要退款", ["order_id", "reason"])
    assert r.payload.should_clarify is True
    e = gw.explain_with_evidence("破损退款依据", ["P-DAMAGED@1#0"])
    assert isinstance(e.payload, EvidenceBoundExplanation)
    assert e.payload.supported is True
    assert e.payload.evidence_refs == ["P-DAMAGED@1#0"]


def test_gateway_high_risk_task_forbidden():
    gw = ModelGateway()
    try:
        gw.run_task(ModelTask.tool_calling, "x", {"text": "x"})
        raised = False
    except ModelContentPolicyError:
        raised = True
    assert raised, "tool_calling（高风险）必须默认禁用"

    try:
        gw.run_task(ModelTask.high_risk_draft, "x", {"text": "x"})
        raised = False
    except ModelContentPolicyError:
        raised = True
    assert raised, "high_risk_draft（高风险）必须默认禁用"
