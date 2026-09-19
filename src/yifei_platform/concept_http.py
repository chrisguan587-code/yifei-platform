"""THS public JSON membership transport (same endpoints documented by adata).

Keep the website's 3xxxxx concept identity; resolve its 8xxxxx index from the
current detail page. Never infer an index by name or combine different taxonomies.
"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from html import unescape
import json
import math
import re
import subprocess
from threading import Event
import time
from urllib.parse import urlsplit


REQUEST_TIMEOUT = 15
SOURCE_TIMEOUT = 900
REQUEST_ATTEMPTS = 3
CATALOG_URL = "http://q.10jqka.com.cn/gn/index/field/addtime/order/desc/page/1/size/1000/"
# Public-page request format used by dragon-quant; no login/session state.
USER_AGENT = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
              "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")


def _get(url: str, deadline: float) -> str:
    parsed = urlsplit(url)
    allowed = (parsed.scheme == "https" and parsed.hostname == "d.10jqka.com.cn"
               or parsed.scheme == "http" and parsed.hostname == "q.10jqka.com.cn")
    if not allowed:
        raise ValueError("unexpected THS host")
    for attempt in range(REQUEST_ATTEMPTS):
        remaining = deadline - time.monotonic()
        if remaining < 0.1:
            raise TimeoutError("THS HTTP source deadline exceeded")
        timeout = min(REQUEST_TIMEOUT, remaining)
        # Public read-only pages only; no user cookies, accounts or proxy.
        try:
            result = subprocess.run(
                ["/usr/bin/curl", "--silent", "--show-error", "--fail",
                 "--noproxy", "*", "--connect-timeout", f"{min(5, timeout):.3f}", "--max-time",
                 f"{timeout:.3f}", "-A", USER_AGENT, "--referer", "http://q.10jqka.com.cn/", url],
                capture_output=True, timeout=timeout + 1,
            )
        except subprocess.TimeoutExpired:
            result = subprocess.CompletedProcess([], 28, b"", b"timeout")
        if result.returncode == 0 and result.stdout.strip():
            try:
                # THS HTML contains malformed bytes in unrelated page widgets.
                # Preserve replacements, then reject them in actual fact fields.
                if parsed.hostname == "q.10jqka.com.cn":
                    return result.stdout.decode("gb18030", errors="replace")
                return result.stdout.decode("utf-8")
            except UnicodeDecodeError:
                pass  # Corrupt response; retry without silently discarding bytes.
        # Access denials are not transient failures to hammer with retries.
        if re.search(rb"returned error: (401|403|429)\b", result.stderr):
            raise ConnectionError("THS HTTP access denied or rate limited")
        if attempt + 1 < REQUEST_ATTEMPTS:
            time.sleep(min(2 ** attempt, max(0, deadline - time.monotonic())))
    raise ConnectionError("THS HTTP failed or empty after 3 attempts")


def _catalog(text: str) -> list[tuple[str, str]]:
    # The sidebar is NOT the complete catalogue (live: 361 vs 390 concepts).
    # Read only the actual 1000-row table, and reject any remaining pagination.
    table = re.search(r"<tbody\b[^>]*>(.*?)</tbody>", text, re.S)
    pagination = re.search(r'class=["\']page_info["\']>\s*(\d+)/(\d+)', text)
    if not table or (pagination and pagination.groups() != ("1", "1")):
        raise ValueError("THS concept directory truncated or missing table")
    pairs = re.findall(
        r'<a\b[^>]*href=["\'][^"\']*/gn/detail/code/(\d{6})/[^"\']*["\'][^>]*>(.*?)</a>',
        table[1], re.S,
    )
    concepts: dict[str, str] = {}
    for code, raw_name in pairs:
        name = unescape(re.sub(r"<[^>]+>", "", raw_name)).strip()
        if not name or "\ufffd" in name or (code in concepts and concepts[code] != name):
            raise ValueError("ambiguous THS concept identity")
        concepts[code] = name
    if (not 300 <= len(concepts) < 1000 or len(pairs) != len(concepts)
            or len(set(concepts.values())) != len(concepts)):
        raise ValueError("incomplete or ambiguous THS concept directory")
    return sorted(concepts.items())


def _jsonp(text: str) -> dict:
    # JSONP is parsed as JSON, never evaluated as JavaScript.
    match = re.fullmatch(r"\s*quotebridge_[\w]+\((\{.*\})\)\s*;?\s*", text, re.S)
    if not match:
        raise ValueError("malformed THS JSONP")
    value = json.loads(match[1])
    if not isinstance(value, dict) or not isinstance(value.get("items"), list):
        raise ValueError("malformed THS member response")
    return value


def _members(concept: tuple[str, str], deadline: float) -> dict:
    code, name = concept
    if "\ufffd" in name:
        raise ValueError("corrupt THS concept name")
    page = _get(f"http://q.10jqka.com.cn/gn/detail/code/{code}/", deadline)
    index = re.search(r'<input\b[^>]*id=["\']clid["\'][^>]*value=["\'](8\d{5})["\']', page)
    heading = re.search(r"<h3>\s*(.*?)\s*<span>(8\d{5})</span>", page, re.S)
    if not index or not heading or index[1] != heading[2] or unescape(heading[1]).strip() != name:
        raise ValueError("THS concept/index identity mismatch")
    base = f"https://d.10jqka.com.cn/v2/blockrank/{index[1]}/8/"
    first = _jsonp(_get(base + "d15.js", deadline))
    block = first.get("block") or {}
    total = block.get("subcodeCount")
    if not isinstance(total, int) or isinstance(total, bool) or not 0 < total <= 6000:
        raise ValueError("missing or unsupported THS member total")
    if block.get("name") != name:
        raise ValueError("THS JSON block name mismatch")
    payloads = [first]
    if total > 15:
        suffixes = ([f"d{math.ceil(total / 15) * 15}.js"] if total < 3000
                    else ["a3000.js", "d3000.js"])
        payloads = [_jsonp(_get(base + suffix, deadline)) for suffix in suffixes]
    codes = []
    for payload in payloads:
        if payload.get("block", {}).get("subcodeCount") != total or payload["block"].get("name") != name:
            raise ValueError("THS member total or identity changed during capture")
        codes.extend(item.get("5") for item in payload["items"])
    valid = [code for code in codes if isinstance(code, str) and re.fullmatch(r"\d{6}", code)]
    unique = sorted(set(valid))
    if len(valid) != len(codes) or len(unique) != total:
        raise ValueError("THS member list truncated, duplicated or invalid")
    return {
        "concept_code": code, "concept_name": name, "index_code": index[1],
        "reported_member_count": total, "parsed_member_count": len(unique),
        "member_codes": unique, "complete": True,
    }


def _retry_transient_concepts(rows: list[dict], deadline: float) -> tuple[int, int]:
    """One serial recovery pass, within the original source deadline."""
    attempted = recovered = 0
    for position, row in enumerate(rows):
        if row.get("complete") or row.get("error") != "ConnectionError: THS HTTP failed or empty after 3 attempts":
            continue
        if time.monotonic() + 1 >= deadline:
            break
        time.sleep(1)
        attempted += 1
        try:
            retry = _members((row["concept_code"], row["concept_name"]), deadline)
            retry["initial_error"] = row["error"]
            rows[position] = retry
            recovered += 1
        except Exception as exc:
            row["retry_error"] = f"{type(exc).__name__}: {exc}"
            if isinstance(exc, ConnectionError) and "access denied" in str(exc):
                break
    return attempted, recovered


def fetch_ths_json_concepts() -> dict:
    from .concept_membership import _complete_report, _failure

    started = time.perf_counter()
    deadline = time.monotonic() + SOURCE_TIMEOUT
    source = "ths_public_json"
    try:
        concepts = _catalog(_get(CATALOG_URL, deadline))
        denied = Event()

        def fetch(concept):
            try:
                if denied.is_set() or time.monotonic() >= deadline:
                    raise TimeoutError("THS source stopped after denial or deadline")
                return _members(concept, deadline)
            except Exception as exc:
                if isinstance(exc, ConnectionError) and "access denied" in str(exc):
                    denied.set()
                return {"concept_code": concept[0], "concept_name": concept[1],
                        "member_codes": [], "complete": False,
                        "reported_member_count": 0, "parsed_member_count": 0,
                        "error": f"{type(exc).__name__}: {exc}"}
            finally:
                if not denied.is_set() and time.monotonic() < deadline:
                    time.sleep(0.25)

        with ThreadPoolExecutor(max_workers=2) as pool:
            rows = list(pool.map(fetch, concepts))
        retry_count, recovered_count = (0, 0) if denied.is_set() else _retry_transient_concepts(rows, deadline)
        total = sum(row["parsed_member_count"] for row in rows)
        report = _complete_report(
            source=source, taxonomy="ths_concept", started=started,
            concepts=rows, reported_concept_count=len(concepts),
            returned_member_rows=total, valid_member_codes=total,
        )
        report["source_lineage"] = {
            "capture_method": "public_http_json",
            "directory_endpoint": CATALOG_URL,
            "membership_endpoint": "d.10jqka.com.cn/v2/blockrank",
            "request_attempts": REQUEST_ATTEMPTS,
            "request_timeout_seconds": REQUEST_TIMEOUT,
            "source_timeout_seconds": SOURCE_TIMEOUT,
            "recovery_passes": 1,
            "retried_concept_count": retry_count,
            "recovered_concept_count": recovered_count,
        }
        return report
    except Exception as exc:
        return _failure(source, "ths_concept", started, exc)
