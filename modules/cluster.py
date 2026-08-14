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

    # ---- 规模分流：超大数据走标题规则分组（DBSCAN 距离矩阵 O(n²) 不可行） ----
    max_rows = getattr(config, "CLUSTER_MAX_ROWS", 15000)
    if len(df) > max_rows and ("title" in df.columns or "normalized_title" in df.columns):
        labels = _title_group_labels(df)
        # 标题即事件分类：词典事件为空的行用清洗后标题回填，保证下游事件类型有值
        cleaned = _clean_title_series(df)
        if "extracted_event" not in df.columns:
            df["extracted_event"] = ""
        ev_empty = df["extracted_event"].astype(str).str.strip() == ""
        df.loc[ev_empty, "extracted_event"] = cleaned[ev_empty].values
        valid = labels[labels != -1]
        info.update({
            "method": "标题规则分组（大数据路线）",
            "fallback_used": False,
            "n_clusters": len(set(valid.tolist())) if len(valid) else 0,
            "coverage": float(len(valid) / len(labels)) if len(labels) else 0.0,
            "noise_count": int((labels == -1).sum()),
        })
        info["messages"].append(
            "数据量 %d 条超过聚类适用规模（%d），已自动切换标题规则分组路线。" % (
                len(df), max_rows))
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
