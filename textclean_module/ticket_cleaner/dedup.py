"""诉求去重与合并。

对同一人的同一诉求或完全一致的诉求进行合并。
支持规则匹配、Embedding语义相似度匹配。
"""

from __future__ import annotations

import re
from typing import Dict, List, Optional, Tuple
from difflib import SequenceMatcher

import numpy as np

from ticket_cleaner.embedding import deserialize_embedding, cosine_similarity


def normalize_request(request: str) -> str:
    """标准化诉求文本，去除多余空格和标点。"""
    if not request:
        return ""
    # 去除首尾空白
    s = request.strip()
    # 统一标点
    s = s.replace("，", ",").replace("。", ".").replace("！", "!").replace("？", "?")
    # 去除多余空格
    s = re.sub(r"\s+", " ", s)
    # 去除末尾标点
    s = s.rstrip(".,!?;。！？；")
    return s.lower()


def compute_text_similarity(text1: str, text2: str) -> float:
    """计算两段文本的相似度（0-1之间）。"""
    if not text1 or not text2:
        return 0.0
    # 使用 SequenceMatcher 计算相似度
    return SequenceMatcher(None, text1, text2).ratio()


def compute_embedding_similarity(emb1: bytes, emb2: bytes) -> float:
    """计算两个Embedding向量的余弦相似度。"""
    if not emb1 or not emb2:
        return 0.0
    try:
        vec1 = deserialize_embedding(emb1)
        vec2 = deserialize_embedding(emb2)
        return cosine_similarity(vec1, vec2)
    except Exception:
        return 0.0


def compute_request_similarity(req1: str, req2: str, 
                               emb1: Optional[bytes] = None, 
                               emb2: Optional[bytes] = None,
                               embedding_weight: float = 0.6) -> float:
    """计算两个诉求的相似度。

    结合文本匹配和Embedding语义相似度。
    
    Args:
        req1, req2: 诉求文本
        emb1, emb2: Embedding向量（可选）
        embedding_weight: Embedding权重（默认0.6，文本权重0.4）
    
    Returns:
        综合相似度（0-1）
    """
    norm1 = normalize_request(req1)
    norm2 = normalize_request(req2)

    if not norm1 or not norm2:
        return 0.0

    # 完全相同
    if norm1 == norm2:
        return 1.0

    # 包含关系（一个包含另一个）
    if norm1 in norm2 or norm2 in norm1:
        return 0.9

    # 计算文本相似度
    text_sim = compute_text_similarity(norm1, norm2)

    # 如果有Embedding，计算语义相似度
    if emb1 and emb2:
        emb_sim = compute_embedding_similarity(emb1, emb2)
        # 加权组合
        return text_sim * (1 - embedding_weight) + emb_sim * embedding_weight
    
    # 没有Embedding，只用文本相似度
    return text_sim


def deduplicate_tickets(tickets: List[Dict], similarity_threshold: float = 0.85) -> List[Dict]:
    """对工单进行诉求去重。

    Args:
        tickets: 工单列表，每个工单是字典
        similarity_threshold: 相似度阈值，超过此值的工单会被合并

    Returns:
        去重后的工单列表，每个工单包含 duplicate_count 和 duplicate_tickets 字段
    """
    if not tickets:
        return []

    # 分组：相似的工单放在一起
    groups = []  # List of (representative_ticket, [similar_tickets])

    for ticket in tickets:
        request = ticket.get('request', '')
        if not request:
            # 没有诉求的工单独独一组
            groups.append((ticket, []))
            continue

        merged = False
        for i, (rep, similar) in enumerate(groups):
            rep_request = rep.get('request', '')
            if not rep_request:
                continue

            # 计算相似度（使用Embedding）
            sim = compute_request_similarity(
                request, rep_request,
                ticket.get('embedding'), rep.get('embedding')
            )
            if sim >= similarity_threshold:
                similar.append(ticket)
                merged = True
                break

        if not merged:
            groups.append((ticket, []))

    # 合并每组工单
    result = []
    for rep, similar in groups:
        merged_ticket = rep.copy()
        merged_ticket['duplicate_count'] = len(similar) + 1  # 包括自己
        merged_ticket['duplicate_tickets'] = [t.get('ticket_no') for t in similar]

        # 如果有重复，合并诉求文本（取最长的）
        if similar:
            all_requests = [rep.get('request', '')] + [t.get('request', '') for t in similar]
            merged_ticket['request'] = max(all_requests, key=len)

            # 合并语义内容（取最长的）
            all_semantic = [rep.get('semantic_content', '')] + [t.get('semantic_content', '') for t in similar]
            merged_ticket['semantic_content'] = max(all_semantic, key=len)

        result.append(merged_ticket)

    return result


def group_by_person_and_request(tickets: List[Dict]) -> Dict[str, List[Dict]]:
    """按人物+诉求分组。

    Returns:
        字典，key 是 "person|request"，value 是工单列表
    """
    groups: Dict[str, List[Dict]] = {}

    for ticket in tickets:
        person = ticket.get('person_normalized', '') or ticket.get('person_raw', '')
        request = normalize_request(ticket.get('request', ''))

        if not person or not request:
            continue

        key = f"{person}|{request}"
        if key not in groups:
            groups[key] = []
        groups[key].append(ticket)

    return groups


def merge_same_person_same_request(tickets: List[Dict], similarity_threshold: float = 0.9) -> List[Dict]:
    """合并同一人的同一诉求。

    Args:
        tickets: 工单列表
        similarity_threshold: 诉求相似度阈值

    Returns:
        合并后的工单列表
    """
    # 先按人物+诉求分组
    person_req_groups = group_by_person_and_request(tickets)

    result = []
    processed_ticket_nos = set()

    for key, group_tickets in person_req_groups.items():
        if len(group_tickets) == 1:
            # 单条工单，直接加入
            ticket = group_tickets[0]
            if ticket.get('ticket_no') not in processed_ticket_nos:
                result.append(ticket)
                processed_ticket_nos.add(ticket.get('ticket_no'))
        else:
            # 多条工单，进一步按诉求相似度合并
            sub_groups = []
            for ticket in group_tickets:
                request = ticket.get('request', '')
                merged = False
                for sub_group in sub_groups:
                    rep_request = sub_group[0].get('request', '')
                    sim = compute_request_similarity(
                        request, rep_request,
                        ticket.get('embedding'), sub_group[0].get('embedding')
                    )
                    if sim >= similarity_threshold:
                        sub_group.append(ticket)
                        merged = True
                        break
                if not merged:
                    sub_groups.append([ticket])

            # 合并每个子组
            for sub_group in sub_groups:
                rep = sub_group[0]
                merged_ticket = rep.copy()
                merged_ticket['duplicate_count'] = len(sub_group)
                merged_ticket['duplicate_tickets'] = [t.get('ticket_no') for t in sub_group[1:]]

                # 合并诉求文本
                all_requests = [t.get('request', '') for t in sub_group]
                merged_ticket['request'] = max(all_requests, key=len)

                # 合并语义内容
                all_semantic = [t.get('semantic_content', '') for t in sub_group]
                merged_ticket['semantic_content'] = max(all_semantic, key=len)

                result.append(merged_ticket)
                for t in sub_group:
                    processed_ticket_nos.add(t.get('ticket_no'))

    # 加入未被分组的工单（没有人物或诉求的）
    for ticket in tickets:
        if ticket.get('ticket_no') not in processed_ticket_nos:
            result.append(ticket)
            processed_ticket_nos.add(ticket.get('ticket_no'))

    return result
