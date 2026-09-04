"""日志配置：统一格式 + 输出前 PII 脱敏（阶段 0 最小落地）。

任何日志路径都不得泄露完整 PII（手机号 / 邮箱 / 身份证）。示例：

    configure_logging()
    logger = logging.getLogger("after_sales")
    logger.info("客户 %s 咨询订单 %s", "13812341234", "ORD-1")   # 落盘为 138****1234
"""
from __future__ import annotations

import logging

from .redact import redact_pii


class PIIRedactingFormatter(logging.Formatter):
    """在完整格式化（含 %s 参数拼接、时间戳等）之后对整行做 PII 脱敏。"""

    def format(self, record: logging.LogRecord) -> str:
        return redact_pii(super().format(record))


def configure_logging(
    level: int = logging.INFO,
    fmt: str = "%(asctime)s %(levelname)s %(name)s %(message)s",
) -> None:
    """配置根 logger（force=True 覆盖已有 basicConfig，便于测试与 CLI）。"""
    handler = logging.StreamHandler()
    handler.setFormatter(PIIRedactingFormatter(fmt=fmt))
    root = logging.getLogger()
    root.setLevel(level)
    root.handlers = [handler]
    root.propagate = False
