"""Agent 运行模式开关（V1.1 阶段 3.6）。

四种模式（README 需明确默认）：
- ``single_agent``：单 Agent LangGraph 工作流（默认）——意图/澄清/证据/审批恢复/unknown 对账，
  确定性领域服务 + PostgreSQL 事实源；
- ``multi_agent``：四角色只读多 Agent（Supervisor orchestration="four-role"：
  Triage → Evidence → Resolution → RiskReview），仅作实验/演示；A/B 无收益 → 默认不启用；
- ``offline_rule``：LLM 未配置时的离线规则模式（= 当前默认：无安全 Key / 不允许 Base URL 时
  自动走 OfflineRuleClient；本开关显式声明该降级路径，行为与 single_agent 一致）；
- ``llm``：真实 LLM 路径（可选）——仅当 ``OPSPILOT_LLM_API_KEY`` 与白名单允许的
  ``OPSPILOT_LLM_BASE_URL`` 均配置时可用；未配置 → 构造失败/拒绝，绝不静默直连。

边界（宪法第三/四/六条，所有模式相同）：
- 模式只影响"意图/澄清/证据编排"的决策来源；金额/资格/库存/权限/审批/最终状态永远由
  确定性领域服务裁决并从 PostgreSQL 重读，任何模式都不能绕过；
- LLM/多 Agent 输出不得直接写业务表或调用写工具；写操作只经 Agent Gateway → 领域服务
  + 人工审批；
- 多 Agent 证据不足 / 角色失败 / 超时 / 非法输出 → 停止或转人工，不猜测继续。
"""
from __future__ import annotations

from enum import Enum


class RuntimeMode(str, Enum):
    SINGLE_AGENT = "single_agent"      # 默认
    MULTI_AGENT = "multi_agent"        # 四角色只读多 Agent（实验）
    OFFLINE_RULE = "offline_rule"      # 无 LLM Key 的离线规则模式（= 当前默认行为）
    LLM = "llm"                        # 真实 LLM（仅显式配置 Key+白名单 Base URL）

    @classmethod
    def default(cls) -> "RuntimeMode":
        """当前项目定位的默认模式：single_agent（多 Agent A/B 无收益、真实 LLM 未接入）。"""
        return cls.SINGLE_AGENT

    @classmethod
    def from_str(cls, value: str) -> "RuntimeMode":
        try:
            return cls(value)
        except ValueError:
            raise ValueError(
                f"未知运行模式 {value!r}；可选 {[m.value for m in cls]}") from None


def resolve_mode(requested: str) -> RuntimeMode:
    """把请求模式解析为实际生效模式（LLM 未配置 → offline_rule，不静默直连）。"""
    mode = RuntimeMode.from_str(requested)
    if mode is RuntimeMode.LLM:
        from src.models import assert_safe_network, load_llm_settings
        try:
            settings = load_llm_settings()
            assert_safe_network(settings)      # Key 缺失 / URL 不在白名单 → 抛错
        except Exception:  # noqa: BLE001  未配置 Key/URL → 离线规则（不静默直连）
            return RuntimeMode.OFFLINE_RULE
        return RuntimeMode.LLM
    return mode
