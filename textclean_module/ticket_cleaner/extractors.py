"""实体抽取。

基于规则 + 词典的实体抽取（Level 1 + Level 2）。
抽取：人物 / 手机号 / 主体 / 地点 / 时间 / 事件 / 诉求。

不调用LLM，避免全量LLM清洗。
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

from ticket_cleaner.cleaners import (
    SHUNDE_TOWNS,
    SHUNDE_COMMUNITIES,
    clean_phone,
    parse_datetime,
)


# ---------- 人物抽取 ----------

# 称谓
_PERSON_TITLES = ["先生", "女士", "小姐", "太太", "夫人", "老师", "师傅", "老板"]
# 非人名黑名单（动词短语、套话片段）
_NON_PERSON_NAMES = {
    "市民", "诉求人", "反映", "希望", "要求", "致电", "来电",
    "表示", "建议", "跟进", "处理", "介入", "核实", "调查",
    "市民致电", "市民来电", "诉求人来电", "诉求人反映",
    "市民曾反", "市民反映", "诉求人反映直至", "市民希望",
    "诉求人希望", "市民表示", "诉求人表示",
}
# 称呼模式：张女士、张先生、张小姐、张某、张某某
# 使用边界限制：称谓前面必须是姓（单字）或"姓+名"
_PERSON_PATTERNS = [
    # 姓+称谓：张女士、张先生 (姓为单字，称谓前不能是"市民/诉求人"等)
    re.compile(r"(?<![市民诉求])([\u4e00-\u9fa5])(先生|女士|小姐|太太|夫人|老师|师傅|老板)"),
    # 某某/张某/张某某
    re.compile(r"([\u4e00-\u9fa5])某(?:某)?"),
    # "市民X先生/女士" 中的姓
    re.compile(r"市民([\u4e00-\u9fa5])(?:先生|女士|小姐)"),
    re.compile(r"诉求人([\u4e00-\u9fa5])(?:先生|女士|小姐)"),
]


def extract_person(text: str) -> Tuple[str, float]:
    """抽取人物。返回 (raw, confidence)。"""
    if not text:
        return "", 0.0
    for pat in _PERSON_PATTERNS:
        for m in pat.finditer(text):
            name = m.group(1)
            # 过滤黑名单
            if name in _NON_PERSON_NAMES:
                continue
            # 单字姓至少1字
            if len(name) < 1:
                continue
            return m.group(0), 0.6
    return "", 0.0


# ---------- 手机号抽取 ----------

_PHONE_RE = re.compile(r"1[3-9]\d{9}")
_PHONE_RE_LOOSE = re.compile(r"1[3-9]\d[\s\-]?\d{4}[\s\-]?\d{4}")


def extract_phone(text: str) -> Tuple[str, str, str, float]:
    """抽取手机号。返回 (raw, normalized, masked, confidence)。"""
    if not text:
        return "", "", "", 0.0
    # 先松匹配，再清洗
    m = _PHONE_RE_LOOSE.search(text)
    if m:
        return clean_phone(m.group(0))
    return "", "", "", 0.0


# ---------- 主体抽取 ----------

# 主体名前缀黑名单：这些字后面的主体名往往是误识别
# 例如 "依法处罚违法行" 中的"行"被识别为企业后缀
_ORG_PREFIX_BLACKLIST = [
    "依法", "请部门", "希望部门", "要求部门", "相关部门", "请相关",
    "市民希望", "诉求人希望", "市民要求", "诉求人要求",
    "位于", "名称", "别墅", "车辆", "牌照",
    "近期多次有人夜晚在加油站",  # 整句
    "请部门尽快调查并且对涉事人员",
    "市民致电反映其是",
    "诉求人来电反映",
    "市民曾反映",
    "位于广东省",
    "市民来电反映",
    "市民致电反映于",
    "诉求人来电反映北",
    "市民致电反映顺德区",
    "期多次有人夜晚在加油站",
    "期多次",
    "近期",
]

# 主体名内部不应包含的动词/套话（出现则视为误识别）
_ORG_INNER_BLACKLIST = [
    "希望", "要求", "请部", "介入", "处理", "核实", "调查",
    "处罚", "制止", "解决", "反映", "表示", "致电", "来电",
    "存在", "位于", "因为", "导致", "影响", "扰民", "违法",
    "燃放", "喧哗", "噪音", "市民", "诉求人",
    "尽快", "及时", "切实", "加强", "依法",
    "针对", "回复", "尊敬",
    "多次", "有人", "夜晚", "加油站", "放烟花", "担", "引发",
    "人为", "灾难", "附近", "楼盘", "住宅",
]

# 主体名不能以这些词开头（否则是误识别）
_ORG_START_BLACKLIST = [
    "期", "近", "多次", "有人", "夜晚", "委会", "人民政",
    "位于", "存在", "因为", "导致", "影响",
    "市民", "诉求", "请部", "希望", "要求",
]

# 主体类型关键词（按优先级排序）
# 优先级原则：社区/小区 > 机构/医院/学校 > 企业 > 商铺/场所
# 因为 12345 热线工单中，"XX小区楼下烧烤店"的真实主体是小区，而非烧烤店
_ORG_KEYWORDS: List[Tuple[str, re.Pattern]] = [
    ("community", re.compile(r"([\u4e00-\u9fa5]{2,12}(?:小区|花园|公寓|苑|居|府|村|社区|工业园|大厦|大楼|家园|雅苑|名居|公馆|庭院|新城|楼盘))")),
    ("hospital", re.compile(r"([\u4e00-\u9fa5]{2,15}(?:医院|卫生院|卫生服务中心|诊所|门诊))")),
    ("school", re.compile(r"([\u4e00-\u9fa5]{2,15}(?:学校|小学|中学|大学|幼儿园|学院))")),
    ("property", re.compile(r"([\u4e00-\u9fa5]{2,15}(?:物业|管理处))")),
    ("department", re.compile(r"([\u4e00-\u9fa5]{2,10}(?:局|委|办|大队|支队|分局|所|站|街道|镇))")),
    ("company", re.compile(r"([\u4e00-\u9fa5]{2,20}(?:公司|厂|企业|集团|商店|商行|经营部))")),
    ("shop", re.compile(r"([\u4e00-\u9fa5]{2,10}(?:体验馆|体育馆|博物馆|店|铺|餐厅|食店|超市|百货|烧烤店|商场))")),
    ("place", re.compile(r"([\u4e00-\u9fa5]{2,10}(?:美食城|夜市街|商业街|广场|公园|市场|步行街|夜市|大道|路口|工地|街道))")),
    ("minsu", re.compile(r"([\u4e00-\u9fa5]{2,15}(?:民宿|客栈|旅馆|酒店|宾馆|公寓酒店))")),
]

# 社区/小区模式单独预编译，用于"社区优先"覆盖逻辑
_COMMUNITY_PATTERN = _ORG_KEYWORDS[0][1]
# 商铺/场所类型集合，用于触发社区覆盖
_SHOP_PLACE_TYPES = {"shop", "place", "minsu"}

# 主体类型 -> 标签
_ORG_TYPE_LABEL = {
    "hospital": "医院",
    "company": "企业",
    "property": "物业",
    "school": "学校",
    "shop": "商铺",
    "community": "小区",
    "department": "部门",
    "minsu": "民宿",
    "place": "场所",
}

# 错误主体黑名单（懒加载）
_SUBJECT_BLACKLIST = None


def _load_subject_blacklist() -> set:
    """加载错误主体黑名单，返回集合。"""
    global _SUBJECT_BLACKLIST
    if _SUBJECT_BLACKLIST is None:
        _SUBJECT_BLACKLIST = set()
        try:
            # 尝试加载项目级黑名单
            project_root = os.path.join(os.path.dirname(__file__), "..", "..")
            bl_path = os.path.join(project_root, "data", "dicts", "subject_blacklist.txt")
            if os.path.exists(bl_path):
                with open(bl_path, "r", encoding="utf-8") as f:
                    for line in f:
                        line = line.strip()
                        if line and not line.startswith("#"):
                            _SUBJECT_BLACKLIST.add(line)
        except Exception:
            pass
    return _SUBJECT_BLACKLIST


def _is_blacklisted_subject(raw: str) -> bool:
    """检查主体是否在黑名单中（支持精确匹配和子串匹配）。"""
    if not raw:
        return False
    blacklist = _load_subject_blacklist()
    # 精确匹配
    if raw in blacklist:
        return True
    # 子串匹配：如果主体包含黑名单词，也视为黑名单
    for bl in blacklist:
        if bl and bl in raw:
            return True
    return False


def _is_valid_org_name(raw: str) -> bool:
    """校验主体名是否有效（非套话片段）。"""
    if not raw or len(raw) < 2:
        return False
    # 不能包含黑名单动词
    for kw in _ORG_INNER_BLACKLIST:
        if kw in raw:
            return False
    # 前缀黑名单
    for prefix in _ORG_PREFIX_BLACKLIST:
        if raw.startswith(prefix):
            return False
    # 起始黑名单
    for start in _ORG_START_BLACKLIST:
        if raw.startswith(start):
            return False
    # 不能全是单个字重复
    if len(set(raw)) == 1:
        return False
    return True


# ---------- 劳动/欠薪场景公司抽取（多频分组用，治"同镇不同公司被并为一簇"） ----------

_LABOR_CONTEXT_KW = ("拖欠", "欠薪", "克扣", "工资", "薪酬", "劳务", "工作", "工钱", "不发")
# 公司名不应包含的套话/动作词（避免把"单位拖欠全厂"等误当公司名）
_LABOR_NAME_BAD = ("拖欠", "欠薪", "克扣", "工资", "薪酬", "单位", "反映", "来电",
                   "致电", "表示", "全厂", "全店", "存在", "正在", "以及", "公司地址",
                   "项目", "要求", "希望", "请部", "介入")
# 公司抽取模式（按优先级逐一尝试；每条独立 search+校验，避免贪婪跨模式吞套话）
_LABOR_COMPANY_PATTERNS = [
    re.compile(r"(?:名称|名字|单位名称)[:：]\s*([\u4e00-\u9fa5A-Za-z0-9]{2,25})"),
    re.compile(r"项目单位[:：]\s*([\u4e00-\u9fa5A-Za-z0-9]{2,20})"),
    re.compile(r"在([\u4e00-\u9fa5A-Za-z0-9]{2,25}?(?:有限公司|公司|厂|集团|分公司|劳务|服务部|经营部|物流园))工作"),
    re.compile(r"(?:被|反映被)(?:佛山市顺德区|佛山市|顺德区)?"
               r"([\u4e00-\u9fa5A-Za-z0-9]{2,25}?(?:有限公司|公司|厂|集团|分公司|劳务|服务部|经营部))(?=[\s，,。？（(]|拖欠)"),
    re.compile(r"([\u4e00-\u9fa5A-Za-z0-9]{2,20}(?:分拨中心|物流园|工业园|有限公司|公司|厂|集团|分公司|劳务|服务部|经营部))[（(]?地址"),
    re.compile(r"([\u4e00-\u9fa5A-Za-z0-9]{2,20}(?:有限公司|公司|厂|集团|分公司|劳务|服务部|经营部))\s*(?:，|,)?\s*拖欠"),
    re.compile(r"([\u4e00-\u9fa5A-Za-z0-9]{2,20}(?:有限公司|公司|厂|集团|分公司|劳务|服务部|经营部))[的]?(?:员工|人员|工人|职工)"),
]
# 泛化主体后缀：公司名不应只是镇街/社区
_GENERIC_ORG_SUFFIX = ("街道", "镇", "社区", "村", "居委会", "村委会")


def _is_labor_context(text: str) -> bool:
    """是否劳动/欠薪相关文本（触发公司主体优先）。"""
    return any(k in text for k in _LABOR_CONTEXT_KW)


def _valid_labor_company(raw: str) -> bool:
    """公司候选是否可作主体：非空、非泛化后缀、不含套话。"""
    if not raw or len(raw) < 2:
        return False
    if raw.endswith(_GENERIC_ORG_SUFFIX):
        return False
    if any(k in raw for k in _LABOR_NAME_BAD):
        return False
    return _is_valid_org_name(raw) and not _is_blacklisted_subject(raw)


def _extract_labor_company(text: str) -> str:
    """从欠薪/劳动文本中提取公司主体；失败返回空字符串。"""
    for pat in _LABOR_COMPANY_PATTERNS:
        for m in pat.finditer(text):
            raw = next((g for g in m.groups() if g), "").strip()
            if _valid_labor_company(raw):
                return raw
    return ""


def extract_organization(text: str) -> Tuple[str, str, float]:
    """抽取主体。返回 (raw, entity_type, confidence)。

    核心策略：社区/小区 > 机构/医院/学校 > 企业 > 商铺/场所。
    当商铺/场所名出现在社区名之后（10字距离内），优先返回社区名。
    黑名单主体视为未命中，返回空。
    """
    if not text:
        return "", "", 0.0

    # 劳动/欠薪场景：公司主体优先（"同一公司欠薪"是比社区更有区分度的多频键）
    if _is_labor_context(text):
        company = _extract_labor_company(text)
        if company:
            return company, "企业", 0.9

    # 预扫描所有社区/小区匹配（用于社区优先覆盖）
    community_matches = []
    for m in _COMMUNITY_PATTERN.finditer(text):
        raw = m.group(1).strip()
        if _is_valid_org_name(raw) and not _is_blacklisted_subject(raw):
            community_matches.append((m.start(), m.end(), raw))

    # 按优先级尝试每种类型
    for typ, pat in _ORG_KEYWORDS:
        for m in pat.finditer(text):
            raw = m.group(1).strip()
            if not _is_valid_org_name(raw) or _is_blacklisted_subject(raw):
                continue

            # 社区优先覆盖：若匹配到商铺/场所，且之前有社区名
            # （位置在商铺前，且距离 ≤ 10 字），则优先返回社区名
            if typ in _SHOP_PLACE_TYPES and community_matches:
                for c_start, c_end, c_raw in community_matches:
                    if c_start < m.start() and m.start() - c_end <= 10:
                        if not _is_blacklisted_subject(c_raw):
                            return c_raw, "小区", 0.85

            # 截取最后一个动词之后的部分
            cut_pos = -1
            for kw in _ORG_INNER_BLACKLIST:
                pos = raw.rfind(kw)
                if pos >= 0 and pos + len(kw) > cut_pos:
                    cut_pos = pos + len(kw)
            if cut_pos > 0 and cut_pos < len(raw):
                candidate = raw[cut_pos:].strip()
                if len(candidate) >= 2 and _is_valid_org_name(candidate) and not _is_blacklisted_subject(candidate):
                    raw = candidate

            if not _is_valid_org_name(raw) or _is_blacklisted_subject(raw):
                continue
            return raw, _ORG_TYPE_LABEL.get(typ, "organization"), 0.7

    # 兜底：有社区匹配但没被选中时，返回第一个社区
    if community_matches:
        _, _, raw = community_matches[0]
        if not _is_blacklisted_subject(raw):
            return raw, "小区", 0.6

    return "", "", 0.0


# ---------- 地点抽取 ----------

# 镇街
_TOWN_RE = re.compile(r"((?:大良|容桂|伦教|勒流|陈村|北滘|乐从|龙江|杏坛|均安)(?:街道|镇)?)")

# 小区/楼盘：限定最多6字前缀，避免贪婪吞掉套话
# 使用非贪婪+边界：前面不能是动词或否定词
_COMMUNITY_RE = re.compile(
    r"(?:(?<![电反映称示位有没不未]))([\u4e00-\u9fa5A-Za-z0-9]{2,8}?)(小区|花园|公寓|府|村|社区|苑)"
)
# 注意：去掉了"城"后缀，因为容易误识别"当地城"、"新城"等

# 楼盘/小区专名后缀，用于"名称（地址）"结构提取
# 按长度降序排列，长后缀优先（如"华府"先于"府"），避免把"明日华府"误拆成"明日华"+"府"。
_ESTATE_SUFFIXES: List[str] = sorted(
    ["小区", "花园", "华府", "豪庭", "家园", "雅苑", "名居", "公馆", "广场",
     "新城", "庭院", "楼盘", "公寓", "社区", "苑", "村", "府", "庭", "居"],
    key=len, reverse=True,
)
# 括号（全角/半角）
_PAREN_OPEN_RE = re.compile(r"[（(]")
# 括号结构：明日华府（地址：大良街道新桂中路） → 提取"明日华府"为社区/小区名。
# 这类名称通常紧跟着地址括号，是最可靠的社区识别信号，优先于通用正则。
# 关键：专名限制为 1~6 字且后缀必须紧贴"（"，因此不会从远处贪婪吞入套话
# （如"市民致电表示自己是明日华府（"只会匹配到"明日华府（"）。
_PAREN_NAME_RE = re.compile(
    r"([\u4e00-\u9fa5A-Za-z0-9]{1,6})(" + "|".join(_ESTATE_SUFFIXES) + r")\s*[（(]"
)

# 道路：限定前缀长度
_ROAD_RE = re.compile(r"([\u4e00-\u9fa5]{2,8}?(?:路|街|大道|巷|公路))")

# 门牌号
_BUILDING_RE = re.compile(r"(\d+(?:栋|幢|座|单元|号|室|层|楼)(?:\d*(?:栋|幢|座|单元|号|室|层|楼))*)")

# 地址片段起始黑名单（不能作为地址开头）
_ADDR_PREFIX_BLACKLIST = [
    "市民", "诉求人", "反映", "致电", "来电", "表示",
    "希望", "要求", "请部", "介入", "处理", "核实",
    "存在", "位于", "因为", "导致", "影响", "扰民",
    "噪音", "违法", "燃放", "喧哗", "尽快", "及时",
    "切实", "加强", "依法", "针对", "回复", "尊敬",
    "是", "其", "该", "此", "于", "在", "的", "了",
    "自己", "自家",  # 代词，不能作为地址/小区开头（如"自己小区"应消解）
    "顺德区", "佛山市", "广东省",  # 这些是上级行政区，已经单独识别
    "人民政府", "居委会", "委会",  # 这些不是小区
    "新城", "新村",  # 这些是小区名前缀，不应作为开头
]

# 小区名内部不应包含的词
# 注意：时间词（今日/昨日/明日/今年…）已从本表移除，因为它们作为子串会误杀
# 真实小区名（如"明日华府"含"明日"）。前导时间词由 _ADDR_PREFIX_BLACKLIST
# 在 _clean_addr_segment 中处理。
_ADDR_INNER_BLACKLIST = [
    "市民", "诉求", "反映", "致电", "来电", "表示",
    "人民政", "委会", "位于", "存在",
    "感谢", "您好", "尊敬", "针对", "回复",
    "执法", "处理", "核实", "调查",
    # 数字年份片段（如2024、2025前缀，避免误识别年份当小区名）
    "202", "201", "200", "199",
]

# 小区名单独不能匹配的整词（无意义）
_COMMUNITY_INVALID = {
    "社区", "村", "新城", "新村",  # 太宽泛
}


def _clean_addr_segment(seg: str) -> str:
    """清理地址片段：去掉前缀黑名单部分。"""
    if not seg:
        return ""
    s = seg
    changed = True
    while changed:
        changed = False
        for kw in _ADDR_PREFIX_BLACKLIST:
            if s.startswith(kw):
                s = s[len(kw):]
                changed = True
    # 内部黑名单：直接返回空
    for kw in _ADDR_INNER_BLACKLIST:
        if kw in s:
            return ""
    return s.strip()


def _strip_leading_noise(s: str) -> str:
    """仅剥离前缀代词/套话（不做内部黑名单清空），用于括号前整段文本。"""
    if not s:
        return ""
    changed = True
    while changed:
        changed = False
        for kw in _ADDR_PREFIX_BLACKLIST:
            if s.startswith(kw):
                s = s[len(kw):]
                changed = True
    return s.strip()


def _is_valid_community(name: str) -> bool:
    """校验小区名是否有效。"""
    if not name or len(name) < 2:
        return False
    # 单独的"社区/村/新城/新村"等无意义
    if name in _COMMUNITY_INVALID:
        return False
    # 仅"X社区"且X长度<2 视为无效（如"小湾社区"OK，但"社区"不OK）
    # 去掉后缀后的前缀
    for suffix in ("小区", "花园", "公寓", "苑", "居", "府", "城", "村", "社区"):
        if name.endswith(suffix):
            prefix = name[:-len(suffix)]
            if not prefix:  # 只有后缀
                return False
            if len(prefix) < 2 and suffix in ("社区", "村", "城"):
                return False
            break
    for kw in _ADDR_INNER_BLACKLIST:
        if kw in name:
            return False
    # 不能以单字动词开头
    if name[0] in ("是", "其", "该", "此", "于", "在", "的", "了"):
        return False
    return True


def extract_address(text: str) -> Dict[str, str]:
    """抽取地址各字段。

    返回 dict: district/town/community/road/building/address_raw。
    地址层级对齐高德地图：区 → 镇街 → 社区/村 → 道路 → 门牌
    """
    result = {
        "district": "顺德区",  # 数据集为顺德区
        "town": "",
        "community": "",
        "road": "",
        "building": "",
        "address_raw": "",
    }
    if not text:
        return result

    # 1. 抽取镇街
    town_match = _TOWN_RE.search(text)
    if town_match:
        t = town_match.group(1)
        # 标准化为"X镇"或"X街道"
        for full in SHUNDE_TOWNS:
            if full.startswith(t) or t.startswith(full):
                result["town"] = full if ("镇" in full or "街道" in full) else full + "镇"
                break

    # 2. 抽取社区/村（优先使用词典匹配）
    search_start = town_match.end() if town_match else 0

    # 2.0 优先从"名称（地址）"括号结构提取小区/楼盘名（最可靠）
    # 该类名称可能出现在镇街之前（如"明日华府（地址：大良街道新桂中路）"），
    # 通用正则从 town 之后才开始搜索会漏掉，因此单独处理。
    # 做法：取第一个"（"前的整段文本，先剥离前缀代词/套话（如"市民致电表示自己是"），
    # 再在其末尾匹配"专名+后缀"，避免贪婪吞入远处套话。
    if not result["community"]:
        p = _PAREN_OPEN_RE.search(text)
        if p:
            left = _strip_leading_noise(text[:p.start()])
            m = re.search(
                r"([\u4e00-\u9fa5A-Za-z0-9]{1,6})("
                + "|".join(_ESTATE_SUFFIXES) + r")$",
                left,
            )
            if m:
                name = m.group(1) + m.group(2)
                if _is_valid_community(name) and len(name) >= 3:
                    result["community"] = name

    # 2.1 优先从词典匹配社区/村
    if result["town"] and result["town"] in SHUNDE_COMMUNITIES:
        town_communities = SHUNDE_COMMUNITIES[result["town"]]
        for comm_name in town_communities:
            if comm_name in text[search_start:]:
                result["community"] = comm_name
                break
    
    # 2.2 如果词典未匹配，使用正则匹配
    if not result["community"]:
        comm_match = None
        for m in _COMMUNITY_RE.finditer(text, search_start):
            candidate = m.group(1) + m.group(2)
            # 清理前缀
            cleaned = _clean_addr_segment(m.group(1)) + m.group(2)
            if _is_valid_community(cleaned):
                result["community"] = cleaned
                comm_match = m
                break
        if not comm_match:
            # 全文找
            for m in _COMMUNITY_RE.finditer(text):
                candidate = m.group(1) + m.group(2)
                cleaned = _clean_addr_segment(m.group(1)) + m.group(2)
                if _is_valid_community(cleaned):
                    result["community"] = cleaned
                    comm_match = m
                    break

    # 3. 抽取道路
    for m in _ROAD_RE.finditer(text, search_start):
        r = m.group(1)
        # 清理前缀
        r = _clean_addr_segment(r)
        if r and len(r) >= 2 and r not in ("顺德区",):
            result["road"] = r
            break

    # 4. 抽取门牌
    bld_match = _BUILDING_RE.search(text)
    if bld_match:
        result["building"] = bld_match.group(1)

    # 5. 组装 address_raw：镇街+社区+道路+门牌
    parts = []
    if result["town"]:
        parts.append(result["town"])
    if result["community"]:
        parts.append(result["community"])
    if result["road"] and result["road"] not in parts:
        parts.append(result["road"])
    if result["building"]:
        parts.append(result["building"])
    if parts:
        result["address_raw"] = "".join(parts)
    elif town_match:
        # 退化：用镇街片段
        seg = text[town_match.start():town_match.start() + 30]
        end_match = re.search(r"[。，；！？\n(（]", seg)
        if end_match:
            seg = seg[:end_match.start()]
        result["address_raw"] = seg.strip()

    return result


# ---------- 时间抽取 ----------

_TIME_RANGE_RE = re.compile(r"(\d{4}年\d{1,2}月\d{1,2}日\s*\d{1,2}:\d{1,2}(?::\d{1,2})?)\s*[至到\-~]\s*(\d{4}年\d{1,2}月\d{1,2}日\s*\d{1,2}:\d{1,2}(?::\d{1,2})?)")
_TIME_RANGE_RE2 = re.compile(r"(\d{1,2}月\d{1,2}日\s*\d{1,2}:\d{1,2}(?::\d{1,2})?)\s*[至到\-~]\s*(\d{1,2}月\d{1,2}日\s*\d{1,2}:\d{1,2}(?::\d{1,2})?)")

# 时间模式（周期性）
_TIME_PATTERNS = {
    "每晚": "nightly",
    "每天晚上": "nightly",
    "每天": "daily",
    "每周": "weekly",
    "工作日": "workday",
    "周末": "weekend",
    "凌晨": "early_morning",
    "夜间": "nightly",
    "半夜": "nightly",
}


def extract_time(text: str) -> Tuple[str, str, str, float]:
    """抽取时间。返回 (time_start, time_end, time_pattern, confidence)。"""
    if not text:
        return "", "", "", 0.0

    # 时间范围
    m = _TIME_RANGE_RE.search(text)
    if m:
        s_iso, _, s_conf = parse_datetime(m.group(1))
        e_iso, _, e_conf = parse_datetime(m.group(2))
        if s_iso and e_iso:
            return s_iso, e_iso, "", max(s_conf, e_conf)

    m = _TIME_RANGE_RE2.search(text)
    if m:
        s_iso, _, s_conf = parse_datetime(m.group(1))
        e_iso, _, e_conf = parse_datetime(m.group(2))
        if s_iso and e_iso:
            return s_iso, e_iso, "", max(s_conf, e_conf) * 0.7

    # 单一时间
    iso, _, conf = parse_datetime(text)
    if iso:
        return iso, "", "", conf

    # 周期模式
    for kw, pat in _TIME_PATTERNS.items():
        if kw in text:
            return "", "", pat, 0.5

    return "", "", "", 0.0


# ---------- 工单类型判断（线上/线下）----------

# 线上关键词
_ONLINE_KEYWORDS = [
    "网店", "网购", "淘宝", "京东", "拼多多", "抖音", "快手",
    "平台", "APP", "小程序", "网站", "网络", "线上",
    "退款", "退货", "售后", "客服", "订单",
    "虚假宣传", "虚假广告", "刷单", "好评",
]

# 线下关键词
_OFFLINE_KEYWORDS = [
    "现场", "实地", "门店", "实体店", "线下",
    "施工", "工地", "违建", "占道", "摆摊",
    "噪音", "扰民", "油烟", "粉尘", "异味",
    "垃圾", "污水", "堵塞", "破损", "塌陷",
    "路灯", "井盖", "护栏", "绿化",
]


def classify_ticket_type(text: str) -> str:
    """判断工单类型：online/offline/unknown。"""
    if not text:
        return "unknown"

    online_score = sum(1 for kw in _ONLINE_KEYWORDS if kw in text)
    offline_score = sum(1 for kw in _OFFLINE_KEYWORDS if kw in text)

    if online_score > 0 and offline_score == 0:
        return "online"
    if offline_score > 0 and online_score == 0:
        return "offline"
    if online_score > 0 and offline_score > 0:
        # 两者都有，看哪个更多
        return "online" if online_score > offline_score else "offline"
    return "unknown"


# ---------- 诉求性质分类（投诉/建议/举报/咨询/求助） ----------

# 投诉类关键词
_COMPLAINT_KEYWORDS = [
    "投诉", "反映问题", "存在问题", "严重影响", "扰民",
    "不满", "强烈要求", "多次反映", "至今未解决",
    "违规", "违法", "损坏", "污染", "噪音", "臭味",
]

# 建议类关键词
_SUGGESTION_KEYWORDS = [
    "建议", "希望改进", "建议增加", "建议优化", "可以考虑",
    "是否可以考虑", "建议相关部门", "建议加强",
]

# 举报类关键词
_REPORT_KEYWORDS = [
    "举报", "揭发", "举报违法", "举报违规", "举报非法",
    "无证经营", "非法", "黑车", "黑作坊", "黑诊所",
    "偷排", "偷排污水", "偷倒垃圾",
]

# 咨询类关键词
_CONSULTATION_KEYWORDS = [
    "咨询", "请问", "想了解", "想咨询", "查询",
    "如何办理", "怎么办理", "办理流程", "需要什么材料",
    "政策解读", "政策咨询",
]

# 求助类关键词
_HELP_KEYWORDS = [
    "求助", "帮助", "请求帮助", "急需帮助", "困难",
    "申请", "援助", "救济", "补贴", "救助",
]


def classify_request_nature(text: str) -> str:
    """判断诉求性质：complaint/suggestion/report/consultation/help/unknown。"""
    if not text:
        return "unknown"

    complaint_score = sum(1 for kw in _COMPLAINT_KEYWORDS if kw in text)
    suggestion_score = sum(1 for kw in _SUGGESTION_KEYWORDS if kw in text)
    report_score = sum(1 for kw in _REPORT_KEYWORDS if kw in text)
    consultation_score = sum(1 for kw in _CONSULTATION_KEYWORDS if kw in text)
    help_score = sum(1 for kw in _HELP_KEYWORDS if kw in text)

    scores = {
        "complaint": complaint_score,
        "suggestion": suggestion_score,
        "report": report_score,
        "consultation": consultation_score,
        "help": help_score,
    }

    max_score = max(scores.values())
    if max_score == 0:
        return "unknown"

    # 返回得分最高的类型
    for nature, score in scores.items():
        if score == max_score:
            return nature

    return "unknown"


# ---------- 事件抽取 ----------

# 事件大类关键词（基于顺德12345标题和内容）
# 顺序即优先级：靠前的类型优先匹配。
# 注意：
#  - "物业管理"放在"占道经营/交通问题"之前，因为物业工单常同时含"占道/停车"
#    等词，但核心诉求是物业服务（如示例"介入协调处理此物业服务问题"）。
#  - "占道经营"不再使用裸词"占道"，避免把"车辆占道/占道停放"误判成摆卖经营。
_EVENT_TYPES: List[Tuple[str, List[str]]] = [
    ("噪音扰民", ["噪音", "扰民", "喧哗", "音响", "音乐声音", "施工噪音", "半夜", "噪声"]),
    ("拖欠工资", ["拖欠工资", "拖欠薪资", "欠薪", "拖欠工资问题", "拖欠", "克扣工资", "被克扣"]),
    ("劳动纠纷", ["劳动纠纷", "劳动合同", "解除劳动合同", "社保", "失业保险金", "劳动关系", "离职证明"]),
    ("消费纠纷", [
        "消费纠纷", "消费维权", "退款", "退费", "商品质量", "虚假宣传",
        "网购", "网购纠纷", "质量问题", "售后", "维修", "退货", "三包",
        "商家", "超市", "价格", "收费问题", "收费不合理",
    ]),
    ("违法建设", ["违建", "违法建设", "违章建筑", "违法搭建", "违规搭建", "集装箱", "加建", "改建", "扩建"]),
    ("物业管理", [
        "物业服务", "物业公司", "物业管理", "物业问题", "物业履职",
        "物业费", "业委会", "业主委员会",
        "保安", "保洁", "保安和保洁", "保安保洁",
        "停车场管理", "停车管理", "物业收费", "充电桩",
    ]),
    ("房屋质量", ["房屋开裂", "墙体开裂", "墙面开裂", "漏水", "渗水", "塌陷", "下陷", "危房", "房屋质量", "装修"]),
    ("占道经营", ["占道经营", "乱摆卖", "流动摊贩", "占道摆卖", "占道经营问题"]),
    ("交通问题", ["交通", "违停", "乱停", "乱停乱放", "停车", "路阻", "堵车", "冲卡", "外来车辆", "车辆停放", "占道停放", "红绿灯", "逆行", "酒驾"]),
    ("环境卫生", ["垃圾", "卫生", "臭味", "污水", "乱扔", "四害", "灭四害", "下水", "化粪"]),
    ("市政设施", ["路灯", "井盖", "路面", "破损", "道路损坏", "道路施工", "人行道", "围蔽"]),
    ("燃放烟花", ["烟花", "爆竹", "燃放"]),
    ("环境污染", ["污染", "废气", "油烟", "粉尘", "养猪场", "嗅气", "异味", "扬尘"]),
    ("养殖问题", ["养殖", "禽畜", "鸡", "鸭", "狗叫"]),
    ("食品安全", ["食品含异物", "食品安全", "食材问题", "餐饮卫生", "餐馆", "餐厅", "食物中毒", "过期食品"]),
    ("证照办理", [
        "注册登记", "注册问题", "注册登", "注销登记", "设立登记", "企业变更",
        "地址变更", "营业执照", "个体户", "个体工商户", "经营许可", "食品经营许可",
        "特种设备", "证照", "居住证", "身份证", "结婚登记", "户籍", "入户",
    ]),
    ("社保医保", [
        "社保", "医保", "参保", "报销", "养老金", "退休金", "退休", "补贴",
        "失业金", "失业保险", "工伤", "生育保险", "缴费", "减员", "伤残补助",
        "门诊", "门特", "异地就医", "参保缴费",
    ]),
    ("教育问题", ["学校", "老师", "入学", "招生", "中考", "高考", "教育", "幼儿园", "学费", "补课"]),
    ("物流快递", ["物流", "快递", "货运", "托运", "配送"]),
    ("无证经营", ["无证", "无照", "黑车", "非法营运"]),
    ("咨询办理", ["咨询", "申请", "办理", "指引", "疑问", "如何", "流程", "资料补正"]),
]


def classify_event_type(text: str, title: str = "") -> str:
    """分类事件大类。"""
    content = f"{title} {text}"
    for event_type, keywords in _EVENT_TYPES:
        for kw in keywords:
            if kw in content:
                return event_type
    return ""


def extract_event(text: str, title: str = "",
                  org_raw: str = "", addr_raw: str = "") -> Dict[str, str]:
    """抽取事件信息。"""
    event_type = classify_event_type(text, title)
    # 事件主体：优先用抽取到的主体，否则尝试从内容中找
    subject = org_raw
    # 事件行为：基于事件类型推断
    action = ""
    if event_type == "噪音扰民":
        if "喧哗" in text:
            action = "客人喧哗"
        elif "音响" in text or "音乐" in text:
            action = "音响播放"
        elif "施工" in text:
            action = "施工"
        else:
            action = "产生噪音"
    elif event_type == "拖欠工资":
        action = "拖欠薪资"
    elif event_type == "占道经营":
        # 仅当确实有"摆卖/摊贩"时才称"占道摆卖"，否则只是占道经营
        if "摆卖" in text or "摊贩" in text:
            action = "占道摆卖"
        else:
            action = "占道经营"
    elif event_type == "物业管理":
        if "保安" in text or "保洁" in text:
            action = "保安保洁缺位"
        elif "停车" in text or "停车场" in text:
            action = "停车管理不力"
        elif "费" in text:
            action = "物业收费争议"
        else:
            action = "物业服务不到位"
    elif event_type == "交通问题":
        if "冲卡" in text or "外来车辆" in text:
            action = "外来车辆冲卡进入"
        elif "违停" in text or "乱停" in text:
            action = "车辆违停"
        elif "占道停放" in text or "占道" in text:
            action = "车辆占道停放"
        else:
            action = "车辆停放问题"
    elif event_type == "燃放烟花":
        action = "燃放烟花"
    elif event_type == "环境卫生":
        if "垃圾" in text:
            action = "乱丢垃圾"
        elif "污水" in text:
            action = "污水排放"
        elif "四害" in text or "灭四害" in text:
            action = "四害滋生"
        else:
            action = "影响卫生"
    elif event_type == "房屋质量":
        if "开裂" in text:
            action = "墙体开裂"
        elif "漏水" in text or "渗水" in text:
            action = "房屋漏水"
        elif "塌陷" in text or "下陷" in text:
            action = "地面塌陷"
        else:
            action = "房屋质量问题"
    elif event_type == "食品安全":
        if "异物" in text:
            action = "食品含异物"
        elif "餐饮" in text or "餐厅" in text or "餐馆" in text:
            action = "餐饮卫生问题"
        else:
            action = "食品安全问题"
    elif event_type == "证照办理":
        if "注册" in text or "设立" in text:
            action = "企业/个体注册登记"
        elif "注销" in text:
            action = "注销登记"
        elif "变更" in text:
            action = "登记信息变更"
        elif "营业执照" in text:
            action = "营业执照办理"
        else:
            action = "证照办理"
    elif event_type == "社保医保":
        if "报销" in text:
            action = "医保/费用报销"
        elif "退休" in text:
            action = "退休待遇"
        elif "失业" in text:
            action = "失业金申领"
        elif "参保" in text or "缴费" in text:
            action = "参保缴费"
        elif "工伤" in text:
            action = "工伤待遇"
        else:
            action = "社保医保业务"
    elif event_type == "教育问题":
        if "入学" in text or "招生" in text:
            action = "入学招生"
        elif "收费" in text or "学费" in text:
            action = "教育收费"
        elif "老师" in text or "补课" in text:
            action = "教学管理"
        else:
            action = "教育管理问题"
    elif event_type == "物流快递":
        action = "物流快递纠纷"
    elif event_type == "咨询办理":
        action = "咨询/申请办理"
    # 事件对象：通常是受影响的居民
    obj = "附近居民"
    # 事件详情：主体+地点+行为
    detail_parts = []
    if addr_raw:
        detail_parts.append(addr_raw)
    if subject:
        detail_parts.append(subject)
    if action:
        detail_parts.append(action)
    event_detail = "".join(detail_parts) if detail_parts else text[:60]

    return {
        "event_type": event_type,
        "event_subject": subject,
        "event_action": action,
        "event_object": obj,
        "event_detail": event_detail,
    }


# ---------- 诉求抽取 ----------

_REQUEST_KEYWORDS: List[Tuple[str, List[str]]] = [
    ("制止噪音扰民", ["制止噪音", "停止噪音", "解决噪音", "消除噪音", "噪音扰民"]),
    ("制止占道经营", ["清理占道", "取缔占道", "制止占道", "整治摆卖"]),
    ("查处违法建设", ["查处违建", "拆除违建", "拆除违法", "整治违建", "违规搭建", "违法搭建"]),
    ("追讨工资", ["发放工资", "支付工资", "追回工资", "补发工资", "支付薪资", "克扣工资"]),
    ("退还费用", ["退款", "退费", "退还", "退货", "退钱"]),
    ("依法处罚", ["依法处罚", "处罚违法", "依法查处", "处罚"]),
    ("处理交通违停", ["处理违停", "处理乱停", "拖离", "乱停乱放"]),
    ("修复市政设施", ["修复路灯", "修复井盖", "修复路面", "维修", "道路破损", "人行道破损"]),
    ("整治环境卫生", ["清理垃圾", "处理污水", "整治卫生", "灭四害", "除四害"]),
    ("查处食品安全", ["食品含异物", "食品安全", "餐饮卫生", "食材问题"]),
    ("房屋质量整改", ["房屋开裂", "墙体开裂", "房屋漏水", "墙体渗水", "地面塌陷", "房屋质量"]),
    ("协调处理物业服务", ["物业服务问题", "物业问题", "物业履职", "物业管理问题",
                            "协调处理此物业", "处理物业服务", "介入协调处理此物业"]),
    ("补办证照", ["办理营业执照", "注册登记", "注销登记", "设立登记", "证照办理",
                   "补办身份证", "居住证办理", "结婚登记", "营业执照"]),
    ("社保医保报销", ["医保报销", "社保报销", "工伤报销", "门诊报销", "门特报销"]),
    ("投诉商家", ["投诉商家", "商家问题", "消费维权", "网购纠纷", "商品质量", "售后服务"]),
    ("咨询办理", ["咨询", "申请办理", "如何办理", "办理流程", "申请补贴", "疑问",
                   "希望了解", "想了解", "请教"]),
    ("介入处理", ["介入处理", "介入", "处理", "跟进", "核实"]),
]


def extract_request(text: str, event_type: str = "") -> Tuple[str, str]:
    """抽取诉求。返回 (request, issue)。

    event_type 用于 issue 兜底：当无法从关键词推出问题时，回退到事件大类，
    避免 issue 为空。
    """
    if not text:
        return "", ""
    req = ""
    for label, kws in _REQUEST_KEYWORDS:
        for kw in kws:
            if kw in text:
                req = label
                break
        if req:
            break
    if not req:
        # 兜底：如果有"希望"/"要求"，归为"介入处理"
        if any(k in text for k in ("希望", "要求", "请", "建议")):
            req = "介入处理"

    # issue：问题简述
    issue = ""
    for label, kws in _REQUEST_KEYWORDS:
        for kw in kws:
            if kw in text:
                issue = label.replace("制止", "").replace("查处", "").replace("追讨", "").replace("退还", "").replace("处理", "").replace("整治", "").replace("修复", "").replace("介入", "").replace("协调", "").strip()
                break
        if issue:
            break
    # 兜底：issue 为空时，用事件大类补充，避免诉求问题缺失
    if not issue and event_type:
        issue = event_type
    return req, issue
