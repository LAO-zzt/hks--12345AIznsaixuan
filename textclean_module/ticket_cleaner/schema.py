"""统一数据Schema。

将不同来源工单字段映射到统一结构；保留 raw / clean / normalized / semantic 多层文本。
原始数据永不覆盖。
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field, asdict
from typing import Any, Dict, Optional


# ---------- 原始工单 ----------

@dataclass
class TicketRecord:
    """统一Schema的原始工单。不同来源字段映射到这里。"""

    ticket_no: str = ""
    caller_name: str = ""
    phone: str = ""
    title: str = ""
    content: str = ""
    organization: str = ""
    address: str = ""
    region: str = ""
    created_at: str = ""
    department: str = ""
    # 源记录标识，用于增量
    source_record_id: str = ""
    source_seq: int = 0

    def content_hash(self) -> str:
        """对关键输入计算hash，用于增量/缓存。"""
        key = f"{self.ticket_no}|{self.content}|{self.title}"
        return hashlib.md5(key.encode("utf-8")).hexdigest()


# ---------- 清洗后工单 ----------

@dataclass
class CleanedTicket:
    """单条工单的清洗结果。同时保留 raw / clean / semantic。"""

    # 标识
    ticket_no: str = ""
    source_record_id: str = ""

    # 多层文本（不覆盖原文）
    raw_content: str = ""
    clean_content: str = ""
    semantic_content: str = ""

    # 人物
    person_raw: str = ""
    person_normalized: str = ""
    person_confidence: float = 0.0

    # 手机号
    phone_raw: str = ""
    phone_normalized: str = ""
    phone_masked: str = ""
    phone_match_confidence: float = 0.0

    # 主体/机构
    organization_raw: str = ""
    organization_normalized: str = ""
    organization_confidence: float = 0.0

    # 地点
    address_raw: str = ""
    address_normalized: str = ""
    district: str = ""
    town: str = ""
    community: str = ""
    road: str = ""
    building: str = ""

    # 事件
    event_type: str = ""
    event_detail: str = ""
    event_subject: str = ""
    event_action: str = ""
    event_object: str = ""

    # 工单类型：线上/线下
    ticket_type: str = ""  # "online" / "offline" / "unknown"

    # 诉求性质：投诉/建议/举报/咨询/求助
    request_nature: str = ""  # "complaint" / "suggestion" / "report" / "consultation" / "help" / "unknown"

    # 诉求
    issue: str = ""
    request: str = ""

    # 时间
    time_start: str = ""
    time_end: str = ""
    time_pattern: str = ""

    # 质量与可用性
    data_quality_score: float = 0.0
    is_usable_for_duplicate: bool = False
    parse_status: str = "success"

    # 版本与缓存
    content_hash: str = ""
    pipeline_version: str = "clean-v1.0"
    processed_at: str = ""

    # Embedding（序列化为bytes/base64时再处理）
    embedding: Optional[bytes] = None

    def to_dict(self) -> Dict[str, Any]:
        d = asdict(self)
        return d


# ---------- 字段映射 ----------

class TicketSchema:
    """字段映射器：把外部DataFrame行映射到 TicketRecord。"""

    # 测试数据集字段映射
    FIELD_MAP = {
        "序号": "source_seq",
        "工单编号": "ticket_no",
        "标题": "title",
        "内容": "content",
    }

    @classmethod
    def from_row(cls, row: Dict[str, Any]) -> TicketRecord:
        rec = TicketRecord()
        for src, dst in cls.FIELD_MAP.items():
            if src in row:
                val = row[src]
                if val is None:
                    val = ""
                if dst == "source_seq":
                    try:
                        setattr(rec, dst, int(val))
                    except (TypeError, ValueError):
                        setattr(rec, dst, 0)
                else:
                    setattr(rec, dst, str(val).strip())
        rec.source_record_id = f"seq-{rec.source_seq}"
        return rec
