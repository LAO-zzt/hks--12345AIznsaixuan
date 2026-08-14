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
USE_EMBEDDING = False
EMBEDDING_MODEL = "paraphrase-multilingual-MiniLM-L12-v2"

# ---------------- 聚类参数（DBSCAN） ----------------
CLUSTER_EPS = 0.5          # 邻域半径（余弦距离）
CLUSTER_MIN_SAMPLES = 2    # 核心点最少样本数
FALLBACK_RULE_GROUP = True # 聚类异常时是否回退规则分组
CONSOLIDATE_BY_RULES = True # 聚类后按规则归并碎片簇、回收同签名噪声点
# 数据量超过该阈值时不跑 DBSCAN（距离矩阵 O(n²) 内存不可行），
# 自动切换“标题规则分组”大数据路线
CLUSTER_MAX_ROWS = 15000

# ---------------- 展示规模上限（超大数据防卡顿） ----------------
MAX_DISPLAY_EVENTS = 100   # 看板最多展示事件数（按优先级取 Top）
MAX_PROFILE_EVENTS = 500   # 事件画像/风险/建议最多处理的簇数（按频次取 Top）
MAX_DISPLAY_ORDERS = 2000  # 前端明细表最多展示工单数
MAX_EXPORT_ORDERS = 20000  # Excel 明细 Sheet 最多导出行数（CSV 不限）
# 拼接聚类文本时实体字段的加权重复次数（强化结构化信号，
# 事件权重最高：保证“同事件不同主体/表述”能聚到一起）
TEXT_WEIGHT_SUBJECT = 1
TEXT_WEIGHT_EVENT = 3
TEXT_WEIGHT_AREA = 2

# ---------------- 趋势分析窗口 ----------------
TREND_RECENT_DAYS = 3      # “近期”窗口（天）
TREND_BASELINE_DAYS = 7    # 对比基线窗口（天）
TREND_RISING_RATIO = 1.5   # 近期日均/基线日均 >= 该比例判为“上升”
TREND_DECLINING_RATIO = 0.6  # <= 该比例判为“下降”

# ---------------- 风险评分权重（合计应为 1.0） ----------------
RISK_WEIGHT_FREQUENCY = 0.35     # 频次
RISK_WEIGHT_TREND = 0.30         # 增长趋势
RISK_WEIGHT_AREA = 0.15          # 空间集中度
RISK_WEIGHT_SENSITIVITY = 0.20   # 事件敏感度

# ---------------- 飞书推送（可选） ----------------
# 留空则跳过推送；不得在此写死真实密钥，正式使用时通过环境变量注入
FEISHU_WEBHOOK = os.environ.get("FEISHU_WEBHOOK", "")
