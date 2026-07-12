from __future__ import annotations

from dataclasses import dataclass
from datetime import date
import json
import os
from pathlib import Path
import re
import tempfile
from typing import Callable

from .quality import DataQualitySnapshotV1, QualityStatus, _fingerprint, _validate_timestamp


class ReadinessConflictError(RuntimeError):
    """Raised when immutable readiness history would be overwritten."""


class ReadinessIntegrityError(RuntimeError):
    """Raised when a persisted marker or referenced snapshot is invalid."""


class DataNotReadyError(ValueError):
    """Raised when strict bundle readiness requirements are not met."""


@dataclass(frozen=True)
class ReadinessMarkerV1:
    marker_id: str
    bundle: str
    as_of: str
    published_at: str
    producer_version: str
    required_datasets: tuple[str, ...]
    quality_snapshot_ref: str
    quality_snapshot_id: str
    status: str = "ready"
    schema_version: str = "readiness-marker.v1"

    def as_dict(self) -> dict[str, object]:
        return {"marker_id": self.marker_id, **self.identity_payload()}

    def identity_payload(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "bundle": self.bundle,
            "as_of": self.as_of,
            "published_at": self.published_at,
            "producer_version": self.producer_version,
            "required_datasets": list(self.required_datasets),
            "quality_snapshot_ref": self.quality_snapshot_ref,
            "quality_snapshot_id": self.quality_snapshot_id,
            "status": self.status,
        }


class ReadinessStoreV1:
    """Persist immutable quality snapshots and atomically publish ready markers."""

    def __init__(self, root: Path, *, publish_file: Callable[[str, str], None] = os.link):
        self._root = root
        self._publish_file = publish_file

    def publish_ready(
        self,
        *,
        bundle: str,
        snapshot: DataQualitySnapshotV1,
        required_datasets: tuple[str, ...],
        published_at: str,
        producer_version: str,
    ) -> ReadinessMarkerV1:
        normalized_bundle = _validate_bundle(bundle)
        date.fromisoformat(snapshot.as_of)
        _validate_timestamp(published_at, "published_at")
        if not producer_version.strip():
            raise ValueError("producer_version is required")
        required = tuple(sorted(set(required_datasets)))
        if not required:
            raise ValueError("required_datasets cannot be empty")
        self._ensure_ready(snapshot, required)

        snapshot_path = self._snapshot_path(snapshot)
        snapshot_ref = snapshot_path.relative_to(self._root).as_posix()
        marker_payload = {
            "schema_version": "readiness-marker.v1",
            "bundle": normalized_bundle,
            "as_of": snapshot.as_of,
            "published_at": published_at,
            "producer_version": producer_version,
            "required_datasets": list(required),
            "quality_snapshot_ref": snapshot_ref,
            "quality_snapshot_id": snapshot.snapshot_id,
            "status": "ready",
        }
        marker = ReadinessMarkerV1(
            marker_id=_fingerprint(marker_payload),
            bundle=normalized_bundle,
            as_of=snapshot.as_of,
            published_at=published_at,
            producer_version=producer_version,
            required_datasets=required,
            quality_snapshot_ref=snapshot_ref,
            quality_snapshot_id=snapshot.snapshot_id,
        )

        self._write_immutable_json(snapshot_path, snapshot.as_dict())
        self._write_immutable_json(self._marker_path(normalized_bundle, snapshot.as_of), marker.as_dict())
        return marker

    def read_ready(self, *, bundle: str, as_of: str) -> ReadinessMarkerV1 | None:
        marker_path = self._marker_path(_validate_bundle(bundle), date.fromisoformat(as_of).isoformat())
        if not marker_path.exists():
            return None
        marker_data = self._read_json(marker_path)
        marker_id = str(marker_data.pop("marker_id", ""))
        if marker_id != _fingerprint(marker_data):
            raise ReadinessIntegrityError("readiness marker fingerprint mismatch")
        snapshot_ref = str(marker_data["quality_snapshot_ref"])
        snapshot_path = (self._root / snapshot_ref).resolve()
        if not snapshot_path.is_relative_to(self._root.resolve()):
            raise ReadinessIntegrityError("quality snapshot reference escapes storage root")
        snapshot_data = self._read_json(snapshot_path)
        snapshot_id = str(snapshot_data.pop("snapshot_id", ""))
        if snapshot_id != marker_data["quality_snapshot_id"] or snapshot_id != _fingerprint(snapshot_data):
            raise ReadinessIntegrityError("quality snapshot fingerprint mismatch")
        return ReadinessMarkerV1(
            marker_id=marker_id,
            bundle=str(marker_data["bundle"]),
            as_of=str(marker_data["as_of"]),
            published_at=str(marker_data["published_at"]),
            producer_version=str(marker_data["producer_version"]),
            required_datasets=tuple(str(item) for item in marker_data["required_datasets"]),
            quality_snapshot_ref=snapshot_ref,
            quality_snapshot_id=str(marker_data["quality_snapshot_id"]),
            status=str(marker_data["status"]),
            schema_version=str(marker_data["schema_version"]),
        )

    @staticmethod
    def _ensure_ready(snapshot: DataQualitySnapshotV1, required: tuple[str, ...]) -> None:
        failures: list[str] = []
        for name in required:
            item = snapshot.dataset(name)
            if item is None:
                failures.append(f"{name}:absent")
            elif item.status is not QualityStatus.OK:
                failures.append(f"{name}:{item.status.value}")
            elif item.observed_as_of != snapshot.as_of:
                failures.append(f"{name}:as_of_mismatch")
        if failures:
            raise DataNotReadyError("required datasets are not ready: " + ", ".join(failures))

    def _snapshot_path(self, snapshot: DataQualitySnapshotV1) -> Path:
        return self._root / "quality" / snapshot.as_of / f"{snapshot.snapshot_id}.json"

    def _marker_path(self, bundle: str, as_of: str) -> Path:
        return self._root / "readiness" / bundle / f"{as_of}.json"

    def _write_immutable_json(self, path: Path, payload: dict[str, object]) -> None:
        encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n"
        if path.exists():
            if path.read_text(encoding="utf-8") == encoded:
                return
            raise ReadinessConflictError(f"immutable record already exists: {path}")
        path.parent.mkdir(parents=True, exist_ok=True)
        descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                handle.write(encoded)
                handle.flush()
                os.fsync(handle.fileno())
            try:
                self._publish_file(temporary_name, str(path))
            except FileExistsError:
                if path.read_text(encoding="utf-8") == encoded:
                    return
                raise ReadinessConflictError(f"immutable record already exists: {path}")
        finally:
            Path(temporary_name).unlink(missing_ok=True)

    @staticmethod
    def _read_json(path: Path) -> dict[str, object]:
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ReadinessIntegrityError(f"cannot read {path}") from exc
        if not isinstance(payload, dict):
            raise ReadinessIntegrityError(f"invalid JSON object: {path}")
        return payload


def _validate_bundle(value: str) -> str:
    normalized = value.strip()
    if not re.fullmatch(r"[a-z0-9][a-z0-9_.-]*", normalized):
        raise ValueError("bundle must match [a-z0-9][a-z0-9_.-]*")
    return normalized
