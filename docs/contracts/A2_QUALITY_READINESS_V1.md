# A2 Data Quality and Readiness Contracts v1

> Status: implemented candidate, pending first producer/consumer compatibility review.
> Schema contracts: `data-quality-snapshot.v1` and `readiness-marker.v1`.

## Separation of Responsibility

```text
DataQualitySnapshot = what is known about dataset quality
ReadinessMarker     = strict proof that a named dataset bundle is consumable
Application policy  = what an application does after reading those facts
```

Platform does not encode a V3/V4 pipeline action in either contract.

## DataQualitySnapshotV1

A snapshot is immutable and content-addressed. Its identity includes `as_of`, observation time, producer version, and sorted dataset quality items.

Each dataset item records:

- Dataset name and source version.
- Quality status: `ok`, `degraded`, `missing`, `stale`, or `blocked`.
- Observed market date.
- Optional coverage in the closed interval `[0, 1]`.
- Optional non-negative freshness lag in trading sessions.
- Neutral reason codes.

Duplicate dataset names are invalid. Dataset order does not change snapshot identity.

## ReadinessMarkerV1

A marker is published only when every required dataset:

1. Exists in the referenced snapshot.
2. Has status `ok`.
3. Has `observed_as_of` equal to the snapshot `as_of`.

Degraded or missing quality is still persisted as a snapshot but cannot create strict readiness. An application that supports a degraded mode must read quality facts and make its own explicit decision; it must not reinterpret a strict ready marker.

## Publication Protocol

```text
validate strict requirements
-> atomically persist immutable quality snapshot
-> atomically persist immutable readiness marker
```

The marker references the snapshot path and fingerprint. Absence of a marker means not ready. If marker publication fails after snapshot persistence, the snapshot remains valid evidence but consumers cannot observe false readiness.

Publishing identical content is idempotent. Publishing different content to the same immutable marker path raises `ReadinessConflictError`.

## Integrity

Readers verify both marker and snapshot fingerprints. Missing, malformed, changed, or mismatched records raise `ReadinessIntegrityError`; they are never downgraded to ready.

Bundle names use lowercase alphanumeric characters plus `.`, `_`, and `-`. Marker paths are isolated by bundle and `as_of`.

## Compatibility

Within v1:

- Existing statuses and their meanings cannot change.
- Required readiness remains strict `ok + exact as_of`.
- New optional quality fields and reason codes may be added with backward-compatible defaults.
- Fingerprint inputs, publication order, and immutable conflict behavior cannot change.
- Allowing degraded data to publish a strict ready marker requires a new contract version.
