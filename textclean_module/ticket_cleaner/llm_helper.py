"""LLM辅助判断模块。

用于主体识别验证和语义分析。
"""

from __future__ import annotations

import json
import os
from typing import Dict, List, Optional, Tuple
from urllib.parse import urlencode
from urllib.request import urlopen, Request
from urllib.error import URLError, HTTPError


def _get_llm_config():
    """获取LLM配置"""
    try:
        from ticket_cleaner.config_loader import config
        return config.get_llm_config()
    except Exception:
        return {}


def _get_llm_params():
    """获取LLM参数"""
    llm_config = _get_llm_config()
    mode = llm_config.get('mode', 'ollama')
    
    if mode == 'openai':
        openai_cfg = llm_config.get('openai', {})
        return {
            'api_url': openai_cfg.get('api_url', 'https://api.openai.com/v1/chat/completions'),
            'api_key': openai_cfg.get('api_key', ''),
            'model': openai_cfg.get('model', 'gpt-3.5-turbo'),
            'temperature': llm_config.get('temperature', 0.3),
            'max_tokens': llm_config.get('max_tokens', 500),
            'timeout': llm_config.get('timeout', 30),
        }
    else:  # ollama
        ollama_cfg = llm_config.get('ollama', {})
        return {
            'api_url': ollama_cfg.get('api_url', 'http://localhost:11434/api/chat'),
            'api_key': '',
            'model': ollama_cfg.get('model', 'qwen2.5:7b'),
            'temperature': llm_config.get('temperature', 0.3),
            'max_tokens': llm_config.get('max_tokens', 500),
            'timeout': llm_config.get('timeout', 30),
        }


def call_llm(prompt: str, system_prompt: str = "") -> Optional[str]:
    """调用LLM API。支持Ollama和OpenAI格式。"""
    params = _get_llm_params()
    # 根据配置选择调用方式
    if "ollama" in params['api_url'] or not params['api_key']:
        return _call_ollama(prompt, system_prompt, params)
    # OpenAI格式（云端）
    return _call_openai(prompt, system_prompt, params)


def _call_ollama(prompt: str, system_prompt: str = "", params: Dict = None) -> Optional[str]:
    """调用Ollama API。"""
    if params is None:
        params = _get_llm_params()
    
    messages = []
    if system_prompt:
        messages.append({"role": "system", "content": system_prompt})
    messages.append({"role": "user", "content": prompt})

    data = {
        "model": params['model'],
        "messages": messages,
        "stream": False,
        "options": {
            "temperature": params['temperature'],
        }
    }

    try:
        req = Request(
            params['api_url'],
            data=json.dumps(data).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST"
        )
        with urlopen(req, timeout=params['timeout']) as response:
            result = json.loads(response.read().decode("utf-8"))
            if "message" in result:
                return result["message"]["content"].strip()
    except (URLError, HTTPError, json.JSONDecodeError, KeyError):
        pass

    return None


def _call_openai(prompt: str, system_prompt: str = "", params: Dict = None) -> Optional[str]:
    """调用OpenAI API。"""
    if params is None:
        params = _get_llm_params()
    
    if not params['api_key']:
        return None

    messages = []
    if system_prompt:
        messages.append({"role": "system", "content": system_prompt})
    messages.append({"role": "user", "content": prompt})

    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {params['api_key']}",
    }

    data = {
        "model": params['model'],
        "messages": messages,
        "temperature": params['temperature'],
        "max_tokens": params['max_tokens'],
    }

    try:
        req = Request(
            params['api_url'],
            data=json.dumps(data).encode("utf-8"),
            headers=headers,
            method="POST"
        )
        with urlopen(req, timeout=params['timeout']) as response:
            result = json.loads(response.read().decode("utf-8"))
            if "choices" in result and result["choices"]:
                return result["choices"][0]["message"]["content"].strip()
    except (URLError, HTTPError, json.JSONDecodeError, KeyError):
        pass

    return None


def analyze_entity_with_llm(entity_name: str, context: str = "") -> Dict[str, any]:
    """使用LLM分析实体信息。

    返回：
        {
            "is_valid": bool,  # 是否为有效实体
            "normalized_name": str,  # 标准化名称
            "entity_type": str,  # 实体类型（企业/小区/学校等）
            "confidence": float,  # 置信度
        }
    """
    system_prompt = """你是一个专业的实体识别和标准化助手。你需要分析从12345工单中提取的实体（企业、小区、学校、医院等），判断其是否为有效实体，并给出标准化名称。

请严格按照以下JSON格式返回结果：
{
    "is_valid": true/false,
    "normalized_name": "标准化名称",
    "entity_type": "企业/小区/学校/医院/物业/商铺/部门/场所/未知",
    "confidence": 0.0-1.0
}"""

    prompt = f"请分析以下实体：\n\n实体名称：{entity_name}"
    if context:
        prompt += f"\n上下文：{context}"

    response = call_llm(prompt, system_prompt)
    if not response:
        return {
            "is_valid": False,
            "normalized_name": entity_name,
            "entity_type": "未知",
            "confidence": 0.0,
        }

    try:
        # 尝试解析JSON
        result = json.loads(response)
        return {
            "is_valid": result.get("is_valid", False),
            "normalized_name": result.get("normalized_name", entity_name),
            "entity_type": result.get("entity_type", "未知"),
            "confidence": float(result.get("confidence", 0.0)),
        }
    except (json.JSONDecodeError, ValueError, TypeError):
        # 解析失败，返回默认值
        return {
            "is_valid": False,
            "normalized_name": entity_name,
            "entity_type": "未知",
            "confidence": 0.0,
        }


def verify_and_normalize_entity(
    entity_name: str,
    context: str = "",
    use_gaode: bool = True
) -> Tuple[str, str, float]:
    """验证并标准化实体。

    流程：
    1. 先查高德地图缓存
    2. 未命中则调用LLM分析

    返回：(normalized_name, entity_type, confidence)
    """
    from ticket_cleaner.gaode_cache import verify_entity_in_gaode

    # 1. 先查高德地图
    if use_gaode and verify_entity_in_gaode(entity_name):
        # 高德验证通过，返回原名
        return entity_name, "企业", 0.9

    # 2. 高德未命中，调用LLM分析
    result = analyze_entity_with_llm(entity_name, context)
    if result["is_valid"]:
        return (
            result["normalized_name"],
            result["entity_type"],
            result["confidence"],
        )

    # 3. LLM判断无效，返回原名但低置信度
    return entity_name, "未知", 0.3


def classify_event_with_llm(text: str, title: str = "") -> Dict[str, str]:
    """使用LLM辅助事件分类。

    返回：
        {
            "event_type": str,  # 事件类型
            "confidence": float,  # 置信度
        }
    """
    system_prompt = """你是一个专业的12345工单事件分类助手。请根据工单标题和内容，判断事件类型。

事件类型包括：噪音扰民、拖欠工资、劳动纠纷、消费纠纷、占道经营、环境卫生、违法建设、交通问题、市政设施、燃放烟花、环境污染、物业管理、养殖问题、无证经营、其他。

请严格按照以下JSON格式返回：
{
    "event_type": "事件类型",
    "confidence": 0.0-1.0
}"""

    prompt = f"工单标题：{title}\n工单内容：{text[:500]}"
    response = call_llm(prompt, system_prompt)

    if not response:
        return {"event_type": "", "confidence": 0.0}

    try:
        result = json.loads(response)
        return {
            "event_type": result.get("event_type", ""),
            "confidence": float(result.get("confidence", 0.0)),
        }
    except (json.JSONDecodeError, ValueError, TypeError):
        return {"event_type": "", "confidence": 0.0}


def extract_request_with_llm(text: str) -> Dict[str, str]:
    """使用LLM辅助诉求抽取。

    返回：
        {
            "request": str,  # 核心诉求
            "issue": str,  # 问题简述
            "confidence": float,
        }
    """
    system_prompt = """你是一个专业的12345工单诉求抽取助手。请从工单内容中提取市民的核心诉求和问题简述。

请严格按照以下JSON格式返回：
{
    "request": "市民的核心诉求（如：要求退款、希望处理噪音等）",
    "issue": "问题简述（如：商家拒绝退款、夜间施工噪音等）",
    "confidence": 0.0-1.0
}"""

    prompt = f"工单内容：{text[:500]}"
    response = call_llm(prompt, system_prompt)

    if not response:
        return {"request": "", "issue": "", "confidence": 0.0}

    try:
        result = json.loads(response)
        return {
            "request": result.get("request", ""),
            "issue": result.get("issue", ""),
            "confidence": float(result.get("confidence", 0.0)),
        }
    except (json.JSONDecodeError, ValueError, TypeError):
        return {"request": "", "issue": "", "confidence": 0.0}
