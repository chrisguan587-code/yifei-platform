from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import sqlite3

from .market_data import ReadStatus


@dataclass(frozen=True)
class BoardDailyFactV1:
    board_code: str
    board_name: str | None
    trade_date: str
    open: float | None
    high: float | None
    low: float | None
    close: float | None
    volume: float | None
    amount: float | None
    pct_chg: float | None


@dataclass(frozen=True)
class SectorCapitalFactV1:
    sector_code: str
    sector_name: str | None
    trade_date: str
    amount: float | None
    change_pct: float | None
    main_inflow: float | None
    up_count: int | None
    down_count: int | None
    lead_stock_name: str | None
    lead_stock_chg: float | None


@dataclass(frozen=True)
class FactReadResultV1:
    status: ReadStatus
    dataset: str
    as_of: str
    facts: tuple[BoardDailyFactV1 | SectorCapitalFactV1, ...]
    latest_available_as_of: str | None
    source_version: str
    schema_version: str
    reason_codes: tuple[str, ...] = ()

    @property
    def ok(self) -> bool:
        return self.status is ReadStatus.OK


class BoardFactReaderV1:
    schema_version = "board-daily-facts.v1"

    def __init__(self, database_path: Path, *, source_version: str):
        self._reader = _FactTableReader(database_path, source_version)

    def read_daily(self, as_of: str) -> FactReadResultV1:
        fields = ("board_code", "board_name", "trade_date", "open", "high", "low", "close", "volume", "amount", "pct_chg")
        return self._reader.read(
            table="ths_board_daily",
            as_of=as_of,
            fields=fields,
            required={"board_code", "trade_date"},
            schema_version=self.schema_version,
            factory=lambda row: BoardDailyFactV1(
                board_code=str(row["board_code"]),
                board_name=_string(row["board_name"]),
                trade_date=str(row["trade_date"]),
                open=_float(row["open"]), high=_float(row["high"]), low=_float(row["low"]),
                close=_float(row["close"]), volume=_float(row["volume"]), amount=_float(row["amount"]),
                pct_chg=_float(row["pct_chg"]),
            ),
            order_by="board_code",
        )


class CapitalFactReaderV1:
    schema_version = "sector-capital-facts.v1"

    def __init__(self, database_path: Path, *, source_version: str):
        self._reader = _FactTableReader(database_path, source_version)

    def read_sector_daily(self, as_of: str) -> FactReadResultV1:
        fields = (
            "sector_code", "sector_name", "trade_date", "amount", "change_pct", "main_inflow",
            "up_count", "down_count", "lead_stock_name", "lead_stock_chg",
        )
        return self._reader.read(
            table="sector_fund_flow_daily",
            as_of=as_of,
            fields=fields,
            required={"sector_code", "trade_date"},
            schema_version=self.schema_version,
            factory=lambda row: SectorCapitalFactV1(
                sector_code=str(row["sector_code"]), sector_name=_string(row["sector_name"]),
                trade_date=str(row["trade_date"]), amount=_float(row["amount"]),
                change_pct=_float(row["change_pct"]), main_inflow=_float(row["main_inflow"]),
                up_count=_int(row["up_count"]), down_count=_int(row["down_count"]),
                lead_stock_name=_string(row["lead_stock_name"]), lead_stock_chg=_float(row["lead_stock_chg"]),
            ),
            order_by="sector_code",
        )


class _FactTableReader:
    def __init__(self, database_path: Path, source_version: str):
        if not source_version.strip():
            raise ValueError("source_version is required")
        self._database_path = database_path
        self._source_version = source_version

    def read(self, *, table: str, as_of: str, fields: tuple[str, ...], required: set[str], schema_version: str, factory, order_by: str) -> FactReadResultV1:
        from datetime import date

        requested = date.fromisoformat(as_of).isoformat()
        if not self._database_path.is_file():
            return self._result(ReadStatus.MISSING, table, requested, schema_version, reasons=("database_missing",))
        try:
            with sqlite3.connect(f"{self._database_path.resolve().as_uri()}?mode=ro", uri=True) as connection:
                connection.row_factory = sqlite3.Row
                connection.execute("PRAGMA query_only = ON")
                columns = self._columns(connection, table)
                if not columns:
                    return self._result(ReadStatus.MISSING, table, requested, schema_version, reasons=(f"{table}_missing",))
                missing = sorted(required - columns)
                if missing:
                    return self._result(
                        ReadStatus.BLOCKED, table, requested, schema_version,
                        reasons=tuple(f"required_column_missing:{name}" for name in missing),
                    )
                latest_row = connection.execute(f"SELECT MAX(trade_date) FROM {table}").fetchone()
                latest = str(latest_row[0]) if latest_row and latest_row[0] else None
                selections = [name if name in columns else f"NULL AS {name}" for name in fields]
                rows = connection.execute(
                    f"SELECT {', '.join(selections)} FROM {table} WHERE trade_date = ? ORDER BY {order_by}",
                    (requested,),
                ).fetchall()
                if not rows:
                    return self._result(
                        ReadStatus.MISSING, table, requested, schema_version, latest=latest,
                        reasons=(f"{table}_as_of_missing",),
                    )
                return self._result(
                    ReadStatus.OK, table, requested, schema_version,
                    facts=tuple(factory(row) for row in rows), latest=latest,
                )
        except sqlite3.Error as exc:
            return self._result(
                ReadStatus.BLOCKED, table, requested, schema_version,
                reasons=(f"sqlite_error:{type(exc).__name__}",),
            )

    def _result(self, status: ReadStatus, dataset: str, as_of: str, schema_version: str, *, facts=(), latest=None, reasons=()) -> FactReadResultV1:
        return FactReadResultV1(
            status=status, dataset=dataset, as_of=as_of, facts=tuple(facts),
            latest_available_as_of=latest, source_version=self._source_version,
            schema_version=schema_version, reason_codes=tuple(reasons),
        )

    @staticmethod
    def _columns(connection: sqlite3.Connection, table: str) -> set[str]:
        exists = connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type IN ('table', 'view') AND name=?", (table,)
        ).fetchone()
        return {str(row[1]) for row in connection.execute(f"PRAGMA table_info({table})")} if exists else set()


def _float(value: object) -> float | None:
    return None if value is None else float(value)


def _int(value: object) -> int | None:
    return None if value is None else int(value)


def _string(value: object) -> str | None:
    return None if value is None else str(value)
