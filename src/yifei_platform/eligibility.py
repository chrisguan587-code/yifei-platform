from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from .market_data import StockDailyFactV1


class MarketSegment(str, Enum):
    MAINBOARD_SH = "mainboard_sh"
    MAINBOARD_SZ = "mainboard_sz"
    CHINEXT = "chinext"
    STAR = "star"
    BSE = "bse"
    OTHER = "other"


class FactState(str, Enum):
    TRUE = "true"
    FALSE = "false"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class EligibilityFactsV1:
    stock_code: str
    as_of: str
    segment: MarketSegment
    st_state: FactState
    delisting_state: FactState
    amount: float | None
    volume: float | None
    turnover: float | None
    market_rules_version: str
    source_version: str
    schema_version: str = "eligibility-facts.v1"


class EligibilityPrimitiveV1:
    """Derive neutral eligibility facts without making an eligibility decision."""

    def __init__(self, *, market_rules_version: str):
        if not market_rules_version.strip():
            raise ValueError("market_rules_version is required")
        self._market_rules_version = market_rules_version

    def evaluate(self, fact: StockDailyFactV1, *, source_version: str) -> EligibilityFactsV1:
        if not source_version.strip():
            raise ValueError("source_version is required")
        return EligibilityFactsV1(
            stock_code=fact.stock_code,
            as_of=fact.trade_date,
            segment=detect_market_segment(fact.stock_code),
            st_state=_fact_state(fact.is_st),
            delisting_state=FactState.UNKNOWN,
            amount=fact.amount,
            volume=fact.volume,
            turnover=fact.turnover,
            market_rules_version=self._market_rules_version,
            source_version=source_version,
        )


def detect_market_segment(stock_code: str) -> MarketSegment:
    code = stock_code.strip()
    if code.startswith(("688", "689")):
        return MarketSegment.STAR
    if code.startswith("60"):
        return MarketSegment.MAINBOARD_SH
    if code.startswith("00"):
        return MarketSegment.MAINBOARD_SZ
    if code.startswith("30"):
        return MarketSegment.CHINEXT
    if code.startswith("8") or code.startswith("920"):
        return MarketSegment.BSE
    return MarketSegment.OTHER


def _fact_state(value: bool | None) -> FactState:
    if value is None:
        return FactState.UNKNOWN
    return FactState.TRUE if value else FactState.FALSE
