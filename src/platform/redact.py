"""PII 脱敏：客户联系方式与订单敏感字段默认脱敏（AGENTS.md 第五条）。

规则（保守原则：宁可多掩，不可漏）：
- 大陆手机号：138****1234；
- 邮箱：用户名首字符保留，其余掩为 ***（a***@example.com）；
- 18 位身份证号：前 4 后 4 保留，中间掩为 **********。
"""
from __future__ import annotations

import re

_PHONE_RE = re.compile(r"(?<!\d)(1\d{2})\d{4}(\d{4})(?!\d)")
_EMAIL_LOCAL_RE = re.compile(r"([A-Za-z0-9._%+-])[A-Za-z0-9._%+-]*(?=@)")
_ID_CARD_RE = re.compile(r"(?<!\d)(\d{4})\d{10}(\d{4})(?!\d)")


def redact_pii(text: str) -> str:
    """对文本中的手机号 / 邮箱 / 身份证号做脱敏；其余原样返回。"""
    text = _PHONE_RE.sub(r"\1****\2", text)
    text = _EMAIL_LOCAL_RE.sub(r"\1***", text)
    text = _ID_CARD_RE.sub(r"\1**********\2", text)
    return text
