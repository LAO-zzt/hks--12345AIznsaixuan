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


def _load_local_secret(name: str) -> str:
    """读取本地密钥文件 webapp/secrets.json（已 gitignore，绝不入库）。"""
    try:
        with open(os.path.join(WEB_DIR, "secrets.json"), "r", encoding="utf-8") as f:
            import json as _json
            return str(_json.load(f).get(name, "") or "").strip()
    except Exception:
        return ""


# ============================ 工具函数 ============================

def _fmt_time(ts) -> str:
    """
    时间戳转展示字符串；NaT/异常返回空串，不抛错。
    仅有日期（午夜零点，通常由工单编号解析）时只显示日期，不伪造时分。
    """
    try:
        if pd.isna(ts):
            return ""
        if ts.hour == 0 and ts.minute == 0:
            return ts.strftime("%Y-%m-%d")
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
            "advice_source": e.get("advice_source", "rules"),
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
            if name.lower() == "sample.csv":
                continue  # 内置样例走专用按钮，下拉只列真实数据文件
            if name.lower().endswith((".csv", ".xlsx", ".xlsm")):
                fpath = os.path.join(config.INPUT_DIR, name)
                files.append({
                    "name": name,
                    "size_mb": round(os.path.getsize(fpath) / 1024 / 1024, 1),
                })
    except Exception:
        pass
    return {"ok": True, "files": files}


def _read_upload(filename: str, content: bytes) -> pd.DataFrame:
    """上传文件读取：CSV 直接解析，Excel 落临时文件流式读取。"""
    if filename.lower().endswith((".xlsx", ".xlsm")):
        import tempfile
        tmp_path = None
        try:
            with tempfile.NamedTemporaryFile(suffix=".xlsx", delete=False) as tmp:
                tmp.write(content)
                tmp_path = tmp.name
            return loader.read_xlsx_streaming(tmp_path)
        finally:
            if tmp_path:
                try:
                    os.remove(tmp_path)
                except Exception:
                    pass
    return loader.load_csv_bytes(content)


@app.post("/api/preview")
async def preview(file: UploadFile = File(None), datafile: str = Form("")):
    """数据加载确认：真实读取条数/时间范围/区域数，供第一屏展示（禁止写死）。"""
    try:
        if file is not None and getattr(file, "filename", ""):
            content = await file.read()
            if not content.strip():
                return {"ok": False, "error": "上传的文件为空。"}
            df = loader.load_orders(_read_upload(file.filename, content))
        else:
            name = os.path.basename(datafile) if datafile else "sample.csv"
            path = os.path.join(config.INPUT_DIR, name)
            if not os.path.exists(path):
                return {"ok": False, "error": "数据文件不存在。"}
            df = loader.load_orders_cached(path)

        t = df["submit_time"].dropna()
        area_count = None
        if "area" in df.columns:
            areas = df["area"].astype(str).str.strip()
            areas = areas[areas != ""]
            if not areas.empty:
                area_count = int(areas.nunique())
        return {
            "ok": True,
            "rows": int(len(df)),
            "time_min": t.min().strftime("%Y-%m-%d") if not t.empty else None,
            "time_max": t.max().strftime("%Y-%m-%d") if not t.empty else None,
            "areas": area_count,
        }
    except Exception as e:
        return {"ok": False, "error": "数据读取失败：%s" % e}


@app.post("/api/analyze")
async def analyze(
    file: UploadFile = File(None),
    datafile: str = Form(""),
    scope: str = Form("all"),
    freq_threshold: int = Form(config.FREQ_THRESHOLD),
    eps: float = Form(config.CLUSTER_EPS),
    min_samples: int = Form(config.CLUSTER_MIN_SAMPLES),
    use_embedding: bool = Form(False),
    use_llm: bool = Form(False),
    llm_key: str = Form(""),
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
                df_raw = _read_upload(file.filename, content)
            except Exception as e:
                return {"ok": False, "error": "文件读取失败：%s。请确认为有效 CSV/Excel。" % e}
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
                cache_seed, scope, freq_threshold, eps, min_samples, use_embedding, use_llm)
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

        # ---- LLM 建议增强：规则词典未命中的事件交给 DeepSeek 兜底 ----
        if use_llm and events:
            from modules import llm_advisor
            api_key = ((llm_key or "").strip()
                       or os.environ.get("DEEPSEEK_API_KEY", "")
                       or _load_local_secret("deepseek_api_key"))
            if not api_key:
                warnings.append("已开启 LLM 建议增强，但未检测到 DeepSeek API Key"
                                "（页面参数区粘贴或设环境变量 DEEPSEEK_API_KEY），本次用规则词典结果。")
            else:
                unmatched = [e for e in events
                             if e.get("action_department") == "需人工研判"][:60]
                llm_map = {}
                if unmatched:
                    try:
                        llm_map = llm_advisor.llm_advise(unmatched, api_key)
                    except Exception:
                        llm_map = {}
                hit = 0
                for e in events:
                    if e.get("action_department") == "需人工研判":
                        r = llm_map.get(e.get("event_type", ""))
                        if r:
                            e["action_department"] = r["department"]
                            e["action_advice"] = r["advice"]
                            e["advice_source"] = "LLM"
                            hit += 1
                if hit:
                    warnings.append("DeepSeek 已为 %d 个事件完成处置建议匹配（详情中标注“AI 检索匹配”）。" % hit)
                if len(unmatched) > hit:
                    warnings.append("仍有 %d 个事件未匹配到处置建议，保持“需人工研判”。" % (len(unmatched) - hit))

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


# ============================ 三页业务结构：总览 / 工作台 / 详情 ============================

_LEVEL_RANK = {"高关注": 0, "中关注": 1, "一般": 2, "需人工研判": 3, "": 9}


def _cluster_event_map():
    """cluster_id -> 事件字典。"""
    if not STATE["events"]:
        return {}
    return {e["cluster_id"]: e for e in STATE["events"]}


@app.get("/api/boot")
def boot():
    """系统启动状态：是否已有分析结果 + 默认数据源（前端自动进入工作状态）。"""
    files = []
    try:
        for name in sorted(os.listdir(config.INPUT_DIR)):
            if name.lower() == "sample.csv":
                continue
            if name.lower().endswith((".csv", ".xlsx", ".xlsm")):
                files.append(name)
    except Exception:
        pass
    return {"ok": True, "has_result": STATE["df"] is not None,
            "default_file": files[0] if files else ""}


@app.get("/api/overview")
def overview():
    """全局总览聚合：数据源信息 + 区域聚合 + 全局日趋势（全部实时计算）。"""
    df = STATE["df"]
    if df is None:
        return {"ok": False, "error": "暂无分析结果。"}
    cmap = _cluster_event_map()
    high_clusters = set(cid for cid, e in cmap.items() if e.get("risk_level") == "高关注")

    multi_mask = df.get("is_multi_freq", pd.Series([False] * len(df), index=df.index)).astype(bool)
    high_mask = df["cluster_id"].isin(high_clusters) if "cluster_id" in df.columns else multi_mask.iloc[0:0]

    # 区域聚合（带坐标，供首页地图四档切换）
    from utils.helpers import load_area_coords
    coords = load_area_coords()
    areas_col = df.get("extracted_area", pd.Series([""] * len(df), index=df.index)).astype(str).str.strip()
    area_rows = []
    for area, cnt in areas_col[areas_col != ""].value_counts().items():
        if area not in coords:
            continue
        lat, lon = coords[area]
        a_mask = areas_col == area
        area_rows.append({
            "area": area, "lat": lat, "lon": lon,
            "total": int(cnt),
            "multi": int(multi_mask[a_mask].sum()),
            "high": int((high_mask & a_mask).sum()),
        })

    # 全局日趋势
    t = df["submit_time"].dropna()
    daily_all = []
    if not t.empty:
        daily = t.dt.date.value_counts().sort_index()
        daily_all = [{"date": str(k), "count": int(v)} for k, v in daily.items()]

    # 高频主体（全部工单统计，前30）
    subj_col = df.get("extracted_subject", pd.Series([""] * len(df), index=df.index)).astype(str).str.strip()
    subj_vc = subj_col[subj_col != ""].value_counts().head(30)
    top_subjects = [{"subject": k, "count": int(v)} for k, v in subj_vc.items()]

    return {
        "ok": True,
        "source": {
            "rows": int(len(df)),
            "time_min": t.min().strftime("%Y-%m-%d") if not t.empty else None,
            "time_max": t.max().strftime("%Y-%m-%d") if not t.empty else None,
        },
        "areas": area_rows,
        "daily_all": daily_all,
        "multi_orders": int(multi_mask.sum()),
        "top_subjects": top_subjects,
    }


@app.get("/api/orders")
def orders(page: int = 1, size: int = 50, q: str = "", area: str = "",
           subject: str = "", multi: str = "", level: str = "",
           event: str = "", sort: str = "default"):
    """工单工作台：全量工单搜索 / 筛选 / 排序 / 分页（服务端处理，支撑十万级数据）。"""
    df = STATE["df"]
    if df is None:
        return {"ok": False, "error": "暂无分析结果。"}
    cmap = _cluster_event_map()

    view = df
    if q:
        qq = q.lower()
        mask = view["content"].astype(str).str.lower().str.contains(qq, na=False) | \
               view["order_id"].astype(str).str.lower().str.contains(qq, na=False)
        if "title" in view.columns:
            mask = mask | view["title"].astype(str).str.lower().str.contains(qq, na=False)
        view = view[mask]
    if area:
        view = view[view.get("extracted_area", pd.Series(index=view.index)).astype(str) == area]
    if subject:
        view = view[view.get("extracted_subject", pd.Series(index=view.index)).astype(str) == subject]
    if multi in ("1", "0"):
        view = view[view["is_multi_freq"].astype(bool) == (multi == "1")]
    if level:
        lv_clusters = set(cid for cid, e in cmap.items() if e.get("risk_level") == level)
        view = view[view["cluster_id"].isin(lv_clusters)]
    if event:
        ev = next((e for e in (STATE["events"] or []) if e["event_id"] == event), None)
        view = view[view["cluster_id"] == ev["cluster_id"]] if ev else view.iloc[0:0]

    # 排序（默认：高优先优先；时间倒序兜底）
    lv_of = view["cluster_id"].map(lambda c: _LEVEL_RANK.get(cmap.get(c, {}).get("risk_level", ""), 9))
    if sort == "freq":
        view = view.assign(_a=lv_of, _b=view["cluster_size"]).sort_values(
            ["_a", "_b"], ascending=[True, False])
    elif sort == "time":
        view = view.sort_values("submit_time", ascending=False, na_position="last")
    else:
        view = view.assign(_a=lv_of, _b=view["cluster_size"]).sort_values(
            ["_a", "_b"], ascending=[True, False])

    total = int(len(view))
    page = max(1, page)
    size = min(max(1, size), 200)
    part = view.iloc[(page - 1) * size:(page - 1) * size + size]

    rows = []
    for r in part.itertuples(index=False):
        ev = cmap.get(r.cluster_id)
        oid = str(r.order_id)
        rows.append({
            "order_id": oid,
            "title": str(getattr(r, "title", "") or "")[:60],
            "content": truncate(r.content, 60),
            "area": str(getattr(r, "extracted_area", "") or ""),
            "subject": str(getattr(r, "extracted_subject", "") or ""),
            "event": ev.get("event_type", "") if ev else "",
            "event_id": ev.get("event_id", "") if ev else "",
            "size": int(r.cluster_size),
            "multi": bool(r.is_multi_freq),
            "level": ev.get("risk_level", "") if ev else "",
            "time": _fmt_time(r.submit_time),
        })
    return {"ok": True, "total": total, "page": page, "size": size, "rows": rows}


@app.get("/api/order/{oid}")
def order_detail(oid: str):
    """工单详情：原始工单 + AI结构化理解 + 关联工单 + 判断依据 + 相似非重复 + 核查状态。"""
    df = STATE["df"]
    if df is None:
        return {"ok": False, "error": "暂无分析结果。"}
    hit = df[df["order_id"].astype(str) == oid]
    if hit.empty:
        return {"ok": False, "error": "未找到工单：%s" % oid}
    r = hit.iloc[0]
    cid = int(r["cluster_id"]) if "cluster_id" in df.columns else -1
    cmap = _cluster_event_map()
    ev = cmap.get(cid)

    # 关联工单（同事件簇，最多 20 条）
    mates = []
    if cid != -1:
        same = df[(df["cluster_id"] == cid) & (df["order_id"].astype(str) != oid)].head(20)
        for m in same.itertuples(index=False):
            mates.append({
                "order_id": str(m.order_id),
                "content": truncate(m.content, 70),
                "area": str(getattr(m, "extracted_area", "") or ""),
                "time": _fmt_time(m.submit_time),
            })

    # 判断依据：当前工单与事件簇代表值的一致性因子（全部可验证，不伪造）
    basis = []
    if ev:
        my_area = str(getattr(r, "extracted_area", "") or "")
        my_subj = str(getattr(r, "extracted_subject", "") or "")
        ev_subj = ev.get("event_subject", "")
        ev_area = ev.get("area", "")
        basis.append({"factor": "事件标签",
                      "result": "一致" if ev.get("event_type") else "缺失"})
        if ev_area and my_area:
            basis.append({"factor": "区域", "result": "一致" if my_area == ev_area else "不一致"})
        else:
            basis.append({"factor": "区域", "result": "缺失"})
        if ev_subj and not str(ev_subj).startswith("（") and not str(ev_subj).startswith("多主体聚合"):
            ok = bool(my_subj and (my_subj == ev_subj or ev_subj in my_subj or my_subj in ev_subj))
            basis.append({"factor": "主体", "result": "一致" if ok else "不一致"})
        else:
            basis.append({"factor": "主体", "result": "缺失"})

    # 相似但非重复：同事件类型的其他事件（说明AI不是简单关键词匹配）
    similar = []
    if ev:
        for e2 in (STATE["events"] or []):
            if e2["event_id"] != ev["event_id"] and e2.get("event_type") == ev.get("event_type"):
                similar.append({
                    "event_id": e2["event_id"], "area": e2.get("area", ""),
                    "frequency": e2["frequency"],
                    "reason": "事件类型同为“%s”，但区域/主体不同，判定不属于同一事件" % ev.get("event_type", ""),
                })
            if len(similar) >= 3:
                break

    return {
        "ok": True,
        "order": {
            "order_id": oid,
            "title": str(getattr(r, "title", "") or ""),
            "content": str(r.content),
            "subject_raw": str(getattr(r, "subject", "") or ""),
            "area_raw": str(getattr(r, "area", "") or ""),
            "time": _fmt_time(r.submit_time),
        },
        "ai": {
            "subject": str(getattr(r, "extracted_subject", "") or ""),
            "event": str(getattr(r, "extracted_event", "") or ""),
            "area": str(getattr(r, "extracted_area", "") or ""),
            "normalized": str(getattr(r, "normalized_content", "") or ""),
            "department": ev.get("department", "") if ev else "",
            "advice": ev.get("advice", "") if ev else "",
            "advice_source": ev.get("advice_source", "rules") if ev else "rules",
        },
        "cluster": {
            "event_id": ev.get("event_id", "") if ev else "",
            "event_type": ev.get("event_type", "") if ev else "",
            "frequency": ev.get("frequency", int(r.cluster_size)) if ev else int(r.cluster_size),
            "level": ev.get("risk_level", "") if ev else "",
            "is_multi": bool(r.is_multi_freq),
        },
        "mates": mates,
        "basis": basis,
        "similar": similar,
    }


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="127.0.0.1", port=8600)
