"""12345热线AI工单数据清洗与标准化模块。

将原始12345自然语言工单转换为可被AI跨工单比较、聚类和重复识别的结构化业务数据。
"""

from ticket_cleaner.schema import TicketRecord, CleanedTicket, TicketSchema
from ticket_cleaner.config import Config
from ticket_cleaner.pipeline import CleaningPipeline
from ticket_cleaner.batch_engine import BatchEngine

__version__ = "1.0.0"
__pipeline_version__ = "clean-v1.0"

__all__ = [
    "TicketRecord",
    "CleanedTicket",
    "TicketSchema",
    "Config",
    "CleaningPipeline",
    "BatchEngine",
    "__version__",
    "__pipeline_version__",
]
