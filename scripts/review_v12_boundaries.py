"""V1.2 审查复现：仅本地合成数据；不修改应用实现。

先加载 scripts/init_d_env.ps1，再运行本脚本。默认只用 memory TestClient。
--pg 额外验证已迁移的本地隔离测试库中的 checkpoint 碰撞，不重置任何表。
所有业务动作经 HTTP；领域只读用于核对最终状态。FAIL 表示未满足验收，
ERROR 表示复现异常；二者都非零退出，不能当作测试通过。
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.api import ApiIdentity  # noqa: E402
from src.domain.after_sales import Role  # noqa: E402
from src.platform.runtime_paths import runtime_dir  # noqa: E402
from tests.e2e.test_agent_http_lifecycle import _client, _h, _start  # noqa: E402


def post(client, path, body, token="tok-approver"):
    response = client.post(path, json=body, headers=_h(token))
    if response.status_code != 200:
        raise RuntimeError(f"setup_http_status={response.status_code}")
    return response.json()


def collision_memory():
    client, _, _ = _client()
    with client:
        client.app.state.registry.register(
            "tok-colon", ApiIdentity("synthetic-owner", "T1:dept", Role.AGENT))
        post(client, "/api/v1/agent/start",
             {"message": "我要退款，商品破损", "thread_id": "victim"}, "tok-colon")
        view = client.get("/api/v1/agent/dept:victim/state", headers=_h("tok-agent"))
        actual = {"other_tenant_http": view.status_code,
                  "returned_owner_tenant": view.json().get("state", {}).get("tenant_id")}
        return view.status_code == 404, actual


def pii_view():
    client, _, _ = _client()
    with client:
        phone = "13800138000"  # 固定合成夹具，不是真实客户资料；报告不输出原文。
        started = _start(client, message="订单 ORD-1 商品破损，要求退款；合成测试电话 " + phone,
                         thread_id="pii-review")
        if started.status_code != 200:
            raise RuntimeError("pii_setup_failed")
        view = client.get("/api/v1/agent/pii-review/state", headers=_h("tok-approver"))
        actual = {"raw_phone_in_start": phone in started.text,
                  "raw_phone_in_approver_state": phone in view.text}
        return view.status_code == 200 and not any(actual.values()), actual


def audit_tenants():
    client, _, _ = _client()
    with client:
        a = _start(client, thread_id="audit-t1").json()
        b = _start(client, message="订单 ORD-T2 商品破损，要求退款", thread_id="audit-t2",
                   order="ORD-T2", tok="tok-agent-t2").json()
        actual = {"t1_ids": len(a["audit_event_ids"]), "t2_ids": len(b["audit_event_ids"]),
                  "t2_domain_events": len(client.app.state.service.audit_log("T2"))}
        return actual["t2_ids"] == actual["t2_domain_events"] == 3, actual


def audit_threads():
    client, _, _ = _client()
    with client:
        a = _start(client, thread_id="audit-a").json()
        b = _start(client, message="订单 ORD-2 商品破损，要求退款", thread_id="audit-b",
                   order="ORD-2").json()
        for item in (a, b):
            post(client, f"/api/operations/{item['operation_id']}/approve", {})
        done = post(client, "/api/v1/agent/audit-a/decision", {})
        leaked = any(b["operation_id"] in event for event in done["audit_event_ids"])
        return not leaked, {"other_thread_approval_in_a_audit": leaked}


def unknown_settlement():
    client, service, _ = _client()
    with client:
        a = _start(client, thread_id="unknown-review").json()
        op_path = f"/api/operations/{a['operation_id']}"
        post(client, op_path + "/approve", {})
        post(client, op_path + "/execute", {"external_result": "timeout"}, "tok-system")
        post(client, "/api/v1/agent/unknown-review/decision", {})
        settled = post(client, op_path + "/reconcile", {"result": "success"}, "tok-system")
        resumed = client.post("/api/v1/agent/unknown-review/decision", json={},
                              headers=_h("tok-approver"))
        view = client.get("/api/v1/agent/unknown-review/state", headers=_h("tok-agent")).json()
        actual = {"domain_status": settled["status"], "workflow_outcome": view["outcome"],
                  "resume_http": resumed.status_code,
                  "ticket_status": service.get_ticket(a["ticket_id"]).status.value,
                  "refunded_amount": str(service.refunded_amount("ORD-1"))}
        passed = (resumed.status_code == 200 and actual["ticket_status"] == "closed"
                  and actual["workflow_outcome"] in {"refunded", "already_executed"}
                  and actual["refunded_amount"] == "100.00")
        return passed, actual


def externally_executed_settlement():
    client, service, _ = _client()
    with client:
        a = _start(client, thread_id="executed-review").json()
        op_path = f"/api/operations/{a['operation_id']}"
        post(client, op_path + "/approve", {})
        post(client, op_path + "/execute", {"external_result": "success"}, "tok-system")
        done = post(client, "/api/v1/agent/executed-review/decision", {})
        actual = {"finished": done["finished"], "outcome": done["outcome"],
                  "ticket_status": service.get_ticket(a["ticket_id"]).status.value,
                  "execute_in_audit": any(":execute:" in i for i in done["audit_event_ids"])}
        return actual["ticket_status"] == "closed" and actual["execute_in_audit"], actual


def collision_pg():
    from fastapi.testclient import TestClient
    from scripts.run_api import build_pg_backend
    from src.agents.checkpoint import close_sqlite_checkpointer
    from src.api import ApiTokenRegistry, create_app
    from src.platform.pg_test_guard import require_isolated_test_db

    url = os.environ.get("OPSPILOT_TEST_DATABASE_URL")
    if not url:
        raise RuntimeError("OPSPILOT_TEST_DATABASE_URL_required")
    require_isolated_test_db(url)  # 必须先于连接；绝不回退到 DATABASE_URL。
    namespace = "REVIEW_" + uuid4().hex[:8]
    checkpoint = runtime_dir("review-20260913") / f"{namespace}.sqlite"
    built = build_pg_backend(url, checkpoint_path=str(checkpoint), owner_id=namespace)
    try:
        registry = ApiTokenRegistry()
        registry.register("victim", ApiIdentity("synthetic-owner", namespace + ":dept", Role.AGENT))
        registry.register("reader", ApiIdentity("synthetic-reader", namespace, Role.AGENT))
        app = create_app(built["backend"], registry, agent_runner=built["runner"],
                         require_expected_version=True)
        with TestClient(app) as client:
            post(client, "/api/v1/agent/start",
                 {"message": "我要退款，商品破损", "thread_id": "victim"}, "victim")
            # PG 中攻击者先 start：旧实现会留下本租户 workflow_threads 行，再报指纹冲突。
            started = client.post("/api/v1/agent/start",
                                  json={"message": "我要退款，商品破损", "thread_id": "dept:victim"},
                                  headers=_h("reader"))
            view = client.get("/api/v1/agent/dept:victim/state", headers=_h("reader"))
            owner = view.json().get("state", {}).get("tenant_id")
            actual = {"colliding_start_http": started.status_code, "read_http": view.status_code,
                      "other_tenant_state_returned": owner == namespace + ":dept"}
            return started.status_code == 200 and view.status_code == 200 and owner == namespace, actual
    finally:
        close_sqlite_checkpointer(built["checkpointer"])


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pg", action="store_true", help="额外检查显式本地隔离 PG 测试库")
    args = parser.parse_args()
    cases = [("R1-memory-namespace", collision_memory), ("R2-http-pii", pii_view),
             ("R3-audit-tenants", audit_tenants), ("R3-audit-threads", audit_threads),
             ("R4-unknown-settlement", unknown_settlement),
             ("R4-executed-settlement", externally_executed_settlement)]
    if args.pg:
        cases.append(("R1-pg-namespace", collision_pg))
    rows = []
    for name, probe in cases:
        try:
            passed, actual = probe()
            row = {"case": name, "status": "PASS" if passed else "FAIL", "actual": actual}
        except Exception as error:
            # 异常原文可能带请求或连接信息，报告只记录类型。
            row = {"case": name, "status": "ERROR", "error_type": type(error).__name__}
        rows.append(row)
        print(json.dumps(row, ensure_ascii=False))
    report = {"head": subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT,
                                             text=True).strip(),
              "run_utc": datetime.now(timezone.utc).isoformat(),
              "mode": "memory+isolated_pg" if args.pg else "memory",
              "synthetic_data": True, "model": "offline_rule; no model network calls",
              "prompt_version": "N/A (deterministic workflow)", "dataset_version": "review_v12_v1",
              "results": rows}
    target = runtime_dir("review-20260913") / ("boundaries-pg.json" if args.pg else "boundaries.json")
    target.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"report={target}")
    return 0 if all(row["status"] == "PASS" for row in rows) else 1


if __name__ == "__main__":
    raise SystemExit(main())
