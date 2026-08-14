# -*- coding: utf-8 -*-
"""
模块 10：可选飞书推送（feishu_pusher.py）

定位：证明“识别结果 → 自动同步 → 进入管理人员工作流”的落地潜力。

规则：
- Webhook 为空时直接跳过（不报错）；
- 网络失败只返回警告信息，不阻塞主流程；
- 不在代码中写死真实密钥（由环境变量或页面输入提供）。
"""
import requests

import config


def push_top_events(events: list, webhook: str = None, top_n: int = 5):
    """
    推送 Top-N 高关注事件到飞书群机器人。

    返回 (ok: bool, message: str)。
    """
    webhook = webhook if webhook else config.FEISHU_WEBHOOK
    if not webhook:
        return True, "未配置飞书 Webhook，已跳过推送。"

    # 优先推送高关注事件，不足则按优先级分数补齐
    ordered = sorted(
        events,
        key=lambda e: (0 if e.get("risk_level") == "高关注" else 1,
                       -float(e.get("priority_score", 0) or 0)),
    )
    top = ordered[:top_n]
    if not top:
        return True, "暂无高频事件，无需推送。"

    lines = ["【12345 高频事件预警】系统识别到以下重点关注事件：", ""]
    for ev in top:
        lines.append(
            "▪ {eid} {subj}｜{etype}\n"
            "  区域：{area}｜频次：{freq}（近7天 {d7}）｜趋势：{trend}\n"
            "  风险等级：{level}（{score}分）\n"
            "  风险原因：{reason}\n"
            "  建议：{dept} → {action}\n"
            "  样例工单：{samples}".format(
                eid=ev["event_id"],
                subj=ev["event_subject"],
                etype=ev["event_type"],
                area=ev["area"],
                freq=ev["frequency"],
                d7=ev.get("last_7d", 0),
                trend=ev["trend"],
                level=ev.get("risk_level", ""),
                score=ev.get("priority_score", ""),
                reason=ev.get("risk_reason", ""),
                dept=ev.get("action_department", ""),
                action=ev.get("action_advice", ""),
                samples="、".join(s["order_id"] for s in ev.get("sample_orders", [])[:2]),
            )
        )

    payload = {"msg_type": "text", "content": {"text": "\n".join(lines)}}
    try:
        resp = requests.post(webhook, json=payload, timeout=8)
        if resp.status_code == 200:
            return True, "已推送 Top%d 高关注事件到飞书。" % len(top)
        return False, "飞书推送失败（HTTP %d），不影响结果展示与下载。" % resp.status_code
    except Exception as e:
        return False, "飞书推送失败（%s），不影响结果展示与下载。" % e
