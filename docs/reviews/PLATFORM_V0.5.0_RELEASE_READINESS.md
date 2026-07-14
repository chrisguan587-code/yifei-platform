# Platform v0.5.0 Release Readiness Review

> Review date: 2026-07-14
> Decision: approved for fixed Platform release after final-SHA Open Code Review; V4 pin remains unchanged until its consumer gate passes.

## Scope

- Add opt-in `PriceLineageGuardV2` with rule version
  `price-lineage-guard.candidate.v2`.
- Preserve `PriceLineageGuardV1` behavior and public positional compatibility.
- When reported `preclose` is missing or non-positive, V2 may derive a neutral
  reference as `close / (1 + pct_chg / 100)`.
- Add `price_lineage_sources` as an optional trailing OutcomeResultV1 field.
- Stop price-lineage reads after an explicit `outcome_as_of` cutoff.

## Boundary

Platform still owns only neutral price-comparability and outcome semantics. V2
does not modify raw market facts, infer a corporate-action type, select a sample,
or make an application decision. A positive reported preclose is never replaced
by an implied value.

## Real-Data Diagnostic

The immutable V4 Replay market snapshot for `2026-04-01..2026-04-16` exposed a
source defect affecting Shanghai main-board rows from `2026-04-09`:

```text
reported preclose = 0 rows: 4683
implied reference agrees with previous close within 0.1%: 4676 (99.85%)
implied reference still disagrees: 7
```

V2 recovers only the 4676 internally consistent rows. The remaining seven rows
stay `corporate_action_or_price_lineage_discontinuity`; no return is fabricated.
Raw `preclose=0` records remain unchanged.

## Evidence

- Local quality gate: 56 source-tree tests passed.
- Final release gate: 56 source-tree tests, 56 sdist tests, wheel build, and
  isolated `0.5.0` install passed.
- Contract tests cover V1 zero-preclose behavior, V2 reported and implied
  sources, missing fallback inputs, implied discontinuity, positive-preclose
  precedence, PIT cutoff, and positional API compatibility.
- Open Code Review session `9b7dc9d5-8f0b-473f-a33e-f04f6fe20b98` found one
  additive-field positional compatibility issue. The field was moved after the
  existing `schema_version`, and a regression test was added.
- Final workspace review session `10d04b90-6933-4dc5-ae3b-64f0ba89fa46`
  reported no findings.
- Final-SHA Open Code Review evidence is recorded after the release commit.

## Consumer Gate

V4 must remain on Platform v0.4.0 until:

1. This release has an immutable commit and `v0.5.0` tag.
2. V4 pins that exact commit.
3. V4 persists `price_lineage_sources` in Outcome evidence.
4. V4 Episode uses V2 with current close and percentage-change inputs.
5. Three-day Outcome and Episode diagnostics retain the seven anomalous rows as
   unavailable and materially reduce false zero-preclose exclusions.

## Rollback

Keep V4 pinned to Platform v0.4.0. Published tags are immutable. Any defect in
V2 requires a new Platform release; do not retag or alter V1 behavior.
