from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from contextlib import contextmanager
import fcntl
from functools import wraps
import hashlib
import json
import math
import os
from pathlib import Path
import sqlite3
import tempfile

from .quality import DataQualitySnapshotV1, DatasetQualityV1, QualityStatus
from .readiness import ReadinessMarkerV1, ReadinessStoreV1
from .turnover_ingestion import TURNOVER_COVERAGE_MINIMUM


BOOTSTRAP_VERSION = "bootstrap-market-data.v1"
TRANSITIONAL_DAILY_VERSION = "transitional-daily-market-data.v1"
TURNOVER_ENRICHED_DAILY_VERSION = (
    "transitional-daily-market-data+baostock-turnover.v2"
)


@dataclass(frozen=True)
class BootstrapResult:
    target_path: Path
    as_of: str
    row_count: int
    session_count: int
    database_sha256: str
    readiness_marker: ReadinessMarkerV1


def _serialized_publication(function):
    @wraps(function)
    def wrapped(*args, **kwargs):
        target_path = kwargs.get("target_path")
        if not isinstance(target_path, Path):
            raise TypeError("target_path must be a Path")
        with _publication_lock(target_path):
            return function(*args, **kwargs)
    return wrapped


@contextmanager
def _publication_lock(target_path: Path):
    parent = target_path.resolve().parent
    parent.mkdir(parents=True, exist_ok=True)
    lock_path = parent / f".{target_path.name}.publish.lock"
    with lock_path.open("a", encoding="utf-8") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


@_serialized_publication
def publish_transitional_daily_market_data(
    *,
    source_path: Path,
    source_health_path: Path,
    target_path: Path,
    readiness_root: Path,
    as_of: str,
    published_at: str,
) -> BootstrapResult:
    """Temporary V3-to-Platform bridge; retire after Platform owns ingestion."""
    expected_as_of = date.fromisoformat(as_of).isoformat()
    source = source_path.resolve(strict=True)
    health = _validate_source_health(source_health_path, expected_as_of)
    target = target_path.resolve()
    if source == target:
        raise ValueError("source_path and target_path must be different files")
    parsed_published_at = datetime.fromisoformat(published_at.replace("Z", "+00:00"))
    if parsed_published_at.utcoffset() is None:
        raise ValueError("published_at must include a timezone")
    target.parent.mkdir(parents=True, exist_ok=True)
    same_day_retry = False
    effective_published_at = published_at
    if target.exists():
        current_as_of = _published_as_of(target)
        if current_as_of > expected_as_of:
            raise FileExistsError(f"target is newer than as_of {expected_as_of}: {target}")
        if current_as_of == expected_as_of:
            metadata = load_market_metadata(target)
            if metadata["producer_version"] != TRANSITIONAL_DAILY_VERSION:
                raise FileExistsError(
                    "existing same-day target was not produced by transitional publisher"
                )
            same_day_retry = True
            effective_published_at = metadata["published_at"]
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{target.name}.", suffix=".tmp", dir=target.parent
    )
    os.close(descriptor)
    temporary = Path(temporary_name)
    try:
        source_stats = _build_database(
            source=source,
            target=temporary,
            published_at=effective_published_at,
            producer_version=TRANSITIONAL_DAILY_VERSION,
            schema_version="market-data.transitional-daily.v1",
        )
        if source_stats["max_trade_date"] != expected_as_of:
            raise ValueError("source stock_daily latest date does not match as_of")
        if source_stats["as_of_row_count"] != health["stock_daily_rows"]:
            raise ValueError("source stock_daily row count does not match health artifact")
        row_count, session_count = _validate_database(temporary, source_stats)
        database_sha256 = _sha256(temporary)
        if same_day_retry:
            if _sha256(target) != database_sha256:
                raise ValueError(
                    "same-day source content changed; explicit correction version required"
                )
            return _republish_existing_daily_target(
                target=target,
                readiness_root=readiness_root,
                health=health,
                as_of=expected_as_of,
            )
        os.replace(temporary, target)
        snapshot = DataQualitySnapshotV1.create(
            as_of=expected_as_of,
            observed_at=published_at,
            producer_version=TRANSITIONAL_DAILY_VERSION,
            datasets=(DatasetQualityV1(
                dataset="stock_daily",
                status=QualityStatus.OK,
                observed_as_of=expected_as_of,
                source_version=database_sha256,
                coverage=1.0,
                freshness_lag_sessions=0,
            ),),
        )
        marker = ReadinessStoreV1(readiness_root).publish_ready(
            bundle="v4-market-core",
            snapshot=snapshot,
            required_datasets=("stock_daily",),
            published_at=published_at,
            producer_version=TRANSITIONAL_DAILY_VERSION,
        )
        return BootstrapResult(
            target, expected_as_of, row_count, session_count, database_sha256, marker
        )
    finally:
        temporary.unlink(missing_ok=True)


@_serialized_publication
def publish_turnover_enriched_daily_market_data(
    *,
    source_path: Path,
    source_health_path: Path,
    turnover_snapshot_path: Path,
    target_path: Path,
    readiness_root: Path,
    as_of: str,
    published_at: str,
) -> BootstrapResult:
    """Publish transitional market facts with a validated BaoStock overlay."""
    expected_as_of = date.fromisoformat(as_of).isoformat()
    source = source_path.resolve(strict=True)
    health = _validate_source_health(source_health_path, expected_as_of)
    snapshot = _load_turnover_snapshot(
        turnover_snapshot_path, expected_as_of
    )
    target = target_path.resolve()
    if source == target:
        raise ValueError("source_path and target_path must be different files")
    parsed_published_at = datetime.fromisoformat(
        published_at.replace("Z", "+00:00")
    )
    if parsed_published_at.utcoffset() is None:
        raise ValueError("published_at must include a timezone")
    snapshot_fetched_at = datetime.fromisoformat(
        str(snapshot["fetched_at"]).replace("Z", "+00:00")
    )
    if snapshot_fetched_at > parsed_published_at:
        raise ValueError(
            "turnover snapshot fetched_at cannot be after published_at"
        )
    target.parent.mkdir(parents=True, exist_ok=True)
    same_day_retry = False
    effective_published_at = published_at
    if target.exists():
        current_as_of = _published_as_of(target)
        if current_as_of > expected_as_of:
            raise FileExistsError(
                f"target is newer than as_of {expected_as_of}: {target}"
            )
        if current_as_of == expected_as_of:
            metadata = load_market_metadata(target)
            if metadata["producer_version"] != TURNOVER_ENRICHED_DAILY_VERSION:
                raise FileExistsError(
                    "existing same-day target was not produced by "
                    "turnover-enriched publisher"
                )
            same_day_retry = True
            effective_published_at = metadata["published_at"]
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{target.name}.", suffix=".tmp", dir=target.parent
    )
    os.close(descriptor)
    temporary = Path(temporary_name)
    try:
        source_stats = _build_database(
            source=source,
            target=temporary,
            published_at=effective_published_at,
            producer_version=TURNOVER_ENRICHED_DAILY_VERSION,
            schema_version="market-data.transitional-daily.v2",
        )
        if source_stats["max_trade_date"] != expected_as_of:
            raise ValueError(
                "source stock_daily latest date does not match as_of"
            )
        if source_stats["as_of_row_count"] != health["stock_daily_rows"]:
            raise ValueError(
                "source stock_daily row count does not match health artifact"
            )
        _apply_turnover_snapshot(
            database=temporary,
            snapshot=snapshot,
            as_of=expected_as_of,
        )
        row_count, session_count = _validate_database(
            temporary, source_stats
        )
        database_sha256 = _sha256(temporary)
        if same_day_retry:
            if _sha256(target) != database_sha256:
                raise ValueError(
                    "same-day source content changed; "
                    "explicit correction version required"
                )
            return _republish_existing_daily_target(
                target=target,
                readiness_root=readiness_root,
                health=health,
                as_of=expected_as_of,
                producer_version=TURNOVER_ENRICHED_DAILY_VERSION,
                coverage=float(snapshot["summary"]["coverage"]),
            )
        os.replace(temporary, target)
        snapshot_quality = DataQualitySnapshotV1.create(
            as_of=expected_as_of,
            observed_at=published_at,
            producer_version=TURNOVER_ENRICHED_DAILY_VERSION,
            datasets=(DatasetQualityV1(
                dataset="stock_daily",
                status=QualityStatus.OK,
                observed_as_of=expected_as_of,
                source_version=database_sha256,
                coverage=float(snapshot["summary"]["coverage"]),
                freshness_lag_sessions=0,
            ),),
        )
        marker = ReadinessStoreV1(readiness_root).publish_ready(
            bundle="v4-market-core",
            snapshot=snapshot_quality,
            required_datasets=("stock_daily",),
            published_at=published_at,
            producer_version=TURNOVER_ENRICHED_DAILY_VERSION,
        )
        return BootstrapResult(
            target,
            expected_as_of,
            row_count,
            session_count,
            database_sha256,
            marker,
        )
    finally:
        temporary.unlink(missing_ok=True)


@_serialized_publication
def bootstrap_market_data(
    *,
    source_path: Path,
    target_path: Path,
    readiness_root: Path,
    published_at: str,
) -> BootstrapResult:
    """Publish an independent Platform database from an explicit legacy source."""
    source = source_path.resolve(strict=True)
    target = target_path.resolve()
    if source == target:
        raise ValueError("source_path and target_path must be different files")
    parsed_published_at = datetime.fromisoformat(published_at.replace("Z", "+00:00"))
    if parsed_published_at.utcoffset() is None:
        raise ValueError("published_at must include a timezone")
    target.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{target.name}.", suffix=".tmp", dir=target.parent
    )
    os.close(descriptor)
    temporary = Path(temporary_name)
    try:
        source_stats = _build_database(
            source=source,
            target=temporary,
            published_at=published_at,
            producer_version=BOOTSTRAP_VERSION,
            schema_version="market-data.bootstrap.v1",
        )
        as_of = str(source_stats["max_trade_date"])
        date.fromisoformat(as_of)
        row_count, session_count = _validate_database(temporary, source_stats)
        database_sha256 = _sha256(temporary)
        if target.exists():
            if _sha256(target) != database_sha256:
                raise FileExistsError(
                    f"target already exists with different content: {target}"
                )
            temporary.unlink()
        else:
            os.replace(temporary, target)

        snapshot = DataQualitySnapshotV1.create(
            as_of=as_of,
            observed_at=published_at,
            producer_version=BOOTSTRAP_VERSION,
            datasets=(DatasetQualityV1(
                dataset="stock_daily",
                status=QualityStatus.OK,
                observed_as_of=as_of,
                source_version=database_sha256,
                coverage=1.0,
                freshness_lag_sessions=0,
            ),),
        )
        marker = ReadinessStoreV1(readiness_root).publish_ready(
            bundle="v4-market-core",
            snapshot=snapshot,
            required_datasets=("stock_daily",),
            published_at=published_at,
            producer_version=BOOTSTRAP_VERSION,
        )
        return BootstrapResult(
            target, as_of, row_count, session_count, database_sha256, marker
        )
    finally:
        temporary.unlink(missing_ok=True)


def load_trading_sessions(database_path: Path) -> tuple[str, ...]:
    uri = f"{database_path.resolve().as_uri()}?mode=ro"
    with sqlite3.connect(uri, uri=True) as connection:
        rows = connection.execute(
            "SELECT trade_date FROM trading_calendar ORDER BY trade_date"
        ).fetchall()
    if not rows:
        raise ValueError("published trading_calendar is empty")
    return tuple(str(row[0]) for row in rows)


def load_market_metadata(database_path: Path) -> dict[str, str]:
    uri = f"{database_path.resolve().as_uri()}?mode=ro"
    with sqlite3.connect(uri, uri=True) as connection:
        rows = connection.execute(
            "SELECT key, value FROM platform_metadata ORDER BY key"
        ).fetchall()
    metadata = {str(key): str(value) for key, value in rows}
    required = {"schema_version", "producer_version", "published_at"}
    if not required.issubset(metadata):
        raise ValueError("published platform metadata is incomplete")
    return metadata


def _build_database(
    *, source: Path, target: Path, published_at: str,
    producer_version: str, schema_version: str,
) -> dict[str, object]:
    target_uri = f"{target.resolve().as_uri()}?mode=rw"
    with sqlite3.connect(target_uri, uri=True) as connection:
        connection.executescript("""
            PRAGMA journal_mode = DELETE;
            PRAGMA synchronous = FULL;
            CREATE TABLE stock_daily (
                stock_code TEXT NOT NULL,
                stock_name TEXT,
                trade_date TEXT NOT NULL,
                open REAL,
                high REAL,
                low REAL,
                close REAL,
                preclose REAL,
                volume REAL,
                amount REAL,
                pct_chg REAL,
                turnover REAL,
                is_st INTEGER,
                PRIMARY KEY (stock_code, trade_date)
            );
            CREATE INDEX idx_stock_daily_trade_date ON stock_daily(trade_date);
            CREATE TABLE trading_calendar (
                trade_date TEXT PRIMARY KEY
            );
            CREATE TABLE platform_metadata (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );
        """)
        source_uri = f"{source.as_uri()}?mode=ro"
        connection.execute("ATTACH DATABASE ? AS legacy", (source_uri,))
        connection.execute("BEGIN")
        source_stats = _attached_source_stats(connection)
        connection.execute("""
            INSERT INTO stock_daily
            SELECT stock_code, stock_name, trade_date, open, high, low, close,
                   preclose, volume, amount, pct_chg, turnover, is_st
            FROM legacy.stock_daily
        """)
        connection.execute(
            "INSERT INTO trading_calendar SELECT DISTINCT trade_date "
            "FROM stock_daily ORDER BY trade_date"
        )
        metadata = {
            "schema_version": schema_version,
            "producer_version": producer_version,
            "published_at": published_at,
            "source_manifest": json.dumps(source_stats, sort_keys=True, separators=(",", ":")),
        }
        connection.executemany(
            "INSERT INTO platform_metadata(key, value) VALUES (?, ?)", metadata.items()
        )
        connection.commit()
    return source_stats


def _attached_source_stats(connection: sqlite3.Connection) -> dict[str, object]:
    columns = {
        str(row[1])
        for row in connection.execute("PRAGMA legacy.table_info(stock_daily)")
    }
    required = {
        "stock_code", "stock_name", "trade_date", "open", "high", "low",
        "close", "preclose", "volume", "amount", "pct_chg", "turnover", "is_st",
    }
    missing = sorted(required - columns)
    if missing:
        raise ValueError("source stock_daily missing columns: " + ", ".join(missing))
    row = connection.execute(
        "SELECT COUNT(*), MIN(trade_date), MAX(trade_date), "
        "COUNT(DISTINCT trade_date) FROM legacy.stock_daily"
    ).fetchone()
    if not row or not row[0] or not row[1] or not row[2]:
        raise ValueError("source stock_daily is empty")
    max_trade_date = str(row[2])
    as_of_row_count = connection.execute(
        "SELECT COUNT(*) FROM legacy.stock_daily WHERE trade_date=?",
        (max_trade_date,),
    ).fetchone()[0]
    return {
        "row_count": int(row[0]),
        "min_trade_date": str(row[1]),
        "max_trade_date": max_trade_date,
        "session_count": int(row[3]),
        "as_of_row_count": int(as_of_row_count),
    }


def _validate_source_health(path: Path, as_of: str) -> dict[str, object]:
    try:
        payload = json.loads(path.resolve(strict=True).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError("source health artifact is unreadable") from exc
    if not isinstance(payload, dict):
        raise ValueError("source health artifact must be an object")
    if payload.get("trade_date") != as_of or payload.get("stock_daily_date") != as_of:
        raise ValueError("source health artifact date does not match as_of")
    if payload.get("status") != "success" or payload.get("final_gate") != "ok":
        raise ValueError("source health artifact is not ready")
    rows = payload.get("stock_daily_rows")
    if type(rows) is not int or rows <= 0:
        raise ValueError("source health artifact stock_daily_rows is invalid")
    return payload


def _published_as_of(path: Path) -> str:
    uri = f"{path.resolve().as_uri()}?mode=ro"
    with sqlite3.connect(uri, uri=True) as connection:
        row = connection.execute("SELECT MAX(trade_date) FROM stock_daily").fetchone()
    if not row or not row[0]:
        raise ValueError("existing target stock_daily is empty")
    return date.fromisoformat(str(row[0])).isoformat()


def _republish_existing_daily_target(
    *,
    target: Path,
    readiness_root: Path,
    health: dict[str, object],
    as_of: str,
    producer_version: str = TRANSITIONAL_DAILY_VERSION,
    coverage: float = 1.0,
) -> BootstrapResult:
    metadata = load_market_metadata(target)
    if metadata["producer_version"] != producer_version:
        raise FileExistsError("existing same-day target was not produced by transitional publisher")
    uri = f"{target.resolve().as_uri()}?mode=ro"
    with sqlite3.connect(uri, uri=True) as connection:
        integrity = connection.execute("PRAGMA integrity_check").fetchone()[0]
        row_count = int(connection.execute("SELECT COUNT(*) FROM stock_daily").fetchone()[0])
        session_count = int(
            connection.execute("SELECT COUNT(DISTINCT trade_date) FROM stock_daily").fetchone()[0]
        )
        as_of_rows = int(connection.execute(
            "SELECT COUNT(*) FROM stock_daily WHERE trade_date=?", (as_of,)
        ).fetchone()[0])
    if integrity != "ok" or as_of_rows != health["stock_daily_rows"]:
        raise ValueError("existing same-day target does not match source health")
    database_sha256 = _sha256(target)
    published_at = metadata["published_at"]
    snapshot = DataQualitySnapshotV1.create(
        as_of=as_of,
        observed_at=published_at,
        producer_version=producer_version,
        datasets=(DatasetQualityV1(
            dataset="stock_daily",
            status=QualityStatus.OK,
            observed_as_of=as_of,
            source_version=database_sha256,
            coverage=coverage,
            freshness_lag_sessions=0,
        ),),
    )
    marker = ReadinessStoreV1(readiness_root).publish_ready(
        bundle="v4-market-core",
        snapshot=snapshot,
        required_datasets=("stock_daily",),
        published_at=published_at,
        producer_version=producer_version,
    )
    return BootstrapResult(
        target, as_of, row_count, session_count, database_sha256, marker
    )


def _load_turnover_snapshot(path: Path, as_of: str) -> dict[str, object]:
    try:
        payload = json.loads(
            path.resolve(strict=True).read_text(encoding="utf-8")
        )
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError("turnover snapshot is unreadable") from exc
    if not isinstance(payload, dict):
        raise ValueError("turnover snapshot must be an object")
    expected = {
        "schema_version": "baostock-turnover-snapshot.v1",
        "as_of": as_of,
    }
    for key, value in expected.items():
        if payload.get(key) != value:
            raise ValueError(f"turnover snapshot {key} mismatch")
    allowed_sources = {
        ("baostock.daily", "baostock-daily-turnover.v1"),
        (
            "baostock.float-shares-derived",
            "baostock-float-share-derived-turnover.v1",
        ),
    }
    if (
        str(payload.get("source")),
        str(payload.get("source_version")),
    ) not in allowed_sources:
        raise ValueError("turnover snapshot source contract mismatch")
    fetched_at = datetime.fromisoformat(
        str(payload.get("fetched_at") or "").replace("Z", "+00:00")
    )
    if fetched_at.utcoffset() is None:
        raise ValueError("turnover snapshot fetched_at must include a timezone")
    if payload.get("units") != {
        "volume": "SHARE",
        "amount": "CNY",
        "turnover": "PERCENT",
    }:
        raise ValueError("turnover snapshot unit contract mismatch")
    if not isinstance(payload.get("summary"), dict):
        raise ValueError("turnover snapshot summary is missing")
    if not isinstance(payload.get("rows"), list):
        raise ValueError("turnover snapshot rows are missing")
    return payload


def _apply_turnover_snapshot(
    *,
    database: Path,
    snapshot: dict[str, object],
    as_of: str,
) -> None:
    with sqlite3.connect(database) as connection:
        connection.row_factory = sqlite3.Row
        source_rows = {
            str(row["stock_code"]): dict(row)
            for row in connection.execute(
                """SELECT stock_code,close,volume,amount
                   FROM stock_daily
                   WHERE trade_date=?
                   ORDER BY stock_code""",
                (as_of,),
            )
        }
        eligible = {
            code
            for code, row in source_rows.items()
            if code.startswith(("00", "30", "60", "68"))
            and _positive_number(row["volume"])
            and _positive_number(row["amount"])
        }
        updates = []
        identities = set()
        for raw in snapshot["rows"]:
            if not isinstance(raw, dict):
                raise ValueError("turnover snapshot row must be an object")
            code = str(raw.get("stock_code") or "")
            trade_date = str(raw.get("trade_date") or "")
            identity = (code, trade_date)
            if identity in identities:
                raise ValueError(
                    f"duplicate turnover snapshot row: {code} {trade_date}"
                )
            identities.add(identity)
            if trade_date != as_of:
                raise ValueError(
                    f"turnover snapshot contains wrong date for {code}"
                )
            source = source_rows.get(code)
            if source is None:
                raise ValueError(
                    f"turnover snapshot stock is absent from source: {code}"
                )
            for field in ("close", "volume", "amount"):
                overlay_value = _finite_non_negative_number(
                    raw.get(field), f"turnover snapshot {field}", code
                )
                source_value = _finite_non_negative_number(
                    source[field], f"source {field}", code
                )
                if not _same_number(
                    overlay_value, source_value, field=field
                ):
                    raise ValueError(
                        f"{field} mismatch for {code}: "
                        f"source={source_value:g}, overlay={overlay_value:g}"
                    )
            turnover = _finite_non_negative_number(
                raw.get("turnover_percent"),
                "turnover snapshot turnover_percent",
                code,
            )
            if code in eligible:
                updates.append((turnover, code, as_of))
        covered = {code for _, code, _ in updates}
        coverage = len(covered) / len(eligible) if eligible else 0.0
        declared = snapshot["summary"]
        if int(declared.get("eligible_row_count", -1)) != len(eligible):
            raise ValueError("turnover snapshot eligible row count mismatch")
        if int(declared.get("covered_row_count", -1)) != len(covered):
            raise ValueError("turnover snapshot covered row count mismatch")
        if not _same_number(
            float(declared.get("coverage", -1.0)),
            coverage,
            field="coverage",
        ):
            raise ValueError("turnover snapshot declared coverage mismatch")
        if coverage < TURNOVER_COVERAGE_MINIMUM:
            raise ValueError(
                f"turnover coverage {coverage:.6%} is below "
                f"{TURNOVER_COVERAGE_MINIMUM:.2%}"
            )
        connection.executemany(
            """UPDATE stock_daily SET turnover=?
               WHERE stock_code=? AND trade_date=?""",
            updates,
        )
        metadata = {
            "turnover_source": str(snapshot["source"]),
            "turnover_source_version": str(snapshot["source_version"]),
            "turnover_unit": "PERCENT",
            "stock_daily_volume_unit": "SHARE",
            "stock_daily_amount_unit": "CNY",
            "turnover_coverage": str(coverage),
            "turnover_covered_row_count": str(len(covered)),
            "turnover_eligible_row_count": str(len(eligible)),
        }
        connection.executemany(
            "INSERT INTO platform_metadata(key,value) VALUES (?,?)",
            metadata.items(),
        )
        connection.commit()


def _positive_number(value: object) -> bool:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return False
    return math.isfinite(number) and number > 0


def _finite_non_negative_number(
    value: object, label: str, stock_code: str,
) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(
            f"invalid {label} for {stock_code}: {value!r}"
        ) from exc
    if not math.isfinite(number) or number < 0:
        raise ValueError(f"invalid {label} for {stock_code}: {value!r}")
    return number


def _same_number(left: float, right: float, *, field: str) -> bool:
    tolerance = 1e-9 if field == "volume" else 1e-6
    return math.isclose(left, right, rel_tol=tolerance, abs_tol=1e-8)


def _validate_database(
    database: Path, source_stats: dict[str, object]
) -> tuple[int, int]:
    with sqlite3.connect(database) as connection:
        integrity = connection.execute("PRAGMA integrity_check").fetchone()[0]
        if integrity != "ok":
            raise ValueError(f"published database integrity check failed: {integrity}")
        row_count = int(connection.execute("SELECT COUNT(*) FROM stock_daily").fetchone()[0])
        session_count = int(
            connection.execute("SELECT COUNT(*) FROM trading_calendar").fetchone()[0]
        )
        bounds = connection.execute(
            "SELECT MIN(trade_date), MAX(trade_date) FROM stock_daily"
        ).fetchone()
        tables = {
            str(row[0]) for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
            )
        }
        columns = {
            table: {
                str(row[1]) for row in connection.execute(f"PRAGMA table_info({table})")
            }
            for table in ("stock_daily", "trading_calendar", "platform_metadata")
        }
    if tables != {"stock_daily", "trading_calendar", "platform_metadata"}:
        raise ValueError(f"unexpected published tables: {sorted(tables)}")
    expected_columns = {
        "stock_daily": {
            "stock_code", "stock_name", "trade_date", "open", "high", "low",
            "close", "preclose", "volume", "amount", "pct_chg", "turnover", "is_st",
        },
        "trading_calendar": {"trade_date"},
        "platform_metadata": {"key", "value"},
    }
    if columns != expected_columns:
        raise ValueError("published database column contract mismatch")
    if row_count != source_stats["row_count"]:
        raise ValueError("published stock_daily row count differs from source")
    if session_count != source_stats["session_count"]:
        raise ValueError("published trading session count differs from source")
    if tuple(bounds) != (
        source_stats["min_trade_date"], source_stats["max_trade_date"]
    ):
        raise ValueError("published stock_daily date bounds differ from source")
    return row_count, session_count


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()
