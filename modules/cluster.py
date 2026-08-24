# -*- coding: utf-8 -*-
"""
模块 4：聚类（cluster.py）【核心识别引擎】

默认路径（完全离线）：
    拼接文本 → jieba 分词 → TF-IDF(ngram=1~2) → 余弦距离 → DBSCAN

可选路径：
    从本地 MODEL_DIR 加载 Embedding 模型（绝不联网下载）；缺失自动回退 TF-IDF。

兜底路径：
    聚类结果异常（无有效簇 / 覆盖率过低 / 明显碎片化）时，
    回退为 extracted_subject + extracted_event + extracted_area 的规则分组。

目标不是数学上最优的聚类，而是稳定产出业务上可解释的事件。
"""
import os
import re

import numpy as np
import pandas as pd
import jieba
from sklearn.cluster import DBSCAN
from sklearn.feature_extraction.text import TfidfVectorizer
from sklearn.metrics.pairwise import cosine_similarity

import config

# 覆盖率低于该值视为“聚类质量差”，触发规则分组兜底
MIN_COVERAGE = 0.3

# jieba 自定义词典只加载一次
_JIEBA_DICT_LOADED = False


def _load_jieba_dicts():
    """把本地事件/主体词典注入 jieba，避免“占道经营”等词被切碎。"""
    global _JIEBA_DICT_LOADED
    if _JIEBA_DICT_LOADED:
        return
    from utils.helpers import load_dict_lines
    for fname in ("events.txt", "subjects.txt"):
        for term in load_dict_lines(fname):
            if len(term) >= 2:
                jieba.add_word(term)
    _JIEBA_DICT_LOADED = True


def _build_text(df):
    """拼接：标准化内容 + 主体 + 事件 + 区域（实体字段按配置分字段加权）。"""
    content = df.get("normalized_content", "").fillna("").astype(str)
    weights = {
        "extracted_subject": max(int(getattr(config, "TEXT_WEIGHT_SUBJECT", 1)), 0),
        "extracted_event": max(int(getattr(config, "TEXT_WEIGHT_EVENT", 2)), 0),
        "extracted_area": max(int(getattr(config, "TEXT_WEIGHT_AREA", 1)), 0),
    }
    parts = [content]
    for field, w in weights.items():
        col = df.get(field, "").fillna("").astype(str)
        parts.extend([col] * w)
    return parts[0].str.cat(parts[1:], sep=" ")


def _embed_local(texts: list) -> np.ndarray:
    """从本地加载 Embedding 模型并编码；失败抛异常由上层兜底。"""
    model_path = os.path.join(config.MODEL_DIR, config.EMBEDDING_MODEL)
    if not os.path.isdir(model_path):
        raise FileNotFoundError("本地 Embedding 模型不存在：%s" % model_path)
    # 延迟导入：未启用 Embedding 时不引入重依赖
    from sentence_transformers import SentenceTransformer  # noqa
    model = SentenceTransformer(model_path)
    return model.encode(texts, normalize_embeddings=True)


def _rule_group_labels(df):
    """规则分组兜底：按 主体+事件+区域 分组；关键字段全空的记为噪声 -1。"""
    keys = list(zip(
        df.get("extracted_subject", "").fillna("").astype(str),
        df.get("extracted_event", "").fillna("").astype(str),
        df.get("extracted_area", "").fillna("").astype(str),
    ))
    key_to_id, labels = {}, []
    for k in keys:
        if all(not x.strip() for x in k):
            labels.append(-1)
            continue
        if k not in key_to_id:
            key_to_id[k] = len(key_to_id)
        labels.append(key_to_id[k])
    return np.array(labels)


# 标题开头的部门/分类标签，如 “（城管）商业噪音” 中的 “（城管）”
_TITLE_TAG_RE = re.compile(r"^[（(][^（）()]{1,15}[）)]\s*")
# 标题开头的动作前缀，剥离后得到事件本体
_TITLE_PREFIX_RE = re.compile(r"^(再次反映|继续反映|重复反映|反映|投诉|举报|咨询|求助)+\s*")


def _clean_title_series(df):
    """
    产出用于分组的“清洗后标题”：
    normalized_title → 去（部门）标签 → 去动作前缀 → 去空白。

    真实数据的标题本身即人工归类的事件标签，是最可靠的分组键。
    """
    if "normalized_title" in df.columns:
        titles = df["normalized_title"].fillna("").astype(str)
    elif "title" in df.columns:
        titles = df["title"].fillna("").astype(str)
    else:
        return pd.Series([""] * len(df), index=df.index)

    cleaned = titles.str.strip()
    cleaned = cleaned.str.replace(_TITLE_TAG_RE, "", regex=True)
    cleaned = cleaned.str.replace(_TITLE_PREFIX_RE, "", regex=True)
    cleaned = cleaned.str.strip()
    return cleaned


def _title_group_labels(df):
    """
    大数据路线：按清洗后标题分组（O(n) 线性，可承载十万级数据）。

    标题为空的记为噪声 -1。返回 labels 数组。
    """
    cleaned = _clean_title_series(df)
    codes, _uniques = pd.factorize(cleaned, sort=False)
    labels = codes.copy()
    labels[cleaned.values == ""] = -1
    return labels.astype(int)


# ---------- 大数据路线（多频对齐版）：按 (归一事件, 主体键) 分组 ----------

# 泛化主体后缀：镇街/社区/村级，不能单独作为"具体主体"键（需地址/公司信号补充）
_GENERIC_SUBJ_SUFFIX = ("街道", "镇", "社区", "村", "居委会", "村委会")
# 地址"地标词"：含地标才算具体地址信号，否则只到镇街级
_ADDR_LANDMARK = ("小区", "花园", "公寓", "公馆", "家园", "雅苑", "名居", "庭院",
                  "新城", "楼盘", "华庭", "市场", "广场", "大厦", "大楼", "写字楼",
                  "中心", "工业区", "工业园", "公园", "路", "街", "大道", "巷", "号",
                  "村", "社区", "苑", "府", "店", "铺", "酒店", "宾馆", "超市", "商场")


def _apply_synonyms(text: str, syn: dict) -> str:
    """按词典做同义词/别名归一（长词优先已在 load_synonyms 处理）。"""
    for raw, std in syn.items():
        if raw and raw in text:
            text = text.replace(raw, std)
    return text


# 系统性事件（政务平台/社保医保类）：跨主体按事件归并（不同市民反映同一系统问题=同一事件）
_SYSTEMIC_EVENTS = {"失业保险金"}

# 场所专名后缀（提取"高黎市场"这类场所，作为同一主体/场所的分组键）
_VENUE_SUFFIXES = ("商业街", "步行街", "美食城", "工业园", "工业区", "大排档", "烧烤店",
                   "市场", "广场", "大厦", "大楼", "公园", "码头", "中心", "商场", "超市",
                   "百货", "夜市", "车站", "酒店", "宾馆", "医院", "学校", "小学", "中学",
                   "幼儿园", "餐厅", "饭店", "酒吧", "KTV", "ktv")
# 场所名前的边界词：向左扫描遇到即停止（避免把"高黎社区/街道"并入"高黎市场"）
_VENUE_BOUNDARY = "区社村号路街巷道门口的在场旁附近正现于（()），,、。;；"


def _extract_venues(text: str) -> list:
    """扫描文本，提取所有"场所名+后缀"候选（名称取后缀前 1..8 字，遇边界词截断）。"""
    found = []
    for suf in _VENUE_SUFFIXES:
        start = 0
        while True:
            i = text.find(suf, start)
            if i < 0:
                break
            j = i
            chars = []
            while j > 0 and text[j - 1] >= "\u4e00" and text[j - 1] <= "\u9fa5" \
                    and text[j - 1] not in _VENUE_BOUNDARY and len(chars) < 8:
                j -= 1
                chars.insert(0, text[j])
            name = "".join(chars)
            if len(name) >= 2:
                found.append(name + suf)
            start = i + 1
    return found


def _best_venue(text: str) -> str:
    """提取最紧凑的场所专名（如"高黎市场"）；无则空。"""
    found = _extract_venues(text or "")
    if not found:
        return ""
    return min(found, key=len)


def _subject_key_series(df):
    """
    构造聚类用"主体键"（业务口径：同一管理对象/场所的工单应归为同一事件）。

    优先级：具体主体（公司/小区/场所，非镇街级） > 地址/内容中的场所专名 >
            含地标地址 > 社区+楼栋 > 镇街；无具体信号为空（系统性事件按事件归并）。
    """
    subj = df.get("extracted_subject", pd.Series(index=df.index, dtype=str)).fillna("").astype(str).str.strip()
    addr = df.get("addr_norm", pd.Series(index=df.index, dtype=str)).fillna("").astype(str).str.strip()
    comm = df.get("addr_community", pd.Series(index=df.index, dtype=str)).fillna("").astype(str).str.strip()
    bld = df.get("addr_building", pd.Series(index=df.index, dtype=str)).fillna("").astype(str).str.strip()
    content = df.get("content", pd.Series(index=df.index, dtype=str)).fillna("").astype(str).str.strip()
    town = df.get("extracted_area", pd.Series(index=df.index, dtype=str)).fillna("").astype(str).str.strip()

    keys = []
    for s, a, c, b, t, ct in zip(subj, addr, comm, bld, town, content):
        if s and len(s) >= 2 and not s.endswith(_GENERIC_SUBJ_SUFFIX):
            keys.append(s)
            continue
        venue = _best_venue(a) or _best_venue(ct)
        if venue:
            keys.append(venue)
            continue
        a_clean = a.replace("街道", "")
        if a and len(a) >= 4 and any(k in a_clean for k in _ADDR_LANDMARK):
            keys.append(a)
            continue
        if c and b:
            keys.append(c + b)
            continue
        if c:
            keys.append(c)
            continue
        if t:
            keys.append(t)
            continue
        keys.append("")
    return pd.Series(keys, index=df.index)


def _bigdata_labels(df):
    """
    判重分组键（所有规模统一使用）：归一事件（标题/事件类型经同义词归一） + 主体键。

    - 事件键 = 标题即人工事件标签，经语义归一规则库对齐（同一事件的不同写法归为同一键）；
      标题缺失时用抽取的事件类型兜底；
    - 主体键 = 同一管理对象/场所（亦做别名归一，如 中电建九局→中电建）；
    - 系统性事件（失业保险金等）跨主体按事件归并；
    - 无具体主体信号时仅按事件归并。
    返回 labels 数组（空键记为噪声 -1）。
    """
    from utils.helpers import load_synonyms
    syn = load_synonyms()
    cleaned = _clean_title_series(df)
    # 事件键兜底：标题整列缺失时，改用抽取的事件类型（避免全空导致全噪声）
    if cleaned.eq("").all() and "extracted_event" in df.columns:
        cleaned = df["extracted_event"].fillna("").astype(str).str.strip()
    canonical = cleaned.apply(lambda t: _apply_synonyms(t, syn))
    subj_key = _subject_key_series(df).apply(lambda s: _apply_synonyms(s, syn))

    keys = []
    for ev, sk in zip(canonical, subj_key):
        if ev:
            if ev in _SYSTEMIC_EVENTS:
                keys.append(ev)
            else:
                keys.append(ev + "|" + sk if sk else ev)
        else:
            keys.append("")
    codes, _uniques = pd.factorize(pd.Series(keys, index=df.index), sort=False)
    labels = codes.astype(int)
    labels[canonical.values == ""] = -1
    return labels


def _consolidate(df, labels):
    """
    聚类后的规则归并（可解释的业务修正）：

    1) 同主体碎片簇合并：两个簇的代表主体相同，且代表事件兼容
       （其一为空或两者相同）时合并——同一管理对象的工单不应被拆散；
    2) 噪声点回收：噪声工单若带有明确的“事件+区域”签名，
       且与某个已有簇的签名一致，则归入该簇（同区域同类事件即同一事件）。

    返回 (新labels, 归并说明列表)。
    """
    labels = labels.copy()
    notes = []
    ids = sorted(set(labels.tolist()) - {-1})

    def cluster_mode(cid, field):
        vals = df.loc[labels == cid, field]
        vals = vals[vals.astype(str).str.strip() != ""]
        return vals.mode().iloc[0] if not vals.empty else ""

    # ---- 1) 同主体碎片簇合并 ----
    merged_into = {cid: cid for cid in ids}
    subj_map = {cid: cluster_mode(cid, "extracted_subject") for cid in ids}
    ev_map = {cid: cluster_mode(cid, "extracted_event") for cid in ids}
    for cid in ids:
        target = merged_into[cid]
        if target != cid:
            continue
        for other in ids:
            if other == cid or merged_into[other] != other:
                continue
            same_subj = subj_map[cid] and subj_map[cid] == subj_map[other]
            ev_ok = (not ev_map[cid]) or (not ev_map[other]) or (ev_map[cid] == ev_map[other])
            if same_subj and ev_ok:
                merged_into[other] = cid
                notes.append("已合并同主体碎片簇：%s" % subj_map[cid])
    for cid in ids:
        if merged_into[cid] != cid:
            labels[labels == cid] = merged_into[cid]

    # 重新整理簇号（紧凑化）
    ids = sorted(set(labels.tolist()) - {-1})

    # ---- 2) 噪声点回收（事件+区域签名一致） ----
    sig_map = {}
    for cid in ids:
        ev = cluster_mode(cid, "extracted_event")
        ar = cluster_mode(cid, "extracted_area")
        if ev and ar:
            sig_map.setdefault((ev, ar), cid)
    noise_idx = np.where(labels == -1)[0]
    recovered = 0
    for i in noise_idx:
        ev = str(df.loc[i, "extracted_event"] or "").strip() if "extracted_event" in df else ""
        ar = str(df.loc[i, "extracted_area"] or "").strip() if "extracted_area" in df else ""
        if ev and ar and (ev, ar) in sig_map:
            labels[i] = sig_map[(ev, ar)]
            recovered += 1
    if recovered:
        notes.append("按事件+区域签名回收噪声工单 %d 条。" % recovered)

    return labels, notes


def _merge_by_signature(df, labels):
    """
    签名归并：簇代表（事件类型众数+区域众数）相同的簇合并为同一事件。

    业务口径：同区域+同问题类型 = 同一多频事件（如「拖欠工资」与「拖欠工资问题」标题
    仅差通用后缀，属同一事件，频次合并计数）。关键字段全空的簇不参与归并。
    """
    labels = labels.copy()

    def mode_of(cid, field):
        if field not in df.columns:
            return ""
        vals = df.loc[labels == cid, field].astype(str).str.strip()
        vals = vals[vals != ""]
        return vals.mode().iloc[0] if not vals.empty else ""

    ids = sorted(set(labels.tolist()) - {-1})
    sig_first, remap = {}, {}
    for cid in ids:
        sig = (mode_of(cid, "extracted_event"), mode_of(cid, "extracted_area"))
        if not sig[0] or not sig[1]:
            # 事件类型或区域任一缺失则不参与归并，避免跨类型/跨区域误并成巨簇
            remap[cid] = cid
            continue
        if sig in sig_first:
            remap[cid] = sig_first[sig]
        else:
            sig_first[sig] = cid
            remap[cid] = cid
    n_merged = sum(1 for c, t in remap.items() if t != c)
    for cid in ids:
        if remap[cid] != cid:
            labels[labels == cid] = remap[cid]
    return labels, n_merged


def cluster_orders(df, eps=None, min_samples=None, use_embedding=None):
    """
    对工单进行聚类，返回 (df_含cluster_id, info字典)。

    info 包含：method（实际使用的路线）、n_clusters、coverage、
    fallback_used、message（供前端展示的说明）。
    """
    eps = config.CLUSTER_EPS if eps is None else eps
    min_samples = config.CLUSTER_MIN_SAMPLES if min_samples is None else min_samples
    use_embedding = config.USE_EMBEDDING if use_embedding is None else use_embedding

    df = df.copy().reset_index(drop=True)
    info = {"fallback_used": False, "messages": []}

    # ---- 判重路线：统一走「归一事件+主体键」分组（O(n) 线性，任意规模精度一致） ----
    # 仅当连事件信号都缺失（无标题且无事件类型）时才回退 DBSCAN 内容相似度
    _has_title = "title" in df.columns or "normalized_title" in df.columns
    _has_event = False
    if not _has_title and "extracted_event" in df.columns:
        _has_event = bool(df["extracted_event"].astype(str).str.strip().ne("").any())
    if _has_title or _has_event:
        labels = _bigdata_labels(df)
        # 事件键即归一后的清洗标题（标题即事件分类）；空事件行回填归一标题保证下游有值
        cleaned = _clean_title_series(df)
        if cleaned.eq("").all() and "extracted_event" in df.columns:
            cleaned = df["extracted_event"].fillna("").astype(str).str.strip()
        from utils.helpers import load_synonyms
        syn = load_synonyms()
        canonical = cleaned.apply(lambda t: _apply_synonyms(t, syn))
        if "extracted_event" not in df.columns:
            df["extracted_event"] = ""
        ev_empty = df["extracted_event"].astype(str).str.strip() == ""
        df.loc[ev_empty, "extracted_event"] = canonical[ev_empty].values
        valid = labels[labels != -1]
        info.update({
            "method": "判重分组（归一事件+主体键）",
            "fallback_used": False,
            "n_clusters": len(set(valid.tolist())) if len(valid) else 0,
            "coverage": float(len(valid) / len(labels)) if len(labels) else 0.0,
            "noise_count": int((labels == -1).sum()),
        })
        info["messages"].append(
            "判重按「归一事件+主体」分组：同一主体/同一事件的多写法工单归并为同一多频事件。")
        df["cluster_id"] = labels
        return df, info

    texts = _build_text(df).fillna("").tolist()

    labels = None
    method = "TF-IDF + 余弦距离 + DBSCAN"

    # ---- 可选 Embedding 路径（仅本地） ----
    if use_embedding:
        try:
            vecs = _embed_local(texts)
            dist = 1.0 - cosine_similarity(vecs)
            labels = DBSCAN(eps=eps, min_samples=min_samples,
                            metric="precomputed").fit_predict(np.clip(dist, 0, 1))
            method = "本地Embedding + 余弦距离 + DBSCAN"
        except Exception as e:
            info["messages"].append("本地模型不可用，已自动切换 TF-IDF。（%s）" % e)
            labels = None

    # ---- 默认 TF-IDF 路径 ----
    if labels is None:
        _load_jieba_dicts()
        tokenized = [" ".join(jieba.lcut(t)) for t in texts]
        vectorizer = TfidfVectorizer(ngram_range=(1, 2), min_df=1)
        try:
            X = vectorizer.fit_transform(tokenized)
            dist = 1.0 - cosine_similarity(X)
            labels = DBSCAN(eps=eps, min_samples=min_samples,
                            metric="precomputed").fit_predict(np.clip(dist, 0, 1))
        except Exception as e:
            info["messages"].append("TF-IDF 聚类失败，启用规则分组兜底。（%s）" % e)
            labels = None

    # ---- 质量评估：是否需要规则兜底 ----
    if labels is not None:
        valid = labels[labels != -1]
        n_clusters = len(set(valid.tolist())) if len(valid) else 0
        coverage = float(len(valid) / len(labels)) if len(labels) else 0.0
        fragmented = n_clusters > max(3, int(0.5 * len(labels)))

        if config.FALLBACK_RULE_GROUP and (n_clusters == 0 or coverage < MIN_COVERAGE or fragmented):
            info["messages"].append(
                "聚类质量不佳（有效簇=%d，覆盖率=%.0f%%），已回退规则分组。" % (
                    n_clusters, coverage * 100))
            labels = None

    if labels is None:
        labels = _rule_group_labels(df)
        method = "规则分组兜底（主体+事件+区域）"
        info["fallback_used"] = True

    # ---- 聚类后规则归并：合并同主体碎片簇、回收同签名噪声点 ----
    if getattr(config, "CONSOLIDATE_BY_RULES", True):
        labels, notes = _consolidate(df, labels)
        info["messages"].extend(notes)

    df["cluster_id"] = labels
    valid = labels[labels != -1]
    info.update({
        "method": method,
        "n_clusters": len(set(valid.tolist())) if len(valid) else 0,
        "coverage": float(len(valid) / len(labels)) if len(labels) else 0.0,
        "noise_count": int((labels == -1).sum()),
    })
    return df, info
