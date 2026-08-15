"""Batch处理引擎。

特性：
- 分Batch串行处理（默认）
- 断点续跑（基于 job_id + batch_no 幂等）
- 失败重试（单Batch重跑，不影响其他Batch）
- 连续失败暂停
- Embedding批处理
- 增量清洗（content_hash 缓存）
- 全局共享实体词典
"""

from __future__ import annotations

import logging
import threading
import time
import traceback
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from ticket_cleaner.config import Config
from ticket_cleaner.embedding import (
    EmbedderBase,
    TfidfEmbedder,
    serialize_embedding,
)
from ticket_cleaner.pipeline import CleaningPipeline
from ticket_cleaner.reader import ExcelReader
from ticket_cleaner.schema import CleanedTicket, TicketRecord
from ticket_cleaner.storage import Storage

# 配置日志
logger = logging.getLogger(__name__)


# ---------- 进度回调 ----------

@dataclass
class ProgressInfo:
    job_id: str
    batch_no: int
    total_batches: int
    status: str  # PENDING/RUNNING/SUCCESS/PARTIAL_SUCCESS/FAILED
    stage: str   # cleaning/embedding/done
    processed: int
    total: int
    message: str = ""

    def as_dict(self) -> Dict[str, Any]:
        return {
            "job_id": self.job_id,
            "batch_no": self.batch_no,
            "total_batches": self.total_batches,
            "status": self.status,
            "stage": self.stage,
            "processed": self.processed,
            "total": self.total,
            "message": self.message,
            "percent": round(self.processed / self.total * 100, 2) if self.total else 0.0,
        }


class BatchEngine:
    """Batch 处理引擎。"""

    def __init__(
        self,
        config: Config,
        storage: Optional[Storage] = None,
        reader: Optional[ExcelReader] = None,
        pipeline: Optional[CleaningPipeline] = None,
        embedder: Optional[EmbedderBase] = None,
    ) -> None:
        self.config = config
        config.validate_batch_size()
        self.storage = storage or Storage(config.db_path)
        self.reader = reader or ExcelReader(config.source_excel_path)
        self.pipeline = pipeline or CleaningPipeline(config, self.storage)
        self.embedder = embedder or TfidfEmbedder(target_dim=config.embedding_dim)
        self._embedder_fitted = False
        # 停止控制：每个 job_id 对应一个 Event
        self._stop_events: Dict[str, threading.Event] = {}

    def request_stop(self, job_id: str) -> bool:
        """请求停止指定 Job。返回是否成功设置停止标志。"""
        if job_id in self._stop_events:
            self._stop_events[job_id].set()
            return True
        return False

    def _should_stop(self, job_id: str) -> bool:
        """检查是否应该停止。"""
        return job_id in self._stop_events and self._stop_events[job_id].is_set()

    # ---------- Job 管理 ----------

    def create_job(self, job_id: str, source_id: str = "excel") -> Dict[str, Any]:
        """创建Job并切分Batch。"""
        total = self.reader.count()
        batch_size = self.config.batch_size
        total_batches = (total + batch_size - 1) // batch_size
        self.storage.create_job(job_id, source_id, total, batch_size, total_batches)
        # 预创建Batch记录
        for b in range(1, total_batches + 1):
            start = (b - 1) * batch_size
            end = min(start + batch_size, total)
            self.storage.create_batch(job_id, b, start, end, end - start)
        return {
            "job_id": job_id,
            "total_records": total,
            "batch_size": batch_size,
            "total_batches": total_batches,
        }

    # ---------- 断点续跑 ----------

    def run_job(self, job_id: str, limit_batches: Optional[int] = None,
                on_progress=None) -> Dict[str, Any]:
        """运行整个Job。自动跳过已完成的Batch。"""
        # 初始化停止事件
        self._stop_events[job_id] = threading.Event()

        job = self.storage.get_job(job_id)
        if job is None:
            raise ValueError(f"Job不存在: {job_id}")

        logger.info("=" * 60)
        logger.info(f"开始执行 Job: {job_id}")
        logger.info(f"总记录数: {job['total_records']}, 总批次: {job['total_batches']}")
        logger.info("=" * 60)

        if job["status"] in ("SUCCESS",):
            logger.info(f"Job {job_id} 已完成，跳过")
            return self.storage.job_stats(job_id)

        self.storage.update_job_status(job_id, "RUNNING")

        batches = self.storage.list_batches(job_id, status="PENDING")
        if not batches:
            # 全部已处理过，检查是否所有都成功
            all_batches = self.storage.list_batches(job_id)
            all_success = all(b["status"] == "SUCCESS" for b in all_batches)
            self.storage.update_job_status(
                job_id,
                "SUCCESS" if all_success else "PARTIAL_SUCCESS",
                finished_at=_now(),
            )
            logger.info(f"Job {job_id} 所有批次已处理完成")
            return self.storage.job_stats(job_id)

        logger.info(f"待处理批次数: {len(batches)}")
        consecutive_failures = 0
        processed_batches = 0
        start_time = time.time()
        
        for b in batches:
            batch_start_time = time.time()
            # 检查停止请求
            if self._should_stop(job_id):
                logger.warning(f"收到停止请求，Job {job_id} 将在当前批次完成后停止")
                if on_progress:
                    on_progress(ProgressInfo(
                        job_id=job_id, batch_no=b["batch_no"],
                        total_batches=job["total_batches"],
                        status="STOPPED", stage="stopped",
                        processed=processed_batches,
                        total=job["total_batches"],
                        message="用户请求停止",
                    ))
                self.storage.update_job_status(job_id, "STOPPED", finished_at=_now())
                break

            if limit_batches is not None and processed_batches >= limit_batches:
                break
            
            logger.info(f"\n--- 批次 {b['batch_no']}/{job['total_batches']} ---")
            logger.info(f"记录范围: {b['start_index']}-{b['end_index']} ({b['record_count']}条)")
            try:
                ok = self.run_batch(job_id, b["batch_no"], on_progress=on_progress)
                processed_batches += 1
                batch_elapsed = time.time() - batch_start_time
                
                if ok:
                    logger.info(f"✓ 批次 {b['batch_no']} 完成，耗时 {batch_elapsed:.2f}s")
                    consecutive_failures = 0
                else:
                    logger.error(f"✗ 批次 {b['batch_no']} 失败，耗时 {batch_elapsed:.2f}s")
                    consecutive_failures += 1
                    if consecutive_failures >= self.config.max_consecutive_batch_failures:
                        self.storage.update_job_status(
                            job_id, "FAILED",
                            finished_at=_now(),
                        )
                        if on_progress:
                            on_progress(ProgressInfo(
                                job_id=job_id, batch_no=b["batch_no"],
                                total_batches=job["total_batches"],
                                status="FAILED", stage="aborted",
                                processed=processed_batches,
                                total=job["total_batches"],
                                message=f"连续 {consecutive_failures} 个Batch失败，已暂停",
                            ))
                        break
            except Exception as e:
                consecutive_failures += 1
                if consecutive_failures >= self.config.max_consecutive_batch_failures:
                    self.storage.update_job_status(
                        job_id, "FAILED", finished_at=_now()
                    )
                    raise

        # 清理停止事件
        self._stop_events.pop(job_id, None)

        # 最终状态
        all_batches = self.storage.list_batches(job_id)
        all_success = all(b["status"] == "SUCCESS" for b in all_batches)
        any_failed = any(b["status"] == "FAILED" for b in all_batches)
        final_status = "SUCCESS" if all_success else (
            "PARTIAL_SUCCESS" if not any(
                b["status"] in ("FAILED",) for b in all_batches
            ) and any(b["status"] == "SUCCESS" for b in all_batches)
            else "PARTIAL_SUCCESS"
        )
        # 简化：只要有失败就 PARTIAL_SUCCESS
        if any_failed:
            final_status = "PARTIAL_SUCCESS"
        self.storage.update_job_status(
            job_id, final_status, finished_at=_now()
        )
        return self.storage.job_stats(job_id)

    # ---------- 单Batch ----------

    def run_batch(self, job_id: str, batch_no: int,
                  on_progress=None) -> bool:
        """运行单个Batch。返回是否成功。"""
        batch_id = f"{job_id}-b{batch_no}"
        batch = self.storage.get_batch(batch_id)
        if batch is None:
            raise ValueError(f"Batch不存在: {batch_id}")

        # 幂等：已完成则跳过
        if batch["status"] == "SUCCESS":
            return True

        self.storage.mark_batch_running(batch_id)
        if on_progress:
            on_progress(ProgressInfo(
                job_id=job_id, batch_no=batch_no,
                total_batches=batch["record_count"],
                status="RUNNING", stage="cleaning",
                processed=0, total=batch["record_count"],
                message="开始清洗",
            ))

        try:
            logger.info(f"  读取记录...")
            records = self.reader.read_range(batch["start_index"], batch["end_index"])
            if not records:
                logger.info(f"  无记录，跳过")
                self.storage.mark_batch_done(batch_id, "SUCCESS", 0, 0)
                self.storage.increment_job_batch(job_id, success=True)
                return True

            logger.info(f"  读取到 {len(records)} 条记录")
            
            # 增量：跳过 content_hash 未变化的
            hashes = [r.content_hash() for r in records]
            existing = self.storage.get_existing_hash(job_id, hashes) if self.config.enable_cache else {}

            # 缓存命中：直接复用
            cached_results: List[CleanedTicket] = []
            to_process: List[TicketRecord] = []
            for r in records:
                h = r.content_hash()
                if h in existing and self.config.enable_cache:
                    cached = self.storage.get_cache(h)
                    if cached:
                        t = CleanedTicket(**cached) if isinstance(cached, dict) else None
                        if t:
                            cached_results.append(t)
                            continue
                to_process.append(r)
            
            if cached_results:
                logger.info(f"  缓存命中: {len(cached_results)} 条，需处理: {len(to_process)} 条")

            # 清洗
            logger.info(f"  开始清洗...")
            cleaned = self.pipeline.process_batch(to_process)
            logger.info(f"  清洗完成: {len(cleaned)} 条")

            # Embedding（批处理）
            if on_progress:
                on_progress(ProgressInfo(
                    job_id=job_id, batch_no=batch_no,
                    total_batches=batch["record_count"],
                    status="RUNNING", stage="embedding",
                    processed=len(cached_results),
                    total=batch["record_count"],
                    message="生成Embedding",
                ))
            logger.info(f"  生成Embedding...")
            self._embed(cleaned)
            logger.info(f"  Embedding完成")

            # 合并结果
            all_cleaned = cached_results + cleaned

            # 缓存新结果
            if self.config.enable_cache:
                for t in cleaned:
                    try:
                        # 不缓存 embedding bytes
                        d = {k: v for k, v in t.to_dict().items() if k != "embedding"}
                        self.storage.set_cache(t.content_hash, d)
                    except Exception:
                        pass

            # 入库
            self.storage.upsert_cleaned(job_id, batch_no, all_cleaned)

            success_count = sum(1 for t in all_cleaned if t.parse_status != "failed")
            error_count = len(all_cleaned) - success_count
            status = "SUCCESS" if error_count == 0 else "PARTIAL_SUCCESS"
            self.storage.mark_batch_done(batch_id, status, success_count, error_count)
            self.storage.increment_job_batch(job_id, success=True)

            if on_progress:
                on_progress(ProgressInfo(
                    job_id=job_id, batch_no=batch_no,
                    total_batches=batch["record_count"],
                    status=status, stage="done",
                    processed=len(all_cleaned),
                    total=batch["record_count"],
                    message=f"Batch {batch_no} 完成",
                ))
            return True

        except Exception as e:
            err = traceback.format_exc()
            logger.error(f"✗ 批次 {batch_no} 失败，错误详情:\n{err}")
            self.storage.mark_batch_done(batch_id, "FAILED", 0, 0, error_message=err)
            self.storage.increment_job_batch(job_id, success=False)
            if on_progress:
                on_progress(ProgressInfo(
                    job_id=job_id, batch_no=batch_no,
                    total_batches=batch["record_count"],
                    status="FAILED", stage="error",
                    processed=0, total=batch["record_count"],
                    message=f"Batch {batch_no} 失败: {e}",
                ))
            return False

    # ---------- Embedding ----------

    def _embed(self, tickets: List[CleanedTicket]) -> None:
        if not tickets:
            return
        texts = [t.semantic_content or t.clean_content or t.raw_content
                 for t in tickets]
        # 首次fit
        if not self._embedder_fitted and isinstance(self.embedder, TfidfEmbedder):
            # 用本批文本fit（生产环境可用更大样本预fit）
            self.embedder.fit(texts)
            self._embedder_fitted = True
        vecs = self.embedder.embed(texts)
        for t, v in zip(tickets, vecs):
            t.embedding = serialize_embedding(v)

    # ---------- 重试 ----------

    def retry_batch(self, job_id: str, batch_no: int,
                    on_progress=None) -> bool:
        """重试失败的Batch。"""
        batch_id = f"{job_id}-b{batch_no}"
        self.storage.reset_batch_for_retry(batch_id)
        return self.run_batch(job_id, batch_no, on_progress=on_progress)

    # ---------- 增量 ----------

    def run_incremental(self, job_id: str, on_progress=None) -> Dict[str, Any]:
        """增量清洗。仅处理 content_hash 未变化的会跳过。"""
        return self.run_job(job_id, on_progress=on_progress)


def _now() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime())
