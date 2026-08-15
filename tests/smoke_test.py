# -*- coding: utf-8 -*-
"""
无头链路验证脚本（smoke_test.py）

不启动 Streamlit，直接串起 loader→normalizer→entity_extractor→cluster→
classifier→event_profiler→exporter，用于快速确认最小闭环是否跑通。

用法：python smoke_test.py
"""
import os
import sys

if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pandas as pd

import config
from modules import (
    loader, normalizer, entity_extractor,
    cluster as cluster_mod, classifier,
    event_profiler, exporter,
)

SEP = "=" * 60


def main():
    sample = os.path.join(config.INPUT_DIR, "sample.csv")
    assert os.path.exists(sample), "样例数据缺失：%s" % sample

    print(SEP)
    print("[1] 加载 sample.csv")
    df_raw = pd.read_csv(sample)
    df = loader.load_orders(df_raw)
    print("  有效工单数：%d" % len(df))

    print(SEP)
    print("[2] 标准化")
    df = normalizer.normalize_orders(df)
    print("  示例：%s -> %s" % (df.iloc[0]["content"], df.iloc[0]["normalized_content"]))

    print(SEP)
    print("[3] 实体识别")
    df = entity_extractor.extract_entities(df)
    print(df[["extracted_subject", "extracted_event", "extracted_area"]].head(3).to_string())

    print(SEP)
    print("[4] 聚类")
    df, info = cluster_mod.cluster_orders(df)
    print("  路线：%s" % info["method"])
    print("  有效簇：%d  覆盖率：%.0f%%  噪声：%d" % (
        info["n_clusters"], info["coverage"] * 100, info["noise_count"]))

    print(SEP)
    print("[5] 多频识别")
    df, multi_ids = classifier.classify_multi_freq(df)
    print("  多频簇数量：%d" % len(multi_ids))

    print(SEP)
    print("[6] 事件画像")
    events = event_profiler.build_event_profiles(df)
    for e in events:
        print("  %s %s·%s | 频次%d | %s ~ %s" % (
            e["event_id"], e["event_subject"], e["event_type"],
            e["frequency"], e["first_seen"], e["last_seen"]))

    print(SEP)
    print("[7] 导出")
    results = exporter.build_results_table(df, events)
    csv_bytes = exporter.export_csv_bytes(results)
    excel_bytes = exporter.export_excel_bytes(results, events)
    os.makedirs(config.OUTPUT_DIR, exist_ok=True)
    csv_path = os.path.join(config.OUTPUT_DIR, "multi_freq_result.csv")
    xlsx_path = os.path.join(config.OUTPUT_DIR, "multi_freq_result.xlsx")
    with open(csv_path, "wb") as f:
        f.write(csv_bytes)
    with open(xlsx_path, "wb") as f:
        f.write(excel_bytes)
    print("  CSV：%d 字节 -> %s" % (len(csv_bytes), csv_path))
    print("  Excel：%d 字节 -> %s" % (len(excel_bytes), xlsx_path))

    print(SEP)
    print("链路验证通过：共识别 %d 个多频事件。" % len(events))


if __name__ == "__main__":
    main()
