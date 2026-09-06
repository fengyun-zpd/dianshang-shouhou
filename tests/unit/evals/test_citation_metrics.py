"""Citation 指标三分类可区分性（K2，V1 口径修正）。

三分类口径（互不稀释）：
1. kind=current（默认）：查询期望现行版，top-1 命中可校验现行版 → citation hit
   （分母 = current 类查询数）；政策/版本错配、不可校验、NO_MATCH → miss（仍计入
   accuracy，指标可区分、不会恒 1.0）；
2. kind=safe_reject：查询词面命中旧版/不适用版本（词面相似但版本/适用范围不符）
   → **不计入 citation accuracy 分母**，单独计 safe_reject_ok/rate（系统没有把旧版
   当现行版采信 = 正确拒绝）；
3. prompt injection：单独 injection 计数（见 rag_checks / test_replay_smoke）。

语义修正说明：旧口径把"版本探针：词面命中旧版 v1 而期望现行 v2"计入 accuracy miss
（得到 0.6667），把"正确拒绝旧政策"误报为准确率下降——本文件按新口径改写断言：
旧版拒绝行进入 safe_reject，不再稀释 citation accuracy。
"""
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


# ---------- 1) 正向 citation（分母 = 期望命中类） ----------

def test_metric_recognizes_correct_reference():
    cases = [
        {"query": "7 天内破损半额补偿", "policy_id": "P-DAMAGED-FULL", "version": 2,
         "kind": "current", "note": "正例"},
    ]
    r = replay.evaluate_citation_cases(_store(), cases)
    assert r["rows"][0]["hit"] is True
    assert r["citation_hits"] == 1 and r["citation_total"] == 1
    assert r["citation_accuracy"] == 1.0
    assert r["safe_reject_total"] == 0          # 拒绝行不与命中行互相稀释


def test_metric_recognizes_wrong_policy_reference_still_miss():
    """current 类政策错配 → miss 且仍计入 accuracy（指标可区分，不恒 1.0）。"""
    cases = [
        {"query": "少件补发如何申请", "policy_id": "P-DAMAGED-FULL", "version": 2,
         "kind": "current", "note": "错误引用"},
    ]
    r = replay.evaluate_citation_cases(_store(), cases)
    assert r["rows"][0]["hit"] is False
    assert "policy" in r["rows"][0]["reason"]
    assert r["citation_hits"] == 0 and r["citation_total"] == 1
    assert r["citation_accuracy"] == 0.0


def test_metric_current_class_version_mismatch_is_miss():
    """current 类内版本错配仍为 miss：现行词面查询命中现行 v2，期望却写旧版 v1。"""
    cases = [
        {"query": "7 天内破损半额补偿", "policy_id": "P-DAMAGED-FULL", "version": 1,
         "kind": "current", "note": "期望版本标注错误 → 应 miss"},
    ]
    r = replay.evaluate_citation_cases(_store(), cases)
    assert r["rows"][0]["hit"] is False
    assert "version" in r["rows"][0]["reason"]
    assert r["citation_accuracy"] == 0.0


def test_metric_recognizes_wrong_scope_no_match():
    """期望命中却 NO_MATCH（错误适用范围）→ current miss，原因可区分。"""
    cases = [
        # T1 检索不存在该政策（仅 T2 注册）→ NO_MATCH（错误适用范围）
        {"query": "会员积分兑换专用规则", "policy_id": "P-OTHER", "version": 1,
         "kind": "current", "note": "错误适用范围"},
    ]
    r = replay.evaluate_citation_cases(_store(), cases)
    assert r["rows"][0]["hit"] is False
    assert "NO_MATCH" in r["rows"][0]["reason"]
    assert r["citation_accuracy"] == 0.0


# ---------- 2) 安全拒绝（旧版/不适用版本；不计 accuracy 分母） ----------

def test_safe_reject_old_version_not_in_accuracy_denominator():
    """版本探针：词面命中旧版 v1（30 天全额）→ safe_reject，不进 citation accuracy。"""
    cases = [
        {"query": "30 天内破损全额退款", "policy_id": "P-DAMAGED-FULL", "version": 2,
         "kind": "safe_reject", "note": "版本探针"},
    ]
    r = replay.evaluate_citation_cases(_store(), cases)
    assert r["rows"][0]["safe_reject_ok"] is True
    assert r["safe_reject_correct"] == 1 and r["safe_reject_total"] == 1
    assert r["safe_reject_rate"] == 1.0
    assert r["citation_total"] == 0              # 拒绝行不在 accuracy 分母
    assert r["citation_accuracy"] == 1.0         # 空分母语义：无期望命中行 → 不误报下降


def test_safe_reject_mixed_does_not_dilute_citation_accuracy():
    """混合 1 hit + 1 旧版拒绝：citation_accuracy 只反映期望命中类（=1.0），
    旧口径会把拒绝计入分母得到 0.5 —— 已修正。"""
    cases = [
        {"query": "7 天内破损半额补偿", "policy_id": "P-DAMAGED-FULL", "version": 2,
         "kind": "current", "note": "正例"},
        {"query": "30 天内破损全额退款", "policy_id": "P-DAMAGED-FULL", "version": 2,
         "kind": "safe_reject", "note": "版本探针"},
    ]
    r = replay.evaluate_citation_cases(_store(), cases)
    assert r["citation_hits"] == 1 and r["citation_total"] == 1
    assert r["citation_accuracy"] == 1.0
    assert r["safe_reject_correct"] == 1 and r["safe_reject_total"] == 1
    assert r["safe_reject_rate"] == 1.0


def test_safe_reject_hitting_current_version_is_wrong():
    """safe_reject 行若命中现行版（pid+version 双等于）→ 判定失败（拒绝路径不可信）。"""
    cases = [
        {"query": "7 天内破损半额补偿", "policy_id": "P-DAMAGED-FULL", "version": 2,
         "kind": "safe_reject", "note": "期望拒绝却命中现行（异常场景）"},
    ]
    r = replay.evaluate_citation_cases(_store(), cases)
    assert r["rows"][0]["safe_reject_ok"] is False
    assert r["safe_reject_rate"] == 0.0


# ---------- 3) prompt injection（单独计数） ----------

def test_injection_counted_separately():
    """injection 单独统计，不稀释 citation/safe_reject 口径。"""
    rag = replay.rag_checks()
    assert rag["injection_blocked"] is True
    assert rag["injection_rejected"] == 1 and rag["injection_total"] == 1
    assert rag["injection_rejection_rate"] == 1.0
    assert "注入查询拦截" in rag["detail"][-1]


# ---------- 引用校验原语（不回归） ----------

def test_validate_citation_rejects_bad_tenant_version_and_missing():
    store = _store()
    # 错误租户（适用范围）
    assert store.validate_citation("T2", "P-DAMAGED-FULL@1#0") is None
    # 错误版本（不存在 v99）
    assert store.validate_citation("T1", "P-DAMAGED-FULL@99#0") is None
    # 不存在的 policy
    assert store.validate_citation("T1", "P-NOPE@1#0") is None


def test_overall_metrics_are_not_constant_one():
    """整体口径仍可区分错误：current miss + safe_reject 各自如实、不互相稀释。"""
    cases = [
        {"query": "7 天内破损半额补偿", "policy_id": "P-DAMAGED-FULL", "version": 2,
         "kind": "current", "note": "ok"},
        {"query": "少件补发如何申请", "policy_id": "P-DAMAGED-FULL", "version": 2,
         "kind": "current", "note": "policy miss"},
        {"query": "30 天内破损全额退款", "policy_id": "P-DAMAGED-FULL", "version": 2,
         "kind": "safe_reject", "note": "旧版拒绝"},
    ]
    r = replay.evaluate_citation_cases(_store(), cases)
    assert r["citation_hits"] == 1 and r["citation_total"] == 2
    assert r["citation_accuracy"] == 0.5         # current 类仍可区分 miss
    assert r["safe_reject_correct"] == 1 and r["safe_reject_total"] == 1
    assert r["safe_reject_rate"] == 1.0          # 拒绝行单独满率，不拉低引用准确率
