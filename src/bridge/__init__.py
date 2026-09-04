"""阶段 6：Mule Agent Bridge（外部 Agent 网络适配器，ADR-003）。"""
from .bridge import BridgeExecutionError, MuleAgentBridge
from .models import (
    BridgeAction,
    BridgeEnvelope,
    BridgeIdentity,
    BridgeLogEntry,
    FORBIDDEN_ACTIONS,
    INBOUND_SCHEMAS,
    OUTBOUND_SCHEMAS,
    IdentityRegistry,
)

__all__ = [
    "BridgeAction",
    "BridgeEnvelope",
    "BridgeExecutionError",
    "BridgeIdentity",
    "BridgeLogEntry",
    "FORBIDDEN_ACTIONS",
    "INBOUND_SCHEMAS",
    "IdentityRegistry",
    "MuleAgentBridge",
    "OUTBOUND_SCHEMAS",
]
