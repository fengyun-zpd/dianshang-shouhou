"""OpsPilot API 可启动服务（阶段三第七步）。

用法（项目根，D 盘 .venv）：
    .venv\\Scripts\\python.exe scripts\\run_api.py                  # memory 开发后端（默认，合成 seed）
    .venv\\Scripts\\python.exe scripts\\run_api.py --host 0.0.0.0 --port 8080
    .venv\\Scripts\\python.exe scripts\\run_api.py --pg-url postgresql+psycopg2://...

行为与边界：
- 默认 `--backend memory`：开发/演示后端——合成 seed 数据 + 内存 token registry，
  不连接任何真实支付/退款/CRM/企业微信/生产系统（禁止项）；
- 可选 `--pg-url`：仅启用 /health/ready 的 PostgreSQL 探测（真实 SELECT 1）；
  业务命令仍由注入 service 承载——**PG-first 命令路由接入 API 为后续收口（未实现，不写成已实现）**；
- /health/live 恒 200；/health/ready 反映 PostgreSQL 可用性（配置探测时不可用 → 503 degraded）。
"""
from __future__ import annotations

import argparse
import os
import sys
from decimal import Decimal
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from fastapi.testclient import TestClient  # noqa: E402  仅用于冒烟检查
from sqlalchemy import create_engine, text  # noqa: E402

from src.api import ApiIdentity, ApiTokenRegistry, create_app  # noqa: E402
from src.domain.after_sales import Order, OrderItem, OrderStatus, PolicyRule, RequestType, Role  # noqa: E402
from src.domain.after_sales import AfterSalesService  # noqa: E402


def build_memory_backend() -> tuple[AfterSalesService, ApiTokenRegistry]:
    """开发 seed：固定合成订单 + 破损全额政策 + 演示 token（不连真实系统）。"""
    svc = AfterSalesService()
    svc.seed_order(Order(
        order_id="ORD-1001", tenant_id="T1", customer_id="C1",
        status=OrderStatus.DELIVERED, paid_amount=Decimal("200.00"),
        items=[OrderItem(sku="SKU-9", name="演示商品", quantity=1,
                         unit_price=Decimal("200.00"))],
        days_since_sign=1,
    ))
    svc.seed_policy(PolicyRule(
        policy_id="P-DEMO", tenant_id="T1", request_type=RequestType.REFUND,
        reason_tags=("damaged",), window_days=30, refund_ratio=Decimal("1.00"),
    ))
    reg = ApiTokenRegistry()
    reg.register("demo-agent", ApiIdentity("agent-1", "T1", Role.AGENT))
    reg.register("demo-approver", ApiIdentity("approver-1", "T1", Role.APPROVER))
    reg.register("demo-system", ApiIdentity("system-1", "T1", Role.SYSTEM))
    reg.register("demo-customer", ApiIdentity("cust-1", "T1", Role.CUSTOMER, customer_id="C1"))
    return svc, reg


def make_pg_probe(pg_url: str):
    def probe() -> bool:
        try:
            engine = create_engine(pg_url, connect_args={"connect_timeout": 3})
            with engine.connect() as conn:
                conn.execute(text("SELECT 1"))
            engine.dispose()
            return True
        except Exception:  # noqa: BLE001
            return False
    return probe


def main() -> int:
    parser = argparse.ArgumentParser(description="OpsPilot After-Sales API 启动入口")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--backend", choices=("memory",), default="memory",
                        help="业务后端：仅 memory（开发演示，不连真实系统）；PG-first 命令路由为规划")
    parser.add_argument("--pg-url", default=os.environ.get("DATABASE_URL", ""),
                        help="PostgreSQL 连接串；提供后 /health/ready 真实探测其可用性")
    parser.add_argument("--require-pg", action="store_true",
                        help="pg profile 门禁：要求 PostgreSQL 可达且 schema 版本=0005；"
                             "不满足则退出码 1（拒绝静默降级到内存）")
    args = parser.parse_args()

    if args.require_pg:
        from src.api.runtime import require_postgres_ready
        url = args.pg_url or os.environ.get("DATABASE_URL", "")
        if not url:
            print("--require-pg 需要 --pg-url 或 DATABASE_URL", file=sys.stderr)
            return 2
        require_postgres_ready(url)   # 不满足 → RuntimeError（不 fallback）
        print("pg profile 门禁通过（PostgreSQL 可达、schema=0005）；"
              "注意：API 业务后端仍为 memory——PG-first 命令路由接入 API 为进行中（阶段四第 3 节），"
              "本入口不静默声称已切换。")

    svc, reg = build_memory_backend()
    probe = make_pg_probe(args.pg_url) if args.pg_url else None
    app = create_app(svc, reg, pg_probe=probe)

    # 冒烟检查（不启动真实监听前的自检；uvicorn 在下方真实启动）
    with TestClient(app) as client:
        assert client.get("/health/live").status_code == 200
        assert client.get("/health/ready").status_code == 200 or \
            client.get("/health/ready").status_code == 503

    import uvicorn
    print(f"OpsPilot API 启动：http://{args.host}:{args.port} "
          f"(backend={args.backend}；pg_probe={'on' if probe else 'off'})")
    print("开发 token：X-Api-Key = demo-agent / demo-approver / demo-system / demo-customer")
    uvicorn.run(app, host=args.host, port=args.port)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
