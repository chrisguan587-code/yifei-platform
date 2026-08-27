from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
import math
import os
from pathlib import Path
import shutil
import sqlite3
import tempfile

from .supplemental_facts import (
    initialize_supplemental_database_v1,
    serialized_supplemental_publication_v1,
)


SECTOR_LEVEL = "THS_L2"
SW_SECTOR_LEVEL = "L2"
SECTOR_MARKET_SOURCE = "platform.stock_daily+sector_membership_history"
SECTOR_MARKET_SOURCE_VERSION = "platform-stock-daily-ths-l2.v1"
SW_SECTOR_MARKET_SOURCE_VERSION = "platform-stock-daily-cninfo-sw-l2.v2"
MINIMUM_SECTOR_COUNT = 80
MINIMUM_SW_SECTOR_COUNT = 120
MINIMUM_MEMBER_COVERAGE = 0.95
MINIMUM_MARKET_AMOUNT_COVERAGE = 0.97
MINIMUM_HISTORICAL_MARKET_AMOUNT_COVERAGE = 0.95
HISTORY_SESSIONS = 30
SW_HISTORY_SESSIONS = 20


@dataclass(frozen=True)
class SectorMarketPublishResultV1:
    target_path: Path
    as_of: str
    first_published_date: str | None
    published_session_count: int
    inserted_row_count: int
    latest_available_as_of: str
    source_version: str
    sector_level: str
    market_amount_coverage: float


@serialized_supplemental_publication_v1
def publish_sector_market_daily_v1(
    *, market_database_path: Path, target_path: Path, as_of: str,
    published_at: str,
    source_version: str = SECTOR_MARKET_SOURCE_VERSION,
) -> SectorMarketPublishResultV1:
    return _publish_sector_market_daily(
        market_database_path=market_database_path,
        target_path=target_path,
        as_of=as_of,
        published_at=published_at,
        source_version=source_version,
        sector_level=SECTOR_LEVEL,
        minimum_sector_count=MINIMUM_SECTOR_COUNT,
        minimum_market_amount_coverage=None,
        historical_market_amount_coverage=None,
        history_sessions=HISTORY_SESSIONS,
    )


@serialized_supplemental_publication_v1
def publish_sector_market_daily_v2(
    *, market_database_path: Path, target_path: Path, as_of: str,
    published_at: str,
    source_version: str = SW_SECTOR_MARKET_SOURCE_VERSION,
) -> SectorMarketPublishResultV1:
    return _publish_sector_market_daily(
        market_database_path=market_database_path,
        target_path=target_path,
        as_of=as_of,
        published_at=published_at,
        source_version=source_version,
        sector_level=SW_SECTOR_LEVEL,
        minimum_sector_count=MINIMUM_SW_SECTOR_COUNT,
        minimum_market_amount_coverage=MINIMUM_MARKET_AMOUNT_COVERAGE,
        historical_market_amount_coverage=(
            MINIMUM_HISTORICAL_MARKET_AMOUNT_COVERAGE
        ),
        history_sessions=SW_HISTORY_SESSIONS,
    )


def _publish_sector_market_daily(
    *, market_database_path: Path, target_path: Path, as_of: str,
    published_at: str, source_version: str, sector_level: str,
    minimum_sector_count: int,
    minimum_market_amount_coverage: float | None,
    historical_market_amount_coverage: float | None,
    history_sessions: int,
) -> SectorMarketPublishResultV1:
    requested = date.fromisoformat(as_of).isoformat()
    timestamp = datetime.fromisoformat(published_at.replace("Z", "+00:00"))
    if timestamp.utcoffset() is None:
        raise ValueError("published_at must include a timezone")
    if not source_version.strip():
        raise ValueError("source_version is required")

    sessions = _market_sessions(
        market_database_path, requested, history_sessions=history_sessions,
    )
    target = target_path.resolve()
    target.parent.mkdir(parents=True, exist_ok=True)
    initialize_supplemental_database_v1(target)
    existing = _existing_dates(
        target, sessions, sector_level=sector_level,
        source_version=source_version,
        minimum_sector_count=minimum_sector_count,
    )
    for session in existing:
        amount_coverage = _stored_market_amount_coverage(
            market_database_path=market_database_path,
            target_path=target,
            trade_date=session,
            sector_level=sector_level,
            source_version=source_version,
        )
        _require_market_amount_coverage(
            session=session,
            requested=requested,
            amount_coverage=amount_coverage,
            current_minimum=minimum_market_amount_coverage,
            historical_minimum=historical_market_amount_coverage,
        )
    missing = tuple(session for session in sessions if session not in existing)
    if not missing:
        market_amount_coverage = _stored_market_amount_coverage(
            market_database_path=market_database_path,
            target_path=target,
            trade_date=requested,
            sector_level=sector_level,
            source_version=source_version,
        )
        return SectorMarketPublishResultV1(
            target_path=target, as_of=requested, first_published_date=None,
            published_session_count=0, inserted_row_count=0,
            latest_available_as_of=requested, source_version=source_version,
            sector_level=sector_level,
            market_amount_coverage=market_amount_coverage,
        )

    memberships, membership_version = _active_memberships(
        target, sessions[0], sessions[-1], sector_level=sector_level,
    )
    rows, amount_coverages = _aggregate_rows(
        market_database_path=market_database_path,
        dates=missing,
        memberships=memberships,
        membership_source_version=membership_version,
        published_at=published_at,
        source_version=source_version,
        sector_level=sector_level,
    )
    for session in missing:
        count = sum(row[3] == session for row in rows)
        if count < minimum_sector_count:
            raise ValueError(
                f"sector coverage below {minimum_sector_count} for "
                f"{session}: {count}"
            )
        amount_coverage = amount_coverages[session]
        _require_market_amount_coverage(
            session=session,
            requested=requested,
            amount_coverage=amount_coverage,
            current_minimum=minimum_market_amount_coverage,
            historical_minimum=historical_market_amount_coverage,
        )

    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{target.name}.", suffix=".tmp", dir=target.parent,
    )
    os.close(descriptor)
    temporary = Path(temporary_name)
    try:
        shutil.copy2(target, temporary)
        initialize_supplemental_database_v1(temporary)
        with sqlite3.connect(temporary) as connection:
            metadata_prefix = (
                "sector_market" if sector_level == SECTOR_LEVEL
                else "sector_market_sw_l2"
            )
            connection.executemany(
                """INSERT INTO sector_market_daily (
                       sector_code,sector_name,sector_level,trade_date,
                       member_count,observed_member_count,
                       equal_weight_return_pct,amount,amount_unit,coverage,
                       source,source_version,membership_source_version,
                       published_at
                   ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                rows,
            )
            connection.executemany(
                """INSERT INTO supplemental_metadata(key,value) VALUES (?,?)
                   ON CONFLICT(key) DO UPDATE SET value=excluded.value""",
                (
                    (f"{metadata_prefix}_source_version", source_version),
                    (
                        f"{metadata_prefix}_membership_source_version",
                        membership_version,
                    ),
                    (f"{metadata_prefix}_published_at", published_at),
                ),
            )
            connection.commit()
            integrity = connection.execute("PRAGMA integrity_check").fetchone()[0]
            if integrity != "ok":
                raise ValueError(
                    f"supplemental database integrity failed: {integrity}"
                )
        os.replace(temporary, target)
    finally:
        temporary.unlink(missing_ok=True)

    return SectorMarketPublishResultV1(
        target_path=target, as_of=requested,
        first_published_date=missing[0],
        published_session_count=len(missing), inserted_row_count=len(rows),
        latest_available_as_of=missing[-1], source_version=source_version,
        sector_level=sector_level,
        market_amount_coverage=amount_coverages[missing[-1]],
    )


def _market_sessions(
    market_database_path: Path, as_of: str, *, history_sessions: int,
) -> tuple[str, ...]:
    with sqlite3.connect(
        f"file:{market_database_path.resolve()}?mode=ro", uri=True,
    ) as connection:
        current_count = int(connection.execute(
            "SELECT COUNT(*) FROM stock_daily WHERE trade_date=?", (as_of,),
        ).fetchone()[0])
        if current_count == 0:
            raise ValueError(f"stock_daily_as_of_missing:{as_of}")
        sessions = tuple(
            str(row[0]) for row in connection.execute(
                """SELECT trade_date FROM trading_calendar
                   WHERE trade_date<=? ORDER BY trade_date DESC LIMIT ?""",
                (as_of, history_sessions),
            )
        )[::-1]
    if len(sessions) < history_sessions or sessions[-1] != as_of:
        raise ValueError(f"insufficient_market_history:{len(sessions)}")
    return sessions


def _existing_dates(
    target_path: Path, sessions: tuple[str, ...], *, sector_level: str,
    source_version: str, minimum_sector_count: int,
) -> set[str]:
    placeholders = ",".join("?" for _ in sessions)
    with sqlite3.connect(
        f"file:{target_path.resolve()}?mode=ro", uri=True,
    ) as connection:
        rows = connection.execute(
            f"""SELECT trade_date,COUNT(*),COUNT(DISTINCT source_version),
                       MIN(source_version),MAX(source_version)
                FROM sector_market_daily
                WHERE sector_level=? AND trade_date IN ({placeholders})
                GROUP BY trade_date""",
            (sector_level, *sessions),
        )
        existing = tuple(rows)
        counts = {str(day): int(count) for day, count, _, _, _ in existing}
        mismatched = {
            str(day): (str(minimum), str(maximum))
            for day, _, version_count, minimum, maximum in existing
            if int(version_count) != 1 or str(minimum) != source_version
            or str(maximum) != source_version
        }
    if mismatched:
        raise ValueError(f"existing_sector_market_source_mismatch:{mismatched}")
    incomplete = {
        day: count for day, count in counts.items()
        if count < minimum_sector_count
    }
    if incomplete:
        raise ValueError(f"existing_sector_market_incomplete:{incomplete}")
    return set(counts)


def _active_memberships(
    target_path: Path, start_date: str, end_date: str, *, sector_level: str,
) -> tuple[dict[str, tuple[tuple[str, str, str], ...]], str]:
    with sqlite3.connect(
        f"file:{target_path.resolve()}?mode=ro", uri=True,
    ) as connection:
        rows = connection.execute(
            """SELECT stock_code,sector_code,sector_name,source_version,
                      valid_from,valid_to_exclusive
               FROM sector_membership_history
               WHERE sector_level=? AND valid_from<=?
                 AND (valid_to_exclusive IS NULL OR valid_to_exclusive>?)""",
            (sector_level, end_date, start_date),
        ).fetchall()
    versions = {str(row[3]) for row in rows}
    if len(versions) != 1:
        raise ValueError(
            f"exact_sector_membership_source_version_count:{len(versions)}"
        )
    memberships: dict[str, list[tuple[str, str, str]]] = {}
    for stock_code, sector_code, sector_name, _, valid_from, valid_to in rows:
        memberships.setdefault(str(stock_code), []).append(
            (str(sector_code), str(sector_name), f"{valid_from}|{valid_to or ''}")
        )
    return {key: tuple(value) for key, value in memberships.items()}, versions.pop()


def _aggregate_rows(
    *, market_database_path: Path, dates: tuple[str, ...],
    memberships: dict[str, tuple[tuple[str, str, str], ...]],
    membership_source_version: str, published_at: str, source_version: str,
    sector_level: str,
) -> tuple[tuple[tuple[object, ...], ...], dict[str, float]]:
    result: list[tuple[object, ...]] = []
    amount_coverages: dict[str, float] = {}
    with sqlite3.connect(
        f"file:{market_database_path.resolve()}?mode=ro", uri=True,
    ) as connection:
        for trade_date in dates:
            stocks = {
                str(code): (pct_chg, amount)
                for code, pct_chg, amount in connection.execute(
                    """SELECT stock_code,pct_chg,amount FROM stock_daily
                       WHERE trade_date=?""",
                    (trade_date,),
                )
            }
            market_amount = sum(
                value for _, raw_amount in stocks.values()
                if (value := _finite(raw_amount)) is not None and value >= 0
            )
            if market_amount <= 0:
                raise ValueError(f"market_amount_unavailable:{trade_date}")
            sectors: dict[str, dict[str, object]] = {}
            assigned: set[str] = set()
            for stock_code, intervals in memberships.items():
                active = [
                    (sector_code, sector_name)
                    for sector_code, sector_name, interval in intervals
                    if _contains(interval, trade_date)
                ]
                if len(active) > 1:
                    raise ValueError(
                        f"ambiguous_sector_membership:{stock_code}:{trade_date}"
                    )
                if not active:
                    continue
                assigned.add(stock_code)
                sector_code, sector_name = active[0]
                sector = sectors.setdefault(sector_code, {
                    "name": sector_name, "members": 0, "returns": [], "amount": 0.0,
                })
                sector["members"] = int(sector["members"]) + 1
                values = stocks.get(stock_code)
                if values is None:
                    continue
                pct_chg = _finite(values[0])
                amount = _finite(values[1])
                if pct_chg is None or amount is None or amount < 0:
                    continue
                sector["returns"].append(pct_chg)
                sector["amount"] = float(sector["amount"]) + amount
            observed_total = sum(
                len(sector["returns"]) for sector in sectors.values()
            )
            if not assigned:
                raise ValueError(f"member_coverage_unavailable:{trade_date}")
            if observed_total / len(assigned) < MINIMUM_MEMBER_COVERAGE:
                raise ValueError(
                    f"member_coverage_below_{MINIMUM_MEMBER_COVERAGE:.2f}:"
                    f"{trade_date}:{observed_total}/{len(assigned)}"
                )
            unobserved_sectors = sorted(
                code for code, sector in sectors.items()
                if not sector["returns"]
            )
            if unobserved_sectors:
                raise ValueError(
                    f"sector_observation_missing:{trade_date}:"
                    f"{','.join(unobserved_sectors)}"
                )
            observed_amount = sum(
                float(sector["amount"]) for sector in sectors.values()
            )
            amount_coverages[trade_date] = observed_amount / market_amount
            for sector_code, sector in sorted(sectors.items()):
                returns = list(sector["returns"])
                member_count = int(sector["members"])
                observed_count = len(returns)
                result.append((
                    sector_code, str(sector["name"]), sector_level, trade_date,
                    member_count, observed_count, sum(returns) / observed_count,
                    float(sector["amount"]), "CNY", observed_count / member_count,
                    SECTOR_MARKET_SOURCE, source_version,
                    membership_source_version, published_at,
                ))
    return tuple(result), amount_coverages


def _stored_market_amount_coverage(
    *, market_database_path: Path, target_path: Path, trade_date: str,
    sector_level: str, source_version: str,
) -> float:
    with sqlite3.connect(
        f"file:{market_database_path.resolve()}?mode=ro", uri=True,
    ) as market:
        market_amount = float(market.execute(
            "SELECT COALESCE(SUM(amount),0) FROM stock_daily "
            "WHERE trade_date=? AND amount>=0",
            (trade_date,),
        ).fetchone()[0])
    with sqlite3.connect(
        f"file:{target_path.resolve()}?mode=ro", uri=True,
    ) as target:
        sector_amount = float(target.execute(
            """SELECT COALESCE(SUM(amount),0) FROM sector_market_daily
               WHERE trade_date=? AND sector_level=? AND source_version=?""",
            (trade_date, sector_level, source_version),
        ).fetchone()[0])
    if market_amount <= 0:
        raise ValueError(f"market_amount_unavailable:{trade_date}")
    return sector_amount / market_amount


def _require_market_amount_coverage(
    *, session: str, requested: str, amount_coverage: float,
    current_minimum: float | None, historical_minimum: float | None,
) -> None:
    required = current_minimum if session == requested else historical_minimum
    if required is not None and amount_coverage < required:
        raise ValueError(
            f"market amount coverage below {required:.2f} for {session}: "
            f"{amount_coverage:.6f}"
        )


def _contains(interval: str, trade_date: str) -> bool:
    valid_from, valid_to = interval.split("|", 1)
    return valid_from <= trade_date and (not valid_to or trade_date < valid_to)


def _finite(value: object) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None
