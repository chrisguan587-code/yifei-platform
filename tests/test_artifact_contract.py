from __future__ import annotations

import json
import os
from pathlib import Path
import tempfile
import unittest

from yifei_platform.artifacts import (
    ArtifactConflictError,
    ArtifactEnvelopeV1,
    ArtifactIntegrityError,
    ArtifactStoreV1,
)


class ArtifactContractTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.root = Path(self.tempdir.name)

    def tearDown(self) -> None:
        self.tempdir.cleanup()

    def test_envelope_separates_producer_payload_schema_and_payload(self) -> None:
        payload = {"items": [{"code": "000001"}], "state": "WATCHING"}
        envelope = self._envelope(payload)
        payload["state"] = "MUTATED"
        self.assertEqual("WATCHING", envelope.payload["state"])
        self.assertEqual("yifei-v4", envelope.producer)
        self.assertEqual("v4.daily-review", envelope.payload_schema)
        self.assertEqual(64, len(envelope.payload_checksum))
        self.assertEqual(64, len(envelope.artifact_id))

    def test_store_writes_content_addressed_artifact_and_index(self) -> None:
        envelope = self._envelope({"items": []})
        receipt = ArtifactStoreV1(self.root).write(envelope)
        artifact = json.loads((self.root / receipt.artifact_ref).read_text(encoding="utf-8"))
        index = json.loads((self.root / receipt.index_ref).read_text(encoding="utf-8"))
        self.assertEqual(envelope.artifact_id, artifact["artifact_id"])
        self.assertEqual(receipt.artifact_ref, index["artifact_ref"])
        self.assertNotIn("payload", index)
        self.assertEqual(envelope, ArtifactStoreV1(self.root).read(receipt.artifact_ref))

    def test_tampered_artifact_and_path_escape_are_rejected(self) -> None:
        store = ArtifactStoreV1(self.root)
        receipt = store.write(self._envelope({"items": []}))
        artifact_path = self.root / receipt.artifact_ref
        payload = json.loads(artifact_path.read_text(encoding="utf-8"))
        payload["payload"]["items"] = [{"code": "tampered"}]
        artifact_path.write_text(json.dumps(payload), encoding="utf-8")
        with self.assertRaises(ArtifactIntegrityError):
            store.read(receipt.artifact_ref)
        with self.assertRaises(ArtifactIntegrityError):
            store.read("../outside.json")

    def test_repeat_is_idempotent_and_concurrent_difference_conflicts(self) -> None:
        envelope = self._envelope({"items": []})
        store = ArtifactStoreV1(self.root)
        self.assertEqual(store.write(envelope), store.write(envelope))
        receipt = store.write(envelope)
        index_path = self.root / receipt.index_ref
        index_path.write_text("{}\n", encoding="utf-8")
        with self.assertRaises(ArtifactConflictError):
            store.write(envelope)

    def test_interrupted_index_publish_leaves_no_false_index(self) -> None:
        calls = 0

        def fail_second_publish(source: str, target: str) -> None:
            nonlocal calls
            calls += 1
            if calls == 2:
                raise OSError("simulated index failure")
            os.link(source, target)

        envelope = self._envelope({"items": []})
        store = ArtifactStoreV1(self.root, publish_file=fail_second_publish)
        with self.assertRaises(OSError):
            store.write(envelope)
        artifacts = list((self.root / "artifacts").rglob("*.json"))
        indexes = list((self.root / "indexes").rglob("*.json")) if (self.root / "indexes").exists() else []
        self.assertEqual(1, len(artifacts))
        self.assertEqual([], indexes)

    def test_names_timestamps_and_json_payload_are_validated(self) -> None:
        with self.assertRaises(ValueError):
            self._envelope({"bad": {1, 2}})
        with self.assertRaises(ValueError):
            ArtifactEnvelopeV1.create(
                artifact_type="../escape",
                producer="yifei-v4",
                producer_version="0.1.0",
                payload_schema="v4.daily-review",
                payload_schema_version="v1",
                as_of="2026-06-05",
                created_at="2026-06-05T18:00:00+08:00",
                source_refs=(),
                payload={},
            )

    @staticmethod
    def _envelope(payload: dict[str, object]) -> ArtifactEnvelopeV1:
        return ArtifactEnvelopeV1.create(
            artifact_type="v4/daily-review",
            producer="yifei-v4",
            producer_version="0.1.0",
            payload_schema="v4.daily-review",
            payload_schema_version="v1",
            as_of="2026-06-05",
            created_at="2026-06-05T18:00:00+08:00",
            source_refs=("quality:abc", "market:def", "quality:abc"),
            payload=payload,
        )
