"""政策证据检索（阶段 3 RAG 本地基线）。

- PolicyDocument（租户内、版本化、启用开关）→ 分块 → EvidenceChunk（含 citation）；
- 关键词倒排 + 词频向量（VectorBackend 协议）→ RRF 混合检索；
- 引用校验：citation 指向存在、启用、版本有效且同租户的块；
- 提示注入防护（双向）：查询注入 → 拒绝检索；文档内容投毒 → 检索结果排除并记录；
- 无证据 → 返回空列表（上层据此转人工，不猜测）。

说明：检索结果只构成“证据引用与解释”，业务资格/金额判定仍以确定性领域服务为准。
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Optional, Sequence

from .vector import DictCosineVectorBackend, SparseVector, VectorBackend

_CJK_RUN = re.compile(r"[\u4e00-\u9fff]+")
_EN_WORD = re.compile(r"[a-z0-9]{2,}")


def tokenize(text: str) -> list[str]:
    """轻量分词：英文/数字词（小写）+ 连续汉字的二元组（bigram）。"""
    tokens: list[str] = []
    low = (text or "").lower()
    for w in _EN_WORD.findall(low):
        tokens.append(w)
    for run in _CJK_RUN.findall(text or ""):
        for i in range(len(run) - 1):
            tokens.append(run[i:i + 2])
    return tokens


# 注入模式（保守名单：命中即拒绝/排除；宁可误伤不可放行）
_INJECTION_PATTERNS: tuple[str, ...] = (
    "忽略", "忽略以上", "忽略之前", "不要管", "请忘记",
    "ignore previous", "ignore above", "ignore all", "disregard",
    "你现在是", "你是一个", "重新设定", "以上作废", "以上规则作废",
    "system prompt", "注入", "扮演",
)


class InjectionDetected(Exception):
    """检索请求或文档内容命中注入模式。"""

    def __init__(self, pattern: str, where: str):
        self.pattern = pattern
        self.where = where
        super().__init__(f"检测到提示注入（{where}，命中模式：{pattern}）")


def detect_injection(text: str) -> Optional[str]:
    """检测注入模式；命中返回命中模式，否则 None。"""
    low = (text or "").lower()
    for p in _INJECTION_PATTERNS:
        if p.lower() in low:
            return p
    return None


@dataclass(frozen=True)
class PolicyDocument:
    """政策文档（合成/参考文本；资格判定以对应 PolicyRule 为准）。"""
    policy_id: str
    tenant_id: str
    title: str
    content: str
    version: int = 1
    active: bool = True


@dataclass(frozen=True)
class EvidenceChunk:
    chunk_id: str
    policy_id: str
    tenant_id: str
    version: int
    seq: int
    text: str

    def citation(self) -> str:
        return f"{self.policy_id}@{self.version}#{self.seq}"


@dataclass(frozen=True)
class RetrievedEvidence:
    chunk: EvidenceChunk
    score: float
    keyword_hits: list[str] = field(default_factory=list)

    def citation(self) -> str:
        return self.chunk.citation()


def _chunk_text(content: str, max_len: int = 60) -> list[str]:
    """按句子拆分并按 max_len 合并（避免碎块过多）。"""
    sentences = re.split(r"(?<=[。！？；;])\s*|\n+", content or "")
    sentences = [s.strip() for s in sentences if s.strip()]
    chunks: list[str] = []
    buf = ""
    for s in sentences:
        if len(s) > max_len:  # 超长句强制切段
            for i in range(0, len(s), max_len):
                chunks.append(s[i:i + max_len])
            continue
        if buf and len(buf) + len(s) > max_len:
            chunks.append(buf)
            buf = s
        else:
            buf = (buf + s) if not buf else (buf + s)
    if buf:
        chunks.append(buf)
    return chunks or [content]


class PolicyStore:
    """政策文档存储与混合检索。"""

    def __init__(self, vector_backend: Optional[VectorBackend] = None):
        self._vector = vector_backend if vector_backend is not None else DictCosineVectorBackend()
        self._chunks: dict[str, EvidenceChunk] = {}          # chunk_id -> chunk
        self._chunk_vectors: dict[str, SparseVector] = {}    # chunk_id -> token freq
        self._keyword: dict[str, dict[str, int]] = {}        # token -> {chunk_id: count}
        # 租户是政策身份的一部分；相同 policy_id/version 可以安全存在于不同租户。
        self._docs: dict[tuple[str, str, int], PolicyDocument] = {}  # (tenant_id, policy_id, version) -> doc
        self._poisoned: list[str] = []                        # 被内容注入排除的 chunk_id

    # ---------- 写入（文档注册） ----------

    def register(self, doc: PolicyDocument) -> list[EvidenceChunk]:
        """注册文档并分块建索引；同 (policy_id, version) 幂等；停用文档不建索引。"""
        key = (doc.tenant_id, doc.policy_id, doc.version)
        if key in self._docs:
            if self._docs[key] != doc:
                raise ValueError(f"政策 {doc.tenant_id}/{doc.policy_id}@{doc.version} 内容冲突")
            return self._chunks_of(doc.tenant_id, doc.policy_id, doc.version)
        self._docs[key] = doc
        if not doc.active:
            return []  # 停用文档不进入检索（validate/search 不命中）
        created: list[EvidenceChunk] = []
        for seq, text in enumerate(_chunk_text(doc.content)):
            chunk = EvidenceChunk(
                chunk_id=f"{doc.tenant_id}:{doc.policy_id}:{doc.version}:c{seq}",
                policy_id=doc.policy_id, tenant_id=doc.tenant_id,
                version=doc.version, seq=seq, text=text,
            )
            self._chunks[chunk.chunk_id] = chunk
            vec = _frequency(tokenize(text))
            self._chunk_vectors[chunk.chunk_id] = vec
            self._vector.add(chunk.chunk_id, vec)
            for tok in vec:
                bucket = self._keyword.setdefault(tok, {})
                bucket[chunk.chunk_id] = bucket.get(chunk.chunk_id, 0) + 1
            created.append(chunk)
        return created

    def list_documents(self) -> list[PolicyDocument]:
        return list(self._docs.values())

    # ---------- 检索 ----------

    def search(self, tenant_id: str, query: str, top_k: int = 5) -> list[RetrievedEvidence]:
        """租户内混合检索（关键词 + 向量，RRF 融合）。

        - 查询注入 → 抛 InjectionDetected；
        - 内容投毒块被排除；
        - 无证据 → 返回空列表。
        """
        hit_pattern = detect_injection(query)
        if hit_pattern:
            raise InjectionDetected(hit_pattern, "query")
        q_tokens = tokenize(query)
        q_vec = _frequency(q_tokens)
        if not q_tokens:
            return []

        # 1) 关键词候选与得分
        kw_score: dict[str, int] = {}
        for tok in q_tokens:
            for chunk_id, count in self._keyword.get(tok, {}).items():
                if self._chunks[chunk_id].tenant_id == tenant_id:
                    kw_score[chunk_id] = kw_score.get(chunk_id, 0) + count

        # 2) 向量相似度（仅保留真实非零相似度，避免无关文档经 RRF 混入）
        vec_hits = {
            cid: score
            for cid, score in dict(
                self._vector.similarity(q_vec, limit=max(top_k * 3, len(self._chunks)))
            ).items()
            if score > 0.0
        }

        # 3) RRF 融合（仅对关键词命中与向量命中做融合）
        def _rank(items: Sequence[str]) -> dict[str, float]:
            rrf: dict[str, float] = {}
            for rank, cid in enumerate(items):
                rrf[cid] = rrf.get(cid, 0.0) + 1.0 / (60 + rank)
            return rrf

        kw_rank = _rank([cid for cid, _ in sorted(kw_score.items(), key=lambda kv: kv[1], reverse=True)])
        vec_rank = _rank([cid for cid, _ in sorted(vec_hits.items(), key=lambda kv: kv[1], reverse=True)])
        fused: dict[str, float] = {}
        for cid in set(kw_rank) | set(vec_rank):
            fused[cid] = kw_rank.get(cid, 0.0) + vec_rank.get(cid, 0.0)

        results: list[RetrievedEvidence] = []
        for cid, score in sorted(fused.items(), key=lambda kv: kv[1], reverse=True):
            chunk = self._chunks[cid]
            if chunk.tenant_id != tenant_id:
                continue
            if detect_injection(chunk.text):  # 内容投毒排除
                self._poisoned.append(cid)
                continue
            hits = [tok for tok in q_tokens if self._keyword.get(tok, {}).get(cid)]
            results.append(RetrievedEvidence(chunk=chunk, score=round(score, 6), keyword_hits=hits[:5]))
            if len(results) >= top_k:
                break
        return results

    # ---------- 引用校验 ----------

    def validate_citation(self, tenant_id: str, citation: str) -> Optional[EvidenceChunk]:
        """解析并校验 citation（policy@version#seq）存在、启用且租户一致。"""
        if "@" not in citation or "#" not in citation:
            return None
        left, _, rest = citation.partition("@")
        version_str, _, seq_str = rest.partition("#")
        try:
            version, seq = int(version_str), int(seq_str)
        except ValueError:
            return None
        doc = self._docs.get((tenant_id, left, version))
        if doc is None or not doc.active:
            return None
        chunk = self._chunks.get(f"{tenant_id}:{left}:{version}:c{seq}")
        if chunk is None:
            return None
        return chunk

    def poisoned_chunks(self) -> list[str]:
        return list(self._poisoned)

    # ---------- 内部 ----------

    def _chunks_of(self, tenant_id: str, policy_id: str, version: int) -> list[EvidenceChunk]:
        prefix = f"{tenant_id}:{policy_id}:{version}:c"
        return sorted(
            (c for cid, c in self._chunks.items() if cid.startswith(prefix)),
            key=lambda c: c.seq,
        )


def _frequency(tokens: Sequence[str]) -> SparseVector:
    freq: SparseVector = {}
    for t in tokens:
        freq[t] = freq.get(t, 0.0) + 1.0
    return freq
