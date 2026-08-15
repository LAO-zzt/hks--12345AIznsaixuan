"""主清洗Pipeline。

把单条工单从 raw → clean → 抽取 → 归一 → semantic → quality。
不含Batch调度和Embedding（由 BatchEngine 负责编排）。
"""

from __future__ import annotations

import logging
import time
from typing import Dict, List, Optional

logger = logging.getLogger(__name__)

from ticket_cleaner.cleaners import (
    DEFAULT_BOILERPLATE,
    clean_caller_name,
    clean_phone,
    clean_text,
    clean_ticket_no,
    compile_boilerplate,
    weaken_boilerplate,
)
from ticket_cleaner.config import Config
from ticket_cleaner.extractors import (
    extract_address,
    extract_event,
    extract_organization,
    extract_person,
    extract_phone,
    extract_request,
    extract_time,
    classify_ticket_type,
    classify_request_nature,
)
from ticket_cleaner.normalizer import (
    EntityNormalizer,
    build_semantic_content,
    compute_quality_score,
    normalize_address,
)
from ticket_cleaner.schema import CleanedTicket, TicketRecord
from ticket_cleaner.storage import Storage


class CleaningPipeline:
    """单条工单清洗Pipeline。

    线程安全：无状态（normalizer 内部有锁）。
    """

    def __init__(self, config: Config, storage: Optional[Storage] = None,
                 normalizer: Optional[EntityNormalizer] = None) -> None:
        self.config = config
        self.storage = storage
        # 套话字典
        boilerplate = DEFAULT_BOILERPLATE
        if config.boilerplate_path:
            try:
                with open(config.boilerplate_path, "r", encoding="utf-8") as f:
                    boilerplate = [line.strip() for line in f if line.strip()]
            except OSError:
                pass
        self._boilerplate_re = compile_boilerplate(boilerplate)
        # 归一化器
        self.normalizer = normalizer or (
            EntityNormalizer(storage) if storage else EntityNormalizer.__new__(EntityNormalizer)
        )

    def process(self, record: TicketRecord) -> CleanedTicket:
        """处理单条工单。"""
        t = CleanedTicket()
        t.processed_at = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime())
        t.pipeline_version = self.config.pipeline_version

        # 标识
        t.ticket_no = clean_ticket_no(record.ticket_no)
        t.source_record_id = record.source_record_id
        t.content_hash = record.content_hash()
        logger.info(f"[工单 {t.ticket_no}] 开始处理")
        logger.debug(f"  [原文] {record.content[:100]}...")

        # 原文保留
        t.raw_content = record.content or ""

        # ---- 文本清洗 ----
        t.clean_content = clean_text(record.content)
        logger.info(f"  [文本清洗] 原文长度: {len(record.content or '')}, 清洗后: {len(t.clean_content)}")

        # ---- 字段清洗 ----
        caller_normalized, _ = clean_caller_name(record.caller_name)
        # 手机号（字段 + 正文）
        phone_norm, phone_masked, phone_raw, phone_conf = clean_phone(record.phone)
        if not phone_norm:
            # 从正文抽取
            phone_raw, phone_norm, phone_masked, phone_conf = extract_phone(record.content)
        t.phone_raw = phone_raw
        t.phone_normalized = phone_norm
        t.phone_masked = phone_masked
        t.phone_match_confidence = phone_conf
        logger.info(f"  [手机号] 原始: {phone_raw}, 标准化: {phone_norm}, 脱敏: {phone_masked}, 置信度: {phone_conf:.2f}")

        # ---- 人物抽取 ----
        person_raw, person_conf = extract_person(record.content)
        t.person_raw = person_raw
        # 不直接归一为同一个人，仅保留
        t.person_normalized = person_raw  # 弱特征
        t.person_confidence = person_conf
        logger.info(f"  [人物] 提取: {person_raw or '无'}, 置信度: {person_conf:.2f}")

        # ---- 地点抽取（先识别地址） ----
        addr = extract_address(record.content)
        t.district = addr["district"]
        t.town = addr["town"]
        t.community = addr["community"]
        t.road = addr["road"]
        t.building = addr["building"]
        t.address_raw = addr["address_raw"]
        t.address_normalized = normalize_address(addr)
        logger.info(f"  [地址] 区: {t.district or '无'}, 镇街: {t.town or '无'}, 社区: {t.community or '无'}, 道路: {t.road or '无'}, 建筑: {t.building or '无'}")
        logger.info(f"  [地址] 原始: {t.address_raw[:50] if t.address_raw else '无'}...")

        # ---- 主体抽取（从地址中寻找主体，高德验证） ----
        # 策略：地址中通常包含主体，如"大良街道XX小区"中的小区
        org_raw = ""
        org_type = ""
        org_conf = 0.0
        
        # 1. 从地址的community字段提取（小区/社区通常是主体）
        if addr["community"]:
            org_raw = addr["community"]
            org_type = "小区"
            org_conf = 0.8
            logger.debug(f"  [主体] 从地址community提取: {org_raw}")

        # 2. 从地址原文中提取主体（使用规则，仅匹配有机构/小区后缀的名称）
        # 注意：不再把门牌号(building)当作主体——"50号""3栋"不是有效主体，
        # 否则会导致 22% 的主体变成门牌/地址噪声。
        elif addr["address_raw"]:
            from ticket_cleaner.extractors import extract_organization
            org_raw, org_type, org_conf = extract_organization(addr["address_raw"])
            if org_raw:
                logger.debug(f"  [主体] 从地址原文提取: {org_raw}")

        # 3. 如果地址中没找到，从全文提取
        if not org_raw:
            from ticket_cleaner.extractors import extract_organization
            org_raw, org_type, org_conf = extract_organization(record.content)
            if org_raw:
                logger.debug(f"  [主体] 从全文提取: {org_raw}")
        
        # 5. 高德地图验证
        if org_raw:
            from ticket_cleaner.gaode_cache import verify_entity_in_gaode
            logger.debug(f"  [主体] 调用高德验证: {org_raw}")
            if verify_entity_in_gaode(org_raw):
                logger.info(f"  [主体] ✓ 高德验证通过: {org_raw}")
                t.organization_normalized = org_raw
                t.organization_confidence = 0.9
            else:
                logger.debug(f"  [主体] 高德未命中，保留原名: {org_raw}")
                t.organization_normalized = org_raw
                t.organization_confidence = org_conf
        else:
            t.organization_normalized = ""
            t.organization_confidence = 0.0
        
        t.organization_raw = org_raw
        
        if org_raw:
            logger.info(f"  [主体] 最终结果: {org_raw} (类型: {org_type}, 置信度: {t.organization_confidence:.2f})")

        # ---- 时间抽取 ----
        ts, te, tp, tconf = extract_time(record.content)
        t.time_start = ts
        t.time_end = te
        t.time_pattern = tp
        logger.info(f"  [时间] 开始: {ts or '无'}, 结束: {te or '无'}, 模式: {tp or '无'}")

        # ---- 事件抽取 ----
        ev = extract_event(record.content, record.title, org_raw, t.address_raw)
        t.event_type = ev["event_type"]
        t.event_detail = ev["event_detail"]
        t.event_subject = ev["event_subject"]
        t.event_action = ev["event_action"]
        t.event_object = ev["event_object"]
        logger.info(f"  [事件] 类型: {t.event_type or '无'}, 主体: {t.event_subject or '无'}, 动作: {t.event_action or '无'}")
        if not t.event_type:
            logger.warning(f"  [事件] 规则抽取失败，无法识别事件类型")

        # ---- 工单类型判断（线上/线下） ----
        t.ticket_type = classify_ticket_type(record.content)
        logger.info(f"  [工单类型] {t.ticket_type}")

        # ---- 诉求性质分类（投诉/建议/举报/咨询/求助） ----
        t.request_nature = classify_request_nature(record.content)
        logger.info(f"  [诉求性质] {t.request_nature}")

        # ---- 诉求抽取 ----
        req, issue = extract_request(record.content, t.event_type)
        t.request = req
        t.issue = issue
        logger.info(f"  [诉求] {req[:50] if req else '无'}..., 问题: {issue[:50] if issue else '无'}...")
        if not t.request:
            logger.warning(f"  [诉求] 规则抽取失败，无法识别诉求内容")

        # ---- Semantic Content（套话弱化后生成） ----
        # 先做套话弱化，再组装
        weakened = weaken_boilerplate(t.clean_content, self._boilerplate_re)
        # 用 semantic_content 模板
        t.semantic_content = build_semantic_content(t)
        # 如果 semantic_content 为空，退化为弱化后的文本
        if not t.semantic_content and weakened:
            t.semantic_content = weakened[:200]
        logger.info(f"  [语义内容] 长度: {len(t.semantic_content)}, 套话弱化后: {len(weakened)}")

        # ---- 质量评分 ----
        score, usable, parse_status = compute_quality_score(
            t, self.config.min_quality_score
        )
        t.data_quality_score = round(score, 4)
        t.is_usable_for_duplicate = usable
        t.parse_status = parse_status
        logger.info(f"  [质量评分] 分数: {t.data_quality_score}, 可用: {usable}, 状态: {parse_status}")
        logger.info(f"[工单 {t.ticket_no}] 处理完成")

        return t

    def process_batch(self, records: List[TicketRecord]) -> List[CleanedTicket]:
        """处理一批。"""
        return [self.process(r) for r in records]
