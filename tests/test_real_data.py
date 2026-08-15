# -*- coding: utf-8 -*-
"""
真实数据端到端验证（test_real_data.py）

用 data/input 下的真实 12345 工单 xlsx 跑完整流水线，
打印各阶段耗时与 Top 事件，验证大数据路线可用。

用法：python test_real_data.py
"""
import glob
import os
import sys
import time

if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config
from modules import (
    loader, normalizer, entity_extractor,
    cluster as cluster_mod, classifier,
    event_profiler,
)


def main():
    files = glob.glob(os.path.join(config.INPUT_DIR, "*.xlsx"))
    assert files, "data/input 下没有 xlsx 真实数据文件"
    path = files[0]
    print("数据文件：%s（%.1f MB）" % (os.path.basename(path), os.path.getsize(path) / 1048576))

    t0 = time.time()
    df = loader.load_orders_cached(path)
    print("[1] 加载+清洗去重：%.1fs → %d 条（时间解析成功 %d 条）" % (
        time.time() - t0, len(df), int(df["submit_time"].notna().sum())))

    t0 = time.time()
    df = normalizer.normalize_orders(df)
    print("[2] 标准化：%.1fs" % (time.time() - t0))

    t0 = time.time()
    df = entity_extractor.extract_entities(df)
    area_hit = (df["extracted_area"].astype(str).str.strip() != "").mean()
    ev_hit = (df["extracted_event"].astype(str).str.strip() != "").mean()
    print("[3] 实体识别：%.1fs（区域命中 %.1f%%，词典事件命中 %.1f%%）" % (
        time.time() - t0, area_hit * 100, ev_hit * 100))

    t0 = time.time()
    df, info = cluster_mod.cluster_orders(df)
    print("[4] 聚类/分组：%.1fs 路线=%s 簇=%d 覆盖=%.0f%%" % (
        time.time() - t0, info["method"], info["n_clusters"], info["coverage"] * 100))
    for m in info.get("messages", []):
        print("    提示：%s" % m)

    t0 = time.time()
    df, multi_ids = classifier.classify_multi_freq(df)
    print("[5] 多频识别：%.1fs → 多频簇 %d 个（阈值=%d）" % (
        time.time() - t0, len(multi_ids), config.FREQ_THRESHOLD))

    t0 = time.time()
    events = event_profiler.build_event_profiles(df)
    print("[6] 事件画像：%.1fs → %d 个事件" % (time.time() - t0, len(events)))

    print("\n== Top15 多频事件（按频次降序） ==")
    for e in events[:15]:
        print("  %-8s %-22s 频次%-6d %s ~ %s" % (
            e["event_id"], e["event_type"][:20], e["frequency"],
            e["first_seen"], e["last_seen"]))


if __name__ == "__main__":
    main()
