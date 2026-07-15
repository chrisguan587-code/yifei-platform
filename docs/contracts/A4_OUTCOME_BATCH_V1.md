# A4 Outcome Batch Contract v1

> Status: implemented in Platform v0.6.0, pending consumer compatibility review.
> Contract version: `outcome-batch.v1`.

## Purpose

`OutcomeCalculatorV1.calculate_many` evaluates caller-owned neutral Outcome
requests while reusing point-in-time market reads inside one batch. It changes
only read efficiency. Scalar and batch calculations share the existing
`OutcomeCalculatorV1.calculate` implementation.

## Input

Each immutable `OutcomeRequestV1` contains:

```text
instrument
observation_session
windows
```

The batch has one explicit `outcome_as_of` cutoff. Request order is preserved;
duplicate requests are permitted and produce duplicate ordered results.

## Output

`OutcomeBatchResultV1` contains:

```text
results: tuple[OutcomeResultV1, ...]
schema_version: outcome-batch.v1
```

Every result must be byte-for-byte data equivalent to calling scalar
`calculate` with the same request and cutoff.

## Read Boundary

- Market facts are indexed lazily by published session inside one batch call.
- The 32 most recently required sessions remain indexed. T+8 batches therefore
  read each required session at most once; unusually wide or disordered batches
  may reread an evicted session without changing result semantics.
- The cache is discarded when the batch call returns.
- No session after `outcome_as_of` may be read because of batching.
- Missing and blocked read results retain their scalar semantics.

## Non-goals

The batch API does not select samples, deduplicate application objects, persist
AttributionOutcome, aggregate returns, infer opportunities, or change price
lineage, T+N, MFE, MAE, or max-drawdown formulas.
