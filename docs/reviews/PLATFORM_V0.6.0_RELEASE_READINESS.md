# Platform v0.6.0 Release Readiness Review

> Review date: 2026-07-15
> Decision: approved for immutable Platform release; consumers remain pinned to
> their prior release until independent compatibility evidence passes.

## Scope

- Add `OutcomeRequestV1` and `OutcomeBatchResultV1`.
- Add `OutcomeCalculatorV1.calculate_many` under `outcome-batch.v1`.
- Reuse one point-in-time session fact index inside a batch call.
- Preserve scalar `OutcomeCalculatorV1.calculate` and PriceLineageGuard V1/V2.
- Publish package version `0.6.0`.

## Boundary

Batch Outcome is a neutral Platform capability. It does not select V4 samples,
read an application database, persist AttributionOutcome, aggregate results, or
infer a trading opportunity. Each batch request is evaluated by the existing
scalar calculator implementation.

The cache is batch-local and lazy. It cannot make a future session visible:
the scalar calculator still decides which sessions to request under the
explicit `outcome_as_of` cutoff. The cache retains at most 32 published
sessions, which covers the current T+8 consumer while bounding memory use.

## Evidence

- 57 source-tree tests passed.
- 57 sdist tests passed.
- Wheel build and isolated `0.6.0` installation passed.
- Batch contract test proves scalar data equality across two instruments,
  PriceLineageGuardV2 implied lineage, and a pending cutoff.
- The same test proves each required published session is read once per batch.
- Final-SHA Open Code Review completed. Three findings were reviewed and
  rejected: zero/non-positive prices remain unavailable under the frozen
  Outcome fact contract; a target beyond the published calendar remains
  pending when an explicit `outcome_as_of` is supplied; and redundant scalar
  `None` guards are retained to avoid unrelated scalar churn in this batch-only
  release.

## Consumer Gate

V4 may update its fixed dependency only after it proves on a new isolated
three-day diagnostic:

1. 1,104 stored Outcome records remain identical by sample/window payload.
2. Discovery, first-upgrade, Benchmark, Episode, and report results are unchanged.
3. Batch runtime is materially lower than the 17 minute 16 second scalar baseline.
4. V4 keeps sample selection and persistence outside Platform.

## Rollback

Published tags are immutable. Consumers can remain pinned to Platform v0.5.0.
Any defect requires a new Platform release; do not alter scalar V1 semantics or
retag v0.6.0.
