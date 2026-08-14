# 12345 高频事件智能预警与处置辅助系统

> 通过 AI 对海量 12345 工单进行自动聚类和事件画像，及时发现正在形成的高频问题，识别趋势与重点区域，并为管理人员提供风险优先级和处置建议。

本项目不是单纯回答"哪些工单重复"，而是回答：

> 最近正在形成哪些高频问题？它们发生在哪里、涉及谁、增长是否明显、哪些应该优先处理？

## 快速开始

```bash
pip install -r requirements.txt
streamlit run main.py
```

现场演示只需三步：打开页面 → 点击"开始分析"（自动加载内置样例，也可上传 CSV）→ 查看看板并下载结果。默认路径完全离线，无需联网。

## 核心链路

```text
CSV 上传 → loader 加载/清洗/去重 → normalizer 标准化/同义词归一
→ entity_extractor 主体/事件/区域提取 → cluster 聚类（TF-IDF+余弦距离+DBSCAN）
→ classifier 多频识别 → event_profiler 事件画像/趋势
→ risk_analyzer 风险等级/优先级 → action_advisor 处置建议
→ exporter 看板/CSV/Excel → feishu_pusher 可选推送
```

技术保底路线为 scikit-learn TF-IDF + jieba 分词 + 余弦距离 + DBSCAN，完全离线；Embedding 仅作为可选增强（`USE_EMBEDDING=True` 且本地 `models/` 目录存在模型时启用），模型缺失自动回退。

## 可靠性设计（不翻车）

- 聚类质量差（无有效簇/覆盖率低/碎片化）→ 自动回退"主体+事件+区域"规则分组
- 聚类后规则归并：同主体碎片簇合并、按"事件+区域"签名回收噪声点
- 时间字段异常 → 识别照常，趋势标记"无法判断"，不编造
- 风险计算异常 → 该事件降级"需人工研判"，页面不崩溃
- 飞书 Webhook 未配置或推送失败 → 跳过/告警，不影响展示与下载
- 部门映射缺失 → 输出"需人工研判"，不伪造归属

## 目录结构

```text
├── main.py                  # Streamlit 入口（管理看板）
├── config.py                # 全局配置（阈值/权重/路径集中管理）
├── requirements.txt
├── smoke_test.py            # 无头全链路验证
├── ui_test.py               # Streamlit AppTest UI 验证
├── test_robustness.py       # 容错验收测试（11 项）
├── data/
│   ├── input/sample.csv     # 内置样例（37 条，覆盖 5 类高频事件场景）
│   ├── dicts/               # 同义词/主体/事件/敏感事件词典
│   └── output/              # 导出结果（运行时生成）
├── modules/                 # 10 个核心流水线模块
└── utils/helpers.py         # 词典加载等工具
```

## 风险评分说明（可解释规则）

```text
priority_score = 频次分×0.35 + 趋势分×0.30 + 空间集中度分×0.15 + 敏感度分×0.20
风险等级：>=70 高关注；>=40 中关注；其余 一般
```

风险等级是"管理优先级"，不是安全事故定性。每个事件都附带 `risk_reason`，说明等级形成的具体原因（频次、趋势、集中度、敏感要素均来自真实数据）。

## 飞书推送（可选）

页面侧边栏可填入飞书群机器人 Webhook，一键推送 Top5 高关注事件；也可通过环境变量 `FEISHU_WEBHOOK` 注入。代码中不写死任何真实密钥。
