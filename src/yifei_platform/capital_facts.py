from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path
import math
import os
import random
import shutil
import sqlite3
import tempfile
from typing import Mapping, Protocol, Sequence, cast

from .supplemental_facts import initialize_supplemental_database_v1, serialized_supplemental_publication_v1

CAPITAL_FACTS_SCHEMA_VERSION = "shared-capital-facts.v1"
MOOTDX_FLOAT_SHARES_SOURCE = "mootdx.finance.liutongguben"
MOOTDX_FLOAT_SHARES_SOURCE_VERSION = "mootdx-finance-liutongguben.v1"
BAOSTOCK_FLOAT_SHARES_SOURCE = "baostock.query_profit_data.liqaShare"
BAOSTOCK_FLOAT_SHARES_SOURCE_VERSION = "baostock-profit-liqashare.v1"
FLOAT_MARKET_CAP_SOURCE_VERSION = "shared-float-market-cap.v1"
FLOAT_SHARES_MINIMUM_COVERAGE = 0.98


@dataclass(frozen=True)
class FloatShareSourceRowV1:
    stock_code: str
    float_shares: float
    source_date: str | None = None


@dataclass(frozen=True)
class CapitalPublicationResultV1:
    target_path: Path
    as_of: str
    dataset: str
    row_count: int
    expected_count: int
    coverage: float
    health_status: str
    missing_count: int
    anomaly_count: int
    source: str
    source_version: str
    elapsed_seconds: float | None = None


class FloatShareClientV1(Protocol):
    source: str
    source_version: str

    def read_float_shares(
        self, stock_codes: Sequence[str]
    ) -> Mapping[str, FloatShareSourceRowV1]: ...


class MootdxFloatShareClientV1:
    source = MOOTDX_FLOAT_SHARES_SOURCE
    source_version = MOOTDX_FLOAT_SHARES_SOURCE_VERSION

    def __init__(self) -> None:
        from mootdx.quotes import Quotes

        self._client = Quotes.factory(market="std")

    def read_float_shares(
        self, stock_codes: Sequence[str]
    ) -> Mapping[str, FloatShareSourceRowV1]:
        rows: dict[str, FloatShareSourceRowV1] = {}
        for code in stock_codes:
            payload = self._client.finance(symbol=code)
            record: dict[str, object]
            if hasattr(payload, "empty") and payload.empty:
                record = {}
            elif hasattr(payload, "iloc"):
                record = dict(payload.iloc[0].to_dict())
            elif isinstance(payload, dict):
                record = payload
            else:
                record = {}
            if "liutongguben" not in record:
                continue
            raw_float_shares = record["liutongguben"]
            updated_date = record.get("updated_date")
            rows[code] = FloatShareSourceRowV1(
                stock_code=code,
                float_shares=float(round(float(raw_float_shares))),
                source_date=_tdx_date(updated_date),
            )
        return rows


class BaoStockFloatShareClientV1:
    source = BAOSTOCK_FLOAT_SHARES_SOURCE
    source_version = BAOSTOCK_FLOAT_SHARES_SOURCE_VERSION

    def __init__(self, *, quarters: Sequence[tuple[int, int]] | None = None) -> None:
        self._quarters = tuple(quarters or _default_recent_quarters())

    def read_float_shares(
        self, stock_codes: Sequence[str]
    ) -> Mapping[str, FloatShareSourceRowV1]:
        import baostock as bs

        rows: dict[str, FloatShareSourceRowV1] = {}
        login = bs.login()
        if getattr(login, "error_code", "") != "0":
            raise RuntimeError("baostock login failed")
        try:
            for code in stock_codes:
                for year, quarter in self._quarters:
                    rs = bs.query_profit_data(
                        code=_baostock_code(code), year=year, quarter=quarter
                    )
                    while rs.error_code == "0" and rs.next():
                        data = dict(zip(rs.fields, rs.get_row_data()))
                        value = data.get("liqaShare")
                        if value not in (None, ""):
                            rows[code] = FloatShareSourceRowV1(
                                stock_code=code,
                                float_shares=float(value),
                                source_date=str(data.get("statDate") or "") or None,
                            )
                            break
                    if code in rows:
                        break
        finally:
            bs.logout()
        return rows


@serialized_supplemental_publication_v1
def publish_float_shares_weekly_v1(
    *,
    client: FloatShareClientV1,
    market_database_path: Path,
    target_path: Path,
    as_of: str,
    updated_at: str,
    minimum_coverage: float = FLOAT_SHARES_MINIMUM_COVERAGE,
    backup_client: FloatShareClientV1 | None = None,
    backup_sample_size: int = 80,
    anomaly_rel_diff_threshold: float = 0.01,
) -> CapitalPublicationResultV1:
    import time

    started = time.perf_counter()
    session = date.fromisoformat(as_of).isoformat()
    timestamp = _validate_timestamp(updated_at)
    if not 0 <= minimum_coverage <= 1:
        raise ValueError("minimum_coverage must be between 0 and 1")
    universe = _eligible_universe(market_database_path, session)
    codes = [row["stock_code"] for row in universe]
    source_rows = client.read_float_shares(codes)
    fetched: list[tuple[object, ...]] = []
    missing: list[tuple[object, ...]] = []
    anomalies: list[tuple[object, ...]] = []
    for item in universe:
        code = item["stock_code"]
        row = source_rows.get(code)
        if row is None:
            missing.append((session, code, item["stock_name"], "missing_source_row"))
            continue
        reason = _float_share_anomaly_reason(row.float_shares)
        if reason:
            anomalies.append((
                session, code, item["stock_name"], reason, row.float_shares,
                None, None, client.source, client.source_version, timestamp,
            ))
            continue
        fetched.append((
            code, item["stock_name"], session, float(row.float_shares),
            "SHARE", client.source, client.source_version,
            row.source_date or session, timestamp, "fresh", "READY",
        ))
    if backup_client is not None and fetched and backup_sample_size > 0:
        sample_codes = _deterministic_sample(
            [str(row[0]) for row in fetched], min(backup_sample_size, len(fetched))
        )
        backup_rows = backup_client.read_float_shares(sample_codes)
        primary = {str(row[0]): float(row[3]) for row in fetched}
        names = {item["stock_code"]: item["stock_name"] for item in universe}
        for code in sample_codes:
            backup = backup_rows.get(code)
            if backup is None:
                anomalies.append((
                    session, code, names.get(code), "backup_missing", primary[code],
                    None, None, backup_client.source, backup_client.source_version,
                    timestamp,
                ))
                continue
            rel = abs(primary[code] - backup.float_shares) / primary[code]
            if rel > anomaly_rel_diff_threshold:
                anomalies.append((
                    session, code, names.get(code), "backup_diff_gt_threshold",
                    primary[code], backup.float_shares, rel,
                    backup_client.source, backup_client.source_version, timestamp,
                ))
    coverage = len(fetched) / len(universe) if universe else 0.0
    health = _health_status(coverage, minimum_coverage)
    target = target_path.resolve()
    target.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{target.name}.", suffix=".tmp", dir=target.parent
    )
    os.close(descriptor)
    temporary = Path(temporary_name)
    temporary.unlink(missing_ok=True)
    temporary.touch()
    try:
        if target.exists():
            shutil.copy2(target, temporary)
        initialize_supplemental_database_v1(temporary)
        _initialize_capital_schema_v1(temporary)
        with sqlite3.connect(temporary) as connection:
            connection.execute("BEGIN")
            connection.execute("DELETE FROM stock_float_shares WHERE as_of=?", (session,))
            connection.execute("DELETE FROM capital_data_quality WHERE as_of=? AND dataset='float_shares'", (session,))
            connection.execute("DELETE FROM capital_float_share_missing WHERE as_of=?", (session,))
            connection.execute("DELETE FROM capital_float_share_anomalies WHERE as_of=?", (session,))
            connection.executemany(
                """INSERT INTO stock_float_shares (
                       stock_code, stock_name, as_of, float_shares, share_unit,
                       source, source_version, source_date, updated_at,
                       freshness, health_status
                   ) VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
                fetched,
            )
            connection.executemany(
                "INSERT INTO capital_float_share_missing VALUES (?,?,?,?)",
                missing,
            )
            connection.executemany(
                "INSERT INTO capital_float_share_anomalies VALUES (?,?,?,?,?,?,?,?,?,?)",
                anomalies,
            )
            connection.execute(
                """INSERT INTO capital_data_quality (
                       dataset, as_of, expected_count, row_count, coverage,
                       missing_count, anomaly_count, freshness, health_status,
                       source, source_version, updated_at
                   ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    "float_shares", session, len(universe), len(fetched), coverage,
                    len(missing), len(anomalies), "fresh", health,
                    client.source, client.source_version, timestamp,
                ),
            )
            connection.execute(
                """INSERT INTO supplemental_metadata(key, value)
                   VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value""",
                ("float_shares_latest_as_of", session),
            )
            connection.commit()
            _require_integrity(connection)
        shutil.move(temporary, target)
    finally:
        temporary.unlink(missing_ok=True)
    return CapitalPublicationResultV1(
        target_path=target,
        as_of=session,
        dataset="float_shares",
        row_count=len(fetched),
        expected_count=len(universe),
        coverage=coverage,
        health_status=health,
        missing_count=len(missing),
        anomaly_count=len(anomalies),
        source=client.source,
        source_version=client.source_version,
        elapsed_seconds=time.perf_counter() - started,
    )


@serialized_supplemental_publication_v1
def publish_float_market_cap_daily_v1(
    *,
    market_database_path: Path,
    target_path: Path,
    as_of: str,
    updated_at: str,
    minimum_coverage: float = FLOAT_SHARES_MINIMUM_COVERAGE,
) -> CapitalPublicationResultV1:
    session = date.fromisoformat(as_of).isoformat()
    timestamp = _validate_timestamp(updated_at)
    universe = _eligible_universe(market_database_path, session)
    prices = {row["stock_code"]: float(row["close"]) for row in universe}
    names = {row["stock_code"]: row["stock_name"] for row in universe}
    target = target_path.resolve()
    if not target.exists():
        raise FileNotFoundError("shared supplemental database is missing")
    with sqlite3.connect(target) as read_connection:
        read_connection.row_factory = sqlite3.Row
        _initialize_capital_schema_connection_v1(read_connection)
        latest_row = read_connection.execute(
            "SELECT MAX(as_of) FROM stock_float_shares WHERE as_of<=?", (session,)
        ).fetchone()
        latest_shares_as_of = str(latest_row[0]) if latest_row and latest_row[0] else None
        if latest_shares_as_of is None:
            source_rows: list[sqlite3.Row] = []
        else:
            source_rows = read_connection.execute(
                """SELECT stock_code,float_shares,share_unit,source,source_version,source_date
                   FROM stock_float_shares WHERE as_of=? AND health_status='READY'""",
                (latest_shares_as_of,),
            ).fetchall()
    shares = {str(row["stock_code"]): row for row in source_rows}
    rows: list[tuple[object, ...]] = []
    missing: list[tuple[object, ...]] = []
    for code, close in prices.items():
        row = shares.get(code)
        if row is None:
            missing.append((session, code, names.get(code), "float_shares_missing"))
            continue
        if str(row["share_unit"]) != "SHARE":
            missing.append((session, code, names.get(code), "unsupported_share_unit"))
            continue
        float_shares = float(row["float_shares"])
        if close <= 0 or float_shares <= 0:
            missing.append((session, code, names.get(code), "invalid_price_or_float_shares"))
            continue
        rows.append((
            code, names.get(code), session, latest_shares_as_of, close,
            float_shares, close * float_shares, "CNY",
            str(row["source"]), str(row["source_version"]),
            str(row["source_date"]), timestamp,
            _freshness(session, latest_shares_as_of), "READY",
        ))
    coverage = len(rows) / len(universe) if universe else 0.0
    health = _health_status(coverage, minimum_coverage)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{target.name}.", suffix=".tmp", dir=target.parent
    )
    os.close(descriptor)
    temporary = Path(temporary_name)
    temporary.unlink(missing_ok=True)
    shutil.copy2(target, temporary)
    try:
        _initialize_capital_schema_v1(temporary)
        with sqlite3.connect(temporary) as connection:
            connection.execute("BEGIN")
            connection.execute("DELETE FROM stock_float_market_cap_daily WHERE trade_date=?", (session,))
            connection.execute("DELETE FROM capital_data_quality WHERE as_of=? AND dataset='float_market_cap'", (session,))
            connection.execute("DELETE FROM capital_float_share_missing WHERE as_of=? AND reason LIKE 'float_shares%'", (session,))
            connection.executemany(
                """INSERT INTO stock_float_market_cap_daily (
                       stock_code, stock_name, trade_date, float_shares_as_of,
                       close, float_shares, float_market_cap, market_cap_unit,
                       float_shares_source, float_shares_source_version,
                       float_shares_source_date, updated_at, freshness,
                       health_status
                   ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                rows,
            )
            connection.executemany(
                "INSERT INTO capital_float_share_missing VALUES (?,?,?,?)",
                missing,
            )
            connection.execute(
                """INSERT INTO capital_data_quality (
                       dataset, as_of, expected_count, row_count, coverage,
                       missing_count, anomaly_count, freshness, health_status,
                       source, source_version, updated_at
                   ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    "float_market_cap", session, len(universe), len(rows),
                    coverage, len(missing), 0,
                    _freshness(session, latest_shares_as_of), health,
                    "market_data.close_x_shared_float_shares",
                    FLOAT_MARKET_CAP_SOURCE_VERSION, timestamp,
                ),
            )
            connection.execute(
                """INSERT INTO supplemental_metadata(key, value)
                   VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value""",
                ("float_market_cap_latest_as_of", session),
            )
            connection.commit()
            _require_integrity(connection)
        shutil.move(temporary, target)
    finally:
        temporary.unlink(missing_ok=True)
    return CapitalPublicationResultV1(
        target_path=target,
        as_of=session,
        dataset="float_market_cap",
        row_count=len(rows),
        expected_count=len(universe),
        coverage=coverage,
        health_status=health,
        missing_count=len(missing),
        anomaly_count=0,
        source="market_data.close_x_shared_float_shares",
        source_version=FLOAT_MARKET_CAP_SOURCE_VERSION,
    )


def _initialize_capital_schema_v1(path: Path) -> None:
    with sqlite3.connect(path) as connection:
        _initialize_capital_schema_connection_v1(connection)
        connection.commit()


def _initialize_capital_schema_connection_v1(connection: sqlite3.Connection) -> None:
    connection.executescript(
        """
        CREATE TABLE IF NOT EXISTS stock_float_shares (
            stock_code TEXT NOT NULL,
            stock_name TEXT,
            as_of TEXT NOT NULL,
            float_shares REAL NOT NULL,
            share_unit TEXT NOT NULL,
            source TEXT NOT NULL,
            source_version TEXT NOT NULL,
            source_date TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            freshness TEXT NOT NULL,
            health_status TEXT NOT NULL,
            PRIMARY KEY(stock_code, as_of)
        );
        CREATE INDEX IF NOT EXISTS idx_stock_float_shares_as_of
            ON stock_float_shares(as_of);
        CREATE TABLE IF NOT EXISTS stock_float_market_cap_daily (
            stock_code TEXT NOT NULL,
            stock_name TEXT,
            trade_date TEXT NOT NULL,
            float_shares_as_of TEXT NOT NULL,
            close REAL NOT NULL,
            float_shares REAL NOT NULL,
            float_market_cap REAL NOT NULL,
            market_cap_unit TEXT NOT NULL,
            float_shares_source TEXT NOT NULL,
            float_shares_source_version TEXT NOT NULL,
            float_shares_source_date TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            freshness TEXT NOT NULL,
            health_status TEXT NOT NULL,
            PRIMARY KEY(stock_code, trade_date)
        );
        CREATE INDEX IF NOT EXISTS idx_stock_float_market_cap_daily_date
            ON stock_float_market_cap_daily(trade_date);
        CREATE TABLE IF NOT EXISTS capital_data_quality (
            dataset TEXT NOT NULL,
            as_of TEXT NOT NULL,
            expected_count INTEGER NOT NULL,
            row_count INTEGER NOT NULL,
            coverage REAL NOT NULL,
            missing_count INTEGER NOT NULL,
            anomaly_count INTEGER NOT NULL,
            freshness TEXT NOT NULL,
            health_status TEXT NOT NULL,
            source TEXT NOT NULL,
            source_version TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            PRIMARY KEY(dataset, as_of)
        );
        CREATE TABLE IF NOT EXISTS capital_float_share_missing (
            as_of TEXT NOT NULL,
            stock_code TEXT NOT NULL,
            stock_name TEXT,
            reason TEXT NOT NULL,
            PRIMARY KEY(as_of, stock_code, reason)
        );
        CREATE TABLE IF NOT EXISTS capital_float_share_anomalies (
            as_of TEXT NOT NULL,
            stock_code TEXT NOT NULL,
            stock_name TEXT,
            reason TEXT NOT NULL,
            primary_float_shares REAL,
            backup_float_shares REAL,
            rel_diff REAL,
            source TEXT NOT NULL,
            source_version TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            PRIMARY KEY(as_of, stock_code, reason, source)
        );
        """
    )


def _eligible_universe(market_database_path: Path, as_of: str) -> list[dict[str, object]]:
    uri = f"{market_database_path.resolve(strict=True).as_uri()}?mode=ro"
    with sqlite3.connect(uri, uri=True) as connection:
        connection.row_factory = sqlite3.Row
        rows = connection.execute(
            """SELECT stock_code, stock_name, close, is_st
               FROM stock_daily WHERE trade_date=? ORDER BY stock_code""",
            (as_of,),
        ).fetchall()
    universe = []
    for row in rows:
        code = str(row["stock_code"])
        name = str(row["stock_name"] or "")
        close = row["close"]
        if row["is_st"] not in (None, 0, False):
            continue
        if name.startswith(("ST", "*ST", "N", "C")):
            continue
        if code.startswith(("688", "689", "8", "4", "920")):
            continue
        if close is None or float(close) <= 0:
            continue
        universe.append({
            "stock_code": code,
            "stock_name": name,
            "close": float(close),
        })
    return universe


def _health_status(coverage: float, minimum: float) -> str:
    if coverage <= 0:
        return "UNAVAILABLE"
    if coverage < minimum:
        return "DEGRADED"
    return "READY"


def _freshness(as_of: str, source_as_of: str | None) -> str:
    if not source_as_of:
        return "missing"
    age = (date.fromisoformat(as_of) - date.fromisoformat(source_as_of)).days
    if age <= 7:
        return "fresh"
    if age <= 45:
        return "usable"
    return "stale"


def _float_share_anomaly_reason(value: float) -> str | None:
    if not math.isfinite(value):
        return "float_shares_not_finite"
    if value <= 0:
        return "float_shares_non_positive"
    if value > 2_000_000_000_000:
        return "float_shares_too_large_gt_2tn"
    return None


def _deterministic_sample(codes: Sequence[str], size: int) -> list[str]:
    ordered = sorted(codes)
    random.Random(20260917).shuffle(ordered)
    return sorted(ordered[:size])


def _default_recent_quarters() -> list[tuple[int, int]]:
    today = datetime.now().date()
    quarters = []
    year = today.year
    current_quarter = (today.month - 1) // 3 + 1
    q = current_quarter
    y = year
    for _ in range(6):
        quarters.append((y, q))
        q -= 1
        if q == 0:
            y -= 1
            q = 4
    return quarters


def _baostock_code(code: str) -> str:
    return ("sh." if code.startswith("6") else "sz.") + code


def _validate_timestamp(value: str) -> str:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("timestamp must include timezone")
    return parsed.astimezone(timezone.utc).isoformat()


def _tdx_date(value: object) -> str | None:
    if value in (None, ""):
        return None
    text = str(value)
    if len(text) != 8 or not text.isdigit():
        return None
    return date(
        int(text[0:4]), int(text[4:6]), int(text[6:8])
    ).isoformat()


def _require_integrity(connection: sqlite3.Connection) -> None:
    integrity = connection.execute("PRAGMA integrity_check").fetchone()[0]
    if integrity != "ok":
        raise ValueError(f"database integrity failed: {integrity}")
