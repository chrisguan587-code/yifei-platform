# A3 Outcome Price Lineage Contract v2

> Status: implemented candidate, pending release and consumer compatibility review.
> Rule contract: `price-lineage-guard.candidate.v2`.

## Purpose

PriceLineageGuardV2 determines whether a raw-price window is internally
comparable. It does not modify market facts, infer a corporate-action type, or
make an application decision.

## Reference Selection

For each session after the observation session:

```text
reported preclose > 0
    -> reference = reported preclose
    -> reference_source = reported_preclose

reported preclose missing or <= 0
and current close > 0
and pct_chg is available
and 1 + pct_chg / 100 > 0
    -> reference = current close / (1 + pct_chg / 100)
    -> reference_source = implied_from_pct_chg

otherwise
    -> price_lineage_input_missing
```

The reference is compared with the preceding published session's raw close.
Relative difference above `0.1%` produces
`corporate_action_or_price_lineage_discontinuity`.

V2 never replaces a positive reported preclose with the implied value. A
disagreement in the positive reported value remains unavailable even when the
percentage-change evidence would pass.

## Output Evidence

`OutcomeResultV1.price_lineage_sources` is an additive optional field containing
the sorted distinct reference sources actually used before `outcome_as_of`.
Pending windows must not expose sources or reasons from future sessions.

## Compatibility

- PriceLineageGuardV1 behavior and rule version are unchanged.
- Callers that do not opt into a guard retain existing behavior.
- Original `preclose`, `close`, and `pct_chg` facts remain unchanged.
- A failed or missing fallback remains unavailable; V2 never fabricates a
  return.
- Applications must explicitly pin a Platform release containing V2 before use.
