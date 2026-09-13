"""Small PG worker used by the cross-process restart integration test.

The worker intentionally creates a fresh runner and checkpointer in each
process. It is test-only and never seeds or resets a database.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from fastapi.testclient import TestClient  # noqa: E402

from scripts.run_api import build_pg_backend, demo_token_registry  # noqa: E402
from src.api import create_app  # noqa: E402


def _build(args):
    built = build_pg_backend(args.url, checkpoint_path=args.checkpoint,
                             owner_id=args.owner, lease_duration_s=args.lease_duration)
    built["app"] = create_app(
        built["backend"], demo_token_registry(), pg_probe=built["probe"],
        require_expected_version=True, agent_runner=built["runner"],
    )
    return built


def run(args) -> dict:
    built = _build(args)
    try:
        with TestClient(built["app"]) as client:
            if args.mode == "start":
                response = client.post(
                    "/api/v1/agent/start",
                    json={"message": "订单 ORD-1 商品破损，要求退款",
                          "thread_id": args.thread, "order_id_hint": "ORD-1"},
                    headers={"X-Api-Key": "demo-agent"},
                )
                return {"status_code": response.status_code, "body": response.json()}

            state = client.get(f"/api/v1/agent/{args.thread}/state",
                               headers={"X-Api-Key": "demo-agent"})
            body = state.json()
            operation_id = body["operation_id"]
            version = built["backend"].get_operation("T1", operation_id).version
            approved = client.post(
                f"/api/operations/{operation_id}/approve",
                json={"expected_version": version},
                headers={"X-Api-Key": "demo-approver"},
            )
            decided = client.post(
                f"/api/v1/agent/{args.thread}/decision", json={},
                headers={"X-Api-Key": "demo-approver"},
            )
            return {
                "state_status": state.status_code,
                "state": body,
                "approve_status": approved.status_code,
                "approve": approved.json(),
                "decision_status": decided.status_code,
                "decision": decided.json(),
            }
    finally:
        from src.agents.checkpoint import close_sqlite_checkpointer
        close_sqlite_checkpointer(built["checkpointer"])


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--mode", choices=("start", "finish"), required=True)
    parser.add_argument("--url", required=True)
    parser.add_argument("--checkpoint", required=True)
    parser.add_argument("--owner", required=True)
    parser.add_argument("--thread", required=True)
    parser.add_argument("--lease-duration", type=int, default=60)
    args = parser.parse_args()
    print(json.dumps(run(args), ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
