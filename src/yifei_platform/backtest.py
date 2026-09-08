from __future__ import annotations

from dataclasses import asdict, dataclass, replace
from decimal import Decimal, ROUND_HALF_UP
from enum import Enum
from statistics import median
from typing import Protocol

from .calendar import CalendarRangeError, TradingCalendarV1
from .market_data import MarketDataReaderV1, ReadStatus, StockDailyFactV1


BACKTEST_ENGINE_VERSION = "backtest-engine.v1"
BACKTEST_RESULT_VERSION = "backtest-result.v1"


class BacktestStatus(str, Enum):
    COMPLETE = "complete"
    DEGRADED = "degraded"
    BLOCKED = "blocked"


@dataclass(frozen=True)
class FeeRuleV1:
    effective_from: str
    commission_rate: float
    minimum_commission: float
    stamp_duty_sell_rate: float
    transfer_fee_rate: float

    def validate(self) -> None:
        _iso(self.effective_from)
        if min(
            self.commission_rate,
            self.minimum_commission,
            self.stamp_duty_sell_rate,
            self.transfer_fee_rate,
        ) < 0:
            raise ValueError("fee values must be non-negative")


@dataclass(frozen=True)
class FeeScheduleV1:
    version: str
    rules: tuple[FeeRuleV1, ...]
    schema_version: str = "fee-schedule.v1"

    def __post_init__(self) -> None:
        if not self.version.strip() or not self.rules:
            raise ValueError("fee schedule version and rules are required")
        for rule in self.rules:
            rule.validate()
        if tuple(sorted(rule.effective_from for rule in self.rules)) != tuple(
            rule.effective_from for rule in self.rules
        ):
            raise ValueError("fee rules must be ordered by effective_from")
        if len({rule.effective_from for rule in self.rules}) != len(self.rules):
            raise ValueError("fee rule effective dates must be unique")

    def rule_for(self, session: str) -> FeeRuleV1:
        session = _iso(session)
        selected = [rule for rule in self.rules if rule.effective_from <= session]
        if not selected:
            raise ValueError(f"fee schedule does not cover {session}")
        return selected[-1]


@dataclass(frozen=True)
class ExecutionPolicyV1:
    version: str
    slippage_rate: float
    maximum_volume_participation: float
    volume_lookback_sessions: int = 20
    minimum_volume_observations: int = 5
    price_limit_model_version: str = "a-share-price-limit.v1"
    liquidity_model_version: str = "daily-open-liquidity.v1"
    schema_version: str = "backtest-execution-policy.v1"

    def __post_init__(self) -> None:
        if not self.version.strip():
            raise ValueError("execution policy version is required")
        if self.price_limit_model_version != "a-share-price-limit.v1":
            raise ValueError("unsupported price-limit model")
        if self.liquidity_model_version != "daily-open-liquidity.v1":
            raise ValueError("unsupported liquidity model")
        if self.slippage_rate < 0:
            raise ValueError("slippage_rate must be non-negative")
        if not 0 < self.maximum_volume_participation <= 1:
            raise ValueError("maximum_volume_participation must be in (0, 1]")
        if self.volume_lookback_sessions <= 0:
            raise ValueError("volume_lookback_sessions must be positive")
        if not 1 <= self.minimum_volume_observations <= self.volume_lookback_sessions:
            raise ValueError("minimum_volume_observations is outside the lookback")


@dataclass(frozen=True)
class BacktestRunRequestV1:
    run_id: str
    decision_start_session: str
    decision_end_session: str
    market_data_start_session: str
    market_data_end_session: str
    initial_cash: float
    maximum_positions: int
    market_data_snapshot_ref: str
    market_data_snapshot_sha256: str
    market_data_source_version: str
    calendar_version: str
    price_basis_version: str
    caller: str
    caller_version: str
    adapter_version: str
    platform_version: str
    schema_version: str = "backtest-run-request.v1"

    def validate(self, calendar: TradingCalendarV1) -> None:
        for value in (
            self.run_id,
            self.market_data_snapshot_ref,
            self.market_data_snapshot_sha256,
            self.market_data_source_version,
            self.calendar_version,
            self.price_basis_version,
            self.caller,
            self.caller_version,
            self.adapter_version,
            self.platform_version,
        ):
            if not value.strip():
                raise ValueError("run request version and identity fields are required")
        if self.calendar_version != calendar.source_version:
            raise ValueError("calendar version mismatch")
        if self.price_basis_version != "raw-unadjusted.v1":
            raise ValueError("execution requires raw-unadjusted.v1 prices")
        dates = (
            self.market_data_start_session,
            self.decision_start_session,
            self.decision_end_session,
            self.market_data_end_session,
        )
        if tuple(sorted(dates)) != dates:
            raise ValueError("market and decision session ranges are inconsistent")
        if any(not calendar.is_session(value) for value in dates):
            raise ValueError("run boundaries must be exact published sessions")
        if self.initial_cash <= 0 or self.maximum_positions <= 0:
            raise ValueError("initial cash and maximum positions must be positive")


@dataclass(frozen=True)
class OrderIntentV1:
    intent_id: str
    instrument: str
    side: str
    decided_as_of: str
    earliest_execution_session: str
    submission_sequence: int
    quantity: int | None = None
    cash_budget: float | None = None
    good_until_session: str | None = None
    caller_ref: str | None = None
    schema_version: str = "backtest-order-intent.v1"

    def validate(self, calendar: TradingCalendarV1) -> None:
        if not self.intent_id.strip() or not self.instrument.strip():
            raise ValueError("intent identity is required")
        if self.side not in {"buy", "sell"}:
            raise ValueError("intent side must be buy or sell")
        if self.submission_sequence < 0:
            raise ValueError("submission_sequence must be non-negative")
        if not calendar.is_session(self.decided_as_of) or not calendar.is_session(
            self.earliest_execution_session
        ):
            raise ValueError("intent dates must be exact published sessions")
        if self.earliest_execution_session <= self.decided_as_of:
            raise ValueError("orders cannot execute on the decision session")
        if self.good_until_session is not None:
            if not calendar.is_session(self.good_until_session):
                raise ValueError("good_until_session must be an exact published session")
            if self.good_until_session < self.earliest_execution_session:
                raise ValueError("good_until_session predates execution")
        if self.side == "buy":
            if (self.quantity is None) == (self.cash_budget is None):
                raise ValueError("buy intent requires exactly one of quantity or cash_budget")
            if self.quantity is not None and (self.quantity <= 0 or self.quantity % 100):
                raise ValueError("buy quantity must be positive 100-share lots")
            if self.cash_budget is not None and self.cash_budget <= 0:
                raise ValueError("cash_budget must be positive")
        elif self.cash_budget is not None or (self.quantity is not None and self.quantity <= 0):
            raise ValueError("sell intent accepts only an optional positive quantity")


@dataclass(frozen=True)
class PortfolioPositionViewV1:
    instrument: str
    quantity: int
    sellable_quantity: int
    entry_session: str
    average_entry_price: float
    last_price: float


@dataclass(frozen=True)
class PortfolioViewV1:
    cash: float
    positions: tuple[PortfolioPositionViewV1, ...]
    pending_intent_ids: tuple[str, ...]


@dataclass(frozen=True)
class BacktestContextV1:
    as_of: str
    market: PointInTimeMarketViewV1
    portfolio: PortfolioViewV1


class OrderIntentProviderV1(Protocol):
    def on_session_close(self, context: BacktestContextV1) -> tuple[OrderIntentV1, ...]: ...


class PointInTimeMarketViewV1:
    """Bound market facts so a caller cannot ask this view for future sessions."""

    def __init__(
        self,
        *,
        as_of: str,
        calendar: TradingCalendarV1,
        market_data: MarketDataReaderV1,
        cache: dict[str, dict[str, StockDailyFactV1]],
    ):
        self.as_of = _iso(as_of)
        self._calendar = calendar
        self._market_data = market_data
        self._cache = cache

    def facts(self, session: str) -> tuple[StockDailyFactV1, ...]:
        requested = _iso(session)
        if requested > self.as_of:
            raise ValueError("point-in-time market view cannot read the future")
        return tuple(self._facts_by_code(requested).values())

    def fact(self, instrument: str, session: str | None = None) -> StockDailyFactV1 | None:
        requested = _iso(session or self.as_of)
        if requested > self.as_of:
            raise ValueError("point-in-time market view cannot read the future")
        return self._facts_by_code(requested).get(instrument)

    def history(
        self, instrument: str, *, end_session: str | None = None, sessions: int
    ) -> tuple[StockDailyFactV1, ...]:
        """Return available facts in the trailing session window.

        Missing facts and dates before the published calendar range are omitted. A short
        result therefore means insufficient history, not a shorter requested lookback.
        """
        if sessions <= 0:
            raise ValueError("history sessions must be positive")
        end = _iso(end_session or self.as_of)
        if end > self.as_of:
            raise ValueError("point-in-time market view cannot read the future")
        if not self._calendar.is_session(end):
            raise ValueError("history end must be an exact session")
        values: list[StockDailyFactV1] = []
        for offset in range(-(sessions - 1), 1):
            try:
                current = self._calendar.offset_session(end, offset).isoformat()
            except CalendarRangeError:
                continue
            fact = self.fact(instrument, current)
            if fact is not None:
                values.append(fact)
        return tuple(values)

    def _facts_by_code(self, session: str) -> dict[str, StockDailyFactV1]:
        if session not in self._cache:
            result = self._market_data.read_stock_daily(session)
            if result.status is ReadStatus.BLOCKED:
                raise ValueError(f"market data blocked for {session}: {result.reason_codes}")
            self._cache[session] = {fact.stock_code: fact for fact in result.facts}
        return self._cache[session]


class FrozenOrderIntentProviderV1:
    """Simple manual-CLI consumer backed by a frozen list of caller-owned intents."""

    def __init__(self, intents: tuple[OrderIntentV1, ...]):
        grouped: dict[str, list[OrderIntentV1]] = {}
        for intent in intents:
            grouped.setdefault(intent.decided_as_of, []).append(intent)
        self._grouped = {
            session: tuple(sorted(rows, key=lambda row: row.submission_sequence))
            for session, rows in grouped.items()
        }

    def on_session_close(self, context: BacktestContextV1) -> tuple[OrderIntentV1, ...]:
        return self._grouped.get(context.as_of, ())


@dataclass(frozen=True)
class BacktestResultV1:
    run_id: str
    status: BacktestStatus
    reason_codes: tuple[str, ...]
    summary: dict[str, object]
    intents: tuple[dict[str, object], ...]
    orders: tuple[dict[str, object], ...]
    fills: tuple[dict[str, object], ...]
    trades: tuple[dict[str, object], ...]
    positions: tuple[dict[str, object], ...]
    daily_snapshots: tuple[dict[str, object], ...]
    schema_version: str = BACKTEST_RESULT_VERSION

    def as_dict(self) -> dict[str, object]:
        payload = asdict(self)
        payload["status"] = self.status.value
        return payload


@dataclass
class _Position:
    instrument: str
    entry_session: str
    sellable_from: str
    initial_quantity: int
    quantity: int
    average_entry_price: float
    entry_turnover: float
    entry_fees: float
    exit_turnover: float = 0.0
    exit_fees: float = 0.0
    last_price: float = 0.0


class BacktestEngineV1:
    """Neutral daily A-share execution simulator for caller-owned order intents."""

    def __init__(
        self,
        *,
        calendar: TradingCalendarV1,
        market_data: MarketDataReaderV1,
        fee_schedule: FeeScheduleV1,
        execution_policy: ExecutionPolicyV1,
    ):
        self.calendar = calendar
        self.market_data = market_data
        self.fee_schedule = fee_schedule
        self.execution_policy = execution_policy
        self._cache: dict[str, dict[str, StockDailyFactV1]] = {}

    def run(
        self, *, request: BacktestRunRequestV1, provider: OrderIntentProviderV1
    ) -> BacktestResultV1:
        request.validate(self.calendar)
        sessions = tuple(
            session.isoformat()
            for session in self.calendar.sessions
            if request.market_data_start_session <= session.isoformat() <= request.market_data_end_session
        )
        if not sessions:
            raise ValueError("market data range contains no sessions")

        cash = request.initial_cash
        positions: dict[str, _Position] = {}
        pending: list[OrderIntentV1] = []
        seen_intents: set[str] = set()
        intents: list[dict[str, object]] = []
        orders: list[dict[str, object]] = []
        fills: list[dict[str, object]] = []
        trades: list[dict[str, object]] = []
        snapshots: list[dict[str, object]] = []
        reasons: set[str] = set()
        used_volume: dict[tuple[str, str], int] = {}

        for session in sessions:
            self._assert_position_price_continuity(session, positions)
            cash, pending = self._execute_open(
                session=session,
                cash=cash,
                positions=positions,
                pending=pending,
                orders=orders,
                fills=fills,
                trades=trades,
                used_volume=used_volume,
                maximum_positions=request.maximum_positions,
                reasons=reasons,
            )
            market_value = 0.0
            stale_position_count = 0
            for position in positions.values():
                fact = self._fact(position.instrument, session)
                if fact is not None and fact.close is not None and fact.close > 0:
                    position.last_price = fact.close
                else:
                    reasons.add("position_close_missing")
                    stale_position_count += 1
                market_value += position.quantity * position.last_price
            snapshots.append(
                {
                    "session": session,
                    "cash": _money(cash),
                    "market_value": _money(market_value),
                    "nav": _money(cash + market_value),
                    "position_count": len(positions),
                    "pending_order_count": len(pending),
                    "stale_position_count": stale_position_count,
                }
            )

            if request.decision_start_session <= session <= request.decision_end_session:
                view = PointInTimeMarketViewV1(
                    as_of=session,
                    calendar=self.calendar,
                    market_data=self.market_data,
                    cache=self._cache,
                )
                try:
                    generated = tuple(provider.on_session_close(BacktestContextV1(
                        as_of=session,
                        market=view,
                        portfolio=self._portfolio_view(cash, positions, pending, session),
                    )))
                except Exception as exc:
                    raise RuntimeError(
                        f"order intent provider failed on {session}: "
                        f"{type(exc).__name__}: {exc}"
                    ) from exc
                for intent in generated:
                    intent.validate(self.calendar)
                    if intent.decided_as_of != session:
                        raise ValueError("provider returned an intent for a different decision session")
                    if intent.intent_id in seen_intents:
                        raise ValueError(f"duplicate intent id: {intent.intent_id}")
                    seen_intents.add(intent.intent_id)
                    intents.append(_intent_dict(intent))
                    pending.append(intent)
                pending.sort(key=lambda row: (row.earliest_execution_session, row.side != "sell", row.submission_sequence))

        if pending:
            reasons.add("orders_pending_at_end")
        final_positions = tuple(
            self._position_dict(position, sessions[-1])
            for position in sorted(positions.values(), key=lambda item: item.instrument)
        )
        if any(item["sellable_quantity"] == 0 for item in final_positions):
            reasons.add("unsettled_positions_at_end")
        final_nav = snapshots[-1]["nav"]
        navs = [float(row["nav"]) for row in snapshots]
        max_drawdown = _max_drawdown(navs)
        closed_returns = [float(row["net_return_pct"]) for row in trades]
        realized = sum(float(row["net_profit"]) for row in trades)
        unrealized = 0.0
        for position in positions.values():
            entry_cost_per_share = (
                position.entry_turnover + position.entry_fees
            ) / position.initial_quantity
            sold_quantity = position.initial_quantity - position.quantity
            realized += (
                position.exit_turnover
                - position.exit_fees
                - entry_cost_per_share * sold_quantity
            )
            unrealized += position.quantity * (
                position.last_price - entry_cost_per_share
            )
        status = BacktestStatus.DEGRADED if reasons else BacktestStatus.COMPLETE
        summary = {
            "engine_version": BACKTEST_ENGINE_VERSION,
            "initial_cash": _money(request.initial_cash),
            "final_cash": _money(cash),
            "final_nav": _money(float(final_nav)),
            "total_return_pct": _pct((float(final_nav) / request.initial_cash - 1) * 100),
            "max_drawdown_pct": _pct(max_drawdown),
            "intent_count": len(intents),
            "fill_count": len(fills),
            "closed_trade_count": len(trades),
            "open_position_count": len(positions),
            "win_rate_pct": _pct(
                sum(value > 0 for value in closed_returns) / len(closed_returns) * 100
                if closed_returns else 0.0
            ),
            "average_closed_trade_return_pct": _pct(
                sum(closed_returns) / len(closed_returns) if closed_returns else 0.0
            ),
            "realized_profit": _money(realized),
            "unrealized_profit": _money(unrealized),
        }
        return BacktestResultV1(
            run_id=request.run_id,
            status=status,
            reason_codes=tuple(sorted(reasons)),
            summary=summary,
            intents=tuple(intents),
            orders=tuple(orders),
            fills=tuple(fills),
            trades=tuple(trades),
            positions=final_positions,
            daily_snapshots=tuple(snapshots),
        )

    def _assert_position_price_continuity(
        self, session: str, positions: dict[str, _Position]
    ) -> None:
        """Fail closed when raw prices cannot preserve an existing position economically.

        A changed pre-close can represent an ex-right/ex-dividend adjustment or broken price
        lineage. V1 has no corporate-action ledger, so continuing would invent portfolio P&L.
        """
        if not positions:
            return
        try:
            previous_session = self.calendar.offset_session(session, -1).isoformat()
        except CalendarRangeError:
            return
        for position in positions.values():
            current = self._fact(position.instrument, session)
            previous = self._fact(position.instrument, previous_session)
            if (
                current is None
                or previous is None
                or current.preclose is None
                or previous.close is None
                or current.preclose <= 0
                or previous.close <= 0
            ):
                continue
            if abs(float(current.preclose) - float(previous.close)) > 0.011:
                raise ValueError(
                    "corporate action or price-lineage discontinuity for "
                    f"{position.instrument} on {session}"
                )

    def _execute_open(
        self,
        *,
        session: str,
        cash: float,
        positions: dict[str, _Position],
        pending: list[OrderIntentV1],
        orders: list[dict[str, object]],
        fills: list[dict[str, object]],
        trades: list[dict[str, object]],
        used_volume: dict[tuple[str, str], int],
        maximum_positions: int,
        reasons: set[str],
    ) -> tuple[float, list[OrderIntentV1]]:
        remaining: list[OrderIntentV1] = []
        due = sorted(
            (row for row in pending if row.earliest_execution_session <= session),
            key=lambda row: (row.side != "sell", row.submission_sequence),
        )
        remaining.extend(row for row in pending if row.earliest_execution_session > session)
        for intent in due:
            if intent.good_until_session is not None and session > intent.good_until_session:
                orders.append(_order_event(intent, session, "expired", "good_until_passed"))
                continue
            if intent.side == "buy" and session != intent.earliest_execution_session:
                orders.append(_order_event(intent, session, "expired", "buy_is_next_open_only"))
                continue
            fact = self._fact(intent.instrument, session)
            if fact is None or not _valid_bar(fact):
                orders.append(_order_event(intent, session, "unfilled", "market_bar_missing"))
                reasons.add("market_bar_missing")
                if intent.side == "sell" and _can_retry(intent, session):
                    remaining.append(intent)
                    reasons.add("sell_delayed_by_missing_bar")
                continue
            limit_reason, lower_limit, upper_limit = self._limit_block(intent.side, fact, session)
            if limit_reason:
                orders.append(_order_event(intent, session, "unfilled", limit_reason))
                if limit_reason in {
                    "preclose_missing",
                    "listing_stage_ambiguous",
                    "price_limit_rule_unknown",
                }:
                    reasons.add(limit_reason)
                if intent.side == "sell" and _can_retry(intent, session):
                    remaining.append(intent)
                    reasons.add("sell_delayed_by_price_limit")
                continue
            capacity = self._volume_capacity(intent.instrument, session)
            if capacity <= 0:
                orders.append(_order_event(intent, session, "unfilled", "liquidity_history_insufficient"))
                reasons.add("liquidity_history_insufficient")
                if intent.side == "sell" and _can_retry(intent, session):
                    remaining.append(intent)
                    reasons.add("sell_delayed_by_liquidity")
                continue
            available_capacity = max(capacity - used_volume.get((intent.instrument, session), 0), 0)
            if intent.side == "buy":
                if intent.instrument in positions:
                    orders.append(_order_event(intent, session, "rejected", "position_already_open"))
                    continue
                if len(positions) >= maximum_positions:
                    orders.append(_order_event(intent, session, "rejected", "position_capacity_reached"))
                    continue
                if upper_limit is None:
                    raise RuntimeError("price-limit model returned no upper limit")
                execution_price = min(
                    float(fact.open) * (1 + self.execution_policy.slippage_rate),
                    upper_limit,
                )
                requested = intent.quantity or int(float(intent.cash_budget or 0) // execution_price // 100 * 100)
                quantity = min(requested, available_capacity)
                quantity = quantity // 100 * 100
                budget = min(cash, float(intent.cash_budget)) if intent.cash_budget is not None else cash
                while quantity > 0:
                    turnover = execution_price * quantity
                    fees = self._fees(turnover, "buy", session)
                    if turnover + fees <= budget:
                        break
                    quantity -= 100
                if quantity <= 0:
                    reason = (
                        "cash_budget_insufficient"
                        if intent.cash_budget is not None and intent.cash_budget < cash
                        else "cash_or_liquidity_insufficient"
                    )
                    orders.append(_order_event(
                        intent,
                        session,
                        "unfilled",
                        reason,
                        requested_quantity=requested,
                        unfilled_quantity=requested,
                    ))
                    continue
                turnover = execution_price * quantity
                fees = self._fees(turnover, "buy", session)
                try:
                    sellable_from = self.calendar.offset_session(session, 1).isoformat()
                except CalendarRangeError:
                    orders.append(_order_event(
                        intent,
                        session,
                        "unfilled",
                        "settlement_session_unavailable",
                        requested_quantity=requested,
                        unfilled_quantity=requested,
                    ))
                    reasons.add("settlement_session_unavailable")
                    continue
                cash -= turnover + fees
                positions[intent.instrument] = _Position(
                    instrument=intent.instrument,
                    entry_session=session,
                    sellable_from=sellable_from,
                    initial_quantity=quantity,
                    quantity=quantity,
                    average_entry_price=execution_price,
                    entry_turnover=turnover,
                    entry_fees=fees,
                    last_price=execution_price,
                )
                used_volume[(intent.instrument, session)] = used_volume.get((intent.instrument, session), 0) + quantity
                status = "partially_filled" if quantity < requested else "filled"
                fills.append(_fill(intent, session, execution_price, quantity, turnover, fees, status))
                orders.append(_order_event(
                    intent,
                    session,
                    status,
                    None,
                    quantity,
                    requested_quantity=requested,
                    unfilled_quantity=requested - quantity,
                ))
                continue

            position = positions.get(intent.instrument)
            if position is None:
                orders.append(_order_event(intent, session, "rejected", "position_missing"))
                continue
            if session < position.sellable_from:
                orders.append(_order_event(intent, session, "unfilled", "t1_not_sellable"))
                if _can_retry(intent, session):
                    remaining.append(intent)
                continue
            requested = min(intent.quantity or position.quantity, position.quantity)
            quantity = min(requested, available_capacity)
            if quantity < position.quantity:
                quantity = quantity // 100 * 100
            if quantity <= 0:
                orders.append(_order_event(intent, session, "unfilled", "liquidity_insufficient"))
                if _can_retry(intent, session):
                    remaining.append(intent)
                continue
            if lower_limit is None:
                raise RuntimeError("price-limit model returned no lower limit")
            execution_price = max(
                float(fact.open) * (1 - self.execution_policy.slippage_rate),
                lower_limit,
            )
            turnover = execution_price * quantity
            fees = self._fees(turnover, "sell", session)
            cash += turnover - fees
            position.exit_turnover += turnover
            position.exit_fees += fees
            position.quantity -= quantity
            used_volume[(intent.instrument, session)] = used_volume.get((intent.instrument, session), 0) + quantity
            unfilled_requested = requested - quantity
            status = "partially_filled" if unfilled_requested else "filled"
            fills.append(_fill(intent, session, execution_price, quantity, turnover, fees, status))
            orders.append(_order_event(
                intent,
                session,
                status,
                None,
                quantity,
                requested_quantity=requested,
                unfilled_quantity=unfilled_requested,
            ))
            if unfilled_requested and _can_retry(intent, session):
                remaining.append(replace(
                    intent,
                    quantity=unfilled_requested if intent.quantity is not None else None,
                ))
            if not position.quantity:
                entry_cost = position.entry_turnover + position.entry_fees
                exit_net = position.exit_turnover - position.exit_fees
                net_profit = exit_net - entry_cost
                trades.append({
                    "instrument": position.instrument,
                    "entry_session": position.entry_session,
                    "exit_session": session,
                    "entry_turnover": _money(position.entry_turnover),
                    "entry_fees": _money(position.entry_fees),
                    "exit_turnover": _money(position.exit_turnover),
                    "exit_fees": _money(position.exit_fees),
                    "net_profit": _money(net_profit),
                    "net_return_pct": _pct(net_profit / entry_cost * 100),
                })
                del positions[intent.instrument]
        return cash, remaining

    def _limit_block(
        self, side: str, fact: StockDailyFactV1, session: str
    ) -> tuple[str | None, float | None, float | None]:
        if fact.preclose is None or fact.preclose <= 0:
            return "preclose_missing", None, None
        # Looking farther back prevents one isolated missing recent bar on a mature stock
        # from being mistaken for a newly listed instrument.
        observed = 0
        for offset in range(250):
            try:
                historical_session = self.calendar.offset_session(session, -offset).isoformat()
            except CalendarRangeError:
                break
            if self._fact(fact.stock_code, historical_session) is not None:
                observed += 1
                if observed == 6:
                    break
        if observed < 6:
            return "listing_stage_ambiguous", None, None
        ratio = _price_limit_ratio(fact.stock_code, session, fact.is_st)
        if ratio is None:
            return "price_limit_rule_unknown", None, None
        upper = _limit_price(fact.preclose, 1 + ratio)
        lower = _limit_price(fact.preclose, 1 - ratio)
        # A-share stocks quote in RMB 0.01 increments. Half a tick only absorbs float noise.
        tolerance = 0.005
        if side == "buy" and float(fact.open) >= upper - tolerance:
            return "open_at_upper_limit", lower, upper
        if side == "sell" and float(fact.open) <= lower + tolerance:
            return "open_at_lower_limit", lower, upper
        return None, lower, upper

    def _volume_capacity(self, instrument: str, session: str) -> int:
        history = self._history(
            instrument,
            session,
            self.execution_policy.volume_lookback_sessions,
            include_current=False,
        )
        volumes = [float(row.volume) for row in history if row.volume is not None and row.volume > 0]
        if len(volumes) < self.execution_policy.minimum_volume_observations:
            return 0
        capacity = int(median(volumes) * self.execution_policy.maximum_volume_participation)
        return capacity // 100 * 100

    def _history(
        self, instrument: str, session: str, count: int, *, include_current: bool
    ) -> tuple[StockDailyFactV1, ...]:
        end_offset = 0 if include_current else -1
        rows: list[StockDailyFactV1] = []
        for offset in range(end_offset - count + 1, end_offset + 1):
            try:
                current = self.calendar.offset_session(session, offset).isoformat()
            except CalendarRangeError:
                continue
            fact = self._fact(instrument, current)
            if fact is not None:
                rows.append(fact)
        return tuple(rows)

    def _fact(self, instrument: str, session: str) -> StockDailyFactV1 | None:
        if session not in self._cache:
            result = self.market_data.read_stock_daily(session)
            if result.status is ReadStatus.BLOCKED:
                raise ValueError(f"market data blocked for {session}: {result.reason_codes}")
            self._cache[session] = {fact.stock_code: fact for fact in result.facts}
        return self._cache[session].get(instrument)

    def _fees(self, turnover: float, side: str, session: str) -> float:
        rule = self.fee_schedule.rule_for(session)
        commission = max(turnover * rule.commission_rate, rule.minimum_commission)
        stamp = turnover * rule.stamp_duty_sell_rate if side == "sell" else 0.0
        transfer = turnover * rule.transfer_fee_rate
        return commission + stamp + transfer

    @staticmethod
    def _portfolio_view(
        cash: float,
        positions: dict[str, _Position],
        pending: list[OrderIntentV1],
        session: str,
    ) -> PortfolioViewV1:
        return PortfolioViewV1(
            cash=_money(cash),
            positions=tuple(
                PortfolioPositionViewV1(
                    instrument=item.instrument,
                    quantity=item.quantity,
                    sellable_quantity=item.quantity if session >= item.sellable_from else 0,
                    entry_session=item.entry_session,
                    average_entry_price=_money(item.average_entry_price),
                    last_price=_money(item.last_price),
                )
                for item in sorted(positions.values(), key=lambda row: row.instrument)
            ),
            pending_intent_ids=tuple(row.intent_id for row in pending),
        )

    @staticmethod
    def _position_dict(position: _Position, session: str) -> dict[str, object]:
        return {
            "instrument": position.instrument,
            "entry_session": position.entry_session,
            "quantity": position.quantity,
            "initial_quantity": position.initial_quantity,
            "sellable_quantity": position.quantity if session >= position.sellable_from else 0,
            "average_entry_price": _money(position.average_entry_price),
            "last_price": _money(position.last_price),
            "entry_turnover": _money(position.entry_turnover),
            "entry_fees": _money(position.entry_fees),
            "partial_exit_turnover": _money(position.exit_turnover),
            "partial_exit_fees": _money(position.exit_fees),
        }


def _price_limit_ratio(code: str, session: str, is_st: bool | None) -> float | None:
    session = _iso(session)
    if code.startswith(("60", "00")):
        if is_st is None:
            return None
        return 0.05 if is_st else 0.10
    if code.startswith("30"):
        return 0.20 if session >= "2020-08-24" else 0.10
    if code.startswith(("688", "689")):
        return 0.20
    if code.startswith(("43", "83", "87", "8", "920")):
        return 0.30
    return None


def _limit_price(preclose: float, multiplier: float) -> float:
    if preclose <= 0:
        raise ValueError("preclose must be positive")
    return float((Decimal(str(preclose)) * Decimal(str(multiplier))).quantize(
        Decimal("0.01"), rounding=ROUND_HALF_UP
    ))


def _valid_bar(fact: StockDailyFactV1) -> bool:
    return all(value is not None and value > 0 for value in (fact.open, fact.high, fact.low, fact.close))


def _can_retry(intent: OrderIntentV1, session: str) -> bool:
    return intent.side == "sell" and (
        intent.good_until_session is None or session < intent.good_until_session
    )


def _intent_dict(intent: OrderIntentV1) -> dict[str, object]:
    return asdict(intent)


def _order_event(
    intent: OrderIntentV1,
    session: str,
    status: str,
    reason: str | None,
    filled_quantity: int = 0,
    *,
    requested_quantity: int | None = None,
    unfilled_quantity: int | None = None,
) -> dict[str, object]:
    return {
        "intent_id": intent.intent_id,
        "instrument": intent.instrument,
        "side": intent.side,
        "session": session,
        "status": status,
        "reason": reason,
        "filled_quantity": filled_quantity,
        "requested_quantity": requested_quantity,
        "unfilled_quantity": unfilled_quantity,
    }


def _fill(
    intent: OrderIntentV1,
    session: str,
    price: float,
    quantity: int,
    turnover: float,
    fees: float,
    status: str,
) -> dict[str, object]:
    return {
        "intent_id": intent.intent_id,
        "instrument": intent.instrument,
        "side": intent.side,
        "session": session,
        "status": status,
        "price": _money(price),
        "quantity": quantity,
        "turnover": _money(turnover),
        "fees": _money(fees),
    }


def _max_drawdown(navs: list[float]) -> float:
    peak = 0.0
    worst = 0.0
    for nav in navs:
        peak = max(peak, nav)
        if peak > 0:
            worst = min(worst, (nav - peak) / peak * 100)
    return abs(worst)


def _money(value: float) -> float:
    return round(float(value), 4)


def _pct(value: float) -> float:
    return round(float(value), 4)


def _iso(value: str) -> str:
    from datetime import date

    return date.fromisoformat(value).isoformat()
