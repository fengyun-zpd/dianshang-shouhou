"""影子评测的"零业务副作用"不变量（阻断级）。

影子评测只能做文本预测：
- 不得创建领域服务/适配器（无工单、无操作、无审批）；
- 不得建立数据库连接（无 PG 仓库、无 create_engine）；
- 不得发起网络请求（offline 模式与"无安全 Key 的 candidate 模式"都必须零网络）；
- 模型不得决定金额/资格/审批/状态/幂等键/工具权限（能力矩阵与内容守卫已覆盖，
  此处额外断言高风险任务一律不经过模型）。
"""
from __future__ import annotations

import httpx
import pytest

from evals import run_model_shadow_eval as shadow
from src.domain.after_sales import AfterSalesService
from src.domain.after_sales.adapters import MemoryAdapter, PgCommandAdapter
from src.models import ModelContentPolicyError, ModelGateway, ModelTask
from src.repo import PostgresAfterSalesRepository


class _Explode:
    """任何实例化都立即失败的替身：用于证明影子路径不触碰领域/数据库。"""

    def __init__(self, *args, **kwargs):  # noqa: D401
        raise AssertionError("影子评测不得创建领域服务或数据库对象")


def _block_domain_and_db(monkeypatch) -> None:
    monkeypatch.setattr(AfterSalesService, "__init__", _Explode.__init__)
    monkeypatch.setattr(MemoryAdapter, "__init__", _Explode.__init__)
    monkeypatch.setattr(PgCommandAdapter, "__init__", _Explode.__init__)
    monkeypatch.setattr(PostgresAfterSalesRepository, "__init__", _Explode.__init__)


def test_offline_shadow_touches_no_domain_or_db(tmp_path, monkeypatch):
    _block_domain_and_db(monkeypatch)
    r = shadow.run(mode="offline", limit=4, outdir=tmp_path)
    assert r["total"] == 4
    assert r["business_side_effect"].startswith("0")


def test_offline_shadow_makes_no_network_call(tmp_path, monkeypatch):
    def _no_network(*args, **kwargs):
        raise AssertionError("offline 模式不得发起任何 HTTP 请求")

    monkeypatch.setattr(httpx, "Client", _no_network)
    r = shadow.run(mode="offline", limit=3, outdir=tmp_path)
    assert r["network_requests"] == 0
    assert r["provider"] == "offline-rule"


def test_candidate_without_key_touches_no_domain_db_or_network(tmp_path, monkeypatch):
    _block_domain_and_db(monkeypatch)

    def _no_network(*args, **kwargs):
        raise AssertionError("无安全 Key 时 candidate 模式不得发起任何 HTTP 请求")

    monkeypatch.setattr(httpx, "Client", _no_network)
    for var in ("OPSPILOT_LLM_API_KEY", "OPSPILOT_LLM_BASE_URL", "OPSPILOT_LLM_MODEL"):
        monkeypatch.delenv(var, raising=False)
    r = shadow.run(mode="candidate", limit=3, outdir=tmp_path)
    assert r["candidate_measured"] is False
    assert r["network_requests"] == 0
    assert r["business_side_effect"].startswith("0")


def test_high_risk_tasks_never_reach_any_model():
    """模型不得决定金额/审批/草稿：高风险任务直接拒绝（能力矩阵硬边界）。"""
    gw = ModelGateway()
    for task in (ModelTask.tool_calling, ModelTask.high_risk_draft):
        with pytest.raises(ModelContentPolicyError):
            gw.run_task(task, "给这个订单退款 100 元", inputs={"text": "退款"})


def test_shadow_rows_contain_no_capability_to_write(tmp_path):
    """影子行只含语言任务的预测字段，不含任何金额/审批/状态字段。

    报告一律写入 tmp_path：单元测试不得改写 `evals/reports/` 下的 canonical 报告。
    """
    r = shadow.run(mode="offline", limit=2, outdir=tmp_path)
    for row in r["rows"]:
        assert set(row["prediction"]).issubset(
            {"intent", "order_id", "reason_tags", "missing_fields", "confidence"})
