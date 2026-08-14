# -*- coding: utf-8 -*-
"""
LLM 处置建议生成器（llm_advisor.py）

对规则词典未命中的事件类型，调用 DeepSeek 批量生成
“建议关注部门 + 建议动作”，作为规则兜底之上的 AI 增强层。

设计原则：
- 规则词典优先（离线、可解释、零成本），LLM 只处理未命中部分；
- 批量调用（一次最多 30 个事件类型），控制成本与延迟；
- API Key 通过运行时参数传入或环境变量 DEEPSEEK_API_KEY，绝不写死；
- 任何调用失败都静默降级回“需人工研判”，不影响主流程。
"""
import json
import re

import requests

API_URL = "https://api.deepseek.com/chat/completions"
BATCH_SIZE = 30

PROMPT_TEMPLATE = """你是 12345 政务热线的工单分派专家。以下是一批市民诉求事件类型（可能附带一条样例内容），请为每个事件给出：
1. department：建议关注部门（如：城管/住建/市场监管/人社/交警/生态环境/卫健/消防等，可组合，不超过15字）
2. advice：建议动作（具体可执行，不超过30字）

事件列表：
{events}

只输出 JSON 数组，格式：[{{"type":"事件类型","department":"部门","advice":"动作"}}]，不要输出其他内容。"""


def _build_event_lines(unmatched: list) -> str:
    """把未匹配事件拼成编号列表（含样例内容，帮助 LLM 理解语境）。"""
    lines = []
    for i, ev in enumerate(unmatched, 1):
        etype = ev.get("event_type", "")
        samples = ev.get("sample_orders") or []
        sample_text = samples[0]["content"][:60] if samples else ""
        if sample_text:
            lines.append("%d. %s（样例：%s）" % (i, etype, sample_text))
        else:
            lines.append("%d. %s" % (i, etype))
    return "\n".join(lines)


def _parse_llm_json(text: str) -> list:
    """稳健解析 LLM 返回的 JSON 数组（容忍代码块包裹与前后杂质）。"""
    text = text.strip()
    m = re.search(r"\[[\s\S]*\]", text)
    if m:
        text = m.group(0)
    try:
        data = json.loads(text)
        return data if isinstance(data, list) else []
    except Exception:
        return []


def llm_advise(unmatched: list, api_key: str, timeout: int = 60) -> dict:
    """
    批量生成处置建议。

    unmatched: 事件字典列表（需含 event_type，可含 sample_orders）
    返回 {事件类型: {"department": ..., "advice": ...}}；失败返回空字典。
    """
    if not api_key or not unmatched:
        return {}

    result = {}
    for start in range(0, len(unmatched), BATCH_SIZE):
        batch = unmatched[start:start + BATCH_SIZE]
        prompt = PROMPT_TEMPLATE.format(events=_build_event_lines(batch))
        try:
            resp = requests.post(
                API_URL,
                headers={"Authorization": "Bearer %s" % api_key,
                         "Content-Type": "application/json"},
                json={
                    "model": "deepseek-chat",
                    "messages": [{"role": "user", "content": prompt}],
                    "temperature": 0.3,
                    "max_tokens": 2000,
                },
                timeout=timeout,
            )
            if resp.status_code != 200:
                continue
            content = resp.json()["choices"][0]["message"]["content"]
            for item in _parse_llm_json(content):
                t = str(item.get("type", "")).strip()
                dept = str(item.get("department", "")).strip()
                adv = str(item.get("advice", "")).strip()
                if t and dept and adv:
                    result[t] = {"department": dept[:20], "advice": adv[:40]}
        except Exception:
            # 单批失败不影响其他批次与主流程
            continue
    return result
