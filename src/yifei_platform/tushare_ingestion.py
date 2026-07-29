from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta
import json
import os
from pathlib import Path
import shutil
import sqlite3
import tempfile
from typing import Iterable
from urllib.request import Request, urlopen

from .supplemental_facts import (
    initialize_supplemental_database_v1,
    serialized_supplemental_publication_v1,
)


TUSHARE_API_URL = "https://api.tushare.pro"
TUSHARE_SOURCE_VERSION = "tushare.moneyflow-ths.daily-basic.sw2021.v1"


@dataclass(frozen=True)
class TushareBackfillResultV1:
    target_path: Path
    start_date: str
    end_date: str
    latest_capital_as_of: str
    membership_available_through: str
    capital_coverage: float
    membership_coverage: float
    source_version: str


class TushareApiClientV1:
    def __init__(self, token: str, *, endpoint: str = TUSHARE_API_URL):
        if not token.strip():
            raise ValueError("Tushare token is required")
        self._token = token
        self._endpoint = endpoint

    def query(
        self,
        api_name: str,
        *,
        params: dict[str, object],
        fields: tuple[str, ...],
    ) -> tuple[dict[str, object], ...]:
        payload = json.dumps({
            "api_name": api_name,
            "token": self._token,
            "params": params,
            "fields": ",".join(fields),
        }).encode("utf-8")
        request = Request(
            self._endpoint,
            data=payload,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urlopen(request, timeout=60) as response:
            body = json.loads(response.read().decode("utf-8"))
        if body.get("code") != 0:
            raise RuntimeError(
                f"Tushare {api_name} failed: {body.get('msg', 'unknown error')}"
            )
        data = body.get("data") or {}
        names = data.get("fields") or []
        rows = data.get("items") or []
        if not isinstance(names, list) or not isinstance(rows, list):
            raise RuntimeError(f"Tushare {api_name} returned malformed data")
        return tuple(dict(zip(names, row, strict=True)) for row in rows)


@serialized_supplemental_publication_v1
def backfill_tushare_supplemental_v1(
    *,
    client: TushareApiClientV1,
    market_database_path: Path,
    target_path: Path,
    start_date: str,
    end_date: str,
    fetched_at: str,
    source_version: str = TUSHARE_SOURCE_VERSION,
    minimum_capital_coverage: float = 0.98,
    minimum_membership_coverage: float = 0.99,
) -> TushareBackfillResultV1:
    start = date.fromisoformat(start_date).isoformat()
    end = date.fromisoformat(end_date).isoformat()
    if start > end:
        raise ValueError("start_date must not be after end_date")
    parsed = datetime.fromisoformat(fetched_at.replace("Z", "+00:00"))
    if parsed.utcoffset() is None:
        raise ValueError("fetched_at must include a timezone")
    if not 0 <= minimum_capital_coverage <= 1:
        raise ValueError("minimum_capital_coverage must be between 0 and 1")
    if not 0 <= minimum_membership_coverage <= 1:
        raise ValueError("minimum_membership_coverage must be between 0 and 1")

    market_path = market_database_path.resolve(strict=True)
    sessions, universe = _market_universe(market_path, start, end)
    if not sessions:
        raise ValueError("market database has no sessions in requested range")

    capital_rows: list[tuple[object, ...]] = []
    covered_capital = 0
    expected_capital = 0
    for session in sessions:
        tushare_date = session.replace("-", "")
        moneyflow = {
            _stock_code(row["ts_code"]): row
            for row in client.query(
                "moneyflow_ths",
                params={"trade_date": tushare_date},
                fields=("ts_code", "trade_date", "name", "net_amount"),
            )
        }
        daily_basic = {
            _stock_code(row["ts_code"]): row
            for row in client.query(
                "daily_basic",
                params={"trade_date": tushare_date},
                fields=("ts_code", "trade_date", "circ_mv"),
            )
        }
        expected_codes = universe[session]
        expected_capital += len(expected_codes)
        for stock_code in sorted(expected_codes):
            flow = moneyflow.get(stock_code)
            basic = daily_basic.get(stock_code)
            if (
                flow is None
                or basic is None
                or flow.get("net_amount") is None
                or basic.get("circ_mv") is None
            ):
                continue
            covered_capital += 1
            capital_rows.append((
                stock_code,
                _optional_string(flow.get("name")),
                session,
                float(flow["net_amount"]),
                float(basic["circ_mv"]),
                "CNY_10K",
                "CNY_10K",
                "tushare.moneyflow_ths+daily_basic",
                source_version,
                fetched_at,
            ))
    capital_coverage = (
        covered_capital / expected_capital if expected_capital else 0.0
    )
    if capital_coverage < minimum_capital_coverage:
        raise ValueError("stock capital coverage is below the frozen threshold")
    latest_session = sessions[-1]
    latest_observed = {
        str(row[0]) for row in capital_rows if str(row[2]) == latest_session
    }
    latest_expected = universe[latest_session]
    latest_coverage = (
        len(latest_observed & latest_expected) / len(latest_expected)
        if latest_expected else 0.0
    )
    if latest_coverage < minimum_capital_coverage:
        raise ValueError(
            "stock capital coverage is below the frozen threshold "
            f"for latest session {latest_session}"
        )

    membership_rows = _fetch_sw_l2_memberships(
        client=client,
        fetched_at=fetched_at,
        source_version=source_version,
    )
    membership_rows = _bound_membership_rows(
        membership_rows,
        start=start,
        end=end,
    )
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
            conflicting_capital = next((
                (str(row[0]), str(row[2]))
                for row in capital_rows
                if connection.execute(
                    """SELECT 1 FROM stock_capital_daily
                       WHERE stock_code=? AND trade_date=? AND source<>?
                       LIMIT 1""",
                    (
                        str(row[0]),
                        str(row[2]),
                        "tushare.moneyflow_ths+daily_basic",
                    ),
                ).fetchone()
            ), None)
            if conflicting_capital is not None:
                raise ValueError(
                    "cross-source stock capital key conflict; "
                    "publish to a separate target"
                )
            existing_metadata = dict(connection.execute(
                """SELECT key,value FROM supplemental_metadata
                   WHERE key IN (
                       'membership_available_from',
                       'membership_available_through'
                   )"""
            ))
            membership_available_from = min(
                start,
                existing_metadata.get("membership_available_from", start),
            )
            membership_available_through = max(
                end,
                existing_metadata.get("membership_available_through", end),
            )
            connection.execute(
                """DELETE FROM stock_capital_daily
                   WHERE trade_date BETWEEN ? AND ? AND source=?""",
                (start, end, "tushare.moneyflow_ths+daily_basic"),
            )
            connection.executemany(
                """INSERT INTO stock_capital_daily (
                       stock_code, stock_name, trade_date,
                       vendor_net_amount, float_market_cap, amount_unit,
                       market_cap_unit, source, source_version, fetched_at
                   ) VALUES (?,?,?,?,?,?,?,?,?,?)""",
                capital_rows,
            )
            existing_memberships = connection.execute(
                """SELECT stock_code, stock_name, sector_code, sector_name,
                          sector_level, valid_from, valid_to_exclusive,
                          source, source_version, fetched_at
                   FROM sector_membership_history
                   WHERE source=?
                   ORDER BY stock_code, valid_from, sector_code""",
                ("tushare.index_member_all",),
            ).fetchall()
            preserved_memberships = _subtract_membership_window(
                existing_memberships,
                start=start,
                end=end,
            )
            merged_memberships = sorted(
                {tuple(row) for row in (
                    preserved_memberships + membership_rows
                )},
                key=lambda row: (str(row[0]), str(row[5]), str(row[2])),
            )
            _validate_membership_intervals(merged_memberships)
            connection.execute(
                "DELETE FROM sector_membership_history WHERE source=?",
                ("tushare.index_member_all",),
            )
            connection.executemany(
                """INSERT INTO sector_membership_history (
                       stock_code, stock_name, sector_code, sector_name,
                       sector_level, valid_from, valid_to_exclusive, source,
                       source_version, fetched_at
                   ) VALUES (?,?,?,?,?,?,?,?,?,?)""",
                merged_memberships,
            )
            connection.executemany(
                """INSERT INTO supplemental_metadata(key, value)
                   VALUES (?, ?)
                   ON CONFLICT(key) DO UPDATE SET value=excluded.value""",
                (
                    ("capital_source_version", source_version),
                    ("capital_fetched_at", fetched_at),
                    ("membership_source_version", source_version),
                    ("membership_fetched_at", fetched_at),
                    (
                        "membership_available_from",
                        membership_available_from,
                    ),
                    (
                        "membership_available_through",
                        membership_available_through,
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

    return TushareBackfillResultV1(
        target_path=target,
        start_date=start,
        end_date=end,
        latest_capital_as_of=sessions[-1],
        membership_available_through=membership_available_through,
        capital_coverage=capital_coverage,
        membership_coverage=membership_coverage,
        source_version=source_version,
    )


def _fetch_sw_l2_memberships(
    *,
    client: TushareApiClientV1,
    fetched_at: str,
    source_version: str,
) -> list[tuple[object, ...]]:
    sectors = client.query(
        "index_classify",
        params={"level": "L2", "src": "SW2021"},
        fields=("index_code", "industry_name", "level", "src"),
    )
    rows: list[tuple[object, ...]] = []
    for sector in sectors:
        sector_code = str(sector["index_code"])
        for is_new in ("Y", "N"):
            members = client.query(
                "index_member_all",
                params={"l2_code": sector_code, "is_new": is_new},
                fields=(
                    "l2_code", "l2_name", "ts_code", "name",
                    "in_date", "out_date", "is_new",
                ),
            )
            for member in members:
                valid_from = _iso_compact_date(member.get("in_date"))
                if valid_from is None:
                    raise ValueError(
                        "sector membership valid_from is missing"
                    )
                rows.append((
                    _stock_code(member["ts_code"]),
                    _optional_string(member.get("name")),
                    str(member.get("l2_code") or sector_code),
                    _optional_string(
                        member.get("l2_name")
                        or sector.get("industry_name")
                    ),
                    "L2",
                    valid_from,
                    _iso_compact_date(member.get("out_date")),
                    "tushare.index_member_all",
                    source_version,
                    fetched_at,
                ))
    unique = {tuple(row) for row in rows}
    return sorted(unique, key=lambda row: (str(row[0]), str(row[5]), str(row[2])))


def _market_universe(
    database_path: Path, start: str, end: str
) -> tuple[tuple[str, ...], dict[str, set[str]]]:
    with sqlite3.connect(
        f"{database_path.resolve().as_uri()}?mode=ro", uri=True
    ) as connection:
        connection.execute("PRAGMA query_only = ON")
        rows = connection.execute(
            """SELECT trade_date, stock_code
               FROM stock_daily
               WHERE trade_date BETWEEN ? AND ?
               ORDER BY trade_date, stock_code""",
            (start, end),
        ).fetchall()
    universe: dict[str, set[str]] = {}
    for trade_date, stock_code in rows:
        universe.setdefault(str(trade_date), set()).add(str(stock_code))
    return tuple(sorted(universe)), universe


def _membership_coverage(
    *,
    sessions: Iterable[str],
    universe: dict[str, set[str]],
    memberships: list[tuple[object, ...]],
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
        expected = universe[session]
        total += len(expected)
        covered += len(expected & active)
    return covered / total if total else 0.0


def _bound_membership_rows(
    memberships: list[tuple[object, ...]],
    *,
    start: str,
    end: str,
) -> list[tuple[object, ...]]:
    """Restrict fetched intervals to the range whose coverage was validated."""
    end_exclusive = (
        date.fromisoformat(end) + timedelta(days=1)
    ).isoformat()
    bounded: list[tuple[object, ...]] = []
    for row in memberships:
        valid_from = str(row[5])
        valid_to = _optional_string(row[6])
        bounded_from = max(valid_from, start)
        bounded_to = min(valid_to or end_exclusive, end_exclusive)
        if bounded_from >= bounded_to:
            continue
        updated = list(row)
        updated[5] = bounded_from
        updated[6] = bounded_to
        bounded.append(tuple(updated))
    return bounded


def _subtract_membership_window(
    memberships: Iterable[tuple[object, ...]],
    *,
    start: str,
    end: str,
) -> list[tuple[object, ...]]:
    end_exclusive = (
        date.fromisoformat(end) + timedelta(days=1)
    ).isoformat()
    preserved: list[tuple[object, ...]] = []
    for raw in memberships:
        row = tuple(raw)
        valid_from = str(row[5])
        valid_to = _optional_string(row[6])
        if (
            (valid_to is not None and valid_to <= start)
            or valid_from >= end_exclusive
        ):
            preserved.append(row)
            continue
        if valid_from < start:
            left = list(row)
            left[6] = start
            preserved.append(tuple(left))
        if valid_to is None or valid_to > end_exclusive:
            right = list(row)
            right[5] = end_exclusive
            preserved.append(tuple(right))
    return preserved


def _validate_membership_intervals(
    memberships: list[tuple[object, ...]],
) -> None:
    # Frozen v1 readers require exactly one active L2 membership per stock.
    # Concurrent L2 intervals are ambiguous facts and must fail closed.
    by_stock: dict[str, list[tuple[str, str | None, str]]] = {}
    for row in memberships:
        by_stock.setdefault(str(row[0]), []).append(
            (str(row[5]), _optional_string(row[6]), str(row[2]))
        )
    for stock_code, intervals in by_stock.items():
        ordered = sorted(intervals)
        for previous, current in zip(ordered, ordered[1:]):
            previous_end = previous[1]
            if previous_end is None or previous_end > current[0]:
                raise ValueError(
                    f"overlapping L2 memberships for stock {stock_code}"
                )


def _stock_code(value: object) -> str:
    code = str(value).split(".", 1)[0]
    if len(code) != 6 or not code.isdigit():
        raise ValueError(f"unsupported stock code: {value}")
    return code


def _iso_compact_date(value: object) -> str | None:
    if value is None or str(value).strip() == "":
        return None
    raw = str(value)
    return date(
        int(raw[0:4]), int(raw[4:6]), int(raw[6:8])
    ).isoformat()


def _optional_string(value: object) -> str | None:
    return None if value is None or str(value) == "" else str(value)
