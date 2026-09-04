"""能力矩阵与 ModelGateway 降级链测试（含影子模式零副作用）。"""
import httpx

from src.domain.after_sales import AfterSalesService
from src.models import (
    LLMSettings,
    ModelEntry,
    ModelGateway,
    ModelRegistry,
    ModelTask,
    OfflineRuleClient,
)
from src.models.openai_compatible import OpenAICompatibleClient
from src.models.schemas import IntentExtraction
from tests.unit.domain.after_sales.helpers import baseline_service


def _make_settings() -> LLMSettings:
    return LLMSettings(api_key="sk-test", base_url="http://127.0.0.1:9000/v1",
                       model="fake-model", model_version="v1", max_retries=0)


def _ok_post(content: str):
    def post(url, headers, body, timeout):
        return {"status_code": 200,
                "body": {"choices": [{"message": {"content": content}}], "usage": {}}}
    return post


def test_registry_high_risk_never_allowed():
    reg = ModelRegistry()
    off = OfflineRuleClient()
    reg.register(ModelEntry(client=off, capabilities={
        ModelTask.tool_calling, ModelTask.high_risk_draft,
    }))
    assert reg.allowed_models(ModelTask.intent_classification) == []  # 不支持
    assert reg.allowed_models(ModelTask.tool_calling) == []           # 高风险禁
    assert reg.allowed_models(ModelTask.high_risk_draft) == []        # 高风险禁


def test_gateway_primary_success_not_degraded():
    gw = ModelGateway(settings=_make_settings())
    gw._primary = OpenAICompatibleClient(_make_settings(), http_post=_ok_post(
        '{"intent": "refund", "order_id": "ORD-1", "reason_tags": ["damaged"],'
        ' "missing_fields": [], "confidence": 0.9, "note": ""}'))
    resp = gw.analyze_intent("订单 ORD-1 破损")
    assert resp.metadata.degraded is False
    assert resp.metadata.provider == "openai-compatible"


def test_gateway_degrades_to_offline_on_primary_failure():
    def fail_post(url, headers, body, timeout):
        return {"status_code": 429, "body": {}}
    gw = ModelGateway(settings=_make_settings())
    gw._primary = OpenAICompatibleClient(_make_settings(), http_post=fail_post)
    resp = gw.analyze_intent("订单 ORD-1 商品破损，要求退款")
    assert resp.payload.intent.value == "refund"          # 离线结果兜底
    assert resp.metadata.degraded is True
    assert resp.metadata.error == "ModelRateLimitedError"


def test_gateway_invalid_config_stays_offline_not_crashing():
    gw = ModelGateway()
    gw._attach_settings(LLMSettings(api_key=None))  # Key 缺失
    assert gw.has_primary_model is False
    resp = gw.analyze_intent("订单 ORD-1 商品破损")
    assert resp.metadata.degraded is False             # 纯离线，未降级标记（未尝试联网）


def test_shadow_prediction_has_zero_business_side_effects():
    """影子评测：模型预测（离线主路径 + 降级路径）不得触碰任何领域状态。"""
    svc: AfterSalesService = baseline_service()
    audit_before = len(svc.audit_log())
    ops_before = len([o for o in svc.operations_of("TKT-ANY")])
    tickets_before = len(svc.list_customer_tickets("T1", "C1"))

    # 离线模式
    gw_off = ModelGateway()
    gw_off.analyze_intent("订单 ORD-1 商品破损，要求退款")
    gw_off.decide_clarification("我要退款", ["order_id"])
    gw_off.explain_with_evidence("x", ["P@1#0"])

    # 候选降级模式（主模型持续失败 → 离线兜底）
    gw_cand = ModelGateway(settings=_make_settings())
    gw_cand._primary = OpenAICompatibleClient(_make_settings(), http_post=lambda *a: {"status_code": 500, "body": {}})
    gw_cand.analyze_intent("订单 ORD-1 商品破损，要求退款")

    assert len(svc.audit_log()) == audit_before
    assert len(svc.operations_of("TKT-ANY")) == ops_before
    assert len(svc.list_customer_tickets("T1", "C1")) == tickets_before
    assert svc.refunded_amount("ORD-1") == 0 or str(svc.refunded_amount("ORD-1")) == "0.00"
