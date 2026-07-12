# B1 Eligibility, Board, and Capital Facts v1

> Schemas: `eligibility-facts.v1`, `board-daily-facts.v1`, `sector-capital-facts.v1`.

## EligibilityPrimitiveV1

The primitive returns market segment, tri-state ST fact, tri-state delisting fact, and raw amount/volume/turnover. It never returns `eligible`, `liquidity_ok`, Setup qualification, score, or action.

Current `stock_daily` has no historical delisting field. Therefore `delisting_state` is explicitly `unknown`; absence of data is not interpreted as false. Segment interpretation requires a versioned market-rules identifier.

## BoardFactReaderV1

Reads exact historical rows from `ths_board_daily` through a read-only connection. It returns raw OHLC, volume, amount, and percentage change with source/schema versions. It does not return mainline, lifecycle, confidence, action, or position coefficients.

## CapitalFactReaderV1

V1 reads sector-level facts from `sector_fund_flow_daily`: raw amount, change, main inflow, breadth counts, and lead-stock fields. Units are passed through unchanged and must be defined by the source version.

There is no audited public individual-stock capital table in the current database. V1 does not invent one from V3 confirmation, rankings, or candidate outputs.

## Deferred Membership Contract

`ths_stock_industry` is a current snapshot with `updated_at`, not a history table with validity intervals. It cannot support reliable PIT replay. A public Board Membership contract is deferred until the Platform writer stores `valid_from/valid_to` or immutable dated snapshots.

## Compatibility

- Exact historical `as_of` remains valid when newer facts exist.
- Optional physical fields map to `None`, never zero/neutral.
- New eligibility decisions or thresholds belong to applications.
- Adding score/action/state fields is forbidden in v1.
- Changing market prefix meaning, capital units, or source semantics requires a new source/rules version.
