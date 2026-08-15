# -*- coding: utf-8 -*-
"""
模块 9：结果导出（exporter.py）

输出 CSV（utf-8-sig，Excel 可直接打开）+ Excel（工单明细 + 事件汇总两个 Sheet）。
排序：频次降序 → 提交时间倒序。
"""
import io

import pandas as pd


def build_results_table(df: pd.DataFrame, events: list) -> pd.DataFrame:
    """把事件级结论回写到每条工单，生成可导出的结果明细表。"""
    df = df.copy()

    ev_map = {ev["cluster_id"]: ev for ev in events}

    def lookup(cid, key, default=""):
        ev = ev_map.get(cid)
        return ev.get(key, default) if ev else default

    df["事件编号"] = df["cluster_id"].apply(lambda c: lookup(c, "event_id"))

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
    })

    out["_freq"] = pd.to_numeric(out["聚类频次"], errors="coerce").fillna(0)
    out = out.sort_values(
        ["_freq", "提交时间"],
        ascending=[False, False],
    ).drop(columns=["_freq"]).reset_index(drop=True)
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
            "首次出现": ev["first_seen"],
            "最近出现": ev["last_seen"],
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
