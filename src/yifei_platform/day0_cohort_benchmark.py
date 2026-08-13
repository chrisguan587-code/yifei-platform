from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import math
from statistics import median

from .calendar import TradingCalendarV1
from .market_data import MarketDataReaderV1


DAY0_COHORT_BENCHMARK_VERSION = "a-share-day0-cohort-median-return.v0.1"
DAY0_COHORT_PRICE_BASIS_VERSION = "pit-official-pct-chain.v0.1"
DAY0_COHORT_MINIMUM_COVERAGE = 0.95


@dataclass(frozen=True)
class AShareDay0CohortBenchmarkResultV1:
    day0: str
    as_of: str
    status: str
    median_return_pct: float | None
    cohort_members: tuple[str, ...]
    cohort_count: int
    comparable_count: int
    coverage: float
    cohort_fingerprint: str
    input_fingerprint: str
    price_basis_version: str
    source_versions: tuple[str, ...]
    reason_code: str | None = None
    contract_version: str = DAY0_COHORT_BENCHMARK_VERSION


class AShareDay0CohortBenchmarkV1:
    """Neutral fixed-Day0 A-share comparison benchmark.

    Cohort membership comes only from the authoritative Day0 market snapshot.
    It intentionally has no dependency on an application's eligibility rules.
    """

    def __init__(
        self,
        *,
        market_data: MarketDataReaderV1,
        calendar: TradingCalendarV1,
    ) -> None:
        self._market_data = market_data
        self._calendar = calendar

    def calculate(
        self, *, day0: str, as_of: str
    ) -> AShareDay0CohortBenchmarkResultV1:
        if not self._calendar.is_session(day0) or not self._calendar.is_session(as_of):
            raise ValueError("day0 and as_of must be exact published sessions")
        if as_of < day0:
            raise ValueError("as_of cannot predate day0")

        day0_result = self._market_data.read_stock_daily(day0)
        source_versions = {day0_result.source_version}
        if not day0_result.ok:
            cohort_fingerprint = _fingerprint({
                "contract_version": DAY0_COHORT_BENCHMARK_VERSION,
                "day0": day0,
                "members": (),
            })
            return self._result(
                day0=day0,
                as_of=as_of,
                cohort=(),
                returns={},
                cohort_fingerprint=cohort_fingerprint,
                source_versions=source_versions,
                consumed=((day0, "market_read", day0_result.status.value),),
                reason_code="day0_market_read_failed",
            )
        cohort_rows = {
            row.stock_code: row
            for row in day0_result.facts
            if _day0_comparable(row)
        }
        cohort = tuple(sorted(cohort_rows))
        cohort_fingerprint = _fingerprint({
            "contract_version": DAY0_COHORT_BENCHMARK_VERSION,
            "day0": day0,
            "members": cohort,
        })
        if not cohort:
            return self._result(
                day0=day0,
                as_of=as_of,
                cohort=cohort,
                returns={},
                cohort_fingerprint=cohort_fingerprint,
                source_versions=source_versions,
                consumed=(),
                reason_code="day0_comparable_cohort_missing",
            )

        returns = {code: 1.0 for code in cohort}
        valid = set(cohort)
        consumed: list[tuple[object, ...]] = [
            (day0, code, cohort_rows[code].close, cohort_rows[code].volume,
             cohort_rows[code].amount)
            for code in cohort
        ]
        sessions = tuple(
            item.isoformat()
            for item in self._calendar.sessions
            if day0 < item.isoformat() <= as_of
        )
        for session in sessions:
            daily = self._market_data.read_stock_daily(session)
            source_versions.add(daily.source_version)
            if not daily.ok:
                valid.clear()
                consumed.append((session, "market_read", daily.status.value))
                break
            rows = {row.stock_code: row for row in daily.facts}
            for code in tuple(valid):
                row = rows.get(code)
                if row is None:
                    valid.remove(code)
                    consumed.append((session, code, "authoritative_row_missing"))
                    continue
                consumed.append(
                    (session, code, row.pct_chg, row.close, row.volume, row.amount)
                )
                if _confirmed_no_trade(row):
                    if session == as_of:
                        valid.remove(code)
                    continue
                if not _positive(row.close) or not _number(row.pct_chg):
                    valid.remove(code)
                    continue
                returns[code] *= 1.0 + float(row.pct_chg) / 100.0

        cumulative = {
            code: (returns[code] - 1.0) * 100.0 for code in sorted(valid)
        }
        coverage = len(cumulative) / len(cohort)
        reason = (
            "market_return_coverage_below_95pct"
            if coverage < DAY0_COHORT_MINIMUM_COVERAGE else None
        )
        return self._result(
            day0=day0,
            as_of=as_of,
            cohort=cohort,
            returns=cumulative,
            cohort_fingerprint=cohort_fingerprint,
            source_versions=source_versions,
            consumed=tuple(consumed),
            reason_code=reason,
        )

    def _result(
        self,
        *,
        day0: str,
        as_of: str,
        cohort: tuple[str, ...],
        returns: dict[str, float],
        cohort_fingerprint: str,
        source_versions: set[str],
        consumed: tuple[tuple[object, ...], ...],
        reason_code: str | None,
    ) -> AShareDay0CohortBenchmarkResultV1:
        coverage = len(returns) / len(cohort) if cohort else 0.0
        value = median(returns.values()) if returns and reason_code is None else None
        return AShareDay0CohortBenchmarkResultV1(
            day0=day0,
            as_of=as_of,
            status="data_unknown" if reason_code else "ok",
            median_return_pct=None if value is None else round(value, 4),
            cohort_members=cohort,
            cohort_count=len(cohort),
            comparable_count=len(returns),
            coverage=coverage,
            cohort_fingerprint=cohort_fingerprint,
            input_fingerprint=_fingerprint({
                "contract_version": DAY0_COHORT_BENCHMARK_VERSION,
                "cohort_fingerprint": cohort_fingerprint,
                "as_of": as_of,
                "consumed": consumed,
            }),
            price_basis_version=DAY0_COHORT_PRICE_BASIS_VERSION,
            source_versions=tuple(sorted(source_versions)),
            reason_code=reason_code,
        )


def _day0_comparable(row: object) -> bool:
    return (
        _positive(getattr(row, "close", None))
        and _positive(getattr(row, "volume", None))
        and _positive(getattr(row, "amount", None))
    )


def _confirmed_no_trade(row: object) -> bool:
    return getattr(row, "volume", None) == 0 and getattr(row, "amount", None) == 0


def _number(value: object) -> bool:
    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isfinite(float(value))
    )


def _positive(value: object) -> bool:
    return _number(value) and float(value) > 0


def _fingerprint(payload: object) -> str:
    encoded = json.dumps(
        payload, ensure_ascii=True, separators=(",", ":"), sort_keys=True
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()
