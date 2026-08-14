# -*- coding: utf-8 -*-
"""
模块 2：工单内容标准化（normalizer.py）

职责：
- 去除特殊符号与噪声、统一全半角
- 依据本地 synonyms.csv 做同义词归一
- 保留原始 content，仅新增 normalized_content 字段
- 全程离线，不调用任何大模型/付费 API
"""
import re
import unicodedata

from utils.helpers import load_synonyms


def _full_to_half(text: str) -> str:
    """全角字符转半角，统一宽度。"""
    return unicodedata.normalize("NFKC", text)


def _clean_noise(text: str) -> str:
    """去除 URL、多余空白及非中英文数字的噪声符号。"""
    # 去网址
    text = re.sub(r"https?://\S+", " ", text)
    # 仅保留中文、字母、数字与空格
    text = re.sub(r"[^\u4e00-\u9fa5A-Za-z0-9 ]", " ", text)
    # 压缩连续空白
    text = re.sub(r"\s+", " ", text).strip()
    return text


def normalize_text(text: str, syn_map: dict) -> str:
    """对单条文本做标准化：全半角统一 -> 去噪 -> 同义词归一。"""
    if not text:
        return ""
    text = _full_to_half(str(text))
    text = _clean_noise(text)
    # 同义词归一（按词典逐条替换）
    for raw, std in syn_map.items():
        if raw and raw in text:
            text = text.replace(raw, std)
    return text


def normalize_orders(df):
    """
    对整个工单 DataFrame 增加 normalized_content 字段。

    原始 content 字段保持不变，保证可追溯。
    """
    syn_map = load_synonyms()
    df = df.copy()
    df["normalized_content"] = df["content"].apply(lambda x: normalize_text(x, syn_map))
    return df
