"""向量后端协议（阶段 3 RAG，可插拔）。

pgvector / Milvus 等外部向量库遵循 VectorBackend 协议接入（规划中，不阻塞核心闭环）。
当前实现 DictCosineVectorBackend：词频稀疏向量的字典余弦（确定性、无外部依赖）。
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Optional

SparseVector = dict[str, float]


class VectorBackend(ABC):
    """向量相似度后端协议。"""

    @abstractmethod
    def add(self, key: str, vector: SparseVector) -> None:
        """注册 key 对应的向量。"""

    @abstractmethod
    def similarity(self, vector: SparseVector, limit: int = 10) -> list[tuple[str, float]]:
        """返回与 query 最相似的 (key, score) 列表，score 越大越相似。"""

    @abstractmethod
    def clear(self) -> None:
        """清空全部向量（测试用）。"""


class DictCosineVectorBackend(VectorBackend):
    """进程内字典余弦实现（无第三方依赖；外部向量库接入时可替换）。"""

    def __init__(self) -> None:
        self._vectors: dict[str, SparseVector] = {}

    def add(self, key: str, vector: SparseVector) -> None:
        self._vectors[key] = dict(vector)

    def similarity(self, vector: SparseVector, limit: int = 10) -> list[tuple[str, float]]:
        scored = [(key, self._cosine(vector, v)) for key, v in self._vectors.items()]
        scored.sort(key=lambda kv: kv[1], reverse=True)
        return [(k, round(s, 6)) for k, s in scored[:limit]]

    def clear(self) -> None:
        self._vectors.clear()

    @staticmethod
    def _cosine(a: SparseVector, b: SparseVector) -> float:
        if not a or not b:
            return 0.0
        dot = 0.0
        for k, va in a.items():
            vb = b.get(k)
            if vb is not None:
                dot += va * vb
        if dot == 0:
            return 0.0
        norm = (sum(v * v for v in a.values()) ** 0.5) * (sum(v * v for v in b.values()) ** 0.5)
        return dot / norm if norm else 0.0
