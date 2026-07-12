# Repository Boundary

This repository owns shared market facts and neutral capabilities.

- Do not import `yifei_v3`, `yifei_v4`, or any future application package.
- Do not introduce Strategy, Candidate, Setup, Pattern, Maturity, score, recommendation, or application-state semantics.
- Public contracts must be versioned and define `as_of`, source version, missing/degraded semantics, and compatibility behavior.
- Add contract tests before migrating a consumer.
- Keep market-data writers inside Platform; applications consume facts read-only.
