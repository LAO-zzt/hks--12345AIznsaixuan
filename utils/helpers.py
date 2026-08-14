# -*- coding: utf-8 -*-
"""
通用工具函数（helpers.py）

提供词典加载、目录保障、安全取值等基础能力。
所有函数遵循“失败不抛出、优雅降级”原则。
"""
import os
import pandas as pd

import config


def ensure_dirs():
    """确保输入/输出/词典目录存在（不存在则创建）。"""
    for d in (config.INPUT_DIR, config.OUTPUT_DIR, config.DICT_DIR, config.MODEL_DIR):
        try:
            os.makedirs(d, exist_ok=True)
        except Exception:
            # 目录创建失败不阻断主流程
            pass


def load_synonyms() -> dict:
    """
    加载同义词归一词典 synonyms.csv。

    文件格式：两列（raw,standard），将 raw 统一替换为 standard。
    加载失败时返回空字典（不阻断流程）。
    """
    path = os.path.join(config.DICT_DIR, "synonyms.csv")
    mapping = {}
    try:
        df = pd.read_csv(path, dtype=str).fillna("")
        for _, row in df.iterrows():
            raw = str(row.iloc[0]).strip()
            std = str(row.iloc[1]).strip()
            if raw and std:
                mapping[raw] = std
    except Exception:
        # 词典缺失时不做任何归一，流程继续
        return {}
    # 长词优先替换，避免短词先行破坏长词匹配
    return dict(sorted(mapping.items(), key=lambda kv: len(kv[0]), reverse=True))


def load_dict_lines(filename: str) -> list:
    """
    按行加载词典文件（subjects.txt / events.txt / sensitive_events.txt）。

    失败时返回空列表，不阻断主流程。
    """
    path = os.path.join(config.DICT_DIR, filename)
    try:
        with open(path, "r", encoding="utf-8") as f:
            return [ln.strip() for ln in f if ln.strip()]
    except Exception:
        return []


def truncate(text: str, n: int = 60) -> str:
    """截断文本用于展示，超长加省略号。"""
    text = str(text)
    return text if len(text) <= n else text[: n] + "…"
