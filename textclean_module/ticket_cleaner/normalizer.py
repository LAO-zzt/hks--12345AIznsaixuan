"""实体归一化 + Semantic Content + 质量评分。

主体归一：使用全局共享的 entity_aliases 表 + 内置同义词字典。
不允许过度归一：未匹配的保留 raw，confidence=low。
"""

from __future__ import annotations

import json
import os
from typing import Dict, List, Optional, Tuple

from ticket_cleaner.schema import CleanedTicket
from ticket_cleaner.storage import Storage


# ---------- 内置同义词字典（顺德区常见主体） ----------

DEFAULT_SYNONYMS: Dict[str, List[str]] = {
    "佛山市顺德区第一人民医院": [
        "顺德第一人民医院", "顺德一院", "顺德一医", "顺德区第一人民医院",
        "区第一人民医院", "第一人民医院",
    ],
    "佛山市顺德区中医院": [
        "顺德中医院", "区中医院", "中医院",
    ],
    "佛山市顺德区妇幼保健院": [
        "顺德妇幼保健院", "区妇幼保健院", "妇幼保健院",
    ],
    "佛山市顺德区容桂街道卫生院": [
        "容桂卫生院", "容桂街道卫生院",
    ],
}


class EntityNormalizer:
    """主体归一化。全局共享。"""

    def __init__(self, storage: Storage, synonyms: Optional[Dict[str, List[str]]] = None) -> None:
        self.storage = storage
        self.synonyms = synonyms if synonyms is not None else DEFAULT_SYNONYMS
        self._loaded = False
        # 内存缓存 alias -> (canonical, entity_id, confidence)
        self._cache: Dict[str, Tuple[str, str, float]] = {}
        self._entity_counter = 0

    def _ensure_loaded(self) -> None:
        if self._loaded:
            return
        # 把内置同义词写入DB（如果不存在）
        for canonical, aliases in self.synonyms.items():
            entity_id = self._gen_entity_id()
            self.storage.upsert_entity_alias(
                entity_id, canonical, canonical, "organization", 1.0, "builtin"
            )
            for alias in aliases:
                self.storage.upsert_entity_alias(
                    entity_id, canonical, alias, "organization", 0.9, "builtin"
                )
        self._loaded = True

    def _gen_entity_id(self) -> str:
        self._entity_counter += 1
        return f"ORG{self._entity_counter:05d}"

    def normalize(self, raw: str, entity_type: str = "organization"
                  ) -> Tuple[str, float]:
        """归一化。返回 (canonical, confidence)。

        - 精确匹配别名：confidence=0.9
        - 包含匹配：confidence=0.7
        - 未匹配：保留raw，confidence=0.3（low）
        不允许过度归一。
        """
        if not raw:
            return "", 0.0
        self._ensure_loaded()
        # 缓存命中
        if raw in self._cache:
            return self._cache[raw][0], self._cache[raw][2]

        # DB精确查找
        row = self.storage.lookup_entity_by_alias(raw, entity_type)
        if row:
            self._cache[raw] = (row["canonical_name"], row["entity_id"], row["confidence"])
            return row["canonical_name"], row["confidence"]

        # 内置字典包含匹配
        for canonical, aliases in self.synonyms.items():
            if raw == canonical:
                self._cache[raw] = (canonical, "", 1.0)
                return canonical, 1.0
            if raw in aliases:
                self._cache[raw] = (canonical, "", 0.9)
                return canonical, 0.9
            # 包含关系
            for alias in aliases:
                if alias in raw or raw in alias:
                    self._cache[raw] = (canonical, "", 0.7)
                    return canonical, 0.7

        # 未匹配：保留raw，confidence=low
        # 不主动归到"某医院"->"顺德第一人民医院"
        self._cache[raw] = (raw, "", 0.3)
        return raw, 0.3


# ---------- 地址归一化 ----------

def normalize_address(addr_parts: Dict[str, str]) -> str:
    """地址归一：组装为标准地址字符串。"""
    parts = []
    if addr_parts.get("district"):
        parts.append(addr_parts["district"])
    if addr_parts.get("town"):
        parts.append(addr_parts["town"])
    if addr_parts.get("community"):
        parts.append(addr_parts["community"])
    if addr_parts.get("road"):
        parts.append(addr_parts["road"])
    if addr_parts.get("building"):
        parts.append(addr_parts["building"])
    return "".join(parts)


# ---------- Semantic Content ----------

def build_semantic_content(t: CleanedTicket) -> str:
    """生成语义核心文本。

    格式：
        主体：XXX
        地点：XXX
        事件：XXX
        诉求：XXX
        时间：XXX
    """
    parts = []
    if t.organization_normalized:
        parts.append(f"主体：{t.organization_normalized}")
    if t.address_normalized:
        parts.append(f"地点：{t.address_normalized}")
    if t.event_type or t.event_detail:
        ev = t.event_type
        if t.event_detail and t.event_type and t.event_type not in t.event_detail:
            ev = f"{t.event_type}({t.event_detail})"
        elif t.event_detail:
            ev = t.event_detail
        parts.append(f"事件：{ev}")
    if t.request:
        parts.append(f"诉求：{t.request}")
    if t.time_start:
        # 截断到分钟
        ts = t.time_start[:16] if len(t.time_start) >= 16 else t.time_start
        parts.append(f"时间：{ts}")
    elif t.time_pattern:
        parts.append(f"时间：{t.time_pattern}")
    return "；".join(parts)


# ---------- 质量评分 ----------

def compute_quality_score(t: CleanedTicket, min_quality: float = 0.3) -> Tuple[float, bool, str]:
    """质量评分 + 是否可用于重复判断 + parse_status。

    评分维度（各占权重）：
        - 工单号存在 0.15
        - 内容存在 0.20
        - 时间有效 0.10
        - 主体识别 0.20
        - 地点识别 0.20
        - 事件识别 0.15
    """
    score = 0.0
    if t.ticket_no:
        score += 0.15
    if t.clean_content:
        score += 0.20
    if t.time_start or t.time_pattern:
        score += 0.10
    if t.organization_normalized:
        score += 0.20
    if t.address_normalized:
        score += 0.20
    if t.event_type:
        score += 0.15

    usable = score >= min_quality and bool(t.ticket_no) and bool(t.clean_content)
    parse_status = "success"
    if not t.ticket_no and not t.clean_content:
        parse_status = "failed"
    elif score < min_quality:
        parse_status = "partial"
    return score, usable, parse_status
