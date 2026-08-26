# B3 Sector Market Daily Facts v1

> Schema: `sector-market-daily-facts.v1`.

## Purpose and consumer

Platform publishes one neutral daily row for each PIT-valid THS L2 industry so
V4 can build its human-only sector battlefield without waiting for the late THS
industry-index endpoint. V4 market cognition is the current production
consumer. The dataset does not contain quadrant labels, attention, score,
recommendation, Setup, Decision, or trading semantics.

## Input and calculation

For each of the latest 30 published market sessions, the publisher joins:

- exact-date `stock_daily.pct_chg` and `stock_daily.amount`;
- `sector_membership_history` rows valid on that date with
  `sector_level=THS_L2`.

Each `sector_market_daily` row stores:

- member and observed-member counts;
- arithmetic equal-weight daily member return in percentage points;
- summed observed member amount in CNY;
- observed-member coverage;
- source, source version, membership source version and publication time.

The publisher requires at least 80 sectors for every newly published session
and at least 95% observed coverage across active THS L2 members. Missing,
ambiguous or incomplete input fails publication; it is never converted to a
neutral sector day. Existing complete dates are immutable and repeated runs are
idempotent.

## Timing and ownership

The weekday Platform job starts at 17:40 and waits for the exact-date
`stock_daily` and `market_breadth_daily` facts. Under normal publication it is
ready before 18:00. If core market facts are late, publication follows their
actual readiness instead of substituting a previous day.

The late `ths_board_daily` publisher remains an independent vendor-index fact
and may be used for audit or research. It is not a fallback for this contract
and no longer blocks V4 market cognition.

## Compatibility

A change to taxonomy, weighting, amount unit, membership validity semantics or
daily-return calculation requires a new source/schema version. Consumers must
read the exact requested date and treat missing rows as unavailable.
