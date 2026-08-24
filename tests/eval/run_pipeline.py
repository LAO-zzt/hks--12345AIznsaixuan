# -*- coding: utf-8 -*-
"""
评测用流水线运行器（run_pipeline.py）

与 webapp/server.py 完全一致的链路：
    load_orders → textclean 清洗/实体抽取 → cluster_orders → classify_multi_freq
把每个工单的 cluster_id 与抽取字段落地 pickle 缓存，供评测脚本复用。

用法：
    python tests/eval/run_pipeline.py [max_rows]
    max_rows 可选（默认全量）；传小值（如 3000）用于快速验证/取样。
"""
import os
import sys
import time
import pickle

if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

import pandas as pd

import config
from modules import loader, cluster as cluster_mod, classifier


def get_textclean_pipeline():
    import logging
    logging.getLogger("ticket_cleaner").setLevel(logging.ERROR)
    import textclean_module as tcm
    data_dir = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                            "textclean_module", "data")
    os.makedirs(data_dir, exist_ok=True)
    cfg = tcm.Config(db_path=os.path.join(data_dir, "cleaner_eval.db"),
                     work_dir=data_dir, source_excel_path="")
    from ticket_cleaner.pipeline import CleaningPipeline
    from ticket_cleaner.storage import Storage
    return CleaningPipeline(cfg, Storage(cfg.db_path))


def run_textclean(df):
    """textclean 清洗+实体抽取，映射到流水线标准列（与 webapp 一致）。"""
    pipeline = get_textclean_pipeline()
    from ticket_cleaner.schema import TicketRecord
    from ticket_cleaner.extractors import extract_organization, _is_blacklisted_subject
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

    def _pick_subject(tc_result, title, raw_subj):
        if tc_result and tc_result.organization_normalized:
            if not _is_blacklisted_subject(tc_result.organization_normalized):
                return tc_result.organization_normalized
        if title:
            c, _, _ = extract_organization(str(title))
            if c and not _is_blacklisted_subject(c):
                return c
        if raw_subj:
            c, _, _ = extract_organization(_nz(raw_subj))
            if c and not _is_blacklisted_subject(c):
                return c
        return ""

    df = df.copy()
    contents = df["content"].astype(str).tolist()
    titles = df["title"].astype(str).tolist() if "title" in df else [""] * len(df)
    subjects = df["subject"].astype(str).tolist() if "subject" in df else [""] * len(df)
    areas = df["area"].astype(str).tolist() if "area" in df else [""] * len(df)
    df["normalized_content"] = [
        (t.semantic_content or t.clean_content or c) for t, c in zip(cleaned, contents)]
    df["extracted_subject"] = [
        _pick_subject(t, title, raw) for t, title, raw in zip(cleaned, titles, subjects)]
    df["extracted_event"] = [t.event_type or "" for t in cleaned]
    df["extracted_area"] = [(t.town or _nz(raw)) for t, raw in zip(cleaned, areas)]
    df["addr_norm"] = [t.address_normalized or "" for t in cleaned]
    df["addr_community"] = [t.community or "" for t in cleaned]
    df["addr_building"] = [t.building or "" for t in cleaned]
    return df


def main():
    max_rows = int(sys.argv[1]) if len(sys.argv) > 1 else None

    files = [f for f in sorted(os.listdir(config.INPUT_DIR))
             if f.lower().endswith((".xlsx", ".xlsm", ".csv"))]
    assert files, "data/input 下没有数据文件"
    path = os.path.join(config.INPUT_DIR, files[0])

    t0 = time.time()
    df = loader.load_orders_cached(path)
    if max_rows:
        df = df.head(max_rows).reset_index(drop=True)
    print("[load] %d 条（%.1fs）" % (len(df), time.time() - t0))

    t0 = time.time()
    df = run_textclean(df)
    print("[clean] %.1fs" % (time.time() - t0))
    subj_hit = (df["extracted_subject"].astype(str).str.strip() != "").mean()
    ev_hit = (df["extracted_event"].astype(str).str.strip() != "").mean()
    area_hit = (df["extracted_area"].astype(str).str.strip() != "").mean()
    print("        抽取命中率：主体 %.1f%% 事件 %.1f%% 区域 %.1f%%" % (
        subj_hit * 100, ev_hit * 100, area_hit * 100))

    t0 = time.time()
    df, info = cluster_mod.cluster_orders(df)
    print("[cluster] %.1fs 路线=%s 簇=%d 覆盖=%.0f%%" % (
        time.time() - t0, info["method"], info["n_clusters"], info["coverage"] * 100))
    for m in info.get("messages", []):
        print("        %s" % m)

    t0 = time.time()
    df, multi_ids = classifier.classify_multi_freq(df)
    print("[classify] %.1fs 多频簇=%d（阈值=%d）" % (
        time.time() - t0, len(multi_ids), config.FREQ_THRESHOLD))

    out_path = os.path.join(config.OUTPUT_DIR, "eval_result.pkl")
    os.makedirs(config.OUTPUT_DIR, exist_ok=True)
    with open(out_path, "wb") as f:
        pickle.dump({
            "df": df[["order_id", "title", "content", "normalized_content",
                      "extracted_subject", "extracted_event", "extracted_area",
                      "addr_norm", "addr_community", "addr_building",
                      "cluster_id", "cluster_size", "is_multi_freq"]],
            "info": info,
        }, f)
    print("[save] %s" % out_path)

    print("\n== Top12 簇（按频次） ==")
    sizes = df["cluster_id"].value_counts()
    for cid, n in sizes.head(12).items():
        g = df[df["cluster_id"] == cid]
        ev = g["extracted_event"].mode().iloc[0] if not g["extracted_event"].mode().empty else ""
        subj = g["extracted_subject"].mode().iloc[0] if not g["extracted_subject"].mode().empty else ""
        title = g["title"].mode().iloc[0] if not g["title"].mode().empty else ""
        print("  #%-6d n=%-6d 事件=%-12s 主体=%-12s 标题=%s" % (
            cid, n, ev[:12], subj[:12], str(title)[:28]))


if __name__ == "__main__":
    main()
