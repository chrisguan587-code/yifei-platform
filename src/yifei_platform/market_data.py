from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from pathlib import Path
import sqlite3


class ReadStatus(str, Enum):
    OK = "ok"
    MISSING = "missing"
    BLOCKED = "blocked"


@dataclass(frozen=True)
class MarketDataSourceV1:
    database_path: Path
    source_version: str
    schema_version: str = "market-data.stock-daily.v1"

    def __post_init__(self) -> None:
        if not self.source_version.strip():
            raise ValueError("source_version is required")


@dataclass(frozen=True)
class StockDailyFactV1:
    stock_code: str
    stock_name: str | None
    trade_date: str
    open: float | None
    high: float | None
    low: float | None
    close: float | None
    preclose: float | None
    volume: float | None
    amount: float | None
    pct_chg: float | None
    turnover: float | None
    is_st: bool | None


@dataclass(frozen=True)
class StockDailyReadResultV1:
    status: ReadStatus
    as_of: str
    facts: tuple[StockDailyFactV1, ...]
    latest_available_as_of: str | None
    source_version: str
    schema_version: str
    reason_codes: tuple[str, ...] = ()

    @property
    def ok(self) -> bool:
        return self.status is ReadStatus.OK


class MarketDataReaderV1:
    """Read-only point-in-time access to neutral market facts."""

    _FIELDS = (
        "stock_code",
        "stock_name",
        "trade_date",
        "open",
        "high",
        "low",
        "close",
        "preclose",
        "volume",
        "amount",
        "pct_chg",
        "turnover",
        "is_st",
    )
    _REQUIRED_FIELDS = {"stock_code", "trade_date"}

    def __init__(self, source: MarketDataSourceV1):
        self._source = source

    def read_stock_daily(self, as_of: str) -> StockDailyReadResultV1:
        requested = _validate_iso_date(as_of)
        if not self._source.database_path.is_file():
            return self._result(ReadStatus.MISSING, requested, reasons=("database_missing",))

        try:
            with self._connect_read_only() as connection:
                columns = self._table_columns(connection, "stock_daily")
                if not columns:
                    return self._result(ReadStatus.MISSING, requested, reasons=("stock_daily_missing",))
                missing_required = sorted(self._REQUIRED_FIELDS - columns)
                if missing_required:
                    return self._result(
                        ReadStatus.BLOCKED,
                        requested,
                        reasons=tuple(f"required_column_missing:{name}" for name in missing_required),
                    )
                latest = self._latest_as_of(connection)
                facts = self._read_facts(connection, columns, requested)
                if not facts:
                    return self._result(
                        ReadStatus.MISSING,
                        requested,
                        latest=latest,
                        reasons=("stock_daily_as_of_missing",),
                    )
                return self._result(ReadStatus.OK, requested, facts=facts, latest=latest)
        except sqlite3.Error as exc:
            return self._result(
                ReadStatus.BLOCKED,
                requested,
                reasons=(f"sqlite_error:{type(exc).__name__}",),
            )

    def _connect_read_only(self) -> sqlite3.Connection:
        database_uri = f"{self._source.database_path.resolve().as_uri()}?mode=ro"
        connection = sqlite3.connect(
            database_uri,
            uri=True,
            timeout=10,
        )
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA query_only = ON")
        return connection

    @staticmethod
    def _table_columns(connection: sqlite3.Connection, table: str) -> set[str]:
        exists = connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type IN ('table', 'view') AND name = ?",
            (table,),
        ).fetchone()
        if not exists:
            return set()
        return {str(row[1]) for row in connection.execute(f"PRAGMA table_info({table})")}

    @staticmethod
    def _latest_as_of(connection: sqlite3.Connection) -> str | None:
        row = connection.execute("SELECT MAX(trade_date) FROM stock_daily").fetchone()
        return str(row[0]) if row and row[0] else None

    def _read_facts(
        self,
        connection: sqlite3.Connection,
        columns: set[str],
        as_of: str,
    ) -> tuple[StockDailyFactV1, ...]:
        selections = [name if name in columns else f"NULL AS {name}" for name in self._FIELDS]
        rows = connection.execute(
            f"SELECT {', '.join(selections)} FROM stock_daily WHERE trade_date = ? ORDER BY stock_code",
            (as_of,),
        ).fetchall()
        return tuple(
            StockDailyFactV1(
                stock_code=str(row["stock_code"]),
                stock_name=_optional_str(row["stock_name"]),
                trade_date=str(row["trade_date"]),
                open=_optional_float(row["open"]),
                high=_optional_float(row["high"]),
                low=_optional_float(row["low"]),
                close=_optional_float(row["close"]),
                preclose=_optional_float(row["preclose"]),
                volume=_optional_float(row["volume"]),
                amount=_optional_float(row["amount"]),
                pct_chg=_optional_float(row["pct_chg"]),
                turnover=_optional_float(row["turnover"]),
                is_st=None if row["is_st"] is None else bool(row["is_st"]),
            )
            for row in rows
        )

    def _result(
        self,
        status: ReadStatus,
        as_of: str,
        *,
        facts: tuple[StockDailyFactV1, ...] = (),
        latest: str | None = None,
        reasons: tuple[str, ...] = (),
    ) -> StockDailyReadResultV1:
        return StockDailyReadResultV1(
            status=status,
            as_of=as_of,
            facts=facts,
            latest_available_as_of=latest,
            source_version=self._source.source_version,
            schema_version=self._source.schema_version,
            reason_codes=reasons,
        )


def _validate_iso_date(value: str) -> str:
    from datetime import date

    return date.fromisoformat(value).isoformat()


def _optional_float(value: object) -> float | None:
    return None if value is None else float(value)


def _optional_str(value: object) -> str | None:
    return None if value is None else str(value)
