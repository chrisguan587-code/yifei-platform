from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from contextlib import contextmanager
import fcntl
from functools import wraps
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import tempfile

from .quality import DataQualitySnapshotV1, DatasetQualityV1, QualityStatus
from .readiness import ReadinessMarkerV1, ReadinessStoreV1


BOOTSTRAP_VERSION = "bootstrap-market-data.v1"
TRANSITIONAL_DAILY_VERSION = "transitional-daily-market-data.v1"


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
    *, target: Path, readiness_root: Path, health: dict[str, object], as_of: str,
) -> BootstrapResult:
    metadata = load_market_metadata(target)
    if metadata["producer_version"] != TRANSITIONAL_DAILY_VERSION:
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
        producer_version=TRANSITIONAL_DAILY_VERSION,
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
        producer_version=TRANSITIONAL_DAILY_VERSION,
    )
    return BootstrapResult(
        target, as_of, row_count, session_count, database_sha256, marker
    )


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
