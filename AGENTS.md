# Repository Boundary

> 📍 **系统身份地图（唯一真源）：`/Users/y-plus/.hermes/YIFEI_MAP.md`** —— 三仓路径 / 数据路径 / 核心 skill 定位，先 reread 该文件再动。

This repository owns shared market facts and neutral capabilities.

- Do not import `yifei_v3`, `yifei_v4`, or any future application package.
- Do not introduce Strategy, Candidate, Setup, Pattern, Maturity, score, recommendation, or application-state semantics.
- Public contracts must be versioned and define `as_of`, source version, missing/degraded semantics, and compatibility behavior.
- Add contract tests before migrating a consumer.
- Keep market-data writers inside Platform; applications consume facts read-only.
