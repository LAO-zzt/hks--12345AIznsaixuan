"""12345工单清洗模块 Demo。

使用方式：
    .venv\\Scripts\\python.exe demo.py            # 默认冒烟测试（10条）
    .venv\\Scripts\\python.exe demo.py --batch 1  # 运行1个完整Batch
    .venv\\Scripts\\python.exe demo.py --full     # 全量运行
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time

# 保证可以直接运行
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from pathlib import Path

# 模块内数据目录（清洗结果 / 库文件默认落在 data/）
_HERE = Path(__file__).resolve().parent
_DATA = _HERE / "data"
_DATA.mkdir(exist_ok=True)


def _default_excel() -> str:
    xs = sorted(_DATA.glob("*.xlsx"))
    return str(xs[0]) if xs else ""


_EXCEL_PATH = _default_excel()


def make_config(**kw):
    """构造指向模块 data/ 的 Config（可被 --excel 覆盖数据源）。"""
    base = dict(db_path=str(_DATA / "cleaner.db"), work_dir=str(_DATA))
    if _EXCEL_PATH:
        base["source_excel_path"] = _EXCEL_PATH
    base.update(kw)
    return Config(**base)


from ticket_cleaner import __pipeline_version__
from ticket_cleaner.batch_engine import BatchEngine, ProgressInfo
from ticket_cleaner.config import Config
from ticket_cleaner.duplicate import DuplicateDetector
from ticket_cleaner.pipeline import CleaningPipeline
from ticket_cleaner.reader import ExcelReader
from ticket_cleaner.storage import Storage


def smoke_test(n: int = 10) -> None:
    """快速冒烟测试：读取N条 → 清洗 → 实体抽取 → Semantic Content → Embedding。"""
    print(f"=== 冒烟测试（{n}条） ===")
    cfg = make_config()
    storage = Storage(cfg.db_path)
    reader = ExcelReader(cfg.source_excel_path)
    pipeline = CleaningPipeline(cfg, storage)

    records = reader.read_range(0, n)
    print(f"读取 {len(records)} 条原始工单")
    print()

    cleaned = pipeline.process_batch(records)

    # Embedding
    from ticket_cleaner.embedding import TfidfEmbedder, serialize_embedding
    embedder = TfidfEmbedder(target_dim=cfg.embedding_dim)
    texts = [t.semantic_content or t.clean_content for t in cleaned]
    embedder.fit(texts)
    vecs = embedder.embed(texts)
    for t, v in zip(cleaned, vecs):
        t.embedding = serialize_embedding(v)

    for i, t in enumerate(cleaned):
        print(f"--- 工单 {i + 1} ---")
        print(f"  ticket_no: {t.ticket_no}")
        print(f"  raw_content: {t.raw_content[:80]}...")
        print(f"  clean_content: {t.clean_content[:80]}...")
        print(f"  semantic_content: {t.semantic_content}")
        print(f"  organization: {t.organization_raw} -> {t.organization_normalized} (conf={t.organization_confidence})")
        print(f"  address: {t.address_raw} -> {t.address_normalized}")
        print(f"    district={t.district} town={t.town} community={t.community} building={t.building}")
        print(f"  event_type: {t.event_type} | event_detail: {t.event_detail}")
        print(f"  issue: {t.issue} | request: {t.request}")
        print(f"  time_start: {t.time_start} | time_pattern: {t.time_pattern}")
        print(f"  phone: {t.phone_raw} -> {t.phone_masked} (conf={t.phone_match_confidence})")
        print(f"  person: {t.person_raw} (conf={t.person_confidence})")
        print(f"  quality: {t.data_quality_score} usable={t.is_usable_for_duplicate} status={t.parse_status}")
        print(f"  content_hash: {t.content_hash}")
        print(f"  embedding_dim: {v.shape[0]}")
        print()

    # 统计
    usable = sum(1 for t in cleaned if t.is_usable_for_duplicate)
    org_recognized = sum(1 for t in cleaned if t.organization_normalized)
    addr_recognized = sum(1 for t in cleaned if t.address_normalized)
    event_recognized = sum(1 for t in cleaned if t.event_type)
    print("=== 冒烟统计 ===")
    print(f"  总数: {len(cleaned)}")
    print(f"  可用于重复判断: {usable}")
    print(f"  主体识别: {org_recognized}/{len(cleaned)}")
    print(f"  地点识别: {addr_recognized}/{len(cleaned)}")
    print(f"  事件识别: {event_recognized}/{len(cleaned)}")


def run_batch(job_id: str, batch_no: int, batch_size: int = 1000) -> None:
    """运行单个Batch。"""
    print(f"=== 运行 Batch {batch_no} (job={job_id}, batch_size={batch_size}) ===")
    cfg = make_config(batch_size=batch_size)
    engine = BatchEngine(cfg)

    # 如果Job不存在，先创建
    if engine.storage.get_job(job_id) is None:
        info = engine.create_job(job_id)
        print(f"创建Job: {info}")
    else:
        print(f"Job已存在，断点续跑")

    def on_progress(p: ProgressInfo) -> None:
        print(f"  [进度] batch={p.batch_no} stage={p.stage} "
              f"{p.processed}/{p.total} ({p.as_dict()['percent']}%) {p.message}")

    ok = engine.run_batch(job_id, batch_no, on_progress=on_progress)
    print(f"Batch {batch_no} {'成功' if ok else '失败'}")

    stats = engine.storage.job_stats(job_id)
    print(f"Job统计: {json.dumps(stats, ensure_ascii=False, indent=2)}")


def run_full(job_id: str, batch_size: int = 1000) -> None:
    """全量运行。"""
    print(f"=== 全量运行 (job={job_id}, batch_size={batch_size}) ===")
    cfg = make_config(batch_size=batch_size)
    engine = BatchEngine(cfg)

    if engine.storage.get_job(job_id) is None:
        info = engine.create_job(job_id)
        print(f"创建Job: {info}")
    else:
        print(f"Job已存在，断点续跑")

    start = time.time()
    last_stage = [None]

    def on_progress(p: ProgressInfo) -> None:
        if p.stage != last_stage[0] or p.batch_no % 5 == 0:
            print(f"  [进度] batch={p.batch_no}/{p.total_batches} "
                  f"stage={p.stage} {p.message}")
            last_stage[0] = p.stage

    stats = engine.run_job(job_id, on_progress=on_progress)
    elapsed = time.time() - start
    print(f"\n完成，耗时 {elapsed:.1f}s")
    print(f"最终统计: {json.dumps(stats, ensure_ascii=False, indent=2)}")


def find_duplicates(job_id: str, top_k: int = 50) -> None:
    """查找重复工单。"""
    print(f"=== 查找重复 (job={job_id}) ===")
    cfg = make_config()
    storage = Storage(cfg.db_path)
    detector = DuplicateDetector(storage)
    candidates = detector.find_candidates(job_id, top_k=top_k, max_pairs=50)
    print(f"找到 {len(candidates)} 个候选对")
    for i, c in enumerate(candidates[:20]):
        print(f"\n--- 候选 {i + 1} ---")
        print(f"  A: {c.ticket_no_a}")
        print(f"  B: {c.ticket_no_b}")
        print(f"  相似度: {c.similarity:.4f}")
        print(f"  判定: {'重复' if c.duplicate else '相似但非重复'}")
        print(f"  特征得分: {c.details.get('feature_score', 0)}")
        print(f"  原因: {c.reason}")
        # 显示工单内容
        a = storage.get_cleaned_by_ticket(c.ticket_no_a, job_id)
        b = storage.get_cleaned_by_ticket(c.ticket_no_b, job_id)
        if a:
            print(f"  A semantic: {a['semantic_content']}")
        if b:
            print(f"  B semantic: {b['semantic_content']}")


def main() -> None:
    parser = argparse.ArgumentParser(description="12345工单清洗模块 Demo")
    parser.add_argument("--smoke", type=int, nargs="?", const=10, default=None,
                        help="冒烟测试，可指定条数，默认10")
    parser.add_argument("--batch", type=int, default=None,
                        help="运行指定Batch号")
    parser.add_argument("--full", action="store_true", help="全量运行")
    parser.add_argument("--dup", action="store_true", help="查找重复工单")
    parser.add_argument("--job", default="demo-job", help="Job ID")
    parser.add_argument("--batch-size", type=int, default=1000,
                        help="Batch大小")
    parser.add_argument("--top-k", type=int, default=50, help="重复识别topK")
    parser.add_argument("--excel", default=None,
                        help="Excel 路径（默认使用 data/ 下第一个 xlsx）")
    args = parser.parse_args()

    global _EXCEL_PATH
    if args.excel:
        _EXCEL_PATH = args.excel

    if args.smoke is not None:
        smoke_test(args.smoke)
    elif args.batch is not None:
        run_batch(args.job, args.batch, args.batch_size)
    elif args.full:
        run_full(args.job, args.batch_size)
    elif args.dup:
        find_duplicates(args.job, args.top_k)
    else:
        # 默认冒烟10条
        smoke_test(10)


if __name__ == "__main__":
    main()
