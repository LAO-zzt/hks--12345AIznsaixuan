# -*- coding: utf-8 -*-
"""
全局配置文件（config.py）

所有可调参数集中在此管理，不散落在业务代码中。
默认路径保证完全离线可运行；Embedding 仅作为可选增强。
"""
import os

# 项目根目录（保证相对路径在任意启动方式下可用）
BASE_DIR = os.path.dirname(os.path.abspath(__file__))

# ---------------- 多频识别阈值 ----------------
# 一个聚类内工单数 >= 该阈值，才判定为“多频事件”
FREQ_THRESHOLD = 3

# ---------------- 路径配置 ----------------
INPUT_DIR = os.path.join(BASE_DIR, "data", "input")
OUTPUT_DIR = os.path.join(BASE_DIR, "data", "output")
DICT_DIR = os.path.join(BASE_DIR, "data", "dicts")
MODEL_DIR = os.path.join(BASE_DIR, "models")

# ---------------- Embedding（可选增强，默认关闭） ----------------
# 仅允许从本地 MODEL_DIR 加载，绝不联网下载；模型缺失自动回退 TF-IDF
USE_EMBEDDING = True
EMBEDDING_MODEL = "paraphrase-multilingual-MiniLM-L12-v2"

# ---------------- 聚类参数（判重分组主路线，任意规模一致） ----------------
CLUSTER_EPS = 0.5          # 邻域半径（余弦距离，仅 DBSCAN 兜底路线使用）
CLUSTER_MIN_SAMPLES = 2    # 核心点最少样本数（仅 DBSCAN 兜底路线使用）
FALLBACK_RULE_GROUP = True # 聚类异常时是否回退规则分组
CONSOLIDATE_BY_RULES = True # 聚类后按规则归并碎片簇、回收同签名噪声点
# 判重统一走「归一事件+主体键」分组路线（O(n) 线性，12.8 万条约 4s），
# 不再按规模切换 DBSCAN；该阈值仅作注释保留（已废弃规模分流）
CLUSTER_MAX_ROWS = 15000

# ---------------- 展示规模上限（超大数据防卡顿） ----------------
MAX_DISPLAY_EVENTS = 100   # 看板最多展示事件数（按优先级取 Top）
MAX_PROFILE_EVENTS = 500   # 事件画像最多处理的簇数（按频次取 Top）
MAX_DISPLAY_ORDERS = 2000  # 前端明细表最多展示工单数
MAX_EXPORT_ORDERS = 20000  # Excel 明细 Sheet 最多导出行数（CSV 不限）
TEXT_WEIGHT_SUBJECT = 1
TEXT_WEIGHT_EVENT = 3
TEXT_WEIGHT_AREA = 2

# ---------------- 飞书推送（可选） ----------------
FEISHU_WEBHOOK = os.environ.get("FEISHU_WEBHOOK", "")

# ---------------- LLM 判重（可选增强） ----------------
# 开启后对聚类结果做二次判重：同主体+同区域但被分到不同簇的候选对，
# 调用 LLM 判断是否同一事件，是则合并，提升召回率。
DEEPSEEK_API_KEY = os.environ.get("DEEPSEEK_API_KEY", "")
DEEPSEEK_BASE_URL = os.environ.get("DEEPSEEK_BASE_URL", "https://api.deepseek.com")
DEEPSEEK_MODEL = os.environ.get("DEEPSEEK_MODEL", "deepseek-chat")
LLM_DEDUP_MAX_PAIRS = 30
LLM_DEDUP_SAMPLE = 2
