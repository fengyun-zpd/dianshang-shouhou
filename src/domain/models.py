"""确定性领域服务的共享规约与金额校验（被 after_sales 主实现只读复用）。

本模块只包含纯数据与校验，不含任何 LLM 逻辑。金额统一使用 Decimal，避免浮点误差
导致的多退 / 少退。

- `Role`：参与者角色枚举（单一来源）。`src/domain/after_sales/models.py`、
  `src/api/deps.py`、`src/agents/`、`src/platform/`（tooling/reliability）等只读复用；
- `parse_money` / `CENT`：金额规约（分精度、禁止 float），被 after_sales 的 Order
  与领域服务复用。

历史：早期退款最小闭环（RefundService 及 RefundStatus/ErrorCode/DomainError/旧命令等
模型）已随 V1 收口删除，本模块不再保留其类型；主业务实现见 `src/domain/after_sales/`。
"""
from __future__ import annotations

from decimal import Decimal, ROUND_HALF_UP
from enum import Enum

CENT = Decimal("0.01")


def parse_money(value) -> Decimal:
    """将输入规约为精确到分的 Decimal。

    - 禁止 float：float 可能已含精度损失（如 0.1 + 0.2）。
    - 小数位超过两位直接报错，防止「多退 / 少退一分钱」。
    """
    if isinstance(value, float):
        raise ValueError("金额禁止使用 float，请传 str 或 Decimal")
    amount = Decimal(str(value))
    exponent = amount.as_tuple().exponent
    if isinstance(exponent, int) and exponent < -2:
        raise ValueError(f"金额最多精确到分，收到 {value!r}")
    return amount.quantize(CENT, rounding=ROUND_HALF_UP)


class Role(str, Enum):
    """参与者角色，决定可执行的操作（对齐宪法第三条职责分离）。"""
    CUSTOMER = "customer"   # 客户：只能发起诉求，不能操作审批或执行
    AGENT = "agent"         # Agent：只能创建草稿并提交审批，不能放行
    APPROVER = "approver"   # 授权人员：触发退款 / 补发 / 升级的最终批准
    SYSTEM = "system"       # 领域服务自身：执行已批准动作
