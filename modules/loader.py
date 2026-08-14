# -*- coding: utf-8 -*-
"""
模块 1：数据加载与预处理（loader.py）

职责：
- 读取 CSV 工单数据（自动识别常见中英文列名）
- 清洗：去空行、按工单编号去重
- 时间字段统一为可计算格式
- 输出标准字段 DataFrame：order_id / content / subject / area / submit_time
"""
import pandas as pd

# 标准字段 -> 可能的中英文列名别名（小写比较）
FIELD_ALIASES = {
    "order_id": ["order_id", "工单编号", "工单号", "编号", "单号", "id", "工单id"],
    "content": ["content", "诉求内容", "工单内容", "内容", "诉求", "事件描述", "问题描述", "描述"],
    "subject": ["subject", "涉及主体", "主体", "涉事主体", "涉及对象", "对象", "被诉主体"],
    "area": ["area", "事发区域", "区域", "发生区域", "所属区域", "属地", "地点", "地址"],
    "submit_time": ["submit_time", "提交时间", "受理时间", "创建时间", "时间", "发生时间", "上报时间", "登记时间"],
}

# 输出的标准字段顺序
STANDARD_FIELDS = ["order_id", "content", "subject", "area", "submit_time"]


def _match_columns(df: pd.DataFrame) -> dict:
    """
    自动识别列名，返回 {标准字段: 实际列名}。

    匹配策略：
    1) 精确匹配（忽略大小写/首尾空格）；
    2) 包含匹配（别名出现在列名中，或列名出现在别名中）；
    3) 找不到则留空，交由下游兜底。
    """
    col_map = {}
    lower_cols = {str(c).strip().lower(): c for c in df.columns}

    for field, aliases in FIELD_ALIASES.items():
        matched = None
        # 1) 精确匹配
        for a in aliases:
            if a.lower() in lower_cols:
                matched = lower_cols[a.lower()]
                break
        # 2) 包含匹配
        if matched is None:
            for a in aliases:
                for lc, orig in lower_cols.items():
                    if a.lower() in lc or lc in a.lower():
                        matched = orig
                        break
                if matched is not None:
                    break
        col_map[field] = matched
    return col_map


def _to_datetime(series: pd.Series) -> pd.Series:
    """把时间列统一为 datetime；无法解析的置为 NaT，不抛错。"""
    return pd.to_datetime(series, errors="coerce")


def load_orders(df_raw: pd.DataFrame) -> pd.DataFrame:
    """
    接收原始 DataFrame（由 Streamlit 上传或本地读取），返回标准化后的工单 DataFrame。

    兜底原则：
    - 缺失 order_id：自动生成 AUTO_{行号}；
    - 缺失 content：置空字符串；
    - 缺失 subject/area：置空字符串；
    - 缺失/无法解析 submit_time：置 NaT（不阻断识别）。
    """
    df = df_raw.copy()
    col_map = _match_columns(df)

    result = pd.DataFrame(index=df.index)
    for field in STANDARD_FIELDS:
        src = col_map.get(field)
        if src is not None and src in df.columns:
            result[field] = df[src]
        else:
            # 位置/缺失兜底
            if field == "order_id":
                result[field] = [f"AUTO_{i}" for i in range(len(df))]
            elif field == "submit_time":
                result[field] = pd.NaT
            else:
                result[field] = ""

    # 统一时间格式
    result["submit_time"] = _to_datetime(result["submit_time"])

    # 文本列转字符串并去首尾空格
    for field in ["order_id", "content", "subject", "area"]:
        result[field] = result[field].fillna("").astype(str).str.strip()

    # 去除完全空行（content 为空视为无效工单）
    result = result[result["content"].str.len() > 0]

    # 按工单编号去重（保留首次出现）
    result = result.drop_duplicates(subset=["order_id"], keep="first")

    result = result.reset_index(drop=True)
    # 附带列映射信息，便于前端展示识别情况
    result.attrs["col_map"] = col_map
    return result


def load_csv_bytes(content: bytes) -> pd.DataFrame:
    """从上传的字节内容读取 CSV，兼容常见编码。"""
    import io
    for enc in ("utf-8-sig", "utf-8", "gbk", "gb18030", "latin-1"):
        try:
            return pd.read_csv(io.BytesIO(content), encoding=enc)
        except UnicodeDecodeError:
            continue
    # 最后兜底：强制读取
    return pd.read_csv(io.BytesIO(content), encoding="utf-8", errors="ignore")
