from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
import json
import logging
import math
import os
from pathlib import Path
import re
import shutil
import sqlite3
import tempfile
import time
from typing import Callable, Mapping, Protocol, Sequence
import urllib.request

from .bootstrap import (
    BootstrapResult,
    _publication_lock,
    _sha256,
    load_market_metadata,
)
from .market_observation import (
    CSI300_CODE,
    append_missing_index_fact_v1,
    append_market_observation_facts_v1,
    initialize_market_observation_schema_v1,
)
from .quality import DataQualitySnapshotV1, DatasetQualityV1, QualityStatus
from .readiness import ReadinessStoreV1


PLATFORM_DAILY_MARKET_VERSION = "platform-daily-market.v2"
PLATFORM_DAILY_SCHEMA_VERSION = "market-data.platform-daily.v2"
INDEX_MISSING_CORRECTION_VERSION = "index-daily-missing-only-correction.v1"
LOGGER = logging.getLogger(__name__)


class DailyMarketSnapshotClientV1(Protocol):
    source_version: str
    universe_discovery_complete: bool

    def fetch(
        self, *, as_of: str, prior_stock_codes: Sequence[str]
    ) -> Sequence[Mapping[str, object]]: ...


class DailyIndexSnapshotClientV1(Protocol):
    source_version: str

    def fetch(self, *, as_of: str) -> Mapping[str, object]: ...


@dataclass(frozen=True)
class DailyMarketQualityPolicyV1:
    minimum_rows: int = 5_000
    minimum_prior_code_coverage: float = 0.95

    def __post_init__(self) -> None:
        if self.minimum_rows <= 0:
            raise ValueError("minimum_rows must be positive")
        if not 0 < self.minimum_prior_code_coverage <= 1:
            raise ValueError("minimum_prior_code_coverage must be in (0, 1]")


class AkshareSinaSnapshotClientV1:
    source_version = "akshare.sina-stock-zh-a-spot.v1"
    universe_discovery_complete = True

    def __init__(self, *, attempts: int = 3, retry_delay_seconds: float = 3.0) -> None:
        if attempts <= 0 or retry_delay_seconds < 0:
            raise ValueError("invalid Sina snapshot retry policy")
        self._attempts = attempts
        self._retry_delay_seconds = retry_delay_seconds

    def fetch(
        self, *, as_of: str, prior_stock_codes: Sequence[str]
    ) -> list[dict[str, object]]:
        del as_of, prior_stock_codes
        import akshare as ak

        last_error: Exception | None = None
        for attempt in range(1, self._attempts + 1):
            try:
                frame = ak.stock_zh_a_spot()
                if frame is None or frame.empty:
                    raise RuntimeError("Sina A-share snapshot is empty")
                return list(frame.to_dict("records"))
            except Exception as exc:
                last_error = exc
                if attempt < self._attempts:
                    time.sleep(self._retry_delay_seconds)
        raise RuntimeError(
            f"Sina A-share snapshot failed after {self._attempts} attempts: "
            f"{last_error}"
        ) from last_error


class TencentPriorUniverseSnapshotClientV1:
    source_version = "tencent.prior-universe-quotes.v1"
    universe_discovery_complete = False

    def fetch(
        self, *, as_of: str, prior_stock_codes: Sequence[str]
    ) -> list[dict[str, object]]:
        rows: list[dict[str, object]] = []
        for start in range(0, len(prior_stock_codes), 60):
            query = ",".join(
                _tencent_symbol(code)
                for code in prior_stock_codes[start:start + 60]
            )
            request = urllib.request.Request(
                f"http://qt.gtimg.cn/q={query}",
                headers={"User-Agent": "Mozilla/5.0"},
            )
            try:
                with urllib.request.urlopen(request, timeout=8) as response:
                    payload = response.read().decode("gbk", errors="ignore")
            except Exception as exc:
                raise RuntimeError("Tencent quote batch failed") from exc
            for line in payload.splitlines():
                row = _parse_tencent_line(line, as_of)
                if row is not None:
                    rows.append(row)
        if not rows:
            raise RuntimeError("Tencent prior-universe snapshot is empty")
        return rows


class PlatformDailySnapshotClientV1:
    def __init__(self) -> None:
        self.source_version = "not-fetched"
        self.universe_discovery_complete = False

    def fetch(
        self, *, as_of: str, prior_stock_codes: Sequence[str]
    ) -> list[dict[str, object]]:
        primary = AkshareSinaSnapshotClientV1()
        try:
            rows = primary.fetch(
                as_of=as_of, prior_stock_codes=prior_stock_codes
            )
            self.source_version = primary.source_version
            self.universe_discovery_complete = True
            return rows
        except RuntimeError:
            fallback = TencentPriorUniverseSnapshotClientV1()
            rows = fallback.fetch(
                as_of=as_of, prior_stock_codes=prior_stock_codes
            )
            self.source_version = fallback.source_version
            self.universe_discovery_complete = False
            return rows


class AkshareCsi300DailyClientV1:
    source_version = "akshare.sina-csi300-daily.v1"

    def fetch(self, *, as_of: str) -> Mapping[str, object]:
        import akshare as ak

        frame = ak.stock_zh_index_daily(symbol="sh000300")
        if frame is None or frame.empty:
            raise RuntimeError("CSI 300 daily response is empty")
        for row in reversed(frame.to_dict("records")):
            raw_date = row.get("date")
            row_date = (
                raw_date.strftime("%Y-%m-%d")
                if hasattr(raw_date, "strftime") else str(raw_date)[:10]
            )
            if row_date == as_of:
                return {**row, "date": row_date}
        raise RuntimeError(f"CSI 300 daily row missing for {as_of}")


class TencentCsi300SnapshotClientV1:
    source_version = "tencent.csi300-quote.v1"

    def fetch(self, *, as_of: str) -> Mapping[str, object]:
        request = urllib.request.Request(
            "http://qt.gtimg.cn/q=sh000300",
            headers={"User-Agent": "Mozilla/5.0"},
        )
        try:
            with urllib.request.urlopen(request, timeout=8) as response:
                payload = response.read().decode("gbk", errors="ignore")
        except OSError as exc:
            raise RuntimeError("Tencent CSI 300 quote failed") from exc
        return _parse_tencent_csi300(payload, as_of)


class PlatformCsi300DailyClientV1:
    """Prefer the exact-date closing quote; retain delayed history fallback."""

    def __init__(self) -> None:
        self.source_version = "not-fetched"

    def fetch(self, *, as_of: str) -> Mapping[str, object]:
        primary = TencentCsi300SnapshotClientV1()
        try:
            row = primary.fetch(as_of=as_of)
            self.source_version = primary.source_version
            return row
        except RuntimeError:
            fallback = AkshareCsi300DailyClientV1()
            row = fallback.fetch(as_of=as_of)
            self.source_version = fallback.source_version
            return row


def correct_missing_csi300_index_v1(
    *,
    target_path: Path,
    as_of: str,
    corrected_at: str,
    index_client: DailyIndexSnapshotClientV1,
) -> bool:
    """Append one absent CSI 300 fact without rewriting frozen market facts."""
    requested = date.fromisoformat(as_of).isoformat()
    timestamp = datetime.fromisoformat(corrected_at.replace("Z", "+00:00"))
    if timestamp.utcoffset() is None:
        raise ValueError("corrected_at must include a timezone")
    if requested > timestamp.date().isoformat():
        raise ValueError("as_of cannot be after corrected_at")
    target = target_path.resolve(strict=True)
    with sqlite3.connect(f"{target.as_uri()}?mode=ro", uri=True) as connection:
        session = connection.execute(
            "SELECT 1 FROM trading_calendar WHERE trade_date=?", (requested,)
        ).fetchone()
        existing = connection.execute(
            "SELECT 1 FROM index_daily WHERE index_code=? AND trade_date=?",
            (CSI300_CODE, requested),
        ).fetchone()
    if session is None:
        raise ValueError(f"not a published Platform session: {requested}")
    if existing is not None:
        return False
    index_row = index_client.fetch(as_of=requested)

    with _publication_lock(target_path):
        protected_before = _protected_table_counts(target)
        with sqlite3.connect(f"{target.as_uri()}?mode=ro", uri=True) as connection:
            session = connection.execute(
                "SELECT 1 FROM trading_calendar WHERE trade_date=?", (requested,)
            ).fetchone()
            existing = connection.execute(
                "SELECT 1 FROM index_daily WHERE index_code=? AND trade_date=?",
                (CSI300_CODE, requested),
            ).fetchone()
        if session is None:
            raise ValueError(f"not a published Platform session: {requested}")
        if existing is not None:
            return False

        descriptor, temporary_name = tempfile.mkstemp(
            prefix=f".{target.name}.", suffix=".tmp", dir=target.parent
        )
        os.close(descriptor)
        temporary = Path(temporary_name)
        try:
            shutil.copy2(target, temporary)
            with sqlite3.connect(temporary) as connection:
                connection.execute("BEGIN IMMEDIATE")
                append_missing_index_fact_v1(
                    connection,
                    as_of=requested,
                    index_row=index_row,
                    index_source_version=index_client.source_version,
                )
                connection.execute("""
                    CREATE TABLE IF NOT EXISTS platform_fact_corrections (
                        correction_id TEXT PRIMARY KEY,
                        contract_version TEXT NOT NULL,
                        dataset TEXT NOT NULL,
                        fact_key TEXT NOT NULL,
                        corrected_at TEXT NOT NULL,
                        source_version TEXT NOT NULL
                    )
                """)
                connection.execute(
                    "INSERT INTO platform_fact_corrections VALUES (?,?,?,?,?,?)",
                    (
                        f"{INDEX_MISSING_CORRECTION_VERSION}:{CSI300_CODE}:{requested}",
                        INDEX_MISSING_CORRECTION_VERSION,
                        "index_daily",
                        f"{CSI300_CODE}:{requested}",
                        corrected_at,
                        index_client.source_version,
                    ),
                )
                connection.commit()
            _validate_missing_index_correction(
                temporary, requested, protected_before
            )
            os.replace(temporary, target)
        finally:
            temporary.unlink(missing_ok=True)
    return True


def repair_recent_missing_csi300_v1(
    *,
    target_path: Path,
    corrected_at: str,
    client_factory: Callable[[], DailyIndexSnapshotClientV1],
    lookback_sessions: int = 5,
) -> dict[str, tuple[str, ...]]:
    if lookback_sessions <= 0:
        raise ValueError("lookback_sessions must be positive")
    target = target_path.resolve(strict=True)
    uri = f"{target.as_uri()}?mode=ro"
    with sqlite3.connect(uri, uri=True) as connection:
        missing = tuple(str(row[0]) for row in connection.execute(
            """SELECT calendar.trade_date
                 FROM (
                     SELECT trade_date FROM trading_calendar
                     ORDER BY trade_date DESC LIMIT ?
                 ) AS calendar
                 LEFT JOIN index_daily AS idx
                   ON idx.index_code=? AND idx.trade_date=calendar.trade_date
                WHERE idx.trade_date IS NULL
                ORDER BY calendar.trade_date""",
            (lookback_sessions, CSI300_CODE),
        ))
    corrected: list[str] = []
    failed: list[str] = []
    for session in missing:
        try:
            if correct_missing_csi300_index_v1(
                target_path=target_path,
                as_of=session,
                corrected_at=corrected_at,
                index_client=client_factory(),
            ):
                corrected.append(session)
        except (OSError, RuntimeError, ValueError, sqlite3.Error) as exc:
            LOGGER.warning(
                "CSI 300 missing-only correction failed for %s: %s",
                session,
                exc,
            )
            failed.append(session)
    return {"corrected": tuple(corrected), "failed": tuple(failed)}


def publish_platform_daily_market_data(
    *,
    client: DailyMarketSnapshotClientV1,
    target_path: Path,
    readiness_root: Path,
    as_of: str,
    published_at: str,
    index_client: DailyIndexSnapshotClientV1 | None = None,
    quality_policy: DailyMarketQualityPolicyV1 = DailyMarketQualityPolicyV1(),
) -> BootstrapResult:
    requested = date.fromisoformat(as_of).isoformat()
    timestamp = datetime.fromisoformat(published_at.replace("Z", "+00:00"))
    if timestamp.utcoffset() is None:
        raise ValueError("published_at must include a timezone")
    if requested > timestamp.date().isoformat():
        raise ValueError("as_of cannot be after published_at")
    target = target_path.resolve(strict=True)

    with _publication_lock(target_path):
        current = _database_state(target)
        _require_market_observation_history(
            target,
            latest_session=str(current["max_trade_date"]),
            expected_session_count=int(current["session_count"]),
        )
        if requested < current["max_trade_date"]:
            raise ValueError(
                f"as_of {requested} is older than current target "
                f"{current['max_trade_date']}"
            )
        if requested == current["max_trade_date"]:
            metadata = load_market_metadata(target)
            if metadata["producer_version"] != PLATFORM_DAILY_MARKET_VERSION:
                raise FileExistsError(
                    "existing same-day target was not produced by Platform daily publisher"
                )

        normalized = _normalize_snapshot(client.fetch(
            as_of=requested,
            prior_stock_codes=tuple(current["latest_codes"]),
        ), requested)
        _validate_snapshot(normalized, current, quality_policy)
        if requested == current["max_trade_date"]:
            if _read_exact_date_rows(target, requested) != normalized:
                raise ValueError(
                    "same-day source content changed; explicit correction version required"
                )
            return _existing_result(target, readiness_root, requested)

        index_row: Mapping[str, object] | None = None
        index_error: str | None = None
        if index_client is not None:
            try:
                index_row = index_client.fetch(as_of=requested)
            except Exception as exc:
                index_error = type(exc).__name__
                LOGGER.warning(
                    "CSI 300 fetch failed for %s; publishing degraded index quality",
                    requested,
                    exc_info=True,
                )

        descriptor, temporary_name = tempfile.mkstemp(
            prefix=f".{target.name}.", suffix=".tmp", dir=target.parent
        )
        os.close(descriptor)
        temporary = Path(temporary_name)
        try:
            shutil.copy2(target, temporary)
            source_stats = _append_snapshot(
                database=temporary,
                rows=normalized,
                as_of=requested,
                published_at=published_at,
                source_version=client.source_version,
                prior_state=current,
                index_row=index_row,
                index_source_version=(
                    index_client.source_version if index_client is not None else None
                ),
            )
            row_count, session_count = _validate_daily_database(
                temporary, source_stats
            )
            database_sha256 = _sha256(temporary)
            os.replace(temporary, target)
        finally:
            temporary.unlink(missing_ok=True)

        prior_codes = set(current["latest_codes"])
        observed_codes = {str(row[0]) for row in normalized}
        coverage = len(prior_codes & observed_codes) / len(prior_codes)
        universe_quality = DatasetQualityV1(
            dataset="universe_discovery",
            status=(
                QualityStatus.OK
                if client.universe_discovery_complete
                else QualityStatus.DEGRADED
            ),
            observed_as_of=(requested if client.universe_discovery_complete else None),
            source_version=client.source_version,
            coverage=(1.0 if client.universe_discovery_complete else None),
            reason_codes=(
                () if client.universe_discovery_complete
                else ("prior_session_universe_only",)
            ),
        )
        snapshot = DataQualitySnapshotV1.create(
            as_of=requested,
            observed_at=published_at,
            producer_version=PLATFORM_DAILY_MARKET_VERSION,
            datasets=(
                DatasetQualityV1(
                    dataset="stock_daily",
                    status=QualityStatus.OK,
                    observed_as_of=requested,
                    source_version=database_sha256,
                    coverage=min(coverage, 1.0),
                    freshness_lag_sessions=0,
                ),
                DatasetQualityV1(
                    dataset="market_breadth_daily",
                    status=QualityStatus.OK,
                    observed_as_of=requested,
                    source_version="platform:stock_daily:breadth.v1",
                    coverage=1.0,
                    freshness_lag_sessions=0,
                ),
                DatasetQualityV1(
                    dataset="index_daily",
                    status=(QualityStatus.OK if source_stats["index_written"]
                            else QualityStatus.DEGRADED),
                    observed_as_of=(requested if source_stats["index_written"] else None),
                    source_version=(
                        index_client.source_version
                        if index_client is not None else "index-client-not-configured"
                    ),
                    coverage=(1.0 if source_stats["index_written"] else 0.0),
                    freshness_lag_sessions=(0 if source_stats["index_written"] else None),
                    reason_codes=(
                        () if source_stats["index_written"]
                        else (("index_fetch_failed:" + index_error,)
                              if index_error else ("index_client_not_configured",))
                    ),
                ),
                DatasetQualityV1(
                    dataset="turnover",
                    status=QualityStatus.DEGRADED,
                    observed_as_of=None,
                    source_version="turnover-unavailable.v1",
                    coverage=0.0,
                    reason_codes=("turnover_not_in_daily_snapshot",),
                ),
                universe_quality,
            ),
        )
        marker = ReadinessStoreV1(readiness_root).publish_ready(
            bundle="v4-market-core",
            snapshot=snapshot,
            required_datasets=("stock_daily",),
            published_at=published_at,
            producer_version=PLATFORM_DAILY_MARKET_VERSION,
        )
        return BootstrapResult(
            target, requested, row_count, session_count, database_sha256, marker
        )


def _normalize_snapshot(
    raw_rows: Sequence[Mapping[str, object]], as_of: str
) -> tuple[tuple[object, ...], ...]:
    rows: list[tuple[object, ...]] = []
    identities: set[str] = set()
    for raw in raw_rows:
        code = _stock_code(raw.get("代码"))
        if code in identities:
            raise ValueError(f"duplicate stock code in daily snapshot: {code}")
        identities.add(code)
        name = str(raw.get("名称") or "").strip()
        open_price = _optional_positive(raw.get("今开"), "open", code)
        high = _optional_positive(raw.get("最高"), "high", code)
        low = _optional_positive(raw.get("最低"), "low", code)
        preclose = _optional_positive(raw.get("昨收"), "preclose", code)
        volume = _non_negative(raw.get("成交量"), "volume", code)
        amount = _non_negative(raw.get("成交额"), "amount", code)
        close = _non_negative(raw.get("最新价"), "close", code)
        if close == 0 and not (
            preclose is not None and volume == 0 and amount == 0
        ):
            raise ValueError(f"invalid zero close for {code}")
        pct_chg = _optional_finite(raw.get("涨跌幅"), "pct_chg", code)
        if pct_chg is None and preclose is not None:
            pct_chg = (close - preclose) / preclose * 100
        observed = [value for value in (open_price, close) if value is not None]
        if high is not None and high < max(observed):
            raise ValueError(f"invalid high for {code}")
        if low is not None and low > min(observed):
            raise ValueError(f"invalid low for {code}")
        if high is not None and low is not None and high < low:
            raise ValueError(f"invalid OHLC range for {code}")
        rows.append((
            code, name, as_of, open_price, high, low, close, preclose,
            volume, amount, pct_chg, None, int("ST" in name.upper()),
        ))
    return tuple(sorted(rows, key=lambda row: str(row[0])))


def _validate_snapshot(
    rows: tuple[tuple[object, ...], ...],
    prior_state: dict[str, object],
    policy: DailyMarketQualityPolicyV1,
) -> None:
    if len(rows) < policy.minimum_rows:
        raise ValueError(
            f"daily snapshot row count {len(rows)} is below {policy.minimum_rows}"
        )
    prior_codes = set(prior_state["latest_codes"])
    observed_codes = {str(row[0]) for row in rows}
    coverage = len(prior_codes & observed_codes) / len(prior_codes)
    if coverage < policy.minimum_prior_code_coverage:
        raise ValueError(
            f"prior-session code coverage {coverage:.6%} is below "
            f"{policy.minimum_prior_code_coverage:.2%}"
        )


def _append_snapshot(
    *, database: Path, rows: tuple[tuple[object, ...], ...], as_of: str,
    published_at: str, source_version: str, prior_state: dict[str, object],
    index_row: Mapping[str, object] | None,
    index_source_version: str | None,
) -> dict[str, object]:
    with sqlite3.connect(database) as connection:
        initialize_market_observation_schema_v1(connection)
        connection.execute("BEGIN IMMEDIATE")
        connection.executemany(
            """INSERT INTO stock_daily (
                   stock_code,stock_name,trade_date,open,high,low,close,preclose,
                   volume,amount,pct_chg,turnover,is_st
               ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            rows,
        )
        connection.execute(
            "INSERT INTO trading_calendar(trade_date) VALUES (?)", (as_of,)
        )
        breadth_written, index_written = append_market_observation_facts_v1(
            connection,
            as_of=as_of,
            index_row=index_row,
            index_source_version=index_source_version,
        )
        if not breadth_written:
            raise ValueError("market breadth was not published for stock snapshot")
        metadata = {
            "schema_version": PLATFORM_DAILY_SCHEMA_VERSION,
            "producer_version": PLATFORM_DAILY_MARKET_VERSION,
            "published_at": published_at,
            "source_manifest": json.dumps(
                {
                    "source_version": source_version,
                    "as_of": as_of,
                    "as_of_row_count": len(rows),
                    "previous_max_trade_date": prior_state["max_trade_date"],
                },
                sort_keys=True,
                separators=(",", ":"),
            ),
        }
        connection.execute("DELETE FROM platform_metadata")
        connection.executemany(
            "INSERT INTO platform_metadata(key,value) VALUES (?,?)", metadata.items()
        )
        connection.commit()
    return {
        "row_count": int(prior_state["row_count"]) + len(rows),
        "min_trade_date": prior_state["min_trade_date"],
        "max_trade_date": as_of,
        "session_count": int(prior_state["session_count"]) + 1,
        "as_of_row_count": len(rows),
        "breadth_written": breadth_written,
        "index_written": index_written,
    }


def _validate_daily_database(
    database: Path, source_stats: dict[str, object]
) -> tuple[int, int]:
    with sqlite3.connect(database) as connection:
        integrity = connection.execute("PRAGMA integrity_check").fetchone()[0]
        row_count = int(connection.execute(
            "SELECT COUNT(*) FROM stock_daily"
        ).fetchone()[0])
        session_count = int(connection.execute(
            "SELECT COUNT(*) FROM trading_calendar"
        ).fetchone()[0])
        current_stock_count = int(connection.execute(
            "SELECT COUNT(*) FROM stock_daily WHERE trade_date=?",
            (source_stats["max_trade_date"],),
        ).fetchone()[0])
        breadth_count = int(connection.execute(
            "SELECT COUNT(*) FROM market_breadth_daily WHERE trade_date=?",
            (source_stats["max_trade_date"],),
        ).fetchone()[0])
        breadth_session_count = int(connection.execute(
            "SELECT COUNT(*) FROM market_breadth_daily"
        ).fetchone()[0])
    if integrity != "ok":
        raise ValueError(f"published database integrity check failed: {integrity}")
    if row_count != source_stats["row_count"]:
        raise ValueError("published stock_daily row count differs from source")
    if session_count != source_stats["session_count"]:
        raise ValueError("published trading session count differs from source")
    if (current_stock_count != source_stats["as_of_row_count"]
            or breadth_count != 1
            or breadth_session_count != session_count):
        raise ValueError("published daily observation coverage mismatch")
    return row_count, session_count


def _database_state(path: Path) -> dict[str, object]:
    load_market_metadata(path)
    uri = f"{path.resolve().as_uri()}?mode=ro"
    with sqlite3.connect(uri, uri=True) as connection:
        integrity = connection.execute("PRAGMA integrity_check").fetchone()[0]
        row = connection.execute(
            "SELECT COUNT(*),MIN(trade_date),MAX(trade_date),"
            "COUNT(DISTINCT trade_date) FROM stock_daily"
        ).fetchone()
        if integrity != "ok" or not row or not row[0] or not row[2]:
            raise ValueError("current Platform market database is invalid")
        latest_codes = tuple(str(item[0]) for item in connection.execute(
            "SELECT stock_code FROM stock_daily WHERE trade_date=? ORDER BY stock_code",
            (row[2],),
        ))
    return {
        "row_count": int(row[0]),
        "min_trade_date": str(row[1]),
        "max_trade_date": str(row[2]),
        "session_count": int(row[3]),
        "latest_codes": latest_codes,
        "latest_code_count": len(latest_codes),
    }


def _require_market_observation_history(
    path: Path, *, latest_session: str, expected_session_count: int
) -> None:
    uri = f"{path.resolve().as_uri()}?mode=ro"
    with sqlite3.connect(uri, uri=True) as connection:
        table = connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' "
            "AND name='market_breadth_daily'"
        ).fetchone()
        bounds = (
            connection.execute(
                "SELECT COUNT(*),MAX(trade_date) FROM market_breadth_daily"
            ).fetchone()
            if table else (0, None)
        )
    if int(bounds[0]) != expected_session_count or bounds[1] != latest_session:
        raise ValueError(
            "market observation history must be migrated before daily publication"
        )


def _read_exact_date_rows(path: Path, as_of: str) -> tuple[tuple[object, ...], ...]:
    uri = f"{path.resolve().as_uri()}?mode=ro"
    with sqlite3.connect(uri, uri=True) as connection:
        return tuple(connection.execute(
            """SELECT stock_code,stock_name,trade_date,open,high,low,close,preclose,
                      volume,amount,pct_chg,turnover,is_st
               FROM stock_daily WHERE trade_date=? ORDER BY stock_code""",
            (as_of,),
        ))


def _existing_result(
    target: Path, readiness_root: Path, as_of: str
) -> BootstrapResult:
    marker = ReadinessStoreV1(readiness_root).read_ready(
        bundle="v4-market-core", as_of=as_of
    )
    if marker is None or marker.producer_version != PLATFORM_DAILY_MARKET_VERSION:
        raise ValueError("same-day Platform target has no matching readiness marker")
    state = _database_state(target)
    return BootstrapResult(
        target,
        as_of,
        int(state["row_count"]),
        int(state["session_count"]),
        _sha256(target),
        marker,
    )


def _stock_code(value: object) -> str:
    match = re.search(r"(\d{6})$", str(value or "").strip())
    if match is None:
        raise ValueError(f"invalid stock code: {value!r}")
    return match.group(1)


def _tencent_symbol(code: str) -> str:
    if code.startswith(("4", "8", "92")):
        return f"bj{code}"
    if code.startswith(("6", "9")):
        return f"sh{code}"
    return f"sz{code}"


def _parse_tencent_line(line: str, as_of: str) -> dict[str, object] | None:
    if "=" not in line:
        return None
    try:
        raw_symbol = line.split("=", 1)[0].split("_")[-1]
        code = _stock_code(raw_symbol)
        fields = line.split("=", 1)[1].strip().strip('\";').split("~")
        if len(fields) < 38:
            return None
        provider_timestamp = fields[30]
        if provider_timestamp[:8] != as_of.replace("-", ""):
            raise ValueError(f"Tencent quote date mismatch for {code}")
        return {
            "代码": code,
            "名称": fields[1] or code,
            "最新价": float(fields[3] or 0),
            "昨收": float(fields[4] or 0),
            "今开": float(fields[5] or 0),
            "成交量": float(fields[6] or 0) * 100,
            "涨跌幅": float(fields[32] or 0),
            "最高": float(fields[33] or 0),
            "最低": float(fields[34] or 0),
            "成交额": float(fields[37] or 0) * 10_000,
        }
    except (IndexError, ValueError) as exc:
        if isinstance(exc, ValueError) and "date mismatch" in str(exc):
            raise
        return None


def _parse_tencent_csi300(payload: str, as_of: str) -> dict[str, object]:
    try:
        fields = payload.split("=", 1)[1].strip().strip('\";').split("~")
        if len(fields) < 38 or fields[2] != "000300":
            raise ValueError("unexpected Tencent CSI 300 payload")
        provider_timestamp = fields[30]
        if provider_timestamp[:8] != as_of.replace("-", ""):
            raise RuntimeError(f"Tencent CSI 300 row missing for {as_of}")
        close = float(fields[3])
        preclose = float(fields[4])
        open_price = float(fields[5])
        high = float(fields[33])
        low = float(fields[34])
        volume = float(fields[6]) * 100
        amount = float(fields[37]) * 10_000
    except RuntimeError:
        raise
    except (IndexError, ValueError) as exc:
        raise RuntimeError("invalid Tencent CSI 300 payload") from exc
    if min(close, preclose, open_price, high, low) <= 0 or volume < 0 or amount < 0:
        raise RuntimeError("invalid Tencent CSI 300 values")
    return {
        "date": as_of,
        "open": open_price,
        "high": high,
        "low": low,
        "close": close,
        "volume": volume,
        "amount": amount,
    }


def _protected_table_counts(path: Path) -> dict[str, int]:
    uri = f"{path.resolve().as_uri()}?mode=ro"
    with sqlite3.connect(uri, uri=True) as connection:
        return {
            table: int(connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
            for table in (
                "stock_daily",
                "market_breadth_daily",
                "trading_calendar",
                "platform_metadata",
            )
        }


def _validate_missing_index_correction(
    path: Path, as_of: str, protected_before: dict[str, int]
) -> None:
    with sqlite3.connect(path) as connection:
        integrity = connection.execute("PRAGMA integrity_check").fetchone()[0]
        index_count = int(connection.execute(
            "SELECT COUNT(*) FROM index_daily WHERE index_code=? AND trade_date=?",
            (CSI300_CODE, as_of),
        ).fetchone()[0])
        audit_count = int(connection.execute(
            "SELECT COUNT(*) FROM platform_fact_corrections "
            "WHERE correction_id=?",
            (f"{INDEX_MISSING_CORRECTION_VERSION}:{CSI300_CODE}:{as_of}",),
        ).fetchone()[0])
    if integrity != "ok" or index_count != 1 or audit_count != 1:
        raise ValueError("missing-index correction validation failed")
    if _protected_table_counts(path) != protected_before:
        raise ValueError("missing-index correction changed protected table counts")


def _number(value: object, label: str, code: str) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"invalid {label} for {code}: {value!r}") from exc
    if not math.isfinite(number):
        raise ValueError(f"invalid {label} for {code}: {value!r}")
    return number


def _non_negative(value: object, label: str, code: str) -> float:
    number = _number(value, label, code)
    if number < 0:
        raise ValueError(f"invalid {label} for {code}: {value!r}")
    return number


def _optional_positive(value: object, label: str, code: str) -> float | None:
    if value in (None, "", "-"):
        return None
    number = _number(value, label, code)
    return number if number > 0 else None


def _optional_finite(value: object, label: str, code: str) -> float | None:
    if value in (None, "", "-"):
        return None
    return _number(value, label, code)
