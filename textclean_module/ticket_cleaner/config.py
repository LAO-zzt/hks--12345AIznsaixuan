"""模块配置。"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Optional


@dataclass
class Config:
    """清洗模块配置。所有参数集中管理，便于整合到宿主项目。"""

    # ---- 数据库 ----
    db_path: str = "database/cleaner.db"

    # ---- Batch ----
    batch_size: int = 1000
    """每批处理记录数。允许 200/500/1000/2000。"""

    max_consecutive_batch_failures: int = 5
    """连续多个Batch异常后暂停Job。"""

    # ---- Pipeline 版本 ----
    pipeline_version: str = "clean-v1.0"

    # ---- Embedding ----
    embedding_backend: str = "tfidf"
    """embedding 后端：tfidf（默认本地）或外部自定义。"""

    embedding_dim: int = 256
    """TF-IDF 降维后的维度（使用截断SVD）。"""

    # ---- 实体归一化 ----
    entity_dict_path: Optional[str] = "database/entity_dict.json"
    """全局实体词典路径。跨Batch共享。"""

    # ---- 套话字典 ----
    boilerplate_path: Optional[str] = None
    """套话字典路径。None 则使用内置默认。"""

    # ---- 同义词字典 ----
    synonym_path: Optional[str] = None
    """同义词字典路径。None 则使用内置默认。"""

    # ---- 数据源 ----
    source_excel_path: str = "database/testdata/政数局资料-顺德区12345热线工单（2025年1月至3月）.xlsx"

    # ---- 缓存 ----
    enable_cache: bool = True
    """相同 content_hash 不重复计算。"""

    # ---- 质量分阈值 ----
    min_quality_score: float = 0.3
    """低于该值则 is_usable_for_duplicate = False。"""

    # ---- 临时 ----
    work_dir: str = "database"

    def __post_init__(self) -> None:
        os.makedirs(self.work_dir, exist_ok=True)
        db_dir = os.path.dirname(self.db_path)
        if db_dir:
            os.makedirs(db_dir, exist_ok=True)

    # 批次大小校验
    def validate_batch_size(self) -> None:
        if self.batch_size not in (200, 500, 1000, 2000):
            raise ValueError(
                f"batch_size 必须为 200/500/1000/2000，当前为 {self.batch_size}"
            )
