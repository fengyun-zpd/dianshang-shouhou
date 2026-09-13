"""OpsPilot API 可启动服务（阶段 A 端口收敛 / R7：--backend pg 真实装配）。

用法（项目根，D 盘 .venv）：
    .venv\\Scripts\\python.exe scripts\\run_api.py                        # memory 开发后端（默认，合成 seed）
    .venv\\Scripts\\python.exe scripts\\run_api.py --host 0.0.0.0 --port 8080
    .venv\\Scripts\\python.exe scripts\\run_api.py --backend pg [--pg-url 连接串] [--checkpoint 文件] [--owner-id 值]

行为与边界：
- `--backend memory`（默认）：开发/演示后端——内存 AfterSalesService + 合成 seed +
  内存 token registry，不连接任何真实支付/退款/CRM/企业微信/生产系统（禁止项）；
  经 MemoryAdapter 注入 create_app（API 只依赖 AfterSalesApplicationPort）。
- `--backend pg`（R7 真实装配，no-fallback）：
    1. 连接串取 --pg-url 或 DATABASE_URL；缺失 → stderr 报错退出码非 0；
    2. require_postgres_ready(url)：PostgreSQL 不可达或 Alembic schema ≠ 0006 → RuntimeError
       退出码非 0，**绝不静默回退 memory**；
    3. PostgresAfterSalesRepository(url) → PgCommandService(repo) → PgCommandAdapter（完整 Port）；
    4. 持久 checkpoint：open_sqlite_checkpointer(--checkpoint 或项目 `.runtime/tmp` 唯一文件)
       ——checkpoint 只存 LangGraph 流程恢复状态，不存业务最终真相（宪法第五条）；
    5. WorkflowRunner(backend, checkpointer=cp, lease_repo=repo, owner_id=--owner-id
       或 "run_api-{hostname}-{pid}"（稳定唯一）, lease_duration_s=60)——pg profile
       默认强制 D9 workflow_threads 租约；
    6. create_app(backend, registry, pg_probe=probe, require_expected_version=True,
       agent_runner=runner)
       ——pg 审批要求客户端提交 expected_version（422 拒绝服务端代填）；
       /api/v1/agent/* 由同一 PG runner 驱动（真实 PostgreSQL 事实源 + 持久 checkpoint
       + D9 租约）；/api/demo/reset 在 pg profile 下**不注册**。
  **PG 业务数据（订单/政策/工单等）启动时不自动 seed**（防污染未知库）；
  seed 仅存在于 memory 分支（build_memory_backend）。token registry 为进程内演示映射。
- Agent 生命周期 HTTP 接口（memory/pg 共用同一装配路径）：
    POST /api/v1/agent/start              启动（Agent/Customer；租户取认证身份）
    POST /api/v1/agent/{thread_id}/clarify 澄清补参（仅澄清中断状态）
    POST /api/v1/agent/{thread_id}/decision 触发恢复（仅 APPROVER/SYSTEM；空 body；
                                            审批结论必须先写入领域事实源）
    GET  /api/v1/agent/{thread_id}/state  只读线程视图（租户作用域）
  未装配 runner 时上述接口返回 503 AGENT_RUNNER_UNAVAILABLE（不静默降级）。
- /health/live 恒 200；/health/ready 反映 PostgreSQL 可用性（pg 就绪 → 200；探测不可用 → 503）。
"""
from __future__ import annotations

import argparse
import os
import socket
import sys
from decimal import Decimal
from pathlib import Path
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from fastapi.testclient import TestClient  # noqa: E402  仅用于冒烟检查
from sqlalchemy import create_engine, text  # noqa: E402

from src.agents import WorkflowRunner  # noqa: E402
from src.api import ApiIdentity, ApiTokenRegistry, create_app  # noqa: E402
from src.api.runtime import require_postgres_ready  # noqa: E402
from src.domain.after_sales import Order, OrderItem, OrderStatus, PolicyRule, RequestType, Role  # noqa: E402
from src.domain.after_sales import AfterSalesService  # noqa: E402
from src.domain.after_sales.adapters import MemoryAdapter, PgCommandAdapter  # noqa: E402
from src.domain.after_sales.pg_commands import PgCommandService  # noqa: E402
from src.repo import PostgresAfterSalesRepository  # noqa: E402


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
    return svc, demo_token_registry()


def demo_token_registry() -> ApiTokenRegistry:
    """进程内演示 token 映射（仅身份；业务数据是否 seed 由后端分支决定）。

    含一个 T2 演示身份：用于工作台与演示脚本的「跨租户拒绝」路径
    （Agent 生命周期只服务内部坐席，T2 读 T1 线程应得到统一 404）。
    """
    reg = ApiTokenRegistry()
    reg.register("demo-agent", ApiIdentity("agent-1", "T1", Role.AGENT))
    reg.register("demo-approver", ApiIdentity("approver-1", "T1", Role.APPROVER))
    reg.register("demo-system", ApiIdentity("system-1", "T1", Role.SYSTEM))
    reg.register("demo-customer", ApiIdentity("cust-1", "T1", Role.CUSTOMER, customer_id="C1"))
    reg.register("demo-agent-t2", ApiIdentity("agent-2", "T2", Role.AGENT))
    return reg


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


def _default_checkpoint_path() -> str:
    """**固定**的 D 盘 SQLite checkpoint 文件：`.runtime/checkpoints/opspilot-agent.sqlite`。

    重启恢复要求：pg profile 不能用 PID 临时文件——否则进程重启后 checkpoint 丢失，
    新实例无法按 (tenant_id, thread_id) 恢复线程。固定路径 + 租户限定的
    workflow_threads 行共同保证跨进程恢复（checkpoint 只存流程状态，不存业务真相）。
    可用 --checkpoint 显式覆盖（多实例部署应各自指定不同文件或改用服务端 checkpointer）。
    """
    from src.platform.runtime_paths import runtime_checkpoints_dir
    return str(runtime_checkpoints_dir() / "opspilot-agent.sqlite")


def _default_owner_id() -> str:
    """pg profile 默认租约 owner：hostname+pid 稳定唯一。"""
    return f"run_api-{socket.gethostname()}-{os.getpid()}"


def build_pg_backend(url: str, checkpoint_path: str | None = None,
                     owner_id: str | None = None, lease_duration_s: int = 60) -> dict:
    """pg profile 真实装配（PA3/R7；失败 → RuntimeError，绝不回退 memory）。

    链：require_postgres_ready(url) → PostgresAfterSalesRepository → PgCommandService →
    PgCommandAdapter（完整 AfterSalesApplicationPort）→ SQLite 持久 checkpoint →
    WorkflowRunner(lease_repo=repo, owner_id=稳定唯一, lease_duration_s)。
    本函数不 seed 任何 PG 业务数据（防污染未知库）。
    返回装配结果字典（backend/runner/checkpointer/probe 等），便于启动与测试。
    """
    require_postgres_ready(url)          # 不可达 / schema≠0006 → RuntimeError（no fallback）
    repo = PostgresAfterSalesRepository(url)
    backend = PgCommandAdapter(PgCommandService(repo), repo)
    cp_path = checkpoint_path or _default_checkpoint_path()
    from src.agents.checkpoint import close_sqlite_checkpointer, open_sqlite_checkpointer
    cp = open_sqlite_checkpointer(cp_path)   # checkpoint 只存流程恢复状态，不存业务真相
    owner = owner_id or _default_owner_id()
    runner = WorkflowRunner(backend, checkpointer=cp,
                            lease_repo=repo, owner_id=owner,
                            lease_duration_s=lease_duration_s)
    return {
        "url": url,
        "repo": repo,
        "backend": backend,
        "runner": runner,
        "checkpointer": cp,
        "checkpoint_path": cp_path,
        "owner_id": owner,
        "probe": make_pg_probe(url),
    }


def _smoke(app) -> None:
    """启动前冒烟自检（TestClient；不启动真实监听）。"""
    with TestClient(app) as client:
        live = client.get("/health/live").status_code
        ready = client.get("/health/ready").status_code
        assert live == 200, f"/health/live={live}"
        assert ready == 200 or ready == 503, f"/health/ready={ready}"


def main() -> int:
    parser = argparse.ArgumentParser(description="OpsPilot After-Sales API 启动入口")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--backend", choices=("memory", "pg"), default="memory",
                        help="业务后端：memory（开发演示 seed）或 pg（PostgreSQL 唯一事实源，"
                             "真实装配 PgCommandAdapter + 持久 checkpoint + D9 租约；"
                             "失败绝不回退 memory）")
    parser.add_argument("--pg-url", default=os.environ.get("DATABASE_URL", ""),
                        help="PostgreSQL 连接串（--backend pg 必需，或设 DATABASE_URL）；"
                             "memory 分支提供时仅启用 /health/ready 真实探测")
    parser.add_argument("--checkpoint", default=None,
                        help="LangGraph 持久 checkpoint 的 SQLite 文件路径（仅 pg；缺省 "
                             ".runtime/checkpoints/opspilot-agent.sqlite 固定路径，"
                             "保证进程重启后仍能恢复线程）。checkpoint 只存流程恢复状态，"
                             "不存业务最终真相")
    parser.add_argument("--owner-id", default=None,
                        help="workflow_threads 租约 owner 标识（仅 pg；缺省 "
                             "run_api-{hostname}-{pid}，稳定唯一）。进程重启接管以同 owner 续租")
    args = parser.parse_args()

    if args.backend == "pg":
        url = args.pg_url or os.environ.get("DATABASE_URL", "")
        if not url:
            print("--backend pg 需要 --pg-url 或 DATABASE_URL（PostgreSQL 连接串）",
                  file=sys.stderr)
            return 2
        built = build_pg_backend(url, checkpoint_path=args.checkpoint,
                                 owner_id=args.owner_id)
        # pg profile：审批 expected_version 必填（服务端不代填）；业务数据未 seed
        app = create_app(built["backend"], demo_token_registry(),
                         pg_probe=built["probe"], require_expected_version=True,
                         agent_runner=built["runner"])
        print(f"pg profile 装配完成：repo={built['url']}；owner={built['owner_id']}；"
              f"checkpoint={built['checkpoint_path']}（固定路径：重启后可按 "
              f"(tenant_id, thread_id) 从 workflow_threads + checkpoint 恢复）")
    else:
        svc, reg = build_memory_backend()
        probe = make_pg_probe(args.pg_url) if args.pg_url else None
        # memory 后端经 MemoryAdapter 注入（API 只依赖 AfterSalesApplicationPort）
        service = MemoryAdapter(svc)
        agent_runner = WorkflowRunner(service)

        def reset_memory_demo() -> None:
            """重置合成演示数据：**同时**重建 service 与 runner。

            runner 持有网关（绑定后端 Port）与内存 checkpoint；只换 service 会让
            runner 继续引用旧服务并残留旧线程，因此必须成对重建。
            """
            nonlocal service, agent_runner
            fresh_service, _ = build_memory_backend()
            service = MemoryAdapter(fresh_service)
            agent_runner = WorkflowRunner(service)
            app.state.service = service
            app.state.agent_runner = agent_runner

        app = create_app(service, reg, pg_probe=probe,
                         demo_reset=reset_memory_demo,
                         agent_runner=agent_runner)

    _smoke(app)

    import uvicorn
    print(f"OpsPilot API 启动：http://{args.host}:{args.port} "
          f"(backend={args.backend}；pg_probe={'on' if args.backend == 'pg' or args.pg_url else 'off'})")
    print("开发 token：X-Api-Key = demo-agent / demo-approver / demo-system / "
          "demo-customer / demo-agent-t2（T2 演示身份，用于跨租户拒绝演示）")
    if args.backend == "pg":
        print("边界：PG 业务数据启动时不自动 seed；checkpoint 仅存流程恢复状态；"
              "线程唯一键 = (tenant_id, thread_id)，跨进程可恢复。")
    else:
        print("边界：memory profile 为**进程内合成演示**——业务数据与流程状态都在进程内存中，"
              "重启即重置，不提供持久恢复。")
    uvicorn.run(app, host=args.host, port=args.port)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
