"""随机抽取 50 条工单 → 清洗 → 整理成 txt（原始元数据 + 清洗后结构化）。

用法:
    .venv/Scripts/python run_50_sample.py [n] [seed]
输出: sample_cleaned.txt
"""
import sys, os, random, time

_MOD = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))  # textclean_module
sys.path.insert(0, _MOD)

from ticket_cleaner.config import Config
from ticket_cleaner.pipeline import CleaningPipeline
from ticket_cleaner.reader import ExcelReader

# 临时关闭高德网络验证（避免演示时卡网络/失败；不影响规则抽取结果）
import ticket_cleaner.gaode_cache as gc
gc.verify_entity_in_gaode = lambda name: False

N = int(sys.argv[1]) if len(sys.argv) > 1 else 50
SEED = int(sys.argv[2]) if len(sys.argv) > 2 else 2025

# 数据路径解析：优先模块 data/，回退到项目根 database/
_DATA = os.path.join(_MOD, "data")
_ROOT_DB = os.path.join(os.path.dirname(_MOD), "database", "testdata")
DS = os.path.join(_DATA, "顺德区12345热线工单_测试10000.xlsx")
if not os.path.exists(DS):
    _alt = os.path.join(_ROOT_DB, "顺德区12345热线工单_测试10000.xlsx")
    if os.path.exists(_alt):
        DS = _alt
OUT = os.path.join(_DATA, "sample_cleaned.txt")

cfg = Config(db_path=os.path.join(_DATA, "run50.db"), work_dir=_DATA)
storage = None  # 不需要写库
reader = ExcelReader(DS)
pipeline = CleaningPipeline(cfg, storage)

total = reader.count()
# 全量读入再随机抽（10000 条内存足够）
all_records = reader.read_range(0, total)
rng = random.Random(SEED)
sampled = rng.sample(all_records, min(N, len(all_records)))

cleaned = pipeline.process_batch(sampled)

# 格式化输出
lines = []
lines.append("12345 工单 AI 清洗 · 抽样展示（50 条）")
lines.append(f"数据集: {os.path.basename(DS)}   抽样数: {len(cleaned)}   seed: {SEED}")
lines.append("=" * 80)

for i, (rec, t) in enumerate(zip(sampled, cleaned), 1):
    lines.append(f"\n========== 样本 {i} ==========")
    # ---- 原始元数据 ----
    lines.append("【原始元数据】")
    lines.append(f"  工单编号 : {t.ticket_no}")
    lines.append(f"  标题     : {rec.title}")
    content = rec.content or ""
    if len(content) > 300:
        content = content[:300] + " …(已截断)"
    lines.append(f"  内容     : {content}")

    # ---- 清洗后结构化 ----
    lines.append("【清洗后结构化】")
    lines.append(f"  主体     : {t.organization_normalized or '（未识别）'}"
                 f"  (原始: {t.organization_raw or '-'}, 置信: {t.organization_confidence:.2f})")
    addr = " / ".join(x for x in [t.district, t.town, t.community, t.road, t.building] if x)
    lines.append(f"  地点     : {addr or '（未识别）'}")
    ev = t.event_type or "（未识别）"
    if t.event_detail and t.event_type and t.event_type not in t.event_detail:
        ev = f"{t.event_type}({t.event_detail})"
    lines.append(f"  事件     : {ev}")
    lines.append(f"  工单类型 : {t.ticket_type}")
    lines.append(f"  诉求性质 : {t.request_nature}")
    lines.append(f"  诉求     : {t.request or '（未识别）'}   问题: {t.issue or '-'}")
    tstr = t.time_start[:16] if t.time_start else (t.time_pattern or "（未识别）")
    lines.append(f"  时间     : {tstr}")
    lines.append(f"  质量评分 : {t.data_quality_score}   可用去重: {t.is_usable_for_duplicate}   状态: {t.parse_status}")
    lines.append(f"  语义内容 : {t.semantic_content}")

lines.append("\n" + "=" * 80)
lines.append("END")

with open(OUT, "w", encoding="utf-8") as f:
    f.write("\n".join(lines))

print(f"已生成 {OUT}（{len(cleaned)} 条）")
