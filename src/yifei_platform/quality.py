from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from enum import Enum
import hashlib
import json
from typing import Iterable


class QualityStatus(str, Enum):
    OK = "ok"
    DEGRADED = "degraded"
    MISSING = "missing"
    STALE = "stale"
    BLOCKED = "blocked"


@dataclass(frozen=True)
class DatasetQualityV1:
    dataset: str
    status: QualityStatus
    observed_as_of: str | None
    source_version: str
    coverage: float | None = None
    freshness_lag_sessions: int | None = None
    reason_codes: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not self.dataset.strip():
            raise ValueError("dataset is required")
        if not self.source_version.strip():
            raise ValueError("source_version is required")
        if self.observed_as_of is not None:
            date.fromisoformat(self.observed_as_of)
        if self.coverage is not None and not 0.0 <= self.coverage <= 1.0:
            raise ValueError("coverage must be between 0 and 1")
        if self.freshness_lag_sessions is not None and self.freshness_lag_sessions < 0:
            raise ValueError("freshness_lag_sessions cannot be negative")

    def as_dict(self) -> dict[str, object]:
        return {
            "dataset": self.dataset,
            "status": self.status.value,
            "observed_as_of": self.observed_as_of,
            "source_version": self.source_version,
            "coverage": self.coverage,
            "freshness_lag_sessions": self.freshness_lag_sessions,
            "reason_codes": list(self.reason_codes),
        }


@dataclass(frozen=True)
class DataQualitySnapshotV1:
    snapshot_id: str
    as_of: str
    observed_at: str
    producer_version: str
    datasets: tuple[DatasetQualityV1, ...]
    schema_version: str = "data-quality-snapshot.v1"

    @classmethod
    def create(
        cls,
        *,
        as_of: str,
        observed_at: str,
        producer_version: str,
        datasets: Iterable[DatasetQualityV1],
    ) -> DataQualitySnapshotV1:
        date.fromisoformat(as_of)
        _validate_timestamp(observed_at, "observed_at")
        if not producer_version.strip():
            raise ValueError("producer_version is required")
        normalized = tuple(sorted(datasets, key=lambda item: item.dataset))
        if not normalized:
            raise ValueError("at least one dataset quality item is required")
        names = [item.dataset for item in normalized]
        if len(names) != len(set(names)):
            raise ValueError("dataset quality items must be unique")
        payload = cls._identity_payload(
            as_of=as_of,
            observed_at=observed_at,
            producer_version=producer_version,
            datasets=normalized,
        )
        return cls(
            snapshot_id=_fingerprint(payload),
            as_of=as_of,
            observed_at=observed_at,
            producer_version=producer_version,
            datasets=normalized,
        )

    def as_dict(self) -> dict[str, object]:
        return {"snapshot_id": self.snapshot_id, **self.identity_payload()}

    def identity_payload(self) -> dict[str, object]:
        return self._identity_payload(
            as_of=self.as_of,
            observed_at=self.observed_at,
            producer_version=self.producer_version,
            datasets=self.datasets,
        )

    def dataset(self, name: str) -> DatasetQualityV1 | None:
        return next((item for item in self.datasets if item.dataset == name), None)

    @staticmethod
    def _identity_payload(
        *,
        as_of: str,
        observed_at: str,
        producer_version: str,
        datasets: tuple[DatasetQualityV1, ...],
    ) -> dict[str, object]:
        return {
            "schema_version": "data-quality-snapshot.v1",
            "as_of": as_of,
            "observed_at": observed_at,
            "producer_version": producer_version,
            "datasets": [item.as_dict() for item in datasets],
        }


def _fingerprint(payload: dict[str, object]) -> str:
    canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _validate_timestamp(value: str, field: str) -> None:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.utcoffset() is None:
        raise ValueError(f"{field} must include a timezone")
