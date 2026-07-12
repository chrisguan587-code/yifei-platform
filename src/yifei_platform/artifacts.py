from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
import hashlib
import json
import os
from pathlib import Path
import re
import tempfile
from typing import Callable, Mapping, Sequence

from .quality import _validate_timestamp


class ArtifactConflictError(RuntimeError):
    """Raised when immutable artifact history would be overwritten."""


class ArtifactIntegrityError(RuntimeError):
    """Raised when a persisted artifact does not match its envelope identity."""


@dataclass(frozen=True)
class ArtifactEnvelopeV1:
    artifact_id: str
    artifact_type: str
    producer: str
    producer_version: str
    payload_schema: str
    payload_schema_version: str
    as_of: str
    created_at: str
    source_refs: tuple[str, ...]
    payload_checksum: str
    _payload_json: str = field(repr=False)
    schema_version: str = "artifact-envelope.v1"

    @classmethod
    def create(
        cls,
        *,
        artifact_type: str,
        producer: str,
        producer_version: str,
        payload_schema: str,
        payload_schema_version: str,
        as_of: str,
        created_at: str,
        source_refs: Sequence[str],
        payload: Mapping[str, object],
    ) -> ArtifactEnvelopeV1:
        _validate_name(artifact_type, "artifact_type", allow_slash=True)
        _validate_name(producer, "producer")
        _validate_name(payload_schema, "payload_schema")
        for field, value in (
            ("producer_version", producer_version),
            ("payload_schema_version", payload_schema_version),
        ):
            if not value.strip():
                raise ValueError(f"{field} is required")
        date.fromisoformat(as_of)
        _validate_timestamp(created_at, "created_at")
        normalized_refs = tuple(sorted(set(source_refs)))
        payload_json = _canonical_json(dict(payload))
        payload_checksum = hashlib.sha256(payload_json.encode("utf-8")).hexdigest()
        identity = {
            "schema_version": "artifact-envelope.v1",
            "artifact_type": artifact_type,
            "producer": producer,
            "producer_version": producer_version,
            "payload_schema": payload_schema,
            "payload_schema_version": payload_schema_version,
            "as_of": as_of,
            "created_at": created_at,
            "source_refs": list(normalized_refs),
            "payload_checksum": payload_checksum,
        }
        return cls(
            artifact_id=_sha256(identity),
            artifact_type=artifact_type,
            producer=producer,
            producer_version=producer_version,
            payload_schema=payload_schema,
            payload_schema_version=payload_schema_version,
            as_of=as_of,
            created_at=created_at,
            source_refs=normalized_refs,
            payload_checksum=payload_checksum,
            _payload_json=payload_json,
        )

    @property
    def payload(self) -> dict[str, object]:
        return json.loads(self._payload_json)

    def as_dict(self) -> dict[str, object]:
        return {
            "artifact_id": self.artifact_id,
            "schema_version": self.schema_version,
            "artifact_type": self.artifact_type,
            "producer": self.producer,
            "producer_version": self.producer_version,
            "payload_schema": self.payload_schema,
            "payload_schema_version": self.payload_schema_version,
            "as_of": self.as_of,
            "created_at": self.created_at,
            "source_refs": list(self.source_refs),
            "payload_checksum": self.payload_checksum,
            "payload": self.payload,
        }


@dataclass(frozen=True)
class ArtifactReceiptV1:
    artifact_id: str
    artifact_ref: str
    index_ref: str


class ArtifactStoreV1:
    """Content-addressed immutable JSON artifact and index storage."""

    def __init__(self, root: Path, *, publish_file: Callable[[str, str], None] = os.link):
        self._root = root
        self._publish_file = publish_file

    def write(self, envelope: ArtifactEnvelopeV1) -> ArtifactReceiptV1:
        artifact_path = self._artifact_path(envelope)
        artifact_ref = artifact_path.relative_to(self._root).as_posix()
        index_path = self._index_path(envelope)
        index_ref = index_path.relative_to(self._root).as_posix()
        index_payload = {
            "schema_version": "artifact-index-entry.v1",
            "artifact_id": envelope.artifact_id,
            "artifact_type": envelope.artifact_type,
            "producer": envelope.producer,
            "producer_version": envelope.producer_version,
            "payload_schema": envelope.payload_schema,
            "payload_schema_version": envelope.payload_schema_version,
            "as_of": envelope.as_of,
            "created_at": envelope.created_at,
            "artifact_ref": artifact_ref,
            "payload_checksum": envelope.payload_checksum,
        }
        self._write_immutable_json(artifact_path, envelope.as_dict())
        self._write_immutable_json(index_path, index_payload)
        return ArtifactReceiptV1(envelope.artifact_id, artifact_ref, index_ref)

    def read(self, artifact_ref: str) -> ArtifactEnvelopeV1:
        path = (self._root / artifact_ref).resolve()
        if not path.is_relative_to(self._root.resolve()):
            raise ArtifactIntegrityError("artifact reference escapes storage root")
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(payload, dict) or payload.get("schema_version") != "artifact-envelope.v1":
                raise ArtifactIntegrityError("invalid artifact envelope schema")
            envelope = ArtifactEnvelopeV1.create(
                artifact_type=str(payload["artifact_type"]),
                producer=str(payload["producer"]),
                producer_version=str(payload["producer_version"]),
                payload_schema=str(payload["payload_schema"]),
                payload_schema_version=str(payload["payload_schema_version"]),
                as_of=str(payload["as_of"]),
                created_at=str(payload["created_at"]),
                source_refs=tuple(str(item) for item in payload["source_refs"]),
                payload=payload["payload"],
            )
        except ArtifactIntegrityError:
            raise
        except (OSError, json.JSONDecodeError, KeyError, TypeError, ValueError) as exc:
            raise ArtifactIntegrityError("cannot validate artifact envelope") from exc
        if envelope.artifact_id != payload.get("artifact_id"):
            raise ArtifactIntegrityError("artifact identity mismatch")
        if envelope.payload_checksum != payload.get("payload_checksum"):
            raise ArtifactIntegrityError("artifact payload checksum mismatch")
        return envelope

    def _artifact_path(self, envelope: ArtifactEnvelopeV1) -> Path:
        return (
            self._root
            / "artifacts"
            / envelope.producer
            / Path(*envelope.artifact_type.split("/"))
            / envelope.as_of
            / f"{envelope.artifact_id}.json"
        )

    def _index_path(self, envelope: ArtifactEnvelopeV1) -> Path:
        return self._root / "indexes" / envelope.as_of / f"{envelope.artifact_id}.json"

    def _write_immutable_json(self, path: Path, payload: dict[str, object]) -> None:
        encoded = _canonical_json(payload) + "\n"
        if path.exists():
            if path.read_text(encoding="utf-8") == encoded:
                return
            raise ArtifactConflictError(f"immutable artifact record already exists: {path}")
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
                raise ArtifactConflictError(f"immutable artifact record already exists: {path}")
        finally:
            Path(temporary_name).unlink(missing_ok=True)


def _validate_name(value: str, field: str, *, allow_slash: bool = False) -> None:
    pattern = r"[a-z0-9][a-z0-9_.-]*(/[a-z0-9][a-z0-9_.-]*)*" if allow_slash else r"[a-z0-9][a-z0-9_.-]*"
    if not re.fullmatch(pattern, value.strip()):
        raise ValueError(f"invalid {field}")


def _canonical_json(payload: object) -> str:
    try:
        return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    except (TypeError, ValueError) as exc:
        raise ValueError("artifact payload must be JSON serializable") from exc


def _sha256(payload: object) -> str:
    return hashlib.sha256(_canonical_json(payload).encode("utf-8")).hexdigest()
