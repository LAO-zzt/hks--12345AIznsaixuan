# AGENT.md — 12345 工单清洗模块 · 组合使用指南

> 本文件面向 **Agent / 自动化脚本 / 前端集成方**。它说明模块能做什么、内部各步骤如何**组合**，
> 以及如何用最少代码把它接入你的系统（前端 / 后端 / 定时任务）。

---

## 1. 这个模块解决什么问题

把"12345 热线"原始的自然语言工单（一段中文描述）转换成**可被 AI 跨工单比较、聚类、去重**的
结构化业务数据。核心产出字段：

| 字段 | 含义 | 用途 |
|------|------|------|
| `organization_normalized` | 主体（小区/单位/机构） | 按主体聚合、找同类诉求 |
| `address_normalized` / `town` / `community` / `road` / `building` | 归一化地点 + 层级 | 按镇街/小区聚合 |
| `event_type` / `event_detail` / `event_action` | 事件类型与动作 | 事件分类、聚类 |
| `request` / `issue` / `request_nature` | 诉求 / 问题描述 / 诉求性质 | 诉求去重、意图识别 |
| `semantic_content` | 套话弱化后的"语义文本" | Embedding 输入、相似度 |
| `embedding` | TF-IDF+SVD 向量（L2 归一化） | 余弦相似度 = 矩阵乘 |
| `data_quality_score` / `is_usable_for_duplicate` | 质量分 / 是否可用于去重 | 过滤低质工单 |
| `content_hash` | 内容指纹 | 增量缓存、完全重复 |

---

## 2. 目录结构（已自包含，可直接拷贝）

```
textclean_module/
├── __init__.py          # ★ 统一门面 TextCleaner（推荐入口）
├── server.py            # ★ Flask REST 服务（前端对接入口）
├── config.yaml          # 配置（llm / 高德 / 数据库 / web）
├── requirements.txt
├── ticket_cleaner/      # 核心清洗包（无需改动）
│   ├── schema.py        # TicketRecord / CleanedTicket 数据结构
│   ├── config.py        # Config 数据类
│   ├── pipeline.py      # CleaningPipeline：单条工单 raw→clean→抽取→归一→semantic→quality
│   ├── extractors.py    # 主体/地点/事件/诉求/时间/人物 抽取（规则）
│   ├── normalizer.py    # 实体归一、build_semantic_content、质量评分
│   ├── cleaners.py      # 文本清洗、套话弱化、手机号/姓名清洗
│   ├── embedding.py     # TF-IDF+SVD Embedder（可插拔外部向量模型）
│   ├── batch_engine.py  # BatchEngine：分批、断点续跑、重试、停止、增量
│   ├── storage.py       # SQLite 存储：Job/Batch/ticket_cleaned/缓存
│   ├── duplicate.py     # DuplicateDetector：重复候选识别
│   ├── dedup.py         # 诉求级去重 / 同人同诉求合并
│   ├── reader.py        # Excel 读取（支持切片 Batch）
│   ├── gaode_cache.py   # 高德 POI 验证（可关闭）
│   └── llm_helper.py    # LLM 调用（规则模式默认不用）
├── tools/               # 开发/验证脚本
│   ├── make_dataset.py  # 从全量 xlsx 抽 N 条做测试集
│   ├── run_50_sample.py # 抽 50 条 → 清洗 → 写 sample_cleaned.txt
│   └── test_dup.py      # 去重召回/误杀实测
├── demo.py              # 命令行冒烟测试
└── data/                # 默认工作目录（db/缓存/上传/日志/样例）
```

---

## 3. 两条集成路径（二选一）

### 路径 A：Python 直接调用（后端 / 定时任务 / Notebook）

```python
import textclean_module as tcm

cleaner = tcm.TextCleaner()                 # 数据落在 textclean_module/data/cleaner.db
stats = cleaner.clean_excel("data/工单.xlsx")   # 清洗 + 入库，返回统计
print(stats)

# 重复识别
cands = cleaner.find_duplicates("job-工单", top_k=50)
dups = [c for c in cands if c["duplicate"]]

# 检索 / 聚合
rows, total = cleaner.search("job-工单", event_type="物业管理", usable_only=True)
print(cleaner.stats("job-工单"))
```

`TextCleaner` 构造参数：`data_dir`、`db_path`、`batch_size`(200/500/1000/2000)、
`embedding_dim`、`min_quality_score`、`enable_cache`。

### 路径 B：REST 服务（前端对接）

```bash
cd textclean_module
pip install -r requirements.txt
python server.py            # 或 python -m textclean_module.server
# 打开 http://127.0.0.1:5000 查看内置管理界面
```

前端只调 REST（JSON），无需理解内部实现。关键接口见第 5 节。

---

## 4. 处理流程（理解"怎么组合"最重要）

一条工单从原始文本到结构化结果，按顺序经过：

```
Excel 行
  │  reader.ExcelReader  →  TicketRecord (统一 Schema)
  ▼
CleaningPipeline.process(record)
  ├─ clean_text           清洗：去噪、去套话、标准化
  ├─ extract_phone/person 手机号、人物
  ├─ extract_address      地点（区/镇街/小区/道路/门牌）
  ├─ extract_organization 主体（优先从小区/机构后缀抽取）
  ├─ extract_time         时间（起止 + 周期模式）
  ├─ extract_event        事件类型/动作/对象（规则优先级）
  ├─ classify_*           工单类型(线上/线下)、诉求性质(投诉/建议/举报/咨询/求助)
  ├─ extract_request      诉求 + 问题简述
  ├─ build_semantic_content  组装"语义内容"
  └─ compute_quality_score   质量分 + is_usable_for_duplicate
  ▼
CleanedTicket  (raw / clean / semantic / 各实体字段)
  ▼
BatchEngine：分批 → process_batch → Embedder.embed(semantic) → Storage.upsert_cleaned
  ▼
（可选）DuplicateDetector.find_candidates  → 重复候选 / 判重
```

**组合原则：**
- 只想"清洗入库"：`clean_excel()` 已经把上面整条链路跑完。
- 想"先试几条看效果"：`pipeline.CleaningPipeline.process_batch(records)` 单步调用，不入批量引擎。
- 想"换向量模型"：实现 `embedding.EmbedderBase`（fit/embed/dim），传给 `BatchEngine(embedder=...)`。
- 想"接外部大模型做抽取"：在 `extractors.py` 的对应函数里调用 `llm_helper`，规则作为兜底。
- 想"自动去重"：在批量跑完后调用 `DuplicateDetector`，可把判定为重复的工单 `is_duplicate` 标记（当前为独立模块，需显式调用，见第 6 节）。

---

## 5. REST API（前端对接）

所有请求/响应均为 JSON。`job_id` 由前端在创建任务时指定或留空自动生成。

| 方法 | 路径 | 说明 |
|------|------|------|
| POST | `/api/upload` | 上传 Excel（multipart `file`） |
| POST | `/api/create_job` | `{job_id, batch_size}` 创建任务 |
| POST | `/api/run_job` | `{job_id}` 后台运行清洗 |
| POST | `/api/stop_job` | `{job_id}` 停止运行 |
| GET  | `/api/jobs` | 任务列表 |
| GET  | `/api/batches?job_id=&status=` | 批次列表 |
| POST | `/api/retry_batch` | `{job_id, batch_no}` 重试失败批次 |
| GET  | `/api/results?job_id=&page=&size=` | 清洗结果分页 |
| GET  | `/api/search_cleaned?job_id=&event_type=&town=&community=&keyword=&usable_only=` | 条件检索 |
| GET  | `/api/group_by_organization?job_id=` | 按主体聚合 |
| GET  | `/api/group_by_town_tree?job_id=` | 镇街→小区 树形聚合 |
| GET  | `/api/event_types?job_id=` | 事件类型分布 |
| GET  | `/api/ticket_detail?job_id=&ticket_no=` | 单工单详情 |
| POST | `/api/duplicates` | `{job_id, top_k}` 重复识别 |

**最小前端调用示例（fetch）：**
```js
// 1) 上传
const fd = new FormData(); fd.append("file", fileInput.files[0]);
await fetch("/api/upload", {method:"POST", body: fd});

// 2) 创建 + 运行
await fetch("/api/create_job", {method:"POST", headers:{"Content-Type":"application/json"},
  body: JSON.stringify({job_id:"job-1", batch_size:1000})});
await fetch("/api/run_job", {method:"POST", headers:{"Content-Type":"application/json"},
  body: JSON.stringify({job_id:"job-1"})});

// 3) 轮询结果
const r = await fetch("/api/results?job_id=job-1&page=0&size=20").then(x=>x.json());
```

---

## 6. 去重：当前是"独立模块"，需显式调用

`BatchEngine` 只负责"读→洗→向量→入库"，**不会**在清洗过程中自动去重。
重复识别是独立的 `DuplicateDetector`，需要单独调用（路径 A 的 `find_duplicates`，
或 REST 的 `/api/duplicates`）。

若希望"清洗完自动标记重复"，可在批量跑完后追加一步（示例，可放进你的编排逻辑）：
```python
cleaner.clean_excel("data/工单.xlsx", job_id="job-1")
cands = cleaner.find_duplicates("job-1", top_k=50)
# 把判为重工的工单在 storage 中标记 is_duplicate=1（当前需自行扩展字段/表）
```

判定逻辑：embedding 余弦相似度 ≥ 0.85 **且** 综合特征分（主体 0.3+地点 0.3+事件 0.2+
诉求 0.1+时间 0.1，语义≥0.9 再加 0.1）≥ 0.7 → 判为重复。阈值在 `DuplicateDetector(...,
similarity_threshold, duplicate_threshold)` 调整。

---

## 7. 常见扩展点

- **新增事件类型**：在 `extractors.py` 的 `_EVENT_TYPES`（按优先级）与动作映射里补充。
- **主体/地点词典**：`Config.entity_dict_path`、`boilerplate_path`、`synonym_path` 可挂外部词典。
- **高德验证开关**：`config.yaml` 的 `gaode.enabled`（网络不可用设 false，不影响规则抽取）。
- **批量大小**：200/500/1000/2000 四档；越大吞吐越高、内存占用越大。
- **数据库**：默认 SQLite（`data/cleaner.db`）。要上规模可把 `Storage` 换成 MySQL/PostgreSQL 实现同接口。

---

## 8. 快速验证

```bash
cd textclean_module
python demo.py --smoke 10            # 洗 10 条，打印结构化结果
python tools/run_50_sample.py        # 抽 50 条，输出 data/sample_cleaned.txt
python tools/test_dup.py             # 去重召回/误杀实测
```
