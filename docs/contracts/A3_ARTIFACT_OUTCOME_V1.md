# A3 Artifact and Outcome Contracts v1

> Status: implemented candidate, pending first V3/V4 consumer compatibility review.
> Schemas: `artifact-envelope.v1`, `artifact-index-entry.v1`, and `outcome-calculator.v1`.

## ArtifactEnvelopeV1

Envelope metadata and application payload are separate. Platform records but does not interpret payload semantics.

Required identity includes producer and producer version, payload schema and schema version, exact `as_of`, timezone-aware creation time, source references, and payload checksum. Artifact identity is content-addressed from this metadata.

Payload is canonical JSON copied at creation. Later caller mutation cannot change the Envelope.

## ArtifactStoreV1

Artifacts and index entries are immutable JSON records:

```text
persist content-addressed artifact
-> persist content-addressed index entry
```

The index contains discovery metadata and an artifact reference, never the application payload. Identical retries are idempotent; different content at an existing path conflicts. If index publication fails, the artifact may remain but no false index is visible, and retry completes publication.

Artifact reads constrain references to the configured storage root and recompute both Envelope identity and payload checksum. Tampered or malformed content raises `ArtifactIntegrityError`.

## OutcomeCalculatorV1

The caller owns sample identity and selection. Calculator input is only instrument, exact observation session, windows, versioned Calendar/Market readers, and explicit price-basis version.

V1 definitions:

- Entry: observation-session close.
- T+N: target trading-session close return versus entry.
- MFE: maximum future daily high from T+1 through the largest published requested window versus entry.
- MAE: minimum future daily low over the same interval versus entry.
- Max drawdown: largest peak-to-subsequent-close decline, beginning with entry close. Close-based drawdown avoids unknowable intraday high/low ordering.

Observation-day high/low are excluded because the observation is captured at that day's close. Missing entry, target, intermediate OHLC, or unpublished sessions return explicit unavailable results; values are never filled with zero.

## Compatibility and Non-goals

- V1 does not select V3 CanonicalEvents or V4 Setups.
- V1 does not aggregate win rates or write application attribution state.
- `price_basis_version` must state raw/adjusted and corporate-action semantics supplied by the data producer. V1 does not silently transform prices.
- Metric formulas and observation timing cannot change within v1.
- Adding a new metric is backward compatible; changing an existing formula requires a new schema version.
