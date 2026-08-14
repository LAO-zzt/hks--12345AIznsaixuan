# -*- coding: utf-8 -*-
"""
模块 3：实体与事件识别（entity_extractor.py）

从 content + subject + area 中提取：
- extracted_subject：核心主体
- extracted_event：核心事件
- extracted_area：核心区域

规则优先级：结构化字段 > 本地词典 > 后缀规则 > 正则 > 留空（不伪造）。
"""
import re

from utils.helpers import load_dict_lines

# 区域后缀（顺德常见街道/镇，及通用行政单位）
AREA_SUFFIX = ["街道", "镇", "乡", "村", "社区", "区"]
# 主体后缀规则
SUBJECT_SUFFIX = ["小区", "花园", "市场", "广场", "学校", "医院", "工业园", "公园",
                  "大厦", "商场", "步行街", "夜市", "烧烤店", "工地", "公寓", "苑",
                  "路口", "路", "街", "大道"]


def _match_area_from_text(text: str) -> str:
    """从文本中用正则提取“XX街道 / XX镇”等区域。"""
    if not text:
        return ""
    m = re.search(r"([\u4e00-\u9fa5]{1,4}(?:街道|镇))", text)
    return m.group(1) if m else ""


def _match_event(text: str, event_terms: list) -> str:
    """在文本中匹配事件词典，返回命中的最长事件词。"""
    if not text:
        return ""
    hit = ""
    for term in event_terms:
        if term and term in text and len(term) > len(hit):
            hit = term
    return hit


def _match_subject_from_text(text: str) -> str:
    """用后缀规则从文本中提取主体（如“XX小区”）。"""
    if not text:
        return ""
    best = ""
    for suf in SUBJECT_SUFFIX:
        # 匹配 1~12 个汉字 + 后缀
        pattern = r"([\u4e00-\u9fa5]{1,12}" + re.escape(suf) + r")"
        m = re.search(pattern, text)
        if m and len(m.group(1)) > len(best):
            best = m.group(1)
    return best


def extract_entities(df):
    """
    为每条工单提取主体/事件/区域三列。

    遵循“无法判断时留空，不得伪造”的原则。
    """
    event_terms = load_dict_lines("events.txt")

    subjects, events, areas = [], [], []
    for _, row in df.iterrows():
        content = str(row.get("normalized_content", "") or row.get("content", ""))
        raw_subject = str(row.get("subject", "") or "").strip()
        raw_area = str(row.get("area", "") or "").strip()

        # ---- 主体 ----
        if raw_subject:
            subj = raw_subject
        else:
            subj = _match_subject_from_text(content)
        subjects.append(subj)

        # ---- 事件 ----
        ev = _match_event(content, event_terms)
        events.append(ev)

        # ---- 区域 ----
        if raw_area:
            ar = raw_area
        else:
            ar = _match_area_from_text(content)
        areas.append(ar)

    df = df.copy()
    df["extracted_subject"] = subjects
    df["extracted_event"] = events
    df["extracted_area"] = areas
    return df
