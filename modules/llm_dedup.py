# -*- coding: utf-8 -*-
"""LLM 判重增强。

聚类后对"同主体+同区域但不同簇"的候选对调用 LLM 判断是否同一事件，
是则合并 cluster_id，提升召回率。未配置 API Key 时自动跳过。
"""
import os
import requests
import pandas as pd

import config


def _resolve_key(llm_key=None):
    k = (llm_key or "").strip() or getattr(config, "DEEPSEEK_API_KEY", "")
    return k.strip() or None


def _ask_llm(samples_a, samples_b, api_key):
    prompt = (
        "你是工单判重助手。下面两组市民诉求工单，请判断它们是否描述同一事件"
        "（同一问题发生在同一主体/地点的重复反映）。\n\n"
        "A组样本：\n" + "\n".join("%d. %s" % (i + 1, s) for i, s in enumerate(samples_a)) +
        "\n\nB组样本：\n" + "\n".join("%d. %s" % (i + 1, s) for i, s in enumerate(samples_b)) +
        "\n\n只回答「是」或「否」。"
    )
    try:
        r = requests.post(
            os.path.join(getattr(config, "DEEPSEEK_BASE_URL", "https://api.deepseek.com"), "v1/chat/completions"),
            headers={"Authorization": "Bearer " + api_key, "Content-Type": "application/json"},
            json={
                "model": getattr(config, "DEEPSEEK_MODEL", "deepseek-chat"),
                "messages": [{"role": "user", "content": prompt}],
                "temperature": 0,
                "max_tokens": 5,
            },
            timeout=20,
        )
        r.raise_for_status()
        ans = r.json()["choices"][0]["message"]["content"].strip()
        return ans.startswith("是")
    except Exception:
        return False


def merge_clusters_by_llm(df, llm_key=None, on_progress=None):
    """对 df 做基于 LLM 的簇合并，返回 (df, info)。

    info: {"enabled": bool, "merged_pairs": int, "messages": [...]}
    """
    info = {"enabled": False, "merged_pairs": 0, "messages": []}
    api_key = _resolve_key(llm_key)
    if not api_key:
        info["messages"].append("LLM 判重：未配置 API Key，已跳过。")
        return df, info
    info["enabled"] = True

    if "cluster_id" not in df.columns or "extracted_subject" not in df.columns:
        info["messages"].append("LLM 判重：缺少必要字段，已跳过。")
        return df, info

    valid = df[df["cluster_id"] >= 0].copy()
    if valid.empty:
        return df, info

    for col in ("extracted_subject", "extracted_area"):
        if col not in valid.columns:
            valid[col] = ""
        valid[col] = valid[col].fillna("").astype(str).str.strip()

    grouped = valid.groupby(["extracted_subject", "extracted_area"])
    sample_n = max(2, int(getattr(config, "LLM_DEDUP_SAMPLE", 2)))
    max_pairs = int(getattr(config, "LLM_DEDUP_MAX_PAIRS", 30))

    candidates = []
    for (subj, area), g in grouped:
        if not subj:
            continue
        cids = [c for c in g["cluster_id"].unique() if c >= 0]
        if len(cids) < 2:
            continue
        sizes = {c: len(g[g["cluster_id"] == c]) for c in cids}
        cids_sorted = sorted(cids, key=lambda c: -sizes[c])
        for i in range(1, len(cids_sorted)):
            candidates.append((cids_sorted[0], cids_sorted[i], subj, area))
        if len(candidates) >= max_pairs:
            break

    if not candidates:
        info["messages"].append("LLM 判重：未发现待合并的候选簇对。")
        return df, info

    merge_map = {}
    done = 0
    for big, small, subj, area in candidates[:max_pairs]:
        if on_progress:
            on_progress(done, min(len(candidates), max_pairs))
        rows_big = valid[valid["cluster_id"] == big]["content"].head(sample_n).tolist()
        rows_small = valid[valid["cluster_id"] == small]["content"].head(sample_n).tolist()
        if not rows_big or not rows_small:
            continue
        if _ask_llm(rows_big, rows_small, api_key):
            merge_map[small] = big
            info["merged_pairs"] += 1
        done += 1

    if merge_map:
        def _remap(c):
            seen = set()
            while c in merge_map and c not in seen:
                seen.add(c)
                c = merge_map[c]
            return c
        df = df.copy()
        df["cluster_id"] = df["cluster_id"].apply(
            lambda c: _remap(c) if c in merge_map else c)
        info["messages"].append("LLM 判重：合并 %d 个簇对。" % len(merge_map))
    else:
        info["messages"].append("LLM 判重：检查 %d 个候选对，未发现可合并。" % done)

    return df, info
