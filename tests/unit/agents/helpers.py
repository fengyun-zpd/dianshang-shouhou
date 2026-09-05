"""Agent 层测试辅助：复用领域插件固定种子工厂构造服务与运行器。"""
from __future__ import annotations

from src.agents import WorkflowRunner
from src.domain.after_sales import AfterSalesService
from src.domain.after_sales.adapters import MemoryAdapter

from tests.unit.domain.after_sales.helpers import (  # noqa: F401  复用领域工厂
    baseline_service,
    make_order,
    service_with_policies,
)

DEFAULT_POLICIES = (("P-DAMAGED-FULL", ("damaged",), "1.00", 30),)

REQUEST_DAMAGED = "订单 ORD-1 商品破损，要求退款"


def make_runner(policies=DEFAULT_POLICIES, order=None):
    """构造带默认订单（ORD-1，实付 100.00，租户 T1）与政策的领域服务 + 工作流运行器。

    运行器只依赖 Port 后端：内存 AfterSalesService 经 MemoryAdapter 包装传入。
    """
    if policies:
        svc = service_with_policies(*policies)
    else:
        svc = AfterSalesService()
    if order is not None:
        svc._orders.clear()
        svc.seed_order(order)
    return svc, WorkflowRunner(MemoryAdapter(svc))


def approve_and_resume(runner, thread_id: str, operation_id: str):
    """授权人员提交通过决定并恢复工作流。"""
    runner.submit_decision(operation_id, "approved")
    return runner.resume(thread_id)
