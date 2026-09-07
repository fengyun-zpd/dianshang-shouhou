"""面向工作台的隔离 Agent Lab。

该模块只使用每次请求新建的固定种子内存后端：它用于展示实际的单 Agent /
Supervisor 只读证据轨迹与本地 RAG 检索，不读取或修改 API 正在服务的业务事实。
"""
from __future__ import annotations

from decimal import Decimal

from src.agents import SupervisorRunner, WorkflowRunner
from src.domain.after_sales import (
    AfterSalesService,
    Order,
    OrderItem,
    OrderStatus,
    PolicyRule,
    RequestType,
)
from src.domain.after_sales.adapters import MemoryAdapter
from src.rag import InjectionDetected, PolicyDocument, PolicyStore
from src.platform.redact import redact_pii


_REQUEST = "订单 ORD-LAB-1001 商品破损，要求退款"
_POLICY_ID = "P-DAMAGED-FULL"


def _service() -> AfterSalesService:
    service = AfterSalesService()
    service.seed_order(Order(
        order_id="ORD-LAB-1001", tenant_id="T1", customer_id="C1",
        status=OrderStatus.DELIVERED, paid_amount=Decimal("200.00"),
        items=[OrderItem(sku="SKU-LAB-1", name="合成演示商品", quantity=1,
                         unit_price=Decimal("200.00"))],
        days_since_sign=2,
    ))
    service.seed_policy(PolicyRule(
        policy_id=_POLICY_ID, tenant_id="T1", request_type=RequestType.REFUND,
        reason_tags=("damaged",), window_days=30, refund_ratio=Decimal("1.00"),
    ))
    return service


def _policy_store() -> PolicyStore:
    store = PolicyStore()
    store.register(PolicyDocument(
        policy_id=_POLICY_ID, tenant_id="T1", title="破损退款政策",
        content="签收后 30 天内，商品破损可申请全额退款。客户需提供破损照片作为凭证。",
        version=1,
    ))
    return store


def run_trace(mode: str) -> dict:
    """运行到审批 interrupt 为止；沙箱内不执行退款副作用。"""
    service = _service()
    store = _policy_store()
    backend = MemoryAdapter(service)
    if mode == "multi_agent":
        runner = SupervisorRunner(backend, policy_store=store, orchestration="four-role")
        mode_label = "四角色 Supervisor（实验）"
    else:
        runner = WorkflowRunner(backend, policy_store=store)
        mode_label = "单 Agent（默认）"

    # 解析器当前的合成订单号规则只覆盖一个连字符段；Lab 明确给出
    # 固定示例的订单提示，不扩张主流程的输入解析语义。
    result = runner.start("T1", _REQUEST, thread_id=f"ui-{mode}",
                          order_id_hint="ORD-LAB-1001")
    state = result.state or {}
    supervisor = (state.get("order_summary") or {}).get("supervisor") or {}
    raw_traces = list(supervisor.get("traces") or [])
    if not raw_traces:
        raw_traces = [{
            "agent_name": "workflow-agent",
            "role": "意图理解、证据汇集与动作草稿解释",
            "status": "ok" if result.waiting_approval else "error",
            "input_summary": _REQUEST,
            "output_summary": "已汇集订单与政策证据，生成待审批退款草稿。",
            "tool_calls": ["get_order", "retrieve_policy"],
            "citations": list((state.get("order_summary") or {}).get("policy_citations") or []),
            "reject_reason": None,
            "duration_ms": None,
        }]

    traces = [{
        "agent_name": item.get("agent_name"),
        "role": item.get("role"),
        "status": item.get("status"),
        "input_summary": item.get("input_summary"),
        "output_summary": item.get("output_summary"),
        "tool_calls": list(item.get("tool_calls") or []),
        "citations": list(item.get("citations") or []),
        "reject_reason": item.get("reject_reason"),
        "duration_ms": item.get("duration_ms"),
    } for item in raw_traces]
    draft = state.get("action_draft") or {}
    return {
        "mode": mode,
        "mode_label": mode_label,
        "request": _REQUEST,
        "outcome": result.outcome,
        "waiting_approval": result.waiting_approval,
        "draft_amount": draft.get("amount"),
        "side_effect": False,
        "isolation": "固定种子内存沙箱；不读取或修改当前业务工单",
        "traces": traces,
    }


def retrieve_policy(query: str) -> dict:
    """运行真实本地混合检索；检索失败时明确返回原因而非编造证据。"""
    store = _policy_store()
    try:
        matches = store.search("T1", query, top_k=3)
    except InjectionDetected as error:
        return {
            "status": "blocked",
            "reason": "INJECTION_DETECTED",
            "message": "检测到提示注入模式，已拒绝检索。",
            "pattern": error.pattern,
            "results": [],
        }
    return {
        "status": "ok" if matches else "no_evidence",
        "reason": None if matches else "NO_EVIDENCE",
        "message": "" if matches else "没有足够证据，工作流应转人工而非猜测。",
        "results": [{
            "citation": item.citation(),
            "text": item.chunk.text,
            "score": item.score,
            "keyword_hits": item.keyword_hits,
            "citation_valid": store.validate_citation("T1", item.citation()) is not None,
        } for item in matches],
    }


def run_boundary_scenario(name: str) -> dict:
    """运行一个固定种子的边界剧本，返回可供工作台展示的证据摘要。

    这些剧本只创建隔离的内存工作流，不读取 API 主服务，也不执行退款副作用。
    业务错误码与工作流结果原样保留，便于面试时说明 Agent 如何安全停止。
    """
    if name == "clarify":
        runner = WorkflowRunner(MemoryAdapter(_service()), policy_store=_policy_store())
        result = runner.start("T1", "我要退款", thread_id="ui-boundary-clarify")
        return {
            "scenario": name,
            "title": "缺少关键信息 → 澄清",
            "outcome": result.outcome or "waiting_clarify",
            "error_code": result.error_code,
            "waiting_clarify": result.waiting_clarify,
            "questions": list((result.interrupt_value or {}).get("questions") or []),
            "side_effect": False,
            "evidence": "未创建工单、未生成退款草稿，先补充订单号和问题描述",
        }

    if name == "no_evidence":
        # 订单存在，但当前问题与现有破损政策不匹配；RAG 不能替代领域资格判断。
        empty_store = PolicyStore()
        runner = WorkflowRunner(MemoryAdapter(_service()), policy_store=empty_store)
        result = runner.start(
            "T1", "订单 ORD-LAB-1001 商品质量问题，要求退款",
            thread_id="ui-boundary-no-evidence", order_id_hint="ORD-LAB-1001",
        )
        return {
            "scenario": name,
            "title": "无适用政策 → 转人工",
            "outcome": result.outcome,
            "error_code": result.error_code,
            "waiting_clarify": result.waiting_clarify,
            "side_effect": False,
            "evidence": "领域服务返回 POLICY_NOT_FOUND，未猜测金额或资格",
        }

    if name == "cross_tenant":
        runner = WorkflowRunner(MemoryAdapter(_service()), policy_store=_policy_store())
        result = runner.start(
            "T2", _REQUEST, thread_id="ui-boundary-cross-tenant",
            order_id_hint="ORD-LAB-1001",
        )
        return {
            "scenario": name,
            "title": "跨租户订单 → 拒绝",
            "outcome": result.outcome,
            "error_code": result.error_code,
            "waiting_clarify": result.waiting_clarify,
            "side_effect": False,
            "evidence": "T2 无权读取 T1 订单，未创建工单、未写入审计",
        }

    if name == "pii":
        original = "客户手机号 13800138000，邮箱 alice@example.com，身份证 110101199001011234"
        masked = redact_pii(original)
        return {
            "scenario": name,
            "title": "敏感信息 → 默认脱敏",
            "outcome": "redacted",
            "error_code": None,
            "waiting_clarify": False,
            "side_effect": False,
            "before": original,
            "after": masked,
            "evidence": "日志与模型上下文只使用脱敏文本，业务事实仍保留在受控数据层",
        }

    raise ValueError(f"未知边界剧本: {name}")
