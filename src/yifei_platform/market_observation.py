from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import json
import math
import os
from pathlib import Path
import shutil
import sqlite3
from statistics import median, pstdev
import tempfile
from typing import Mapping

from .bootstrap import _publication_lock, _sha256, load_market_metadata


MARKET_OBSERVATION_MIGRATION_VERSION = "market-observation-migration.v1"
MARKET_OBSERVATION_SCHEMA_VERSION = "market-observation-facts.v1"
CSI300_CODE = "000300.SH"


@dataclass(frozen=True)
class MarketObservationMigrationResultV1:
    target_path: Path
    index_row_count: int
    breadth_row_count: int
    min_trade_date: str
    max_trade_date: str
    database_sha256: str


def migrate_market_observation_facts_v1(
    *,
    target_path: Path,
    legacy_index_path: Path,
    published_at: str,
    latest_index_row: Mapping[str, object] | None = None,
    latest_index_source_version: str | None = None,
) -> MarketObservationMigrationResultV1:
    timestamp = datetime.fromisoformat(published_at.replace("Z", "+00:00"))
    if timestamp.utcoffset() is None:
        raise ValueError("published_at must include a timezone")
    target = target_path.resolve(strict=True)
    legacy = legacy_index_path.resolve(strict=True)

    with _publication_lock(target_path):
        load_market_metadata(target)
        with sqlite3.connect(f"{target.as_uri()}?mode=ro", uri=True) as current:
            already_migrated = current.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' "
                "AND name='market_breadth_daily'"
            ).fetchone()
            if already_migrated and current.execute(
                "SELECT 1 FROM market_breadth_daily LIMIT 1"
            ).fetchone():
                raise FileExistsError("market observation facts already migrated")
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=f".{target.name}.", suffix=".tmp", dir=target.parent
        )
        os.close(descriptor)
        temporary = Path(temporary_name)
        try:
            shutil.copy2(target, temporary)
            with sqlite3.connect(temporary) as connection:
                _create_schema(connection)
                _migrate_legacy_index(connection, legacy)
                target_as_of = str(connection.execute(
                    "SELECT MAX(trade_date) FROM stock_daily"
                ).fetchone()[0])
                if connection.execute(
                    "SELECT 1 FROM index_daily WHERE index_code=? AND trade_date=?",
                    (CSI300_CODE, target_as_of),
                ).fetchone() is None and latest_index_row is not None:
                    connection.execute("BEGIN IMMEDIATE")
                    _append_index_row(
                        connection,
                        as_of=target_as_of,
                        raw=latest_index_row,
                        source_version=(latest_index_source_version or "unknown"),
                    )
                    connection.commit()
                _rebuild_market_breadth(connection)
                metadata = load_market_metadata(temporary)
                manifest = {
                    "prior_producer_version": metadata["producer_version"],
                    "legacy_index_path": str(legacy),
                    "legacy_index_table": "index_daily",
                    "index_code": CSI300_CODE,
                    "breadth_source": "platform:stock_daily",
                }
                connection.execute("BEGIN IMMEDIATE")
                connection.execute(
                    "INSERT OR REPLACE INTO platform_metadata(key,value) VALUES (?,?)",
                    ("schema_version", MARKET_OBSERVATION_SCHEMA_VERSION),
                )
                connection.execute(
                    "INSERT OR REPLACE INTO platform_metadata(key,value) VALUES (?,?)",
                    ("producer_version", MARKET_OBSERVATION_MIGRATION_VERSION),
                )
                connection.execute(
                    "INSERT OR REPLACE INTO platform_metadata(key,value) VALUES (?,?)",
                    ("published_at", published_at),
                )
                connection.execute(
                    "INSERT OR REPLACE INTO platform_metadata(key,value) VALUES (?,?)",
                    ("market_observation_manifest", json.dumps(
                        manifest, sort_keys=True, separators=(",", ":")
                    )),
                )
                connection.commit()
                _validate_observation_tables(connection)
            os.replace(temporary, target)
        finally:
            temporary.unlink(missing_ok=True)

    with sqlite3.connect(target) as connection:
        index_count = int(connection.execute(
            "SELECT COUNT(*) FROM index_daily WHERE index_code=?", (CSI300_CODE,)
        ).fetchone()[0])
        breadth = connection.execute(
            "SELECT COUNT(*),MIN(trade_date),MAX(trade_date) "
            "FROM market_breadth_daily"
        ).fetchone()
    return MarketObservationMigrationResultV1(
        target_path=target,
        index_row_count=index_count,
        breadth_row_count=int(breadth[0]),
        min_trade_date=str(breadth[1]),
        max_trade_date=str(breadth[2]),
        database_sha256=_sha256(target),
    )


def append_market_observation_facts_v1(
    connection: sqlite3.Connection,
    *,
    as_of: str,
    index_row: Mapping[str, object] | None,
    index_source_version: str | None,
) -> tuple[bool, bool]:
    if index_row is not None:
        _append_index_row(
            connection,
            as_of=as_of,
            raw=index_row,
            source_version=index_source_version or "unknown",
        )
    breadth_written = _upsert_breadth_for_date(connection, as_of)
    index_written = connection.execute(
        "SELECT 1 FROM index_daily WHERE index_code=? AND trade_date=?",
        (CSI300_CODE, as_of),
    ).fetchone() is not None
    return breadth_written, index_written


def initialize_market_observation_schema_v1(
    connection: sqlite3.Connection,
) -> None:
    _create_schema(connection)


def _create_schema(connection: sqlite3.Connection) -> None:
    connection.executescript("""
        CREATE TABLE IF NOT EXISTS index_daily (
            index_code TEXT NOT NULL,
            trade_date TEXT NOT NULL,
            open REAL,
            high REAL,
            low REAL,
            close REAL NOT NULL,
            preclose REAL,
            volume REAL,
            amount REAL,
            pct_chg REAL,
            return_20d_pct REAL,
            realized_vol_10d_pct REAL,
            source_version TEXT NOT NULL,
            PRIMARY KEY (index_code, trade_date)
        );
        CREATE INDEX IF NOT EXISTS idx_index_daily_trade_date
            ON index_daily(trade_date);
        CREATE TABLE IF NOT EXISTS market_breadth_daily (
            trade_date TEXT PRIMARY KEY,
            advance_count INTEGER NOT NULL,
            decline_count INTEGER NOT NULL,
            flat_count INTEGER NOT NULL,
            valid_return_count INTEGER NOT NULL,
            advance_share REAL,
            equal_weight_return_pct REAL,
            pct_ge_3_share REAL,
            pct_le_minus_8_share REAL,
            above_ma20_share REAL,
            ma20_eligible_stock_count INTEGER NOT NULL,
            total_amount REAL,
            amount_ratio_vs_prior20_median REAL,
            source_version TEXT NOT NULL
        );
    """)


def _migrate_legacy_index(connection: sqlite3.Connection, legacy: Path) -> None:
    source_uri = f"{legacy.as_uri()}?mode=ro"
    with sqlite3.connect(source_uri, uri=True) as source:
        columns = {
            str(row[1])
            for row in source.execute("PRAGMA table_info(index_daily)")
        }
        required = {"date", "open", "close", "high", "low", "volume", "amount"}
        if not required.issubset(columns):
            raise ValueError("legacy index_daily contract mismatch")
        rows = source.execute(
            "SELECT date,open,high,low,close,volume,amount "
            "FROM index_daily ORDER BY date"
        ).fetchall()
    if not rows:
        raise ValueError("legacy index_daily is empty")

    connection.execute("BEGIN IMMEDIATE")
    connection.execute("DELETE FROM index_daily WHERE index_code=?", (CSI300_CODE,))
    closes: list[float] = []
    returns: list[float] = []
    inserts = []
    prior_close: float | None = None
    for raw in rows:
        close = _positive(raw[4], "legacy index close")
        pct_chg = (
            (close / prior_close - 1) * 100 if prior_close is not None else None
        )
        if prior_close is not None:
            returns.append(math.log(close / prior_close))
        closes.append(close)
        return_20d = (
            (close / closes[-21] - 1) * 100 if len(closes) >= 21 else None
        )
        realized_vol = (
            pstdev(returns[-10:]) * math.sqrt(252) * 100
            if len(returns) >= 10 else None
        )
        inserts.append((
            CSI300_CODE, str(raw[0]), raw[1], raw[2], raw[3], close,
            prior_close, raw[5], raw[6], pct_chg, return_20d, realized_vol,
            "legacy-v3:akshare-sina-csi300.v1",
        ))
        prior_close = close
    connection.executemany(
        """INSERT INTO index_daily (
               index_code,trade_date,open,high,low,close,preclose,volume,amount,
               pct_chg,return_20d_pct,realized_vol_10d_pct,source_version
           ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        inserts,
    )
    connection.commit()


def _rebuild_market_breadth(connection: sqlite3.Connection) -> None:
    aggregate_rows = connection.execute("""
        WITH calendar_sequence AS (
            SELECT trade_date,
                   ROW_NUMBER() OVER (ORDER BY trade_date) AS session_sequence
            FROM trading_calendar
        ), stock_windows AS (
            SELECT stock_code,stock_daily.trade_date,close,preclose,amount,
                   session_sequence,
                   AVG(CASE WHEN close > 0 THEN close END) OVER (
                       PARTITION BY stock_code ORDER BY trade_date
                       ROWS BETWEEN 19 PRECEDING AND CURRENT ROW
                   ) AS ma20,
                   COUNT(CASE WHEN close > 0 THEN 1 END) OVER (
                       PARTITION BY stock_code ORDER BY trade_date
                       ROWS BETWEEN 19 PRECEDING AND CURRENT ROW
                   ) AS ma20_count,
                   MIN(session_sequence) OVER (
                       PARTITION BY stock_code ORDER BY stock_daily.trade_date
                       ROWS BETWEEN 19 PRECEDING AND CURRENT ROW
                   ) AS ma20_first_sequence
            FROM stock_daily
            JOIN calendar_sequence USING (trade_date)
        )
        SELECT trade_date,
               SUM(CASE WHEN close > preclose AND preclose > 0 THEN 1 ELSE 0 END),
               SUM(CASE WHEN close < preclose AND close > 0 AND preclose > 0 THEN 1 ELSE 0 END),
               SUM(CASE WHEN close = preclose AND close > 0 THEN 1 ELSE 0 END),
               SUM(CASE WHEN close > 0 AND preclose > 0 THEN 1 ELSE 0 END),
               AVG(CASE WHEN close > 0 AND preclose > 0 THEN close / preclose - 1 END) * 100,
               SUM(CASE WHEN close > 0 AND preclose > 0 AND
                    (close / preclose - 1) * 100 >= 3 THEN 1 ELSE 0 END),
               SUM(CASE WHEN close > 0 AND preclose > 0 AND
                    (close / preclose - 1) * 100 <= -8 THEN 1 ELSE 0 END),
               SUM(CASE WHEN ma20_count = 20 AND
                    session_sequence - ma20_first_sequence = 19 AND
                    close > 0 AND close >= ma20 THEN 1 ELSE 0 END),
               SUM(CASE WHEN ma20_count = 20 AND
                    session_sequence - ma20_first_sequence = 19 AND
                    close > 0 THEN 1 ELSE 0 END),
               SUM(CASE WHEN amount > 0 THEN amount ELSE 0 END)
        FROM stock_windows GROUP BY trade_date ORDER BY trade_date
    """).fetchall()
    connection.execute("BEGIN IMMEDIATE")
    connection.execute("DELETE FROM market_breadth_daily")
    prior_amounts: list[float | None] = []
    for row in aggregate_rows:
        valid_count = int(row[4])
        advance_count = int(row[1])
        amount = float(row[10]) if row[10] is not None else 0.0
        prior_window = prior_amounts[-20:]
        amount_ratio = None
        if len(prior_window) == 20 and all(
            value is not None and value > 0 for value in prior_window
        ):
            amount_ratio = amount / median(prior_window) if amount > 0 else None
        connection.execute(
            """INSERT INTO market_breadth_daily VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                row[0], advance_count, int(row[2]), int(row[3]), valid_count,
                advance_count / valid_count if valid_count else None,
                row[5], int(row[6]) / valid_count if valid_count else None,
                int(row[7]) / valid_count if valid_count else None,
                int(row[8]) / int(row[9]) if int(row[9]) else None,
                int(row[9]), amount if amount > 0 else None, amount_ratio,
                "platform:stock_daily:breadth.v1",
            ),
        )
        prior_amounts.append(amount if amount > 0 else None)
    connection.commit()


def _append_index_row(
    connection: sqlite3.Connection,
    *, as_of: str,
    raw: Mapping[str, object],
    source_version: str,
) -> None:
    raw_date = str(raw.get("date") or raw.get("日期") or "")[:10]
    if raw_date != as_of:
        raise ValueError("CSI 300 row does not match as_of")
    close = _positive(raw.get("close", raw.get("收盘")), "CSI 300 close")
    prior_sessions = [
        str(row[0])
        for row in connection.execute(
            "SELECT trade_date FROM trading_calendar WHERE trade_date<? "
            "ORDER BY trade_date DESC LIMIT 20",
            (as_of,),
        )
    ]
    history_by_date = {
        str(row[0]): float(row[1])
        for row in connection.execute(
            "SELECT trade_date,close FROM index_daily WHERE index_code=? "
            "AND trade_date IN (SELECT trade_date FROM trading_calendar "
            "WHERE trade_date<? ORDER BY trade_date DESC LIMIT 20)",
            (CSI300_CODE, as_of),
        )
    }
    history = [history_by_date[day] for day in prior_sessions if day in history_by_date]
    prior_close = history_by_date.get(prior_sessions[0]) if prior_sessions else None
    pct_chg = (close / prior_close - 1) * 100 if prior_close else None
    return_20d = (
        (close / history[19] - 1) * 100 if len(history) == 20 else None
    )
    recent_history = [
        history_by_date[day]
        for day in prior_sessions[:10]
        if day in history_by_date
    ]
    closes = list(reversed(recent_history)) + [close]
    log_returns = [math.log(closes[i] / closes[i - 1]) for i in range(1, len(closes))]
    realized_vol = (
        pstdev(log_returns[-10:]) * math.sqrt(252) * 100
        if len(recent_history) == 10 else None
    )
    connection.execute(
        """INSERT INTO index_daily (
               index_code,trade_date,open,high,low,close,preclose,volume,amount,
               pct_chg,return_20d_pct,realized_vol_10d_pct,source_version
           ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            CSI300_CODE, as_of,
            raw.get("open", raw.get("开盘")), raw.get("high", raw.get("最高")),
            raw.get("low", raw.get("最低")), close, prior_close,
            raw.get("volume", raw.get("成交量")), raw.get("amount", raw.get("成交额")),
            pct_chg, return_20d, realized_vol, source_version,
        ),
    )


def _upsert_breadth_for_date(connection: sqlite3.Connection, as_of: str) -> bool:
    sessions = [
        str(row[0])
        for row in connection.execute(
            "SELECT trade_date FROM trading_calendar WHERE trade_date<=? "
            "ORDER BY trade_date DESC LIMIT 21",
            (as_of,),
        )
    ]
    if not sessions or sessions[0] != as_of:
        return False
    sessions.reverse()
    placeholders = ",".join("?" for _ in sessions)
    rows = connection.execute(
        f"SELECT stock_code,trade_date,close,preclose,pct_chg,amount "
        f"FROM stock_daily WHERE trade_date IN ({placeholders})",
        sessions,
    ).fetchall()
    by_code: dict[str, dict[str, sqlite3.Row | tuple]] = {}
    current_rows = []
    for row in rows:
        by_code.setdefault(str(row[0]), {})[str(row[1])] = row
        if str(row[1]) == as_of:
            current_rows.append(row)
    valid = [
        row for row in current_rows
        if row[2] is not None and float(row[2]) > 0
        and row[3] is not None and float(row[3]) > 0
    ]
    valid_count = len(valid)
    advances = sum(float(row[2]) > float(row[3]) for row in valid)
    declines = sum(float(row[2]) < float(row[3]) for row in valid)
    flats = sum(float(row[2]) == float(row[3]) for row in valid)
    returns = [(float(row[2]) / float(row[3]) - 1) * 100 for row in valid]
    above: list[bool] = []
    ma20_sessions = sessions[-20:]
    if len(ma20_sessions) == 20:
        for row in current_rows:
            history = [by_code[str(row[0])].get(day) for day in ma20_sessions]
            closes = [
                float(item[2])
                for item in history
                if item is not None and item[2] is not None and float(item[2]) > 0
            ]
            if len(closes) == 20:
                above.append(float(row[2]) >= sum(closes) / 20)
    amount = sum(
        float(row[5]) for row in current_rows
        if row[5] is not None and float(row[5]) > 0
    )
    prior_sessions = [
        str(row[0])
        for row in connection.execute(
            "SELECT trade_date FROM trading_calendar WHERE trade_date<? "
            "ORDER BY trade_date DESC LIMIT 20",
            (as_of,),
        )
    ]
    prior_amount_by_date = {
        str(row[0]): row[1]
        for row in connection.execute(
            "SELECT trade_date,total_amount FROM market_breadth_daily "
            "WHERE trade_date IN (SELECT trade_date FROM trading_calendar "
            "WHERE trade_date<? ORDER BY trade_date DESC LIMIT 20)",
            (as_of,),
        )
    }
    prior_amounts = [prior_amount_by_date.get(day) for day in prior_sessions]
    amount_ratio = (
        amount / median(prior_amounts)
        if amount > 0 and len(prior_amounts) == 20
        and all(value is not None and float(value) > 0 for value in prior_amounts)
        else None
    )
    connection.execute(
        """INSERT INTO market_breadth_daily VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            as_of, advances, declines, flats, valid_count,
            advances / valid_count if valid_count else None,
            sum(returns) / valid_count if valid_count else None,
            sum(value >= 3 for value in returns) / valid_count if valid_count else None,
            sum(value <= -8 for value in returns) / valid_count if valid_count else None,
            sum(above) / len(above) if above else None,
            len(above), amount if amount > 0 else None, amount_ratio,
            "platform:stock_daily:breadth.v1",
        ),
    )
    return True


def _validate_observation_tables(connection: sqlite3.Connection) -> None:
    integrity = connection.execute("PRAGMA integrity_check").fetchone()[0]
    if integrity != "ok":
        raise ValueError(f"market observation migration integrity failed: {integrity}")
    stock_bounds = connection.execute(
        "SELECT MIN(trade_date),MAX(trade_date),COUNT(DISTINCT trade_date) FROM stock_daily"
    ).fetchone()
    breadth_bounds = connection.execute(
        "SELECT MIN(trade_date),MAX(trade_date),COUNT(*) FROM market_breadth_daily"
    ).fetchone()
    if tuple(stock_bounds) != tuple(breadth_bounds):
        raise ValueError("market breadth session coverage differs from stock_daily")


def _positive(value: object, field: str) -> float:
    number = float(value)
    if not math.isfinite(number) or number <= 0:
        raise ValueError(f"{field} must be positive")
    return number
