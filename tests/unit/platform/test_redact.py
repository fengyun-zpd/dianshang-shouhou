"""PII 脱敏与日志脱敏测试（record 级验证，不依赖全局 handler / IO）。"""
import logging

from src.platform.logging_config import PIIRedactingFormatter, configure_logging
from src.platform.redact import redact_pii


def _record(msg: str, *args) -> logging.LogRecord:
    return logging.LogRecord("test", logging.INFO, "test_redact.py", 1, msg, args, None)


# ---------- 纯函数脱敏 ----------

def test_redact_phone():
    assert redact_pii("手机 13812341234 联系") == "手机 138****1234 联系"


def test_redact_email():
    assert redact_pii("邮箱 alice.wang@example.com") == "邮箱 a***@example.com"


def test_redact_id_card():
    assert redact_pii("身份证 110101199001011234") == "身份证 1101**********1234"


def test_redact_plain_text_unchanged():
    assert redact_pii("订单 ORD-1 正常") == "订单 ORD-1 正常"


def test_redact_short_numbers_safe():
    # 年份等短数字不应被误掩；多条记录同时脱敏
    text = "2026 年手机 13812341234 与邮箱 a@b.cn 均脱敏"
    out = redact_pii(text)
    assert "138****1234" in out
    assert "2026" in out


# ---------- 日志 formatter（完整格式化后整行脱敏） ----------

def test_formatter_redacts_rendered_message():
    fmt = PIIRedactingFormatter(fmt="%(message)s")
    out = fmt.format(_record("客户电话 %s 发起退款", "13812341234"))
    assert "13812341234" not in out
    assert "138****1234" in out


def test_formatter_redacts_email_with_default_fmt():
    fmt = PIIRedactingFormatter()
    out = fmt.format(_record("联系 %s 失败", "alice@example.com"))
    assert "alice@example.com" not in out
    assert "a***@example.com" in out


def test_configure_logging_sets_level_and_formatter():
    configure_logging(level=logging.WARNING)
    root = logging.getLogger()
    assert root.level == logging.WARNING
    assert len(root.handlers) == 1
    assert isinstance(root.handlers[0].formatter, PIIRedactingFormatter)
