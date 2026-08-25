# C4 Platform Market Observation Facts V1

## Purpose

Platform publishes neutral market facts consumed by V4 Market Observation and
Attribution. These facts describe what happened; they do not change Discovery,
Setup, Decision, R1-R3, ranking, or recommendation.

## Contracts

`index_daily.v1` contains one CSI 300 (`000300.SH`) row per exchange session:
OHLC, prior close, volume, amount, daily return, 20-session close return, and
10-session annualized realized volatility calculated from log returns.

`market_breadth_daily.v1` contains one row per exchange session:

- advance, decline, and flat counts;
- valid return count and advance share;
- equal-weight daily return;
- shares returning at least +3% and at most -8%;
- share above the inclusive 20-session simple moving average and its eligible
  stock count;
- total positive traded amount and its ratio to the prior 20-session median.

Rows with missing/non-positive close or prior close do not enter return
denominators. MA20 requires 20 positive closes for the same stock. Missing
values remain `NULL`; they are never replaced with zero.

## Ownership and publication

Historical CSI 300 rows may be copied once from an explicitly supplied legacy
database. This is migration provenance, not a production dependency. Historical
breadth is deterministically derived from Platform `stock_daily`.

After migration, `yifei-platform-publish-daily-market` is the only Writer. It
publishes stock, breadth, and any available CSI 300 row in the same temporary
database replacement. It refuses to run until breadth history reaches the
current latest stock session, so a missed migration cannot silently create a
one-day partial history. A missing index row degrades market observation
quality but does not block migration or `v4-market-core`, whose required
dataset remains `stock_daily`.

## Consumers and constraints

Consumers are V4 Market Observation and Attribution. V4 reads these tables
read-only and may produce contextual display labels. Platform never stores
`risk_on`, `risk_off`, quadrant labels, scores, or application state.
