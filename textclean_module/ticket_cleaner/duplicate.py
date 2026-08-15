"""重复识别引擎（简化版）。

使用清洗结果 + Embedding 综合判断。
不做"一个字段相同就判定重复"，而是综合主体+地点+事件+诉求+时间+语义。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

import numpy as np

from ticket_cleaner.embedding import (
    deserialize_embedding,
    cosine_similarity,
)
from ticket_cleaner.storage import Storage


@dataclass
class DuplicateCandidate:
    """重复候选对。"""
    ticket_no_a: str
    ticket_no_b: str
    similarity: float
    duplicate: bool
    reason: str = ""
    details: Dict[str, Any] = field(default_factory=dict)


class DuplicateDetector:
    """重复识别引擎。

    判断逻辑（综合）：
    1. 必须都 is_usable_for_duplicate=True
    2. embedding 相似度 >= threshold（默认0.85）
    3. 综合特征加权得分：
       - organization_normalized 相同 +0.3
       - address_normalized 相同 +0.3
       - event_type 相同 +0.2
       - request 相同 +0.1
       - time_start 相同 +0.1
    4. 总分 >= 0.7 判为重复
    保留"相似但非重复"样本。
    """

    def __init__(self, storage: Storage, similarity_threshold: float = 0.85,
                 duplicate_threshold: float = 0.7) -> None:
        self.storage = storage
        self.similarity_threshold = similarity_threshold
        self.duplicate_threshold = duplicate_threshold

    def find_candidates(self, job_id: str, top_k: int = 100,
                        max_pairs: int = 1000) -> List[DuplicateCandidate]:
        """从已清洗的工单中找重复候选。"""
        # 读取所有 usable 的工单
        rows = self._load_usable(job_id)
        if len(rows) < 2:
            return []
        # 计算所有pair的embedding相似度
        embeddings = []
        ticket_nos = []
        for r in rows:
            if r["embedding"]:
                try:
                    vec = deserialize_embedding(r["embedding"])
                    embeddings.append(vec)
                    ticket_nos.append(r["ticket_no"])
                except Exception:
                    continue
        if len(embeddings) < 2:
            return []

        emb_arr = np.array(embeddings)
        # 相似度矩阵
        sim_matrix = emb_arr @ emb_arr.T

        # 取上三角（不含对角线）
        n = len(ticket_nos)
        pairs = []
        for i in range(n):
            # 取与i最相似的前 top_k
            sims = sim_matrix[i]
            # 排除自身
            sims_idx = np.argsort(-sims)
            count = 0
            for j in sims_idx:
                if j == i:
                    continue
                if sims[j] < self.similarity_threshold:
                    break
                a, b = ticket_nos[i], ticket_nos[j]
                # 避免重复对
                key = tuple(sorted([a, b]))
                pairs.append((sims[j], a, b, i, j))
                count += 1
                if count >= top_k:
                    break
            if len(pairs) >= max_pairs:
                break

        # 去重
        seen = set()
        candidates = []
        for sim, a, b, i, j in sorted(pairs, reverse=True):
            key = tuple(sorted([a, b]))
            if key in seen:
                continue
            seen.add(key)
            row_a = next(r for r in rows if r["ticket_no"] == a)
            row_b = next(r for r in rows if r["ticket_no"] == b)
            dup, score, reason = self._judge(row_a, row_b, sim)
            candidates.append(DuplicateCandidate(
                ticket_no_a=a, ticket_no_b=b,
                similarity=float(sim), duplicate=dup,
                reason=reason,
                details={
                    "feature_score": round(score, 4),
                    "org_same": row_a["organization_normalized"] == row_b["organization_normalized"] and bool(row_a["organization_normalized"]),
                    "addr_same": row_a["address_normalized"] == row_b["address_normalized"] and bool(row_a["address_normalized"]),
                    "event_same": row_a["event_type"] == row_b["event_type"] and bool(row_a["event_type"]),
                    "request_same": row_a["request"] == row_b["request"] and bool(row_a["request"]),
                },
            ))
            if len(candidates) >= max_pairs:
                break
        return candidates

    def _judge(self, a: Dict[str, Any], b: Dict[str, Any],
               sim: float) -> Tuple[bool, float, str]:
        """综合判断是否重复。"""
        score = 0.0
        reasons = []
        if a["organization_normalized"] and a["organization_normalized"] == b["organization_normalized"]:
            score += 0.3
            reasons.append("主体相同")
        if a["address_normalized"] and a["address_normalized"] == b["address_normalized"]:
            score += 0.3
            reasons.append("地点相同")
        if a["event_type"] and a["event_type"] == b["event_type"]:
            score += 0.2
            reasons.append("事件相同")
        if a["request"] and a["request"] == b["request"]:
            score += 0.1
            reasons.append("诉求相同")
        if a["time_start"] and a["time_start"] == b["time_start"]:
            score += 0.1
            reasons.append("时间相同")
        # 语义相似度也作为一项
        if sim >= 0.9:
            score += 0.1
            reasons.append(f"语义高度相似({sim:.2f})")
        duplicate = score >= self.duplicate_threshold
        return duplicate, score, "+".join(reasons)

    def _load_usable(self, job_id: str) -> List[Dict[str, Any]]:
        # 简单全量加载（生产环境可分页）
        all_rows = []
        offset = 0
        while True:
            rows = self.storage.get_cleaned(job_id, limit=5000, offset=offset)
            if not rows:
                break
            all_rows.extend([r for r in rows if r.get("is_usable_for_duplicate")])
            offset += 5000
        return all_rows
