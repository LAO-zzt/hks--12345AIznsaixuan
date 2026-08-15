"""从原始工单 xlsx 中随机抽取 10000 条，生成新的测试数据集。

用法:
    .venv/Scripts/python make_dataset.py [n] [seed]
"""
import sys
import os
import pandas as pd

_MOD = os.path.dirname(os.path.abspath(__file__))      # tools
_MOD = os.path.dirname(_MOD)                           # textclean_module
_DATA = os.path.join(_MOD, "data")
_ROOT = os.path.join(os.path.dirname(_MOD), "database", "testdata")
SRC = os.path.join(_DATA, "政数局资料-顺德区12345热线工单（2025年1月至3月）.xlsx")
if not os.path.exists(SRC):
    _alt = os.path.join(_ROOT, "政数局资料-顺德区12345热线工单（2025年1月至3月）.xlsx")
    if os.path.exists(_alt):
        SRC = _alt
OUT_DIR = os.path.join(_DATA, "testdata")
N = int(sys.argv[1]) if len(sys.argv) > 1 else 10000
SEED = int(sys.argv[2]) if len(sys.argv) > 2 else 42

os.makedirs(OUT_DIR, exist_ok=True)

print(f"读取原始数据: {SRC}")
df = pd.read_excel(SRC)
df = df.fillna("")
total = len(df)
print(f"原始数据共 {total} 条")

if total <= N:
    print(f"原始数据不足 {N} 条，全部使用（共 {total} 条）")
    sampled = df
else:
    sampled = df.sample(n=N, random_state=SEED).reset_index(drop=True)

out_name = f"顺德区12345热线工单_测试{N}.xlsx"
out_path = os.path.join(OUT_DIR, out_name)
sampled.to_excel(out_path, index=False)
print(f"已生成数据集: {out_path}  ({len(sampled)} 条)")
