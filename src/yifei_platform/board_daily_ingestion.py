from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
import math
import os
from pathlib import Path
import shutil
import sqlite3
import tempfile
from typing import Mapping, Protocol, Sequence

from .supplemental_facts import (
    BOARD_DAILY_MINIMUM_ROWS,
    initialize_supplemental_database_v1,
    serialized_supplemental_publication_v1,
)


BOARD_DAILY_SOURCE_VERSION = "akshare-ths-industry.v1"


class BoardDailyClientV1(Protocol):
    def list_boards(self) -> Sequence[Mapping[str, object]]: ...

    def read_history(
        self, board_name: str, start_date: str, end_date: str
    ) -> Sequence[Mapping[str, object]]: ...


@dataclass(frozen=True)
class BoardDailySyncResultV1:
    target_path: Path
    as_of: str
    first_synced_date: str | None
    synced_session_count: int
    inserted_row_count: int
    latest_available_as_of: str
    source_version: str


class AkshareThsBoardDailyClientV1:
    """Thin adapter for the current THS industry-board endpoints."""

    def __init__(self):
        import akshare as ak

        self._ak = ak

    def list_boards(self) -> tuple[dict[str, object], ...]:
        return tuple(
            {
                "board_code": row.get("code"),
                "board_name": row.get("name"),
            }
            for _, row in self._ak.stock_board_industry_name_ths().iterrows()
        )

    def read_history(
        self, board_name: str, start_date: str, end_date: str
    ) -> tuple[dict[str, object], ...]:
        frame = self._ak.stock_board_industry_index_ths(
            symbol=board_name, start_date=start_date, end_date=end_date,
        )
        return tuple(dict(row) for _, row in frame.iterrows())


@serialized_supplemental_publication_v1
def sync_board_daily_v1(
    *,
    client: BoardDailyClientV1,
    market_database_path: Path,
    target_path: Path,
    as_of: str,
    fetched_at: str,
    source_version: str = BOARD_DAILY_SOURCE_VERSION,
) -> BoardDailySyncResultV1:
    requested = date.fromisoformat(as_of).isoformat()
    timestamp = datetime.fromisoformat(fetched_at.replace("Z", "+00:00"))
    if timestamp.utcoffset() is None:
        raise ValueError("fetched_at must include a timezone")
    if not source_version.strip():
        raise ValueError("source_version is required")

    expected_dates = _required_sessions(
        market_database_path=market_database_path, target_path=target_path,
        as_of=requested,
    )
    if not expected_dates:
        latest = _latest_board_date(target_path)
        if latest is None:
            raise ValueError("no board dates are available")
        return BoardDailySyncResultV1(
            target_path=target_path.resolve(), as_of=requested,
            first_synced_date=None, synced_session_count=0,
            inserted_row_count=0, latest_available_as_of=latest,
            source_version=source_version,
        )

    boards = _valid_boards(client.list_boards())
    if len(boards) < BOARD_DAILY_MINIMUM_ROWS:
        raise ValueError(
            f"board list has fewer than {BOARD_DAILY_MINIMUM_ROWS} valid rows"
        )
    start = _prior_calendar_date(
        market_database_path, expected_dates[0]
    )
    rows = _collect_rows(
        client=client, boards=boards, start_date=start,
        end_date=requested, expected_dates=set(expected_dates),
    )
    for session in expected_dates:
        count = sum(row[2] == session for row in rows)
        if count < BOARD_DAILY_MINIMUM_ROWS:
            raise ValueError(
                f"board coverage below {BOARD_DAILY_MINIMUM_ROWS} for {session}: {count}"
            )

    target = target_path.resolve()
    target.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{target.name}.", suffix=".tmp", dir=target.parent,
    )
    os.close(descriptor)
    temporary = Path(temporary_name)
    try:
        if target.exists():
            shutil.copy2(target, temporary)
        initialize_supplemental_database_v1(temporary)
        with sqlite3.connect(temporary) as connection:
            connection.execute("BEGIN")
            connection.execute(
                "DELETE FROM ths_board_daily WHERE trade_date BETWEEN ? AND ?",
                (expected_dates[0], expected_dates[-1]),
            )
            connection.executemany(
                """INSERT INTO ths_board_daily (
                       board_code, board_name, trade_date, open, high, low,
                       close, volume, amount, pct_chg
                   ) VALUES (?,?,?,?,?,?,?,?,?,?)""",
                rows,
            )
            connection.executemany(
                """INSERT INTO supplemental_metadata(key, value)
                   VALUES (?, ?)
                   ON CONFLICT(key) DO UPDATE SET value=excluded.value""",
                (
                    ("board_source_version", source_version),
                    ("board_published_at", fetched_at),
                ),
            )
            connection.commit()
            integrity = connection.execute("PRAGMA integrity_check").fetchone()[0]
            if integrity != "ok":
                raise ValueError(f"supplemental database integrity failed: {integrity}")
        os.replace(temporary, target)
    finally:
        temporary.unlink(missing_ok=True)

    return BoardDailySyncResultV1(
        target_path=target, as_of=requested,
        first_synced_date=expected_dates[0],
        synced_session_count=len(expected_dates), inserted_row_count=len(rows),
        latest_available_as_of=expected_dates[-1], source_version=source_version,
    )


def _required_sessions(
    *, market_database_path: Path, target_path: Path, as_of: str,
) -> tuple[str, ...]:
    with sqlite3.connect(
        f"file:{market_database_path.resolve()}?mode=ro", uri=True,
    ) as connection:
        stock_rows = connection.execute(
            "SELECT COUNT(*) FROM stock_daily WHERE trade_date=?", (as_of,)
        ).fetchone()[0]
        if not stock_rows:
            raise ValueError(f"stock_daily_as_of_missing:{as_of}")
        latest = _latest_board_date(target_path)
        clause = "trade_date <= ?" if latest is None else "trade_date > ? AND trade_date <= ?"
        parameters = (as_of,) if latest is None else (latest, as_of)
        return tuple(
            str(row[0]) for row in connection.execute(
                f"SELECT trade_date FROM trading_calendar WHERE {clause} ORDER BY trade_date",
                parameters,
            )
        )


def _prior_calendar_date(market_database_path: Path, first_date: str) -> str:
    with sqlite3.connect(
        f"file:{market_database_path.resolve()}?mode=ro", uri=True,
    ) as connection:
        row = connection.execute(
            "SELECT MAX(trade_date) FROM trading_calendar WHERE trade_date < ?",
            (first_date,),
        ).fetchone()
    return str(row[0]) if row and row[0] else first_date


def _latest_board_date(target_path: Path) -> str | None:
    if not target_path.is_file():
        return None
    with sqlite3.connect(f"file:{target_path.resolve()}?mode=ro", uri=True) as connection:
        row = connection.execute(
            "SELECT MAX(trade_date) FROM ths_board_daily"
        ).fetchone()
    return str(row[0]) if row and row[0] else None


def _valid_boards(rows: Sequence[Mapping[str, object]]) -> tuple[tuple[str, str], ...]:
    boards = {
        (_text(row.get("board_code")), _text(row.get("board_name")))
        for row in rows
    }
    valid = tuple(sorted((code, name) for code, name in boards if code and name))
    if len({code for code, _ in valid}) != len(valid):
        raise ValueError("board list has duplicate board codes")
    return valid


def _collect_rows(
    *, client: BoardDailyClientV1, boards: Sequence[tuple[str, str]],
    start_date: str, end_date: str, expected_dates: set[str],
) -> tuple[tuple[object, ...], ...]:
    rows: list[tuple[object, ...]] = []
    seen: set[tuple[str, str]] = set()
    for board_code, board_name in boards:
        previous_close: float | None = None
        for row in client.read_history(
            board_name, start_date.replace("-", ""), end_date.replace("-", ""),
        ):
            trade_date = _date_text(row.get("日期"))
            close = _number(row.get("收盘价"))
            if trade_date is None or close is None:
                continue
            pct_chg = (
                (close / previous_close - 1.0) * 100.0
                if previous_close is not None else None
            )
            previous_close = close
            if trade_date not in expected_dates:
                continue
            values = (
                _number(row.get("开盘价")), _number(row.get("最高价")),
                _number(row.get("最低价")), close, _number(row.get("成交量")),
                _number(row.get("成交额")), pct_chg,
            )
            if any(value is None for value in values):
                continue
            identity = (board_code, trade_date)
            if identity in seen:
                raise ValueError(f"duplicate board daily row: {board_code} {trade_date}")
            seen.add(identity)
            rows.append((board_code, board_name, trade_date, *values))
    return tuple(rows)


def _date_text(value: object) -> str | None:
    if hasattr(value, "strftime"):
        return value.strftime("%Y-%m-%d")
    try:
        return date.fromisoformat(str(value)[:10]).isoformat()
    except ValueError:
        return None


def _number(value: object) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _text(value: object) -> str:
    return "" if value is None else str(value).strip()
