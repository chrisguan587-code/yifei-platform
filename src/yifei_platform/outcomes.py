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
    price_lineage_sources: tuple[str, ...] = ()


@dataclass(frozen=True)
class PriceLineageCheckV1:
    reason_code: str | None
    reference_source: str


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
        current_close: float | None = None,
        current_pct_chg: float | None = None,
    ) -> str | None:
        return self.check(
            previous_close=previous_close,
            current_preclose=current_preclose,
            current_close=current_close,
            current_pct_chg=current_pct_chg,
        ).reason_code

    def check(
        self,
        *,
        previous_close: float | None,
        current_preclose: float | None,
        current_close: float | None = None,
        current_pct_chg: float | None = None,
    ) -> PriceLineageCheckV1:
        if previous_close is None or previous_close <= 0 or current_preclose is None:
            return PriceLineageCheckV1("price_lineage_input_missing", "unavailable")
        difference = abs(current_preclose - previous_close) / previous_close
        if difference > self.tolerance_ratio:
            return PriceLineageCheckV1(
                "corporate_action_or_price_lineage_discontinuity",
                "reported_preclose",
            )
        return PriceLineageCheckV1(None, "reported_preclose")


@dataclass(frozen=True)
class PriceLineageGuardV2(PriceLineageGuardV1):
    rule_version: str = "price-lineage-guard.candidate.v2"

    def check(
        self,
        *,
        previous_close: float | None,
        current_preclose: float | None,
        current_close: float | None = None,
        current_pct_chg: float | None = None,
    ) -> PriceLineageCheckV1:
        if previous_close is None or previous_close <= 0:
            return PriceLineageCheckV1("price_lineage_input_missing", "unavailable")
        if current_preclose is not None and current_preclose > 0:
            reference = current_preclose
            source = "reported_preclose"
        else:
            denominator = (
                1 + current_pct_chg / 100
                if current_pct_chg is not None
                else None
            )
            if (
                current_close is None
                or current_close <= 0
                or denominator is None
                or denominator <= 0
            ):
                return PriceLineageCheckV1(
                    "price_lineage_input_missing", "unavailable"
                )
            reference = current_close / denominator
            source = "implied_from_pct_chg"
        difference = abs(reference - previous_close) / previous_close
        if difference > self.tolerance_ratio:
            return PriceLineageCheckV1(
                "corporate_action_or_price_lineage_discontinuity", source
            )
        return PriceLineageCheckV1(None, source)


class OutcomeCalculatorV1:
    """Calculate neutral post-observation outcomes for a caller-owned sample."""

    def __init__(
        self,
        *,
        calendar: TradingCalendarV1,
        market_data: MarketDataReaderV1,
        price_basis_version: str,
        price_lineage_guard: PriceLineageGuardV1 | PriceLineageGuardV2 | None = None,
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
        lineage_sources: set[str] = set()
        previous_fact = entry_fact
        for offset in range(1, max(normalized_windows) + 1):
            try:
                session = self._calendar.offset_session(observation_session, offset).isoformat()
            except CalendarRangeError:
                break
            if outcome_as_of is not None and session > outcome_as_of:
                break
            current_fact = fact_for(session)
            if self._price_lineage_guard is not None:
                check = self._price_lineage_guard.check(
                    previous_close=previous_fact.close if previous_fact else None,
                    current_preclose=current_fact.preclose if current_fact else None,
                    current_close=current_fact.close if current_fact else None,
                    current_pct_chg=current_fact.pct_chg if current_fact else None,
                )
                if check.reference_source != "unavailable":
                    lineage_sources.add(check.reference_source)
                if check.reason_code is not None:
                    lineage_reasons[offset] = check.reason_code
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
            return self._with_unavailable_metrics(
                instrument, observation_session, entry_price, outcomes,
                "metric_window_unpublished", lineage_sources,
            )
        if outcome_as_of is not None and target_sessions[max_window] > outcome_as_of:
            return self._with_unavailable_metrics(
                instrument, observation_session, entry_price, outcomes,
                "metric_window_pending", lineage_sources,
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
                lineage_sources,
            )
        series: list[StockDailyFactV1] = []
        for offset in range(1, max_window + 1):
            session = self._calendar.offset_session(observation_session, offset).isoformat()
            fact = fact_for(session)
            if fact is None or any(value is None or value <= 0 for value in (fact.high, fact.low, fact.close)):
                return self._with_unavailable_metrics(
                    instrument, observation_session, entry_price, outcomes,
                    "metric_series_incomplete", lineage_sources,
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
            price_lineage_sources=tuple(sorted(lineage_sources)),
        )

    def _with_unavailable_metrics(
        self,
        instrument: str,
        observation_session: str,
        entry_price: float,
        outcomes: list[ForwardOutcomeV1],
        reason: str,
        lineage_sources: set[str],
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
            price_lineage_sources=tuple(sorted(lineage_sources)),
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
