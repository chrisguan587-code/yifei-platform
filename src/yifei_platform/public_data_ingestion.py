from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
import json
import math
import os
import random
from http.client import HTTPException
from pathlib import Path
import shutil
import sqlite3
import tempfile
import time
from typing import Protocol, Sequence
from urllib.parse import urlencode
from urllib.error import HTTPError, URLError
from urllib.request import ProxyHandler, Request, build_opener, urlopen

from .supplemental_facts import (
    initialize_supplemental_database_v1,
    serialized_supplemental_publication_v1,
)


PUBLIC_DATA_SOURCE_VERSION = (
    "eastmoney-https-moneyflow+baostock-daily"
    "+cninfo-sw-l2.v2"
)
CAPITAL_SOURCE_VERSION = "eastmoney-https-moneyflow+baostock-daily.v3"
CAPITAL_SOURCE = "akshare.eastmoney+baostock"
SINA_CAPITAL_SOURCE_VERSION = "sina-moneyflow-r0+baostock-daily.v2"
SINA_CAPITAL_SOURCE = "sina.moneyflow.r0+baostock"
MEMBERSHIP_SOURCE = "akshare.cninfo"
MEMBERSHIP_SOURCE_VERSION = "akshare.cninfo-sw-l2.v1"
CNINFO_SW_CLASSIFICATION_CODE = "008003"
EASTMONEY_REQUEST_INTERVAL_SECONDS = 0.75
SINA_REQUEST_INTERVAL_SECONDS = 1.0


class CapitalFlowClientV1(Protocol):
    def read(self, stock_code: str) -> Sequence[dict[str, object]]: ...


class CapitalCacheClientV1(CapitalFlowClientV1, Protocol):
    def has_capital_cache(self, stock_code: str) -> bool: ...


class DailyMarketClientV1(Protocol):
    def read(
        self, stock_code: str, start_date: str, end_date: str
    ) -> Sequence[dict[str, object]]: ...


class IndustryHistoryClientV1(Protocol):
    def read(
        self, stock_code: str, start_date: str, end_date: str
    ) -> Sequence[dict[str, object]]: ...


@dataclass(frozen=True)
class PublicDataBackfillResultV1:
    target_path: Path
    start_date: str
    end_date: str
    latest_capital_as_of: str
    membership_available_through: str
    capital_coverage: float
    membership_coverage: float
    source_version: str


@dataclass(frozen=True)
class MembershipBackfillResultV1:
    target_path: Path
    start_date: str
    end_date: str
    membership_available_through: str
    stock_count: int
    coverage: float
    source_version: str


@dataclass(frozen=True)
class CapitalBackfillResultV1:
    target_path: Path
    start_date: str
    end_date: str
    latest_capital_as_of: str
    stock_count: int
    coverage: float
    source_version: str


@dataclass(frozen=True)
class CapitalPrefetchResultV1:
    stock_count: int
    cached_before: int
    prefetched_count: int
    remaining_count: int
    source_version: str


class AksharePublicDataClientV1:
    """Thin adapter; imports the optional dependency only when instantiated."""

    def __init__(
        self,
        *,
        cache_dir: Path | None = None,
        retry_attempts: int = 3,
        fund_flow_payload_reader=None,
    ) -> None:
        if retry_attempts < 1:
            raise ValueError("retry_attempts must be positive")
        try:
            import akshare as ak
        except ImportError as exc:
            raise RuntimeError(
                "AKShare is required; install yifei-platform[public-data]"
            ) from exc
        self._ak = ak
        self._cache_dir = cache_dir
        self._retry_attempts = retry_attempts
        self._fund_flow_payload_reader = (
            fund_flow_payload_reader or _read_eastmoney_fund_flow_payload
        )

    def read(self, stock_code: str) -> tuple[dict[str, object], ...]:
        cached = self._cached("capital", stock_code)
        if cached is not None:
            return cached
        market_number = (
            "1" if stock_code.startswith(("5", "6", "9")) else "0"
        )
        try:
            payload = _retry(
                lambda: self._fund_flow_payload_reader(
                    stock_code=stock_code,
                    market_number=market_number,
                ),
                attempts=self._retry_attempts,
                label=f"Eastmoney HTTPS capital flow {stock_code}",
                base_delay_seconds=1,
            )
        except RuntimeError:
            if stock_code == "000001":
                raise
            control = _retry(
                lambda: self._fund_flow_payload_reader(
                    stock_code="000001",
                    market_number="0",
                ),
                attempts=2,
                label="Eastmoney HTTPS capital flow health control",
                base_delay_seconds=1,
            )
            if not parse_eastmoney_capital_payload_v1(control):
                raise
            rows: tuple[dict[str, object], ...] = ()
            return rows
        rows = parse_eastmoney_capital_payload_v1(payload)
        time.sleep(EASTMONEY_REQUEST_INTERVAL_SECONDS)
        self._store("capital", stock_code, rows)
        return rows

    def has_capital_cache(self, stock_code: str) -> bool:
        path = self._cache_path("capital", stock_code)
        return path is not None and path.is_file()

    def read_industry(
        self, stock_code: str, start_date: str, end_date: str
    ) -> tuple[dict[str, object], ...]:
        range_key = f"{start_date}_{end_date}"
        cached = self._cached(
            "industry", stock_code, range_key=range_key
        )
        if cached is not None:
            return cached
        try:
            frame = _retry(
                lambda: self._ak.stock_industry_change_cninfo(
                    symbol=stock_code,
                    start_date=start_date.replace("-", ""),
                    end_date=end_date.replace("-", ""),
                ),
                attempts=self._retry_attempts,
                label=f"AKShare CNInfo industry {stock_code}",
            )
        except RuntimeError as exc:
            if not _caused_by_missing_column(exc, "变更日期"):
                raise
            rows: tuple[dict[str, object], ...] = ()
            self._store(
                "industry", stock_code, rows, range_key=range_key
            )
            return rows
        rows = tuple(
            {
                "stock_name": row.get("新证券简称"),
                "classification_code": row.get("分类标准编码"),
                "industry_code": row.get("行业编码"),
                "industry_l2_name": row.get("行业次类"),
                "valid_from": _iso_date(row.get("变更日期")),
            }
            for row in frame.to_dict("records")
        )
        self._store("industry", stock_code, rows, range_key=range_key)
        return rows

    def _cached(
        self,
        dataset: str,
        stock_code: str,
        *,
        range_key: str | None = None,
    ) -> tuple[dict[str, object], ...] | None:
        path = self._cache_path(
            dataset, stock_code, range_key=range_key
        )
        if path is None or not path.is_file():
            return None
        payload = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(payload, list):
            raise ValueError(f"invalid public-data cache: {path}")
        return tuple(dict(row) for row in payload)

    def _store(
        self,
        dataset: str,
        stock_code: str,
        rows: Sequence[dict[str, object]],
        *,
        range_key: str | None = None,
    ) -> None:
        path = self._cache_path(
            dataset, stock_code, range_key=range_key
        )
        if path is None:
            return
        _write_immutable_json(path, list(rows))

    def _cache_path(
        self,
        dataset: str,
        stock_code: str,
        *,
        range_key: str | None = None,
    ) -> Path | None:
        if self._cache_dir is None:
            return None
        root = self._cache_dir / "akshare" / dataset
        if range_key is not None:
            root = root / range_key
        return root / f"{stock_code}.json"


class SinaCapitalFlowClientV1:
    """No-token Sina daily main-flow adapter with immutable per-stock cache."""

    def __init__(
        self,
        *,
        cache_dir: Path | None = None,
        retry_attempts: int = 3,
        request_interval_seconds: float = SINA_REQUEST_INTERVAL_SECONDS,
        fund_flow_payload_reader=None,
    ) -> None:
        if retry_attempts < 1:
            raise ValueError("retry_attempts must be positive")
        if request_interval_seconds < 0:
            raise ValueError("request_interval_seconds must not be negative")
        self._cache_dir = cache_dir
        self._retry_attempts = retry_attempts
        self._request_interval_seconds = request_interval_seconds
        self._fund_flow_payload_reader = (
            fund_flow_payload_reader or _read_sina_fund_flow_payload
        )

    def read(self, stock_code: str) -> tuple[dict[str, object], ...]:
        cached = self._cached(stock_code)
        if cached is not None:
            return cached
        symbol = _sina_symbol(stock_code)

        def fetch() -> tuple[dict[str, object], ...]:
            rows = parse_sina_capital_payload_v1(
                self._fund_flow_payload_reader(
                    stock_code=stock_code,
                    symbol=symbol,
                    days=120,
                )
            )
            if not rows:
                raise ValueError("Sina capital response is empty")
            return rows

        rows = _retry(
            fetch,
            attempts=self._retry_attempts,
            label=f"Sina capital flow {stock_code}",
            base_delay_seconds=1,
        )
        if self._request_interval_seconds:
            time.sleep(
                self._request_interval_seconds + random.uniform(0.1, 0.5)
            )
        self._store(stock_code, rows)
        return rows

    def has_capital_cache(self, stock_code: str) -> bool:
        path = self._cache_path(stock_code)
        return path is not None and path.is_file()

    def _cached(
        self, stock_code: str
    ) -> tuple[dict[str, object], ...] | None:
        path = self._cache_path(stock_code)
        if path is None or not path.is_file():
            return None
        payload = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(payload, list):
            raise ValueError(f"invalid Sina capital cache: {path}")
        return tuple(dict(row) for row in payload)

    def _store(
        self, stock_code: str, rows: Sequence[dict[str, object]]
    ) -> None:
        path = self._cache_path(stock_code)
        if path is not None:
            _write_immutable_json(path, list(rows))

    def _cache_path(self, stock_code: str) -> Path | None:
        if self._cache_dir is None:
            return None
        return self._cache_dir / "sina" / "capital" / f"{stock_code}.json"


class AkshareIndustryHistoryClientV1:
    def __init__(self, client: AksharePublicDataClientV1):
        self._client = client

    def read(
        self, stock_code: str, start_date: str, end_date: str
    ) -> Sequence[dict[str, object]]:
        return self._client.read_industry(stock_code, start_date, end_date)


class BaoStockDailyClientV1:
    """BaoStock adapter with explicit raw units at the source boundary."""

    def __init__(
        self,
        *,
        cache_dir: Path | None = None,
        retry_attempts: int = 3,
    ) -> None:
        if retry_attempts < 1:
            raise ValueError("retry_attempts must be positive")
        try:
            import baostock as bs
        except ImportError as exc:
            raise RuntimeError(
                "BaoStock is required; install yifei-platform[public-data]"
            ) from exc
        def login_once():
            result = bs.login()
            if result.error_code != "0":
                raise RuntimeError(
                    f"BaoStock login failed: {result.error_msg}"
                )
            return result

        _retry(
            login_once,
            attempts=retry_attempts,
            label="BaoStock login",
        )
        self._bs = bs
        self._closed = False
        self._cache_dir = cache_dir
        self._retry_attempts = retry_attempts

    def read(
        self, stock_code: str, start_date: str, end_date: str
    ) -> tuple[dict[str, object], ...]:
        cache_path = (
            self._cache_dir
            / "baostock"
            / f"{start_date}_{end_date}"
            / f"{stock_code}.json"
            if self._cache_dir is not None else None
        )
        if cache_path is not None and cache_path.is_file():
            payload = json.loads(cache_path.read_text(encoding="utf-8"))
            if not isinstance(payload, list):
                raise ValueError(f"invalid public-data cache: {cache_path}")
            return tuple(dict(row) for row in payload)
        exchange = "sh" if stock_code.startswith(("5", "6", "9")) else "sz"
        result = _retry(
            lambda: self._bs.query_history_k_data_plus(
                f"{exchange}.{stock_code}",
                "date,close,volume,amount,turn",
                start_date=start_date,
                end_date=end_date,
                frequency="d",
                adjustflag="3",
            ),
            attempts=self._retry_attempts,
            label=f"BaoStock daily {stock_code}",
        )
        if result.error_code != "0":
            raise RuntimeError(
                f"BaoStock daily query failed for {stock_code}: "
                f"{result.error_msg}"
            )
        rows = tuple(
            {
                "trade_date": row[0],
                "close": row[1],
                "volume": row[2],
                "amount": row[3],
                "turnover_percent": row[4],
                "volume_unit": "SHARE",
                "amount_unit": "CNY",
                "turnover_unit": "PERCENT",
            }
            for row in result.data
        )
        if cache_path is not None:
            _write_immutable_json(cache_path, list(rows))
        return rows

    def close(self) -> None:
        if not self._closed:
            self._bs.logout()
            self._closed = True


def prepare_public_cache_v1(
    *,
    cache_dir: Path,
    start_date: str,
    end_date: str,
    source_version: str = PUBLIC_DATA_SOURCE_VERSION,
) -> None:
    payload = {
        "cache_schema": "public-data-raw-cache.v1",
        "start_date": date.fromisoformat(start_date).isoformat(),
        "end_date": date.fromisoformat(end_date).isoformat(),
        "source_version": source_version,
    }
    path = cache_dir / "manifest.json"
    if path.is_file():
        existing = json.loads(path.read_text(encoding="utf-8"))
        if existing != payload:
            raise ValueError(
                "public-data cache belongs to a different range or source version"
            )
        return
    _write_immutable_json(path, payload)


@serialized_supplemental_publication_v1
def backfill_public_supplemental_v1(
    *,
    capital_client: CapitalFlowClientV1,
    daily_client: DailyMarketClientV1,
    industry_client: IndustryHistoryClientV1,
    market_database_path: Path,
    target_path: Path,
    start_date: str,
    end_date: str,
    fetched_at: str,
    source_version: str = PUBLIC_DATA_SOURCE_VERSION,
    minimum_capital_coverage: float = 0.98,
    minimum_membership_coverage: float = 0.99,
) -> PublicDataBackfillResultV1:
    start = date.fromisoformat(start_date).isoformat()
    end = date.fromisoformat(end_date).isoformat()
    if start > end:
        raise ValueError("start_date must not be after end_date")
    parsed = datetime.fromisoformat(fetched_at.replace("Z", "+00:00"))
    if parsed.utcoffset() is None:
        raise ValueError("fetched_at must include a timezone")
    if not source_version.strip():
        raise ValueError("source_version is required")
    for value, name in (
        (minimum_capital_coverage, "minimum_capital_coverage"),
        (minimum_membership_coverage, "minimum_membership_coverage"),
    ):
        if not 0 <= value <= 1:
            raise ValueError(f"{name} must be between 0 and 1")

    sessions, universe, names = _market_universe(
        market_database_path.resolve(strict=True), start, end
    )
    if not sessions:
        raise ValueError("market database has no sessions in requested range")
    all_codes = sorted(set().union(*universe.values()))

    capital_rows: list[tuple[object, ...]] = []
    membership_rows: list[tuple[object, ...]] = []
    for stock_code in all_codes:
        flows = {
            _iso_date(row.get("trade_date")): row
            for row in capital_client.read(stock_code)
        }
        daily = {
            _iso_date(row.get("trade_date")): row
            for row in daily_client.read(stock_code, start, end)
        }
        for session in sessions:
            if stock_code not in universe[session]:
                continue
            flow = flows.get(session)
            bar = daily.get(session)
            if flow is None or bar is None:
                continue
            if flow.get("vendor_row_status", "VALID") != "VALID":
                continue
            if not _daily_trading_inputs_available(
                stock_code=stock_code, row=bar
            ):
                continue
            amount = _required_finite(flow.get("vendor_net_amount"))
            if str(flow.get("amount_unit")) != "CNY":
                raise ValueError(
                    f"unsupported capital amount unit for {stock_code}: "
                    f"{flow.get('amount_unit')}"
                )
            if not validate_vendor_flow_against_turnover_cny_v1(
                stock_code=stock_code,
                flow=flow,
                daily=bar,
            ):
                continue
            float_market_cap = derive_float_market_cap_cny_v1(
                stock_code=stock_code, row=bar
            )
            capital_rows.append((
                stock_code,
                names.get(stock_code),
                session,
                amount,
                float_market_cap,
                "CNY",
                "CNY",
                CAPITAL_SOURCE,
                source_version,
                fetched_at,
            ))

        history = industry_client.read(stock_code, "1990-01-01", end)
        membership_rows.extend(
            _cninfo_sw_l2_intervals(
                stock_code=stock_code,
                rows=history,
                fetched_at=fetched_at,
                source_version=source_version,
            )
        )

    expected_capital = sum(len(codes) for codes in universe.values())
    capital_coverage = len(capital_rows) / expected_capital
    if capital_coverage < minimum_capital_coverage:
        raise ValueError("stock capital coverage is below the frozen threshold")

    _validate_membership_intervals(membership_rows)
    membership_coverage = _membership_coverage(
        sessions=sessions,
        universe=universe,
        memberships=membership_rows,
    )
    if membership_coverage < minimum_membership_coverage:
        raise ValueError("PIT sector membership coverage is below the frozen threshold")

    target = target_path.resolve()
    target.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{target.name}.", suffix=".tmp", dir=target.parent
    )
    os.close(descriptor)
    temporary = Path(temporary_name)
    try:
        if target.exists():
            shutil.copy2(target, temporary)
        initialize_supplemental_database_v1(temporary)
        with sqlite3.connect(temporary) as connection:
            connection.execute("BEGIN")
            connection.execute(
                """DELETE FROM stock_capital_daily
                   WHERE trade_date BETWEEN ? AND ? AND source=?""",
                (start, end, CAPITAL_SOURCE),
            )
            connection.executemany(
                """INSERT INTO stock_capital_daily (
                       stock_code, stock_name, trade_date,
                       vendor_net_amount, float_market_cap, amount_unit,
                       market_cap_unit, source, source_version, fetched_at
                   ) VALUES (?,?,?,?,?,?,?,?,?,?)""",
                capital_rows,
            )
            connection.execute(
                "DELETE FROM sector_membership_history WHERE source=?",
                (MEMBERSHIP_SOURCE,),
            )
            connection.executemany(
                """INSERT INTO sector_membership_history (
                       stock_code, stock_name, sector_code, sector_name,
                       sector_level, valid_from, valid_to_exclusive, source,
                       source_version, fetched_at
                   ) VALUES (?,?,?,?,?,?,?,?,?,?)""",
                membership_rows,
            )
            connection.executemany(
                """INSERT INTO supplemental_metadata(key, value)
                   VALUES (?, ?)
                   ON CONFLICT(key) DO UPDATE SET value=excluded.value""",
                (
                    ("capital_source_version", source_version),
                    ("capital_fetched_at", fetched_at),
                    ("capital_amount_unit", "CNY"),
                    ("capital_volume_raw_unit", "SHARE"),
                    ("capital_turnover_raw_unit", "PERCENT"),
                    ("membership_source_version", source_version),
                    ("membership_fetched_at", fetched_at),
                    (
                        "membership_classification_code",
                        CNINFO_SW_CLASSIFICATION_CODE,
                    ),
                ),
            )
            connection.commit()
            integrity = connection.execute(
                "PRAGMA integrity_check"
            ).fetchone()[0]
            if integrity != "ok":
                raise ValueError(
                    f"supplemental database integrity failed: {integrity}"
                )
        os.replace(temporary, target)
    finally:
        temporary.unlink(missing_ok=True)

    return PublicDataBackfillResultV1(
        target_path=target,
        start_date=start,
        end_date=end,
        latest_capital_as_of=sessions[-1],
        membership_available_through=end,
        capital_coverage=capital_coverage,
        membership_coverage=membership_coverage,
        source_version=source_version,
    )


@serialized_supplemental_publication_v1
def backfill_public_capital_v1(
    *,
    capital_client: CapitalFlowClientV1,
    daily_client: DailyMarketClientV1,
    market_database_path: Path,
    target_path: Path,
    start_date: str,
    end_date: str,
    fetched_at: str,
    source_version: str = CAPITAL_SOURCE_VERSION,
    capital_source: str = CAPITAL_SOURCE,
    minimum_capital_coverage: float = 0.98,
) -> CapitalBackfillResultV1:
    start = date.fromisoformat(start_date).isoformat()
    end = date.fromisoformat(end_date).isoformat()
    if start > end:
        raise ValueError("start_date must not be after end_date")
    parsed = datetime.fromisoformat(fetched_at.replace("Z", "+00:00"))
    if parsed.utcoffset() is None:
        raise ValueError("fetched_at must include a timezone")
    if not source_version.strip():
        raise ValueError("source_version is required")
    if not capital_source.strip():
        raise ValueError("capital_source is required")
    if not 0 <= minimum_capital_coverage <= 1:
        raise ValueError("minimum_capital_coverage must be between 0 and 1")

    sessions, universe, names = _market_universe(
        market_database_path.resolve(strict=True), start, end
    )
    if not sessions:
        raise ValueError("market database has no sessions in requested range")
    all_codes = sorted(set().union(*universe.values()))
    capital_rows: list[tuple[object, ...]] = []
    for stock_code in all_codes:
        flows = {
            _iso_date(row.get("trade_date")): row
            for row in capital_client.read(stock_code)
        }
        daily = {
            _iso_date(row.get("trade_date")): row
            for row in daily_client.read(stock_code, start, end)
        }
        for session in sessions:
            if stock_code not in universe[session]:
                continue
            flow = flows.get(session)
            bar = daily.get(session)
            if flow is None or bar is None:
                continue
            if flow.get("vendor_row_status", "VALID") != "VALID":
                continue
            if not _daily_trading_inputs_available(
                stock_code=stock_code, row=bar
            ):
                continue
            amount = _required_finite(flow.get("vendor_net_amount"))
            if str(flow.get("amount_unit")) != "CNY":
                raise ValueError(
                    f"unsupported capital amount unit for {stock_code}: "
                    f"{flow.get('amount_unit')}"
                )
            if not validate_vendor_flow_against_turnover_cny_v1(
                stock_code=stock_code,
                flow=flow,
                daily=bar,
                mismatch_policy=(
                    "EXCLUDE_ROW"
                    if capital_source == SINA_CAPITAL_SOURCE
                    else "RAISE"
                ),
            ):
                continue
            float_market_cap = derive_float_market_cap_cny_v1(
                stock_code=stock_code, row=bar
            )
            capital_rows.append((
                stock_code,
                names.get(stock_code),
                session,
                amount,
                float_market_cap,
                "CNY",
                "CNY",
                capital_source,
                source_version,
                fetched_at,
            ))

    expected = sum(len(codes) for codes in universe.values())
    coverage = len(capital_rows) / expected
    if coverage < minimum_capital_coverage:
        raise ValueError("stock capital coverage is below the frozen threshold")

    target = target_path.resolve()
    target.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{target.name}.", suffix=".tmp", dir=target.parent
    )
    os.close(descriptor)
    temporary = Path(temporary_name)
    try:
        if target.exists():
            shutil.copy2(target, temporary)
        initialize_supplemental_database_v1(temporary)
        with sqlite3.connect(temporary) as connection:
            connection.execute("BEGIN")
            connection.execute(
                """DELETE FROM stock_capital_daily
                   WHERE trade_date BETWEEN ? AND ? AND source=?""",
                (start, end, capital_source),
            )
            connection.executemany(
                """INSERT INTO stock_capital_daily (
                       stock_code, stock_name, trade_date,
                       vendor_net_amount, float_market_cap, amount_unit,
                       market_cap_unit, source, source_version, fetched_at
                   ) VALUES (?,?,?,?,?,?,?,?,?,?)""",
                capital_rows,
            )
            connection.executemany(
                """INSERT INTO supplemental_metadata(key, value)
                   VALUES (?, ?)
                   ON CONFLICT(key) DO UPDATE SET value=excluded.value""",
                (
                    ("capital_source_version", source_version),
                    ("capital_source", capital_source),
                    ("capital_fetched_at", fetched_at),
                    ("capital_amount_unit", "CNY"),
                    ("capital_volume_raw_unit", "SHARE"),
                    ("capital_turnover_raw_unit", "PERCENT"),
                    (
                        "capital_vendor_ratio_unit",
                        "PERCENT_OF_DAILY_TURNOVER_CNY",
                    ),
                ),
            )
            connection.commit()
            integrity = connection.execute(
                "PRAGMA integrity_check"
            ).fetchone()[0]
            if integrity != "ok":
                raise ValueError(
                    f"supplemental database integrity failed: {integrity}"
                )
        os.replace(temporary, target)
    finally:
        temporary.unlink(missing_ok=True)

    return CapitalBackfillResultV1(
        target_path=target,
        start_date=start,
        end_date=end,
        latest_capital_as_of=sessions[-1],
        stock_count=len(all_codes),
        coverage=coverage,
        source_version=source_version,
    )


def prefetch_public_capital_v1(
    *,
    capital_client: CapitalCacheClientV1,
    market_database_path: Path,
    start_date: str,
    end_date: str,
    batch_size: int,
    source_version: str = CAPITAL_SOURCE_VERSION,
) -> CapitalPrefetchResultV1:
    start = date.fromisoformat(start_date).isoformat()
    end = date.fromisoformat(end_date).isoformat()
    if start > end:
        raise ValueError("start_date must not be after end_date")
    if batch_size < 1:
        raise ValueError("batch_size must be positive")
    if not source_version.strip():
        raise ValueError("source_version is required")

    sessions, universe, _ = _market_universe(
        market_database_path.resolve(strict=True), start, end
    )
    if not sessions:
        raise ValueError("market database has no sessions in requested range")
    all_codes = sorted(set().union(*universe.values()))
    pending = [
        code for code in all_codes
        if not capital_client.has_capital_cache(code)
    ]
    selected = pending[:batch_size]
    for stock_code in selected:
        capital_client.read(stock_code)
    return CapitalPrefetchResultV1(
        stock_count=len(all_codes),
        cached_before=len(all_codes) - len(pending),
        prefetched_count=len(selected),
        remaining_count=len(pending) - len(selected),
        source_version=source_version,
    )


@serialized_supplemental_publication_v1
def backfill_cninfo_membership_v1(
    *,
    industry_client: IndustryHistoryClientV1,
    market_database_path: Path,
    target_path: Path,
    start_date: str,
    end_date: str,
    fetched_at: str,
    source_version: str = MEMBERSHIP_SOURCE_VERSION,
    minimum_membership_coverage: float = 0.99,
) -> MembershipBackfillResultV1:
    start = date.fromisoformat(start_date).isoformat()
    end = date.fromisoformat(end_date).isoformat()
    if start > end:
        raise ValueError("start_date must not be after end_date")
    parsed = datetime.fromisoformat(fetched_at.replace("Z", "+00:00"))
    if parsed.utcoffset() is None:
        raise ValueError("fetched_at must include a timezone")
    if not source_version.strip():
        raise ValueError("source_version is required")
    if not 0 <= minimum_membership_coverage <= 1:
        raise ValueError(
            "minimum_membership_coverage must be between 0 and 1"
        )

    sessions, universe, _ = _market_universe(
        market_database_path.resolve(strict=True), start, end
    )
    if not sessions:
        raise ValueError("market database has no sessions in requested range")
    all_codes = sorted(set().union(*universe.values()))
    membership_rows: list[tuple[object, ...]] = []
    for stock_code in all_codes:
        history = industry_client.read(stock_code, "1990-01-01", end)
        membership_rows.extend(
            _cninfo_sw_l2_intervals(
                stock_code=stock_code,
                rows=history,
                fetched_at=fetched_at,
                source_version=source_version,
            )
        )

    _validate_membership_intervals(membership_rows)
    coverage = _membership_coverage(
        sessions=sessions,
        universe=universe,
        memberships=membership_rows,
    )
    if coverage < minimum_membership_coverage:
        raise ValueError("PIT sector membership coverage is below the frozen threshold")

    target = target_path.resolve()
    target.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{target.name}.", suffix=".tmp", dir=target.parent
    )
    os.close(descriptor)
    temporary = Path(temporary_name)
    try:
        if target.exists():
            shutil.copy2(target, temporary)
        initialize_supplemental_database_v1(temporary)
        with sqlite3.connect(temporary) as connection:
            connection.execute("BEGIN")
            connection.execute(
                "DELETE FROM sector_membership_history WHERE source=?",
                (MEMBERSHIP_SOURCE,),
            )
            connection.executemany(
                """INSERT INTO sector_membership_history (
                       stock_code, stock_name, sector_code, sector_name,
                       sector_level, valid_from, valid_to_exclusive, source,
                       source_version, fetched_at
                   ) VALUES (?,?,?,?,?,?,?,?,?,?)""",
                membership_rows,
            )
            connection.executemany(
                """INSERT INTO supplemental_metadata(key, value)
                   VALUES (?, ?)
                   ON CONFLICT(key) DO UPDATE SET value=excluded.value""",
                (
                    ("membership_source_version", source_version),
                    ("membership_fetched_at", fetched_at),
                    (
                        "membership_classification_code",
                        CNINFO_SW_CLASSIFICATION_CODE,
                    ),
                ),
            )
            connection.commit()
            integrity = connection.execute(
                "PRAGMA integrity_check"
            ).fetchone()[0]
            if integrity != "ok":
                raise ValueError(
                    f"supplemental database integrity failed: {integrity}"
                )
        os.replace(temporary, target)
    finally:
        temporary.unlink(missing_ok=True)

    return MembershipBackfillResultV1(
        target_path=target,
        start_date=start,
        end_date=end,
        membership_available_through=end,
        stock_count=len(all_codes),
        coverage=coverage,
        source_version=source_version,
    )


def derive_float_market_cap_cny_v1(
    *, stock_code: str, row: dict[str, object]
) -> float:
    if str(row.get("volume_unit")) != "SHARE":
        raise ValueError(
            f"BaoStock volume unit must be SHARE for {stock_code}"
        )
    if str(row.get("amount_unit")) != "CNY":
        raise ValueError(
            f"BaoStock amount unit must be CNY for {stock_code}"
        )
    if str(row.get("turnover_unit")) != "PERCENT":
        raise ValueError(
            f"BaoStock turnover unit must be PERCENT for {stock_code}"
        )
    close = _required_positive(row.get("close"))
    volume = _required_positive(row.get("volume"))
    amount = _required_positive(row.get("amount"))
    turnover_percent = _required_positive(row.get("turnover_percent"))

    amount_consistency = amount / (volume * close)
    if not 0.5 <= amount_consistency <= 1.5:
        raise ValueError(
            f"volume/amount unit mismatch for {stock_code}: "
            f"amount/(volume*close)={amount_consistency:.6g}"
        )
    float_shares = volume / (turnover_percent / 100.0)
    if not 1_000_000 <= float_shares <= 1_000_000_000_000:
        raise ValueError(
            f"implausible implied float shares for {stock_code}: "
            f"{float_shares:.6g}"
        )
    return float_shares * close


def _daily_trading_inputs_available(
    *, stock_code: str, row: dict[str, object]
) -> bool:
    for field in ("close", "volume", "amount", "turnover_percent"):
        value = row.get(field)
        if value in (None, ""):
            return False
        number = _required_finite(value)
        if number < 0:
            raise ValueError(
                f"negative BaoStock {field} for {stock_code}: {number}"
            )
        if number == 0:
            return False
    return True


def validate_vendor_flow_against_turnover_cny_v1(
    *,
    stock_code: str,
    flow: dict[str, object],
    daily: dict[str, object],
    mismatch_policy: str = "RAISE",
) -> bool:
    if mismatch_policy not in {"RAISE", "EXCLUDE_ROW"}:
        raise ValueError("unsupported turnover mismatch policy")
    net_amount = _required_finite(flow.get("vendor_net_amount"))
    net_ratio = _required_finite(flow.get("vendor_net_ratio_percent"))
    turnover_amount = _required_positive(daily.get("amount"))
    if not -100 <= net_ratio <= 100:
        raise ValueError(
            f"implausible vendor net ratio for {stock_code}: {net_ratio}"
        )
    if abs(net_amount) > turnover_amount * 1.01:
        if mismatch_policy == "EXCLUDE_ROW":
            return False
        raise ValueError(
            f"vendor flow/turnover unit mismatch for {stock_code}: "
            "absolute net amount exceeds daily turnover"
        )
    if abs(net_ratio) < 0.1:
        return True
    implied_turnover = abs(net_amount) / (abs(net_ratio) / 100.0)
    consistency = implied_turnover / turnover_amount
    minimum_consistency = _required_positive(
        flow.get("turnover_consistency_min", 0.9)
    )
    maximum_consistency = _required_positive(
        flow.get("turnover_consistency_max", 1.1)
    )
    if minimum_consistency > maximum_consistency:
        raise ValueError("invalid flow turnover consistency range")
    if not minimum_consistency <= consistency <= maximum_consistency:
        if mismatch_policy == "EXCLUDE_ROW":
            return False
        raise ValueError(
            f"vendor flow ratio/turnover unit mismatch for {stock_code}: "
            f"implied/observed turnover={consistency:.6g}"
        )
    return True


def parse_eastmoney_capital_payload_v1(
    payload: object,
) -> tuple[dict[str, object], ...]:
    if not isinstance(payload, dict) or payload.get("rc") != 0:
        raise ValueError("Eastmoney capital response is not successful")
    data = payload.get("data")
    if data is None:
        return ()
    if not isinstance(data, dict) or not isinstance(data.get("klines"), list):
        raise ValueError("Eastmoney capital response has invalid data")
    rows: list[dict[str, object]] = []
    for raw in data["klines"]:
        values = str(raw).split(",")
        if len(values) != 15:
            raise ValueError("Eastmoney capital row has invalid field count")
        rows.append({
            "trade_date": _iso_date(values[0]),
            "vendor_net_amount": _required_finite(values[1]),
            "vendor_net_ratio_percent": _required_finite(values[6]),
            "amount_unit": "CNY",
            "ratio_unit": "PERCENT_OF_DAILY_TURNOVER",
        })
    return tuple(rows)


def parse_sina_capital_payload_v1(
    payload: object,
) -> tuple[dict[str, object], ...]:
    if not isinstance(payload, list):
        raise ValueError("Sina capital response must be a list")
    rows: list[dict[str, object]] = []
    for raw in payload:
        if not isinstance(raw, dict):
            raise ValueError("Sina capital row must be an object")
        main_ratio_percent = _sina_ratio_percent(
            raw, amount_key="r0_net", ratio_key="r0_ratio"
        )
        total_ratio_percent = _sina_ratio_percent(
            raw, amount_key="netamount", ratio_key="ratioamount"
        )
        row_status = (
            "VALID"
            if (
                abs(main_ratio_percent) <= 100
                and abs(total_ratio_percent) <= 100
            )
            else "INVALID_VENDOR_RATIO"
        )
        rows.append({
            "trade_date": _iso_date(raw.get("opendate")),
            "vendor_net_amount": _required_finite(raw.get("r0_net")),
            "vendor_net_ratio_percent": main_ratio_percent,
            "vendor_total_net_amount": _required_finite(
                raw.get("netamount")
            ),
            "vendor_total_net_ratio_percent": total_ratio_percent,
            "vendor_turnover_raw": raw.get("turnover"),
            "turnover_raw_unit": "SINA_LEGACY_UNVERIFIED",
            "vendor_flow_definition": "SINA_SINGLE_TRADE_GTE_CNY_1M",
            "vendor_row_status": row_status,
            "turnover_consistency_min": 0.75,
            "turnover_consistency_max": 1.25,
            "amount_unit": "CNY",
            "ratio_unit": "PERCENT_OF_DAILY_TURNOVER",
        })
    return tuple(rows)


def _sina_ratio_percent(
    row: dict[str, object], *, amount_key: str, ratio_key: str
) -> float:
    amount = _required_finite(row.get(amount_key))
    ratio = row.get(ratio_key)
    if ratio is None:
        if amount == 0:
            return 0.0
        raise ValueError(f"Sina {ratio_key} is missing for nonzero {amount_key}")
    return _required_finite(ratio) * 100.0


def _read_eastmoney_fund_flow_payload(
    *, stock_code: str, market_number: str
) -> object:
    params = {
        "lmt": "100",
        "klt": "101",
        "secid": f"{market_number}.{stock_code}",
        "fields1": "f1,f2,f3,f7",
        "fields2": (
            "f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61,f62,f63,f64,f65"
        ),
        "ut": "b2884a393a59ad64002292a3e90d46a5",
    }
    request = Request(
        "https://push2his.eastmoney.com/api/qt/stock/fflow/daykline/get?"
        + urlencode(params),
        headers={
            "User-Agent": "Mozilla/5.0",
            "Referer": "https://data.eastmoney.com/",
        },
    )
    try:
        response = urlopen(request, timeout=15)
    except (HTTPError, HTTPException, URLError):
        response = build_opener(ProxyHandler({})).open(request, timeout=15)
    with response:
        return json.loads(response.read().decode("utf-8"))


def _read_sina_fund_flow_payload(
    *, stock_code: str, symbol: str, days: int
) -> object:
    params = {
        "page": "1",
        "num": str(days),
        "sort": "opendate",
        "asc": "0",
        "daima": symbol,
    }
    request = Request(
        "https://vip.stock.finance.sina.com.cn/quotes_service/api/"
        "json_v2.php/MoneyFlow.ssl_qsfx_zjlrqs?"
        + urlencode(params),
        headers={
            "User-Agent": "Mozilla/5.0",
            "Referer": "https://finance.sina.com.cn/",
        },
    )
    with urlopen(request, timeout=15) as response:
        return json.loads(response.read().decode("utf-8"))


def _sina_symbol(stock_code: str) -> str:
    if stock_code.startswith(("8", "92")):
        return f"bj{stock_code}"
    if stock_code.startswith(("5", "6", "9")):
        return f"sh{stock_code}"
    return f"sz{stock_code}"


def _cninfo_sw_l2_intervals(
    *,
    stock_code: str,
    rows: Sequence[dict[str, object]],
    fetched_at: str,
    source_version: str,
) -> list[tuple[object, ...]]:
    selected: dict[str, tuple[str | None, str]] = {}
    for row in rows:
        if str(row.get("classification_code")) != CNINFO_SW_CLASSIFICATION_CODE:
            continue
        valid_from = _iso_date(row.get("valid_from"))
        sector_name = _optional_string(row.get("industry_l2_name"))
        if sector_name is None:
            raise ValueError(f"CNInfo SW L2 name missing for {stock_code}")
        current = selected.get(valid_from)
        candidate = (_optional_string(row.get("stock_name")), sector_name)
        if current is not None and current[1] != sector_name:
            raise ValueError(
                f"ambiguous CNInfo SW L2 membership for {stock_code} "
                f"on {valid_from}"
            )
        selected[valid_from] = (
            candidate[0] if candidate[0] is not None else (
                current[0] if current is not None else None
            ),
            sector_name,
        )

    ordered = sorted(selected.items())
    result: list[tuple[object, ...]] = []
    for index, (valid_from, (stock_name, sector_name)) in enumerate(ordered):
        valid_to = ordered[index + 1][0] if index + 1 < len(ordered) else None
        result.append((
            stock_code,
            stock_name,
            f"CNINFO_SW_L2:{sector_name}",
            sector_name,
            "L2",
            valid_from,
            valid_to,
            MEMBERSHIP_SOURCE,
            source_version,
            fetched_at,
        ))
    return result


def _market_universe(
    database_path: Path, start: str, end: str
) -> tuple[tuple[str, ...], dict[str, set[str]], dict[str, str | None]]:
    with sqlite3.connect(
        f"{database_path.as_uri()}?mode=ro", uri=True
    ) as connection:
        connection.execute("PRAGMA query_only = ON")
        columns = {
            str(row[1])
            for row in connection.execute("PRAGMA table_info(stock_daily)")
        }
        name_sql = "stock_name" if "stock_name" in columns else "NULL"
        rows = connection.execute(
            f"""SELECT trade_date, stock_code, {name_sql}
                FROM stock_daily
                WHERE trade_date BETWEEN ? AND ?
                ORDER BY trade_date, stock_code""",
            (start, end),
        ).fetchall()
    universe: dict[str, set[str]] = {}
    names: dict[str, str | None] = {}
    for trade_date, stock_code, stock_name in rows:
        code = str(stock_code)
        if not _public_source_supported(code):
            continue
        universe.setdefault(str(trade_date), set()).add(code)
        if code not in names or names[code] is None:
            names[code] = _optional_string(stock_name)
    return tuple(sorted(universe)), universe, names


def _public_source_supported(stock_code: str) -> bool:
    return stock_code.startswith(("00", "30", "60", "68"))


def _membership_coverage(
    *,
    sessions: Sequence[str],
    universe: dict[str, set[str]],
    memberships: Sequence[tuple[object, ...]],
) -> float:
    total = 0
    covered = 0
    for session in sessions:
        active = {
            str(row[0])
            for row in memberships
            if str(row[5]) <= session
            and (row[6] is None or str(row[6]) > session)
        }
        total += len(universe[session])
        covered += len(universe[session] & active)
    return covered / total if total else 0.0


def _validate_membership_intervals(
    memberships: Sequence[tuple[object, ...]],
) -> None:
    by_stock: dict[str, list[tuple[str, str | None]]] = {}
    for row in memberships:
        by_stock.setdefault(str(row[0]), []).append(
            (str(row[5]), _optional_string(row[6]))
        )
    for stock_code, intervals in by_stock.items():
        ordered = sorted(intervals)
        for previous, current in zip(ordered, ordered[1:]):
            if previous[1] is None or previous[1] > current[0]:
                raise ValueError(
                    f"overlapping L2 memberships for stock {stock_code}"
                )


def _iso_date(value: object) -> str:
    if isinstance(value, date):
        return value.isoformat()
    return date.fromisoformat(str(value)[:10]).isoformat()


def _required_finite(value: object) -> float:
    result = float(value)
    if not math.isfinite(result):
        raise ValueError("numeric value must be finite")
    return result


def _required_positive(value: object) -> float:
    result = _required_finite(value)
    if result <= 0:
        raise ValueError("numeric value must be positive")
    return result


def _optional_string(value: object) -> str | None:
    if value is None or str(value).strip() in {"", "nan", "None"}:
        return None
    return str(value)


def _retry(
    call,
    *,
    attempts: int,
    label: str,
    base_delay_seconds: float = 0.5,
):
    last_error: Exception | None = None
    for attempt in range(attempts):
        try:
            return call()
        except Exception as exc:
            last_error = exc
            if attempt + 1 < attempts:
                time.sleep(base_delay_seconds * (2 ** attempt))
    raise RuntimeError(f"{label} failed after {attempts} attempts") from last_error


def _caused_by_missing_column(error: BaseException, column: str) -> bool:
    current: BaseException | None = error
    while current is not None:
        if isinstance(current, KeyError) and current.args == (column,):
            return True
        current = current.__cause__
    return False


def _write_immutable_json(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        return
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    os.close(descriptor)
    temporary = Path(temporary_name)
    try:
        temporary.write_text(
            json.dumps(
                payload,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ),
            encoding="utf-8",
        )
        try:
            os.link(temporary, path)
        except FileExistsError:
            pass
    finally:
        temporary.unlink(missing_ok=True)
