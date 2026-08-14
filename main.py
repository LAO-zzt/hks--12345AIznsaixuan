# -*- coding: utf-8 -*-
"""
Streamlit 演示入口（main.py）

页面围绕“管理者看到什么”设计：
    顶部 KPI → 高频事件看板 → 事件详情（画像/趋势/区域/风险/建议）
    → 完整工单表 → 下载 / 飞书推送

现场操作路径：上传 CSV（或加载内置样例）→ 一键分析 → 看板展示。
任何非关键模块失败都不会让页面崩溃。
"""
import os
import sys

# 保证以任意工作目录启动时都能导入项目模块
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import pandas as pd
import streamlit as st
import plotly.express as px

import config
from modules import (
    loader, normalizer, entity_extractor,
    cluster as cluster_mod, classifier,
    event_profiler, risk_analyzer, action_advisor,
    exporter, feishu_pusher,
)
from utils.helpers import ensure_dirs, truncate, load_area_coords

st.set_page_config(
    page_title="12345 高频事件智能预警与处置辅助系统",
    page_icon="📊",
    layout="wide",
)
ensure_dirs()

# 风险等级 -> 颜色标记（用于表格高亮）
LEVEL_COLOR = {
    "高关注": "#fde2e2",
    "中关注": "#fff3d6",
    "一般": "#e8f4ea",
    "需人工研判": "#eeeeee",
}


# ============================ 流水线 ============================

def run_pipeline(df_raw: pd.DataFrame, params: dict) -> dict:
    """
    执行完整识别流水线。

    每个阶段独立 try/except：非关键阶段失败只记录警告，不中断主流程。
    返回结果字典，前端据此渲染。
    """
    result = {"warnings": [], "stage": "加载"}

    # 1) 加载与清洗
    df = loader.load_orders(df_raw)
    if df.empty:
        result["error"] = "未读取到有效工单：请检查文件是否为空、或诉求内容列是否缺失。"
        return result
    result["column_map"] = df.attrs.get("col_map", {})

    # 2) 文本标准化
    result["stage"] = "标准化"
    df = normalizer.normalize_orders(df)

    # 3) 实体与事件识别
    result["stage"] = "实体识别"
    df = entity_extractor.extract_entities(df)

    # 4) 聚类
    result["stage"] = "聚类"
    df, cluster_info = cluster_mod.cluster_orders(
        df,
        eps=params["eps"],
        min_samples=params["min_samples"],
        use_embedding=params["use_embedding"],
    )
    result["cluster_info"] = cluster_info
    result["warnings"].extend(cluster_info.get("messages", []))

    # 5) 多频识别
    result["stage"] = "多频识别"
    df, multi_ids = classifier.classify_multi_freq(df, freq_threshold=params["freq_threshold"])

    # 6) 事件画像
    result["stage"] = "事件画像"
    try:
        events = event_profiler.build_event_profiles(df)
    except Exception as e:
        events = []
        result["warnings"].append("事件画像生成失败：%s" % e)

    # 7) 风险分析
    result["stage"] = "风险分析"
    try:
        events = risk_analyzer.analyze_risks(events, df)
    except Exception as e:
        result["warnings"].append("风险分析失败：%s" % e)

    # 8) 处置建议
    result["stage"] = "处置建议"
    try:
        events = action_advisor.advise_actions(events)
    except Exception as e:
        result["warnings"].append("处置建议生成失败：%s" % e)

    result.update({"df": df, "events": events})
    return result


# ============================ 渲染辅助 ============================

def style_level_column(df_view: pd.DataFrame, col: str):
    """给风险等级列加背景色，帮助评委 10 秒定位重点。"""
    def _apply(v):
        color = LEVEL_COLOR.get(v, "")
        return "background-color: %s" % color if color else ""
    try:
        return df_view.style.map(_apply, subset=[col])
    except Exception:
        return df_view


def render_kpis(res: dict):
    """顶部四个关键指标。"""
    df, events = res["df"], res["events"]
    high = [e for e in events if e.get("risk_level") == "高关注"]
    biggest = events[0]["event_subject"] if events else "—"

    c1, c2, c3, c4 = st.columns(4)
    c1.metric("工单总量", len(df))
    c2.metric("多频事件数", len(events))
    c3.metric("高关注事件数", len(high))
    c4.metric("最大频次事件", truncate(biggest, 14))


def render_event_board(res: dict):
    """高频事件看板：事件名称 / 区域 / 频次 / 趋势 / 风险等级 / 优先级。"""
    events = res["events"]

    def _fmt_score(v):
        return "%.1f" % v if isinstance(v, (int, float)) else v

    board = pd.DataFrame([{
        "事件编号": e["event_id"],
        "事件名称": "%s · %s" % (e["event_subject"], e["event_type"]),
        "区域": e["area"],
        "频次": e["frequency"],
        "近7天": e.get("last_7d", 0),
        "趋势": e["trend"],
        "风险等级": e.get("risk_level", ""),
        "优先级分数": _fmt_score(e.get("priority_score", "")),
        "建议关注部门": e.get("action_department", ""),
    } for e in events])
    st.dataframe(style_level_column(board, "风险等级"), width="stretch", hide_index=True)


def render_event_detail(res: dict):
    """事件详情：画像 + 趋势图 + 区域分布 + 典型工单 + 风险原因 + 处置建议。"""
    events = res["events"]
    df = res["df"]
    options = {("%s %s·%s（%s次）" % (
        e["event_id"], e["event_subject"], e["event_type"], e["frequency"])): e for e in events}
    chosen_label = st.selectbox("选择要查看的事件", list(options.keys()))
    ev = options[chosen_label]
    g = df[df["cluster_id"] == ev["cluster_id"]].copy()

    # ---- 画像与结论 ----
    left, right = st.columns([1, 1])
    with left:
        st.markdown("#### 事件画像")
        st.markdown(
            "**主体**：%s  \n**类型**：%s  \n**区域**：%s  \n"
            "**频次**：%s（近24小时 %s，近7天 %s）  \n"
            "**首次出现**：%s  \n**最近出现**：%s" % (
                ev["event_subject"], ev["event_type"], ev["area"],
                ev["frequency"], ev.get("last_24h", 0), ev.get("last_7d", 0),
                ev["first_seen"], ev["last_seen"],
            )
        )
        score = ev.get("priority_score", "—")
        score_text = "%.1f" % score if isinstance(score, (int, float)) else score
        st.markdown("**风险等级**：%s（优先级分数 %s）" % (
            ev.get("risk_level", "—"), score_text))
        st.info("风险原因：%s" % ev.get("risk_reason", "暂无"))
        st.success("处置建议：%s → %s（持续监控：%s）" % (
            ev.get("action_department", "—"),
            ev.get("action_advice", "—"),
            ev.get("monitor_required", "—"),
        ))
        if ev.get("score_breakdown"):
            bd = ev["score_breakdown"]
            st.caption("评分构成（可解释）：" + "｜".join(
                "%s %.2f" % (k, v) for k, v in bd.items()))

    # ---- 时间趋势（横向条形，日期平铺） ----
    with right:
        st.markdown("#### 时间趋势")
        t = g["submit_time"].dropna()
        if t.empty:
            st.caption("该事件缺少有效时间字段，趋势图不可用。")
        else:
            daily = t.dt.date.value_counts().sort_index().reset_index()
            daily.columns = ["日期", "工单数"]
            daily["日期"] = daily["日期"].astype(str)
            fig = px.bar(
                daily, x="工单数", y="日期", orientation="h",
                color_discrete_sequence=["#4c78a8"],
                text="工单数",
            )
            fig.update_layout(
                xaxis=dict(title="工单数", dtick=1),
                # 日期按时间升序自下而上，趋势一眼可读
                yaxis=dict(automargin=True, categoryorder="category ascending"),
                margin=dict(t=12, b=12, l=12, r=24),
                height=max(200, 48 * len(daily) + 100),
            )
            st.plotly_chart(fig, width="stretch", config={"displayModeBar": False})

    # ---- 区域分布（地图气泡） ----
    st.markdown("#### 区域分布")
    areas = g["extracted_area"].replace("", None).dropna()
    if areas.empty:
        st.caption("该事件缺少区域信息。")
    else:
        area_dist = areas.value_counts().reset_index()
        area_dist.columns = ["区域", "工单数"]
        coords = load_area_coords()
        area_dist["纬度"] = area_dist["区域"].map(lambda a: coords.get(a, (None, None))[0])
        area_dist["经度"] = area_dist["区域"].map(lambda a: coords.get(a, (None, None))[1])
        known = area_dist.dropna(subset=["纬度", "经度"])

        if known.empty:
            # 坐标词典未覆盖 → 降级横向条形图，保证始终有结果
            st.caption("当前区域暂无坐标数据，降级为条形图展示。")
            fig = px.bar(
                area_dist, x="工单数", y="区域", orientation="h",
                color_discrete_sequence=["#4c78a8"], text="工单数",
            )
            fig.update_layout(
                xaxis=dict(title="工单数", dtick=1),
                yaxis=dict(automargin=True, categoryorder="total ascending"),
                margin=dict(t=12, b=12, l=12, r=24),
                height=max(180, 56 * len(area_dist) + 90),
            )
            st.plotly_chart(fig, width="stretch", config={"displayModeBar": False})
        else:
            fig = px.scatter_map(
                known, lat="纬度", lon="经度",
                size="工单数", hover_name="区域",
                hover_data={"纬度": False, "经度": False, "工单数": True},
                color_discrete_sequence=["#e74c3c"],
                size_max=32, zoom=11,
                center=dict(lat=float(known["纬度"].mean()),
                            lon=float(known["经度"].mean())),
                map_style="open-street-map",
            )
            fig.update_layout(
                margin=dict(t=12, b=12, l=12, r=12),
                height=420,
            )
            st.plotly_chart(fig, width="stretch", config={"displayModeBar": False})
            uncovered = area_dist[~area_dist["区域"].isin(known["区域"])]
            if not uncovered.empty:
                st.caption("坐标词典未覆盖区域（未上图）：" + "、".join(uncovered["区域"]))

    # ---- 典型工单 ----
    st.markdown("#### 典型工单")
    samples = pd.DataFrame(ev.get("sample_orders", []))
    if samples.empty:
        st.caption("暂无样例。")
    else:
        st.dataframe(samples, width="stretch", hide_index=True)


def render_full_table(res: dict):
    """完整工单表（含识别结果）。"""
    df = res["df"]
    view = pd.DataFrame({
        "工单编号": df["order_id"],
        "原始诉求": df["content"].apply(lambda x: truncate(x, 50)),
        "核心主体": df.get("extracted_subject", ""),
        "核心事件": df.get("extracted_event", ""),
        "区域": df.get("extracted_area", ""),
        "提交时间": df["submit_time"].dt.strftime("%Y-%m-%d %H:%M"),
        "聚类ID": df["cluster_id"],
        "聚类频次": df.get("cluster_size", 0),
        "是否多频": df["is_multi_freq"].map({True: "是", False: "否"}),
    })
    st.dataframe(view, width="stretch", hide_index=True, height=320)


# ============================ 页面主体 ============================

st.title("12345 高频事件智能预警与处置辅助系统")
st.caption("从海量工单中发现正在形成的高频问题 → 判断风险与优先级 → 给出处置建议（完全离线可运行）")

with st.sidebar:
    st.header("数据与参数")
    uploaded = st.file_uploader("上传工单 CSV", type=["csv"])
    use_sample = st.button("使用内置样例数据演示", width="stretch")

    st.subheader("识别参数")
    freq_threshold = st.slider("多频阈值 FREQ_THRESHOLD", 2, 15, config.FREQ_THRESHOLD)
    eps = st.slider("聚类邻域 CLUSTER_EPS", 0.1, 0.9, float(config.CLUSTER_EPS), 0.05)
    min_samples = st.slider("最少样本 CLUSTER_MIN_SAMPLES", 2, 10, config.CLUSTER_MIN_SAMPLES)
    use_embedding = st.checkbox("启用本地 Embedding（可选）", value=config.USE_EMBEDDING)

    with st.expander("高级参数（开发调试用，默认隐藏）"):
        st.write("风险权重：频次 %.2f / 趋势 %.2f / 集中度 %.2f / 敏感度 %.2f" % (
            config.RISK_WEIGHT_FREQUENCY, config.RISK_WEIGHT_TREND,
            config.RISK_WEIGHT_AREA, config.RISK_WEIGHT_SENSITIVITY))
        st.write("趋势窗口：近 %d 天 vs 基线 %d 天" % (
            config.TREND_RECENT_DAYS, config.TREND_BASELINE_DAYS))

    st.subheader("飞书推送（可选）")
    webhook = st.text_input("Webhook 地址", value=config.FEISHU_WEBHOOK, type="password")

    run = st.button("开始分析", type="primary", width="stretch")

# ---- 数据源确定 ----
df_raw = None
if run:
    if uploaded is not None:
        try:
            df_raw = loader.load_csv_bytes(uploaded.read())
        except Exception as e:
            st.error("文件读取失败：%s。请确认为有效的 CSV 文件。" % e)
    elif use_sample or uploaded is None:
        sample_path = os.path.join(config.INPUT_DIR, "sample.csv")
        if os.path.exists(sample_path):
            df_raw = pd.read_csv(sample_path)
        else:
            st.error("未上传文件，且内置样例数据缺失。")

# ---- 执行分析 ----
if run and df_raw is not None:
    with st.spinner("正在执行识别流水线…"):
        params = {
            "freq_threshold": freq_threshold,
            "eps": eps,
            "min_samples": min_samples,
            "use_embedding": use_embedding,
        }
        res = run_pipeline(df_raw, params)

    if res.get("error"):
        st.error(res["error"])
    else:
        st.session_state["result"] = res

# ---- 展示结果 ----
if "result" in st.session_state:
    res = st.session_state["result"]
    df, events = res["df"], res["events"]

    # 流水线状态提示
    info = res.get("cluster_info", {})
    col_a, col_b = st.columns([3, 1])
    with col_a:
        st.success("识别完成：共 %d 条工单，识别出 %d 个多频事件。" % (len(df), len(events)))
    with col_b:
        st.caption("识别路线：%s" % info.get("method", "—"))
    for w in res.get("warnings", []):
        st.warning(w)

    # 顶部 KPI
    render_kpis(res)
    st.divider()

    if not events:
        st.info("当前数据未识别出达到阈值的多频事件，可尝试下调多频阈值。")
    else:
        st.subheader("高频事件看板")
        render_event_board(res)
        st.divider()
        st.subheader("事件详情")
        render_event_detail(res)
        st.divider()

    st.subheader("完整工单表")
    render_full_table(res)
    st.divider()

    # ---- 导出与推送 ----
    st.subheader("结果导出与流转")
    try:
        results_table = exporter.build_results_table(df, events)
        csv_bytes = exporter.export_csv_bytes(results_table)
        excel_bytes = exporter.export_excel_bytes(results_table, events)
        c1, c2, c3 = st.columns([1, 1, 2])
        c1.download_button("下载结果 CSV", csv_bytes,
                           file_name="multi_freq_result.csv", mime="text/csv",
                           width="stretch")
        c2.download_button("下载结果 Excel", excel_bytes,
                           file_name="multi_freq_result.xlsx",
                           mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                           width="stretch")
        if c3.button("推送 Top5 高关注事件到飞书", width="stretch"):
            ok, msg = feishu_pusher.push_top_events(events, webhook=webhook)
            (st.success if ok else st.warning)(msg)
    except Exception as e:
        st.warning("导出/推送模块异常，不影响页面展示：%s" % e)
