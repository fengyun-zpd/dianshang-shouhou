"""阶段 3 RAG：政策证据检索（关键词 + 可插拔向量；本地基线）。"""
from .store import (
    EvidenceChunk,
    InjectionDetected,
    PolicyDocument,
    PolicyStore,
    RetrievedEvidence,
    detect_injection,
    tokenize,
)
from .vector import DictCosineVectorBackend, VectorBackend

__all__ = [
    "DictCosineVectorBackend",
    "EvidenceChunk",
    "InjectionDetected",
    "PolicyDocument",
    "PolicyStore",
    "RetrievedEvidence",
    "VectorBackend",
    "detect_injection",
    "tokenize",
]
