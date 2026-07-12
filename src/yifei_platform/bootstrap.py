from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import tempfile

from .quality import DataQualitySnapshotV1, DatasetQualityV1, QualityStatus
from .readiness import ReadinessMarkerV1, ReadinessStoreV1


BOOTSTRAP_VERSION = "bootstrap-market-data.v1"


@dataclass(frozen=True)
class BootstrapResult:
    target_path: Path
    as_of: str
    row_count: int
    session_count: int
    database_sha256: str
    readiness_marker: ReadinessMarkerV1


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


def _build_database(
    *, source: Path, target: Path, published_at: str,
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
            "schema_version": "market-data.bootstrap.v1",
            "producer_version": BOOTSTRAP_VERSION,
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
    return {
        "row_count": int(row[0]),
        "min_trade_date": str(row[1]),
        "max_trade_date": str(row[2]),
        "session_count": int(row[3]),
    }


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
    if tables != {"stock_daily", "trading_calendar", "platform_metadata"}:
        raise ValueError(f"unexpected published tables: {sorted(tables)}")
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
