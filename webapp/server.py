# -*- coding: utf-8 -*-
"""
FastAPI 服务入口（webapp/server.py）——自研 Web 版

业务逻辑 100% 复用项目根目录 modules/ 流水线，本文件只负责协议层：
    POST /api/analyze        上传 CSV（或用内置样例）→ 全链路分析 → JSON
    GET  /api/download/csv   导出工单明细 CSV
    GET  /api/download/excel 导出 Excel（明细 + 事件汇总）
    POST /api/feishu         推送 Top5 高关注事件到飞书（可选）
    GET  /                   自研前端页面

启动：python webapp/server.py   （默认 http://127.0.0.1:8600）
"""
import os
import sys

# 保证从任意目录启动都能导入项目模块
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)

import pandas as pd
from fastapi import FastAPI, File, Form, UploadFile
from fastapi.responses import HTMLResponse, Response
from fastapi.staticfiles import StaticFiles

import config
from modules import (
    loader, normalizer, entity_extractor,
    cluster as cluster_mod, classifier,
    event_profiler, risk_analyzer, action_advisor,
    exporter, feishu_pusher,
)
from utils.helpers import truncate, load_area_coords

WEB_DIR = os.path.dirname(os.path.abspath(__file__))

app = FastAPI(title="12345 高频事件智能预警与处置辅助系统 API")
app.mount("/static", StaticFiles(directory=os.path.join(WEB_DIR, "static")), name="static")

# 演示态：仅保留最近一次分析结果（单用户演示场景，不引入数据库）
STATE = {"df": None, "events": None}

# 结果级缓存：相同 数据源+参数 组合直接命中，避免重复跑全量流水线
RESULT_CACHE_DIR = os.path.join(config.OUTPUT_DIR, "cache")


def _result_cache_key(*parts) -> str:
    """把数据源指纹与参数拼成缓存键。"""
    import hashlib
    s = "|".join(str(p) for p in parts)
    return hashlib.md5(s.encode("utf-8")).hexdigest()[:16]


def _load_result_cache(key: str):
    """读取结果缓存；缺失或损坏返回 None。"""
    path = os.path.join(RESULT_CACHE_DIR, "result_%s.pkl" % key)
    if not os.path.exists(path):
        return None
    try:
        return pd.read_pickle(path)
    except Exception:
        return None


def _save_result_cache(key: str, data: dict):
    """保存结果缓存；失败不影响主流程。"""
    try:
        os.makedirs(RESULT_CACHE_DIR, exist_ok=True)
        pd.to_pickle(data, os.path.join(RESULT_CACHE_DIR, "result_%s.pkl" % key))
    except Exception:
        pass


# ============================ 工具函数 ============================

def _fmt_time(ts) -> str:
    """时间戳转展示字符串；NaT/异常返回空串，不抛错。"""
    try:
        if pd.isna(ts):
            return ""
        return ts.strftime("%Y-%m-%d %H:%M")
    except Exception:
        return ""


def _event_daily(df, cluster_id) -> list:
    """该事件按日工单数（供前端趋势图）。"""
    t = df.loc[df["cluster_id"] == cluster_id, "submit_time"].dropna()
    if t.empty:
        return []
    daily = t.dt.date.value_counts().sort_index()
    return [{"date": str(k), "count": int(v)} for k, v in daily.items()]


def _event_areas(df, cluster_id) -> list:
    """该事件区域分布（附坐标，供前端地理示意散点图；坐标缺失为 None）。"""
    coords = load_area_coords()
    areas = df.loc[df["cluster_id"] == cluster_id, "extracted_area"].astype(str).str.strip()
    areas = areas[areas != ""]
    if areas.empty:
        return []
    result = []
    for k, v in areas.value_counts().items():
        lat, lon = coords.get(str(k), (None, None))
        result.append({"area": str(k), "count": int(v), "lat": lat, "lon": lon})
    return result


def _build_payload(df, events, info, warnings) -> dict:
    """把分析结果序列化为前端 JSON（超大数据按配置限量展示）。"""
    high_count = sum(1 for e in events if e.get("risk_level") == "高关注")
    multi_total = int(df["is_multi_freq"].groupby(df["cluster_id"]).any().sum()) \
        if "is_multi_freq" in df.columns else len(events)

    # 事件按文档口径排序：风险等级优先，同级按频次降序，限量展示 Top N
    level_order = {"高关注": 0, "中关注": 1, "一般": 2, "需人工研判": 3}
    max_events = getattr(config, "MAX_DISPLAY_EVENTS", 100)
    ranked = sorted(
        events,
        key=lambda e: (level_order.get(e.get("risk_level", ""), 9),
                       -int(e.get("frequency", 0))))
    shown_events = ranked[:max_events]

    events_json = []
    for e in shown_events:
        events_json.append({
            "event_id": e["event_id"],
            "subject": e["event_subject"],
            "type": e["event_type"],
            "area": e["area"],
            "frequency": e["frequency"],
            "last_24h": e.get("last_24h", 0),
            "last_7d": e.get("last_7d", 0),
            "first_seen": e["first_seen"],
            "last_seen": e["last_seen"],
            "trend": e["trend"],
            "risk_level": e.get("risk_level", ""),
            "priority_score": e.get("priority_score", 0),
            "risk_reason": e.get("risk_reason", ""),
            "breakdown": e.get("score_breakdown", {}),
            "department": e.get("action_department", ""),
            "advice": e.get("action_advice", ""),
            "monitor": e.get("monitor_required", ""),
            "is_key": e.get("is_key_event", ""),
            "samples": e.get("sample_orders", []),
            "daily": _event_daily(df, e["cluster_id"]),
            "areas": _event_areas(df, e["cluster_id"]),
        })

    # 工单明细：多频优先，限量展示
    max_orders = getattr(config, "MAX_DISPLAY_ORDERS", 2000)
    order_df = df.sort_values(["is_multi_freq", "cluster_size"], ascending=[False, False]).head(max_orders)
    orders_json = []
    for r in order_df.itertuples(index=False):
        orders_json.append({
            "order_id": r.order_id,
            "content": truncate(r.content, 60),
            "subject": r.extracted_subject,
            "event": r.extracted_event,
            "area": r.extracted_area,
            "time": _fmt_time(r.submit_time),
            "size": int(r.cluster_size),
            "multi": bool(r.is_multi_freq),
        })

    if len(ranked) > max_events:
        warnings = warnings + ["事件总数 %d 个，看板按优先级展示 Top %d。" % (len(ranked), max_events)]
    if len(df) > max_orders:
        warnings = warnings + ["明细表展示多频优先的前 %d 条（共 %d 条），完整数据请下载结果文件。" % (max_orders, len(df))]

    def _display_name(e):
        subj = e["event_subject"]
        if not subj or subj.startswith("（") or subj.startswith("多主体聚合"):
            return e["event_type"]
        return subj

    return {
        "kpis": {
            "total_orders": int(len(df)),
            "multi_events": multi_total,
            "high_events": high_count,
            "biggest_event": _display_name(events[0]) if events else "—",
        },
        "method": info.get("method", ""),
        "warnings": warnings,
        "events": events_json,
        "orders": orders_json,
    }


# ============================ API 路由 ============================

@app.get("/", response_class=HTMLResponse)
def index():
    """自研前端入口。"""
    path = os.path.join(WEB_DIR, "static", "index.html")
    with open(path, "r", encoding="utf-8") as f:
        return f.read()


@app.get("/api/datafiles")
def datafiles():
    """列出 data/input 下可用的数据文件（真实数据放入该目录即可被识别）。"""
    files = []
    try:
        for name in sorted(os.listdir(config.INPUT_DIR)):
            if name.lower().endswith((".csv", ".xlsx", ".xlsm")):
                fpath = os.path.join(config.INPUT_DIR, name)
                files.append({
                    "name": name,
                    "size_mb": round(os.path.getsize(fpath) / 1024 / 1024, 1),
                })
    except Exception:
        pass
    return {"ok": True, "files": files}


@app.post("/api/analyze")
async def analyze(
    file: UploadFile = File(None),
    datafile: str = Form(""),
    scope: str = Form("all"),
    freq_threshold: int = Form(config.FREQ_THRESHOLD),
    eps: float = Form(config.CLUSTER_EPS),
    min_samples: int = Form(config.CLUSTER_MIN_SAMPLES),
    use_embedding: bool = Form(False),
):
    """执行全链路分析：加载→标准化→实体→聚类→多频→画像→风险→建议。"""
    warnings = []
    try:
        # ---- 数据源：上传文件 > data/input 本地文件 > 内置样例 ----
        df = None
        cache_seed = None
        if file is not None and getattr(file, "filename", ""):
            content = await file.read()
            if not content.strip():
                return {"ok": False, "error": "上传的文件为空。"}
            try:
                df_raw = loader.load_csv_bytes(content)
            except Exception as e:
                return {"ok": False, "error": "文件读取失败：%s。请确认为有效 CSV。" % e}
            df = loader.load_orders(df_raw)
        elif datafile:
            # 防路径穿越：只允许 data/input 下的文件名
            safe_name = os.path.basename(datafile)
            path = os.path.join(config.INPUT_DIR, safe_name)
            if not os.path.exists(path):
                return {"ok": False, "error": "本地数据文件不存在：%s" % safe_name}
            try:
                df = loader.load_orders_cached(path)
            except Exception as e:
                return {"ok": False, "error": "本地文件读取失败：%s" % e}
            st_info = os.stat(path)
            cache_seed = "file|%s|%d|%d" % (safe_name, int(st_info.st_mtime), st_info.st_size)
        else:
            sample = os.path.join(config.INPUT_DIR, "sample.csv")
            if not os.path.exists(sample):
                return {"ok": False, "error": "未上传文件，且内置样例数据缺失。"}
            df_raw = pd.read_csv(sample)
            df = loader.load_orders(df_raw)
            cache_seed = "sample"

        if df.empty:
            return {"ok": False, "error": "未读取到有效工单：请检查文件是否为空或诉求内容列缺失。"}

        # ---- 分析范围：全部 / 近30天 / 近60天（以数据集最近时间为基准） ----
        scope = scope if scope in ("d30", "d60") else "all"
        if scope != "all" and df["submit_time"].notna().any():
            end = df["submit_time"].max()
            days = 30 if scope == "d30" else 60
            before = len(df)
            df = df[df["submit_time"] >= end - pd.Timedelta(days=days)].reset_index(drop=True)
            warnings.append("分析范围：近 %d 天（%d/%d 条）。" % (days, len(df), before))
            if df.empty:
                return {"ok": False, "error": "近 %d 天内无工单数据。" % days}

        # ---- 结果级缓存：相同数据源+参数秒出 ----
        cache_key = None
        if cache_seed:
            cache_key = _result_cache_key(
                cache_seed, scope, freq_threshold, eps, min_samples, use_embedding)
            cached = _load_result_cache(cache_key)
            if cached is not None:
                STATE["df"], STATE["events"] = cached["df"], cached["events"]
                warnings.append("已命中分析缓存，结果与上次相同参数分析一致。")
                return {"ok": True, "payload": _build_payload(
                    cached["df"], cached["events"], cached["info"], warnings)}

        # ---- 流水线（与 Streamlit 版完全一致） ----
        df = normalizer.normalize_orders(df)
        df = entity_extractor.extract_entities(df)
        df, info = cluster_mod.cluster_orders(
            df, eps=eps, min_samples=min_samples, use_embedding=use_embedding)
        warnings.extend(info.get("messages", []))
        df, _ = classifier.classify_multi_freq(df, freq_threshold=freq_threshold)

        try:
            events = event_profiler.build_event_profiles(df)
        except Exception as e:
            events, warnings = [], warnings + ["事件画像生成失败：%s" % e]
        try:
            events = risk_analyzer.analyze_risks(events, df)
        except Exception as e:
            warnings.append("风险分析失败：%s" % e)
        try:
            events = action_advisor.advise_actions(events)
        except Exception as e:
            warnings.append("处置建议生成失败：%s" % e)

        STATE["df"], STATE["events"] = df, events
        if cache_key:
            _save_result_cache(cache_key, {"df": df, "events": events, "info": info})
        return {"ok": True, "payload": _build_payload(df, events, info, warnings)}
    except Exception as e:
        return {"ok": False, "error": "分析过程异常：%s" % e}


@app.get("/api/download/csv")
def download_csv():
    """导出结果 CSV（utf-8-sig，Excel 可直接打开）。"""
    if STATE["df"] is None:
        return Response("请先运行分析。", status_code=400)
    table = exporter.build_results_table(STATE["df"], STATE["events"])
    data = exporter.export_csv_bytes(table)
    return Response(
        content=data, media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=multi_freq_result.csv"})


@app.get("/api/download/excel")
def download_excel():
    """导出结果 Excel（工单明细 + 高频事件汇总）。明细按优先级限量，防超大数据卡顿。"""
    if STATE["df"] is None:
        return Response("请先运行分析。", status_code=400)
    table = exporter.build_results_table(STATE["df"], STATE["events"])
    max_rows = getattr(config, "MAX_EXPORT_ORDERS", 20000)
    table = table.head(max_rows)
    data = exporter.export_excel_bytes(table, STATE["events"])
    return Response(
        content=data,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": "attachment; filename=multi_freq_result.xlsx"})


@app.post("/api/feishu")
async def feishu(body: dict):
    """推送 Top5 高关注事件到飞书（webhook 为空则跳过）。"""
    if STATE["events"] is None:
        return {"ok": False, "message": "请先运行分析。"}
    ok, msg = feishu_pusher.push_top_events(STATE["events"], webhook=body.get("webhook", ""))
    return {"ok": ok, "message": msg}


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="127.0.0.1", port=8600)
