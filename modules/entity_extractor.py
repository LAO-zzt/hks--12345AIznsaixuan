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

# 主体后缀规则（按优先级分组）
# 社区/小区类：优先级最高，因为 12345 工单中"XX小区"才是真正的主体
_COMMUNITY_SUFFIX = ["小区", "花园", "公寓", "苑", "居", "府", "村", "社区",
                     "工业园", "大厦", "大楼", "家园", "雅苑", "名居", "公馆", "庭院", "新城", "楼盘"]
# 机构类
_INSTITUTION_SUFFIX = ["医院", "卫生院", "卫生服务中心", "诊所", "门诊",
                        "学校", "小学", "中学", "大学", "幼儿园", "学院",
                        "物业", "管理处", "局", "委", "办", "大队", "支队", "分局", "所", "站"]
# 企业类
_COMPANY_SUFFIX = ["公司", "厂", "企业", "集团", "商店", "商行", "经营部"]
# 商铺/场所类（优先级最低，社区名优先）
_SHOP_SUFFIX = ["体验馆", "体育馆", "博物馆", "店", "铺", "餐厅", "食店", "超市", "百货", "烧烤店", "商场"]
_PLACE_SUFFIX = ["美食城", "夜市街", "商业街", "广场", "公园", "市场",
                  "步行街", "夜市", "大道", "路口", "工地", "街道", "镇"]

# 通用区域正则（词典未命中时的兜底）
_GENERIC_AREA_RE = r"([\u4e00-\u9fa5]{1,4}(?:街道|镇))"

# 按优先级编译的主体正则（用于向量化提取）
_COMMUNITY_RE = re.compile(r"([\u4e00-\u9fa5]{2,12}(?:" + "|".join(_COMMUNITY_SUFFIX) + r"))")
_INSTITUTION_RE = re.compile(r"([\u4e00-\u9fa5]{2,15}(?:" + "|".join(_INSTITUTION_SUFFIX) + r"))")
_COMPANY_RE = re.compile(r"([\u4e00-\u9fa5]{2,20}(?:" + "|".join(_COMPANY_SUFFIX) + r"))")
_SHOP_PLACE_RE = re.compile(r"([\u4e00-\u9fa5]{2,12}(?:" + "|".join(_SHOP_SUFFIX + _PLACE_SUFFIX) + r"))")

# 商铺/场所后缀集合（用于社区覆盖判断）
_SHOP_PLACE_SUFFIX = set(_SHOP_SUFFIX + _PLACE_SUFFIX)

_AREA_TERMS = None
_AREA_DICT_RE = None


def _extract_subject_from_text(text: str) -> str:
    """从单条文本中按优先级提取主体（社区 > 机构 > 企业 > 商铺/场所）。

    当商铺/场所名出现在社区名之后（10字距离内），优先返回社区名。
    """
    if not text:
        return ""
    text = str(text)

    # 预扫描社区匹配
    community_hits = [(m.start(), m.end(), m.group(1))
                      for m in _COMMUNITY_RE.finditer(text)]

    # 按优先级尝试：社区 → 机构 → 企业 → 商铺/场所
    for pat in (_COMMUNITY_RE, _INSTITUTION_RE, _COMPANY_RE, _SHOP_PLACE_RE):
        for m in pat.finditer(text):
            raw = m.group(1).strip()
            if not raw:
                continue
            # 社区优先覆盖：若当前是商铺/场所类型，且之前有社区名
            if pat is _SHOP_PLACE_RE and community_hits:
                for c_start, c_end, c_raw in community_hits:
                    if c_start < m.start() and m.start() - c_end <= 10:
                        return c_raw
            return raw

    # 兜底：有社区匹配但没被选中时
    if community_hits:
        return community_hits[0][2]
    return ""


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


def _clean_area_match(s: str) -> str:
    """清洗通用正则的区域匹配：截掉首个 区/市/县 之前的残字。"""
    if not s:
        return ""
    for sep in ("区", "市", "县"):
        idx = s.find(sep)
        if 0 <= idx < len(s) - 2:
            return s[idx + 1:]
    return s


def _match_area_vec(texts: pd.Series) -> pd.Series:
    """向量化区域提取：先区域词典，再通用“XX街道/XX镇”正则（结果清洗）。"""
    res = pd.Series([""] * len(texts), index=texts.index, dtype=object)
    dict_re = _area_patterns()
    if dict_re:
        hit = texts.str.extract(dict_re)[0].fillna("")
        res = hit
    # 词典未命中的行走通用正则
    miss = res == ""
    if miss.any():
        generic = texts[miss].str.extract(_GENERIC_AREA_RE)[0].fillna("")
        generic = generic.map(_clean_area_match)
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

    # ---- 主体：原始字段清洗 → 内容提取（社区优先） ----
    raw_subject = df.get("subject", pd.Series([""] * len(df), index=df.index)).fillna("").astype(str).str.strip()
    raw_subject_clean = raw_subject.apply(_extract_subject_from_text)
    subj_from_text = content.apply(_extract_subject_from_text)
    df["extracted_subject"] = raw_subject_clean.where(raw_subject_clean != "", subj_from_text)

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
