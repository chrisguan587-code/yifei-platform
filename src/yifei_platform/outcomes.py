from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from enum import Enum

from .calendar import CalendarRangeError, TradingCalendarV1
from .market_data import MarketDataReaderV1, ReadStatus, StockDailyFactV1


class OutcomeStatus(str, Enum):
    COMPLETE = "complete"
    PENDING = "pending"
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
    price_lineage_rule_version: str | None = None
    schema_version: str = "outcome-calculator.v1"


@dataclass(frozen=True)
class PriceLineageGuardV1:
    tolerance_ratio: float = 0.001
    rule_version: str = "price-lineage-guard.candidate.v1"

    def __post_init__(self) -> None:
        if self.tolerance_ratio < 0:
            raise ValueError("price lineage tolerance must be non-negative")

    def discontinuity_reason(
        self,
        *,
        previous_close: float | None,
        current_preclose: float | None,
    ) -> str | None:
        if previous_close is None or previous_close <= 0 or current_preclose is None:
            return "price_lineage_input_missing"
        difference = abs(current_preclose - previous_close) / previous_close
        if difference > self.tolerance_ratio:
            return "corporate_action_or_price_lineage_discontinuity"
        return None


class OutcomeCalculatorV1:
    """Calculate neutral post-observation outcomes for a caller-owned sample."""

    def __init__(
        self,
        *,
        calendar: TradingCalendarV1,
        market_data: MarketDataReaderV1,
        price_basis_version: str,
        price_lineage_guard: PriceLineageGuardV1 | None = None,
    ):
        if not price_basis_version.strip():
            raise ValueError("price_basis_version is required")
        self._calendar = calendar
        self._market_data = market_data
        self._price_basis_version = price_basis_version
        self._price_lineage_guard = price_lineage_guard

    def calculate(
        self,
        *,
        instrument: str,
        observation_session: str,
        windows: tuple[int, ...] = (1, 3, 5),
        outcome_as_of: str | None = None,
    ) -> OutcomeResultV1:
        normalized_windows = tuple(sorted(set(windows)))
        if not instrument.strip():
            raise ValueError("instrument is required")
        if not normalized_windows or normalized_windows[0] <= 0:
            raise ValueError("windows must contain positive trading-session offsets")
        if not self._calendar.is_session(observation_session):
            raise ValueError("observation_session must be an exact published trading session")
        if outcome_as_of is not None:
            if date.fromisoformat(outcome_as_of) < date.fromisoformat(observation_session):
                raise ValueError("outcome_as_of cannot predate observation_session")

        entry_fact = self._fact(instrument, observation_session)
        fact_cache = {observation_session: entry_fact}

        def fact_for(session: str) -> StockDailyFactV1 | None:
            if session not in fact_cache:
                fact_cache[session] = self._fact(instrument, session)
            return fact_cache[session]

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
                price_lineage_rule_version=self._lineage_version,
            )

        outcomes: list[ForwardOutcomeV1] = []
        target_sessions: dict[int, str] = {}
        lineage_reasons: dict[int, str] = {}
        previous_fact = entry_fact
        for offset in range(1, max(normalized_windows) + 1):
            try:
                session = self._calendar.offset_session(observation_session, offset).isoformat()
            except CalendarRangeError:
                break
            current_fact = fact_for(session)
            if self._price_lineage_guard is not None:
                reason = self._price_lineage_guard.discontinuity_reason(
                    previous_close=previous_fact.close if previous_fact else None,
                    current_preclose=current_fact.preclose if current_fact else None,
                )
                if reason is not None:
                    lineage_reasons[offset] = reason
            previous_fact = current_fact
        for window in normalized_windows:
            try:
                target = self._calendar.offset_session(observation_session, window).isoformat()
            except CalendarRangeError:
                status = (
                    OutcomeStatus.PENDING
                    if outcome_as_of is not None
                    else OutcomeStatus.UNAVAILABLE
                )
                outcomes.append(
                    ForwardOutcomeV1(
                        window,
                        None,
                        status,
                        None,
                        (
                            "target_session_pending"
                            if status is OutcomeStatus.PENDING
                            else "target_session_unpublished",
                        ),
                    )
                )
                continue
            target_sessions[window] = target
            if outcome_as_of is not None and target > outcome_as_of:
                outcomes.append(
                    ForwardOutcomeV1(
                        window,
                        target,
                        OutcomeStatus.PENDING,
                        None,
                        ("target_session_pending",),
                    )
                )
                continue
            lineage_reason = next(
                (
                    lineage_reasons[offset]
                    for offset in range(1, window + 1)
                    if offset in lineage_reasons
                ),
                None,
            )
            if lineage_reason is not None:
                outcomes.append(
                    ForwardOutcomeV1(
                        window,
                        target,
                        OutcomeStatus.UNAVAILABLE,
                        None,
                        (lineage_reason,),
                    )
                )
                continue
            fact = fact_for(target)
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
        if outcome_as_of is not None and target_sessions[max_window] > outcome_as_of:
            return self._with_unavailable_metrics(
                instrument, observation_session, entry_price, outcomes, "metric_window_pending"
            )
        metric_lineage_reason = next(
            (
                lineage_reasons[offset]
                for offset in range(1, max_window + 1)
                if offset in lineage_reasons
            ),
            None,
        )
        if metric_lineage_reason is not None:
            return self._with_unavailable_metrics(
                instrument,
                observation_session,
                entry_price,
                outcomes,
                metric_lineage_reason,
            )
        series: list[StockDailyFactV1] = []
        for offset in range(1, max_window + 1):
            session = self._calendar.offset_session(observation_session, offset).isoformat()
            fact = fact_for(session)
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
            price_lineage_rule_version=self._lineage_version,
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
            price_lineage_rule_version=self._lineage_version,
        )

    @property
    def _lineage_version(self) -> str | None:
        return (
            self._price_lineage_guard.rule_version
            if self._price_lineage_guard is not None
            else None
        )

    def _fact(self, instrument: str, session: str) -> StockDailyFactV1 | None:
        result = self._market_data.read_stock_daily(session)
        if result.status is not ReadStatus.OK:
            return None
        return next((fact for fact in result.facts if fact.stock_code == instrument), None)
