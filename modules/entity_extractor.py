# -*- coding: utf-8 -*-
"""
模块 3：实体与事件识别（entity_extractor.py）

从 content + title + subject + area 中提取：
- extracted_subject：核心主体
- extracted_event：核心事件
- extracted_area：核心区域

规则优先级：结构化字段 > 本地词典 > 后缀规则 > 正则 > 留空（不伪造）。
全部采用向量化实现，十万级数据可在数十秒内完成。
"""
import re

import pandas as pd

from utils.helpers import load_dict_lines

# 主体后缀规则
SUBJECT_SUFFIX = ["小区", "花园", "市场", "广场", "学校", "医院", "工业园", "公园",
                  "大厦", "商场", "步行街", "夜市", "烧烤店", "工地", "公寓", "苑",
                  "路口", "大道"]

# 通用区域正则（词典未命中时的兜底）
_GENERIC_AREA_RE = r"([\u4e00-\u9fa5]{1,4}(?:街道|镇))"
# 主体后缀联合正则
_SUBJECT_RE = re.compile(
    r"([\u4e00-\u9fa5]{1,12}(?:" + "|".join(SUBJECT_SUFFIX) + r"))")

_AREA_TERMS = None
_AREA_DICT_RE = None


def _area_patterns():
    """加载区域词典并编译联合正则（长词优先，缓存结果）。"""
    global _AREA_TERMS, _AREA_DICT_RE
    if _AREA_DICT_RE is None:
        _AREA_TERMS = sorted(
            [t for t in load_dict_lines("areas.txt") if t], key=len, reverse=True)
        if _AREA_TERMS:
            _AREA_DICT_RE = re.compile("(" + "|".join(map(re.escape, _AREA_TERMS)) + ")")
        else:
            _AREA_DICT_RE = False
    return _AREA_DICT_RE


def _match_event_vec(texts: pd.Series, event_terms: list) -> pd.Series:
    """
    向量化事件词典匹配：长词优先，每条取首个命中的最长事件词。

    返回与 texts 等长的 Series（未命中为空串）。
    """
    res = pd.Series([""] * len(texts), index=texts.index, dtype=object)
    for term in sorted(event_terms, key=len, reverse=True):
        if not term:
            continue
        mask = texts.str.contains(term, na=False) & (res == "")
        if mask.any():
            res[mask] = term
    return res


def _match_area_vec(texts: pd.Series) -> pd.Series:
    """向量化区域提取：先区域词典，再通用“XX街道/XX镇”正则。"""
    res = pd.Series([""] * len(texts), index=texts.index, dtype=object)
    dict_re = _area_patterns()
    if dict_re:
        hit = texts.str.extract(dict_re)[0].fillna("")
        res = hit
    # 词典未命中的行走通用正则
    miss = res == ""
    if miss.any():
        generic = texts[miss].str.extract(_GENERIC_AREA_RE)[0].fillna("")
        res[miss] = generic.values
    return res


def extract_entities(df):
    """
    为每条工单提取主体/事件/区域三列（向量化，支持十万级数据）。

    遵循“无法判断时留空，不得伪造”的原则。
    """
    event_terms = load_dict_lines("events.txt")
    df = df.copy()

    content = df.get("normalized_content", df.get("content", pd.Series([""] * len(df), index=df.index))).fillna("").astype(str)
    title = df.get("normalized_title", df.get("title", pd.Series([""] * len(df), index=df.index))).fillna("").astype(str)

    # ---- 主体：结构化字段优先，其次后缀规则（向量化） ----
    raw_subject = df.get("subject", pd.Series([""] * len(df), index=df.index)).fillna("").astype(str).str.strip()
    subj_from_text = content.str.extract(_SUBJECT_RE)[0].fillna("")
    df["extracted_subject"] = raw_subject.where(raw_subject != "", subj_from_text)

    # ---- 事件：词典匹配（标题优先，其次内容） ----
    ev_from_title = _match_event_vec(title, event_terms)
    ev_from_content = _match_event_vec(content, event_terms)
    df["extracted_event"] = ev_from_title.where(ev_from_title != "", ev_from_content)

    # ---- 区域：结构化字段优先，其次内容抽取，最后标题抽取 ----
    raw_area = df.get("area", pd.Series([""] * len(df), index=df.index)).fillna("").astype(str).str.strip()
    area_from_content = _match_area_vec(content)
    area_from_title = _match_area_vec(title)
    area = area_from_content.where(area_from_content != "", area_from_title)
    df["extracted_area"] = raw_area.where(raw_area != "", area)

    return df
