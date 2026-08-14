# -*- coding: utf-8 -*-
"""
模块 5：多频事件识别（classifier.py）

职责：
- 统计每个聚类的工单数量（cluster_id=-1 不参与多频判断）
- 工单数 >= FREQ_THRESHOLD 的聚类判定为“多频事件”
- 输出：is_multi_freq / cluster_size / cluster_subject / cluster_event / cluster_area

注意：本模块只回答“是不是多频”，不回答“是不是高风险”。
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


def classify_multi_freq(df, freq_threshold=None):
    """
    识别多频事件，返回追加了识别字段的 DataFrame。

    freq_threshold 支持 UI 动态传入；默认取 config.FREQ_THRESHOLD。
    """
    freq_threshold = config.FREQ_THRESHOLD if freq_threshold is None else int(freq_threshold)
    df = df.copy()

    # 每个簇的规模（噪声 -1 不计入多频判断，但保留 size 便于展示）
    size_map = df.groupby("cluster_id").size().to_dict()
    df["cluster_size"] = df["cluster_id"].map(size_map).fillna(0).astype(int)

    valid = df[df["cluster_id"] != -1]
    multi_ids = (
        valid.groupby("cluster_id")
        .size()
        .loc[lambda s: s >= freq_threshold]
        .index
        .tolist()
    )
    df["is_multi_freq"] = df["cluster_id"].isin(multi_ids)

    # 每个簇的代表性 主体/事件/区域（取众数）
    subj_map, ev_map, area_map = {}, {}, {}
    for cid, g in valid.groupby("cluster_id"):
        subj_map[cid] = _mode_or_blank(g.get("extracted_subject", pd.Series()))
        ev_map[cid] = _mode_or_blank(g.get("extracted_event", pd.Series()))
        area_map[cid] = _mode_or_blank(g.get("extracted_area", pd.Series()))

    df["cluster_subject"] = df["cluster_id"].map(subj_map).fillna("")
    df["cluster_event"] = df["cluster_id"].map(ev_map).fillna("")
    df["cluster_area"] = df["cluster_id"].map(area_map).fillna("")

    return df, multi_ids
