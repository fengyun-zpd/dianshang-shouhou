"""Public-response redaction for untrusted user text and error payloads."""
from __future__ import annotations

import re
from typing import Any

_PHONE = re.compile(r"(?<!\d)1[3-9]\d{9}(?!\d)")
_EMAIL = re.compile(r"(?i)(?<![\w.+-])[\w.+-]+@[\w-]+(?:\.[\w-]+)+(?![\w.-])")
_CN_ID = re.compile(r"(?<![0-9A-Za-z])\d{17}[0-9Xx](?![0-9A-Za-z])")


def redact_text(value: str) -> str:
    """Mask common contact/identity values without changing business IDs."""
    value = _PHONE.sub("[REDACTED_PHONE]", value)
    value = _EMAIL.sub("[REDACTED_EMAIL]", value)
    return _CN_ID.sub("[REDACTED_ID]", value)


def redact_value(value: Any) -> Any:
    """Recursively redact strings in public nested views and errors."""
    if isinstance(value, str):
        return redact_text(value)
    if isinstance(value, dict):
        return {key: redact_value(item) for key, item in value.items()}
    if isinstance(value, list):
        return [redact_value(item) for item in value]
    if isinstance(value, tuple):
        return [redact_value(item) for item in value]
    return value
