# -*- coding: utf-8 -*-
"""
模块 7：风险与优先级判断（risk_analyzer.py）【V2.0 新增】

把“多频”转化为“值得多大程度关注”。
不使用复杂训练模型，仅使用可解释规则评分：

    priority_score = 频次分 + 趋势分 + 空间集中度分 + 敏感度分（权重集中在 config）

风险等级是“管理优先级”，不是安全事故定性；
无充分依据时保守输出；任何异常降级为“需人工研判”，绝不让页面崩溃。
"""
import pandas as pd

import config
from utils.helpers import load_dict_lines

# 趋势 -> 分数（可解释的固定映射）
TREND_SCORE = {"上升": 1.0, "平稳": 0.4, "下降": 0.1, "无法判断": 0.3}


def _frequency_score(freq: int) -> float:
    """频次分：以阈值4倍为满分基准，线性封顶。"""
    base = max(config.FREQ_THRESHOLD * 4, 1)
    return min(1.0, freq / base)


def _area_concentration(df_cluster: pd.DataFrame) -> float:
    """空间集中度：最大区域占比越高，得分越高。"""
    areas = df_cluster.get("extracted_area", pd.Series()).astype(str).str.strip()
    areas = areas[areas != ""]
    if areas.empty:
        return 0.2  # 区域信息缺失，保守给低分
    ratio = areas.value_counts(normalize=True).iloc[0]
    if ratio >= 0.8:
        return 1.0
    if ratio >= 0.6:
        return 0.6
    return 0.3


def _sensitivity(df_cluster: pd.DataFrame, sensitive_terms: list) -> list:
    """返回该簇工单命中的敏感词列表（未命中为空）。最多扫描前300条，防超大簇卡顿。"""
    if not sensitive_terms:
        return []
    texts = (
        df_cluster.get("normalized_content", df_cluster.get("content", pd.Series()))
        .fillna("").astype(str)
    )
    texts = texts.head(300)
    hits = []
    joined = " ".join(texts.tolist())
    for term in sensitive_terms:
        if term in joined and term not in hits:
            hits.append(term)
    return hits


def _build_reason(ev: dict, area_ratio_text: str, sensitive_hits: list) -> str:
    """用真实数据拼装风险原因，不使用模板化空话。"""
    parts = []
    parts.append("共出现%d次（近7天%d次、近24小时%d次）" % (
        ev["frequency"], ev.get("last_7d", 0), ev.get("last_24h", 0)))

    trend = ev.get("trend", "无法判断")
    if trend == "上升":
        parts.append("近%d天频次明显上升" % config.TREND_RECENT_DAYS)
    elif trend == "下降":
        parts.append("近期频次回落")
    elif trend == "平稳":
        parts.append("频次保持平稳")
    else:
        parts.append("时间数据不足，趋势暂无法判断")

    if area_ratio_text:
        parts.append(area_ratio_text)
    if sensitive_hits:
        parts.append("涉及敏感要素：%s" % "、".join(sensitive_hits[:3]))
    return "；".join(parts) + "。"


def analyze_risks(events: list, df: pd.DataFrame) -> list:
    """
    为每个事件对象补充：
    priority_score / risk_level / risk_reason / score_breakdown。

    单事件异常不影响其他事件，异常事件降级为“需人工研判”。
    """
    sensitive_terms = load_dict_lines("sensitive_events.txt")
    w = {
        "frequency": config.RISK_WEIGHT_FREQUENCY,
        "trend": config.RISK_WEIGHT_TREND,
        "area": config.RISK_WEIGHT_AREA,
        "sensitivity": config.RISK_WEIGHT_SENSITIVITY,
    }

    # 只对事件涉及的簇分组，避免全量簇物化（十万级数据性能关键）
    needed = set(ev["cluster_id"] for ev in events)
    sub = df[df["cluster_id"].isin(needed)]
    grouped = dict(tuple(sub.groupby("cluster_id")))

    # 频次分相对基数：本批最大事件频次（小数据退化为阈值4倍）
    max_freq = max([ev["frequency"] for ev in events] + [1])
    freq_base = max(config.FREQ_THRESHOLD * 4, max_freq)
    # 大体量保底阈值：最大事件的 10% 且不低于 50
    volume_floor = max(50, int(0.1 * max_freq))

    for ev in events:
        try:
            g = grouped.get(ev["cluster_id"])
            if g is None or g.empty:
                raise KeyError("cluster 缺失")

            f_score = min(1.0, ev["frequency"] / freq_base)
            t_score = TREND_SCORE.get(ev.get("trend", "无法判断"), 0.3)
            a_score = _area_concentration(g)
            hits = _sensitivity(g, sensitive_terms)
            s_score = 1.0 if hits else 0.0

            score = 100 * (
                w["frequency"] * f_score
                + w["trend"] * t_score
                + w["area"] * a_score
                + w["sensitivity"] * s_score
            )
            ev["priority_score"] = round(score, 1)

            if score >= 70:
                ev["risk_level"] = "高关注"
            elif score >= 40:
                ev["risk_level"] = "中关注"
            else:
                ev["risk_level"] = "一般"

            # 大体量保底：头部问题不因趋势平稳而被小簇高趋势压过
            if ev["frequency"] >= volume_floor and ev["risk_level"] != "高关注":
                ev["risk_level"] = "高关注"
                ev["risk_reason_extra"] = "该事件体量进入本批前10%%（≥%d次），列为高关注。" % volume_floor

            # 集中度描述
            areas = g.get("extracted_area", pd.Series()).astype(str).str.strip()
            areas = areas[areas != ""]
            if not areas.empty:
                vc = areas.value_counts(normalize=True)
                top_area, ratio = vc.index[0], vc.iloc[0]
                area_text = "集中发生于%s（占%.0f%%）" % (top_area, ratio * 100) if ratio >= 0.6 else ""
            else:
                area_text = ""

            reason = _build_reason(ev, area_text, hits)
            if ev.get("risk_reason_extra"):
                reason += " " + ev["risk_reason_extra"]
                del ev["risk_reason_extra"]
            ev["risk_reason"] = reason
            ev["score_breakdown"] = {
                "频次分": round(f_score, 2),
                "趋势分": round(t_score, 2),
                "集中度分": round(a_score, 2),
                "敏感度分": round(s_score, 2),
            }
        except Exception:
            # 风险计算异常不影响多频识别结果本身
            ev["priority_score"] = 0
            ev["risk_level"] = "需人工研判"
            ev["risk_reason"] = "风险计算出现异常，建议人工研判。"
            ev["score_breakdown"] = {}
    return events
