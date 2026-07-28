from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from contextlib import closing
import os
from pathlib import Path
import shutil
import sqlite3
from statistics import median
import tempfile
from typing import Sequence

from .market_data import ReadStatus, StockDailyFactV1
from .quality import DataQualitySnapshotV1, DatasetQualityV1, QualityStatus
from .readiness import ReadinessMarkerV1, ReadinessStoreV1


SUPPLEMENTAL_SCHEMA_VERSION = "supplemental-market-facts.v1"


@dataclass(frozen=True)
class StockCapitalDailyFactV1:
    stock_code: str
    stock_name: str | None
    trade_date: str
    vendor_net_amount: float | None
    float_market_cap: float | None
    amount_unit: str
    market_cap_unit: str
    source: str
    source_version: str
    fetched_at: str

    @property
    def net_inflow_ratio(self) -> float | None:
        if (
            self.vendor_net_amount is None
            or self.float_market_cap is None
            or self.float_market_cap <= 0
            or self.amount_unit != self.market_cap_unit
        ):
            return None
        return self.vendor_net_amount / self.float_market_cap


@dataclass(frozen=True)
class SectorMembershipFactV1:
    stock_code: str
    stock_name: str | None
    sector_code: str
    sector_name: str | None
    sector_level: str
    valid_from: str
    valid_to_exclusive: str | None
    source: str
    source_version: str
    fetched_at: str


@dataclass(frozen=True)
class SectorStrengthDailyFactV1:
    sector_code: str
    sector_name: str | None
    trade_date: str
    member_count: int
    observed_member_count: int
    advancing_count: int
    declining_count: int
    median_pct_chg: float | None
    advancing_ratio: float | None
    coverage: float


@dataclass(frozen=True)
class SupplementalFactReadResultV1:
    status: ReadStatus
    dataset: str
    as_of: str
    facts: tuple[StockCapitalDailyFactV1 | SectorMembershipFactV1, ...]
    latest_available_as_of: str | None
    schema_version: str
    reason_codes: tuple[str, ...] = ()

    @property
    def ok(self) -> bool:
        return self.status is ReadStatus.OK


@dataclass(frozen=True)
class SupplementalMigrationResultV1:
    target_path: Path
    latest_available_as_of: str
    schema_version: str


class StockCapitalFactReaderV1:
    schema_version = "stock-capital-daily-facts.v1"

    def __init__(self, database_path: Path):
        self._database_path = database_path

    def read_daily(self, as_of: str) -> SupplementalFactReadResultV1:
        requested = date.fromisoformat(as_of).isoformat()
        if not self._database_path.is_file():
            return self._result(
                ReadStatus.MISSING, requested, reasons=("database_missing",)
            )
        try:
            with closing(_read_only(self._database_path)) as connection:
                columns = _columns(connection, "stock_capital_daily")
                required = {
                    "stock_code", "trade_date", "vendor_net_amount",
                    "float_market_cap", "amount_unit", "market_cap_unit",
                    "source", "source_version", "fetched_at",
                }
                if not columns:
                    return self._result(
                        ReadStatus.MISSING,
                        requested,
                        reasons=("stock_capital_daily_missing",),
                    )
                missing = sorted(required - columns)
                if missing:
                    return self._result(
                        ReadStatus.BLOCKED,
                        requested,
                        reasons=tuple(
                            f"required_column_missing:{name}" for name in missing
                        ),
                    )
                latest = _latest(connection, "stock_capital_daily", "trade_date")
                stock_name = (
                    "stock_name"
                    if "stock_name" in columns
                    else "NULL AS stock_name"
                )
                rows = connection.execute(
                    f"""SELECT stock_code, {stock_name}, trade_date,
                              vendor_net_amount, float_market_cap,
                              amount_unit, market_cap_unit, source,
                              source_version, fetched_at
                       FROM stock_capital_daily
                       WHERE trade_date=?
                       ORDER BY stock_code""",
                    (requested,),
                ).fetchall()
                if not rows:
                    return self._result(
                        ReadStatus.MISSING,
                        requested,
                        latest=latest,
                        reasons=("stock_capital_daily_as_of_missing",),
                    )
                facts = tuple(
                    StockCapitalDailyFactV1(
                        stock_code=str(row["stock_code"]),
                        stock_name=_optional_string(row["stock_name"]),
                        trade_date=str(row["trade_date"]),
                        vendor_net_amount=_optional_float(
                            row["vendor_net_amount"]
                        ),
                        float_market_cap=_optional_float(
                            row["float_market_cap"]
                        ),
                        amount_unit=str(row["amount_unit"]),
                        market_cap_unit=str(row["market_cap_unit"]),
                        source=str(row["source"]),
                        source_version=str(row["source_version"]),
                        fetched_at=str(row["fetched_at"]),
                    )
                    for row in rows
                )
                units = {
                    (fact.amount_unit, fact.market_cap_unit) for fact in facts
                }
                matched_supported_units = (
                    len(units) == 1
                    and units.pop() in {
                        ("CNY", "CNY"),
                        ("CNY_10K", "CNY_10K"),
                    }
                )
                if not matched_supported_units:
                    return self._result(
                        ReadStatus.BLOCKED,
                        requested,
                        latest=latest,
                        reasons=("unsupported_or_mixed_units",),
                    )
                return self._result(
                    ReadStatus.OK, requested, facts=facts, latest=latest
                )
        except sqlite3.Error as exc:
            return self._result(
                ReadStatus.BLOCKED,
                requested,
                reasons=(f"sqlite_error:{type(exc).__name__}",),
            )

    def _result(
        self, status: ReadStatus, as_of: str, *, facts=(), latest=None, reasons=()
    ) -> SupplementalFactReadResultV1:
        return SupplementalFactReadResultV1(
            status=status,
            dataset="stock_capital_daily",
            as_of=as_of,
            facts=tuple(facts),
            latest_available_as_of=latest,
            schema_version=self.schema_version,
            reason_codes=tuple(reasons),
        )


class SectorMembershipReaderV1:
    schema_version = "sector-membership-history.v1"

    def __init__(self, database_path: Path, *, sector_level: str = "L2"):
        if not sector_level.strip():
            raise ValueError("sector_level is required")
        self._database_path = database_path
        self._sector_level = sector_level

    def read_as_of(self, as_of: str) -> SupplementalFactReadResultV1:
        requested = date.fromisoformat(as_of).isoformat()
        if not self._database_path.is_file():
            return self._result(
                ReadStatus.MISSING, requested, reasons=("database_missing",)
            )
        try:
            with closing(_read_only(self._database_path)) as connection:
                columns = _columns(connection, "sector_membership_history")
                required = {
                    "stock_code", "sector_code", "sector_level", "valid_from",
                    "valid_to_exclusive", "source", "source_version",
                    "fetched_at",
                }
                if not columns:
                    return self._result(
                        ReadStatus.MISSING,
                        requested,
                        reasons=("sector_membership_history_missing",),
                    )
                missing = sorted(required - columns)
                if missing:
                    return self._result(
                        ReadStatus.BLOCKED,
                        requested,
                        reasons=tuple(
                            f"required_column_missing:{name}" for name in missing
                        ),
                    )
                stock_name = (
                    "stock_name"
                    if "stock_name" in columns
                    else "NULL AS stock_name"
                )
                sector_name = (
                    "sector_name"
                    if "sector_name" in columns
                    else "NULL AS sector_name"
                )
                rows = connection.execute(
                    f"""SELECT stock_code, {stock_name}, sector_code,
                              {sector_name},
                              sector_level, valid_from, valid_to_exclusive,
                              source,
                              source_version, fetched_at
                       FROM sector_membership_history
                       WHERE sector_level=?
                         AND valid_from<=?
                         AND (
                             valid_to_exclusive IS NULL
                             OR valid_to_exclusive>?
                         )
                       ORDER BY stock_code, sector_code""",
                    (self._sector_level, requested, requested),
                ).fetchall()
                if not rows:
                    return self._result(
                        ReadStatus.MISSING,
                        requested,
                        reasons=("sector_membership_as_of_missing",),
                    )
                facts = tuple(
                    SectorMembershipFactV1(
                        stock_code=str(row["stock_code"]),
                        stock_name=_optional_string(row["stock_name"]),
                        sector_code=str(row["sector_code"]),
                        sector_name=_optional_string(row["sector_name"]),
                        sector_level=str(row["sector_level"]),
                        valid_from=str(row["valid_from"]),
                        valid_to_exclusive=_optional_string(
                            row["valid_to_exclusive"]
                        ),
                        source=str(row["source"]),
                        source_version=str(row["source_version"]),
                        fetched_at=str(row["fetched_at"]),
                    )
                    for row in rows
                )
                seen: set[tuple[str, str]] = set()
                ambiguous: list[str] = []
                for fact in facts:
                    key = (fact.stock_code, fact.sector_level)
                    if key in seen:
                        ambiguous.append(
                            f"ambiguous_membership:{fact.stock_code}:"
                            f"{fact.sector_level}"
                        )
                    seen.add(key)
                if ambiguous:
                    return self._result(
                        ReadStatus.BLOCKED,
                        requested,
                        reasons=tuple(sorted(set(ambiguous))),
                    )
                return self._result(
                    ReadStatus.OK,
                    requested,
                    facts=facts,
                    latest=requested,
                )
        except sqlite3.Error as exc:
            return self._result(
                ReadStatus.BLOCKED,
                requested,
                reasons=(f"sqlite_error:{type(exc).__name__}",),
            )

    def _result(
        self, status: ReadStatus, as_of: str, *, facts=(), latest=None, reasons=()
    ) -> SupplementalFactReadResultV1:
        return SupplementalFactReadResultV1(
            status=status,
            dataset="sector_membership_history",
            as_of=as_of,
            facts=tuple(facts),
            latest_available_as_of=latest,
            schema_version=self.schema_version,
            reason_codes=tuple(reasons),
        )


def calculate_sector_strength_v1(
    *,
    as_of: str,
    memberships: Sequence[SectorMembershipFactV1],
    stock_daily: Sequence[StockDailyFactV1],
) -> tuple[SectorStrengthDailyFactV1, ...]:
    requested = date.fromisoformat(as_of).isoformat()
    returns = {
        fact.stock_code: fact.pct_chg
        for fact in stock_daily
        if fact.trade_date == requested and fact.pct_chg is not None
    }
    grouped: dict[
        tuple[str, str | None], list[SectorMembershipFactV1]
    ] = {}
    for membership in memberships:
        if (
            membership.valid_from <= requested
            and (
                membership.valid_to_exclusive is None
                or membership.valid_to_exclusive > requested
            )
        ):
            grouped.setdefault(
                (membership.sector_code, membership.sector_name), []
            ).append(membership)
    facts = []
    for (sector_code, sector_name), members in sorted(grouped.items()):
        values = [
            float(returns[item.stock_code])
            for item in members
            if item.stock_code in returns
        ]
        member_count = len(members)
        observed_count = len(values)
        facts.append(SectorStrengthDailyFactV1(
            sector_code=sector_code,
            sector_name=sector_name,
            trade_date=requested,
            member_count=member_count,
            observed_member_count=observed_count,
            advancing_count=sum(value > 0 for value in values),
            declining_count=sum(value < 0 for value in values),
            median_pct_chg=median(values) if values else None,
            advancing_ratio=(
                sum(value > 0 for value in values) / observed_count
                if observed_count else None
            ),
            coverage=observed_count / member_count if member_count else 0.0,
        ))
    return tuple(facts)


def migrate_legacy_board_facts_v1(
    *,
    source_path: Path,
    target_path: Path,
    published_at: str,
    source_version: str,
) -> SupplementalMigrationResultV1:
    source = source_path.resolve(strict=True)
    target = target_path.resolve()
    if source == target:
        raise ValueError("source_path and target_path must be different")
    parsed = datetime.fromisoformat(published_at.replace("Z", "+00:00"))
    if parsed.utcoffset() is None:
        raise ValueError("published_at must include a timezone")
    if not source_version.strip():
        raise ValueError("source_version is required")
    target.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{target.name}.", suffix=".tmp", dir=target.parent
    )
    os.close(descriptor)
    temporary = Path(temporary_name)
    try:
        if target.exists():
            shutil.copy2(target, temporary)
        with sqlite3.connect(temporary, uri=True) as connection:
            _create_schema(connection)
            source_uri = f"{source.as_uri()}?mode=ro"
            connection.execute("ATTACH DATABASE ? AS legacy", (source_uri,))
            required = {
                "board_code", "board_name", "trade_date", "open", "high",
                "low", "close", "volume", "amount", "pct_chg",
            }
            missing = sorted(
                required - _columns(connection, "legacy.ths_board_daily")
            )
            if missing:
                raise ValueError(
                    "legacy ths_board_daily missing columns: "
                    + ", ".join(missing)
                )
            connection.execute("BEGIN")
            connection.execute("DELETE FROM ths_board_daily")
            connection.execute(
                """INSERT INTO ths_board_daily
                   SELECT board_code, board_name, trade_date, open, high, low,
                          close, volume, amount, pct_chg
                   FROM legacy.ths_board_daily"""
            )
            latest = _latest(connection, "ths_board_daily", "trade_date")
            if latest is None:
                raise ValueError("legacy ths_board_daily is empty")
            connection.executemany(
                """INSERT INTO supplemental_metadata(key, value)
                   VALUES (?, ?)
                   ON CONFLICT(key) DO UPDATE SET value=excluded.value""",
                (
                    ("schema_version", SUPPLEMENTAL_SCHEMA_VERSION),
                    ("board_source_version", source_version),
                    ("board_published_at", published_at),
                ),
            )
            connection.commit()
            integrity = connection.execute(
                "PRAGMA main.integrity_check"
            ).fetchone()[0]
            if integrity != "ok":
                raise ValueError(
                    f"supplemental database integrity failed: {integrity}"
                )
        os.replace(temporary, target)
        return SupplementalMigrationResultV1(
            target_path=target,
            latest_available_as_of=latest,
            schema_version=SUPPLEMENTAL_SCHEMA_VERSION,
        )
    finally:
        temporary.unlink(missing_ok=True)


def initialize_supplemental_database_v1(database_path: Path) -> None:
    database_path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(database_path) as connection:
        _create_schema(connection)


def publish_supplemental_readiness_v1(
    *,
    readiness_root: Path,
    as_of: str,
    published_at: str,
    source_versions: dict[str, str],
    bundle: str = "v4-research-supplemental",
) -> ReadinessMarkerV1:
    requested = date.fromisoformat(as_of).isoformat()
    if not source_versions:
        raise ValueError("source_versions cannot be empty")
    snapshot = DataQualitySnapshotV1.create(
        as_of=requested,
        observed_at=published_at,
        producer_version=SUPPLEMENTAL_SCHEMA_VERSION,
        datasets=tuple(
            DatasetQualityV1(
                dataset=dataset,
                status=QualityStatus.OK,
                observed_as_of=requested,
                source_version=source_version,
                coverage=None,
                freshness_lag_sessions=0,
            )
            for dataset, source_version in source_versions.items()
        ),
    )
    return ReadinessStoreV1(readiness_root).publish_ready(
        bundle=bundle,
        snapshot=snapshot,
        required_datasets=tuple(source_versions),
        published_at=published_at,
        producer_version=SUPPLEMENTAL_SCHEMA_VERSION,
    )


def _create_schema(connection: sqlite3.Connection) -> None:
    membership_columns = _columns(
        connection, "sector_membership_history"
    )
    if (
        "valid_to" in membership_columns
        and "valid_to_exclusive" not in membership_columns
    ):
        connection.execute(
            "ALTER TABLE sector_membership_history "
            "RENAME COLUMN valid_to TO valid_to_exclusive"
        )
    connection.executescript(
        """
        CREATE TABLE IF NOT EXISTS ths_board_daily (
            board_code TEXT NOT NULL,
            board_name TEXT,
            trade_date TEXT NOT NULL,
            open REAL,
            high REAL,
            low REAL,
            close REAL,
            volume REAL,
            amount REAL,
            pct_chg REAL,
            PRIMARY KEY(board_code, trade_date)
        );
        CREATE INDEX IF NOT EXISTS idx_ths_board_daily_date
            ON ths_board_daily(trade_date);
        CREATE TABLE IF NOT EXISTS sector_fund_flow_daily (
            trade_date TEXT NOT NULL,
            sector_code TEXT NOT NULL,
            sector_name TEXT,
            amount REAL,
            change_pct REAL,
            main_inflow REAL,
            up_count INTEGER,
            down_count INTEGER,
            lead_stock_name TEXT,
            lead_stock_chg REAL,
            amount_unit TEXT NOT NULL,
            main_inflow_unit TEXT NOT NULL,
            source TEXT NOT NULL,
            source_version TEXT NOT NULL,
            fetched_at TEXT NOT NULL,
            PRIMARY KEY(trade_date, sector_code)
        );
        CREATE INDEX IF NOT EXISTS idx_sector_fund_flow_daily_date
            ON sector_fund_flow_daily(trade_date);
        CREATE TABLE IF NOT EXISTS stock_capital_daily (
            stock_code TEXT NOT NULL,
            stock_name TEXT,
            trade_date TEXT NOT NULL,
            vendor_net_amount REAL,
            float_market_cap REAL,
            amount_unit TEXT NOT NULL,
            market_cap_unit TEXT NOT NULL,
            source TEXT NOT NULL,
            source_version TEXT NOT NULL,
            fetched_at TEXT NOT NULL,
            PRIMARY KEY(stock_code, trade_date)
        );
        CREATE INDEX IF NOT EXISTS idx_stock_capital_daily_date
            ON stock_capital_daily(trade_date);
        CREATE TABLE IF NOT EXISTS sector_membership_history (
            stock_code TEXT NOT NULL,
            stock_name TEXT,
            sector_code TEXT NOT NULL,
            sector_name TEXT,
            sector_level TEXT NOT NULL,
            valid_from TEXT NOT NULL,
            valid_to_exclusive TEXT,
            source TEXT NOT NULL,
            source_version TEXT NOT NULL,
            fetched_at TEXT NOT NULL,
            PRIMARY KEY(stock_code, sector_code, sector_level, valid_from)
        );
        CREATE INDEX IF NOT EXISTS idx_sector_membership_validity
            ON sector_membership_history(
                sector_level, valid_from, valid_to_exclusive
            );
        CREATE TABLE IF NOT EXISTS supplemental_metadata (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        );
        """
    )


def _read_only(path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(
        f"{path.resolve().as_uri()}?mode=ro", uri=True
    )
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA query_only = ON")
    return connection


def _columns(connection: sqlite3.Connection, table: str) -> set[str]:
    schema, separator, name = table.partition(".")
    if not separator:
        schema, name = "main", schema
    exists = connection.execute(
        f"SELECT 1 FROM {schema}.sqlite_master "
        "WHERE type IN ('table', 'view') AND name=?",
        (name,),
    ).fetchone()
    if not exists:
        return set()
    return {
        str(row[1])
        for row in connection.execute(f"PRAGMA {schema}.table_info({name})")
    }


def _latest(
    connection: sqlite3.Connection, table: str, date_column: str
) -> str | None:
    row = connection.execute(
        f"SELECT MAX({date_column}) FROM {table}"
    ).fetchone()
    return str(row[0]) if row and row[0] else None


def _optional_float(value: object) -> float | None:
    return None if value is None else float(value)


def _optional_string(value: object) -> str | None:
    return None if value is None else str(value)
