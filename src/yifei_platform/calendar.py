from __future__ import annotations

from bisect import bisect_left, bisect_right
from dataclasses import dataclass
from datetime import date, datetime
from typing import Iterable


class CalendarRangeError(ValueError):
    """Raised when a date cannot be resolved inside the published calendar."""


@dataclass(frozen=True)
class TradeDateContextV1:
    requested_date: date
    session: date
    previous_session: date | None
    next_session: date | None
    is_session: bool
    source_version: str
    schema_version: str = "trading-calendar.v1"


class TradingCalendarV1:
    """Versioned, deterministic trading-session contract.

    Session production belongs to Platform data ingestion. This contract only
    consumes an explicit published session set; it never guesses exchange
    sessions from weekdays or public-workday calendars.
    """

    schema_version = "trading-calendar.v1"

    def __init__(self, sessions: Iterable[date | str], *, source_version: str):
        if not source_version.strip():
            raise ValueError("source_version is required")
        normalized = sorted({_coerce_date(item) for item in sessions})
        if not normalized:
            raise ValueError("at least one trading session is required")
        self._sessions = tuple(normalized)
        self._source_version = source_version

    @property
    def source_version(self) -> str:
        return self._source_version

    @property
    def sessions(self) -> tuple[date, ...]:
        return self._sessions

    def is_session(self, current: date | datetime | str) -> bool:
        requested = _coerce_date(current)
        index = bisect_left(self._sessions, requested)
        return index < len(self._sessions) and self._sessions[index] == requested

    def resolve_session(self, current: date | datetime | str) -> date:
        """Resolve to the latest published session on or before current."""
        requested = _coerce_date(current)
        index = bisect_right(self._sessions, requested) - 1
        if index < 0:
            raise CalendarRangeError(f"no session on or before {requested.isoformat()}")
        return self._sessions[index]

    def offset_session(self, current: date | datetime | str, offset: int) -> date:
        base = self.resolve_session(current)
        base_index = bisect_left(self._sessions, base)
        target_index = base_index + offset
        if target_index < 0 or target_index >= len(self._sessions):
            raise CalendarRangeError(
                f"session offset {offset} from {base.isoformat()} is outside the published range"
            )
        return self._sessions[target_index]

    def context(self, current: date | datetime | str) -> TradeDateContextV1:
        requested = _coerce_date(current)
        session = self.resolve_session(requested)
        index = bisect_left(self._sessions, session)
        previous_session = self._sessions[index - 1] if index > 0 else None
        next_session = self._sessions[index + 1] if index + 1 < len(self._sessions) else None
        return TradeDateContextV1(
            requested_date=requested,
            session=session,
            previous_session=previous_session,
            next_session=next_session,
            is_session=self.is_session(requested),
            source_version=self._source_version,
        )


def _coerce_date(value: date | datetime | str) -> date:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    return date.fromisoformat(str(value))
