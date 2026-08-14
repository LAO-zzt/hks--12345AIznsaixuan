# -*- coding: utf-8 -*-
"""
Streamlit UI 无头验证（ui_test.py）

使用官方 streamlit.testing.v1.AppTest 框架，模拟用户操作：
打开页面 → 点击“开始分析”（自动加载内置样例）→ 校验 KPI/看板/下载区渲染。

用法：python ui_test.py
"""
import os
import sys

if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from streamlit.testing.v1 import AppTest


def main():
    app = os.path.join(os.path.dirname(os.path.abspath(__file__)), "main.py")
    at = AppTest.from_file(app, default_timeout=120)

    print("[1] 首次渲染页面…")
    at.run()
    assert not at.exception, "页面渲染异常：%s" % [str(e.value) for e in at.exception]
    print("    OK，标题：%s" % at.title[0].value)

    # 找到“开始分析”按钮并点击
    run_btn = None
    for b in at.button:
        if b.label == "开始分析":
            run_btn = b
            break
    assert run_btn is not None, "未找到“开始分析”按钮"

    print("[2] 点击“开始分析”（自动加载内置样例数据）…")
    run_btn.click()
    at.run()
    assert not at.exception, "分析过程异常：%s" % [str(e.value) for e in at.exception]

    # 校验成功提示
    success_texts = [s.value for s in at.success]
    print("    成功提示：%s" % success_texts)
    assert any("识别完成" in t for t in success_texts), "未出现识别完成提示"

    # 校验 KPI
    metrics = {m.label: str(m.value) for m in at.metric}
    print("    KPI：%s" % metrics)
    assert metrics.get("工单总量") == "37", "工单总量应为37，实际 %s" % metrics.get("工单总量")
    assert metrics.get("多频事件数") == "5", "多频事件数应为5，实际 %s" % metrics.get("多频事件数")
    assert metrics.get("高关注事件数") == "1", "高关注事件数应为1，实际 %s" % metrics.get("高关注事件数")

    # 校验下载按钮存在
    dl_labels = [d.label for d in at.download_button]
    print("    下载按钮：%s" % dl_labels)
    assert any("CSV" in l for l in dl_labels), "缺少 CSV 下载按钮"
    assert any("Excel" in l for l in dl_labels), "缺少 Excel 下载按钮"

    print("\nUI 无头验证通过：页面可渲染、一键分析可用、KPI 与下载区正常。")


if __name__ == "__main__":
    main()
