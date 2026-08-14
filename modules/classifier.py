# -*- coding: utf-8 -*-
"""
模块 5：多频事件识别（classifier.py）

职责：
- 统计每个聚类的工单数量（cluster_id=-1 不参与多频判断）
- 工单数 >= FREQ_THRESHOLD 的聚类判定为“多频事件”
- 输出：is_multi_freq / cluster_size / cluster_subject / cluster_event / cluster_area

注意：本模块只回答“是不是多频”，不回答“是不是高风险”。
实现全部向量化，十万级数据秒级完成。
"""
import pandas as pd

import config


def _mode_or_blank(series: pd.Series) -> str:
    """取一列中出现最多的非空值；全空时返回空字符串。"""
    s = series.dropna().astype(str).str.strip()
    s = s[s != ""]
    if s.empty:
        return ""
    return s.mode().iloc[0]


def _group_mode_map(valid: pd.DataFrame, field: str) -> dict:
    """
    向量化求每个簇在某字段的众数（非空），返回 {cluster_id: 众数}。

    用 (cluster_id, 值) 双键 groupby 计数再取每组最大，
    避免逐簇 Python 循环（十万级数据性能关键）。
    """
    if field not in valid.columns:
        return {}
    vals = valid[field].astype(str).str.strip()
    nonempty = vals[vals != ""]
    if nonempty.empty:
        return {}
    cids = valid.loc[nonempty.index, "cluster_id"]
    counts = pd.Series(1, index=nonempty.index).groupby([cids, nonempty]).sum()
    idx = counts.groupby(level=0).idxmax()
    # idx 的每项是 (cluster_id, 值) 元组
    return {int(k): v[1] for k, v in idx.items()}


def classify_multi_freq(df, freq_threshold=None):
    """
    识别多频事件，返回追加了识别字段的 DataFrame。

    freq_threshold 支持 UI 动态传入；默认取 config.FREQ_THRESHOLD。
    """
    freq_threshold = config.FREQ_THRESHOLD if freq_threshold is None else int(freq_threshold)
    df = df.copy()

    # 每个簇的规模（噪声 -1 不计入多频判断，但保留 size 便于展示）
    size_map = df.groupby("cluster_id").size()
    df["cluster_size"] = df["cluster_id"].map(size_map).fillna(0).astype(int)

    valid_sizes = size_map.drop(index=-1, errors="ignore")
    multi_ids = valid_sizes[valid_sizes >= freq_threshold].index.tolist()
    df["is_multi_freq"] = df["cluster_id"].isin(multi_ids)

    # 每个簇的代表性 主体/事件/区域（向量化众数）
    valid = df[df["cluster_id"] != -1]
    subj_map = _group_mode_map(valid, "extracted_subject")
    ev_map = _group_mode_map(valid, "extracted_event")
    area_map = _group_mode_map(valid, "extracted_area")

    df["cluster_subject"] = df["cluster_id"].map(subj_map).fillna("")
    df["cluster_event"] = df["cluster_id"].map(ev_map).fillna("")
    df["cluster_area"] = df["cluster_id"].map(area_map).fillna("")

    return df, multi_ids
