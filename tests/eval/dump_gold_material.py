# -*- coding: utf-8 -*-
"""
标注素材导出（dump_gold_material.py）

从全量流水线结果 eval_result.pkl 中，按高频事件类别 + 标题变体抽取候选工单，
输出 order_id / 标题 / 主体 / 区域 / 内容(截断) / cluster_id，
供人工判定"两两是否同一事件"，据此构造金标准标注集 gold_set.csv。

用法：python tests/eval/dump_gold_material.py
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

import config


def main():
    out_path = os.path.join(config.OUTPUT_DIR, "eval_result.pkl")
    with open(out_path, "rb") as f:
        data = pickle.load(f)
    df = data["df"]
    print("全量工单：%d，簇数：%d，多频簇数：%d" % (
        len(df), df["cluster_id"].nunique(),
        df.loc[df["is_multi_freq"], "cluster_id"].nunique()))

    # 关注的高频事件类别（对应标注集覆盖的 5+ 类场景）
    categories = [
        "拖欠工资", "噪音扰民", "生活噪音", "商业噪音", "施工噪音",
        "劳动纠纷", "消费纠纷", "失业保险金", "网购纠纷", "占道经营",
        "油烟扰民", "车辆乱停放", "违建", "环境卫生",
    ]
    ev = df["extracted_event"].astype(str).str.strip()
    out = []
    for cat in categories:
        sub = df[ev == cat]
        if sub.empty:
            continue
        # 每个类别取：不同标题变体各 3 条 + 不同主体 3 条
        titles = sub["title"].value_counts().head(3).index
        seen = set()
        for t in titles:
            tt = sub[sub["title"] == t]
            for r in tt.head(3).itertuples(index=False):
                if r.order_id in seen:
                    continue
                seen.add(r.order_id)
                out.append(r)
        # 不同主体（主体字段有值时）
        subj_grp = sub[sub["extracted_subject"].astype(str).str.strip() != ""]
        for s in subj_grp["extracted_subject"].value_counts().head(3).index:
            for r in subj_grp[subj_grp["extracted_subject"] == s].head(2).itertuples(index=False):
                if r.order_id in seen:
                    continue
                seen.add(r.order_id)
                out.append(r)

    print("\n== 候选工单（共 %d 条，按类别分组） ==" % len(out))
    cur_cat = None
    for r in out:
        cat = str(r.extracted_event)
        if cat != cur_cat:
            cur_cat = cat
            print("\n########## %s ##########" % cat)
        print("  %s | 簇%-6d | 主体=%-10s 区域=%-8s" % (
            r.order_id, r.cluster_id,
            str(r.extracted_subject)[:10], str(r.extracted_area)[:8]))
        print("      标题：%s" % str(r.title)[:50])
        print("      内容：%s" % str(r.content)[:90])

    # ---- 同主体聚焦：高频主体下的工单（找同主体多频正样本） ----
    print("\n\n################ 同主体聚焦（候选正样本） ################")
    subj_s = df["extracted_subject"].astype(str).str.strip()
    mask = (subj_s != "") & (subj_s != "nan")
    top_subjs = subj_s[mask].value_counts().head(12)
    for s, cnt in top_subjs.items():
        sub = df[subj_s == s]
        print("\n@@@@@@ 主体=%-12s（共%d条） @@@@@@" % (s[:12], cnt))
        for r in sub.head(4).itertuples(index=False):
            print("  %s | 簇%-6d | 事件=%-10s" % (
                r.order_id, r.cluster_id, str(r.extracted_event)[:10]))
            print("      标题：%s" % str(r.title)[:46])
            print("      内容：%s" % str(r.content)[:80])


if __name__ == "__main__":
    main()
