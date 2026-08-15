# -*- coding: utf-8 -*-
"""
模块 6：事件画像（event_profiler.py）

对每个多频聚类生成“事件对象”，含：主体/类型/区域/频次/首末次出现/样例工单。
（趋势/时间窗口/空间集中度已按需求对齐决策移除）
"""
import pandas as pd

import config
from utils.helpers import truncate


def _mode_or_blank(series: pd.Series) -> str:
    s = series.dropna().astype(str).str.strip()
    s = s[s != ""]
    return s.mode().iloc[0] if not s.empty else ""


def _fmt_seen(ts) -> str:
    if ts.hour == 0 and ts.minute == 0:
        return ts.strftime("%Y-%m-%d")
    return ts.strftime("%Y-%m-%d %H:%M")


def _build_dedup_metrics(g, times_valid):
    """计算多频识别依据：首末间隔/日均频次/独立提交人/主体集中度/区域集中度。"""
    freq = int(len(g))
    if not times_valid.empty:
        span_days = max((times_valid.max() - times_valid.min()).days, 1)
        daily_avg = round(freq / span_days, 2)
        span_label = "%d 天" % span_days
    else:
        daily_avg = 0
        span_label = "未知"
    submitter_n = int(g["submitter"].nunique()) if "submitter" in g.columns else 0

    def _concentration(col):
        if col not in g.columns:
            return 0, 0
        s = g[col].astype(str).str.strip()
        s = s[(s != "") & (s != "nan")]
        if s.empty:
            return 0, 0
        top = s.value_counts().iloc[0]
        return int(top), round(int(top) / len(s) * 100)

    subj_top, subj_pct = _concentration("extracted_subject")
    area_top, area_pct = _concentration("extracted_area")

    def _freq_level(d):
        if d >= 3:
            return "高发"
        if d >= 1:
            return "中频"
        return "低频"

    def _concentrated(pct):
        return "集中" if pct >= 80 else "分散"

    return {
        "span_days": span_label,
        "daily_avg": daily_avg,
        "freq_level": _freq_level(daily_avg),
        "unique_submitters": submitter_n,
        "submitter_type": "同一人反复" if submitter_n <= 2 and freq >= 5 else ("群体反映" if submitter_n >= 10 else "少数人"),
        "subject_top_count": subj_top,
        "subject_concentration": subj_pct,
        "subject_type": _concentrated(subj_pct),
        "area_top_count": area_top,
        "area_concentration": area_pct,
        "area_type": _concentrated(area_pct),
    }


def build_event_profiles(df, max_events=None):
    """将多频聚类转换为事件对象列表（按频次降序）。"""
    multi = df[df["is_multi_freq"]]
    if multi.empty:
        return []

    if max_events is None:
        max_events = getattr(config, "MAX_PROFILE_EVENTS", 500)

    grouped = multi.groupby("cluster_id")
    top_ids = grouped.size().sort_values(ascending=False).head(max_events).index

    events = []
    for idx, cid in enumerate(top_ids, start=1):
        g = grouped.get_group(cid)
        times_valid = g["submit_time"].dropna()

        samples = [
            {"order_id": r.order_id, "content": truncate(r.content, 80)}
            for r in g.head(8).itertuples()
        ]

        subj_series = g.get("extracted_subject", pd.Series()).astype(str).str.strip()
        uniq_subjects = [s for s in set(subj_series) if s]
        if len(uniq_subjects) >= 3:
            subject_label = "多主体聚合（%d处）" % len(uniq_subjects)
        else:
            subject_label = _mode_or_blank(g.get("extracted_subject", pd.Series())) or "（未识别主体）"

        event = {
            "event_id": f"EV{idx:03d}",
            "cluster_id": int(cid),
            "event_subject": subject_label,
            "event_type": _mode_or_blank(g.get("extracted_event", pd.Series())) or "（未识别类型）",
            "area": _mode_or_blank(g.get("extracted_area", pd.Series())) or "（未识别区域）",
            "frequency": int(len(g)),
            "first_seen": _fmt_seen(times_valid.min()) if not times_valid.empty else "未知",
            "last_seen": _fmt_seen(times_valid.max()) if not times_valid.empty else "未知",
            "sample_orders": samples,
            "dedup_metrics": _build_dedup_metrics(g, times_valid),
        }
        if "submitter" in g.columns:
            event["unique_submitters"] = int(g["submitter"].nunique())
        events.append(event)

    events.sort(key=lambda e: e["frequency"], reverse=True)
    for i, e in enumerate(events, start=1):
        e["event_id"] = f"EV{i:03d}"
    return events
