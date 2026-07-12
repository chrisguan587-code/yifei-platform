from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from .calendar import CalendarRangeError, TradingCalendarV1
from .market_data import MarketDataReaderV1, ReadStatus, StockDailyFactV1


class OutcomeStatus(str, Enum):
    COMPLETE = "complete"
    UNAVAILABLE = "unavailable"


@dataclass(frozen=True)
class ForwardOutcomeV1:
    window: int
    target_session: str | None
    status: OutcomeStatus
    return_pct: float | None
    reason_codes: tuple[str, ...] = ()


@dataclass(frozen=True)
class OutcomeResultV1:
    instrument: str
    observation_session: str
    entry_price: float | None
    price_basis_version: str
    windows: tuple[ForwardOutcomeV1, ...]
    metric_end_session: str | None
    mfe_pct: float | None
    mae_pct: float | None
    max_drawdown_pct: float | None
    reason_codes: tuple[str, ...]
    schema_version: str = "outcome-calculator.v1"


class OutcomeCalculatorV1:
    """Calculate neutral post-observation outcomes for a caller-owned sample."""

    def __init__(
        self,
        *,
        calendar: TradingCalendarV1,
        market_data: MarketDataReaderV1,
        price_basis_version: str,
    ):
        if not price_basis_version.strip():
            raise ValueError("price_basis_version is required")
        self._calendar = calendar
        self._market_data = market_data
        self._price_basis_version = price_basis_version

    def calculate(
        self,
        *,
        instrument: str,
        observation_session: str,
        windows: tuple[int, ...] = (1, 3, 5),
    ) -> OutcomeResultV1:
        normalized_windows = tuple(sorted(set(windows)))
        if not instrument.strip():
            raise ValueError("instrument is required")
        if not normalized_windows or normalized_windows[0] <= 0:
            raise ValueError("windows must contain positive trading-session offsets")
        if not self._calendar.is_session(observation_session):
            raise ValueError("observation_session must be an exact published trading session")

        entry_fact = self._fact(instrument, observation_session)
        entry_price = entry_fact.close if entry_fact else None
        if entry_price is None or entry_price <= 0:
            return OutcomeResultV1(
                instrument=instrument,
                observation_session=observation_session,
                entry_price=None,
                price_basis_version=self._price_basis_version,
                windows=tuple(
                    ForwardOutcomeV1(window, None, OutcomeStatus.UNAVAILABLE, None, ("entry_close_missing",))
                    for window in normalized_windows
                ),
                metric_end_session=None,
                mfe_pct=None,
                mae_pct=None,
                max_drawdown_pct=None,
                reason_codes=("entry_close_missing",),
            )

        outcomes: list[ForwardOutcomeV1] = []
        target_sessions: dict[int, str] = {}
        for window in normalized_windows:
            try:
                target = self._calendar.offset_session(observation_session, window).isoformat()
            except CalendarRangeError:
                outcomes.append(
                    ForwardOutcomeV1(window, None, OutcomeStatus.UNAVAILABLE, None, ("target_session_unpublished",))
                )
                continue
            target_sessions[window] = target
            fact = self._fact(instrument, target)
            if fact is None or fact.close is None or fact.close <= 0:
                outcomes.append(
                    ForwardOutcomeV1(window, target, OutcomeStatus.UNAVAILABLE, None, ("target_close_missing",))
                )
                continue
            outcomes.append(
                ForwardOutcomeV1(
                    window,
                    target,
                    OutcomeStatus.COMPLETE,
                    round((fact.close - entry_price) / entry_price * 100, 4),
                )
            )

        max_window = max(target_sessions, default=None)
        if max_window is None:
            return self._with_unavailable_metrics(instrument, observation_session, entry_price, outcomes, "metric_window_unpublished")
        series: list[StockDailyFactV1] = []
        for offset in range(1, max_window + 1):
            session = self._calendar.offset_session(observation_session, offset).isoformat()
            fact = self._fact(instrument, session)
            if fact is None or any(value is None or value <= 0 for value in (fact.high, fact.low, fact.close)):
                return self._with_unavailable_metrics(
                    instrument, observation_session, entry_price, outcomes, "metric_series_incomplete"
                )
            series.append(fact)

        highs = [fact.high for fact in series if fact.high is not None]
        lows = [fact.low for fact in series if fact.low is not None]
        closes = [fact.close for fact in series if fact.close is not None]
        peak_close = entry_price
        max_drawdown = 0.0
        for close in closes:
            peak_close = max(peak_close, close)
            max_drawdown = min(max_drawdown, (close - peak_close) / peak_close * 100)
        return OutcomeResultV1(
            instrument=instrument,
            observation_session=observation_session,
            entry_price=entry_price,
            price_basis_version=self._price_basis_version,
            windows=tuple(outcomes),
            metric_end_session=target_sessions[max_window],
            mfe_pct=round((max(highs) - entry_price) / entry_price * 100, 4),
            mae_pct=round((min(lows) - entry_price) / entry_price * 100, 4),
            max_drawdown_pct=round(max_drawdown, 4),
            reason_codes=(),
        )

    def _with_unavailable_metrics(
        self,
        instrument: str,
        observation_session: str,
        entry_price: float,
        outcomes: list[ForwardOutcomeV1],
        reason: str,
    ) -> OutcomeResultV1:
        return OutcomeResultV1(
            instrument=instrument,
            observation_session=observation_session,
            entry_price=entry_price,
            price_basis_version=self._price_basis_version,
            windows=tuple(outcomes),
            metric_end_session=None,
            mfe_pct=None,
            mae_pct=None,
            max_drawdown_pct=None,
            reason_codes=(reason,),
        )

    def _fact(self, instrument: str, session: str) -> StockDailyFactV1 | None:
        result = self._market_data.read_stock_daily(session)
        if result.status is not ReadStatus.OK:
            return None
        return next((fact for fact in result.facts if fact.stock_code == instrument), None)
