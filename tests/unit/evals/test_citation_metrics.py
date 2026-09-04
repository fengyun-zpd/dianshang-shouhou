"""Citation 指标可区分性（K2）：能识别错误引用 / 错误版本 / 错误适用范围。"""
from src.rag import PolicyDocument, PolicyStore

from evals import replay


def _store() -> PolicyStore:
    store = PolicyStore()
    store.register(PolicyDocument(
        policy_id="P-DAMAGED-FULL", tenant_id="T1", title="破损（旧版）",
        content="旧版政策：签收后 30 天内商品破损可申请全额退款。", version=1,
    ))
    store.register(PolicyDocument(
        policy_id="P-DAMAGED-FULL", tenant_id="T1", title="破损（现行）",
        content="现行政策：签收后 7 天内商品破损可按 50% 金额补偿退款。", version=2,
    ))
    store.register(PolicyDocument(
        policy_id="P-MISSING-FULL", tenant_id="T1", title="少件",
        content="签收后 30 天内订单少件（漏发）可申请补发。", version=1,
    ))
    store.register(PolicyDocument(
        policy_id="P-OTHER", tenant_id="T2", title="他租户政策",
        content="他租户：破损全额退款。", version=1,
    ))
    return store


def test_metric_recognizes_correct_reference():
    cases = [
        {"query": "7 天内破损半额补偿", "policy_id": "P-DAMAGED-FULL", "version": 2, "note": "正例"},
    ]
    r = replay.evaluate_citation_cases(_store(), cases)
    assert r["rows"][0]["hit"] is True
    assert r["accuracy"] == 1.0


def test_metric_recognizes_wrong_version():
    cases = [
        {"query": "30 天内破损全额退款", "policy_id": "P-DAMAGED-FULL", "version": 2, "note": "版本探针"},
    ]
    r = replay.evaluate_citation_cases(_store(), cases)
    assert r["rows"][0]["hit"] is False
    assert "version" in r["rows"][0]["reason"]
    assert r["accuracy"] == 0.0


def test_metric_recognizes_wrong_policy_reference():
    cases = [
        {"query": "少件补发如何申请", "policy_id": "P-DAMAGED-FULL", "version": 2, "note": "错误引用"},
    ]
    r = replay.evaluate_citation_cases(_store(), cases)
    assert r["rows"][0]["hit"] is False
    assert "policy" in r["rows"][0]["reason"]


def test_metric_recognizes_wrong_scope_no_match():
    cases = [
        # T1 检索不存在该政策（仅 T2 注册）→ NO_MATCH（错误适用范围）
        {"query": "会员积分兑换专用规则", "policy_id": "P-OTHER", "version": 1, "note": "错误适用范围"},
    ]
    r = replay.evaluate_citation_cases(_store(), cases)
    assert r["rows"][0]["hit"] is False
    assert "NO_MATCH" in r["rows"][0]["reason"]


def test_validate_citation_rejects_bad_tenant_version_and_missing():
    store = _store()
    # 错误租户（适用范围）
    assert store.validate_citation("T2", "P-DAMAGED-FULL@1#0") is None
    # 错误版本（不存在 v99）
    assert store.validate_citation("T1", "P-DAMAGED-FULL@99#0") is None
    # 不存在的 policy
    assert store.validate_citation("T1", "P-NOPE@1#0") is None


def test_overall_accuracy_is_not_constant_one():
    cases = [
        {"query": "7 天内破损半额补偿", "policy_id": "P-DAMAGED-FULL", "version": 2, "note": "ok"},
        {"query": "30 天内破损全额退款", "policy_id": "P-DAMAGED-FULL", "version": 2, "note": "version miss"},
        {"query": "少件补发如何申请", "policy_id": "P-DAMAGED-FULL", "version": 2, "note": "policy miss"},
    ]
    r = replay.evaluate_citation_cases(_store(), cases)
    assert r["correct"] == 1 and r["total"] == 3
    assert 0.0 < r["accuracy"] < 1.0
