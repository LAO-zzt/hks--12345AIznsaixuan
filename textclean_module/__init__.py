"""12345 热线 AI 工单数据清洗与标准化模块（独立可集成版）。

把原始 12345 自然语言工单转换为可被 AI 跨工单比较、聚类与重复识别的
结构化业务数据。本目录是一个自包含模块，可直接拷贝给前端 / 其他项目使用。

典型用法（Python 门面）
-----------------------
    import textclean_module as tcm

    cleaner = tcm.TextCleaner()                       # 默认使用模块内 data/ 工作目录
    cleaner.clean_excel("data/工单.xlsx")             # 清洗并写入 SQLite
    cleaner.find_duplicates("job-工单")               # 重复识别
    rows, total = cleaner.search("job-工单", event_type="物业管理")
    print(cleaner.stats("job-工单"))

或启动 REST 服务供前端调用：
    cleaner.run_server(port=5000)
"""
from __future__ import annotations

import importlib.util
import os
import sys
from dataclasses import asdict
from pathlib import Path
from typing import Any, Dict, List, Optional

# 让内部 `ticket_cleaner` 的绝对导入可用（无需 pip install）
_MODULE_DIR = Path(__file__).resolve().parent
if str(_MODULE_DIR) not in sys.path:
    sys.path.insert(0, str(_MODULE_DIR))

from ticket_cleaner.config import Config
from ticket_cleaner.storage import Storage
from ticket_cleaner.batch_engine import BatchEngine
from ticket_cleaner.duplicate import DuplicateDetector
from ticket_cleaner.schema import TicketRecord, CleanedTicket

__version__ = "1.0.0"
__pipeline_version__ = "clean-v1.0"

DEFAULT_DATA_DIR = _MODULE_DIR / "data"


class TextCleaner:
    """统一门面：一个对象串联 清洗 / 去重 / 检索 / 统计。"""

    def __init__(
        self,
        data_dir: Optional[str] = None,
        db_path: Optional[str] = None,
        batch_size: int = 1000,
        embedding_dim: int = 256,
        min_quality_score: float = 0.3,
        enable_cache: bool = True,
    ) -> None:
        self.data_dir = Path(data_dir or DEFAULT_DATA_DIR)
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.db_path = db_path or str(self.data_dir / "cleaner.db")
        self.config = Config(
            db_path=self.db_path,
            batch_size=batch_size,
            embedding_dim=embedding_dim,
            min_quality_score=min_quality_score,
            enable_cache=enable_cache,
            work_dir=str(self.data_dir),
            source_excel_path="",
        )
        self.storage = Storage(self.db_path)

    # ---- 清洗 ----
    def clean_excel(
        self,
        excel_path: str,
        job_id: Optional[str] = None,
        batch_size: Optional[int] = None,
        on_progress=None,
    ) -> Dict[str, Any]:
        """读取 Excel 工单 -> 清洗 -> 实体抽取 -> 归一 -> Semantic -> Embedding -> 入库。

        Args:
            excel_path: .xlsx 文件路径
            job_id:     任务ID（留空自动用文件名生成）
            batch_size: 单批条数（200/500/1000/2000）
            on_progress: 进度回调 ProgressInfo -> None
        Returns:
            job_stats 字典
        """
        excel_path = str(excel_path)
        if not os.path.exists(excel_path):
            raise FileNotFoundError(f"数据文件不存在: {excel_path}")
        if job_id is None:
            job_id = "job-" + os.path.splitext(os.path.basename(excel_path))[0]
        if batch_size:
            self.config.batch_size = batch_size
        self.config.source_excel_path = excel_path
        engine = BatchEngine(self.config)
        if engine.storage.get_job(job_id) is None:
            engine.create_job(job_id)
        return engine.run_job(job_id, on_progress=on_progress)

    # ---- 重复识别 ----
    def find_duplicates(
        self, job_id: str, top_k: int = 50, max_pairs: int = 200
    ) -> List[Dict[str, Any]]:
        detector = DuplicateDetector(self.storage)
        cands = detector.find_candidates(job_id, top_k=top_k, max_pairs=max_pairs)
        return [asdict(c) for c in cands]

    # ---- 检索 ----
    def search(self, job_id: str, limit: int = 100, offset: int = 0, **filters):
        """按条件检索清洗结果。filters 支持 organization/town/community/
        event_type/keyword/usable_only。返回 (rows, total)。"""
        return self.storage.search_cleaned(
            job_id, limit=limit, offset=offset, **filters
        )

    def get_ticket(self, job_id: str, ticket_no: str) -> Optional[Dict[str, Any]]:
        return self.storage.get_cleaned_by_ticket(ticket_no, job_id)

    # ---- 统计 ----
    def stats(self, job_id: str) -> Dict[str, Any]:
        return self.storage.job_stats(job_id)

    def list_jobs(self) -> List[Dict[str, Any]]:
        with self.storage._conn() as conn:
            rows = conn.execute(
                "SELECT * FROM cleaning_job ORDER BY created_at DESC LIMIT 200"
            ).fetchall()
        return [dict(r) for r in rows]

    # ---- 服务 ----
    def run_server(self, host: str = "127.0.0.1", port: int = 5000,
                   debug: bool = False) -> None:
        """启动 Flask REST 服务（供前端调用）。"""
        spec = importlib.util.spec_from_file_location(
            "textclean_module_server", _MODULE_DIR / "server.py"
        )
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        mod.app.run(host=host, port=port, debug=debug, threaded=True)


__all__ = [
    "TextCleaner",
    "Config",
    "Storage",
    "BatchEngine",
    "DuplicateDetector",
    "TicketRecord",
    "CleanedTicket",
    "__version__",
    "__pipeline_version__",
]
