from __future__ import annotations

import json
from datetime import date
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from yifei_platform.concept_membership import (
    CONCEPT_SCHEMA_VERSION,
    _evaluate_ths_crawl,
    fetch_ths_web_concepts,
    latest_session_on_or_before,
    resolve_concept_snapshot,
    run_concept_update,
)


class FakeWebSocket:
    def __init__(self, response):
        self.response = response
        self.closed = False

    def settimeout(self, _seconds):
        pass

    def send(self, _message):
        pass

    def recv(self):
        return json.dumps(self.response)

    def close(self):
        self.closed = True


class ConceptUpdateTest(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.root = Path(self.tempdir.name)
        self.calendar = self.root / "calendar.json"
        self.calendar.write_text(json.dumps({
            "schema_version": "exchange-trading-calendar.v1",
            "sessions": [
                "2026-08-20", "2026-08-21", "2026-08-24", "2026-08-25",
                "2026-08-26", "2026-08-27", "2026-08-28",
            ],
        }), encoding="utf-8")

    def tearDown(self):
        self.tempdir.cleanup()

    def test_browser_crawl_decodes_cdp_result_and_closes_socket(self):
        concept = {
            "concept_code": "309269", "concept_name": "MLCC概念",
            "reported_member_count": 1, "parsed_member_count": 1,
            "member_codes": ["000001"], "complete": True,
            "returned_member_rows": 1, "valid_member_rows": 1,
        }
        connection = FakeWebSocket({
            "id": 1, "result": {"result": {"value": [concept]}},
        })
        with patch(
            "yifei_platform.concept_membership.websocket.create_connection",
            return_value=connection,
        ):
            self.assertEqual([concept], _evaluate_ths_crawl("ws://test"))
        self.assertTrue(connection.closed)

    def test_ths_report_aggregates_browser_membership_without_helper_fields(self):
        concepts = [
            {
                "concept_code": f"{300000 + index}",
                "concept_name": f"概念{index}",
                "reported_member_count": 1,
                "parsed_member_count": 1,
                "member_codes": [f"{index:06d}"],
                "complete": True,
                "returned_member_rows": 1,
                "valid_member_rows": 1,
            }
            for index in range(300)
        ]
        with patch(
            "yifei_platform.concept_membership._run_ths_browser_crawl",
            return_value=concepts,
        ):
            report = fetch_ths_web_concepts()
        self.assertTrue(report["ok"])
        self.assertEqual(1.0, report["member_code_parse_ratio"])
        self.assertNotIn("returned_member_rows", report["concepts"][0])

    def test_update_selects_one_source_without_mixing(self):
        ths_report = {
            "source": "ths_web", "taxonomy": "ths_concept", "ok": True,
            "duration_seconds": 1.0, "concept_count": 300,
            "complete_concept_ratio": 1.0, "member_code_parse_ratio": 1.0,
            "concepts": [{
                "concept_code": "885001.TI", "concept_name": "机器人",
                "member_codes": ["000001"], "complete": True,
            }],
        }
        with patch(
            "yifei_platform.concept_membership.fetch_ths_web_concepts",
            return_value=ths_report,
        ):
            result = run_concept_update(
                trade_date="2026-08-28",
                exchange_calendar=self.calendar,
                output_root=self.root / "concepts",
            )
        self.assertEqual("updated", result["status"])
        snapshot = json.loads(Path(result["selected_snapshot"]).read_text())
        self.assertEqual(CONCEPT_SCHEMA_VERSION, snapshot["schema_version"])
        self.assertEqual("ths_web", snapshot["selected_source"])
        self.assertFalse(snapshot["mixed_sources"])

    def test_failed_update_reuses_current_week_snapshot_as_normal(self):
        old_root = self.root / "concepts" / "2026-08-24"
        old_root.mkdir(parents=True)
        old_path = old_root / "concept_membership_2026-08-24.json"
        old_path.write_text(json.dumps({
            "schema_version": CONCEPT_SCHEMA_VERSION,
            "selected_source": "ths_web",
            "taxonomy": "ths_concept",
            "concepts": [{"concept_code": "x", "concept_name": "A",
                          "member_codes": ["000001"]}],
        }), encoding="utf-8")
        with patch(
            "yifei_platform.concept_membership.fetch_ths_web_concepts",
            return_value={
                "source": "ths_web", "taxonomy": "ths_concept",
                "ok": False, "concept_count": 0,
                "error": "down",
            },
        ):
            result = run_concept_update(
                trade_date="2026-08-27",
                exchange_calendar=self.calendar,
                output_root=self.root / "concepts",
            )
        self.assertEqual("reused", result["status"])
        self.assertEqual("normal", result["freshness"]["status"])
        self.assertEqual(3, result["freshness"]["age_trading_days"])

    def test_snapshot_older_than_fifteen_trading_days_is_not_used(self):
        sessions = [f"2026-09-{day:02d}" for day in range(1, 21)]
        self.calendar.write_text(json.dumps({
            "schema_version": "exchange-trading-calendar.v1",
            "sessions": sessions,
        }), encoding="utf-8")
        old_root = self.root / "concepts" / sessions[0]
        old_root.mkdir(parents=True)
        (old_root / f"concept_membership_{sessions[0]}.json").write_text(json.dumps({
            "schema_version": CONCEPT_SCHEMA_VERSION,
            "selected_source": "ths_web",
            "taxonomy": "ths_concept",
            "concepts": [{"concept_code": "x", "concept_name": "A",
                          "member_codes": ["000001"]}],
        }), encoding="utf-8")
        with self.assertRaises(FileNotFoundError):
            resolve_concept_snapshot(
                concept_root=self.root / "concepts",
                exchange_calendar=self.calendar,
                as_of_trade_date=sessions[-1],
            )

    def test_weekly_schedule_uses_latest_session_on_holiday(self):
        self.assertEqual(
            "2026-08-28",
            latest_session_on_or_before(self.calendar, date(2026, 8, 29)),
        )

    def test_snapshot_from_previous_week_is_degraded(self):
        date_root = self.root / "concepts" / "2026-08-20"
        date_root.mkdir(parents=True)
        (date_root / "concept_membership_2026-08-20.json").write_text(
            json.dumps({
                "schema_version": CONCEPT_SCHEMA_VERSION,
                "selected_source": "ths_web",
                "taxonomy": "ths_concept",
                "concepts": [{"concept_code": "x", "concept_name": "A",
                              "member_codes": ["000001"]}],
            }),
            encoding="utf-8",
        )
        _, freshness, _ = resolve_concept_snapshot(
            concept_root=self.root / "concepts",
            exchange_calendar=self.calendar,
            as_of_trade_date="2026-08-28",
        )
        self.assertEqual(6, freshness["age_trading_days"])
        self.assertEqual("degraded", freshness["status"])

    def test_bootstrap_snapshot_is_used_until_canonical_exists(self):
        date_root = self.root / "concepts" / "2026-08-28"
        date_root.mkdir(parents=True)
        bootstrap = date_root / (
            "concept_membership_2026-08-28_bootstrap-ths-web.json"
        )
        payload = {
            "schema_version": CONCEPT_SCHEMA_VERSION,
            "selected_source": "ths_web",
            "taxonomy": "ths_concept",
            "concepts": [{"concept_code": "x", "concept_name": "启动",
                          "member_codes": ["000001"]}],
        }
        bootstrap.write_text(json.dumps(payload), encoding="utf-8")
        concepts, _, selected = resolve_concept_snapshot(
            concept_root=self.root / "concepts",
            exchange_calendar=self.calendar,
            as_of_trade_date="2026-08-28",
        )
        self.assertEqual("启动", concepts[0]["concept_name"])
        self.assertEqual(bootstrap.resolve(), selected)

        canonical = date_root / "concept_membership_2026-08-28.json"
        payload["concepts"][0]["concept_name"] = "正式"
        canonical.write_text(json.dumps(payload), encoding="utf-8")
        concepts, _, selected = resolve_concept_snapshot(
            concept_root=self.root / "concepts",
            exchange_calendar=self.calendar,
            as_of_trade_date="2026-08-28",
        )
        self.assertEqual("正式", concepts[0]["concept_name"])
        self.assertEqual(canonical.resolve(), selected)


if __name__ == "__main__":
    unittest.main()
