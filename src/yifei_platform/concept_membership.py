from __future__ import annotations

import argparse
from datetime import date, datetime
import json
import os
from pathlib import Path
import subprocess
import tempfile
import time
from typing import Mapping, Sequence
import urllib.request

import websocket

CONCEPT_SCHEMA_VERSION = "platform-concept-membership.v1"
CONCEPT_UPDATE_STATUS_VERSION = "platform-concept-update-status.v1"
NORMAL_REUSE_TRADING_DAYS = 5
MAX_REUSE_TRADING_DAYS = 15
THS_BASE_URL = "https://q.10jqka.com.cn"
THS_ENTRY_URL = f"{THS_BASE_URL}/gn/detail/code/300816/"
CHROME_PATH = Path(
    os.environ.get(
        "YIFEI_PLATFORM_CHROME_PATH",
        "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    )
)
_THS_CRAWL_EXPRESSION = r"""(async () => {
  const sleep = milliseconds => new Promise(resolve => setTimeout(resolve, milliseconds));
  const decode = async response => {
    if (!response.ok) throw new Error(`HTTP ${response.status}: ${response.url}`);
    const text = new TextDecoder("gbk").decode(await response.arrayBuffer());
    if (!text.includes("m-pager-table")) throw new Error(`Malformed table: ${response.url}`);
    return text;
  };
  const fetchText = async url => {
    let failure;
    for (let attempt = 0; attempt < 2; attempt += 1) {
      try { return await decode(await fetch(url)); }
      catch (error) { failure = error; await sleep(250); }
    }
    throw failure;
  };
  const indexText = await fetchText(
    "/gn/index/field/addtime/order/desc/page/1/ajax/1/size/1000/"
  );
  const indexDocument = new DOMParser().parseFromString(indexText, "text/html");
  const pending = [];
  const seen = new Set();
  for (const row of indexDocument.querySelectorAll("tbody tr")) {
    const link = row.querySelector('a[href*="/gn/detail/code/"]');
    const cells = row.querySelectorAll("td");
    const match = link && link.href.match(/\/gn\/detail\/code\/(\d+)\//);
    const reportedCount = cells.length && Number(cells[cells.length - 1].textContent.trim());
    if (!match || !Number.isFinite(reportedCount) || seen.has(match[1])) continue;
    seen.add(match[1]);
    pending.push([match[1], link.textContent.trim(), reportedCount]);
  }
  const results = [];
  const crawl = async ([conceptCode, conceptName, reportedCount]) => {
    const pageCount = Math.max(1, Math.ceil(reportedCount / 1000));
    const texts = [];
    for (let page = 1; page <= pageCount; page += 1) {
      texts.push(await fetchText(
        `/gn/detail/field/199112/order/desc/page/${page}/ajax/1/size/1000/code/${conceptCode}`
      ));
      await sleep(40);
    }
    const rawCodes = [];
    for (const text of texts) {
      for (const match of text.matchAll(/stockpage\.10jqka\.com\.cn\/([^/\"<]+)\//g)) {
        rawCodes.push(match[1]);
      }
    }
    const memberCodes = [...new Set(rawCodes.filter(code => /^\d{6}$/.test(code)))].sort();
    return {
      concept_code: conceptCode,
      concept_name: conceptName,
      reported_member_count: reportedCount,
      parsed_member_count: memberCodes.length,
      member_codes: memberCodes,
      complete: true,
      returned_member_rows: rawCodes.length,
      valid_member_rows: rawCodes.filter(code => /^\d{6}$/.test(code)).length
    };
  };
  const worker = async () => {
    while (pending.length) {
      const item = pending.shift();
      try { results.push(await crawl(item)); }
      catch (error) {
        results.push({
          concept_code: item[0], concept_name: item[1],
          reported_member_count: item[2], parsed_member_count: 0,
          member_codes: [], complete: false,
          returned_member_rows: 0, valid_member_rows: 0, error: String(error)
        });
      }
      await sleep(80);
    }
  };
  await Promise.all([worker(), worker(), worker(), worker()]);
  results.sort((left, right) => left.concept_code.localeCompare(right.concept_code));
  return results;
})()"""


def fetch_ths_web_concepts() -> dict[str, object]:
    started = time.perf_counter()
    try:
        concepts = _run_ths_browser_crawl()
        returned_member_rows = sum(
            int(item.get("returned_member_rows") or 0) for item in concepts
        )
        valid_member_codes = sum(
            int(item.get("valid_member_rows") or 0) for item in concepts
        )
        for item in concepts:
            item.pop("returned_member_rows", None)
            item.pop("valid_member_rows", None)
        return _complete_report(
            source="ths_web",
            taxonomy="ths_concept",
            started=started,
            concepts=concepts,
            reported_concept_count=len(concepts),
            returned_member_rows=returned_member_rows,
            valid_member_codes=valid_member_codes,
        )
    except Exception as exc:
        return _failure("ths_web", "ths_concept", started, exc)


def _run_ths_browser_crawl() -> list[dict[str, object]]:
    if not CHROME_PATH.exists():
        raise FileNotFoundError(f"Chrome not found: {CHROME_PATH}")
    with tempfile.TemporaryDirectory(prefix="yifei-platform-ths-") as profile:
        profile_path = Path(profile)
        process = subprocess.Popen(
            [
                str(CHROME_PATH),
                "--remote-debugging-port=0",
                "--remote-allow-origins=*",
                f"--user-data-dir={profile}",
                "--no-first-run",
                "--no-default-browser-check",
                THS_ENTRY_URL,
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        try:
            port = _wait_for_devtools_port(profile_path)
            target = _wait_for_cdp_target(port)
            websocket_url = str(target["webSocketDebuggerUrl"])
            _wait_for_ths_browser(websocket_url)
            return _evaluate_ths_crawl(websocket_url)
        finally:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    pass


def _wait_for_devtools_port(profile: Path) -> int:
    port_file = profile / "DevToolsActivePort"
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        try:
            port = int(port_file.read_text(encoding="utf-8").splitlines()[0])
            if 0 < port < 65536:
                return port
        except (FileNotFoundError, IndexError, ValueError):
            pass
        time.sleep(0.2)
    raise RuntimeError("Chrome did not publish a DevTools port")


def _wait_for_cdp_target(port: int) -> Mapping[str, object]:
    deadline = time.monotonic() + 20
    last_error: Exception | None = None
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(
                f"http://127.0.0.1:{port}/json/list", timeout=1
            ) as response:
                targets = json.loads(response.read().decode("utf-8"))
            for target in targets:
                if target.get("type") == "page" and "10jqka.com.cn" in str(
                    target.get("url")
                ):
                    return target
        except Exception as exc:
            last_error = exc
        time.sleep(0.2)
    raise RuntimeError(f"Chrome CDP target unavailable: {last_error}")


def _wait_for_ths_browser(websocket_url: str) -> None:
    connection = websocket.create_connection(websocket_url, timeout=5)
    expression = """JSON.stringify({
      cookie: document.cookie.split('; ').find(item => item.startsWith('v=')) || '',
      user_agent: navigator.userAgent,
      title: document.title
    })"""
    try:
        deadline = time.monotonic() + 25
        command_id = 0
        while time.monotonic() < deadline:
            command_id += 1
            connection.send(json.dumps({
                "id": command_id,
                "method": "Runtime.evaluate",
                "params": {"expression": expression, "returnByValue": True},
            }))
            message = _receive_cdp_response(connection, command_id, deadline)
            result = message.get("result") or {}
            raw_value = (result.get("result") or {}).get("value")
            if raw_value and not result.get("exceptionDetails"):
                value = json.loads(raw_value)
                if value.get("cookie") and value.get("user_agent"):
                    return
            time.sleep(0.25)
        raise TimeoutError("THS browser challenge timed out")
    finally:
        connection.close()


def _evaluate_ths_crawl(websocket_url: str) -> list[dict[str, object]]:
    connection = websocket.create_connection(websocket_url, timeout=10)
    connection.settimeout(300)
    try:
        connection.send(json.dumps({
            "id": 1,
            "method": "Runtime.evaluate",
            "params": {
                "expression": _THS_CRAWL_EXPRESSION,
                "awaitPromise": True,
                "returnByValue": True,
                "timeout": 270000,
            },
        }))
        message = _receive_cdp_response(
            connection, 1, time.monotonic() + 280
        )
        result = message.get("result") or {}
        if result.get("exceptionDetails"):
            description = (
                (result.get("exceptionDetails") or {}).get("exception") or {}
            ).get("description", "unknown browser error")
            raise RuntimeError(f"THS browser crawl failed: {description}")
        value = (result.get("result") or {}).get("value")
        if not isinstance(value, list) or not value:
            raise RuntimeError("THS browser crawl returned no concepts")
        if not all(isinstance(item, dict) for item in value):
            raise RuntimeError("THS browser crawl returned malformed concepts")
        return [dict(item) for item in value]
    finally:
        connection.close()


def _receive_cdp_response(
    connection: websocket.WebSocket, command_id: int, deadline: float
) -> Mapping[str, object]:
    while time.monotonic() < deadline:
        remaining = deadline - time.monotonic()
        connection.settimeout(max(0.1, remaining))
        try:
            message = json.loads(connection.recv())
        except websocket.WebSocketTimeoutException as exc:
            raise TimeoutError(
                f"Chrome CDP command {command_id} timed out"
            ) from exc
        if message.get("id") == command_id:
            return message
    raise TimeoutError(f"Chrome CDP command {command_id} timed out")


def run_concept_update(
    *, trade_date: str, exchange_calendar: Path, output_root: Path,
) -> dict[str, object]:
    sessions = _calendar_sessions(exchange_calendar)
    if trade_date not in sessions:
        return {"status": "skipped", "reason": "not_trading_day"}

    date_root = output_root.resolve() / trade_date
    snapshot_path = date_root / f"concept_membership_{trade_date}.json"
    if snapshot_path.exists():
        return {
            "status": "already_current",
            "snapshot": str(snapshot_path),
            "trade_date": trade_date,
        }

    attempts: list[dict[str, object]] = []
    report = fetch_ths_web_concepts()
    attempts.append(_attempt_summary(report))
    selected = report if report.get("ok") else None

    captured_at = datetime.now().isoformat(timespec="seconds")
    if selected is not None:
        snapshot = {
            "schema_version": CONCEPT_SCHEMA_VERSION,
            "trade_date": trade_date,
            "captured_at": captured_at,
            "selected_source": selected["source"],
            "taxonomy": selected["taxonomy"],
            "concept_count": selected["concept_count"],
            "complete_concept_ratio": selected["complete_concept_ratio"],
            "member_code_parse_ratio": selected["member_code_parse_ratio"],
            "concepts": selected["concepts"],
            "source_attempts": attempts,
            "source_lineage": {
                "capture_method": "scheduled_chrome_cdp_public_pages",
                "endpoint": "q.10jqka.com.cn/gn",
                "page_size": 1000,
            },
            "mixed_sources": False,
        }
        _write_immutable_json(snapshot_path, snapshot)
        status = {
            "schema_version": CONCEPT_UPDATE_STATUS_VERSION,
            "trade_date": trade_date,
            "captured_at": captured_at,
            "status": "updated",
            "selected_snapshot": str(snapshot_path),
            "selected_source": selected["source"],
            "source_attempts": attempts,
        }
    else:
        try:
            _, freshness, reused_path = resolve_concept_snapshot(
                concept_root=output_root,
                exchange_calendar=exchange_calendar,
                as_of_trade_date=trade_date,
            )
            status = {
                "schema_version": CONCEPT_UPDATE_STATUS_VERSION,
                "trade_date": trade_date,
                "captured_at": captured_at,
                "status": "reused",
                "selected_snapshot": str(reused_path),
                "freshness": freshness,
                "source_attempts": attempts,
            }
        except FileNotFoundError:
            status = {
                "schema_version": CONCEPT_UPDATE_STATUS_VERSION,
                "trade_date": trade_date,
                "captured_at": captured_at,
                "status": "unavailable",
                "source_attempts": attempts,
            }
    run_suffix = captured_at.replace("-", "").replace(":", "").replace("T", "-")
    _write_immutable_json(
        date_root / f"concept_update_{trade_date}_{run_suffix}.json", status
    )
    return status


def resolve_concept_snapshot(
    *, concept_root: Path, exchange_calendar: Path, as_of_trade_date: str,
) -> tuple[list[dict[str, object]], dict[str, object], Path]:
    sessions = _calendar_sessions(exchange_calendar)
    try:
        as_of_position = sessions.index(as_of_trade_date)
    except ValueError as exc:
        raise ValueError(f"unknown trading day: {as_of_trade_date}") from exc
    for age in range(MAX_REUSE_TRADING_DAYS + 1):
        position = as_of_position - age
        if position < 0:
            break
        snapshot_date = sessions[position]
        date_root = concept_root.resolve() / snapshot_date
        canonical = date_root / f"concept_membership_{snapshot_date}.json"
        candidates = [canonical] if canonical.exists() else sorted(
            date_root.glob(f"concept_membership_{snapshot_date}_bootstrap-*.json"),
            reverse=True,
        )
        if not candidates:
            continue
        path = candidates[0]
        payload = json.loads(path.read_text(encoding="utf-8"))
        if payload.get("schema_version") != CONCEPT_SCHEMA_VERSION:
            continue
        concepts = payload.get("concepts")
        if not isinstance(concepts, list) or not concepts:
            continue
        freshness = {
            "status": (
                "normal" if age <= NORMAL_REUSE_TRADING_DAYS else "degraded"
            ),
            "snapshot_trade_date": snapshot_date,
            "age_trading_days": age,
            "maximum_reuse_trading_days": MAX_REUSE_TRADING_DAYS,
            "selected_source": payload.get("selected_source"),
            "taxonomy": payload.get("taxonomy"),
        }
        return concepts, freshness, path
    raise FileNotFoundError(
        f"no usable concept snapshot through {as_of_trade_date} "
        f"within {MAX_REUSE_TRADING_DAYS} trading days"
    )


def _complete_report(
    *, source: str, taxonomy: str, started: float,
    concepts: Sequence[Mapping[str, object]], reported_concept_count: int,
    returned_member_rows: int, valid_member_codes: int,
) -> dict[str, object]:
    complete_count = sum(bool(item.get("complete")) for item in concepts)
    complete_ratio = (
        complete_count / reported_concept_count if reported_concept_count else 0.0
    )
    parse_ratio = (
        valid_member_codes / returned_member_rows if returned_member_rows else 0.0
    )
    thresholds = {
        "minimum_concept_count": 300,
        "minimum_complete_concept_ratio": 0.95,
        "minimum_member_code_parse_ratio": 0.98,
    }
    return {
        "source": source,
        "taxonomy": taxonomy,
        "ok": (
            len(concepts) >= thresholds["minimum_concept_count"]
            and complete_ratio >= thresholds["minimum_complete_concept_ratio"]
            and parse_ratio >= thresholds["minimum_member_code_parse_ratio"]
        ),
        "duration_seconds": round(time.perf_counter() - started, 3),
        "reported_concept_count": reported_concept_count,
        "concept_count": len(concepts),
        "complete_concept_count": complete_count,
        "complete_concept_ratio": round(complete_ratio, 4),
        "returned_member_row_count": returned_member_rows,
        "valid_member_code_count": valid_member_codes,
        "member_code_parse_ratio": round(parse_ratio, 4),
        "thresholds": thresholds,
        "concepts": list(concepts),
    }


def _attempt_summary(report: Mapping[str, object]) -> dict[str, object]:
    return {
        key: report.get(key)
        for key in (
            "source", "taxonomy", "ok", "duration_seconds", "concept_count",
            "complete_concept_ratio", "member_code_parse_ratio", "error",
        )
        if key in report
    }


def _failure(
    source: str, taxonomy: str, started: float, exc: Exception
) -> dict[str, object]:
    return {
        "source": source,
        "taxonomy": taxonomy,
        "ok": False,
        "duration_seconds": round(time.perf_counter() - started, 3),
        "concept_count": 0,
        "error": f"{type(exc).__name__}: {exc}",
    }


def _calendar_sessions(path: Path) -> list[str]:
    payload = json.loads(path.resolve(strict=True).read_text(encoding="utf-8"))
    if payload.get("schema_version") != "exchange-trading-calendar.v1":
        raise ValueError("unsupported exchange calendar")
    return [str(item) for item in payload.get("sessions", [])]


def latest_session_on_or_before(path: Path, calendar_date: date) -> str:
    target = calendar_date.isoformat()
    sessions = [item for item in _calendar_sessions(path) if item <= target]
    if not sessions:
        raise ValueError(f"exchange calendar has no session through {target}")
    return sessions[-1]


def _write_immutable_json(
    path: Path, payload: Mapping[str, object]
) -> None:
    """Atomically create one shared fact and refuse to replace it."""
    path.parent.mkdir(parents=True, exist_ok=True)
    encoded = json.dumps(
        payload, ensure_ascii=False, indent=2, sort_keys=True
    ) + "\n"
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", dir=path.parent
    )
    try:
        try:
            handle = os.fdopen(descriptor, "w", encoding="utf-8")
        except OSError:
            os.close(descriptor)
            raise
        with handle:
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
        try:
            os.link(temporary_name, path)
        except FileExistsError as exc:
            raise FileExistsError(
                f"concept snapshot already exists: {path}"
            ) from exc
    finally:
        Path(temporary_name).unlink(missing_ok=True)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Publish reusable Platform concept membership facts."
    )
    parser.add_argument("--exchange-calendar", type=Path, required=True)
    parser.add_argument("--output-root", type=Path, required=True)
    parser.add_argument("--trade-date")
    args = parser.parse_args()
    trade_date = args.trade_date or latest_session_on_or_before(
        args.exchange_calendar, date.today()
    )
    result = run_concept_update(
        trade_date=trade_date,
        exchange_calendar=args.exchange_calendar,
        output_root=args.output_root,
    )
    print(json.dumps(result, ensure_ascii=False))
    return 0 if result.get("status") != "unavailable" else 2


if __name__ == "__main__":
    raise SystemExit(main())
