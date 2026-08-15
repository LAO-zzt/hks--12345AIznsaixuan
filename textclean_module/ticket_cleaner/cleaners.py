"""字段清洗 + 文本清洗 + 套话弱化。

Level 1 规则层：日期/手机号/工单号/固定套话/常见格式。
"""

from __future__ import annotations

import re
import unicodedata
from typing import List, Tuple

# ---------- 默认套话字典 ----------

DEFAULT_BOILERPLATE: List[str] = [
    "市民致电反映",
    "市民反映",
    "诉求人表示",
    "诉求人反映",
    "据市民反映",
    "据诉求人反映",
    "希望相关部门介入",
    "希望部门介入",
    "要求部门处理",
    "要求相关部门处理",
    "要求给予合理解释",
    "要求部门给与合理解释",
    "请相关部门核实",
    "请部门核实",
    "市民希望",
    "诉求人希望",
    "市民要求",
    "诉求人要求",
    "市民表示",
    "（市民方便接听部门电话）",
    "（市民不方便接听部门电话）",
    "（工单号：",
    "跟进中。",
    "跟进中",
    "已跟进",
    "市民来电反映",
    "来电反映",
]

# ---------- 顺德区镇街（用于地点识别） ----------

SHUNDE_TOWNS: List[str] = [
    "大良街道", "大良",
    "容桂街道", "容桂",
    "伦教街道", "伦教",
    "勒流街道", "勒流",
    "陈村镇", "陈村",
    "北滘镇", "北滘",
    "乐从镇", "乐从",
    "龙江镇", "龙江",
    "杏坛镇", "杏坛",
    "均安镇", "均安",
]

# ---------- 顺德区各镇街社区/村词典（用于地址层级对齐） ----------
# 格式：{镇街: [社区/村列表]}
SHUNDE_COMMUNITIES: Dict[str, List[str]] = {
    "大良街道": [
        "升平社区", "文秀社区", "云路社区", "新桂社区", "府又社区",
        "广源社区", "凤山社区", "中区社区", "逢沙村", "红岗村",
        "古鉴村", "大门社区", "新松村", "近良社区", "苏岗村",
        "大良社区", "金榜社区", "北区社区", "南区社区",
    ],
    "容桂街道": [
        "容桂社区", "振华社区", "朝阳社区", "东风社区", "红星社区",
        "马岗社区", "扁滘社区", "小黄圃社区", "小黄圃村", "高黎社区",
        "华口社区", "容山社区", "容新社区", "桂洲社区", "细滘社区",
        "大福基社区", "幸福社区", "天佑社区", "容桂村",
    ],
    "伦教街道": [
        "伦教社区", "鸡洲村", "永丰村", "乌洲村", "仕版村",
        "新塘村", "荔村村", "霞石村", "三洲社区", "羊额村",
        "常教社区", "熹涌村", "大洲村",
    ],
    "勒流街道": [
        "勒流社区", "大晚社区", "众涌村", "江义村", "稔海村",
        "富裕村", "西华村", "黄连社区", "南水村", "裕源村",
        "大晚村", "新城社区", "连杜村", "龙眼村", "新明村",
    ],
    "陈村镇": [
        "陈村社区", "合成社区", "勒竹社区", "南涌社区", "旧墟社区",
        "锦龙社区", "赤花社区", "石洲村", "仙涌村", "庄头村",
        "弼教村", "潭州村", "大都村",
    ],
    "北滘镇": [
        "北滘社区", "碧江社区", "桃村", "西海村", "广教村",
        "三桂村", "林头村", "莘村", "马龙村", "西海村",
        "黄龙村", "水口村", "上僚村", "高村",
    ],
    "乐从镇": [
        "乐从社区", "大罗村", "沙边村", "良村", "荷村",
        "藤冲村", "路州村", "水藤村", "沙滘社区", "葛岸村",
        "大墩村", "小涌村", "杨滘村",
    ],
    "龙江镇": [
        "龙江社区", "仙塘村", "沙富村", "苏溪村", "万安村",
        "东海村", "西庆村", "东头村", "旺岗村", "坦田村",
        "左滩村", "麦朗村", "仙塘村",
    ],
    "杏坛镇": [
        "杏坛社区", "昌教村", "吉祐村", "北水村", "南朗村",
        "古朗村", "马东村", "逢简村", "桑麻村", "龙潭村",
        "光辉村", "南华村", "东马宁村",
    ],
    "均安镇": [
        "均安社区", "沙头社区", "仓门社区", "南面村", "星槎村",
        "天连村", "三华村", "新华村", "沙浦村", "畅兴村",
        "菱溪村", "鹤峰村", "欧阳村",
    ],
}


# ---------- 字段清洗 ----------

def clean_ticket_no(raw: str) -> str:
    """工单号：去前后空格、统一大小写、保留格式（不删连接符）。"""
    if not raw:
        return ""
    return str(raw).strip().upper()


def clean_caller_name(raw: str) -> Tuple[str, str]:
    """姓名：去空格、标准化称谓。返回 (normalized, raw)。"""
    if not raw:
        return "", ""
    s = str(raw).strip()
    # 标准化称谓
    normalized = s
    title_map = {
        "先生": "先生", "女士": "女士", "小姐": "女士",
        "太太": "女士", "夫人": "女士",
    }
    for k, v in title_map.items():
        if s.endswith(k):
            normalized = s[:-len(k)] + v
            break
    return normalized, s


def clean_phone(raw: str) -> Tuple[str, str, str, float]:
    """手机号归一。

    返回 (normalized, masked, raw, confidence)。
    """
    if not raw:
        return "", "", "", 0.0
    s = str(raw)
    # 抽取所有数字
    digits = re.sub(r"\D", "", s)
    # 国内手机号 11 位
    if re.fullmatch(r"1[3-9]\d{9}", digits):
        masked = digits[:3] + "****" + digits[-4:]
        return digits, masked, s.strip(), 1.0
    # 座机 7-8 位（视为弱特征）
    if 7 <= len(digits) <= 8:
        return digits, digits[:3] + "****", s.strip(), 0.4
    return "", "", s.strip(), 0.0


# ---------- 日期解析 ----------

_DATE_PATTERNS: List[Tuple[re.Pattern, str]] = [
    # 2024-12-31 23:59:39 / 2024-12-31 23:59
    (re.compile(r"(\d{4})[-/](\d{1,2})[-/](\d{1,2})[ T](\d{1,2}):(\d{1,2})(?::(\d{1,2}))?"), "ymd_hms"),
    # 2024年12月31日23:59:39 / 2024年12月31日 23:59
    (re.compile(r"(\d{4})年(\d{1,2})月(\d{1,2})日\s*(\d{1,2}):(\d{1,2})(?::(\d{1,2}))?"), "ymd_hms"),
    # 2024年12月31日
    (re.compile(r"(\d{4})年(\d{1,2})月(\d{1,2})日"), "ymd"),
    # 2024-12-31
    (re.compile(r"(\d{4})[-/](\d{1,2})[-/](\d{1,2})"), "ymd"),
    # 12月31日23:59
    (re.compile(r"(\d{1,2})月(\d{1,2})日\s*(\d{1,2}):(\d{1,2})(?::(\d{1,2}))?"), "md_hms"),
    # 12月31日
    (re.compile(r"(\d{1,2})月(\d{1,2})日"), "md"),
]


def parse_datetime(raw: str) -> Tuple[str, str, float]:
    """解析日期时间。返回 (iso_str, raw, confidence)。

    无法识别时返回 ("", raw, 0.0)。不能猜。
    """
    if not raw:
        return "", "", 0.0
    s = str(raw).strip()
    for pat, kind in _DATE_PATTERNS:
        m = pat.search(s)
        if not m:
            continue
        groups = m.groups()
        try:
            if kind == "ymd_hms":
                y, mo, d, h, mi = int(groups[0]), int(groups[1]), int(groups[2]), int(groups[3]), int(groups[4])
                sec = int(groups[5]) if groups[5] else 0
                if not _valid_dt(y, mo, d, h, mi, sec):
                    continue
                return f"{y:04d}-{mo:02d}-{d:02d} {h:02d}:{mi:02d}:{sec:02d}", m.group(0), 1.0
            if kind == "ymd":
                y, mo, d = int(groups[0]), int(groups[1]), int(groups[2])
                if not _valid_dt(y, mo, d, 0, 0, 0):
                    continue
                return f"{y:04d}-{mo:02d}-{d:02d} 00:00:00", m.group(0), 0.9
            if kind == "md_hms":
                mo, d, h, mi = int(groups[0]), int(groups[1]), int(groups[2]), int(groups[3])
                sec = int(groups[4]) if groups[4] else 0
                if not _valid_dt(2000, mo, d, h, mi, sec):
                    continue
                # 年份缺失：返回仅月日时间
                return f"--{mo:02d}-{d:02d} {h:02d}:{mi:02d}:{sec:02d}", m.group(0), 0.5
            if kind == "md":
                mo, d = int(groups[0]), int(groups[1])
                if not _valid_dt(2000, mo, d, 0, 0, 0):
                    continue
                return f"--{mo:02d}-{d:02d}", m.group(0), 0.4
        except (ValueError, IndexError):
            continue
    return "", s, 0.0


def _valid_dt(y: int, mo: int, d: int, h: int, mi: int, s: int) -> bool:
    if not (1 <= mo <= 12):
        return False
    if not (1 <= d <= 31):
        return False
    if not (0 <= h <= 23):
        return False
    if not (0 <= mi <= 59):
        return False
    if not (0 <= s <= 59):
        return False
    return True


# ---------- 文本清洗 ----------

# 全角转半角
def full_to_half(s: str) -> str:
    if not s:
        return ""
    return unicodedata.normalize("NFKC", s)


# HTML残留
_HTML_TAG_RE = re.compile(r"<[^>]+>")
_HTML_ENTITY_RE = re.compile(r"&[a-zA-Z#0-9]+;")


def clean_text(raw: str) -> str:
    """文本标准化：连续空格/换行/制表符/HTML/控制字符/全角半角/连续标点。"""
    if not raw:
        return ""
    s = str(raw)
    # HTML标签
    s = _HTML_TAG_RE.sub(" ", s)
    # HTML实体（简单替换）
    s = s.replace("&nbsp;", " ").replace("&amp;", "&").replace("&lt;", "<").replace("&gt;", ">")
    s = _HTML_ENTITY_RE.sub(" ", s)
    # 全角→半角（保留中文标点感，NFKC会变全角标点为半角？实际NFKC把全角逗号变半角逗号）
    # 但12345文本以中文为主，全角标点应保留，因此只转字母数字和ASCII符号
    s = _convert_ascii_fullwidth(s)
    # 制表符
    s = s.replace("\t", " ")
    # 多余换行
    s = re.sub(r"\r\n|\r", "\n", s)
    s = re.sub(r"\n{2,}", "\n", s)
    # 连续空格
    s = re.sub(r"[ ]{2,}", " ", s)
    # 连续标点：！！！→！；？？？→？
    s = re.sub(r"([!？。，、；：])\1{1,}", r"\1", s)
    # 去掉行首尾空格
    s = "\n".join(line.strip() for line in s.split("\n"))
    # 整体去首尾
    return s.strip()


def _convert_ascii_fullwidth(s: str) -> str:
    """仅把ASCII字母数字和常见符号从全角转半角，中文标点保留。"""
    out = []
    for ch in s:
        code = ord(ch)
        # 全角ASCII：0xFF01-0xFF5E
        if 0xFF01 <= code <= 0xFF5E:
            out.append(chr(code - 0xFEE0))
        # 全角空格
        elif code == 0x3000:
            out.append(" ")
        else:
            out.append(ch)
    return "".join(out)


# ---------- 套话弱化 ----------

def compile_boilerplate(boilerplate: List[str]) -> re.Pattern:
    """编译套话正则。按长度降序匹配，避免短套话吃掉长套话。"""
    items = sorted(set(boilerplate), key=len, reverse=True)
    escaped = [re.escape(b) for b in items]
    return re.compile("|".join(escaped))


def weaken_boilerplate(text: str, pattern: re.Pattern) -> str:
    """把套话降权（删除）以生成 semantic_content。原文不删。"""
    if not text:
        return ""
    return pattern.sub("", text)
