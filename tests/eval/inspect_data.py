# -*- coding: utf-8 -*-
"""
数据摸底（inspect_data.py）

读取 data/input 下真实工单 xlsx，打印列映射 / 条数 / 时间范围 /
高频标题 / 高频区域，为构造标注测试集提供素材。

用法：python tests/eval/inspect_data.py
"""
import glob
import os
import sys

if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

import config
from modules import loader


def main():
    files = glob.glob(os.path.join(config.INPUT_DIR, "*.xlsx")) \
        + glob.glob(os.path.join(config.INPUT_DIR, "*.xlsm")) \
        + glob.glob(os.path.join(config.INPUT_DIR, "*.csv"))
    assert files, "data/input 下没有数据文件"
    path = sorted(files)[0]
    print("数据文件：%s（%.1f MB）" % (os.path.basename(path), os.path.getsize(path) / 1048576))

    df = loader.load_orders_cached(path)
    print("有效工单数：%d" % len(df))
    print("列映射：%s" % df.attrs.get("col_map", {}))
    print("原始列：%s" % list(df.columns))

    t = df["submit_time"].dropna()
    if not t.empty:
        print("时间范围：%s ~ %s（有效时间 %d/%d）" % (
            t.min().strftime("%Y-%m-%d"), t.max().strftime("%Y-%m-%d"),
            len(t), len(df)))
    else:
        print("时间范围：无有效时间列")

    print("\n== Top25 标题（含频次） ==")
    for k, v in df["title"].value_counts().head(25).items():
        print("  %6d  %s" % (v, str(k)[:50]))

    print("\n== Top15 区域 ==")
    for k, v in df["area"].value_counts().head(15).items():
        print("  %6d  %s" % (v, str(k)[:30]))

    print("\n== 样例 8 条 ==")
    for r in df.head(8).itertuples():
        print("  [%s] %s | %s" % (
            r.order_id, str(r.title)[:30], str(r.content)[:50]))


if __name__ == "__main__":
    main()
