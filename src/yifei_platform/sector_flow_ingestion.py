from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time
import json
import math
import os
from pathlib import Path
import shutil
import sqlite3
import tempfile
from typing import Protocol, Sequence
from zoneinfo import ZoneInfo

from .supplemental_facts import (
    initialize_supplemental_database_v1,
    serialized_supplemental_publication_v1,
)


SECTOR_FLOW_SOURCE_VERSION = "levistock.eastmoney-sector-em.industry.v1"
SECTOR_FLOW_SOURCE = "levistock.sector_em"
SECTOR_FLOW_SCHEMA_VERSION = "sector-capital-daily-facts.v1"


class SectorFlowClientV1(Protocol):
    def read_industry(self) -> Sequence[dict[str, object]]: ...


@dataclass(frozen=True)
class SectorFlowPublishResultV1:
    target_path: Path
    raw_snapshot_path: Path
    as_of: str
    sector_count: int
    source_version: str


class LevistockSectorFlowClientV1:
    def __init__(self) -> None:
        try:
            import levistock
        except ImportError as exc:
            raise RuntimeError(
                "Levistock is required; install yifei-platform[public-data]"
            ) from exc
        self._levistock = levistock

    def read_industry(self) -> Sequence[dict[str, object]]:
        return self._levistock.sector_em(sector_type="industry")


@serialized_supplemental_publication_v1
def publish_sector_flow_daily_v1(
    *,
    client: SectorFlowClientV1,
    market_database_path: Path,
    target_path: Path,
    raw_snapshot_root: Path,
    as_of: str,
    fetched_at: str,
    source_version: str = SECTOR_FLOW_SOURCE_VERSION,
    minimum_sector_count: int = 400,
    observed_now: datetime | None = None,
) -> SectorFlowPublishResultV1:
    requested = date.fromisoformat(as_of).isoformat()
    observed = datetime.fromisoformat(fetched_at.replace("Z", "+00:00"))
    if observed.utcoffset() is None:
        raise ValueError("fetched_at must include a timezone")
    local_observed = observed.astimezone(ZoneInfo("Asia/Shanghai"))
    actual_observed = observed_now or datetime.now(
        ZoneInfo("Asia/Shanghai")
    )
    if actual_observed.utcoffset() is None:
        raise ValueError("observed_now must include a timezone")
    actual_local = actual_observed.astimezone(ZoneInfo("Asia/Shanghai"))
    if local_observed.date().isoformat() != requested:
        raise ValueError(
            "sector_em is current-day only; fetched_at date must equal as_of"
        )
    if actual_local.date().isoformat() != requested:
        raise ValueError(
            "sector_em is current-day only; as_of must equal Shanghai fetch date"
        )
    if local_observed > actual_local:
        raise ValueError("fetched_at cannot be in the future")
    if actual_local.time() < time(15, 10):
        raise ValueError("sector flow may publish only after 15:10 Asia/Shanghai")
    if not source_version.strip():
        raise ValueError("source_version is required")
    if minimum_sector_count < 1:
        raise ValueError("minimum_sector_count must be positive")
    _require_market_session(
        market_database_path.resolve(strict=True), requested
    )

    raw_rows = tuple(dict(row) for row in client.read_industry())
    rows = _normalize_rows(
        raw_rows,
        as_of=requested,
        fetched_at=fetched_at,
        source_version=source_version,
    )
    if len(rows) < minimum_sector_count:
        raise ValueError("sector flow row count is below the frozen threshold")

    snapshot_path = (
        raw_snapshot_root.resolve()
        / "sector_em"
        / f"{requested}.json"
    )
    snapshot = {
        "schema_version": "sector-flow-raw-snapshot.v1",
        "as_of": requested,
        "fetched_at": fetched_at,
        "source": SECTOR_FLOW_SOURCE,
        "source_version": source_version,
        "consumed_units": {
            "amount": "CNY",
            "main_inflow": "CNY",
        },
        "unconsumed_fields": {
            "volume": "unit_not_audited",
            "turnover_rate": "not_used",
            "total_market": "not_used",
        },
        "rows": list(raw_rows),
    }
    _preflight_existing_rows(
        target_path=target_path,
        rows=rows,
        as_of=requested,
    )
    _preflight_immutable_json(snapshot_path, snapshot)
    snapshot_created = _publish_immutable_json(snapshot_path, snapshot)
    try:
        _publish_rows(
            target_path=target_path,
            rows=rows,
            as_of=requested,
            fetched_at=fetched_at,
            source_version=source_version,
        )
    except BaseException:
        if snapshot_created:
            snapshot_path.unlink(missing_ok=True)
        raise
    return SectorFlowPublishResultV1(
        target_path=target_path.resolve(),
        raw_snapshot_path=snapshot_path,
        as_of=requested,
        sector_count=len(rows),
        source_version=source_version,
    )


def _normalize_rows(
    raw_rows: Sequence[dict[str, object]],
    *,
    as_of: str,
    fetched_at: str,
    source_version: str,
) -> tuple[tuple[object, ...], ...]:
    normalized: list[tuple[object, ...]] = []
    seen: set[str] = set()
    for raw in raw_rows:
        sector_code = str(raw.get("sector_code") or "").strip()
        sector_name = str(raw.get("sector_name") or "").strip()
        if not sector_code or not sector_name:
            raise ValueError("sector code and name are required")
        if sector_code in seen:
            raise ValueError(f"duplicate sector code: {sector_code}")
        seen.add(sector_code)
        amount = _positive_finite(raw.get("amount"), "amount", sector_code)
        main_inflow = _finite(
            raw.get("main_inflow"), "main_inflow", sector_code
        )
        if abs(main_inflow) > amount:
            raise ValueError(
                f"sector amount/main_inflow unit mismatch: {sector_code}"
            )
        normalized.append((
            as_of,
            sector_code,
            sector_name,
            amount,
            _finite(raw.get("change_pct"), "change_pct", sector_code),
            main_inflow,
            _nonnegative_int(raw.get("up_count"), "up_count", sector_code),
            _nonnegative_int(
                raw.get("down_count"), "down_count", sector_code
            ),
            _optional_string(raw.get("lead_stock_name")),
            _optional_finite(raw.get("lead_stock_chg")),
            "CNY",
            "CNY",
            SECTOR_FLOW_SOURCE,
            source_version,
            fetched_at,
        ))
    return tuple(sorted(normalized, key=lambda row: str(row[1])))


def _publish_rows(
    *,
    target_path: Path,
    rows: Sequence[tuple[object, ...]],
    as_of: str,
    fetched_at: str,
    source_version: str,
) -> None:
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
            existing = connection.execute(
                """SELECT trade_date, sector_code, sector_name, amount,
                          change_pct, main_inflow, up_count, down_count,
                          lead_stock_name, lead_stock_chg, amount_unit,
                          main_inflow_unit, source, source_version, fetched_at
                   FROM sector_fund_flow_daily
                   WHERE trade_date=?
                   ORDER BY sector_code""",
                (as_of,),
            ).fetchall()
            if existing:
                if tuple(existing) != tuple(rows):
                    raise ValueError(
                        "sector flow date already published with different content"
                    )
                return
            connection.executemany(
                """INSERT INTO sector_fund_flow_daily (
                       trade_date, sector_code, sector_name, amount,
                       change_pct, main_inflow, up_count, down_count,
                       lead_stock_name, lead_stock_chg, amount_unit,
                       main_inflow_unit, source, source_version, fetched_at
                   ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                rows,
            )
            connection.executemany(
                """INSERT INTO supplemental_metadata(key, value)
                   VALUES (?, ?)
                   ON CONFLICT(key) DO UPDATE SET value=excluded.value""",
                (
                    ("sector_flow_source_version", source_version),
                    ("sector_flow_latest_as_of", as_of),
                    ("sector_flow_fetched_at", fetched_at),
                    ("sector_flow_amount_unit", "CNY"),
                    ("sector_flow_main_inflow_unit", "CNY"),
                    ("sector_flow_volume_status", "not_consumed_unit_unaudited"),
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


def _preflight_existing_rows(
    *,
    target_path: Path,
    rows: Sequence[tuple[object, ...]],
    as_of: str,
) -> None:
    target = target_path.resolve()
    if not target.exists():
        return
    with sqlite3.connect(
        f"{target.as_uri()}?mode=ro", uri=True
    ) as connection:
        table = connection.execute(
            """SELECT 1 FROM sqlite_master
               WHERE type='table' AND name='sector_fund_flow_daily'"""
        ).fetchone()
        if table is None:
            return
        existing = connection.execute(
            """SELECT trade_date, sector_code, sector_name, amount,
                      change_pct, main_inflow, up_count, down_count,
                      lead_stock_name, lead_stock_chg, amount_unit,
                      main_inflow_unit, source, source_version, fetched_at
               FROM sector_fund_flow_daily
               WHERE trade_date=?
               ORDER BY sector_code""",
            (as_of,),
        ).fetchall()
    if existing and tuple(existing) != tuple(rows):
        raise ValueError(
            "sector flow date already published with different content"
        )


def _require_market_session(database_path: Path, as_of: str) -> None:
    with sqlite3.connect(
        f"{database_path.as_uri()}?mode=ro", uri=True
    ) as connection:
        connection.execute("PRAGMA query_only = ON")
        row = connection.execute(
            "SELECT 1 FROM stock_daily WHERE trade_date=? LIMIT 1",
            (as_of,),
        ).fetchone()
    if row is None:
        raise ValueError("as_of is not a published market session")


def _encoded_snapshot(payload: dict[str, object]) -> str:
    return json.dumps(
        payload,
        allow_nan=False,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _preflight_immutable_json(
    path: Path, payload: dict[str, object],
) -> None:
    encoded = _encoded_snapshot(payload)
    if path.exists() and path.read_text(encoding="utf-8") != encoded:
        raise ValueError(
            "raw sector-flow snapshot already exists with different content"
        )


def _publish_immutable_json(
    path: Path, payload: dict[str, object],
) -> bool:
    encoded = _encoded_snapshot(payload)
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        if path.read_text(encoding="utf-8") != encoded:
            raise ValueError(
                "raw sector-flow snapshot already exists with different content"
            )
        return False
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    os.close(descriptor)
    temporary = Path(temporary_name)
    try:
        temporary.write_text(encoded, encoding="utf-8")
        try:
            os.link(temporary, path)
        except FileExistsError:
            if path.read_text(encoding="utf-8") != encoded:
                raise ValueError(
                    "raw sector-flow snapshot publish conflict"
                )
            return False
        return True
    finally:
        temporary.unlink(missing_ok=True)


def _finite(value: object, field: str, sector_code: str) -> float:
    result = float(value)
    if not math.isfinite(result):
        raise ValueError(f"{field} must be finite: {sector_code}")
    return result


def _positive_finite(value: object, field: str, sector_code: str) -> float:
    result = _finite(value, field, sector_code)
    if result <= 0:
        raise ValueError(f"{field} must be positive: {sector_code}")
    return result


def _nonnegative_int(value: object, field: str, sector_code: str) -> int:
    result = int(value)
    if result < 0 or float(value) != result:
        raise ValueError(f"{field} must be a nonnegative integer: {sector_code}")
    return result


def _optional_finite(value: object) -> float | None:
    if value is None or str(value).strip() == "":
        return None
    result = float(value)
    if not math.isfinite(result):
        raise ValueError("optional numeric value must be finite")
    return result


def _optional_string(value: object) -> str | None:
    if value is None or str(value).strip() == "":
        return None
    return str(value)
