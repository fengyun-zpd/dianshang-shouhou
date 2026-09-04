"""意图识别与缺参判断（V1 规则化实现；当前无 LLM 运行时）。

本模块只做确定性文本分类与字段抽取，不产出任何业务参数（金额等一律不解析）。
用户输入视为不可信数据：其中的指令/角色声明不影响流程（宪法第五条）。
"""
from __future__ import annotations

import re

from typing import Optional

# 订单号模式（合成数据 ORD-xxxx）；大小写不敏感
_ORDER_RE = re.compile(r"(ORD-\w+)", re.IGNORECASE)

# 意图关键词表（顺序敏感：先命中先得）
_INTENT_KEYWORDS: list[tuple[str, tuple[str, ...]]] = [
    ("refund", ("退款", "退钱", "退掉", "破损", "坏了", "碎了", "少件", "漏发", "质量问题", "退货退款", "瑕疵")),
    ("return", ("退货", "退回", "七天无理由", "不想要了")),
    ("exchange", ("换货", "换一个", "更换")),
    ("replace", ("补发", "重发", "重新发货", "少发")),
    ("upgrade", ("升级", "补差价", "换高级")),
]

_TAG_KEYWORDS: list[tuple[str, tuple[str, ...]]] = [
    ("damaged", ("破损", "坏了", "碎了", "磕碰", "瑕疵")),
    ("missing_item", ("少件", "漏发", "缺件", "少发", "没收到")),
    ("quality", ("质量问题", "故障", "失灵", "不工作")),
]


def extract_intent(request: str, order_id_hint: Optional[str] = None) -> dict:
    """从用户请求抽取 intent / order_id / reason_tags / missing_fields。

    返回仅含 AgentState 字段的字典；不做金额或状态推断。
    """
    text = request or ""
    intent: Optional[str] = None
    for name, kws in _INTENT_KEYWORDS:
        if any(k in text for k in kws):
            intent = name
            break
    if intent is None:
        intent = "unknown"

    order_id = order_id_hint
    if order_id is None:
        m = _ORDER_RE.search(text)
        if m:
            order_id = m.group(1).upper()

    tags: list[str] = []
    for tag, kws in _TAG_KEYWORDS:
        if any(k in text for k in kws):
            tags.append(tag)

    missing: list[str] = []
    # 仅对可推进的动作意图要求订单号/材料；unknown 一律转人工而非澄清缺单
    if intent in ("refund", "return", "exchange", "replace", "upgrade"):
        if order_id is None:
            missing.append("order_id")
    if intent == "refund" and not tags:
        missing.append("reason")  # 缺少问题描述/材料 → 无法匹配政策证据
    if not text.strip():
        missing.append("request")

    return {
        "intent": intent,
        "order_id": order_id,
        "reason_tags": tags,
        "missing_fields": missing,
        "user_request": request,
    }


def clarify_questions(missing_fields: list[str]) -> list[str]:
    """缺参 → 澄清问题（V1 模板话术）。"""
    q: list[str] = []
    if "order_id" in missing_fields:
        q.append("请提供订单号（形如 ORD-xxxx）")
    if "reason" in missing_fields:
        q.append("请描述商品问题（如破损 / 少件 / 质量故障），以便匹配售后政策")
    if "request" in missing_fields:
        q.append("请描述您的售后诉求")
    return q
