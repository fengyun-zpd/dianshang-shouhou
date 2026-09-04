"""影子评测入口冒烟测试：offline 实测 / candidate 无 Key 安全降级不联网。"""
from evals import run_model_shadow_eval as shadow


def test_offline_shadow_run(tmp_path):
    r = shadow.run(mode="offline", limit=3, outdir=tmp_path)
    assert r["total"] == 3
    assert r["error_count"] == 0
    assert r["provider"] == "offline-rule"
    assert r["intent_accuracy"] == 1.0
    assert (tmp_path / "shadow_eval_offline.md").exists()
    assert "business_side_effect" in r and "0" in r["business_side_effect"]


def test_candidate_without_key_safe_degrade_no_network(tmp_path, monkeypatch):
    monkeypatch.delenv("OPSPILOT_LLM_API_KEY", raising=False)
    monkeypatch.delenv("OPSPILOT_LLM_BASE_URL", raising=False)
    r = shadow.run(mode="candidate", limit=2, outdir=tmp_path)
    assert r["provider"] == "offline-rule"           # 未启用主模型
    assert any("未实测" in n or "安全失败" in n for n in r["notes"])
    assert r["degraded_count"] == 0                   # 未联网 → 无主模型降级记录（纯离线对照）


def test_candidate_notes_mark_unmeasured_in_report(tmp_path, monkeypatch):
    monkeypatch.delenv("OPSPILOT_LLM_API_KEY", raising=False)
    r = shadow.run(mode="candidate", limit=1, outdir=tmp_path)
    content = (tmp_path / "shadow_eval_candidate.md").read_text(encoding="utf-8")
    assert "未实测" in content
