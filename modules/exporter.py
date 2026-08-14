# -*- coding: utf-8 -*-
"""
模块 9：结果导出（exporter.py）

输出 CSV（utf-8-sig，Excel 可直接打开）+ Excel（工单明细 + 事件汇总两个 Sheet）。
排序优先级：高关注 → 中关注 → 一般 → 频次降序 → 提交时间倒序。
"""
import io

import pandas as pd

# 风险等级排序权重（越小越靠前）
LEVEL_ORDER = {"高关注": 0, "中关注": 1, "一般": 2, "需人工研判": 3}


def build_results_table(df: pd.DataFrame, events: list) -> pd.DataFrame:
    """把事件级结论回写到每条工单，生成可导出的结果明细表。"""
    df = df.copy()

    # cluster_id -> 事件字段
    ev_map = {}
    for ev in events:
        ev_map[ev["cluster_id"]] = ev

    def lookup(cid, key, default=""):
        ev = ev_map.get(cid)
        return ev.get(key, default) if ev else default

    df["事件编号"] = df["cluster_id"].apply(lambda c: lookup(c, "event_id"))
    df["事件趋势"] = df["cluster_id"].apply(lambda c: lookup(c, "trend"))
    df["风险等级"] = df["cluster_id"].apply(lambda c: lookup(c, "risk_level"))
    df["优先级分数"] = df["cluster_id"].apply(lambda c: lookup(c, "priority_score"))
    df["风险原因"] = df["cluster_id"].apply(lambda c: lookup(c, "risk_reason"))
    df["建议关注部门"] = df["cluster_id"].apply(lambda c: lookup(c, "action_department"))
    df["建议动作"] = df["cluster_id"].apply(lambda c: lookup(c, "action_advice"))
    df["是否持续监控"] = df["cluster_id"].apply(lambda c: lookup(c, "monitor_required"))

    out = pd.DataFrame({
        "工单编号": df["order_id"],
        "原始诉求": df["content"],
        "涉及主体": df["subject"],
        "事发区域": df["area"],
        "提交时间": df["submit_time"].dt.strftime("%Y-%m-%d %H:%M"),
        "标准化内容": df.get("normalized_content", ""),
        "核心主体": df.get("extracted_subject", ""),
        "核心事件": df.get("extracted_event", ""),
        "聚类ID": df["cluster_id"],
        "聚类频次": df.get("cluster_size", 0),
        "是否多频": df["is_multi_freq"].map({True: "是", False: "否"}),
        "事件编号": df["事件编号"],
        "事件趋势": df["事件趋势"],
        "风险等级": df["风险等级"],
        "优先级分数": df["优先级分数"],
        "风险原因": df["风险原因"],
        "建议关注部门": df["建议关注部门"],
        "建议动作": df["建议动作"],
        "是否持续监控": df["是否持续监控"],
    })

    # 排序：风险等级 → 频次降序 → 时间倒序
    out["_level"] = out["风险等级"].map(lambda x: LEVEL_ORDER.get(x, 9))
    out["_freq"] = pd.to_numeric(out["聚类频次"], errors="coerce").fillna(0)
    out = out.sort_values(
        ["_level", "_freq", "提交时间"],
        ascending=[True, False, False],
    ).drop(columns=["_level", "_freq"]).reset_index(drop=True)
    return out


def build_event_summary(events: list) -> pd.DataFrame:
    """生成事件汇总表（看板/Excel 第二个 Sheet 使用）。"""
    rows = []
    for ev in events:
        rows.append({
            "事件编号": ev["event_id"],
            "核心主体": ev["event_subject"],
            "事件类型": ev["event_type"],
            "区域": ev["area"],
            "频次": ev["frequency"],
            "近24小时": ev.get("last_24h", 0),
            "近7天": ev.get("last_7d", 0),
            "首次出现": ev["first_seen"],
            "最近出现": ev["last_seen"],
            "趋势": ev["trend"],
            "风险等级": ev.get("risk_level", ""),
            "优先级分数": ev.get("priority_score", ""),
            "风险原因": ev.get("risk_reason", ""),
            "建议关注部门": ev.get("action_department", ""),
            "建议动作": ev.get("action_advice", ""),
            "是否持续监控": ev.get("monitor_required", ""),
            "重点事件": ev.get("is_key_event", ""),
        })
    return pd.DataFrame(rows)


def export_csv_bytes(results: pd.DataFrame) -> bytes:
    """导出 CSV 字节（utf-8-sig，兼容 Excel 中文）。"""
    buf = io.StringIO()
    results.to_csv(buf, index=False, encoding="utf-8-sig")
    return buf.getvalue().encode("utf-8-sig")


def export_excel_bytes(results: pd.DataFrame, events: list) -> bytes:
    """导出 Excel 字节：Sheet1 工单明细，Sheet2 高频事件汇总。"""
    buf = io.BytesIO()
    summary = build_event_summary(events)
    with pd.ExcelWriter(buf, engine="openpyxl") as writer:
        results.to_excel(writer, sheet_name="工单明细", index=False)
        summary.to_excel(writer, sheet_name="高频事件汇总", index=False)
    return buf.getvalue()
