# B1 Eligibility, Board, and Capital Facts v1

> Schemas: `eligibility-facts.v1`, `board-daily-facts.v1`, `sector-capital-facts.v1`.

## EligibilityPrimitiveV1

The primitive returns market segment, tri-state ST fact, tri-state delisting fact, and raw amount/volume/turnover. It never returns `eligible`, `liquidity_ok`, Setup qualification, score, or action.

Current `stock_daily` has no historical delisting field. Therefore `delisting_state` is explicitly `unknown`; absence of data is not interpreted as false. Segment interpretation requires a versioned market-rules identifier.

## BoardFactReaderV1

Reads exact historical rows from `ths_board_daily` through a read-only connection. It returns raw OHLC, volume, amount, and percentage change with source/schema versions. It does not return mainline, lifecycle, confidence, action, or position coefficients.

## BoardDailyPublisherV1

The board publisher runs after the authoritative `stock_daily` row for the same
market date is available. It synchronizes missing sessions from the AKShare THS
industry-board endpoint into `ths_board_daily`; it does not read V3 tables or
application output.

- each newly published market date requires at least 80 valid board rows;
- all missing sessions since the latest published board date are validated
  before a single atomic replacement of the shared supplemental database;
- a source failure or insufficient coverage leaves the previous database
  unchanged and is reported as unavailable, never as a neutral board day;
- `pct_chg` is deterministically calculated from adjacent source closes because
  the THS history endpoint does not return a daily percentage-change field.

The local production schedule runs at 21:10 Shanghai time, after the observed
THS daily-history publication window. A second invocation at 22:10 is the only
retry. Publication is idempotent: when the first invocation succeeded, the
second finds no missing session and performs no source fetch or database write.

## CapitalFactReaderV1

V1 reads sector-level facts from `sector_fund_flow_daily`: raw amount, change,
main inflow, breadth counts, lead-stock fields, and explicit amount/main-inflow
units. Units are passed through unchanged and must be defined by the source
version.

There is no audited public individual-stock capital table in the current database. V1 does not invent one from V3 confirmation, rankings, or candidate outputs.

### Sector flow daily publisher

`levistock.sector_em(sector_type="industry")` is the current-day upstream for
sector capital facts. It is not a historical backfill API. The publisher
therefore requires:

- `as_of` equals the Shanghai-local fetch date;
- fetch time is at or after `15:10 Asia/Shanghai`;
- `as_of` already exists in the Platform market database;
- at least 400 unique sector codes;
- `amount` and `main_inflow` are finite and explicitly normalized as `CNY`;
- `abs(main_inflow) <= amount`;
- breadth counts are nonnegative integers.

The 400-row floor is source-specific and was calibrated against the actual V3
`sector_em(industry)` series: recent complete snapshots contain 487–496 rows
and 2026-07-28 contains 496. Falling below 400 is therefore treated as a
truncated response, not as a change in the expected industry taxonomy.

The upstream `volume` field is copied only into the immutable raw snapshot and
is marked `unit_not_audited`. It is not written to the normalized fact table
and must not be interpreted as shares or lots.

Each current-day response is stored as an immutable raw snapshot before atomic
fact publication. Repeating identical content is idempotent. Different content
for an already snapshotted or published date is a conflict and cannot overwrite
the existing fact.

V3 `sector_fund_flow/{date}` files are a cache layer, not an independent source.
`get_sector_hot_plates()` contains hotspot and limit-up semantics and remains
outside this neutral fact contract.

## THS Annual Membership Snapshot v1

The 2026 research contract permits one fixed annual THS L2 membership snapshot.
It is imported once into `sector_membership_history` with
`sector_level=THS_L2`, `valid_from=2026-01-01`, a source version and fetch
time. The imported rows are independent of V3 at read time.

This is a deliberately coarse annual classification, not a claim of intrayear
PIT membership history. It is valid only for the frozen 2026 research window;
a future annual refresh must publish a new explicit version. Import fails unless
every mapped board name exactly exists in the published THS board taxonomy.

## Compatibility

- Exact historical `as_of` remains valid when newer facts exist.
- Optional physical fields map to `None`, never zero/neutral.
- New eligibility decisions or thresholds belong to applications.
- Adding score/action/state fields is forbidden in v1.
- Changing market prefix meaning, capital units, or source semantics requires a new source/rules version.
