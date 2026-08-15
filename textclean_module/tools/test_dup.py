# -*- coding: utf-8 -*-
"""实测去重引擎：召回 + 误杀抽查。结果写入 dup_result.txt (UTF-8)。"""
import sys, os, io
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")
_MOD = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))  # textclean_module
sys.path.insert(0, _MOD)

from ticket_cleaner.config import Config
from ticket_cleaner.pipeline import CleaningPipeline
from ticket_cleaner.reader import ExcelReader
from ticket_cleaner.storage import Storage
from ticket_cleaner.embedding import TfidfEmbedder, serialize_embedding
from ticket_cleaner.duplicate import DuplicateDetector
import ticket_cleaner.gaode_cache as gc
gc.verify_entity_in_gaode = lambda name: False

_DATA = os.path.join(_MOD, "data")
_ROOT_DB = os.path.join(os.path.dirname(_MOD), "database", "testdata")
DS = os.path.join(_DATA, "顺德区12345热线工单_测试10000.xlsx")
if not os.path.exists(DS):
    _alt = os.path.join(_ROOT_DB, "顺德区12345热线工单_测试10000.xlsx")
    if os.path.exists(_alt):
        DS = _alt
DB = os.path.join(_DATA, "test_dup.db")
N = 3000

if os.path.exists(DB):
    os.remove(DB)
cfg = Config(db_path=DB, batch_size=2000, work_dir=_DATA)
storage = Storage(cfg.db_path)
reader = ExcelReader(DS)
pipeline = CleaningPipeline(cfg, storage)

records = reader.read_range(0, N)
cleaned = pipeline.process_batch(records)
texts = [t.semantic_content or t.clean_content for t in cleaned]
embedder = TfidfEmbedder(target_dim=cfg.embedding_dim)
embedder.fit(texts)
vecs = embedder.embed(texts)
for t, v in zip(cleaned, vecs):
    t.embedding = serialize_embedding(v)

job_id = "dup-test"
storage.create_job(job_id, "test", len(cleaned), cfg.batch_size, 1)
storage.upsert_cleaned(job_id, 1, cleaned)
storage.update_job_status(job_id, "DONE", "2025-01-01 00:00:00")

det = DuplicateDetector(storage, similarity_threshold=0.85, duplicate_threshold=0.7)
cands = det.find_candidates(job_id, top_k=20, max_pairs=3000)
dups = [c for c in cands if c.duplicate]

lines = []
lines.append(f"总工单数(本次测试): {len(cleaned)}")
lines.append(f"去重候选对: {len(cands)}，判为重复: {len(dups)}")
lines.append("")

# 误杀抽查：从判重对里抽样 12 对，对比两边语义内容
lines.append("=== 判为重复杂对抽查（看是否真重复 vs 误杀）===")
import random
random.seed(1)
sample = random.sample(dups, min(12, len(dups)))
for i, c in enumerate(sample, 1):
    a = storage.get_cleaned_by_ticket(c.ticket_no_a, job_id)
    b = storage.get_cleaned_by_ticket(c.ticket_no_b, job_id)
    sa = a["semantic_content"] if a else "?"
    sb = b["semantic_content"] if b else "?"
    lines.append(f"\n[{i}] sim={c.similarity:.3f} reason={c.reason}")
    lines.append(f"  A({c.ticket_no_a}): {sa}")
    lines.append(f"  B({c.ticket_no_b}): {sb}")

with open(os.path.join(_DATA, "dup_result.txt"), "w", encoding="utf-8") as f:
    f.write("\n".join(lines))
print("DONE", len(cands), len(dups))
