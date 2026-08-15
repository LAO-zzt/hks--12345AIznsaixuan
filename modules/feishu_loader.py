# -*- coding: utf-8 -*-
"""飞书多维表格数据加载器。

从飞书 Bitable 拉取工单记录，转为标准 DataFrame。
凭证读取 密钥/config.env；结果本地缓存避免重复拉取。
"""
import os
import time
import json
import requests
import pandas as pd

_CONFIG_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "密钥", "config.env")
_CACHE_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "feishu_cache.parquet")
_OPEN_BASE = "https://open.feishu.cn/open-apis"


def _load_creds():
    env = {}
    if not os.path.exists(_CONFIG_PATH):
        raise RuntimeError("未找到飞书凭证文件：%s" % _CONFIG_PATH)
    for line in open(_CONFIG_PATH, encoding="utf-8"):
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            env[k] = v
    app_id = env.get("FEISHU_APP_ID", "").strip()
    app_secret = env.get("FEISHU_APP_SECRET", "").strip()
    if not app_id or not app_secret:
        raise RuntimeError("config.env 中 FEISHU_APP_ID / FEISHU_APP_SECRET 为空")
    return app_id, app_secret


def _tenant_token():
    app_id, app_secret = _load_creds()
    r = requests.post(
        "%s/auth/v3/tenant_access_token/internal" % _OPEN_BASE,
        json={"app_id": app_id, "app_secret": app_secret},
        timeout=15,
    )
    r.raise_for_status()
    d = r.json()
    if d.get("code") != 0:
        raise RuntimeError("获取 tenant_access_token 失败：%s" % d)
    return d["tenant_access_token"]


def _parse_cell(v):
    """飞书单元格值 → 字符串。"""
    if v is None:
        return ""
    if isinstance(v, list):
        parts = []
        for it in v:
            if isinstance(it, dict):
                parts.append(it.get("text") or it.get("name") or "")
            else:
                parts.append(str(it))
        return "".join(parts)
    if isinstance(v, dict):
        return v.get("text") or v.get("name") or json.dumps(v, ensure_ascii=False)
    return str(v)


def _parse_app_token_from_url(url):
    """从飞书表格链接解析 app_token / table_id。"""
    if "/base/" in url:
        seg = url.split("/base/", 1)[1]
        app_token = seg.split("?")[0].split("/")[0]
    else:
        app_token = ""
    import urllib.parse as up
    qs = up.urlparse(url).query
    params = up.parse_qs(qs)
    table_id = (params.get("table") or [""])[0]
    if not app_token or not table_id:
        raise RuntimeError("无法从链接解析 app_token / table_id：%s" % url)
    return app_token, table_id


def _list_fields(token, app_token, table_id):
    r = requests.get(
        "%s/bitable/v1/apps/%s/tables/%s/fields" % (_OPEN_BASE, app_token, table_id),
        headers={"Authorization": "Bearer " + token},
        timeout=15,
    )
    r.raise_for_status()
    d = r.json()
    if d.get("code") != 0:
        raise RuntimeError("拉取字段失败：%s" % d)
    return [it["field_name"] for it in d["data"]["items"]]


def fetch_records(url, on_progress=None, use_cache=True):
    """从飞书表格拉全量记录，返回 (DataFrame, meta)。

    meta: {"source": "飞书多维表格", "rows": N, "fields": [...], "fetched_at": ts, "app_token":..., "table_id":...}
    """
    app_token, table_id = _parse_app_token_from_url(url)
    if use_cache and os.path.exists(_CACHE_PATH):
        try:
            df = pd.read_parquet(_CACHE_PATH)
            meta = {
                "source": "飞书多维表格（缓存）",
                "rows": len(df),
                "fields": list(df.columns),
                "fetched_at": int(os.path.getmtime(_CACHE_PATH)),
                "app_token": app_token,
                "table_id": table_id,
            }
            return df, meta
        except Exception:
            pass

    token = _tenant_token()
    fields = _list_fields(token, app_token, table_id)
    headers = {"Authorization": "Bearer " + token}
    rows = []
    page_token = None
    total = None
    page_size = 500
    while True:
        params = {"page_size": page_size}
        if page_token:
            params["page_token"] = page_token
        r = requests.get(
            "%s/bitable/v1/apps/%s/tables/%s/records" % (_OPEN_BASE, app_token, table_id),
            headers=headers, params=params, timeout=30,
        )
        r.raise_for_status()
        d = r.json()
        if d.get("code") != 0:
            raise RuntimeError("拉取记录失败：%s" % d)
        data = d["data"]
        if total is None:
            total = data.get("total", 0)
        for rec in data.get("items", []):
            f = rec.get("fields", {})
            rows.append({k: _parse_cell(f.get(k)) for k in fields})
        if on_progress:
            on_progress(len(rows), total)
        if not data.get("has_more"):
            break
        page_token = data.get("page_token")
        if not page_token:
            break

    df = pd.DataFrame(rows)
    os.makedirs(os.path.dirname(_CACHE_PATH), exist_ok=True)
    try:
        df.to_parquet(_CACHE_PATH, index=False)
    except Exception:
        pass
    meta = {
        "source": "飞书多维表格",
        "rows": len(df),
        "fields": fields,
        "fetched_at": int(time.time()),
        "app_token": app_token,
        "table_id": table_id,
    }
    return df, meta


def clear_cache():
    if os.path.exists(_CACHE_PATH):
        os.remove(_CACHE_PATH)
