# A1 Calendar and Market Data Contracts v1

> Status: implemented candidate, pending first consumer compatibility review.
> Schema contracts: `trading-calendar.v1` and `market-data.stock-daily.v1`.

## TradingCalendarV1

Input is a non-empty, versioned set of published exchange sessions. The contract does not infer sessions from weekdays or public-workday calendars.

| Operation | Semantics |
|:--|:--|
| `is_session(date)` | Exact membership in the published session set |
| `resolve_session(date)` | Latest published session on or before the date |
| `offset_session(date, n)` | Resolve the base, then move by trading-session index |
| `context(date)` | Requested date, resolved/adjacent sessions, source/schema versions |

Dates before the first session or offsets beyond the published range raise `CalendarRangeError`. The contract never silently guesses an unavailable session.

## MarketDataReaderV1

`read_stock_daily(as_of)` reads the exact requested trading date through a SQLite read-only connection. A newer database date does not make a valid historical observation stale.

The Reader returns all available market rows. It does not apply V3/V4 eligibility, segment, ST, liquidity, score, or opportunity filters.

### Status

| Status | Meaning |
|:--|:--|
| `ok` | At least one fact exists for exact `as_of` |
| `missing` | Database, dataset, or exact date is absent |
| `blocked` | Required schema is absent or SQLite cannot safely read |

`latest_available_as_of` is metadata, not a replacement for the requested date. `reason_codes` explain non-OK results without embedding application action.

### Fields

Required physical fields are `stock_code` and `trade_date`. V1 exposes optional name, OHLC, preclose, volume, amount, percentage change, turnover, and ST facts. If an optional physical column is absent, its value is `None`; the Reader does not invent zero or neutral values.

### Source identity

The caller must provide a non-empty `source_version`. Results include source and schema versions. Dataset production and source-version assignment belong to the Platform writer, not the Reader or an application.

## Compatibility

Within v1:

- Existing field meanings and status semantics cannot change.
- New optional result fields may be added with defaults.
- New reason codes may be added without changing existing meanings.
- Required physical columns cannot be added without a new contract version or an explicit compatibility window.
- Price adjustment and corporate-action semantics are not yet claimed by this contract; consumers must not infer them.

A future provider/calendar adapter must pass these golden contract fixtures before becoming a published source.
