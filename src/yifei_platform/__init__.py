"""Shared market facts and neutral capabilities for Yifei applications."""

from .bootstrap import (
    BOOTSTRAP_VERSION,
    BootstrapResult,
    bootstrap_market_data,
    load_trading_sessions,
)
from .calendar import CalendarRangeError, TradeDateContextV1, TradingCalendarV1
from .board_capital import (
    BoardDailyFactV1,
    BoardFactReaderV1,
    CapitalFactReaderV1,
    FactReadResultV1,
    SectorCapitalFactV1,
)
from .eligibility import EligibilityFactsV1, EligibilityPrimitiveV1, FactState, MarketSegment
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

__version__ = "0.2.0"

__all__ = [
    "BOOTSTRAP_VERSION",
    "BootstrapResult",
    "CalendarRangeError",
    "BoardDailyFactV1",
    "BoardFactReaderV1",
    "CapitalFactReaderV1",
    "EligibilityFactsV1",
    "EligibilityPrimitiveV1",
    "FactReadResultV1",
    "FactState",
    "MarketSegment",
    "SectorCapitalFactV1",
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
    "bootstrap_market_data",
    "load_trading_sessions",
    "__version__",
]
