"""阶段四收口 RED 契约（先写失败测试，再修实现；修复后移除 xfail 转 PASS）。

R1/R2：WorkflowRunner 需暴露可识别失约错误并支持 lease/owner 注入（现状缺失）。
R3/R4：workflow_threads fingerprint 语义（同 owner 续租不可改 fp；异 fp 接管/重复 start 拒绝）
       须写入 claim_thread 契约并在实现中强制。
R5：PG API 审批 decided_by = 认证主体（API 路由须把 identity principal 传入审批）。
R6：pg profile approve/reject 缺 expected_version → 稳定错误；服务端不得为客户端补版本。
R7：run_api --backend pg 真实装配（现仅 memory choices + --require-pg 门禁）。
R8（正回归）：端口只读 tenant-first。
"""
import inspect
import io
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
red = pytest.mark.xfail(strict=True, reason="P4-FINAL: 阶段四收口契约尚未实现")


def _read(rel: str) -> str:
    return io.open(ROOT / rel, encoding="utf-8").read()


# ---------- R1/R2 ----------

@red
def test_runner_exposes_lease_error_and_injection():
    """Runner 需提供 ThreadLeaseError 与 lease/owner 构造注入（现状缺失 → RED）。"""
    src = _read("src/agents/runner.py")
    assert "ThreadLeaseError" in src
    assert "owner" in src and "lease" in src


# ---------- R3/R4 ----------

def test_claim_thread_fingerprint_semantics_documented_and_enforced():
    """claim_thread 契约含 fp 语义：同 owner 续租不可变、异 fp 接管/重复 start 拒绝。
    行为断言见 tests/phase4/test_d9_workflow_lease_live.py（live，PG）。"""
    src = _read("src/repo/interfaces.py")
    block = src[src.index("def claim_thread"):]
    doc = block[:700]
    assert "不可变" in doc
    assert "不同 → 拒绝" in doc or "拒绝" in doc


# ---------- R5 ----------

@red
def test_api_approve_passes_identity_principal():
    """API approve/reject 必须把认证 principal 传入审批（decided_by=approver-1 而非角色）。"""
    src = _read("src/api/app.py")
    approve_idx = src.index("def approve(")
    block = src[approve_idx:approve_idx + 1200]
    assert "principal" in block


# ---------- R6 ----------

def test_pg_approve_requires_client_expected_version():
    """pg profile 审批缺 expected_version → 稳定拒绝；服务端不得用 op.version 替客户端补。
    行为断言见 tests/unit/api/test_decision_version_required.py。"""
    src = _read("src/api/app.py")
    approve_idx = src.index("def approve(")
    block = src[approve_idx:approve_idx + 1200]
    assert "expected_version" in block
    assert "else op.version" not in block      # 服务端不再以当前版本兜底
    assert "or op.version" not in block
    assert "EXPECTED_VERSION_REQUIRED" in src


# ---------- R7 ----------

@red
def test_run_api_backend_pg_assembly():
    """run_api 必须支持 --backend pg 真正装配 PG 后端（现仅 memory choices）。"""
    src = _read("scripts/run_api.py")
    assert '"pg"' in src or "'pg'" in src
    assert "require_postgres_ready" in src


# ---------- R8（正回归：端口只读 tenant-first） ----------

def test_port_reads_are_tenant_first():
    src = _read("src/domain/after_sales/ports.py")
    assert src.count("tenant_id") >= 2
    assert "def get_ticket(self, tenant_id" in src
    assert "def get_operation(self, tenant_id" in src
