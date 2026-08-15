# 数据目录

本目录是模块的默认工作区（db / 缓存 / 上传 / 日志 / 样例数据）。

- `cleaner.db`        清洗结果与 Job/Batch 元数据的 SQLite 库（自动创建）
- `uploads/`          前端上传的 Excel 暂存
- `cache/`            高德 POI 缓存
- `logs/`             运行日志
- `testdata/`        抽样测试数据集（可选，由 tools/make_dataset.py 生成）

把你的工单 Excel 放到本目录（或任意路径），通过
`TextCleaner.clean_excel(path)` 或 REST 上传接口即可开始清洗。
