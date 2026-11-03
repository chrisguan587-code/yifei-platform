"""Source-specific attention observations; no pool membership or strategy decisions."""
from __future__ import annotations

import argparse
from datetime import datetime
import json
from pathlib import Path
import re
from urllib.parse import urlencode
from urllib.request import ProxyHandler, Request, build_opener
from zoneinfo import ZoneInfo

from .artifacts import ArtifactEnvelopeV1, ArtifactStoreV1

VERSION = "source-attention.v1"
EM_BASE = "https://emappdata.eastmoney.com/stockrank/"
THS_URL = "https://api.zizizaizai.com/v3/open/sentiment/media/ths2/top"
EM_BODY = {"appId": "appId01", "globalId": "786e4c21-70dc-435a-93bb-38", "marketType": ""}
TZ = ZoneInfo("Asia/Shanghai")


def request_json(url, body=None, headers=None):
    request = Request(url, data=None if body is None else json.dumps(body).encode(),
                      headers={"Content-Type": "application/json", **(headers or {})})
    with build_opener(ProxyHandler({})).open(request, timeout=15) as response:
        return json.load(response)


def _stock_code(symbol):
    code = str(symbol).removeprefix("SH").removeprefix("SZ").removeprefix("BJ")
    if not re.fullmatch(r"\d{6}", code):
        raise ValueError("invalid_stock_code")
    return code


def normalize(source, raw, captured_at, *, single=False):
    """Keep cross-platform ranks separate and never invent a missing source timestamp."""
    expected = 20000 if source == "ths_via_zzshare" else 0
    if raw.get("code") != expected:
        raise ValueError("upstream_business_error")
    data = raw.get("data")
    if single:
        data = [data] if isinstance(data, dict) else None
    if not isinstance(data, list) or not data:
        raise ValueError("upstream_empty_or_invalid")
    capture = datetime.fromisoformat(captured_at)
    if capture.tzinfo is None:
        raise ValueError("capture_timezone_required")
    rows = []
    seen = set()
    for row in data:
        ths = source == "ths_via_zzshare"
        code = _stock_code(row["symbol_code"] if ths else row["srcSecurityCode"] if single else row["sc"])
        rank = row["rank"] if ths or single else row["rk"]
        if isinstance(rank, bool) or not isinstance(rank, int) or rank < 1 or code in seen:
            raise ValueError("invalid_rank_or_duplicate_stock")
        seen.add(code)
        source_time = row.get("update_time") if ths else row.get("calcTime")
        time_status = "unknown"
        if source_time:
            stamp = datetime.fromisoformat(source_time)
            stamp = stamp.replace(tzinfo=TZ) if stamp.tzinfo is None else stamp
            if stamp > capture:
                time_status = "future_timestamp"
            elif stamp.astimezone(TZ).date() != capture.astimezone(TZ).date():
                time_status = "different_date"
            else:
                time_status = "same_date"
        rows.append(dict(stock_code=code, rank=rank, source=source, source_time=source_time,
                         time_status=time_status, captured_at=captured_at))
    return rows


def validate_representatives(manifest, captured_at):
    if (manifest.get('schema_version') != 'human-stock-identities.v1'
            or manifest.get('source') != 'user_confirmation' or not manifest.get('source_version')):
        raise ValueError('invalid_representative_manifest')
    confirmed = datetime.fromisoformat(manifest['confirmed_at'])
    if confirmed.tzinfo is None or confirmed > datetime.fromisoformat(captured_at):
        raise ValueError('representatives_not_yet_confirmed')
    seen = set()
    for row in manifest['stocks']:
        code = _stock_code(row['stock_code'])
        if code in seen or any(not str(row.get(k) or '').strip() for k in ('stock_name','direction','sub_direction','role')):
            raise ValueError('duplicate_or_incomplete_representative')
        seen.add(code)
    if not seen or len(seen) > 200:
        raise ValueError('invalid_representative_count')


def capture_attention(*, snapshot_root, codes=(), representatives=None, fetch=request_json):
    started = datetime.now(TZ).isoformat()
    if representatives is not None:
        validate_representatives(representatives, started)
        codes = (*codes, *(r['stock_code'] for r in representatives['stocks']))
    symbols = []
    for code in dict.fromkeys(codes):
        code = _stock_code(code)
        if not code.startswith(("0", "3", "6", "4", "8", "9")):
            raise ValueError("unsupported_stock_code")
        symbols.append(("SH" if code.startswith("6") else "SZ" if code.startswith(("0", "3")) else "BJ") + code)
    day = started[:10]
    jobs = [("eastmoney", "top100", EM_BASE + "getAllCurrentList",
             EM_BODY | {"pageNo": 1, "pageSize": 100}, {}, False),
            ("ths_via_zzshare", "top100", THS_URL + "?" + urlencode({"date1": day.replace("-", ""), "top_n": 100}),
             None, {"sdk-key": "anonymous"}, False)]
    jobs.extend(("eastmoney", symbol, EM_BASE + "getCurrentLatest",
                 EM_BODY | {"srcSecurityCode": symbol}, {}, True) for symbol in symbols)
    observations = []
    for source, scope, url, body, headers, single in jobs:
        result = dict(source=source, scope=scope, request_url=url, request_body=body)
        try:
            raw = fetch(url, body, headers)
            at = datetime.now(TZ).isoformat()
            result.update(raw_response=raw, captured_at=at)
            rows = normalize(source, raw, at, single=single)
            if single and rows[0]["stock_code"] != scope[2:]:
                raise ValueError("requested_stock_mismatch")
            result.update(status="ok", rows=rows)
            if any(r["time_status"] in ("different_date", "future_timestamp") for r in rows):
                result["status"] = "degraded"
        except Exception as exc:
            result.update(status="unavailable", error=f"{type(exc).__name__}: {exc}", rows=[])
        observations.append(result)
    at = datetime.now(TZ).isoformat()
    payload = dict(schema_version=VERSION, as_of=day, started_at=started, captured_at=at,
                   status="ok" if all(o["status"] == "ok" for o in observations) else "degraded",
                   observations=observations, semantics="observed_at_capture_not_historical_intraday",
                   coverage="source_limited_top100_plus_explicit_eastmoney_codes_not_full_market",
                   missing_semantics="missing_rank_is_unknown_not_unpopular")
    if representatives is not None:
        # This is a verbatim human identity assertion, not a Platform stock pool,
        # score or inferred market role. Consumers own their display semantics.
        payload['representative_assertions'] = representatives
    envelope = ArtifactEnvelopeV1.create(artifact_type="stock-attention", producer="yifei-platform",
        producer_version=VERSION, payload_schema="stock-attention", payload_schema_version="v1",
        as_of=day, created_at=at, source_refs=(EM_BASE, THS_URL), payload=payload)
    receipt = ArtifactStoreV1(Path(snapshot_root)).write(envelope)
    return receipt, payload


def is_post_close_session(calendar_path, now):
    """Scheduled captures use the published exchange calendar, never weekdays alone."""
    if now.tzinfo is None:
        raise ValueError("capture_timezone_required")
    local = now.astimezone(TZ)
    day = local.date().isoformat()
    calendar = json.loads(Path(calendar_path).read_text(encoding="utf-8"))
    if calendar.get("schema_version") != "exchange-trading-calendar.v1":
        raise ValueError("unsupported_exchange_calendar")
    if not calendar["coverage_start"] <= day <= calendar["coverage_end"]:
        raise ValueError("exchange_calendar_out_of_coverage")
    if day not in calendar["sessions"]:
        return False
    if local.hour < 15:
        raise ValueError("scheduled_attention_capture_before_close")
    return True


def main():
    parser = argparse.ArgumentParser(description="Capture source-specific stock attention without strategy changes")
    parser.add_argument("--snapshot-root", type=Path, required=True)
    parser.add_argument("--codes", nargs="*", default=[])
    parser.add_argument("--representatives", type=Path, help="Explicit human-confirmed identity manifest")
    parser.add_argument("--exchange-calendar", type=Path,
                        help="Require a current exchange session after close (scheduled runs).")
    args = parser.parse_args()
    if args.exchange_calendar and not is_post_close_session(args.exchange_calendar, datetime.now(TZ)):
        print(json.dumps(dict(status="skipped", reason="non_trading_day",
                              checked_at=datetime.now(TZ).isoformat())))
        return 0
    representatives = json.loads(args.representatives.read_text(encoding='utf-8')) if args.representatives else None
    receipt, payload = capture_attention(snapshot_root=args.snapshot_root, codes=args.codes,
                                         representatives=representatives)
    print(json.dumps(dict(artifact_ref=receipt.artifact_ref, status=payload["status"],
        sources=[dict(source=o["source"], scope=o["scope"], status=o["status"],
                      count=len(o["rows"]), error=o.get("error")) for o in payload["observations"]]), ensure_ascii=False))
    return 0 if payload["status"] == "ok" else 1


if __name__ == "__main__":
    raise SystemExit(main())
