# B2 Supplemental Capital and Sector Facts v1

> Schemas: `supplemental-market-facts.v1`,
> `stock-capital-daily-facts.v1`, `sector-membership-history.v1`.

## Scope

This contract adds neutral, read-only facts needed for historical capital and
sector research. It does not change Discovery, Setup, Timeline, Evidence,
Understanding, score, or action semantics.

The supplemental database is independent from the transitional
`market_data.db` publisher. This prevents the temporary V3 daily bridge from
silently deleting or rewriting independently sourced facts.

## StockCapitalDailyFactV1

`stock_capital_daily` stores a vendor-reported stock net amount, float market
capitalization, and explicit units, source, source version, and fetch timestamp.

The Tushare adapter stores both numeric inputs in `CNY_10K`. The public-data
adapter stores both in `CNY`. The neutral ratio is defined only when the units
match and float market capitalization is positive:

```text
vendor_net_amount / float_market_cap
```

`vendor_net_amount` is a vendor classification. It must not be renamed or
interpreted as institutional buying, exchange-confirmed capital, or smart
money. Missing values remain unknown. Turnover or traded amount may not replace
this field.

### Public-data unit normalization

The no-token adapter uses Eastmoney individual capital-flow history over HTTPS
for the numerator and BaoStock unadjusted daily bars for the denominator.
Source version `eastmoney-https-moneyflow+baostock-daily.v3` fails closed when
the authenticated transport is unavailable. It does not downgrade to
plaintext HTTP. Every published row is also cross-checked against independently
fetched BaoStock turnover before publication.
BaoStock fields are declared and normalized at the source boundary:

- `volume`: `SHARE`, never lots;
- `amount`: `CNY`;
- `turn`: `PERCENT`, where `1.0` means one percent.

A daily row with missing or zero volume, amount, or turnover is treated as an
unobservable trading day for this fact and is not published. It remains a
coverage miss; it is never converted to zero capital flow. Negative numeric
inputs remain invalid and block atomic publication.

Float market capitalization is derived as:

```text
float_shares = volume_shares / (turnover_percent / 100)
float_market_cap_cny = float_shares * unadjusted_close_cny
```

Every input row must satisfy:

```text
0.5 <= amount_cny / (volume_shares * close_cny) <= 1.5
```

This is an explicit lot/share guard. If a source value measured in lots is
mislabeled as shares, the ratio is approximately `100` and the complete atomic
publication is blocked. Raw units are persisted in `supplemental_metadata`; an
absent or unsupported unit declaration also blocks publication.

For an Eastmoney row whose absolute reported net-flow ratio is at least
`0.1%`, the adapter also requires:

```text
0.9 <=
  (abs(vendor_net_amount) / (abs(vendor_net_ratio_percent) / 100))
  / baostock_daily_amount_cny
<= 1.1
```

Absolute vendor net flow may not exceed BaoStock daily turnover by more than
one percent. These gates independently reject yuan/ten-thousand-yuan
mismatches.

The endpoint is rate limited. The adapter uses finite retry, proxy/direct
fallback, and a fixed delay after each real response. A per-stock transport
failure may be recorded as explicit missing only when a fixed control stock
returns a valid non-empty response at the same time. If the control also
fails, the whole batch stops and resumes from immutable cache later. A
transport failure is not written into the immutable cache as a legitimate
empty vendor response, so a later run retries that stock.

Public-data coverage is evaluated over A-share codes supported consistently by
both historical adapters: Shenzhen main board (`00`), ChiNext (`30`), Shanghai
main board (`60`), and STAR (`68`). Beijing Exchange rows are not silently
remapped to Shenzhen and are outside this source version's coverage universe.

### Sina main-flow source version

Source version `sina-moneyflow-r0+baostock-daily.v2` is an independent
no-token alternative for the same supported coverage universe. It must be
backfilled as a complete source version and must not fill gaps inside an
Eastmoney series.

The Sina endpoint reports ratios as decimal fractions. The source boundary
normalizes them before applying the same BaoStock turnover checks:

```text
vendor_net_amount = r0_net_cny
vendor_net_ratio_percent = r0_ratio_decimal * 100
```

Sina defines this `r0` class as trades of at least CNY 1,000,000. This remains
a vendor classification and is stored as source
`sina.moneyflow.r0+baostock`; it is not exchange-confirmed institutional
activity. `netamount` and `ratioamount` represent total directional net flow,
not `r0`, and are retained separately in the immutable source cache. They may
not replace the published `r0` numerator.

The legacy Sina `turnover` response field has an unverified scale and is
retained only as raw cache metadata. It is never used as volume, shares,
turnover percentage, daily amount, or float-market-cap input. BaoStock remains
the sole denominator source, with `SHARE`, `CNY`, and `PERCENT` declarations
and the same lot/share guard described above.

For source-unit validation, a Sina row with an absolute `r0` ratio of at least
`0.1%` requires its ratio-implied turnover to be within `0.75` to `1.25` of
independent BaoStock turnover. This wider source-specific range preserves the
unit guard while allowing for the vendors' different treatment of neutral
transactions. It does not alter the Eastmoney `0.9` to `1.1` range. A Sina
`r0` or total-flow ratio outside `[-100%, 100%]` is retained in raw cache with
status `INVALID_VENDOR_RATIO`, excluded from publication, and counted as
missing against the frozen coverage gate.

A Sina row outside the 0.75-to-1.25 cross-source consistency range is likewise
excluded and counted as missing; the range is not widened to rescue it.
Systematic source or unit failure therefore still blocks the batch through the
frozen 0.98 coverage gate. Eastmoney retains fail-fast behavior for the same
check.

## SectorMembershipFactV1

`sector_membership_history` stores level-2 membership with inclusive
`valid_from` and optional `valid_to_exclusive`. The source `out_date` is treated
as the effective exclusion date and preserved as the exclusive boundary.
`read_as_of()` returns only members active on the requested date.

The public-data adapter selects only CNInfo classification standard `008003`
(申银万国), maps `行业次类` to level 2, and constructs intervals from successive
change dates. Other classification standards returned in the same response are
not mixed into this taxonomy.

CNInfo membership may be published independently when the capital-flow
provider is unavailable. The membership-only writer validates the complete
requested market universe, atomically replaces the complete membership
snapshot, and leaves `stock_capital_daily` unchanged. It publishes source
version `akshare.cninfo-sw-l2.v1`; it must not inherit a combined capital-source
label.

CNInfo can represent a stock with no history as an empty response. The adapter
accepts that state only after the finite retry policy is exhausted, caches it
as an explicit empty response, and counts it as uncovered. Empty responses
therefore cannot be used to bypass the frozen `0.99` coverage gate.

For a given stock and sector level, more than one active sector on the same
date is ambiguous and blocks the read. A current membership snapshot without
validity intervals is not compatible with this contract.

## SectorStrengthDailyFactV1

Sector strength is recalculated from PIT-valid sector membership for the
requested date and same-date raw `stock_daily.pct_chg`.

The result contains member count, observed member count, advancing and
declining counts, median percentage change, advancing ratio, and coverage. It
contains no score, label, ranking, recommendation, or threshold decision.

## Writers and quality gates

All backfills are atomic. They validate the complete requested range before
replacing the owned fact table or tables.

- capital coverage must be at least `0.98`;
- PIT membership coverage must be at least `0.99`;
- stock/date primary keys must be unique;
- units must be a matched pair of `CNY` or `CNY_10K`;
- public-data volume, amount, and turnover units must be `SHARE`, `CNY`, and
  `PERCENT`;
- the public-data amount/volume/close consistency gate must pass;
- overlapping L2 validity intervals block publication;
- source and source version are mandatory.

The public-data command requires a persistent, caller-owned batch cache.
Per-stock source responses are written immutably. A failed run can resume from
the same cache, but a cache must not be reused for a different date range or
source version. Finite retries handle transient errors; exhausting retries
leaves the published database unchanged.

Date-range-dependent BaoStock daily and CNInfo membership payloads include the
requested range in their cache identity. A narrower cached response therefore
cannot satisfy a later wider request.

Tushare membership intervals are intersected with the requested backfill
window before publication. The stored metadata records
`membership_available_from` and `membership_available_through`; reads outside
that validated window cannot observe an open-ended upstream interval.

The Sina batch is serial and applies at least a one-second delay plus random
jitter after each successful network response. An empty or malformed response
is retried and is never cached as a successful stock response.

The legacy board migration copies only `ths_board_daily`. It intentionally does
not copy V3 `sector_health_daily`, scores, labels, or application output.

## Holdout discipline

Ingestion may store raw rows for a sealed holdout interval. Before an explicit
research unlock, operators and research code may inspect only schema, source
version, hashes, and `available / unavailable / immature` state. They may not
inspect holdout row counts, factor hits, load, outcomes, or descriptive
statistics.

## Compatibility

- Exact historical reads remain valid when newer facts are appended.
- A change in provider, units, membership taxonomy, or validity semantics
  requires a new source/schema version.
- Consumers must treat `missing` and `blocked` as unknown, never false or zero.
