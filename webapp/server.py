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
import re
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
    event_profiler, exporter, feishu_pusher, feishu_loader, llm_dedup,
)
from utils.helpers import truncate, load_area_coords

WEB_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_DIR = os.path.dirname(WEB_DIR)

app = FastAPI(title="12345 高频事件智能预警与处置辅助系统 API")
app.mount("/static", StaticFiles(directory=os.path.join(WEB_DIR, "static")), name="static")

# 演示态：仅保留最近一次分析结果（单用户演示场景，不引入数据库）
STATE = {"df": None, "events": None, "source_meta": None, "info": {}}

# 结果级缓存：相同 数据源+参数 组合直接命中，避免重复跑全量流水线
RESULT_CACHE_DIR = os.path.join(config.OUTPUT_DIR, "cache")

# textclean_module 清洗流水线（懒加载单例）
_TCM = {"pipeline": None}


def _get_textclean_pipeline():
    import logging
    if _TCM["pipeline"] is None:
        logging.getLogger("ticket_cleaner").setLevel(logging.ERROR)
        import textclean_module as tcm
        data_dir = os.path.join(PROJECT_DIR, "textclean_module", "data")
        os.makedirs(data_dir, exist_ok=True)
        cfg = tcm.Config(db_path=os.path.join(data_dir, "cleaner.db"),
                         work_dir=data_dir, source_excel_path="")
        from ticket_cleaner.pipeline import CleaningPipeline
        _TCM["pipeline"] = CleaningPipeline(cfg, tcm.Storage(cfg.db_path))
    return _TCM["pipeline"]


def _run_textclean(df):
    """textclean_module 清洗+实体抽取，映射到现有流水线列（布局/接口不变）。"""
    pipeline = _get_textclean_pipeline()
    from ticket_cleaner.schema import TicketRecord
    from ticket_cleaner.extractors import extract_organization
    records = [
        TicketRecord(
            ticket_no=str(r.order_id), title=str(r.title or ""),
            content=str(r.content or ""), region=str(r.area or ""))
        for r in df.itertuples(index=False)
    ]
    cleaned = pipeline.process_batch(records)

    def _nz(v):
        v = str(v).strip()
        return v if v and v != "nan" else ""

    def _pick_subject(tc_result, raw_subj):
        """优先 textclean 结果，其次清洗原始 subject（社区优先覆盖），最后原样兜底。"""
        if tc_result and tc_result.organization_normalized:
            return tc_result.organization_normalized
        cleaned, _, _ = extract_organization(_nz(raw_subj))
        if cleaned:
            return cleaned
        return _nz(raw_subj)

    df = df.copy()
    contents = df["content"].astype(str).tolist()
    subjects = df["subject"].astype(str).tolist() if "subject" in df else [""] * len(df)
    areas = df["area"].astype(str).tolist() if "area" in df else [""] * len(df)
    df["normalized_content"] = [
        (t.semantic_content or t.clean_content or c) for t, c in zip(cleaned, contents)]
    df["extracted_subject"] = [
        _pick_subject(t, raw) for t, raw in zip(cleaned, subjects)]
    df["extracted_event"] = [t.event_type or "" for t in cleaned]
    df["extracted_area"] = [(t.town or _nz(raw)) for t, raw in zip(cleaned, areas)]
    df["addr_norm"] = [t.address_normalized or "" for t in cleaned]
    df["addr_community"] = [t.community or "" for t in cleaned]
    df["addr_building"] = [t.building or "" for t in cleaned]
    return df


_ADDR_LANDMARK = ("村", "社区", "路", "街", "大道", "园", "市场", "广场", "工业区")
_SUBJ_GENERIC = {"政府", "村委会", "居委会", "村委", "居委",
                 "物业公司", "物业", "管理处", "服务中心"}


def _has_landmark(s):
    """含地标词（“街道”行政后缀不算，防整个街道误匹配）。"""
    return any(k in s.replace("街道", "") for k in _ADDR_LANDMARK)


def _addr_contains(a, b):
    """地址去门牌后互为包含（短串≥5字且含地标词）。对称。"""
    if not a or not b:
        return False
    ca = re.sub(r"[0-9０-９]", "", a)
    cb = re.sub(r"[0-9０-９]", "", b)
    short, lng = (ca, cb) if len(ca) <= len(cb) else (cb, ca)
    return len(short) >= 5 and short in lng and _has_landmark(short)


def _norm_subject(s):
    """有效语义主体：≥4字且非泛化机构名，否则视为无主体。"""
    s = (s or "").strip()
    return s if len(s) >= 4 and s not in _SUBJ_GENERIC else ""


def _is_same_place(a, b, sim=None):
    """同簇内判定两工单是否同一地点。纯对称函数：A↔B 判定结果必然一致。

    条件（从严到宽）：
      1 归一化地址完全一致
      2 社区+楼栋均一致
      3 同社区 且 地址去门牌互含
      4 双方无社区但楼栋一致
      5 主体语义一致且同镇街，且 地址互含 或 双方均无社区
      6 双方地址字段全空时，内容语义相似度（TF-IDF+SVD 余弦）≥0.65 兜底
    """
    if a["addr"] and a["addr"] == b["addr"]:
        return True
    if a["comm"] and a["comm"] == b["comm"]:
        if a["bld"] and a["bld"] == b["bld"]:
            return True
        if _addr_contains(a["addr"], b["addr"]):
            return True
    if not a["comm"] and not b["comm"] and a["bld"] and a["bld"] == b["bld"]:
        return True
    sa, sb = _norm_subject(a["subj"]), _norm_subject(b["subj"])
    if sa and sa == sb and a["area"] == b["area"] and \
            (_addr_contains(a["addr"], b["addr"]) or (not a["comm"] and not b["comm"])):
        return True
    if sim is not None and sim >= 0.65 and \
            not any((a["addr"], a["comm"], a["bld"])) and \
            not any((b["addr"], b["comm"], b["bld"])):
        return True
    return False


# 全量语义向量缓存（textclean TfidfEmbedder：TF-IDF+SVD，向量已L2归一化）
_SEM = {"df_id": None, "matrix": None}


def _semantic_matrix(df):
    """懒加载：对全量 normalized_content 计算语义向量矩阵（与分析 df 对齐）。"""
    if _SEM["df_id"] == id(df):
        return _SEM["matrix"]
    matrix = None
    try:
        from ticket_cleaner.embedding import TfidfEmbedder
        texts = df["normalized_content"].astype(str).fillna("").tolist()
        emb = TfidfEmbedder(target_dim=128)
        emb.fit(texts)
        matrix = emb.embed(texts)
    except Exception:
        matrix = None
    _SEM.update(df_id=id(df), matrix=matrix)
    return matrix


# 跨簇同址候选桶索引（详情页关联工单用：同地址的不同事件工单也算同址重复）
_GIDX = {"df_id": None, "buckets": None}


def _addr_bucket_keys(addr, comm, bld, subj, area):
    """统一桶键生成（详情页关联工单候选，跨簇不带簇维度）。"""
    keys = []
    subj = _norm_subject(subj)
    if addr:
        keys.append(("a", addr))
        if comm:
            keys.append(("c", comm))
    if comm and bld:
        keys.append(("cb", comm, bld))
    if not comm and bld:
        keys.append(("b", bld))
    if subj:
        keys.append(("s", subj, area))
    return keys


def _global_addr_buckets(df):
    """懒构建全量工单的同址候选桶（一次构建，随 df 缓存）。"""
    if _GIDX["df_id"] == id(df):
        return _GIDX["buckets"]
    buckets = {}
    try:
        for t in df.itertuples(index=False):
            for k in _addr_bucket_keys(
                    str(getattr(t, "addr_norm", "") or ""),
                    str(getattr(t, "addr_community", "") or ""),
                    str(getattr(t, "addr_building", "") or ""),
                    str(getattr(t, "extracted_subject", "") or ""),
                    str(getattr(t, "extracted_area", "") or "")):
                buckets.setdefault(k, []).append(t)
    except Exception:
        buckets = {}
    _GIDX.update(df_id=id(df), buckets=buckets)
    return buckets


def _result_cache_key(*parts) -> str:
    """把数据源指纹与参数拼成缓存键。"""
    import hashlib
    s = "|".join(str(p) for p in parts)
    return hashlib.md5(s.encode("utf-8")).hexdigest()[:16]


def _load_result_cache(key: str):
    """读取结果缓存；缺失/损坏（含0字节）自动删除坏文件并返回 None。"""
    path = os.path.join(RESULT_CACHE_DIR, "result_%s.pkl" % key)
    if not os.path.exists(path):
        return None
    try:
        data = pd.read_pickle(path)
        if not isinstance(data, dict) or data.get("df") is None:
            raise ValueError("invalid cache payload")
        return data
    except Exception:
        try:
            os.remove(path)
        except OSError:
            pass
        return None


def _save_result_cache(key: str, data: dict):
    """保存结果缓存（原子写入：先写 .tmp 再替换，避免中断留下 0 字节坏缓存）。"""
    tmp = None
    try:
        os.makedirs(RESULT_CACHE_DIR, exist_ok=True)
        path = os.path.join(RESULT_CACHE_DIR, "result_%s.pkl" % key)
        tmp = path + ".tmp"
        pd.to_pickle(data, tmp)
        os.replace(tmp, path)
    except Exception:
        if tmp and os.path.exists(tmp):
            try:
                os.remove(tmp)
            except OSError:
                pass


# ============================ 重启恢复（开发模式：免每次重跑分析） ============================
import threading as _threading

_RESTORE_LOCK = _threading.Lock()


def _restore_latest_cache():
    """从磁盘恢复最近一次分析结果到会话（按缓存文件 mtime 取最新）。"""
    with _RESTORE_LOCK:
        if STATE["df"] is not None:
            return True
        try:
            if not os.path.isdir(RESULT_CACHE_DIR):
                return False
            files = [f for f in os.listdir(RESULT_CACHE_DIR)
                     if f.startswith("result_") and f.endswith(".pkl")]
            if not files:
                return False
            # 坏缓存会被 _load_result_cache 自愈删除；跳过玩具数据集（<1000条），
            # 其余按 mtime 取最新（最新一次全量分析结果，与算法版本一致）
            cand = []
            for f in files:
                p = os.path.join(RESULT_CACHE_DIR, f)
                if os.path.getsize(p) > 200_000:  # 玩具数据缓存很小，直接按体积粗滤
                    cand.append(f)
            cand.sort(key=lambda f: os.path.getmtime(os.path.join(RESULT_CACHE_DIR, f)), reverse=True)
            for name in cand:
                data = _load_result_cache(name[len("result_"):-len(".pkl")])
                if data is None:
                    continue
                STATE["df"], STATE["events"] = data["df"], data.get("events", [])
                STATE["info"] = data.get("info", {}) or {}
                return True
            return False
        except Exception:
            return False


@app.on_event("startup")
def _startup_restore():
    _threading.Thread(target=_restore_latest_cache, daemon=True).start()


@app.get("/api/payload")
def get_payload():
    """前端 boot 优先调用：服务端已有（或已恢复）分析结果则秒回，跳过重新分析。"""
    if not _restore_latest_cache():
        return {"ok": False, "error": "无可用分析结果"}
    return {"ok": True, "restored": True,
            "payload": _build_payload(STATE["df"], STATE["events"], STATE["info"],
                                      ["已从本地缓存恢复上次分析结果（服务重启免重跑）。"])}


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


# PII 脱敏：手机号 / 身份证 / 姓名称呼（市民姓名等敏感信息一律遮罩）
import re as _re
_PII_PHONE = _re.compile(r'1[3-9]\d{9}')
_PII_IDCARD = _re.compile(r'\d{17}[\dXx]')
_PII_NAME = _re.compile(r'([\u4e00-\u9fa5])(?:先生|女士|同志|小姐|同学)')

def mask_pii(text) -> str:
    """对文本中的手机号/身份证/姓名称呼做脱敏，返回安全字符串。"""
    if not text:
        return ""
    s = str(text)
    s = _PII_PHONE.sub(lambda m: m.group()[:3] + "****" + m.group()[-2:], s)
    s = _PII_IDCARD.sub(lambda m: m.group()[:6] + "********" + m.group()[-4:], s)
    s = _PII_NAME.sub(lambda m: m.group(1) + "*", s)
    return s


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
    multi_total = int(df["is_multi_freq"].groupby(df["cluster_id"]).any().sum()) \
        if "is_multi_freq" in df.columns else len(events)

    # 事件按频次降序，限量展示 Top N
    max_events = getattr(config, "MAX_DISPLAY_EVENTS", 100)
    ranked = sorted(events, key=lambda e: -int(e.get("frequency", 0)))
    shown_events = ranked[:max_events]

    # 现算补充指标（峰值单日/涉及区域数）：不依赖缓存里的 dedup_metrics，改版即时生效
    need_cids = {e["cluster_id"] for e in shown_events if "cluster_id" in e}
    _grp = {cid: g for cid, g in df.groupby("cluster_id") if cid in need_cids} \
        if "cluster_id" in df.columns else {}

    def _extra_metrics(cid):
        g = _grp.get(cid)
        if g is None:
            return {}
        dm = {}
        tv = g["submit_time"].dropna() if "submit_time" in g.columns else pd.Series(dtype="datetime64[ns]")
        if not tv.empty:
            vc = tv.dt.date.value_counts()
            dm["peak_day_count"] = int(vc.iloc[0])
            dm["peak_day_date"] = vc.index[0].strftime("%m-%d")
        if "extracted_area" in g.columns:
            ac = g["extracted_area"].astype(str).str.strip()
            ac = ac[~ac.isin(["", "nan"])]
            dm["area_count"] = int(ac.nunique())
            if not ac.empty:
                dm["area_top_name"] = str(ac.value_counts().index[0])
        return dm

    events_json = []
    for e in shown_events:
        dm = dict(e.get("dedup_metrics", {}))
        dm.update(_extra_metrics(e.get("cluster_id")))
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
            "trend": e.get("trend", ""),
            "dedup_metrics": dm,
            "samples": [mask_pii(s) if isinstance(s, str) else
                        {"order_id": s.get("order_id", ""),
                         "content": mask_pii(str(s.get("content", "")))}
                        for s in e.get("sample_orders", [])],
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
            "content": mask_pii(truncate(r.content, 60)),
            "subject": r.extracted_subject,
            "event": r.extracted_event,
            "area": r.extracted_area,
            "time": _fmt_time(r.submit_time),
            "size": int(r.cluster_size),
            "multi": bool(r.is_multi_freq),
        })

    if len(ranked) > max_events:
        warnings = warnings + ["事件总数 %d 个，看板按频次展示 Top %d。" % (len(ranked), max_events)]
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
    return {"ok": True, "files": files, "feishu_sources": _load_feishu_sources()}


@app.post("/api/clear_cache")
def clear_cache():
    """清空分析结果缓存并重置当前会话，下次分析将全量重跑。"""
    removed = 0
    try:
        if os.path.isdir(RESULT_CACHE_DIR):
            for name in os.listdir(RESULT_CACHE_DIR):
                if name.startswith("result_") and name.endswith(".pkl"):
                    os.remove(os.path.join(RESULT_CACHE_DIR, name))
                    removed += 1
    except Exception as e:
        return {"ok": False, "error": "清理失败：%s" % e}
    STATE.update(df=None, events=None, source_meta=None, info={})
    _SEM.update(df_id=None, matrix=None)
    _GIDX.update(df_id=None, buckets=None)
    return {"ok": True, "removed": removed}


FEISHU_SOURCES_PATH = os.path.join(config.OUTPUT_DIR, "feishu_sources.json")


# ============================ 流水线实时状态 ============================
import time as _time

_PIPELINE = {
    "running": False, "t_start": 0.0, "t_stage": 0.0,
    "source": "", "stages": [], "error": "", "finished": False,
}
_STAGE_DEFS = (
    ("load", "加载工单数据"),
    ("clean", "textclean 清洗与实体抽取"),
    ("cluster", "语义聚类与多频识别"),
    ("llm", "LLM 判重合并事件簇"),
    ("profile", "生成事件画像"),
    ("finish", "结果落盘与会话更新"),
)


def _stage_begin(source):
    _PIPELINE.update(running=True, t_start=_time.time(), t_stage=_time.time(),
                     source=str(source or ""), error="", finished=False,
                     stages=[{"key": k, "name": n, "status": "pending", "ms": 0}
                             for k, n in _STAGE_DEFS])


def _stage_set(key):
    now = _time.time()
    for s in _PIPELINE["stages"]:
        if s["status"] == "running":
            s["status"] = "done"
            s["ms"] = int((now - _PIPELINE["t_stage"]) * 1000)
        if key is not None and s["key"] == key and s["status"] == "pending":
            s["status"] = "running"
    _PIPELINE["t_stage"] = now


def _stage_skip(key):
    for s in _PIPELINE["stages"]:
        if s["key"] == key and s["status"] == "pending":
            s["status"] = "skip"


def _stage_end(error=""):
    _stage_set(None)
    for s in _PIPELINE["stages"]:
        if s["status"] == "pending":
            s["status"] = "skip"
    _PIPELINE.update(running=False, finished=True, error=str(error or ""))


@app.get("/api/status")
def pipeline_status():
    """后台流水线实时状态（前端弹层轮询）。"""
    stages = _PIPELINE["stages"]
    restored = (not stages) and (STATE["df"] is not None)
    if restored:
        stages = [{"key": "restore", "name": "本地缓存恢复（服务重启免重跑）",
                   "status": "done", "ms": None}]
    return {
        "ok": True,
        "running": _PIPELINE["running"],
        "finished": _PIPELINE["finished"],
        "source": _PIPELINE["source"],
        "error": _PIPELINE["error"],
        "restored": restored,
        "elapsed_ms": (int((_time.time() - _PIPELINE["t_start"]) * 1000)
                       if _PIPELINE["t_start"] else 0),
        "stages": stages,
        "has_result": STATE["df"] is not None,
    }


def _load_feishu_sources():
    try:
        import json as _json
        with open(FEISHU_SOURCES_PATH, encoding="utf-8") as f:
            return _json.load(f)
    except Exception:
        return []


def _save_feishu_sources(sources):
    import json as _json
    os.makedirs(os.path.dirname(FEISHU_SOURCES_PATH), exist_ok=True)
    with open(FEISHU_SOURCES_PATH, "w", encoding="utf-8") as f:
        _json.dump(sources, f, ensure_ascii=False, indent=2)


@app.post("/api/feishu_source")
async def add_feishu_source(body: dict):
    """保存一个飞书在线表格数据源。"""
    url = (body.get("url") or "").strip()
    name = (body.get("name") or "").strip()
    if not url:
        return {"ok": False, "error": "链接不能为空"}
    if not name:
        name = "飞书表格"
    sources = _load_feishu_sources()
    src_id = "fs_" + str(abs(hash(url)))[-8:]
    for s in sources:
        if s["url"] == url:
            s["name"] = name
            _save_feishu_sources(sources)
            return {"ok": True, "source": s}
    src = {"id": src_id, "name": name, "url": url}
    sources.append(src)
    _save_feishu_sources(sources)
    return {"ok": True, "source": src}


@app.delete("/api/feishu_source/{src_id}")
def del_feishu_source(src_id: str):
    sources = _load_feishu_sources()
    sources = [s for s in sources if s["id"] != src_id]
    _save_feishu_sources(sources)
    return {"ok": True}


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
async def preview(file: UploadFile = File(None), datafile: str = Form(""), feishu_url: str = Form("")):
    """数据加载确认：真实读取条数/时间范围/区域数，供第一屏展示（禁止写死）。"""
    try:
        if file is not None and getattr(file, "filename", ""):
            content = await file.read()
            if not content.strip():
                return {"ok": False, "error": "上传的文件为空。"}
            df = loader.load_orders(_read_upload(file.filename, content))
        elif feishu_url:
            df_raw, meta = feishu_loader.fetch_records(feishu_url)
            df = loader.load_orders(df_raw)
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
    feishu_url: str = Form(""),
    scope: str = Form("all"),
    freq_threshold: int = Form(config.FREQ_THRESHOLD),
    eps: float = Form(config.CLUSTER_EPS),
    min_samples: int = Form(config.CLUSTER_MIN_SAMPLES),
    use_embedding: bool = Form(False),
    use_llm: bool = Form(False),
    llm_key: str = Form(""),
):
    """执行全链路分析：加载→标准化→实体→聚类→多频→画像。"""
    warnings = []
    try:
        # ---- 数据源：上传文件 > 飞书表格 > data/input 本地文件 > 内置样例 ----
        df = None
        cache_seed = None
        STATE["source_meta"] = None
        if file is not None and getattr(file, "filename", ""):
            _stage_begin("上传文件：" + file.filename)
            _stage_set("load")
            content = await file.read()
            if not content.strip():
                _stage_end("上传的文件为空。")
                return {"ok": False, "error": "上传的文件为空。"}
            try:
                df_raw = _read_upload(file.filename, content)
            except Exception as e:
                _stage_end("文件读取失败：%s" % e)
                return {"ok": False, "error": "文件读取失败：%s。请确认为有效 CSV/Excel。" % e}
            df = loader.load_orders(df_raw)
        elif feishu_url:
            _stage_begin("飞书多维表格")
            _stage_set("load")
            try:
                df_raw, meta = feishu_loader.fetch_records(feishu_url)
            except Exception as e:
                _stage_end("飞书数据拉取失败：%s" % e)
                return {"ok": False, "error": "飞书数据拉取失败：%s" % e}
            STATE["source_meta"] = meta
            cache_seed = "feishu|%s|%s|%d" % (meta["app_token"], meta["table_id"], meta["rows"])
            warnings.append("已从飞书多维表格加载 %d 条工单（%d 个字段）。"
                            % (meta["rows"], len(meta["fields"])))
        elif datafile:
            # 防路径穿越：只允许 data/input 下的文件名
            safe_name = os.path.basename(datafile)
            _stage_begin("本地文件：" + safe_name)
            _stage_set("load")
            path = os.path.join(config.INPUT_DIR, safe_name)
            if not os.path.exists(path):
                _stage_end("本地数据文件不存在：%s" % safe_name)
                return {"ok": False, "error": "本地数据文件不存在：%s" % safe_name}
            try:
                df = loader.load_orders_cached(path)
            except Exception as e:
                _stage_end("本地文件读取失败：%s" % e)
                return {"ok": False, "error": "本地文件读取失败：%s" % e}
            st_info = os.stat(path)
            cache_seed = "file|%s|%d|%d" % (safe_name, int(st_info.st_mtime), st_info.st_size)
        else:
            _stage_begin("内置样例数据")
            _stage_set("load")
            sample = os.path.join(config.INPUT_DIR, "sample.csv")
            if not os.path.exists(sample):
                _stage_end("未上传文件，且内置样例数据缺失。")
                return {"ok": False, "error": "未上传文件，且内置样例数据缺失。"}
            df_raw = pd.read_csv(sample)
            df = loader.load_orders(df_raw)
            cache_seed = "sample"

        if df.empty:
            _stage_end("未读取到有效工单")
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
                cache_seed + "|tcm1.3", scope, freq_threshold, eps, min_samples, use_embedding, use_llm)
            cached = _load_result_cache(cache_key)
            if cached is not None:
                STATE["df"], STATE["events"] = cached["df"], cached["events"]
                STATE["info"] = cached.get("info", {}) or {}
                _SEM.update(df_id=None, matrix=None)
                _GIDX.update(df_id=None, buckets=None)
                warnings.append("已命中分析缓存，结果与上次相同参数分析一致。")
                _stage_end("命中分析缓存，秒级返回")
                return {"ok": True, "payload": _build_payload(
                    cached["df"], cached["events"], cached["info"], warnings)}

        # ---- 流水线（清洗/抽取已切换至 textclean_module；线程池执行避免阻塞状态轮询） ----
        def _pipeline_body(df, eps, min_samples, use_embedding, use_llm, llm_key, freq_threshold):
            info, llm_msgs = {}, []
            _stage_set("clean")
            df = _run_textclean(df)
            _stage_set("cluster")
            df, info = cluster_mod.cluster_orders(
                df, eps=eps, min_samples=min_samples, use_embedding=use_embedding)
            if use_llm:
                _stage_set("llm")
                df, llm_info = llm_dedup.merge_clusters_by_llm(df, llm_key=llm_key)
                llm_msgs = llm_info.get("messages", [])
            else:
                _stage_skip("llm")
            df, _ = classifier.classify_multi_freq(df, freq_threshold=freq_threshold)
            _stage_set("profile")
            events = event_profiler.build_event_profiles(df)
            return df, events, info, llm_msgs

        from fastapi.concurrency import run_in_threadpool
        try:
            df, events, info, llm_msgs = await run_in_threadpool(
                _pipeline_body, df, eps, min_samples, use_embedding, use_llm, llm_key, freq_threshold)
            warnings.extend(info.get("messages", []))
            warnings.extend(llm_msgs)
        except Exception as e:
            events, info, warnings = [], {}, warnings + ["分析流水线失败：%s" % e]

        _stage_set("finish")
        STATE["df"], STATE["events"], STATE["info"] = df, events, info
        _SEM.update(df_id=None, matrix=None)
        _GIDX.update(df_id=None, buckets=None)
        if cache_key:
            _save_result_cache(cache_key, {"df": df, "events": events, "info": info})
        _stage_end()
        return {"ok": True, "payload": _build_payload(df, events, info, warnings)}
    except Exception as e:
        _stage_end("分析过程异常：%s" % e)
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

    multi_mask = df.get("is_multi_freq", pd.Series([False] * len(df), index=df.index)).astype(bool)

    # 区域聚合（带坐标，供首页地图切换）
    from utils.helpers import load_area_coords
    coords = load_area_coords()
    areas_col = df.get("extracted_area", pd.Series([""] * len(df), index=df.index)).astype(str).str.strip()
    area_rows = []
    for area, cnt in areas_col[areas_col != ""].value_counts().items():
        if area not in coords:
            continue
        lat, lon = coords[area]
        a_mask = areas_col == area
        a_multi = df.loc[a_mask & multi_mask]
        top_events = []
        if not a_multi.empty and "cluster_id" in a_multi.columns:
            for cid, g in a_multi.groupby("cluster_id"):
                ev = cmap.get(cid)
                if not ev:
                    continue
                top_events.append({
                    "event_id": ev["event_id"],
                    "name": (ev["event_subject"] if not ev["event_subject"].startswith(("（", "多主体聚合"))
                             else "") + (" · " if not ev["event_subject"].startswith(("（", "多主体聚合")) else "") + ev["event_type"],
                    "frequency": int(len(g)),
                })
            top_events.sort(key=lambda x: x["frequency"], reverse=True)
            top_events = top_events[:5]
        area_rows.append({
            "area": area, "lat": lat, "lon": lon,
            "total": int(cnt),
            "multi": int(multi_mask[a_mask].sum()),
            "top_events": top_events,
        })

    # 全局日趋势
    t = df["submit_time"].dropna()
    daily_all = []
    if not t.empty:
        daily = t.dt.date.value_counts().sort_index()
        daily_all = [{"date": str(k), "count": int(v)} for k, v in daily.items()]

    # 分区域日趋势（供前端按区域筛选切换趋势图）
    daily_by_area = {}
    for area in set(a["area"] for a in area_rows):
        a_mask = areas_col == area
        ta = df.loc[a_mask, "submit_time"].dropna()
        if ta.empty:
            continue
        d = ta.dt.date.value_counts().sort_index()
        daily_by_area[area] = [{"date": str(k), "count": int(v)} for k, v in d.items()]

    return {
        "ok": True,
        "source": {
            "rows": int(len(df)),
            "time_min": t.min().strftime("%Y-%m-%d") if not t.empty else None,
            "time_max": t.max().strftime("%Y-%m-%d") if not t.empty else None,
            "meta": STATE.get("source_meta"),
        },
        "areas": area_rows,
        "daily_all": daily_all,
        "daily_by_area": daily_by_area,
        "multi_orders": int(multi_mask.sum()),
        "subjects": _top_subjects(df, 30),
        "top_subjects": _top_subjects(df, 30),
        "categories": _top_categories(df, 30),
    }


def _top_subjects(df, top_n=30):
    """全部工单中识别到的高频主体（如某广场、某派出所、某街道办等）。"""
    if "extracted_subject" not in df.columns:
        return []
    s = df["extracted_subject"].astype(str).str.strip()
    s = s[(s != "") & (s != "nan") & (~s.str.startswith("多主体聚合"))]
    if s.empty:
        return []
    vc = s.value_counts().head(top_n)
    return [{"subject": str(k), "count": int(v)} for k, v in vc.items()]


def _top_categories(df, top_n=30):
    """全部工单中的高频诉求分类（textclean 事件类型）。"""
    if "extracted_event" not in df.columns:
        return []
    s = df["extracted_event"].astype(str).str.strip()
    s = s[(s != "") & (s != "nan")]
    if s.empty:
        return []
    vc = s.value_counts().head(top_n)
    return [{"category": str(k), "count": int(v)} for k, v in vc.items()]


@app.get("/api/orders")
def orders(page: int = 1, size: int = 50, q: str = "", area: str = "",
           subject: str = "", multi: str = "", category: str = "",
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
    if category:
        view = view[view.get("extracted_event", pd.Series(index=view.index)).astype(str) == category]
    if event:
        ev = next((e for e in (STATE["events"] or []) if e["event_id"] == event), None)
        view = view[view["cluster_id"] == ev["cluster_id"]] if ev else view.iloc[0:0]

    # 排序（默认：多频优先 + 频次降序）
    if sort == "time":
        view = view.sort_values("submit_time", ascending=False, na_position="last")
    else:
        view = view.assign(_b=view["cluster_size"]).sort_values("_b", ascending=False)

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
            "content": mask_pii(truncate(r.content, 60)),
            "area": str(getattr(r, "extracted_area", "") or ""),
            "subject": str(getattr(r, "extracted_subject", "") or ""),
            "event": ev.get("event_type", "") if ev else "",
            "event_id": ev.get("event_id", "") if ev else "",
            "size": int(r.cluster_size),
            "multi": bool(r.is_multi_freq),
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

    # 关联工单：同簇同地点（含语义兜底）+ 跨簇同地点（地址/主体硬信号），
    # 判定均为纯对称函数 → A↔B 必互相命中；全量返回不截断
    mates_all = []
    cur = {
        "addr": str(r.get("addr_norm", "") or ""),
        "comm": str(r.get("addr_community", "") or ""),
        "bld": str(r.get("addr_building", "") or ""),
        "subj": str(r.get("extracted_subject", "") or ""),
        "area": str(r.get("extracted_area", "") or ""),
    }
    matched = {oid}

    def _mate_row(t):
        return {
            "order_id": str(t.order_id),
            "content": mask_pii(truncate(t.content, 70)),
            "area": str(getattr(t, "extracted_area", "") or ""),
            "time": _fmt_time(t.submit_time),
        }

    if cid != -1:
        cur_no_addr = not any((cur["addr"], cur["comm"], cur["bld"]))
        sem = _semantic_matrix(df) if cur_no_addr else None
        pos = ({str(v): i for i, v in enumerate(df["order_id"].astype(str))}
               if sem is not None else None)
        my_pos = pos.get(oid) if pos else None
        same = df[(df["cluster_id"] == cid) & (df["order_id"].astype(str) != oid)]
        for m in same.itertuples(index=False):
            mb = {
                "addr": str(getattr(m, "addr_norm", "") or ""),
                "comm": str(getattr(m, "addr_community", "") or ""),
                "bld": str(getattr(m, "addr_building", "") or ""),
                "subj": str(getattr(m, "extracted_subject", "") or ""),
                "area": str(getattr(m, "extracted_area", "") or ""),
            }
            sim = None
            if sem is not None and my_pos is not None and \
                    not any((mb["addr"], mb["comm"], mb["bld"])):
                mp = pos.get(str(m.order_id))
                if mp is not None:
                    sim = float((sem[my_pos] * sem[mp]).sum())  # 向量已L2归一化，点积即余弦
            if _is_same_place(cur, mb, sim):
                matched.add(str(m.order_id))
                mates_all.append(_mate_row(m))

    # 跨簇同址：同地址的不同事件工单（sim=None → 语义兜底仅限同簇，跨簇只用硬信号）
    my_keys = _addr_bucket_keys(cur["addr"], cur["comm"], cur["bld"], cur["subj"], cur["area"])
    for k in my_keys:
        for t in _global_addr_buckets(df).get(k, ()):
            toid = str(t.order_id)
            if toid in matched:
                continue
            tb = {
                "addr": str(getattr(t, "addr_norm", "") or ""),
                "comm": str(getattr(t, "addr_community", "") or ""),
                "bld": str(getattr(t, "addr_building", "") or ""),
                "subj": str(getattr(t, "extracted_subject", "") or ""),
                "area": str(getattr(t, "extracted_area", "") or ""),
            }
            if _is_same_place(cur, tb):
                matched.add(toid)
                mates_all.append(_mate_row(t))

    mates_all.sort(key=lambda x: x["time"], reverse=True)
    mates = mates_all

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
                    "reason": "事件类型同为“%s”，但区域/主体不同，判定不属于同一工单" % ev.get("event_type", ""),
                })
            if len(similar) >= 3:
                break

    detail_addr = str(r.get("addr_norm", "") or "") or " ".join(
        p for p in (str(r.get("addr_community", "") or ""),
                    str(r.get("addr_building", "") or "")) if p)

    return {
        "ok": True,
        "order": {
            "order_id": oid,
            "title": str(getattr(r, "title", "") or ""),
            "content": mask_pii(str(r.content)),
            "subject_raw": str(getattr(r, "subject", "") or ""),
            "area_raw": str(getattr(r, "area", "") or ""),
            "time": _fmt_time(r.submit_time),
        },
        "ai": {
            "subject": str(getattr(r, "extracted_subject", "") or ""),
            "event": str(getattr(r, "extracted_event", "") or ""),
            "area": str(getattr(r, "extracted_area", "") or ""),
            "detail_addr": detail_addr,
            "normalized": mask_pii(str(getattr(r, "normalized_content", "") or "")),
        },
        "cluster": {
            "event_id": ev.get("event_id", "") if ev else "",
            "event_type": ev.get("event_type", "") if ev else "",
            "frequency": ev.get("frequency", int(r.cluster_size)) if ev else int(r.cluster_size),
            "is_multi": bool(r.is_multi_freq),
        },
        "mates": mates,
        "mates_total": len(mates_all),
        "basis": basis,
        "similar": similar,
    }


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="127.0.0.1", port=8600)
