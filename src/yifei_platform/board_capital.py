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
    amount_unit: str | None = None
    main_inflow_unit: str | None = None


@dataclass(frozen=True)
class SectorMarketDailyFactV1:
    sector_code: str
    sector_name: str
    sector_level: str
    trade_date: str
    member_count: int
    observed_member_count: int
    equal_weight_return_pct: float
    amount: float
    amount_unit: str
    coverage: float
    source: str
    source_version: str
    membership_source_version: str
    published_at: str


@dataclass(frozen=True)
class FactReadResultV1:
    status: ReadStatus
    dataset: str
    as_of: str
    facts: tuple[
        BoardDailyFactV1 | SectorCapitalFactV1 | SectorMarketDailyFactV1, ...
    ]
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
            "amount_unit", "main_inflow_unit",
        )
        return self._reader.read(
            table="sector_fund_flow_daily",
            as_of=as_of,
            fields=fields,
            required={
                "sector_code", "trade_date",
                "amount_unit", "main_inflow_unit",
            },
            schema_version=self.schema_version,
            factory=lambda row: SectorCapitalFactV1(
                sector_code=str(row["sector_code"]), sector_name=_string(row["sector_name"]),
                trade_date=str(row["trade_date"]), amount=_float(row["amount"]),
                change_pct=_float(row["change_pct"]), main_inflow=_float(row["main_inflow"]),
                up_count=_int(row["up_count"]), down_count=_int(row["down_count"]),
                lead_stock_name=_string(row["lead_stock_name"]), lead_stock_chg=_float(row["lead_stock_chg"]),
                amount_unit=_string(row["amount_unit"]),
                main_inflow_unit=_string(row["main_inflow_unit"]),
            ),
            order_by="sector_code",
        )


class SectorMarketFactReaderV1:
    schema_version = "sector-market-daily-facts.v1"

    def __init__(
        self, database_path: Path, *, sector_level: str, source_version: str,
    ):
        if not sector_level.strip():
            raise ValueError("sector_level is required")
        self._sector_level = sector_level
        self._reader = _FactTableReader(database_path, source_version)

    def read_daily(self, as_of: str) -> FactReadResultV1:
        fields = (
            "sector_code", "sector_name", "sector_level", "trade_date",
            "member_count", "observed_member_count",
            "equal_weight_return_pct", "amount", "amount_unit", "coverage",
            "source", "source_version", "membership_source_version",
            "published_at",
        )
        return self._reader.read(
            table="sector_market_daily",
            dataset=f"sector_market_daily_{self._sector_level.lower()}",
            as_of=as_of,
            fields=fields,
            required=set(fields),
            schema_version=self.schema_version,
            factory=lambda row: SectorMarketDailyFactV1(
                sector_code=str(row["sector_code"]),
                sector_name=str(row["sector_name"]),
                sector_level=str(row["sector_level"]),
                trade_date=str(row["trade_date"]),
                member_count=int(row["member_count"]),
                observed_member_count=int(row["observed_member_count"]),
                equal_weight_return_pct=float(
                    row["equal_weight_return_pct"]
                ),
                amount=float(row["amount"]),
                amount_unit=str(row["amount_unit"]),
                coverage=float(row["coverage"]),
                source=str(row["source"]),
                source_version=str(row["source_version"]),
                membership_source_version=str(
                    row["membership_source_version"]
                ),
                published_at=str(row["published_at"]),
            ),
            order_by="sector_code",
            filters={
                "sector_level": self._sector_level,
                "source_version": self._reader.source_version,
            },
        )


class _FactTableReader:
    def __init__(self, database_path: Path, source_version: str):
        if not source_version.strip():
            raise ValueError("source_version is required")
        self._database_path = database_path
        self._source_version = source_version

    @property
    def source_version(self) -> str:
        return self._source_version

    def read(
        self, *, table: str, as_of: str, fields: tuple[str, ...],
        required: set[str], schema_version: str, factory, order_by: str,
        dataset: str | None = None, filters: dict[str, str] | None = None,
    ) -> FactReadResultV1:
        from datetime import date

        requested = date.fromisoformat(as_of).isoformat()
        dataset_name = dataset or table
        active_filters = dict(filters or {})
        if not self._database_path.is_file():
            return self._result(ReadStatus.MISSING, dataset_name, requested, schema_version, reasons=("database_missing",))
        try:
            with sqlite3.connect(f"{self._database_path.resolve().as_uri()}?mode=ro", uri=True) as connection:
                connection.row_factory = sqlite3.Row
                connection.execute("PRAGMA query_only = ON")
                columns = self._columns(connection, table)
                if not columns:
                    return self._result(
                        ReadStatus.MISSING, dataset_name, requested,
                        schema_version,
                        reasons=(f"{dataset_name}_missing",),
                    )
                missing = sorted((required | set(active_filters)) - columns)
                if missing:
                    return self._result(
                        ReadStatus.BLOCKED, dataset_name, requested, schema_version,
                        reasons=tuple(f"required_column_missing:{name}" for name in missing),
                    )
                filter_names = tuple(sorted(active_filters))
                filter_sql = "".join(f" AND {name}=?" for name in filter_names)
                filter_values = tuple(active_filters[name] for name in filter_names)
                latest_row = connection.execute(
                    f"SELECT MAX(trade_date) FROM {table} WHERE 1=1{filter_sql}",
                    filter_values,
                ).fetchone()
                latest = str(latest_row[0]) if latest_row and latest_row[0] else None
                selections = [name if name in columns else f"NULL AS {name}" for name in fields]
                rows = connection.execute(
                    f"SELECT {', '.join(selections)} FROM {table} "
                    f"WHERE trade_date=?{filter_sql} ORDER BY {order_by}",
                    (requested, *filter_values),
                ).fetchall()
                if not rows:
                    return self._result(
                        ReadStatus.MISSING, dataset_name, requested, schema_version, latest=latest,
                        reasons=(f"{dataset_name}_as_of_missing",),
                    )
                null_required = sorted({
                    name
                    for row in rows
                    for name in required
                    if row[name] is None or (
                        isinstance(row[name], str)
                        and not row[name].strip()
                    )
                })
                if null_required:
                    return self._result(
                        ReadStatus.BLOCKED,
                        dataset_name,
                        requested,
                        schema_version,
                        latest=latest,
                        reasons=tuple(
                            f"required_value_missing:{name}"
                            for name in null_required
                        ),
                    )
                return self._result(
                    ReadStatus.OK, dataset_name, requested, schema_version,
                    facts=tuple(factory(row) for row in rows), latest=latest,
                )
        except sqlite3.Error as exc:
            return self._result(
                ReadStatus.BLOCKED, dataset_name, requested, schema_version,
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
