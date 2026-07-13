"""Shared market facts and neutral capabilities for Yifei applications."""

from .bootstrap import (
    BOOTSTRAP_VERSION,
    TRANSITIONAL_DAILY_VERSION,
    BootstrapResult,
    bootstrap_market_data,
    load_market_metadata,
    load_trading_sessions,
    publish_transitional_daily_market_data,
)
from .calendar import CalendarRangeError, TradeDateContextV1, TradingCalendarV1
from .board_capital import (
    BoardDailyFactV1,
    BoardFactReaderV1,
    CapitalFactReaderV1,
    FactReadResultV1,
    SectorCapitalFactV1,
)
from .eligibility import (
    HISTORICAL_ST_RULE_VERSION,
    EligibilityFactsV1,
    EligibilityPrimitiveV1,
    FactState,
    HistoricalStFactV1,
    MarketSegment,
    derive_historical_st_v1,
)
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

__version__ = "0.3.0"

__all__ = [
    "BOOTSTRAP_VERSION",
    "BootstrapResult",
    "TRANSITIONAL_DAILY_VERSION",
    "CalendarRangeError",
    "BoardDailyFactV1",
    "BoardFactReaderV1",
    "CapitalFactReaderV1",
    "EligibilityFactsV1",
    "EligibilityPrimitiveV1",
    "FactReadResultV1",
    "FactState",
    "HISTORICAL_ST_RULE_VERSION",
    "HistoricalStFactV1",
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
    "derive_historical_st_v1",
    "load_market_metadata",
    "load_trading_sessions",
    "publish_transitional_daily_market_data",
    "__version__",
]
