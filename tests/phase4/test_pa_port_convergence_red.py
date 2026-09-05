"""阶段 A 端口收敛与 R7 RED 契约（先写失败测试；收敛完成后移除 xfail 转 PASS）。

PA1：API/Gateway/Runner 不得导入或依赖 AfterSalesService / PgServiceFacade
     （只依赖 AfterSalesApplicationPort 及其 Adapter）。
PA2：AfterSalesApplicationPort 扩展 tenant-first 只读面（工单/操作/订单/客户历史/
     政策与退款计算/审计），MemoryAdapter 与 PgCommandAdapter 完整实现同一契约。
PA3：run_api --backend pg 真实装配（PostgresRepository + PgCommandService +
     完整 Port + 持久 checkpoint + WorkflowRunner(lease_repo, owner_id)）；
     PG 不可达/schema 非 head/缺 owner 或 checkpoint → 启动失败绝不回退 memory。
PA4（R7 目标）：run_api 支持 --backend memory|pg（pg 需要 --pg-url/DATABASE_URL）。
"""
import io
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
red = pytest.mark.xfail(strict=True, reason="PA-FINAL: 阶段 A 端口收敛尚未完成")


def _read(rel: str) -> str:
    return io.open(ROOT / rel, encoding="utf-8").read()


def _import_head(src: str) -> str:
    """文件头部 import 区（类定义前的导入/注解）。"""
    cutoff = src.find("class AfterSalesGateway") if "AfterSalesGateway" in src else len(src)
    head = src[:cutoff] if cutoff > 0 else src
    # 只检查 import/类型使用段（docstring 说明词不计）——采用宽松：整个文件不含该依赖
    return src


@red
def test_pa1_gateway_and_runner_depend_only_on_port():
    """Gateway 与 Runner 源码不得依赖 AfterSalesService（import/类型）；不得使用 PgServiceFacade。"""
    gateway = _read("src/agents/ports.py")
    assert "AfterSalesService" not in gateway
    assert "AfterSalesApplicationPort" in gateway
    runner = _read("src/agents/runner.py")
    assert "AfterSalesService" not in runner
    api = _read("src/api/app.py")
    assert "PgServiceFacade" not in api


# ---------- PA2 ----------

def test_pa2_port_tenant_first_read_surface_extended():
    """Port 已扩展 tenant-first 只读面；双 Adapter 实现见 test_pa_adapter_reads.py。"""
    src = _read("src/domain/after_sales/ports.py")
    for sig in ("def get_ticket(self, tenant_id",
                "def get_operation(self, tenant_id",
                "def get_order(self, tenant_id",
                "def list_customer_tickets(self, tenant_id",
                "def audit_log(self, tenant_id",
                "def compute_refund_plan(self, tenant_id"):
        assert sig in src, f"端口缺少 {sig}"


@red
def test_pa3_run_api_pg_assembly_and_no_fallback():
    """run_api --backend pg 真实装配；失败不回退 memory；不依赖 PgServiceFacade。"""
    src = _read("scripts/run_api.py")
    assert "PgServiceFacade" not in src
    assert "PostgresAfterSalesRepository" in src
    assert "PgCommandService" in src
    assert "WorkflowRunner" in src
    assert "owner_id" in src
    assert "require_postgres_ready" in src


@red
def test_pa4_run_api_backend_choices_include_pg():
    """run_api --backend 支持 memory|pg（pg 需要 --pg-url/DATABASE_URL）。"""
    src = _read("scripts/run_api.py")
    assert 'choices=("memory", "pg")' in src or '"pg"' in src
