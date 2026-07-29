from __future__ import annotations

from datetime import date, datetime
import hashlib
import json
import math
import os
from pathlib import Path
import sqlite3
import tempfile
from typing import Protocol


BAOSTOCK_TURNOVER_SCHEMA_VERSION = "baostock-turnover-snapshot.v1"
BAOSTOCK_TURNOVER_SOURCE_VERSION = "baostock-daily-turnover.v1"
FLOAT_SHARE_REFERENCE_SCHEMA_VERSION = "baostock-float-share-reference.v1"
DERIVED_TURNOVER_SOURCE_VERSION = "baostock-float-share-derived-turnover.v1"
FLOAT_SHARE_REFERENCE_MAX_AGE_SESSIONS = 20
TURNOVER_COVERAGE_MINIMUM = 0.99
FLOAT_SHARE_REFERENCE_SOURCE_VERSIONS = frozenset({
    "sina-moneyflow-r0+baostock-daily.v2",
})


class BaoStockTurnoverClientV1(Protocol):
    def read(
        self, stock_code: str, start_date: str, end_date: str
    ) -> tuple[dict[str, object], ...]: ...


def build_baostock_turnover_snapshot_v1(
    *,
    market_database_path: Path,
    as_of: str,
    fetched_at: str,
    client: BaoStockTurnoverClientV1,
) -> dict[str, object]:
    requested = date.fromisoformat(as_of).isoformat()
    timestamp = datetime.fromisoformat(fetched_at.replace("Z", "+00:00"))
    if timestamp.utcoffset() is None:
        raise ValueError("fetched_at must include a timezone")
    source_rows = _source_rows(market_database_path, requested)
    eligible = tuple(
        row
        for row in source_rows
        if _baostock_supported(str(row["stock_code"]))
        and _positive(row["volume"])
        and _positive(row["amount"])
    )
    rows = []
    missing = []
    for source in eligible:
        stock_code = str(source["stock_code"])
        try:
            raw_rows = client.read(stock_code, requested, requested)
        except RuntimeError:
            missing.append(stock_code)
            if len(missing) / len(eligible) > 0.01:
                raise RuntimeError(
                    "BaoStock failures already exceed the frozen 99% "
                    "coverage gate"
                )
            continue
        matching = tuple(
            row for row in raw_rows
            if str(row.get("trade_date") or "") == requested
        )
        if not matching:
            missing.append(stock_code)
            continue
        if len(matching) != 1:
            raise ValueError(
                f"BaoStock returned duplicate exact-date rows for {stock_code}"
            )
        normalized = _normalize_row(
            stock_code=stock_code, row=matching[0]
        )
        for field in ("close", "volume", "amount"):
            if not math.isclose(
                float(normalized[field]),
                float(source[field]),
                rel_tol=1e-9,
                abs_tol=1e-8,
            ):
                raise ValueError(
                    f"BaoStock {field} does not match market database "
                    f"for {stock_code}"
                )
        rows.append(normalized)
    coverage = len(rows) / len(eligible) if eligible else 0.0
    _require_coverage(coverage)
    return {
        "schema_version": BAOSTOCK_TURNOVER_SCHEMA_VERSION,
        "source": "baostock.daily",
        "source_version": BAOSTOCK_TURNOVER_SOURCE_VERSION,
        "as_of": requested,
        "fetched_at": fetched_at,
        "units": {
            "volume": "SHARE",
            "amount": "CNY",
            "turnover": "PERCENT",
        },
        "summary": {
            "eligible_row_count": len(eligible),
            "covered_row_count": len(rows),
            "missing_row_count": len(missing),
            "coverage": coverage,
            "missing_stock_codes": missing,
        },
        "rows": sorted(rows, key=lambda item: str(item["stock_code"])),
    }


def validate_baostock_turnover_snapshot_market_v1(
    *,
    market_database_path: Path,
    payload: dict[str, object],
) -> None:
    requested = date.fromisoformat(str(payload.get("as_of"))).isoformat()
    if payload.get("units") != {
        "volume": "SHARE",
        "amount": "CNY",
        "turnover": "PERCENT",
    }:
        raise ValueError("existing turnover snapshot unit mismatch")
    source_rows = _source_rows(market_database_path, requested)
    eligible = {
        str(row["stock_code"]): row
        for row in source_rows
        if _baostock_supported(str(row["stock_code"]))
        and _positive(row["volume"])
        and _positive(row["amount"])
    }
    raw_rows = payload.get("rows")
    if not isinstance(raw_rows, list):
        raise ValueError("existing turnover snapshot rows are missing")
    covered: set[str] = set()
    for raw in raw_rows:
        if not isinstance(raw, dict):
            raise ValueError("existing turnover snapshot row must be an object")
        code = str(raw.get("stock_code") or "")
        if code in covered:
            raise ValueError("existing turnover snapshot has duplicate rows")
        source = eligible.get(code)
        if source is None or raw.get("trade_date") != requested:
            raise ValueError("existing turnover snapshot market-db mismatch")
        for field in ("close", "volume", "amount"):
            if not math.isclose(
                float(raw.get(field)),
                float(source[field]),
                rel_tol=1e-9,
                abs_tol=1e-8,
            ):
                raise ValueError(
                    "existing turnover snapshot market-db mismatch"
                )
        if not _positive_or_zero(raw.get("turnover_percent")):
            raise ValueError("existing turnover snapshot turnover is invalid")
        covered.add(code)
    missing = sorted(set(eligible) - covered)
    coverage = len(covered) / len(eligible) if eligible else 0.0
    summary = payload.get("summary")
    if not isinstance(summary, dict) or summary != {
        "eligible_row_count": len(eligible),
        "covered_row_count": len(covered),
        "missing_row_count": len(missing),
        "coverage": coverage,
        "missing_stock_codes": missing,
    }:
        raise ValueError("existing turnover snapshot summary mismatch")
    _require_coverage(coverage)


def build_float_share_reference_v1(
    *,
    market_database_path: Path,
    capital_database_path: Path,
    as_of: str,
    created_at: str,
) -> dict[str, object]:
    requested = date.fromisoformat(as_of).isoformat()
    timestamp = datetime.fromisoformat(created_at.replace("Z", "+00:00"))
    if timestamp.utcoffset() is None:
        raise ValueError("created_at must include a timezone")
    if requested > timestamp.date().isoformat():
        raise ValueError(
            "float-share reference as_of cannot be after created_at"
        )
    market_uri = f"{market_database_path.resolve(strict=True).as_uri()}?mode=ro"
    capital_uri = f"{capital_database_path.resolve(strict=True).as_uri()}?mode=ro"
    with sqlite3.connect(market_uri, uri=True) as market:
        market_rows = {
            str(code): float(close)
            for code, close in market.execute(
                """SELECT stock_code,close FROM stock_daily
                   WHERE trade_date=? AND close>0
                   ORDER BY stock_code""",
                (requested,),
            )
        }
    with sqlite3.connect(capital_uri, uri=True) as capital:
        metadata = dict(capital.execute(
            "SELECT key,value FROM supplemental_metadata ORDER BY key"
        ))
        rows = capital.execute(
            """SELECT stock_code,float_market_cap
               FROM stock_capital_daily
               WHERE trade_date=? AND float_market_cap>0
               ORDER BY stock_code""",
            (requested,),
        ).fetchall()
    capital_fetched_at = datetime.fromisoformat(
        str(metadata.get("capital_fetched_at") or "").replace("Z", "+00:00")
    )
    if capital_fetched_at.utcoffset() is None:
        raise ValueError(
            "capital reference fetched_at must include a timezone"
        )
    if capital_fetched_at > timestamp:
        raise ValueError(
            "capital reference fetched_at cannot be after created_at"
        )
    expected_metadata = {
        "capital_amount_unit": "CNY",
        "capital_turnover_raw_unit": "PERCENT",
        "capital_volume_raw_unit": "SHARE",
    }
    for key, value in expected_metadata.items():
        if metadata.get(key) != value:
            raise ValueError(f"capital reference {key} mismatch")
    if "baostock" not in str(metadata.get("capital_source", "")).lower():
        raise ValueError("capital reference is not BaoStock-derived")
    source_version = str(metadata.get("capital_source_version") or "")
    if source_version not in FLOAT_SHARE_REFERENCE_SOURCE_VERSIONS:
        raise ValueError("capital reference source version mismatch")
    reference_rows = []
    for stock_code, float_market_cap in rows:
        code = str(stock_code)
        close = market_rows.get(code)
        if close is None:
            continue
        float_shares = float(float_market_cap) / close
        if not 1_000_000 <= float_shares <= 1_000_000_000_000:
            raise ValueError(
                f"implausible float shares for {code}: {float_shares:.6g}"
            )
        reference_rows.append({
            "stock_code": code,
            "reference_date": requested,
            "float_shares": float_shares,
        })
    if not reference_rows:
        raise ValueError("float-share reference is empty")
    return {
        "schema_version": FLOAT_SHARE_REFERENCE_SCHEMA_VERSION,
        "source": "baostock.daily",
        "source_version": source_version,
        "as_of": requested,
        "created_at": created_at,
        "units": {"float_shares": "SHARE"},
        "row_count": len(reference_rows),
        "rows": reference_rows,
    }


def build_derived_turnover_snapshot_v1(
    *,
    market_database_path: Path,
    as_of: str,
    fetched_at: str,
    reference: dict[str, object],
) -> dict[str, object]:
    requested = date.fromisoformat(as_of).isoformat()
    timestamp = datetime.fromisoformat(fetched_at.replace("Z", "+00:00"))
    if timestamp.utcoffset() is None:
        raise ValueError("fetched_at must include a timezone")
    if reference.get("schema_version") != FLOAT_SHARE_REFERENCE_SCHEMA_VERSION:
        raise ValueError("float-share reference schema mismatch")
    if reference.get("source") != "baostock.daily":
        raise ValueError("float-share reference source mismatch")
    reference_source_version = str(reference.get("source_version") or "")
    if reference_source_version not in FLOAT_SHARE_REFERENCE_SOURCE_VERSIONS:
        raise ValueError("float-share reference source version mismatch")
    if reference.get("units") != {"float_shares": "SHARE"}:
        raise ValueError("float-share reference unit mismatch")
    created_at = datetime.fromisoformat(
        str(reference.get("created_at") or "").replace("Z", "+00:00")
    )
    if created_at.utcoffset() is None:
        raise ValueError(
            "float-share reference created_at must include a timezone"
        )
    if created_at > timestamp:
        raise ValueError(
            "float-share reference created_at cannot be after fetched_at"
        )
    reference_rows = reference.get("rows")
    if not isinstance(reference_rows, list):
        raise ValueError("float-share reference rows are missing")
    if int(reference.get("row_count", -1)) != len(reference_rows):
        raise ValueError("float-share reference row count mismatch")
    reference_date = date.fromisoformat(str(reference.get("as_of"))).isoformat()
    reference_content_sha256 = _reference_content_sha256(reference)
    age = _session_age(
        market_database_path, reference_date=reference_date, as_of=requested
    )
    if age > FLOAT_SHARE_REFERENCE_MAX_AGE_SESSIONS:
        raise ValueError(
            f"float-share reference age {age} exceeds "
            f"{FLOAT_SHARE_REFERENCE_MAX_AGE_SESSIONS} sessions"
        )
    shares = {}
    for row in reference_rows:
        if not isinstance(row, dict):
            raise ValueError("float-share reference row must be an object")
        code = str(row.get("stock_code") or "")
        if not _baostock_supported(code):
            raise ValueError(
                f"unsupported float-share reference stock code: {code}"
            )
        if code in shares:
            raise ValueError(
                f"duplicate float-share reference stock code: {code}"
            )
        row_date = date.fromisoformat(
            str(row.get("reference_date"))
        ).isoformat()
        if row_date != reference_date:
            raise ValueError(
                f"float-share reference date mismatch for {code}"
            )
        float_shares = _finite_non_negative(
            row.get("float_shares"), "float_shares", code
        )
        if not 1_000_000 <= float_shares <= 1_000_000_000_000:
            raise ValueError(
                f"implausible float shares for {code}: {float_shares:.6g}"
            )
        shares[code] = float_shares
    source_rows = _source_rows(market_database_path, requested)
    eligible = tuple(
        row
        for row in source_rows
        if _baostock_supported(str(row["stock_code"]))
        and _positive(row["volume"])
        and _positive(row["amount"])
    )
    rows = []
    missing = []
    for source in eligible:
        code = str(source["stock_code"])
        float_shares = shares.get(code)
        if not float_shares:
            missing.append(code)
            continue
        rows.append({
            "stock_code": code,
            "trade_date": requested,
            "close": float(source["close"]),
            "volume": float(source["volume"]),
            "amount": float(source["amount"]),
            "turnover_percent": float(source["volume"]) / float_shares * 100.0,
        })
    coverage = len(rows) / len(eligible) if eligible else 0.0
    _require_coverage(coverage)
    return {
        "schema_version": BAOSTOCK_TURNOVER_SCHEMA_VERSION,
        "source": "baostock.float-shares-derived",
        "source_version": DERIVED_TURNOVER_SOURCE_VERSION,
        "as_of": requested,
        "fetched_at": fetched_at,
        "reference_as_of": reference_date,
        "reference_source_version": reference_source_version,
        "reference_content_sha256": reference_content_sha256,
        "reference_age_sessions": age,
        "units": {
            "volume": "SHARE",
            "amount": "CNY",
            "turnover": "PERCENT",
        },
        "summary": {
            "eligible_row_count": len(eligible),
            "covered_row_count": len(rows),
            "missing_row_count": len(missing),
            "coverage": coverage,
            "missing_stock_codes": missing,
        },
        "rows": rows,
    }


def write_turnover_snapshot_v1(
    *, payload: dict[str, object], output: Path,
) -> None:
    encoded = (
        json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        + "\n"
    ).encode("utf-8")
    output.parent.mkdir(parents=True, exist_ok=True)
    if output.exists():
        if output.read_bytes() != encoded:
            raise FileExistsError(
                f"turnover snapshot already exists with different content: {output}"
            )
        return
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{output.name}.", suffix=".tmp", dir=output.parent
    )
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
        try:
            os.link(temporary_name, output)
            directory_fd = os.open(output.parent, os.O_RDONLY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        except FileExistsError:
            if output.read_bytes() != encoded:
                raise FileExistsError(
                    "turnover snapshot already exists with different content: "
                    f"{output}"
                )
    finally:
        Path(temporary_name).unlink(missing_ok=True)


def write_float_share_reference_v1(
    *, payload: dict[str, object], output: Path,
) -> None:
    write_turnover_snapshot_v1(payload=payload, output=output)


def float_share_reference_identity_v1(
    reference: dict[str, object],
) -> tuple[str, str, str]:
    return (
        date.fromisoformat(str(reference.get("as_of"))).isoformat(),
        str(reference.get("source_version") or ""),
        _reference_content_sha256(reference),
    )


def _reference_content_sha256(reference: dict[str, object]) -> str:
    encoded = json.dumps(
        reference,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _source_rows(database_path: Path, as_of: str) -> tuple[dict[str, object], ...]:
    uri = f"{database_path.resolve(strict=True).as_uri()}?mode=ro"
    with sqlite3.connect(uri, uri=True) as connection:
        connection.row_factory = sqlite3.Row
        rows = connection.execute(
            """SELECT stock_code,close,volume,amount
               FROM stock_daily
               WHERE trade_date=?
               ORDER BY stock_code""",
            (as_of,),
        ).fetchall()
    if not rows:
        raise ValueError(f"source stock_daily has no rows for {as_of}")
    return tuple(dict(row) for row in rows)


def _normalize_row(
    *, stock_code: str, row: dict[str, object],
) -> dict[str, object]:
    expected_units = {
        "volume_unit": "SHARE",
        "amount_unit": "CNY",
        "turnover_unit": "PERCENT",
    }
    for field, expected in expected_units.items():
        if str(row.get(field) or "") != expected:
            raise ValueError(
                f"BaoStock {field} must be {expected} for {stock_code}"
            )
    close = _finite_non_negative(row.get("close"), "close", stock_code)
    volume = _finite_non_negative(row.get("volume"), "volume", stock_code)
    amount = _finite_non_negative(row.get("amount"), "amount", stock_code)
    turnover = _finite_non_negative(
        row.get("turnover_percent"), "turnover_percent", stock_code
    )
    if close <= 0 or volume <= 0 or amount <= 0:
        raise ValueError(f"BaoStock trading row is not positive for {stock_code}")
    amount_consistency = amount / (volume * close)
    if not 0.5 <= amount_consistency <= 1.5:
        raise ValueError(
            f"BaoStock volume/amount unit mismatch for {stock_code}: "
            f"amount/(volume*close)={amount_consistency:.6g}"
        )
    return {
        "stock_code": stock_code,
        "trade_date": str(row["trade_date"]),
        "close": close,
        "volume": volume,
        "amount": amount,
        "turnover_percent": turnover,
    }


def _finite_non_negative(
    value: object, field: str, stock_code: str,
) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(
            f"invalid BaoStock {field} for {stock_code}: {value!r}"
        ) from exc
    if not math.isfinite(number) or number < 0:
        raise ValueError(
            f"invalid BaoStock {field} for {stock_code}: {value!r}"
        )
    return number


def _positive(value: object) -> bool:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return False
    return math.isfinite(number) and number > 0


def _positive_or_zero(value: object) -> bool:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return False
    return math.isfinite(number) and number >= 0


def _require_coverage(coverage: float) -> None:
    if coverage < TURNOVER_COVERAGE_MINIMUM:
        raise ValueError(
            f"turnover coverage {coverage:.6%} is below the frozen "
            f"{TURNOVER_COVERAGE_MINIMUM:.2%} gate"
        )


def _baostock_supported(stock_code: str) -> bool:
    return stock_code.startswith(("00", "30", "60", "68"))


def _session_age(
    database_path: Path, *, reference_date: str, as_of: str,
) -> int:
    if reference_date > as_of:
        raise ValueError("float-share reference cannot be in the future")
    uri = f"{database_path.resolve(strict=True).as_uri()}?mode=ro"
    with sqlite3.connect(uri, uri=True) as connection:
        sessions = int(connection.execute(
            """SELECT COUNT(DISTINCT trade_date)
               FROM stock_daily
               WHERE trade_date>? AND trade_date<=?""",
            (reference_date, as_of),
        ).fetchone()[0])
    return sessions
