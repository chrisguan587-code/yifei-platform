# C3 Platform Daily Market Publisher V1

## Purpose

Platform owns the neutral A-share daily market snapshot after the V3
retirement. The publisher fetches one exact post-close session, appends it to
the existing Platform database, validates the complete candidate database, and
atomically advances `market_data.db`.

The direct consumers are V4 and the lightweight Shortline companion. Neither
consumer may import the publisher or a vendor client; they read only the
versioned Platform database and the `v4-market-core` readiness marker.

## Source and units

V1 first uses the public Sina A-share spot snapshot exposed by AkShare. If that
whole-market request fails, it makes one bounded fallback to Tencent quotes for
the prior session's known stock codes. The fallback cannot discover a stock
first listed today, so readiness explicitly records
`universe_discovery = degraded / prior_session_universe_only`; it is not
reported as a complete universe snapshot. The publisher normalizes vendor
fields into the existing `stock_daily` contract:

- `volume`: shares;
- `amount`: CNY;
- `pct_chg`: percent;
- Tencent A-share `volume` lots are converted to shares and its `amount` in ten
  thousand CNY is converted to CNY;
- `turnover`: missing (`NULL`) until a separately validated Platform turnover
  fact is available;
- `is_st`: derived only from the exact-date stock name.

The publisher does not import V3 code, read a V3 database, or read V3 health
artifacts.

## Publication gate

Before replacement, the candidate must satisfy all of the following:

- the requested date and publication timestamp are valid;
- the target exists and is a valid Platform market database;
- the requested date is not older than the current latest session;
- normalized identities are unique `(stock_code, trade_date)` pairs;
- every normalized row has a six-digit code, non-negative volume and amount,
  and internally valid OHLC values; an explicit zero-close row is accepted
  only when preclose is positive and both volume and amount are zero (suspended
  quote), never as a traded price;
- the exact-date row count is at least 5,000;
- at least 95% of the prior session's stock codes are present;
- SQLite integrity, table/column contract, history row count, session count,
  and date bounds all pass after the append.

The row-count and prior-session coverage thresholds are V1 data-health gates,
not trading rules.

## Atomicity and retries

The writer holds the existing Platform publication lock. It builds a temporary
database beside the target and uses `os.replace` only after all checks pass.
Any fetch, normalization, validation, or write failure leaves the current
database and readiness history unchanged.

The first publication for a date freezes its normalized rows and
`published_at`. A same-day retry is accepted only when the normalized rows are
identical. Different same-day content requires a future explicit correction
contract; V1 never silently rewrites it.

## Readiness and failure semantics

After successful replacement, Platform publishes immutable
`v4-market-core` readiness with:

- `stock_daily = ok`, exact-date, with measured prior-session coverage;
- `turnover = degraded`, reason `turnover_not_in_daily_snapshot`;
- `universe_discovery = ok` for Sina, or explicit degraded for the bounded
  prior-universe Tencent fallback.

Only `stock_daily` is required for this bundle. A failed run publishes no ready
marker. Missing turnover remains visible and cannot be converted to zero or a
neutral trading interpretation.

## Explicit non-goals

This publisher does not produce signals, rankings, eligibility decisions,
market interpretations, board quadrants, outcomes, or attribution. It does not
modify V4 or Shortline state.
