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

import config
from modules import (
    loader, normalizer, entity_extractor,
    cluster as cluster_mod, classifier,
    event_profiler, exporter, feishu_pusher,
)
from utils.helpers import ensure_dirs, truncate

st.set_page_config(
    page_title="12345 多频工单识别",
    page_icon="📊",
    layout="wide",
)
ensure_dirs()


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

    result.update({"df": df, "events": events})
    return result


# ============================ 渲染辅助 ============================

def render_kpis(res: dict):
    """顶部关键指标。"""
    df, events = res["df"], res["events"]
    biggest = events[0]["event_subject"] if events else "—"

    c1, c2, c3 = st.columns(3)
    c1.metric("工单总量", len(df))
    c2.metric("多频事件数", len(events))
    c3.metric("最大频次事件", truncate(biggest, 14))


def render_event_board(res: dict):
    """多频事件看板：事件名称 / 区域 / 频次 / 首末出现。"""
    events = res["events"]
    board = pd.DataFrame([{
        "事件编号": e["event_id"],
        "事件名称": "%s · %s" % (e["event_subject"], e["event_type"]),
        "区域": e["area"],
        "频次": e["frequency"],
        "首次出现": e["first_seen"],
        "最近出现": e["last_seen"],
    } for e in events])
    st.dataframe(board, width="stretch", hide_index=True)


def render_event_detail(res: dict):
    """事件详情：画像 + 典型工单。"""
    events = res["events"]
    df = res["df"]
    options = {("%s %s·%s（%s次）" % (
        e["event_id"], e["event_subject"], e["event_type"], e["frequency"])): e for e in events}
    chosen_label = st.selectbox("选择要查看的事件", list(options.keys()))
    ev = options[chosen_label]

    st.markdown("#### 事件画像")
    st.markdown(
        "**主体**：%s  \n**类型**：%s  \n**区域**：%s  \n"
        "**频次**：%s  \n"
        "**首次出现**：%s  \n**最近出现**：%s" % (
            ev["event_subject"], ev["event_type"], ev["area"],
            ev["frequency"], ev["first_seen"], ev["last_seen"],
        )
    )

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


# ============================ 页面 1：数据上传与参数 ============================

PAGE_CONFIG = "① 数据上传与参数"
PAGE_RESULTS = "② 分析结果看板"


def render_config_page():
    """页面①：CSV 上传 + 识别参数调整 + 飞书配置 + 一键分析。"""
    st.subheader("第一步 · 上传工单数据")
    uploaded = st.file_uploader("上传工单 CSV（支持中英文列名自动识别）", type=["csv"])
    st.caption("未上传文件时，分析将自动使用内置样例数据（37 条，覆盖 5 类高频事件场景）。")

    st.divider()
    st.subheader("第二步 · 设置识别参数")
    c1, c2, c3 = st.columns(3)
    freq_threshold = c1.slider("多频阈值 FREQ_THRESHOLD", 2, 15, config.FREQ_THRESHOLD)
    eps = c2.slider("聚类邻域 CLUSTER_EPS", 0.1, 0.9, float(config.CLUSTER_EPS), 0.05)
    min_samples = c3.slider("最少样本 CLUSTER_MIN_SAMPLES", 2, 10, config.CLUSTER_MIN_SAMPLES)
    use_embedding = st.checkbox(
        "启用本地 Embedding（可选，模型缺失自动回退 TF-IDF）", value=config.USE_EMBEDDING)

    st.divider()
    st.subheader("第三步 · 飞书推送（可选）")
    webhook = st.text_input("Webhook 地址", value=config.FEISHU_WEBHOOK, type="password")

    st.divider()
    b1, b2 = st.columns([1, 1])
    run_upload = b1.button("开始分析", type="primary", width="stretch")
    run_sample = b2.button("使用内置样例数据演示", width="stretch")

    if not (run_upload or run_sample):
        return

    # ---- 数据源确定：优先上传文件，其次内置样例 ----
    df_raw = None
    if run_upload and uploaded is not None:
        try:
            df_raw = loader.load_csv_bytes(uploaded.read())
        except Exception as e:
            st.error("文件读取失败：%s。请确认为有效的 CSV 文件。" % e)
            return
    if df_raw is None:
        sample_path = os.path.join(config.INPUT_DIR, "sample.csv")
        if os.path.exists(sample_path):
            df_raw = pd.read_csv(sample_path)
        else:
            st.error("未上传文件，且内置样例数据缺失。")
            return

    # ---- 执行分析，成功后自动跳转结果页 ----
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
        return

    st.session_state["result"] = res
    st.session_state["webhook"] = webhook
    st.session_state["page"] = PAGE_RESULTS
    st.rerun()


# ============================ 页面 2：分析结果看板 ============================

def render_results_page():
    """页面②：工单识别结果呈现（KPI/看板/详情/明细/导出）。"""
    res = st.session_state.get("result")

    if res is None:
        st.info("还没有分析结果。请先上传数据并运行分析。")
        if st.button("前往上传数据并开始分析", type="primary"):
            st.session_state["page"] = PAGE_CONFIG
            st.rerun()
        return

    if st.button("← 返回参数调整", width="stretch"):
        st.session_state["page"] = PAGE_CONFIG
        st.rerun()

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
            ok, msg = feishu_pusher.push_top_events(
                events, webhook=st.session_state.get("webhook", ""))
            (st.success if ok else st.warning)(msg)
    except Exception as e:
        st.warning("导出/推送模块异常，不影响页面展示：%s" % e)


# ============================ 页面主体（导航） ============================

st.title("12345 高频事件智能预警与处置辅助系统")
st.caption("从海量工单中发现正在形成的高频问题 → 判断风险与优先级 → 给出处置建议（完全离线可运行）")

if "page" not in st.session_state:
    st.session_state["page"] = PAGE_CONFIG

# 顶部页面切换（按钮式导航，当前页高亮）
nav1, nav2 = st.columns(2)
result_badge = "（已有结果）" if st.session_state.get("result") else ""
if nav1.button(PAGE_CONFIG, width="stretch",
               type="primary" if st.session_state["page"] == PAGE_CONFIG else "secondary"):
    st.session_state["page"] = PAGE_CONFIG
    st.rerun()
if nav2.button(PAGE_RESULTS + result_badge, width="stretch",
               type="primary" if st.session_state["page"] == PAGE_RESULTS else "secondary"):
    st.session_state["page"] = PAGE_RESULTS
    st.rerun()

st.divider()

if st.session_state["page"] == PAGE_CONFIG:
    render_config_page()
else:
    render_results_page()
