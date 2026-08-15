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
    推送 Top-N 多频事件到飞书群机器人。

    返回 (ok: bool, message: str)。
    """
    webhook = webhook if webhook else config.FEISHU_WEBHOOK
    if not webhook:
        return True, "未配置飞书 Webhook，已跳过推送。"

    ordered = sorted(events, key=lambda e: -e["frequency"])
    top = ordered[:top_n]
    if not top:
        return True, "暂无多频事件，无需推送。"

    lines = ["【12345 多频工单识别】系统识别到以下 Top%d 多频事件：" % top_n, ""]
    for ev in top:
        lines.append(
            "▪ {eid} {subj}｜{etype}\n"
            "  区域：{area}｜频次：{freq}\n"
            "  首次：{first}｜最近：{last}\n"
            "  样例工单：{samples}".format(
                eid=ev["event_id"],
                subj=ev["event_subject"],
                etype=ev["event_type"],
                area=ev["area"],
                freq=ev["frequency"],
                first=ev["first_seen"],
                last=ev["last_seen"],
                samples="、".join(s["order_id"] for s in ev.get("sample_orders", [])[:2]),
            )
        )

    payload = {"msg_type": "text", "content": {"text": "\n".join(lines)}}
    try:
        resp = requests.post(webhook, json=payload, timeout=8)
        if resp.status_code == 200:
            return True, "已推送 Top%d 多频事件到飞书。" % len(top)
        return False, "飞书推送失败（HTTP %d），不影响结果展示与下载。" % resp.status_code
    except Exception as e:
        return False, "飞书推送失败（%s），不影响结果展示与下载。" % e
