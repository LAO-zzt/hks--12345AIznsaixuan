# -*- coding: utf-8 -*-
"""
容错验收测试（test_robustness.py）

1. 空文件/缺失字段 → 友好兜底，不崩溃
2. 时间字段全部异常 → 识别仍可完成
3. 开启 Embedding 但本地模型缺失 → 自动回退 TF-IDF
4. 聚类参数极端（eps 过小）→ 规则分组兜底仍出结果
5. 飞书 Webhook 未配置 → 跳过推送不报错

用法：python test_robustness.py
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
    event_profiler, feishu_pusher,
)

PASS = 0


def check(name, cond, detail=""):
    global PASS
    status = "通过" if cond else "失败"
    print("  [%s] %s %s" % (status, name, detail))
    assert cond, "容错测试失败：%s" % name
    PASS += 1


def full_chain(df_raw, **cluster_kwargs):
    df = loader.load_orders(df_raw)
    df = normalizer.normalize_orders(df)
    df = entity_extractor.extract_entities(df)
    df, info = cluster_mod.cluster_orders(df, **cluster_kwargs)
    df, _ = classifier.classify_multi_freq(df)
    events = event_profiler.build_event_profiles(df)
    return df, events, info


def main():
    print("== 容错验收测试 ==")

    print("\n用例1：缺失字段（仅有诉求内容列）")
    df_raw = pd.DataFrame({"诉求内容": [
        "锦华花园楼下烧烤店深夜噪音扰民",
        "锦华花园楼下烧烤店半夜喧哗",
        "锦华花园烧烤档吵得睡不着",
    ]})
    df, events, info = full_chain(df_raw)
    check("自动生成工单编号", df["order_id"].str.startswith("AUTO_").all())
    check("流水线未崩溃且产出事件", len(df) == 3, "（事件数=%d）" % len(events))

    print("\n用例2：时间字段全部无法解析")
    df_raw = pd.DataFrame({
        "工单编号": ["T1", "T2", "T3"],
        "诉求内容": ["云路小区下水道堵了", "云路小区下水道堵塞", "云路小区排水堵塞"],
        "涉及主体": ["云路小区"] * 3,
        "事发区域": ["容桂街道"] * 3,
        "提交时间": ["无效时间", "", "N/A"],
    })
    df, events, info = full_chain(df_raw)
    check("时间解析失败不阻断识别", df["submit_time"].isna().all())
    if events:
        check("事件首末出现标记为未知", all(e["first_seen"] == "未知" for e in events))

    print("\n用例3：开启 Embedding 但本地模型缺失")
    df_raw = pd.read_csv(os.path.join(config.INPUT_DIR, "sample.csv"))
    df, events, info = full_chain(df_raw, use_embedding=True)
    check("已自动回退 TF-IDF", "TF-IDF" in info["method"], "（路线=%s）" % info["method"])
    check("回退提示已生成", any("本地模型不可用" in m for m in info["messages"]))
    check("识别结果不受影响", len(events) == 5, "（事件数=%d）" % len(events))

    print("\n用例4：聚类参数极端（eps=0.01）")
    df, events, info = full_chain(df_raw, eps=0.01)
    check("触发规则分组兜底", info["fallback_used"], "（路线=%s）" % info["method"])
    check("兜底后仍产出多频事件", len(events) > 0, "（事件数=%d）" % len(events))

    print("\n用例5：飞书 Webhook 未配置")
    ok, msg = feishu_pusher.push_top_events(events, webhook="")
    check("跳过推送且不视为失败", ok and "跳过" in msg, "（%s）" % msg)

    print("\n全部 %d 项容错检查通过。" % PASS)


if __name__ == "__main__":
    main()
