"""API 健康端点测试（/health/live、/health/ready——readiness 反映 PostgreSQL 探测）。"""
from fastapi.testclient import TestClient

from src.api import ApiIdentity, ApiTokenRegistry, create_app
from src.domain.after_sales import Role
from src.domain.after_sales.adapters import MemoryAdapter
from tests.unit.domain.after_sales.helpers import service_with_policies


def _app(pg_probe=None):
    svc = service_with_policies(("P-DAMAGED-FULL", ("damaged",), "1.00", 30))
    reg = ApiTokenRegistry()
    reg.register("tok", ApiIdentity("a", "T1", Role.AGENT))
    return create_app(MemoryAdapter(svc), reg, pg_probe=pg_probe)


def test_health_live_ok():
    assert TestClient(_app()).get("/health/live").status_code == 200


def test_health_ready_ok_without_probe():
    """未配置外部依赖探测 → ready 恒 ok（标注 not-configured）。"""
    r = TestClient(_app()).get("/health/ready")
    assert r.status_code == 200
    assert r.json()["deps"]["postgresql"] == "not-configured"


def test_health_ready_degraded_when_pg_down():
    """配置了 PG 探测且不可用 → ready 503 degraded。"""
    def probe_down():
        return False

    r = TestClient(_app(pg_probe=probe_down)).get("/health/ready")
    assert r.status_code == 503
    assert r.json()["status"] == "degraded"


def test_health_ready_ok_when_pg_up():
    def probe_up():
        return True

    r = TestClient(_app(pg_probe=probe_up)).get("/health/ready")
    assert r.status_code == 200
    assert r.json()["deps"]["postgresql"] == "ok"


def test_health_endpoints_public_no_auth():
    """health 端点无需认证（探活/负载均衡用）。"""
    client = TestClient(_app())
    assert client.get("/health/live").status_code == 200
    assert client.get("/health/ready").status_code == 200


def test_api_documentation_public_but_business_routes_stay_protected():
    """本地 API 文档可打开；业务接口仍不可绕过 X-Api-Key。"""
    client = TestClient(_app())
    assert client.get("/docs").status_code == 200
    assert client.get("/openapi.json").status_code == 200
    assert client.get("/api/tickets/TKT-1").status_code == 401
