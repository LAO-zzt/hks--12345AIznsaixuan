"""数据读取器。

支持Excel读取，并支持切片读取以适配Batch处理。
"""

from __future__ import annotations

import os
from typing import Iterator, List, Optional

import pandas as pd

from ticket_cleaner.schema import TicketRecord, TicketSchema


class ExcelReader:
    """读取12345工单Excel。"""

    def __init__(self, file_path: str, sheet_name: str = "Sheet1") -> None:
        if not os.path.exists(file_path):
            raise FileNotFoundError(f"数据文件不存在: {file_path}")
        self.file_path = file_path
        self.sheet_name = sheet_name
        self._df: Optional[pd.DataFrame] = None

    def _load(self) -> pd.DataFrame:
        if self._df is None:
            self._df = pd.read_excel(self.file_path, sheet_name=self.sheet_name)
            # 把所有NaN转为空串
            self._df = self._df.fillna("")
        return self._df

    def count(self) -> int:
        return len(self._load())

    def read_range(self, start: int, end: int) -> List[TicketRecord]:
        """读取 [start, end) 的记录，0-based。"""
        df = self._load()
        n = len(df)
        if start >= n:
            return []
        end = min(end, n)
        sub = df.iloc[start:end]
        records: List[TicketRecord] = []
        for _, row in sub.iterrows():
            row_dict = {k: row[k] for k in row.index}
            records.append(TicketSchema.from_row(row_dict))
        return records

    def iter_batches(self, batch_size: int
                     ) -> Iterator[tuple[int, int, List[TicketRecord]]]:
        """迭代所有批次：yield (start, end, records)。"""
        df = self._load()
        n = len(df)
        for start in range(0, n, batch_size):
            end = min(start + batch_size, n)
            yield start, end, self.read_range(start, end)
