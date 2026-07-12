# Yifei Platform

Shared Platform owns market facts and versioned neutral capabilities used by Yifei applications.

```text
Applications -> Versioned Shared Platform

Forbidden:
Application -> Application
Shared Platform -> Application
```

Platform may contain calendar, market-data access, quality/readiness, artifact protocol, notification transport, outcome calculation, and runtime primitives. It must not contain Strategy, Candidate, Setup, Pattern, Maturity, recommendation, or application state semantics.

The initial engineering sequence is:

```text
A0 golden contract fixtures
-> A1 TradingCalendarV1 + MarketDataReaderV1
-> A2 DataQualitySnapshotV1 + ReadinessMarkerV1
-> A3 ArtifactEnvelopeV1 + OutcomeCalculatorV1
```

Run the repository checks with:

```bash
python3 -m unittest discover -s tests -v
```
