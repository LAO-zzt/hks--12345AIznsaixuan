# 12345 热线工单 AI 数据清洗与标准化模块

把原始 12345 自然语言工单，转换为可被 AI **跨工单比较、聚类、去重**的结构化业务数据。

本目录是**自包含模块**：拷贝到任意项目即可使用，所有数据默认落在 `data/` 目录。

## 功能
- 文本清洗 + 套话弱化
- 实体抽取：主体 / 地点（区·镇街·小区·道路·门牌）/ 事件 / 诉求 / 时间 / 人物 / 手机号
- 归一化与 `semantic_content` 语义内容生成
- TF-IDF + SVD 向量化（L2 归一化，余弦相似度 = 矩阵乘；可插拔外部向量模型）
- 批量引擎：分批、断点续跑、失败重试、停止控制、增量缓存
- 重复识别：embedding 相似度 + 多特征综合判定

## 快速开始

```bash
cd textclean_module
pip install -r requirements.txt

# 方式一：Python 门面
python -c "import textclean_module as tcm; c=tcm.TextCleaner(); print(c.clean_excel('data/你的工单.xlsx'))"

# 方式二：启动 REST 服务（前端对接）
python server.py
# 浏览器打开 http://127.0.0.1:5000
```

## 对接方式
| 场景 | 入口 |
|------|------|
| 后端 / 定时任务 / Notebook | `import textclean_module as tcm; tcm.TextCleaner()` |
| 前端 | `server.py` 的 REST API（见 `AGENT.md` 第 5 节） |

## 目录与组合说明
完整目录结构、处理流水线、API 列表、扩展点见 **`AGENT.md`**（面向 Agent / 集成方的组合指南）。

## 验证
```bash
python demo.py --smoke 10
python tools/run_50_sample.py      # 输出 data/sample_cleaned.txt
python tools/test_dup.py           # 去重实测
```
