# -*- coding: utf-8 -*-
"""
多频识别评测脚本（eval_multi_freq.py）

读取全量流水线结果 eval_result.pkl（order_id -> cluster_id），
对金标准标注集 gold_set.csv 逐对打分：
    "两工单是否同一多频事件" = 是否落在同一 cluster_id。

输出：精确率 / 召回率 / F1（overall + 分类别），并列出判错的成对样本。

用法：python tests/eval/eval_multi_freq.py
"""
import os
import sys
import pickle

if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

import pandas as pd

import config

EVAL_DIR = os.path.dirname(os.path.abspath(__file__))


def load_cluster_map():
    path = os.path.join(config.OUTPUT_DIR, "eval_result.pkl")
    with open(path, "rb") as f:
        data = pickle.load(f)
    df = data["df"]
    return dict(zip(df["order_id"].astype(str), df["cluster_id"])), df


def main():
    cmap, df = load_cluster_map()
    gold = pd.read_csv(os.path.join(EVAL_DIR, "gold_set.csv"), dtype={"id_a": str, "id_b": str})
    gold["label"] = gold["label"].astype(int)

    missing = [r for r in gold.itertuples() if r.id_a not in cmap or r.id_b not in cmap]
    if missing:
        print("警告：标注集中 %d 对工单不存在于分析结果，已跳过：" % len(missing))
        for r in missing:
            print("   %s / %s" % (r.id_a, r.id_b))

    rows = []
    for r in gold.itertuples():
        if r.id_a not in cmap or r.id_b not in cmap:
            continue
        pred = int(cmap[r.id_a] == cmap[r.id_b])
        rows.append({
            "id_a": r.id_a, "id_b": r.id_b,
            "gold": r.label, "pred": pred, "category": r.category,
            "tp": pred == 1 and r.label == 1, "fp": pred == 1 and r.label == 0,
            "fn": pred == 0 and r.label == 1, "tn": pred == 0 and r.label == 0,
        })
    res = pd.DataFrame(rows)
    tp, fp, fn, tn = res["tp"].sum(), res["fp"].sum(), res["fn"].sum(), res["tn"].sum()
    prec = tp / (tp + fp) if tp + fp else 0
    rec = tp / (tp + fn) if tp + fn else 0
    f1 = 2 * prec * rec / (prec + rec) if prec + rec else 0

    print("=" * 66)
    print("多频识别评测（成对判重：同一簇=同一事件）")
    print("=" * 66)
    print("标注集：%d 对（正样本 %d / 负样本 %d），覆盖类别 %d" % (
        len(res), int(res["gold"].sum()), int((res["gold"] == 0).sum()),
        res["category"].nunique()))
    print("系统判定：正 %d / 负 %d" % (int(res["pred"].sum()), int((res["pred"] == 0).sum())))
    print("-" * 66)
    print("混淆矩阵：TP=%d  FP=%d  FN=%d  TN=%d" % (tp, fp, fn, tn))
    print("精确率 Precision = %.3f" % prec)
    print("召回率 Recall    = %.3f" % rec)
    print("F1              = %.3f" % f1)

    print("\n== 分类别 F1 ==")
    for cat, g in res.groupby("category"):
        t, f_p, f_n = g["tp"].sum(), g["fp"].sum(), g["fn"].sum()
        p = t / (t + f_p) if t + f_p else 0
        r = t / (t + f_n) if t + f_n else 0
        f = 2 * p * r / (p + r) if p + r else 0
        print("  %-8s 正%-2d 负%-2d  P=%.3f R=%.3f F1=%.3f" % (
            cat, int(g["gold"].sum()), int((g["gold"] == 0).sum()), p, r, f))

    wrong = res[res["gold"] != res["pred"]]
    if not wrong.empty:
        print("\n== 判错的 %d 对（用于定位规则漏/聚类碎） ==" % len(wrong))
        for r in wrong.itertuples():
            err = "误并(应分)" if (r.gold == 0 and r.pred == 1) else "漏并(应合)"
            print("  [%s] %s vs %s  %s" % (err, r.id_a, r.id_b, r.category))
    else:
        print("\n全部判定正确！")

    # 输出便于追查的字段对照
    print("\n== 判错对字段对照 ==")
    if not wrong.empty:
        show_cols = ["order_id", "extracted_subject", "extracted_event", "extracted_area", "cluster_id"]
        for r in wrong.itertuples():
            for oid in (r.id_a, r.id_b):
                row = df[df["order_id"].astype(str) == oid]
                if row.empty:
                    continue
                rr = row.iloc[0]
                print("  %s | 簇%d | 主体=%s | 事件=%s | 区域=%s" % (
                    oid, rr["cluster_id"],
                    str(rr.get("extracted_subject", ""))[:18],
                    str(rr.get("extracted_event", ""))[:14],
                    str(rr.get("extracted_area", ""))[:10]))
            print("  ---")


if __name__ == "__main__":
    main()
