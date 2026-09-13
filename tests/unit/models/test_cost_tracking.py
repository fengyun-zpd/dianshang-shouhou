"""LLM 成本追踪测试（阶段 5B）：价格配置、双向计算、未配置标记、安全回退与报告列。

核心不变量：
- 未配置价格 → 成本为 None（报告显示 N/A），**绝不写 0 冒充真实成本**；
- 旧变量 `OPSPILOT_LLM_COST_PER_1K_IN` 仍兼容（仅提供输入单价）；
- 非法价格字符串 → 安全回退为未配置，不猜测价格；
- 报告与日志不出现 API Key。
"""
from pathlib import Path

import pytest

from src.models import (
    IntentExtraction,
    LLMSettings,
    ModelInvocationMetadata,
    OpenAICompatibleClient,
)
from src.models.config import (
    PRICE_ENV_INPUT,
    PRICE_ENV_LEGACY_INPUT,
    PRICE_ENV_OUTPUT,
    PRICING_NOT_CONFIGURED,
    load_llm_settings,
)
from evals import run_model_shadow_eval as shadow

SECRET = "sk-super-secret-key-do-not-log"


def _env(**overrides) -> dict:
    base = {
        "OPSPILOT_LLM_API_KEY": SECRET,
        "OPSPILOT_LLM_BASE_URL": "https://api.deepseek.com/v1",
        "OPSPILOT_LLM_MODEL": "deepseek-chat",
    }
    base.update(overrides)
    return base


# ---------- 1. 环境变量加载 ----------

def test_prices_loaded_from_new_env_vars():
    s = load_llm_settings(_env(**{PRICE_ENV_INPUT: "0.001", PRICE_ENV_OUTPUT: "0.002"}))
    assert s.input_price_per_1k == pytest.approx(0.001)
    assert s.output_price_per_1k == pytest.approx(0.002)
    assert s.pricing_configured is True
    assert PRICE_ENV_INPUT in s.pricing_source and PRICE_ENV_OUTPUT in s.pricing_source
    assert s.model_configured is True


def test_currency_env_is_honored():
    s = load_llm_settings(_env(**{PRICE_ENV_INPUT: "1", PRICE_ENV_OUTPUT: "1",
                                  "OPSPILOT_LLM_PRICE_CURRENCY": "cny"}))
    assert s.currency == "CNY"


# ---------- 2. 输入输出分别计算 ----------

def test_cost_computed_for_input_and_output_separately():
    s = load_llm_settings(_env(**{PRICE_ENV_INPUT: "0.001", PRICE_ENV_OUTPUT: "0.004"}))
    cost = s.cost_breakdown(input_tokens=2000, output_tokens=500)
    assert cost.input_cost == pytest.approx(0.002)     # 2000/1000*0.001
    assert cost.output_cost == pytest.approx(0.002)    # 500/1000*0.004
    assert cost.total_cost == pytest.approx(0.004)
    assert cost.configured is True


def test_client_metadata_records_dual_cost():
    """实测路径：适配器按 usage 的输入/输出 token 双向记账。"""
    settings = LLMSettings(
        api_key="sk", base_url="http://127.0.0.1:9000/v1", model="fake",
        input_price_per_1k=0.001, output_price_per_1k=0.002,
        pricing_source="test", currency="USD",
    )

    def post(url, headers, json_body, timeout_s):
        return {"status_code": 200, "body": {
            "choices": [{"message": {"content": '{"intent": "refund", "missing_fields": [], "note": ""}'}}],
            "usage": {"prompt_tokens": 1000, "completion_tokens": 500},
        }}

    resp = OpenAICompatibleClient(settings, http_post=post).invoke(
        "intent_classification", "prompt", IntentExtraction, {"text": "破损"})
    meta = resp.metadata
    assert meta.input_tokens == 1000 and meta.output_tokens == 500
    assert meta.total_tokens == 1500
    assert meta.input_cost == pytest.approx(0.001)
    assert meta.output_cost == pytest.approx(0.001)
    assert meta.cost_estimate_usd == pytest.approx(0.002)
    assert meta.token_source == "measured"


# ---------- 3. 缺少价格 → 未配置 ----------

def test_missing_prices_marked_not_configured():
    s = load_llm_settings(_env())
    assert s.input_price_per_1k is None and s.output_price_per_1k is None
    assert s.pricing_source == PRICING_NOT_CONFIGURED
    cost = s.cost_breakdown(input_tokens=1000, output_tokens=1000)
    assert cost.input_cost is None and cost.output_cost is None and cost.total_cost is None
    assert cost.configured is False
    assert cost.display_total() == "N/A（未配置价格）"


def test_metadata_cost_display_is_na_when_unconfigured():
    meta = ModelInvocationMetadata(provider="openai-compatible", model_name="m",
                                   task="intent_classification", prompt_version="1.0")
    assert meta.cost_estimate_usd is None
    assert meta.cost_display() == "N/A"


# ---------- 4. 旧变量兼容 ----------

def test_legacy_input_price_env_still_supported():
    s = load_llm_settings(_env(**{PRICE_ENV_LEGACY_INPUT: "0.003"}))
    assert s.input_price_per_1k == pytest.approx(0.003)
    assert s.cost_per_1k_input_usd == pytest.approx(0.003)   # 旧字段仍同步
    assert s.output_price_per_1k is None
    assert PRICE_ENV_LEGACY_INPUT in s.pricing_source
    cost = s.cost_breakdown(input_tokens=1000, output_tokens=1000)
    assert cost.input_cost == pytest.approx(0.003)
    assert cost.output_cost is None
    assert cost.total_cost is None, "输出单价未配置时不得给出伪造的总成本"


def test_new_env_var_takes_precedence_over_legacy():
    s = load_llm_settings(_env(**{PRICE_ENV_INPUT: "0.001", PRICE_ENV_LEGACY_INPUT: "9.999"}))
    assert s.input_price_per_1k == pytest.approx(0.001)
    assert PRICE_ENV_LEGACY_INPUT not in s.pricing_source


# ---------- 5. 非法价格安全回退 ----------

@pytest.mark.parametrize("bad", ["not-a-number", "", "  ", "-1", "nan", "inf"])
def test_illegal_price_falls_back_safely(bad):
    s = load_llm_settings(_env(**{PRICE_ENV_INPUT: bad, PRICE_ENV_OUTPUT: "0.002"}))
    assert s.input_price_per_1k is None, "非法价格必须回退为未配置，不得猜测"
    assert s.output_price_per_1k == pytest.approx(0.002)
    assert s.cost_breakdown(1000, 1000).total_cost is None
    if bad.strip() and bad not in {"nan", "inf"}:
        assert PRICE_ENV_INPUT in s.price_errors


def test_illegal_legacy_price_does_not_break_loading():
    s = load_llm_settings(_env(**{PRICE_ENV_LEGACY_INPUT: "abc"}))
    assert s.input_price_per_1k is None
    assert PRICE_ENV_LEGACY_INPUT in s.price_errors
    assert s.pricing_source == PRICING_NOT_CONFIGURED


# ---------- 6. 影子报告包含成本列 ----------

def test_offline_report_shows_zero_tokens_and_na_cost(tmp_path):
    r = shadow.run(mode="offline", limit=3, outdir=tmp_path)
    assert r["provider"] == "offline-rule"
    assert r["input_tokens"] == 0 and r["output_tokens"] == 0
    assert r["total_cost"] is None and r["avg_cost_per_case"] is None
    assert "N/A" in r["pricing_source"]

    report = (tmp_path / "shadow_eval_offline.md").read_text(encoding="utf-8")
    for field in ("输入 token", "输出 token", "总 token", "输入成本", "输出成本",
                  "总成本", "单条平均成本", "价格配置来源"):
        assert field in report, f"报告缺少成本字段：{field}"
    assert "候选模型" in report and "业务副作用" in report
    assert "in_tok" in report and "out_tok" in report


def test_candidate_without_key_is_unmeasured_and_never_networks(tmp_path, monkeypatch):
    monkeypatch.delenv("OPSPILOT_LLM_API_KEY", raising=False)
    monkeypatch.delenv("OPSPILOT_LLM_BASE_URL", raising=False)
    monkeypatch.delenv("OPSPILOT_LLM_MODEL", raising=False)
    r = shadow.run(mode="candidate", limit=2, outdir=tmp_path)
    assert r["candidate_measured"] is False
    assert r["network_requests"] == 0
    assert r["provider"] == "offline-rule"
    assert r["candidate_blockers"], "必须记录被阻断的原因"

    report = (tmp_path / "shadow_eval_candidate.md").read_text(encoding="utf-8")
    assert shadow.UNMEASURED_NOTE in report
    assert "真实准确率" in report and "真实成本" in report


def test_candidate_requires_explicit_model_name(tmp_path, monkeypatch):
    """Key 与白名单就绪但未显式配置模型名 → 仍不联网。"""
    monkeypatch.setenv("OPSPILOT_LLM_API_KEY", SECRET)
    monkeypatch.setenv("OPSPILOT_LLM_BASE_URL", "https://api.deepseek.com/v1")
    monkeypatch.delenv("OPSPILOT_LLM_MODEL", raising=False)
    r = shadow.run(mode="candidate", limit=2, outdir=tmp_path)
    assert r["candidate_measured"] is False
    assert any("模型名" in b for b in r["candidate_blockers"])


def test_candidate_with_all_conditions_but_no_price_marks_na(tmp_path, monkeypatch):
    """全部联网条件满足但价格未配置：允许联网，但报告必须把成本标为 N/A。

    为避免真实联网，用本地回环 mock 端点（不可达 → 全部降级离线），
    断言成本仍为 N/A 且未把 0 写成真实成本。
    """
    monkeypatch.setenv("OPSPILOT_LLM_API_KEY", SECRET)
    monkeypatch.setenv("OPSPILOT_LLM_BASE_URL", "http://127.0.0.1:59999/v1")
    monkeypatch.setenv("OPSPILOT_LLM_MODEL", "mock-model")
    monkeypatch.setenv("OPSPILOT_LLM_TIMEOUT_S", "0.05")
    monkeypatch.setenv("OPSPILOT_LLM_MAX_RETRIES", "0")
    monkeypatch.delenv(PRICE_ENV_INPUT, raising=False)
    monkeypatch.delenv(PRICE_ENV_OUTPUT, raising=False)
    monkeypatch.delenv(PRICE_ENV_LEGACY_INPUT, raising=False)

    r = shadow.run(mode="candidate", limit=2, outdir=tmp_path)
    assert r["candidate_measured"] is True           # 安全条件满足 → 允许候选路径
    assert r["pricing_configured"] is False
    assert r["total_cost"] is None
    report = (tmp_path / "shadow_eval_candidate.md").read_text(encoding="utf-8")
    assert "N/A" in report
    assert "不代表 0 成本" in report


# ---------- 7. 密钥不出现在报告与日志 ----------

def test_api_key_never_appears_in_report_or_summary(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("OPSPILOT_LLM_API_KEY", SECRET)
    monkeypatch.setenv("OPSPILOT_LLM_BASE_URL", "https://api.deepseek.com/v1")
    monkeypatch.setenv("OPSPILOT_LLM_MODEL", "deepseek-chat")
    monkeypatch.setenv(PRICE_ENV_INPUT, "0.001")
    monkeypatch.setenv(PRICE_ENV_OUTPUT, "0.002")

    r = shadow.run(mode="candidate", limit=1, outdir=tmp_path)
    printed = capsys.readouterr().out
    report = (tmp_path / "shadow_eval_candidate.md").read_text(encoding="utf-8")
    assert SECRET not in printed
    assert SECRET not in report
    assert SECRET not in Path(r["report_path"]).read_text(encoding="utf-8")


def test_redacted_summary_includes_pricing_but_not_key():
    s = load_llm_settings(_env(**{PRICE_ENV_INPUT: "0.001", PRICE_ENV_OUTPUT: "0.002"}))
    summary = s.redacted_summary()
    assert SECRET not in summary
    assert "pricing=" in summary
