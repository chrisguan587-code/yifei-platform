"""Shared market facts and neutral capabilities for Yifei applications."""

from .calendar import CalendarRangeError, TradeDateContextV1, TradingCalendarV1
from .market_data import (
    MarketDataReaderV1,
    MarketDataSourceV1,
    ReadStatus,
    StockDailyFactV1,
    StockDailyReadResultV1,
)

__version__ = "0.1.0"

__all__ = [
    "CalendarRangeError",
    "MarketDataReaderV1",
    "MarketDataSourceV1",
    "ReadStatus",
    "StockDailyFactV1",
    "StockDailyReadResultV1",
    "TradeDateContextV1",
    "TradingCalendarV1",
    "__version__",
]
