"""SQLite存储层。

包含 Job / Batch / ticket_cleaned / entity_aliases 表。
支持断点续跑、幂等、增量。
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from contextlib import contextmanager
from typing import Any, Dict, Iterable, List, Optional, Tuple

from ticket_cleaner.schema import CleanedTicket


# ---------- 表结构 ----------

# 完整列定义：(列名, 类型)。用于启动时自动迁移旧表，补齐缺失列。
TICKET_CLEANED_COLUMNS = [
    ("ticket_no", "TEXT"),
    ("source_record_id", "TEXT"),
    ("job_id", "TEXT"),
    ("batch_no", "INTEGER"),
    ("raw_content", "TEXT"),
    ("clean_content", "TEXT"),
    ("semantic_content", "TEXT"),
    ("person_raw", "TEXT"),
    ("person_normalized", "TEXT"),
    ("person_confidence", "REAL"),
    ("phone_raw", "TEXT"),
    ("phone_normalized", "TEXT"),
    ("phone_masked", "TEXT"),
    ("phone_match_confidence", "REAL"),
    ("organization_raw", "TEXT"),
    ("organization_normalized", "TEXT"),
    ("organization_confidence", "REAL"),
    ("address_raw", "TEXT"),
    ("address_normalized", "TEXT"),
    ("district", "TEXT"),
    ("town", "TEXT"),
    ("community", "TEXT"),
    ("road", "TEXT"),
    ("building", "TEXT"),
    ("event_type", "TEXT"),
    ("event_detail", "TEXT"),
    ("event_subject", "TEXT"),
    ("event_action", "TEXT"),
    ("event_object", "TEXT"),
    ("ticket_type", "TEXT"),
    ("request_nature", "TEXT"),
    ("issue", "TEXT"),
    ("request", "TEXT"),
    ("time_start", "TEXT"),
    ("time_end", "TEXT"),
    ("time_pattern", "TEXT"),
    ("data_quality_score", "REAL"),
    ("is_usable_for_duplicate", "INTEGER"),
    ("parse_status", "TEXT"),
    ("content_hash", "TEXT"),
    ("pipeline_version", "TEXT"),
    ("processed_at", "TEXT"),
    ("embedding", "BLOB"),
]
SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS cleaning_job (
    id TEXT PRIMARY KEY,
    source_id TEXT,
    total_records INTEGER,
    batch_size INTEGER,
    total_batches INTEGER,
    completed_batches INTEGER DEFAULT 0,
    failed_batches INTEGER DEFAULT 0,
    status TEXT DEFAULT 'PENDING',
    created_at TEXT,
    finished_at TEXT
);

CREATE TABLE IF NOT EXISTS cleaning_batch (
    id TEXT PRIMARY KEY,
    job_id TEXT,
    batch_no INTEGER,
    start_index INTEGER,
    end_index INTEGER,
    status TEXT DEFAULT 'PENDING',
    record_count INTEGER DEFAULT 0,
    success_count INTEGER DEFAULT 0,
    error_count INTEGER DEFAULT 0,
    started_at TEXT,
    finished_at TEXT,
    error_message TEXT,
    UNIQUE(job_id, batch_no)
);

CREATE TABLE IF NOT EXISTS ticket_cleaned (
    ticket_no TEXT,
    source_record_id TEXT,
    job_id TEXT,
    batch_no INTEGER,
    raw_content TEXT,
    clean_content TEXT,
    semantic_content TEXT,
    person_raw TEXT,
    person_normalized TEXT,
    person_confidence REAL,
    phone_raw TEXT,
    phone_normalized TEXT,
    phone_masked TEXT,
    phone_match_confidence REAL,
    organization_raw TEXT,
    organization_normalized TEXT,
    organization_confidence REAL,
    address_raw TEXT,
    address_normalized TEXT,
    district TEXT,
    town TEXT,
    community TEXT,
    road TEXT,
    building TEXT,
    event_type TEXT,
    event_detail TEXT,
    event_subject TEXT,
    event_action TEXT,
    event_object TEXT,
    ticket_type TEXT,
    request_nature TEXT,
    issue TEXT,
    request TEXT,
    time_start TEXT,
    time_end TEXT,
    time_pattern TEXT,
    data_quality_score REAL,
    is_usable_for_duplicate INTEGER,
    parse_status TEXT,
    content_hash TEXT,
    pipeline_version TEXT,
    processed_at TEXT,
    embedding BLOB,
    PRIMARY KEY (ticket_no, job_id)
);

CREATE INDEX IF NOT EXISTS idx_cleaned_job ON ticket_cleaned(job_id);
CREATE INDEX IF NOT EXISTS idx_cleaned_hash ON ticket_cleaned(content_hash);
CREATE INDEX IF NOT EXISTS idx_cleaned_usable ON ticket_cleaned(is_usable_for_duplicate);

CREATE TABLE IF NOT EXISTS entity_aliases (
    entity_id TEXT,
    canonical_name TEXT,
    alias TEXT,
    entity_type TEXT,
    confidence REAL,
    source TEXT,
    PRIMARY KEY (entity_id, alias)
);

CREATE INDEX IF NOT EXISTS idx_entity_alias ON entity_aliases(alias);

CREATE TABLE IF NOT EXISTS cleaner_cache (
    content_hash TEXT PRIMARY KEY,
    result_json TEXT,
    created_at TEXT
);
"""


class Storage:
    """SQLite 存储封装。线程安全（每个连接独立）。"""

    def __init__(self, db_path: str) -> None:
        self.db_path = db_path
        self._local = threading.local()
        # 初始化建表
        with self._conn() as conn:
            conn.executescript(SCHEMA_SQL)
            self._migrate_ticket_cleaned(conn)

    def _migrate_ticket_cleaned(self, conn: sqlite3.Connection) -> None:
        """补齐 ticket_cleaned 历史表缺失的列（向前兼容旧库）。"""
        try:
            existing = {
                row["name"]
                for row in conn.execute("PRAGMA table_info(ticket_cleaned)")
            }
        except sqlite3.OperationalError:
            # 表不存在（理论上不会发生，executescript 已建）
            return
        for col, col_type in TICKET_CLEANED_COLUMNS:
            if col not in existing:
                conn.execute(
                    f"ALTER TABLE ticket_cleaned ADD COLUMN {col} {col_type}"
                )

    @contextmanager
    def _conn(self) -> Iterable[sqlite3.Connection]:
        conn = getattr(self._local, "conn", None)
        if conn is None:
            conn = sqlite3.connect(self.db_path, timeout=30, check_same_thread=False)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA journal_mode=WAL;")
            conn.execute("PRAGMA synchronous=NORMAL;")
            self._local.conn = conn
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise

    # ---------- Job ----------

    def create_job(
        self,
        job_id: str,
        source_id: str,
        total_records: int,
        batch_size: int,
        total_batches: int,
    ) -> None:
        with self._conn() as conn:
            conn.execute(
                """INSERT OR REPLACE INTO cleaning_job
                   (id, source_id, total_records, batch_size, total_batches,
                    completed_batches, failed_batches, status, created_at, finished_at)
                   VALUES (?, ?, ?, ?, ?, 0, 0, 'PENDING', ?, NULL)""",
                (job_id, source_id, total_records, batch_size, total_batches,
                 _now()),
            )

    def update_job_status(self, job_id: str, status: str,
                          finished_at: Optional[str] = None) -> None:
        with self._conn() as conn:
            if finished_at:
                conn.execute(
                    "UPDATE cleaning_job SET status=?, finished_at=? WHERE id=?",
                    (status, finished_at, job_id),
                )
            else:
                conn.execute(
                    "UPDATE cleaning_job SET status=? WHERE id=?",
                    (status, job_id),
                )

    def increment_job_batch(self, job_id: str, success: bool) -> None:
        with self._conn() as conn:
            if success:
                conn.execute(
                    "UPDATE cleaning_job SET completed_batches=completed_batches+1 "
                    "WHERE id=?",
                    (job_id,),
                )
            else:
                conn.execute(
                    "UPDATE cleaning_job SET failed_batches=failed_batches+1 "
                    "WHERE id=?",
                    (job_id,),
                )

    def get_job(self, job_id: str) -> Optional[Dict[str, Any]]:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT * FROM cleaning_job WHERE id=?", (job_id,)
            ).fetchone()
            return dict(row) if row else None

    # ---------- Batch ----------

    def create_batch(self, job_id: str, batch_no: int,
                     start_index: int, end_index: int,
                     record_count: int) -> str:
        batch_id = f"{job_id}-b{batch_no}"
        with self._conn() as conn:
            conn.execute(
                """INSERT OR IGNORE INTO cleaning_batch
                   (id, job_id, batch_no, start_index, end_index, status,
                    record_count, success_count, error_count, started_at,
                    finished_at, error_message)
                   VALUES (?, ?, ?, ?, ?, 'PENDING', ?, 0, 0, NULL, NULL, NULL)""",
                (batch_id, job_id, batch_no, start_index, end_index,
                 record_count),
            )
        return batch_id

    def mark_batch_running(self, batch_id: str) -> None:
        with self._conn() as conn:
            conn.execute(
                "UPDATE cleaning_batch SET status='RUNNING', started_at=? WHERE id=?",
                (_now(), batch_id),
            )

    def mark_batch_done(self, batch_id: str, status: str,
                        success_count: int, error_count: int,
                        error_message: Optional[str] = None) -> None:
        with self._conn() as conn:
            conn.execute(
                """UPDATE cleaning_batch
                   SET status=?, success_count=?, error_count=?,
                       finished_at=?, error_message=?
                   WHERE id=?""",
                (status, success_count, error_count, _now(),
                 error_message, batch_id),
            )

    def get_batch(self, batch_id: str) -> Optional[Dict[str, Any]]:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT * FROM cleaning_batch WHERE id=?", (batch_id,)
            ).fetchone()
            return dict(row) if row else None

    def list_batches(self, job_id: str,
                     status: Optional[str] = None) -> List[Dict[str, Any]]:
        with self._conn() as conn:
            if status:
                rows = conn.execute(
                    "SELECT * FROM cleaning_batch WHERE job_id=? AND status=? "
                    "ORDER BY batch_no",
                    (job_id, status),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM cleaning_batch WHERE job_id=? ORDER BY batch_no",
                    (job_id,),
                ).fetchall()
            return [dict(r) for r in rows]

    def reset_batch_for_retry(self, batch_id: str) -> None:
        with self._conn() as conn:
            conn.execute(
                """UPDATE cleaning_batch
                   SET status='PENDING', started_at=NULL, finished_at=NULL,
                       error_message=NULL, success_count=0, error_count=0
                   WHERE id=?""",
                (batch_id,),
            )

    # ---------- 清洗结果 ----------

    def upsert_cleaned(self, job_id: str, batch_no: int,
                       tickets: Iterable[CleanedTicket]) -> int:
        rows = []
        for t in tickets:
            rows.append((
                t.ticket_no, t.source_record_id, job_id, batch_no,
                t.raw_content, t.clean_content, t.semantic_content,
                t.person_raw, t.person_normalized, t.person_confidence,
                t.phone_raw, t.phone_normalized, t.phone_masked,
                t.phone_match_confidence,
                t.organization_raw, t.organization_normalized,
                t.organization_confidence,
                t.address_raw, t.address_normalized,
                t.district, t.town, t.community, t.road, t.building,
                t.event_type, t.event_detail, t.event_subject,
                t.event_action, t.event_object,
                t.ticket_type, t.request_nature,
                t.issue, t.request,
                t.time_start, t.time_end, t.time_pattern,
                t.data_quality_score,
                int(t.is_usable_for_duplicate),
                t.parse_status,
                t.content_hash, t.pipeline_version, t.processed_at,
                t.embedding,
            ))
        sql = """INSERT OR REPLACE INTO ticket_cleaned
            (ticket_no, source_record_id, job_id, batch_no,
             raw_content, clean_content, semantic_content,
             person_raw, person_normalized, person_confidence,
             phone_raw, phone_normalized, phone_masked, phone_match_confidence,
             organization_raw, organization_normalized, organization_confidence,
             address_raw, address_normalized,
             district, town, community, road, building,
             event_type, event_detail, event_subject, event_action, event_object,
             ticket_type, request_nature, issue, request, time_start, time_end, time_pattern,
             data_quality_score, is_usable_for_duplicate, parse_status,
             content_hash, pipeline_version, processed_at, embedding)
            VALUES (""" + ",".join(["?"] * 43) + ")"
        with self._conn() as conn:
            conn.executemany(sql, rows)
        return len(rows)

    def get_cleaned(self, job_id: str, limit: int = 100,
                    offset: int = 0) -> List[Dict[str, Any]]:
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT * FROM ticket_cleaned WHERE job_id=? "
                "ORDER BY rowid LIMIT ? OFFSET ?",
                (job_id, limit, offset),
            ).fetchall()
            return [dict(r) for r in rows]

    def get_cleaned_by_ticket(self, ticket_no: str,
                              job_id: str) -> Optional[Dict[str, Any]]:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT * FROM ticket_cleaned WHERE ticket_no=? AND job_id=?",
                (ticket_no, job_id),
            ).fetchone()
            return dict(row) if row else None

    def group_by_organization(self, job_id: str,
                              min_count: int = 1,
                              limit: int = 200) -> List[Dict[str, Any]]:
        """按主体归一化名称分组聚合。"""
        with self._conn() as conn:
            rows = conn.execute(
                """SELECT organization_normalized AS name,
                          COUNT(*) AS cnt,
                          GROUP_CONCAT(ticket_no) AS ticket_nos
                   FROM ticket_cleaned
                   WHERE job_id=? AND organization_normalized != ''
                   GROUP BY organization_normalized
                   HAVING cnt >= ?
                   ORDER BY cnt DESC
                   LIMIT ?""",
                (job_id, min_count, limit),
            ).fetchall()
            return [dict(r) for r in rows]

    def group_by_address(self, job_id: str, level: str = "town",
                         min_count: int = 1,
                         limit: int = 200) -> List[Dict[str, Any]]:
        """按地点分组聚合。

        level: 'town'(镇街) / 'community'(小区) / 'address'(完整地址)
        """
        col_map = {
            "town": "town",
            "community": "community",
            "address": "address_normalized",
        }
        col = col_map.get(level, "town")
        with self._conn() as conn:
            rows = conn.execute(
                f"""SELECT {col} AS name,
                           COUNT(*) AS cnt,
                           GROUP_CONCAT(ticket_no) AS ticket_nos
                    FROM ticket_cleaned
                    WHERE job_id=? AND {col} != ''
                    GROUP BY {col}
                    HAVING cnt >= ?
                    ORDER BY cnt DESC
                    LIMIT ?""",
                (job_id, min_count, limit),
            ).fetchall()
            return [dict(r) for r in rows]

    def group_by_town_tree(self, job_id: str,
                           min_count: int = 1) -> List[Dict[str, Any]]:
        """按镇街分组，每个镇街下包含其小区列表（树形结构）。"""
        with self._conn() as conn:
            # 先获取所有镇街及其工单数
            towns = conn.execute(
                """SELECT town AS name,
                          COUNT(*) AS cnt,
                          GROUP_CONCAT(ticket_no) AS ticket_nos
                   FROM ticket_cleaned
                   WHERE job_id=? AND town != ''
                   GROUP BY town
                   HAVING cnt >= ?
                   ORDER BY cnt DESC""",
                (job_id, min_count),
            ).fetchall()

            result = []
            for town_row in towns:
                town_dict = dict(town_row)
                town_name = town_dict["name"]

                # 获取该镇街下的小区
                communities = conn.execute(
                    """SELECT community AS name,
                              COUNT(*) AS cnt,
                              GROUP_CONCAT(ticket_no) AS ticket_nos
                       FROM ticket_cleaned
                       WHERE job_id=? AND town=? AND community != ''
                       GROUP BY community
                       HAVING cnt >= ?
                       ORDER BY cnt DESC""",
                    (job_id, town_name, min_count),
                ).fetchall()

                town_dict["children"] = [dict(c) for c in communities]
                result.append(town_dict)

            return result

    def search_cleaned(self, job_id: str,
                       organization: Optional[str] = None,
                       town: Optional[str] = None,
                       community: Optional[str] = None,
                       event_type: Optional[str] = None,
                       keyword: Optional[str] = None,
                       usable_only: bool = False,
                       limit: int = 100,
                       offset: int = 0) -> Tuple[List[Dict[str, Any]], int]:
        """按条件检索清洗结果。返回 (rows, total)。"""
        where = ["job_id=?"]
        params: List[Any] = [job_id]
        if organization:
            where.append("organization_normalized=?")
            params.append(organization)
        if town:
            where.append("town=?")
            params.append(town)
        if community:
            where.append("community=?")
            params.append(community)
        if event_type:
            where.append("event_type=?")
            params.append(event_type)
        if keyword:
            where.append("(clean_content LIKE ? OR semantic_content LIKE ? OR raw_content LIKE ?)")
            kw = f"%{keyword}%"
            params.extend([kw, kw, kw])
        if usable_only:
            where.append("is_usable_for_duplicate=1")
        where_clause = " AND ".join(where)

        with self._conn() as conn:
            total = conn.execute(
                f"SELECT COUNT(*) AS c FROM ticket_cleaned WHERE {where_clause}",
                params,
            ).fetchone()["c"]
            rows = conn.execute(
                f"SELECT * FROM ticket_cleaned WHERE {where_clause} "
                f"ORDER BY rowid LIMIT ? OFFSET ?",
                params + [limit, offset],
            ).fetchall()
        # 去掉 embedding 字段
        result = []
        for r in rows:
            d = dict(r)
            d.pop("embedding", None)
            result.append(d)
        return result, total

    def find_duplicates(self, job_id: str, content_hash: str) -> List[Dict[str, Any]]:
        """查找完全重复工单（基于content_hash）。"""
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT * FROM ticket_cleaned WHERE job_id=? AND content_hash=? "
                "ORDER BY rowid",
                (job_id, content_hash),
            ).fetchall()
        result = []
        for r in rows:
            d = dict(r)
            d.pop("embedding", None)
            result.append(d)
        return result

    def find_related_by_organization(self, job_id: str, organization: str,
                                     exclude_ticket_no: str = "",
                                     limit: int = 50) -> List[Dict[str, Any]]:
        """查找同一主体的相关联工单。"""
        if not organization:
            return []
        with self._conn() as conn:
            where = ["job_id=?", "organization_normalized=?"]
            params = [job_id, organization]
            if exclude_ticket_no:
                where.append("ticket_no!=?")
                params.append(exclude_ticket_no)
            where_clause = " AND ".join(where)
            rows = conn.execute(
                f"SELECT * FROM ticket_cleaned WHERE {where_clause} "
                f"ORDER BY rowid LIMIT ?",
                params + [limit],
            ).fetchall()
        result = []
        for r in rows:
            d = dict(r)
            d.pop("embedding", None)
            result.append(d)
        return result

    def find_related_by_address(self, job_id: str, town: str, community: str = "",
                                exclude_ticket_no: str = "",
                                limit: int = 50) -> List[Dict[str, Any]]:
        """查找同一地点的相关联工单。"""
        if not town:
            return []
        with self._conn() as conn:
            where = ["job_id=?", "town=?"]
            params = [job_id, town]
            if community:
                where.append("community=?")
                params.append(community)
            if exclude_ticket_no:
                where.append("ticket_no!=?")
                params.append(exclude_ticket_no)
            where_clause = " AND ".join(where)
            rows = conn.execute(
                f"SELECT * FROM ticket_cleaned WHERE {where_clause} "
                f"ORDER BY rowid LIMIT ?",
                params + [limit],
            ).fetchall()
        result = []
        for r in rows:
            d = dict(r)
            d.pop("embedding", None)
            result.append(d)
        return result

    def list_event_types(self, job_id: str) -> List[Dict[str, Any]]:
        """列出本任务所有事件类型及计数。"""
        with self._conn() as conn:
            rows = conn.execute(
                """SELECT event_type AS name, COUNT(*) AS cnt
                   FROM ticket_cleaned
                   WHERE job_id=? AND event_type != ''
                   GROUP BY event_type
                   ORDER BY cnt DESC""",
                (job_id,),
            ).fetchall()
            return [dict(r) for r in rows]

    def list_towns(self, job_id: str) -> List[Dict[str, Any]]:
        """列出本任务所有镇街及计数。"""
        with self._conn() as conn:
            rows = conn.execute(
                """SELECT town AS name, COUNT(*) AS cnt
                   FROM ticket_cleaned
                   WHERE job_id=? AND town != ''
                   GROUP BY town
                   ORDER BY cnt DESC""",
                (job_id,),
            ).fetchall()
            return [dict(r) for r in rows]

    def get_existing_hash(self, job_id: str,
                          hashes: List[str]) -> Dict[str, str]:
        """返回已存在的 content_hash -> ticket_no 映射（用于增量）。"""
        if not hashes:
            return {}
        placeholders = ",".join(["?"] * len(hashes))
        with self._conn() as conn:
            rows = conn.execute(
                f"SELECT content_hash, ticket_no FROM ticket_cleaned "
                f"WHERE job_id=? AND content_hash IN ({placeholders})",
                [job_id] + hashes,
            ).fetchall()
            return {r["content_hash"]: r["ticket_no"] for r in rows}

    # ---------- 实体词典（全局共享） ----------

    def upsert_entity_alias(self, entity_id: str, canonical_name: str,
                            alias: str, entity_type: str,
                            confidence: float, source: str) -> None:
        with self._conn() as conn:
            conn.execute(
                """INSERT OR REPLACE INTO entity_aliases
                   (entity_id, canonical_name, alias, entity_type, confidence, source)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (entity_id, canonical_name, alias, entity_type, confidence,
                 source),
            )

    def lookup_entity_by_alias(self, alias: str,
                               entity_type: Optional[str] = None
                               ) -> Optional[Dict[str, Any]]:
        with self._conn() as conn:
            if entity_type:
                row = conn.execute(
                    "SELECT * FROM entity_aliases WHERE alias=? AND entity_type=?",
                    (alias, entity_type),
                ).fetchone()
            else:
                row = conn.execute(
                    "SELECT * FROM entity_aliases WHERE alias=?", (alias,)
                ).fetchone()
            return dict(row) if row else None

    # ---------- 缓存 ----------

    def get_cache(self, content_hash: str) -> Optional[Dict[str, Any]]:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT result_json FROM cleaner_cache WHERE content_hash=?",
                (content_hash,),
            ).fetchone()
            if row is None:
                return None
            return json.loads(row["result_json"])

    def set_cache(self, content_hash: str, result: Dict[str, Any]) -> None:
        with self._conn() as conn:
            conn.execute(
                """INSERT OR REPLACE INTO cleaner_cache
                   (content_hash, result_json, created_at)
                   VALUES (?, ?, ?)""",
                (content_hash, json.dumps(result, ensure_ascii=False), _now()),
            )

    # ---------- 统计 ----------

    def job_stats(self, job_id: str) -> Dict[str, Any]:
        with self._conn() as conn:
            job = conn.execute(
                "SELECT * FROM cleaning_job WHERE id=?", (job_id,)
            ).fetchone()
            if job is None:
                return {}
            total = conn.execute(
                "SELECT COUNT(*) AS c FROM ticket_cleaned WHERE job_id=?",
                (job_id,),
            ).fetchone()["c"]
            usable = conn.execute(
                "SELECT COUNT(*) AS c FROM ticket_cleaned WHERE job_id=? "
                "AND is_usable_for_duplicate=1",
                (job_id,),
            ).fetchone()["c"]
            org_rate = conn.execute(
                "SELECT COUNT(*) AS c FROM ticket_cleaned WHERE job_id=? "
                "AND organization_normalized != ''",
                (job_id,),
            ).fetchone()["c"]
            addr_rate = conn.execute(
                "SELECT COUNT(*) AS c FROM ticket_cleaned WHERE job_id=? "
                "AND address_normalized != ''",
                (job_id,),
            ).fetchone()["c"]
            event_rate = conn.execute(
                "SELECT COUNT(*) AS c FROM ticket_cleaned WHERE job_id=? "
                "AND event_type != ''",
                (job_id,),
            ).fetchone()["c"]
            req_rate = conn.execute(
                "SELECT COUNT(*) AS c FROM ticket_cleaned WHERE job_id=? "
                "AND request != ''",
                (job_id,),
            ).fetchone()["c"]
            failed = conn.execute(
                "SELECT COUNT(*) AS c FROM ticket_cleaned WHERE job_id=? "
                "AND parse_status='failed'",
                (job_id,),
            ).fetchone()["c"]
            return {
                "job": dict(job),
                "total_cleaned": total,
                "usable_for_duplicate": usable,
                "failed": failed,
                "org_recognition_rate": org_rate / total if total else 0,
                "addr_recognition_rate": addr_rate / total if total else 0,
                "event_recognition_rate": event_rate / total if total else 0,
                "request_recognition_rate": req_rate / total if total else 0,
            }


def _now() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime())
