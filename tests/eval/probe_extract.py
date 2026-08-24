# -*- coding: utf-8 -*-
"""
textclean 抽取质量探测（probe_extract.py）

对标注集关键工单跑 textclean，打印原始抽取字段，
判断"主体/地点信号够不够细"（决定修复落点在 cluster 分组还是 textclean 抽取）。
"""
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


def get_pipeline():
    import logging
    logging.getLogger("ticket_cleaner").setLevel(logging.ERROR)
    import textclean_module as tcm
    data_dir = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                            "textclean_module", "data")
    cfg = tcm.Config(db_path=os.path.join(data_dir, "cleaner_probe.db"),
                     work_dir=data_dir, source_excel_path="")
    from ticket_cleaner.pipeline import CleaningPipeline
    from ticket_cleaner.storage import Storage
    return CleaningPipeline(cfg, Storage(cfg.db_path))


TARGETS = {
    "250101007310109-01": "瀛源服装拖欠工资",
    "250101007800108-01": "中通快递拖欠工资",
    "250101006200109-01": "中电建水系欠薪",
    "250101001030109-01": "骏华轩小区噪音",
    "250101004130101-01": "新感觉公寓噪音",
    "250101006300102-01": "乐华恒业网购",
    "250101012970109-01": "高黎市场占道",
}


def main():
    files = [f for f in sorted(os.listdir(config.INPUT_DIR))
             if f.lower().endswith((".xlsx", ".xlsm", ".csv"))]
    path = os.path.join(config.INPUT_DIR, files[0])
    df = loader.load_orders_cached(path)
    df = df[df["order_id"].astype(str).isin(TARGETS)].copy()

    pipeline = get_pipeline()
    from ticket_cleaner.schema import TicketRecord
    records = [TicketRecord(ticket_no=str(r.order_id), title=str(r.title or ""),
                            content=str(r.content or ""), region=str(r.area or ""))
               for r in df.itertuples(index=False)]
    cleaned = pipeline.process_batch(records)

    by_id = {r.order_id: r for r in df.itertuples(index=False)}
    for rec, tc in zip(records, cleaned):
        tag = TARGETS.get(rec.ticket_no, "")
        print("\n==== %s (%s) ====" % (rec.ticket_no, tag))
        print("  标题: %s" % str(rec.title)[:40])
        print("  内容: %s" % str(rec.content)[:70])
        print("  organization_normalized = %s" % getattr(tc, "organization_normalized", ""))
        print("  address_normalized      = %s" % getattr(tc, "address_normalized", ""))
        print("  town/community/building = %s | %s | %s" % (
            getattr(tc, "town", ""), getattr(tc, "community", ""), getattr(tc, "building", "")))
        print("  road / addr_level       = %s" % getattr(tc, "road", ""))
        print("  event_type              = %s" % getattr(tc, "event_type", ""))
        print("  event_detail            = %s" % getattr(tc, "event_detail", ""))


if __name__ == "__main__":
    main()
