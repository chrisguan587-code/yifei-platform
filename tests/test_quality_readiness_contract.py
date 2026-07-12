from __future__ import annotations

import json
import os
from pathlib import Path
import tempfile
import unittest

from yifei_platform.quality import DataQualitySnapshotV1, DatasetQualityV1, QualityStatus
from yifei_platform.readiness import (
    DataNotReadyError,
    ReadinessConflictError,
    ReadinessIntegrityError,
    ReadinessStoreV1,
)


class QualityReadinessContractTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.root = Path(self.tempdir.name)

    def tearDown(self) -> None:
        self.tempdir.cleanup()

    def test_snapshot_identity_is_deterministic_and_dataset_order_independent(self) -> None:
        first = self._snapshot((self._quality("stock_daily"), self._quality("board_daily")))
        second = self._snapshot((self._quality("board_daily"), self._quality("stock_daily")))
        self.assertEqual(first.snapshot_id, second.snapshot_id)
        self.assertEqual(["board_daily", "stock_daily"], [item.dataset for item in first.datasets])

    def test_snapshot_validates_coverage_and_unique_dataset(self) -> None:
        with self.assertRaises(ValueError):
            self._quality("stock_daily", coverage=1.1)
        duplicate = (self._quality("stock_daily"), self._quality("stock_daily"))
        with self.assertRaises(ValueError):
            self._snapshot(duplicate)

    def test_snapshot_and_marker_require_timezone(self) -> None:
        with self.assertRaises(ValueError):
            DataQualitySnapshotV1.create(
                as_of="2026-06-05",
                observed_at="2026-06-05T18:00:00",
                producer_version="platform.0.1.0",
                datasets=(self._quality("stock_daily"),),
            )
        snapshot = self._snapshot((self._quality("stock_daily"),))
        with self.assertRaises(ValueError):
            ReadinessStoreV1(self.root).publish_ready(
                bundle="eod-core",
                snapshot=snapshot,
                required_datasets=("stock_daily",),
                published_at="2026-06-05T18:01:00",
                producer_version="platform.0.1.0",
            )

    def test_publish_ready_writes_snapshot_then_marker_and_can_be_read(self) -> None:
        store = ReadinessStoreV1(self.root)
        snapshot = self._snapshot((self._quality("stock_daily"), self._quality("board_daily")))
        marker = store.publish_ready(
            bundle="eod-core",
            snapshot=snapshot,
            required_datasets=("stock_daily", "board_daily"),
            published_at="2026-06-05T18:01:00+08:00",
            producer_version="platform.0.1.0",
        )
        self.assertTrue((self.root / marker.quality_snapshot_ref).is_file())
        loaded = store.read_ready(bundle="eod-core", as_of="2026-06-05")
        self.assertEqual(marker, loaded)

    def test_degraded_missing_or_wrong_as_of_never_publishes_ready(self) -> None:
        store = ReadinessStoreV1(self.root)
        cases = (
            self._snapshot((self._quality("stock_daily", status=QualityStatus.DEGRADED),)),
            self._snapshot((self._quality("board_daily"),)),
            self._snapshot((self._quality("stock_daily", observed_as_of="2026-06-03"),)),
        )
        for snapshot in cases:
            with self.subTest(snapshot=snapshot.snapshot_id):
                with self.assertRaises(DataNotReadyError):
                    store.publish_ready(
                        bundle="eod-core",
                        snapshot=snapshot,
                        required_datasets=("stock_daily",),
                        published_at="2026-06-05T18:01:00+08:00",
                        producer_version="platform.0.1.0",
                    )
        self.assertIsNone(store.read_ready(bundle="eod-core", as_of="2026-06-05"))

    def test_interrupted_marker_publish_leaves_snapshot_but_no_false_ready(self) -> None:
        calls = 0

        def fail_second_publish(source: str, target: str) -> None:
            nonlocal calls
            calls += 1
            if calls == 2:
                raise OSError("simulated marker failure")
            os.link(source, target)

        store = ReadinessStoreV1(self.root, publish_file=fail_second_publish)
        snapshot = self._snapshot((self._quality("stock_daily"),))
        with self.assertRaises(OSError):
            store.publish_ready(
                bundle="eod-core",
                snapshot=snapshot,
                required_datasets=("stock_daily",),
                published_at="2026-06-05T18:01:00+08:00",
                producer_version="platform.0.1.0",
            )
        self.assertTrue((self.root / "quality" / snapshot.as_of / f"{snapshot.snapshot_id}.json").exists())
        self.assertIsNone(store.read_ready(bundle="eod-core", as_of="2026-06-05"))

    def test_repeat_is_idempotent_but_changed_marker_conflicts(self) -> None:
        store = ReadinessStoreV1(self.root)
        snapshot = self._snapshot((self._quality("stock_daily"),))
        arguments = {
            "bundle": "eod-core",
            "snapshot": snapshot,
            "required_datasets": ("stock_daily",),
            "published_at": "2026-06-05T18:01:00+08:00",
            "producer_version": "platform.0.1.0",
        }
        first = store.publish_ready(**arguments)
        self.assertEqual(first, store.publish_ready(**arguments))
        with self.assertRaises(ReadinessConflictError):
            store.publish_ready(**{**arguments, "published_at": "2026-06-05T18:02:00+08:00"})

    def test_tampered_marker_is_rejected(self) -> None:
        store = ReadinessStoreV1(self.root)
        snapshot = self._snapshot((self._quality("stock_daily"),))
        marker = store.publish_ready(
            bundle="eod-core",
            snapshot=snapshot,
            required_datasets=("stock_daily",),
            published_at="2026-06-05T18:01:00+08:00",
            producer_version="platform.0.1.0",
        )
        marker_path = self.root / "readiness" / "eod-core" / "2026-06-05.json"
        payload = json.loads(marker_path.read_text(encoding="utf-8"))
        payload["producer_version"] = "tampered"
        marker_path.write_text(json.dumps(payload), encoding="utf-8")
        with self.assertRaises(ReadinessIntegrityError):
            store.read_ready(bundle="eod-core", as_of="2026-06-05")
        self.assertTrue(marker.quality_snapshot_ref)

    def test_tampered_snapshot_is_rejected(self) -> None:
        store = ReadinessStoreV1(self.root)
        snapshot = self._snapshot((self._quality("stock_daily"),))
        marker = store.publish_ready(
            bundle="eod-core",
            snapshot=snapshot,
            required_datasets=("stock_daily",),
            published_at="2026-06-05T18:01:00+08:00",
            producer_version="platform.0.1.0",
        )
        snapshot_path = self.root / marker.quality_snapshot_ref
        payload = json.loads(snapshot_path.read_text(encoding="utf-8"))
        payload["producer_version"] = "tampered"
        snapshot_path.write_text(json.dumps(payload), encoding="utf-8")
        with self.assertRaises(ReadinessIntegrityError):
            store.read_ready(bundle="eod-core", as_of="2026-06-05")

    def _snapshot(self, datasets: tuple[DatasetQualityV1, ...]) -> DataQualitySnapshotV1:
        return DataQualitySnapshotV1.create(
            as_of="2026-06-05",
            observed_at="2026-06-05T18:00:00+08:00",
            producer_version="platform.0.1.0",
            datasets=datasets,
        )

    @staticmethod
    def _quality(
        dataset: str,
        *,
        status: QualityStatus = QualityStatus.OK,
        observed_as_of: str = "2026-06-05",
        coverage: float = 1.0,
    ) -> DatasetQualityV1:
        return DatasetQualityV1(
            dataset=dataset,
            status=status,
            observed_as_of=observed_as_of,
            source_version=f"{dataset}.v1",
            coverage=coverage,
            freshness_lag_sessions=0,
        )
