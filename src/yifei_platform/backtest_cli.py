from __future__ import annotations

import argparse
from dataclasses import asdict
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import subprocess
import tempfile

from . import __version__
from .backtest import (
    BACKTEST_ENGINE_VERSION,
    BacktestEngineV1,
    BacktestRunRequestV1,
    ExecutionPolicyV1,
    FeeRuleV1,
    FeeScheduleV1,
    FrozenOrderIntentProviderV1,
    OrderIntentV1,
)
from .bootstrap import load_trading_sessions
from .calendar import TradingCalendarV1
from .market_data import MarketDataReaderV1, MarketDataSourceV1


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run one immutable, manual Yifei Platform execution backtest."
    )
    parser.add_argument("--spec", type=Path, required=True)
    parser.add_argument("--market-db", type=Path, required=True)
    parser.add_argument("--output-root", type=Path, required=True)
    return parser.parse_args(argv)


def run_from_files(*, spec_path: Path, market_db: Path, output_root: Path) -> Path:
    spec = _read_object(spec_path)
    _require_keys(spec, (
        "run_id", "calendar_version", "fee_schedule", "execution_policy",
        "decision_start_session", "decision_end_session", "market_data_start_session",
        "market_data_end_session", "initial_cash", "maximum_positions",
        "market_data_source_version", "price_basis_version", "caller",
        "caller_version", "adapter_version",
    ))
    run_id = _safe_run_id(str(spec["run_id"]))
    output = output_root.resolve() / run_id
    if output.exists():
        raise FileExistsError(f"backtest run already exists: {output}")
    source = market_db.resolve(strict=True)
    output.mkdir(parents=True)
    try:
        snapshot = output / "market_data.snapshot.db"
        _sqlite_snapshot(source, snapshot)
        snapshot_sha = _sha256_file(snapshot)
        calendar_version = str(spec["calendar_version"])
        calendar = TradingCalendarV1(
            load_trading_sessions(snapshot), source_version=calendar_version
        )
        fee_schedule = FeeScheduleV1(
            version=str(spec["fee_schedule"]["version"]),
            rules=tuple(FeeRuleV1(**item) for item in spec["fee_schedule"]["rules"]),
        )
        execution_policy = ExecutionPolicyV1(**spec["execution_policy"])
        request = BacktestRunRequestV1(
            run_id=run_id,
            decision_start_session=str(spec["decision_start_session"]),
            decision_end_session=str(spec["decision_end_session"]),
            market_data_start_session=str(spec["market_data_start_session"]),
            market_data_end_session=str(spec["market_data_end_session"]),
            initial_cash=float(spec["initial_cash"]),
            maximum_positions=int(spec["maximum_positions"]),
            market_data_snapshot_ref=str(snapshot),
            market_data_snapshot_sha256=snapshot_sha,
            market_data_source_version=str(spec["market_data_source_version"]),
            calendar_version=calendar_version,
            price_basis_version=str(spec["price_basis_version"]),
            caller=str(spec["caller"]),
            caller_version=str(spec["caller_version"]),
            adapter_version=str(spec["adapter_version"]),
            platform_version=f"{__version__}+{_git_sha()}",
        )
        intents = tuple(OrderIntentV1(**item) for item in spec.get("intents", []))
        for intent in intents:
            intent.validate(calendar)
        outside = tuple(
            item.intent_id for item in intents
            if not request.decision_start_session <= item.decided_as_of <= request.decision_end_session
        )
        if outside:
            raise ValueError(f"intent decision dates are outside the run range: {outside}")
        reader = MarketDataReaderV1(MarketDataSourceV1(
            snapshot,
            source_version=request.market_data_source_version,
        ))
        result = BacktestEngineV1(
            calendar=calendar,
            market_data=reader,
            fee_schedule=fee_schedule,
            execution_policy=execution_policy,
        ).run(request=request, provider=FrozenOrderIntentProviderV1(intents))
        manifest = {
            "schema_version": "backtest-manifest.v1",
            "created_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
            "engine_version": BACKTEST_ENGINE_VERSION,
            "request": asdict(request),
            "fee_schedule": asdict(fee_schedule),
            "execution_policy": asdict(execution_policy),
            "source_market_database": str(source),
            "source_market_database_sha256": _sha256_file(source),
            "snapshot_sha256": snapshot_sha,
            "spec_sha256": _sha256_file(spec_path),
        }
        _write_json(output / "manifest.json", manifest)
        payload = result.as_dict()
        _write_json(output / "result.json", payload)
        for name in ("intents", "orders", "fills", "trades", "positions", "daily_snapshots"):
            _write_json(output / f"{name}.json", payload[name])
        _write_json(output / "summary.json", payload["summary"])
        (output / "report.md").write_text(_report(payload), encoding="utf-8")
        snapshot.chmod(0o444)
        return output
    except Exception:
        _remove_new_workspace(output)
        raise


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    print(run_from_files(
        spec_path=args.spec,
        market_db=args.market_db,
        output_root=args.output_root,
    ))
    return 0


def _sqlite_snapshot(source: Path, destination: Path) -> None:
    with sqlite3.connect(
        f"file:{source}?mode=ro", uri=True, timeout=30
    ) as source_connection, sqlite3.connect(
        destination, timeout=30
    ) as destination_connection:
        source_connection.backup(destination_connection)
    with sqlite3.connect(f"file:{destination}?mode=ro", uri=True) as connection:
        result = connection.execute("PRAGMA integrity_check").fetchone()
        if not result or result[0] != "ok":
            raise ValueError("market data snapshot integrity check failed")


def _read_object(path: Path) -> dict[str, object]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("backtest spec must be a JSON object")
    return payload


def _safe_run_id(value: str) -> str:
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", value):
        raise ValueError("run_id must contain only letters, digits, '.', '_' or '-'")
    return value


def _require_keys(payload: dict[str, object], keys: tuple[str, ...]) -> None:
    missing = tuple(key for key in keys if key not in payload)
    if missing:
        raise ValueError(f"backtest spec missing required fields: {missing}")


def _write_json(path: Path, payload: object) -> None:
    encoded = json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _git_sha() -> str:
    root = Path(__file__).resolve().parents[2]
    try:
        return subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=root, check=True,
            capture_output=True, text=True,
        ).stdout.strip()
    except (OSError, subprocess.CalledProcessError):
        return "package"


def _remove_new_workspace(path: Path) -> None:
    if not path.exists():
        return
    for child in sorted(path.rglob("*"), reverse=True):
        if child.is_symlink():
            child.unlink(missing_ok=True)
        elif child.is_file():
            child.chmod(0o600)
            child.unlink()
        elif child.is_dir():
            child.rmdir()
    path.rmdir()


def _report(payload: dict[str, object]) -> str:
    summary = payload["summary"]
    if not isinstance(summary, dict):
        raise TypeError("payload['summary'] must be a dict")
    reasons = payload["reason_codes"]
    reason_text = "、".join(str(item) for item in reasons) if reasons else "无"
    return "\n".join((
        f"# Yifei 回测结果｜{payload['run_id']}",
        "",
        f"- 状态：{payload['status']}",
        f"- 总收益：{summary['total_return_pct']:+.2f}%",
        f"- 最大回撤：{summary['max_drawdown_pct']:.2f}%",
        f"- 订单意图：{summary['intent_count']}",
        f"- 成交：{summary['fill_count']}",
        f"- 已结束交易：{summary['closed_trade_count']}",
        f"- 期末持仓：{summary['open_position_count']}",
        f"- 已实现收益：{summary['realized_profit']:+.2f}",
        f"- 未实现收益：{summary['unrealized_profit']:+.2f}",
        f"- 降级原因：{reason_text}",
        "",
        "仅记录冻结订单在统一成交规则下的模拟结果，不生成策略、评分或交易建议。",
        "",
    ))


if __name__ == "__main__":
    raise SystemExit(main())
