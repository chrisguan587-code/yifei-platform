from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from contextlib import closing, contextmanager
from functools import wraps
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
BOARD_DAILY_MINIMUM_ROWS = 90


def serialized_supplemental_publication_v1(function):
    @wraps(function)
    def wrapped(*args, **kwargs):
        target_path = kwargs.get("target_path")
        if not isinstance(target_path, Path):
            raise TypeError("target_path must be a Path")
        with _supplemental_publication_lock(target_path):
            return function(*args, **kwargs)
    return wrapped


@contextmanager
def _supplemental_publication_lock(target_path: Path):
    import fcntl

    target = target_path.resolve()
    target.parent.mkdir(parents=True, exist_ok=True)
    lock_path = target.parent / f".{target.name}.publish.lock"
    with lock_path.open("a", encoding="utf-8") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


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


@dataclass(frozen=True)
class ThsMembershipMigrationResultV1:
    target_path: Path
    valid_from: str
    stock_count: int
    board_count: int
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


@serialized_supplemental_publication_v1
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
            if target.exists() and _legacy_board_migration_is_idempotent(
                connection=connection,
                published_at=published_at,
                source_version=source_version,
            ):
                latest = _latest(
                    connection, "ths_board_daily", "trade_date"
                )
                return SupplementalMigrationResultV1(
                    target_path=target,
                    latest_available_as_of=latest,
                    schema_version=SUPPLEMENTAL_SCHEMA_VERSION,
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


@serialized_supplemental_publication_v1
def migrate_legacy_ths_membership_v1(
    *,
    source_path: Path,
    target_path: Path,
    valid_from: str,
    fetched_at: str,
    source_version: str,
) -> ThsMembershipMigrationResultV1:
    """Import one fixed annual THS industry snapshot without a runtime V3 dependency."""
    source = source_path.resolve(strict=True)
    target = target_path.resolve(strict=True)
    if source == target:
        raise ValueError("source_path and target_path must be different")
    date.fromisoformat(valid_from)
    parsed = datetime.fromisoformat(fetched_at.replace("Z", "+00:00"))
    if parsed.utcoffset() is None:
        raise ValueError("fetched_at must include a timezone")
    if not source_version.strip():
        raise ValueError("source_version is required")

    target.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{target.name}.", suffix=".tmp", dir=target.parent
    )
    os.close(descriptor)
    temporary = Path(temporary_name)
    source_name = "v3_snapshot.pywencai"
    try:
        shutil.copy2(target, temporary)
        with sqlite3.connect(temporary, uri=True) as connection:
            _create_schema(connection)
            source_uri = f"{source.as_uri()}?mode=ro"
            connection.execute("ATTACH DATABASE ? AS legacy", (source_uri,))
            required = {"stock_code", "ths_l2_industry"}
            missing = sorted(
                required - _columns(connection, "legacy.ths_stock_industry")
            )
            if missing:
                raise ValueError(
                    "legacy ths_stock_industry missing columns: "
                    + ", ".join(missing)
                )
            names = connection.execute(
                """SELECT DISTINCT board_name FROM ths_board_daily
                   WHERE trade_date=(SELECT MAX(trade_date)
                                     FROM ths_board_daily)"""
            ).fetchall()
            if len(names) < BOARD_DAILY_MINIMUM_ROWS:
                raise ValueError("target ths_board_daily coverage is insufficient")
            board_names = {str(row[0]) for row in names}
            legacy_columns = _columns(connection, "legacy.ths_stock_industry")
            stock_name = "stock_name" if "stock_name" in legacy_columns else "NULL"
            rows = connection.execute(
                f"""SELECT stock_code, {stock_name}, ths_l2_industry
                    FROM legacy.ths_stock_industry
                    WHERE stock_code IS NOT NULL
                      AND ths_l2_industry IS NOT NULL
                      AND ths_l2_industry<>''
                    ORDER BY stock_code"""
            ).fetchall()
            mapped_names = {str(row[2]) for row in rows}
            unknown = sorted(mapped_names - board_names)
            if unknown:
                raise ValueError(
                    "legacy THS names absent from ths_board_daily: "
                    + ", ".join(unknown[:5])
                )
            if mapped_names != board_names:
                raise ValueError("legacy THS membership does not cover all boards")
            connection.execute("BEGIN")
            connection.execute(
                """DELETE FROM sector_membership_history
                   WHERE source=? AND sector_level='THS_L2' AND valid_from=?""",
                (source_name, valid_from),
            )
            connection.executemany(
                """INSERT INTO sector_membership_history (
                       stock_code, stock_name, sector_code, sector_name,
                       sector_level, valid_from, valid_to_exclusive, source,
                       source_version, fetched_at
                   ) VALUES (?,?,?,?,?,?,?,?,?,?)""",
                (
                    (
                        str(stock_code), stock_name, f"THS_L2:{sector_name}",
                        str(sector_name), "THS_L2", valid_from, None,
                        source_name, source_version, fetched_at,
                    )
                    for stock_code, stock_name, sector_name in rows
                ),
            )
            connection.executemany(
                """INSERT INTO supplemental_metadata(key, value)
                   VALUES (?, ?)
                   ON CONFLICT(key) DO UPDATE SET value=excluded.value""",
                (
                    ("ths_membership_source_version", source_version),
                    ("ths_membership_fetched_at", fetched_at),
                    ("ths_membership_valid_from", valid_from),
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
        return ThsMembershipMigrationResultV1(
            target_path=target,
            valid_from=valid_from,
            stock_count=len(rows),
            board_count=len(mapped_names),
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
    database_path: Path,
    readiness_root: Path,
    as_of: str,
    published_at: str,
    source_versions: dict[str, str],
    dataset_coverages: dict[str, float | None],
    dataset_gate_coverages: dict[str, float] | None = None,
    bundle: str = "v4-research-supplemental",
) -> ReadinessMarkerV1:
    requested = date.fromisoformat(as_of).isoformat()
    if not source_versions:
        raise ValueError("source_versions cannot be empty")
    if set(dataset_coverages) != set(source_versions):
        raise ValueError(
            "dataset_coverages must match source_versions"
        )
    gate_coverages = dataset_gate_coverages or {}
    if "stock_capital_daily" in source_versions:
        capital_coverage = gate_coverages.get("stock_capital_daily")
        if capital_coverage is None or capital_coverage < 0.98:
            raise ValueError(
                "stock capital readiness coverage is below "
                "the frozen threshold"
            )
    if "sector_membership_history" in source_versions:
        membership_coverage = gate_coverages.get(
            "sector_membership_history"
        )
        if membership_coverage is None or membership_coverage < 0.99:
            raise ValueError(
                "sector membership readiness coverage is below "
                "the frozen threshold"
            )
    with closing(_read_only(database_path.resolve(strict=True))) as connection:
        for dataset, source_version in source_versions.items():
            _require_ready_dataset(
                connection=connection,
                dataset=dataset,
                as_of=requested,
                source_version=source_version,
            )
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
                coverage=dataset_coverages[dataset],
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


def _require_ready_dataset(
    *,
    connection: sqlite3.Connection,
    dataset: str,
    as_of: str,
    source_version: str,
) -> None:
    queries = {
        "stock_capital_daily": (
            """SELECT COUNT(*) FROM stock_capital_daily
               WHERE trade_date=? AND source_version=?""",
            (as_of, source_version),
            1,
        ),
        "sector_membership_history": (
            """SELECT COUNT(*) FROM sector_membership_history
               WHERE sector_level='L2'
                 AND valid_from<=?
                 AND (valid_to_exclusive IS NULL OR valid_to_exclusive>?)
                 AND source_version=?""",
            (as_of, as_of, source_version),
            1,
        ),
        "sector_fund_flow_daily": (
            """SELECT COUNT(*) FROM sector_fund_flow_daily
               WHERE trade_date=? AND source_version=?
                 AND amount_unit='CNY'
                 AND main_inflow_unit='CNY'""",
            (as_of, source_version),
            400,
        ),
        "sector_market_daily": (
            """SELECT COUNT(*) FROM sector_market_daily
               WHERE trade_date=? AND sector_level='THS_L2'
                 AND source_version=? AND amount_unit='CNY'""",
            (as_of, source_version),
            80,
        ),
        "sector_market_daily_sw_l2": (
            """SELECT COUNT(*) FROM sector_market_daily
               WHERE trade_date=? AND sector_level='L2'
                 AND source_version=? AND amount_unit='CNY'""",
            (as_of, source_version),
            120,
        ),
    }
    if dataset == "ths_board_daily":
        count = connection.execute(
            "SELECT COUNT(*) FROM ths_board_daily WHERE trade_date=?",
            (as_of,),
        ).fetchone()[0]
        metadata = dict(connection.execute(
            """SELECT key,value FROM supplemental_metadata
               WHERE key='board_source_version'"""
        ))
        if metadata.get("board_source_version") != source_version:
            raise ValueError("board readiness source version mismatch")
    elif dataset in queries:
        query, parameters, minimum_count = queries[dataset]
        count = connection.execute(query, parameters).fetchone()[0]
    else:
        raise ValueError(f"unsupported readiness dataset: {dataset}")
    required_count = (
        BOARD_DAILY_MINIMUM_ROWS
        if dataset == "ths_board_daily" else minimum_count
    )
    if int(count) < required_count:
        raise ValueError(
            f"cannot publish ready for missing dataset or incomplete dataset "
            f"{dataset} at {as_of}: {count} < {required_count}"
        )


def _legacy_board_migration_is_idempotent(
    *,
    connection: sqlite3.Connection,
    published_at: str,
    source_version: str,
) -> bool:
    existing_count = int(connection.execute(
        "SELECT COUNT(*) FROM main.ths_board_daily"
    ).fetchone()[0])
    if existing_count == 0:
        return False
    metadata = dict(connection.execute(
        """SELECT key,value FROM main.supplemental_metadata
           WHERE key IN ('board_source_version','board_published_at')"""
    ))
    if metadata != {
        "board_source_version": source_version,
        "board_published_at": published_at,
    }:
        raise FileExistsError(
            "existing board publication identity differs from migration"
        )
    columns = (
        "board_code,board_name,trade_date,open,high,low,close,"
        "volume,amount,pct_chg"
    )
    main_only = int(connection.execute(
        f"""SELECT COUNT(*) FROM (
                SELECT {columns} FROM main.ths_board_daily
                EXCEPT
                SELECT {columns} FROM legacy.ths_board_daily
            )"""
    ).fetchone()[0])
    legacy_only = int(connection.execute(
        f"""SELECT COUNT(*) FROM (
                SELECT {columns} FROM legacy.ths_board_daily
                EXCEPT
                SELECT {columns} FROM main.ths_board_daily
            )"""
    ).fetchone()[0])
    if main_only or legacy_only:
        raise FileExistsError(
            "existing board publication content differs from migration"
        )
    return True
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
        CREATE TABLE IF NOT EXISTS sector_market_daily (
            sector_code TEXT NOT NULL,
            sector_name TEXT NOT NULL,
            sector_level TEXT NOT NULL,
            trade_date TEXT NOT NULL,
            member_count INTEGER NOT NULL,
            observed_member_count INTEGER NOT NULL,
            equal_weight_return_pct REAL NOT NULL,
            amount REAL NOT NULL,
            amount_unit TEXT NOT NULL,
            coverage REAL NOT NULL,
            source TEXT NOT NULL,
            source_version TEXT NOT NULL,
            membership_source_version TEXT NOT NULL,
            published_at TEXT NOT NULL,
            PRIMARY KEY(sector_code, sector_level, trade_date)
        );
        CREATE INDEX IF NOT EXISTS idx_sector_market_daily_date
            ON sector_market_daily(sector_level, trade_date);
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
