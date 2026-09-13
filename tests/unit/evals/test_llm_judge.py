"""LLM-as-Judge 评测单元测试：离线规则裁判 + 安全降级 + 诚实边界。

覆盖：
1. rubric 加载与版本化；
2. rubric 非法（缺版本/维度为空/min>max）→ RubricError 安全失败；
3. PII 脱敏（judge 收到的文本已脱敏，报告不含完整号码）；
4. Judge 不触发领域写操作（不构造领域服务/适配器/仓储）；
5. 真实 Key 缺失时不联网（mode=judge 仍成功，measured=False，网络请求 0）；
6. 业务失败不被 Judge 高分掩盖（deterministic_check / business_assertion_failed 标记）；
7. 离线确定性（同输入同分）；
8. 报告含每条必需字段与三条声明。
"""
from __future__ import annotations

import json

import httpx
import pytest

from evals import llm_judge
from src.models.base import ModelConfigError


# ---------- 1) rubric 加载与版本化 ----------

def test_rubric_loads_and_is_versioned():
    rubric = llm_judge.load_rubric()
    assert rubric["rubric_version"] == "1.0"
    dims = rubric["dimensions"]
    assert set(dims.keys()) == {
        "accuracy", "completeness", "clarity", "tone",
        "citation_alignment", "boundary_disclosure",
    }
    for spec in dims.values():
        assert spec["min"] < spec["max"]
    assert "金额正确性" in rubric["out_of_scope"]
    assert any("权限" in s for s in rubric["out_of_scope"])
    assert any("审批" in s for s in rubric["out_of_scope"])
    assert any("状态迁移" in s for s in rubric["out_of_scope"])
    assert any("幂等" in s for s in rubric["out_of_scope"])
    assert any("副作用" in s for s in rubric["out_of_scope"])


# ---------- 2) rubric 非法 → 安全失败 ----------

def _write_rubric(tmp_path, data: dict):
    p = tmp_path / "rubric.json"
    p.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    return p


def test_rubric_missing_version_raises(tmp_path):
    p = _write_rubric(tmp_path, {"dimensions": {"accuracy": {"min": 1, "max": 5, "description": "x"}}})
    with pytest.raises(llm_judge.RubricError):
        llm_judge.load_rubric(path=p)


def test_rubric_empty_dimensions_raises(tmp_path):
    p = _write_rubric(tmp_path, {"rubric_version": "1.0", "dimensions": {}})
    with pytest.raises(llm_judge.RubricError):
        llm_judge.load_rubric(path=p)


def test_rubric_min_gt_max_raises(tmp_path):
    p = _write_rubric(tmp_path, {
        "rubric_version": "1.0",
        "dimensions": {"accuracy": {"min": 5, "max": 1, "description": "x"}},
    })
    with pytest.raises(llm_judge.RubricError):
        llm_judge.load_rubric(path=p)


# ---------- 3) PII 脱敏 ----------

class _SpyJudge:
    name = "spy"
    measured = False

    def __init__(self):
        self.seen_text = None

    def judge(self, case, rubric):
        self.seen_text = case.output_text
        return llm_judge.OfflineRuleJudge().judge(case, rubric)


def test_pii_redacted_before_judge():
    spy = _SpyJudge()
    case = llm_judge.JudgeCase(case_id="pii-1", output_text="手机 13812341234 订单破损申请退款")
    llm_judge.judge_case(case, llm_judge.load_rubric(), judge=spy)
    assert "13812341234" not in spy.seen_text
    assert "138****1234" in spy.seen_text


def test_pii_not_leaked_in_report(tmp_path):
    case = llm_judge.JudgeCase(case_id="pii-1", output_text="手机 13812341234 订单破损申请退款")
    llm_judge.run_judge([case], mode="offline", outdir=tmp_path)
    content = (tmp_path / "judge_offline.md").read_text(encoding="utf-8")
    assert "13812341234" not in content


# ---------- 4) Judge 不触发领域写操作 ----------

def test_judge_does_not_construct_domain_writers(tmp_path, monkeypatch):
    def _boom(*args, **kwargs):
        raise AssertionError("Judge 不得构造领域服务/适配器/仓储")

    monkeypatch.setattr("src.domain.after_sales.AfterSalesService.__init__", _boom)
    monkeypatch.setattr("src.domain.after_sales.adapters.MemoryAdapter.__init__", _boom)
    monkeypatch.setattr("src.repo.PostgresAfterSalesRepository.__init__", _boom)

    cases = [llm_judge.JudgeCase(case_id="c1", output_text="退款申请，金额由系统裁决")]
    r = llm_judge.run_judge(cases, mode="offline", outdir=tmp_path)
    assert r["total"] == 1
    assert r["results"][0]["case_id"] == "c1"


# ---------- 5) 真实 Key 缺失时不联网 ----------

def test_judge_mode_without_key_no_network(tmp_path, monkeypatch):
    monkeypatch.delenv("OPSPILOT_LLM_API_KEY", raising=False)
    monkeypatch.delenv("OPSPILOT_LLM_BASE_URL", raising=False)

    def _boom_client(*args, **kwargs):
        raise AssertionError("Judge 不得联网")

    def _boom_post(*args, **kwargs):
        raise AssertionError("Judge 不得联网")

    monkeypatch.setattr(httpx, "Client", _boom_client)
    monkeypatch.setattr("src.models.openai_compatible._default_http_post", _boom_post)

    cases = [llm_judge.JudgeCase(case_id="c1", output_text="退款申请")]
    r = llm_judge.run_judge(cases, mode="judge", outdir=tmp_path)
    assert r["measured"] is False
    assert r["network_requests"] == 0
    assert r["degraded_count"] == 0  # 未联网 → 纯离线规则，无裁判降级记录
    content = (tmp_path / "judge_candidate.md").read_text(encoding="utf-8")
    assert "真实 Judge 未实测" in content
    assert "未产生网络请求" in content


# ---------- 6) 业务失败不被 Judge 高分掩盖 ----------

def test_business_failure_not_masked_by_high_score(tmp_path):
    case = llm_judge.JudgeCase(
        case_id="biz-fail",
        output_text="请问订单号是多少？已依据政策证据（P-DAMAGED-FULL）说明处理方向；"
                    "金额、权限与审批由系统裁决，不迁移状态。",
        expected_summary="说明处理方向",
        evidence_refs=["P-DAMAGED-FULL"],
        business_ok=False,
        outcome="escalated",
        error_code="AFTER_SALES_ORDER_NOT_FOUND",
    )
    r = llm_judge.run_judge([case], mode="offline", outdir=tmp_path)

    dc = r["deterministic_check"]
    assert dc["checked"] == 1
    assert dc["business_assertion_failed_count"] == 1
    assert dc["business_assertion_failed"] == ["biz-fail"]
    assert r["business_assertion_failed"] == ["biz-fail"]
    assert r["results"][0]["business_ok"] is False  # 单条结果透传失败，不表述为成功

    content = (tmp_path / "judge_offline.md").read_text(encoding="utf-8")
    assert "不能替代" in content
    assert "biz-fail" in content
    assert "业务断言失败" in content


# ---------- 7) 离线确定性 ----------

def test_offline_deterministic_same_input_same_scores():
    rubric = llm_judge.load_rubric()
    case = llm_judge.JudgeCase(
        case_id="d1",
        output_text="请问订单号是多少？金额由系统裁决",
        expected_summary="提供订单号",
    )
    r1 = llm_judge.judge_case(case, rubric)
    r2 = llm_judge.judge_case(case, rubric)
    assert r1.scores == r2.scores
    assert r1.total_score == r2.total_score
    assert r1.short_reason == r2.short_reason


# ---------- 8) 报告必需字段与三条声明 ----------

def test_report_contains_required_fields_and_declarations(tmp_path):
    case = llm_judge.JudgeCase(case_id="c1", output_text="退款申请，转人工处理", expected_summary="转人工")
    llm_judge.run_judge([case], mode="offline", outdir=tmp_path)
    content = (tmp_path / "judge_offline.md").read_text(encoding="utf-8")
    for field in ("case_id", "rubric_version", "judge_model", "scores",
                  "short_reason", "degraded", "error"):
        assert field in content, f"报告缺少字段 {field}"
    assert "不能替代业务断言" in content
    assert "只能作为参考" in content
    assert "未实测" in content


# ---------- 额外：真实裁判构造门禁 + judge_case 异常安全降级 ----------

def test_llm_judge_requires_safe_env_and_judge_case_degrades(monkeypatch):
    monkeypatch.delenv("OPSPILOT_LLM_API_KEY", raising=False)
    monkeypatch.delenv("OPSPILOT_LLM_BASE_URL", raising=False)
    with pytest.raises(ModelConfigError):
        llm_judge.LLMJudge()  # 缺 Key → 构造失败（零网络）

    class _Raising:
        name = "raising-judge"
        measured = True

        def judge(self, case, rubric):
            raise RuntimeError("boom")

    result = llm_judge.judge_case(
        llm_judge.JudgeCase(case_id="x", output_text="退款申请"),
        llm_judge.load_rubric(),
        judge=_Raising(),
    )
    assert result.degraded is True
    assert result.error == "RuntimeError"
    assert result.judge_model == llm_judge.OfflineRuleJudge.name


# ---------- 9) 真实裁判路径（mock 注入 http_post，不联网） ----------

_SAFE_ENV = {
    "OPSPILOT_LLM_API_KEY": "sk-judge-test-key",
    "OPSPILOT_LLM_BASE_URL": "http://127.0.0.1:9100/v1",
    "OPSPILOT_LLM_MODEL": "fake-judge",
}


def _set_safe_env(monkeypatch) -> None:
    for k, v in _SAFE_ENV.items():
        monkeypatch.setenv(k, v)


def _judge_settings():
    from src.models.config import LLMSettings
    return LLMSettings(api_key="sk-judge-test-key", base_url="http://127.0.0.1:9100/v1",
                       model="fake-judge", model_version="test-v1",
                       input_price_per_1k=0.001, output_price_per_1k=0.002)


def _mock_post(content: str, calls: list):
    def post(url, headers, json_body, timeout_s):
        calls.append({"url": url, "headers": dict(headers), "body": json_body})
        return {"status_code": 200, "body": {
            "choices": [{"message": {"content": content}}],
            "usage": {"prompt_tokens": 120, "completion_tokens": 40},
        }}
    return post


def _valid_judge_json() -> str:
    return json.dumps({
        "accuracy": 4, "completeness": 3, "clarity": 5, "tone": 4,
        "citation_alignment": 2, "boundary_disclosure": 5,
        "short_reason": "解释完整、披露了系统边界",
    }, ensure_ascii=False)


def test_real_judge_path_scores_within_rubric_range():
    """真实裁判路径已实现：注入 mock http_post 即可整条链路单测。"""
    calls: list = []
    judge = llm_judge.LLMJudge(_judge_settings(), http_post=_mock_post(_valid_judge_json(), calls))
    case = llm_judge.JudgeCase(case_id="j1", output_text="已说明处理方向；金额由系统裁决。",
                               evidence_refs=["P-1"])
    rubric = llm_judge.load_rubric()
    result = judge.judge(case, rubric)

    assert result.measured is True and result.degraded is False
    assert result.scores["accuracy"] == 4 and result.scores["boundary_disclosure"] == 5
    assert result.max_score == sum(int(s["max"]) for s in rubric["dimensions"].values())
    assert judge.calls == 1
    # 请求确实带上了 rubric 维度与版本化 Prompt；输入走脱敏
    sent = calls[0]["body"]["messages"][1]["content"]
    assert "accuracy" in sent and "boundary_disclosure" in sent
    assert "Authorization" in calls[0]["headers"]     # Key 只在请求头，不落报告


def test_real_judge_input_is_redacted_before_send():
    calls: list = []
    judge = llm_judge.LLMJudge(_judge_settings(), http_post=_mock_post(_valid_judge_json(), calls))
    llm_judge.judge_case(
        llm_judge.JudgeCase(case_id="pii", output_text="客户手机 13812341234 申请退款"),
        llm_judge.load_rubric(), judge=judge)
    sent = calls[0]["body"]["messages"][1]["content"]
    assert "13812341234" not in sent
    assert "138****1234" in sent


def test_real_judge_out_of_range_score_degrades_safely():
    """模型给越界分数 → Schema 校验失败 → 降级离线，绝不当成实测结果。"""
    calls: list = []
    bad = json.dumps({"accuracy": 99, "completeness": 3, "clarity": 5, "tone": 4,
                      "citation_alignment": 2, "boundary_disclosure": 5,
                      "short_reason": "越界"}, ensure_ascii=False)
    judge = llm_judge.LLMJudge(_judge_settings(), http_post=_mock_post(bad, calls))
    result = llm_judge.judge_case(
        llm_judge.JudgeCase(case_id="j2", output_text="退款申请"),
        llm_judge.load_rubric(), judge=judge)
    assert result.degraded is True and result.measured is False
    assert result.error == "ModelSchemaError"
    assert result.judge_model == llm_judge.OfflineRuleJudge.name


def test_real_judge_content_guard_blocks_action_words():
    """裁判理由里出现业务动作词 → 内容守卫拦截 → 降级（不把被拦截输出当结论）。"""
    calls: list = []
    bad = json.dumps({"accuracy": 5, "completeness": 5, "clarity": 5, "tone": 5,
                      "citation_alignment": 5, "boundary_disclosure": 5,
                      "short_reason": "已批准退款"}, ensure_ascii=False)
    judge = llm_judge.LLMJudge(_judge_settings(), http_post=_mock_post(bad, calls))
    result = llm_judge.judge_case(
        llm_judge.JudgeCase(case_id="j3", output_text="退款申请"),
        llm_judge.load_rubric(), judge=judge)
    assert result.degraded is True and result.measured is False
    assert result.error == "ModelContentPolicyError"


# ---------- 10) run_judge 的"实测"判定必须依附真实调用 ----------

class _FakeRealJudge:
    """替身真实裁判：记录调用次数，按需成功或失败。"""

    provider = "openai-compatible-judge"
    measured = True
    fail = False

    def __init__(self, settings=None, http_post=None):
        self.name = "openai-compatible-judge/fake"
        self.model_version = "test-v1"
        self.calls = 0

    def judge(self, case, rubric):
        from src.models.base import ModelTimeoutError
        self.calls += 1
        if _FakeRealJudge.fail:
            raise ModelTimeoutError("mock timeout")
        dims = rubric["dimensions"]
        scores = {name: int(spec["max"]) for name, spec in dims.items()}
        return llm_judge.JudgeResult(
            case_id=case.case_id, rubric_version=rubric["rubric_version"],
            judge_model=self.name, scores=scores, short_reason="模型裁判：mock",
            degraded=False, error=None, total_score=sum(scores.values()),
            max_score=sum(int(s["max"]) for s in dims.values()), measured=True,
            business_ok=case.business_ok)


def test_judge_mode_with_safe_env_reports_measured(tmp_path, monkeypatch):
    _set_safe_env(monkeypatch)
    _FakeRealJudge.fail = False
    monkeypatch.setattr(llm_judge, "LLMJudge", _FakeRealJudge)
    r = llm_judge.run_judge([llm_judge.JudgeCase(case_id="c1", output_text="已说明边界")],
                            mode="judge", outdir=tmp_path)
    assert r["judge_enabled"] is True
    assert r["measured"] is True
    assert r["measured_cases"] == 1
    assert r["network_requests"] == 1
    content = (tmp_path / "judge_candidate.md").read_text(encoding="utf-8")
    assert "实测（存在未降级的真实模型裁判结果）" in content


def test_judge_mode_all_degraded_still_reported_unmeasured(tmp_path, monkeypatch):
    """启用真实裁判但全部降级 → 报告仍写"未实测"，不得夸大。"""
    _set_safe_env(monkeypatch)
    _FakeRealJudge.fail = True
    try:
        monkeypatch.setattr(llm_judge, "LLMJudge", _FakeRealJudge)
        r = llm_judge.run_judge([llm_judge.JudgeCase(case_id="c1", output_text="x")],
                                mode="judge", outdir=tmp_path)
        assert r["judge_enabled"] is True
        assert r["measured"] is False
        assert r["measured_cases"] == 0
        assert r["degraded_count"] == 1
        assert r["network_requests"] == 1        # 确实尝试过一次调用
        content = (tmp_path / "judge_candidate.md").read_text(encoding="utf-8")
        assert "未实测" in content
        assert "不得将本报告中的任何分数表述为真实模型" in content
    finally:
        _FakeRealJudge.fail = False


def test_judge_mode_missing_model_name_stays_offline(tmp_path, monkeypatch):
    """Key + 白名单就绪但未显式配置模型名 → 仍不启用真实裁判（零网络）。"""
    monkeypatch.setenv("OPSPILOT_LLM_API_KEY", "sk-judge-test-key")
    monkeypatch.setenv("OPSPILOT_LLM_BASE_URL", "http://127.0.0.1:9100/v1")
    monkeypatch.delenv("OPSPILOT_LLM_MODEL", raising=False)
    r = llm_judge.run_judge([llm_judge.JudgeCase(case_id="c1", output_text="x")],
                            mode="judge", outdir=tmp_path)
    assert r["judge_enabled"] is False
    assert r["network_requests"] == 0
    assert any("模型名" in b for b in r["blockers"])
