# -*- coding: utf-8 -*-
"""
模块 6：事件画像（event_profiler.py）【V2.0 新增】

对每个多频聚类生成管理视角的“事件对象”，包含：
- 基础画像：主体/类型/区域/频次/首末次出现/样例工单
- 时间窗口统计：最近24小时、最近7天、整体
- 趋势判断：近期频次 vs 基线频次 → 上升/平稳/下降/无法判断

时间数据不足时输出“无法判断”，不编造趋势。
"""
from datetime import timedelta

import pandas as pd

import config
from utils.helpers import truncate


def _mode_or_blank(series: pd.Series) -> str:
    s = series.dropna().astype(str).str.strip()
    s = s[s != ""]
    return s.mode().iloc[0] if not s.empty else ""


def _compute_trend(times: pd.Series, ref_now: pd.Timestamp):
    """
    基于近期窗口与基线窗口的日均频次比较，返回趋势标签。

    基线窗口按事件自身活跃期自适应：
    - 基线起点不早于事件首次出现时间，新爆发事件不会被误判；
    - 时间跨度不足以对比时返回“无法判断”，不编造趋势。
    """
    times = times.dropna()
    if times.empty or pd.isna(ref_now):
        return "无法判断"

    recent_days = config.TREND_RECENT_DAYS
    baseline_days = config.TREND_BASELINE_DAYS

    span_days = (ref_now - times.min()).days
    # 跨度不足近期窗口时，缺少对比基础
    if span_days < recent_days:
        return "无法判断"

    recent_start = ref_now - timedelta(days=recent_days)
    # 基线长度 = min(配置基线, 事件活跃期剩余天数)，保证窗口落在事件活跃期内
    baseline_len = min(baseline_days, span_days - recent_days + 1)
    baseline_start = recent_start - timedelta(days=baseline_len)

    recent_cnt = int(((times > recent_start) & (times <= ref_now)).sum())
    baseline_cnt = int(((times > baseline_start) & (times <= recent_start)).sum())

    recent_rate = recent_cnt / recent_days
    baseline_rate = baseline_cnt / baseline_len

    if baseline_cnt == 0:
        # 基线期无工单、近期集中出现 → 视为新爆发
        return "上升" if recent_cnt >= 2 else "平稳"

    ratio = recent_rate / baseline_rate
    if ratio >= config.TREND_RISING_RATIO:
        return "上升"
    if ratio <= config.TREND_DECLINING_RATIO:
        return "下降"
    return "平稳"


def _fmt_seen(ts) -> str:
    """首末出现时间展示：仅日期（午夜，来自工单编号解析）时不伪造时分。"""
    if ts.hour == 0 and ts.minute == 0:
        return ts.strftime("%Y-%m-%d")
    return ts.strftime("%Y-%m-%d %H:%M")


def build_event_profiles(df, ref_now=None, max_events=None):
    """
    将多频聚类转换为事件对象列表（按频次降序）。

    ref_now：趋势计算的时间基准，默认取数据集最近一条工单的时间。
    max_events：最多画像事件数（默认取 config.MAX_PROFILE_EVENTS），
    超大数据下仅处理频次最高的 Top N 簇，保证流水线时效。
    """
    multi = df[df["is_multi_freq"]]
    if multi.empty:
        return []

    if max_events is None:
        max_events = getattr(config, "MAX_PROFILE_EVENTS", 500)

    if ref_now is None:
        valid_times = df["submit_time"].dropna()
        ref_now = valid_times.max() if not valid_times.empty else pd.Timestamp.now()

    grouped = multi.groupby("cluster_id")
    # 频次降序取 Top N 簇
    top_ids = grouped.size().sort_values(ascending=False).head(max_events).index

    events = []
    for idx, cid in enumerate(top_ids, start=1):
        g = grouped.get_group(cid)
        times = g["submit_time"]
        times_valid = times.dropna()

        # 时间窗口统计
        last_24h = int((times_valid > ref_now - timedelta(days=1)).sum()) if not times_valid.empty else 0
        last_7d = int((times_valid > ref_now - timedelta(days=7)).sum()) if not times_valid.empty else 0

        # 样例工单（最多 8 条，供前端溯源展示）
        samples = [
            {"order_id": r.order_id, "content": truncate(r.content, 80)}
            for r in g.head(8).itertuples()
        ]

        # 主体展示：同事件涉及多个主体时不冒用单一主体名义
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
            "last_24h": last_24h,
            "last_7d": last_7d,
            "area_count": int(g["extracted_area"].replace("", pd.NA).dropna().nunique()),
            "sample_orders": samples,
            "trend": _compute_trend(times, ref_now),
        }
        # 可选：真实数据若提供提交人字段则统计
        if "submitter" in g.columns:
            event["unique_submitters"] = int(g["submitter"].nunique())
        events.append(event)

    # 频次降序，并重排 event_id 保证稳定展示
    events.sort(key=lambda e: e["frequency"], reverse=True)
    for i, e in enumerate(events, start=1):
        e["event_id"] = f"EV{i:03d}"
    return events
