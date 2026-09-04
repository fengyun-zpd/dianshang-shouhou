"""全局测试夹具（最小化，不依赖任何领域实体）。

提供固定随机种子（默认 42）与合成数据辅助。所有演示/测试数据均为固定种子
合成数据，不代表真实企业收益（AGENTS.md 第二条）。
"""
from __future__ import annotations

import random

import pytest

FIXED_SEED = 42


@pytest.fixture
def fixed_seed() -> int:
    """每个测试开始时重置随机种子，保证可复现。"""
    random.seed(FIXED_SEED)
    return FIXED_SEED


def sample_money() -> str:
    """生成"分"级金额字符串（测试用，避免 float 精度问题）。"""
    cents = random.randint(1, 100_000)
    return f"{cents // 100}.{cents % 100:02d}"


def make_id(prefix: str) -> str:
    """生成确定性随机 id，如 make_id("ORD") -> 'ORD-12345'。"""
    return f"{prefix}-{random.randint(10_000, 99_999)}"
