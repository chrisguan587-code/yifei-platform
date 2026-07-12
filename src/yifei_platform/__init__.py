"""Shared market facts and neutral capabilities for Yifei applications."""

from .calendar import CalendarRangeError, TradeDateContextV1, TradingCalendarV1
from .artifacts import (
    ArtifactConflictError,
    ArtifactEnvelopeV1,
    ArtifactIntegrityError,
    ArtifactReceiptV1,
    ArtifactStoreV1,
)
from .market_data import (
    MarketDataReaderV1,
    MarketDataSourceV1,
    ReadStatus,
    StockDailyFactV1,
    StockDailyReadResultV1,
)
from .quality import DataQualitySnapshotV1, DatasetQualityV1, QualityStatus
from .readiness import (
    DataNotReadyError,
    ReadinessConflictError,
    ReadinessIntegrityError,
    ReadinessMarkerV1,
    ReadinessStoreV1,
)
from .outcomes import ForwardOutcomeV1, OutcomeCalculatorV1, OutcomeResultV1, OutcomeStatus

__version__ = "0.1.0"

__all__ = [
    "CalendarRangeError",
    "ArtifactConflictError",
    "ArtifactEnvelopeV1",
    "ArtifactIntegrityError",
    "ArtifactReceiptV1",
    "ArtifactStoreV1",
    "DataNotReadyError",
    "DataQualitySnapshotV1",
    "DatasetQualityV1",
    "MarketDataReaderV1",
    "MarketDataSourceV1",
    "ForwardOutcomeV1",
    "OutcomeCalculatorV1",
    "OutcomeResultV1",
    "OutcomeStatus",
    "ReadStatus",
    "ReadinessConflictError",
    "ReadinessIntegrityError",
    "ReadinessMarkerV1",
    "ReadinessStoreV1",
    "QualityStatus",
    "StockDailyFactV1",
    "StockDailyReadResultV1",
    "TradeDateContextV1",
    "TradingCalendarV1",
    "__version__",
]
