import tempfile
import unittest
from pathlib import Path

from yifei_platform.artifacts import ArtifactStoreV1
from yifei_platform.attention_capture import capture_attention, normalize, is_post_close_session
from datetime import datetime
import json
import plistlib
from unittest.mock import patch


class AttentionCaptureTests(unittest.TestCase):
    at = "2026-10-23T22:30:00+08:00"

    def test_em_top_has_unknown_source_time(self):
        rows = normalize("eastmoney", {"code": 0, "data": [{"sc": "SZ000678", "rk": 9}]}, self.at)
        self.assertEqual(rows[0]["stock_code"], "000678")
        self.assertIsNone(rows[0]["source_time"])
        self.assertEqual(rows[0]["time_status"], "unknown")

    def test_individual_beyond_top100_is_retained(self):
        rows = normalize("eastmoney", {"code": 0, "data": {
            "srcSecurityCode": "SH600664", "rank": 260, "calcTime": "2026-10-23 22:26:00"}}, self.at, single=True)
        self.assertEqual(rows[0]["rank"], 260)

    def test_ths_timestamp_not_replaced(self):
        for stamp, status in (("2026-10-23 22:01:39", "same_date"),
                              ("2026-10-22 22:01:39", "different_date"),
                              ("2026-10-24 22:01:39", "future_timestamp")):
            rows = normalize("ths_via_zzshare", {"code": 20000, "data": [
                {"symbol_code": "603278", "rank": 1, "update_time": stamp}]}, self.at)
            self.assertEqual(rows[0]["source_time"], stamp)
            self.assertEqual(rows[0]["time_status"], status)

    def test_invalid_data_not_silently_skipped(self):
        for data in ([], [{"sc": "SZ000678", "rk": 0}],
                     [{"sc": "SZ000678", "rk": 1}] * 2):
            with self.assertRaises(ValueError):
                normalize("eastmoney", {"code": 0, "data": data}, self.at)

    def test_failure_is_preserved_without_discarding_other_source(self):
        def fetch(url, body, headers):
            if "zizizaizai" in url:
                raise TimeoutError("timeout")
            return {"code": 0, "data": [{"sc": "SZ000678", "rk": 9}]}
        with tempfile.TemporaryDirectory() as directory:
            receipt, payload = capture_attention(snapshot_root=directory, fetch=fetch)
            self.assertEqual(payload["status"], "degraded")
            self.assertEqual([o["status"] for o in payload["observations"]], ["ok", "unavailable"])
            saved = ArtifactStoreV1(Path(directory)).read(receipt.artifact_ref)
            self.assertEqual(saved.payload, payload)

    def test_source_ranks_remain_separate(self):
        def fetch(url, body, headers):
            if "zizizaizai" in url:
                return {"code": 20000, "data": [{"symbol_code": "000678", "rank": 27}]}
            if "getCurrentLatest" in url:
                return {"code": 0, "data": {"srcSecurityCode": "SZ000678", "rank": 9}}
            return {"code": 0, "data": [{"sc": "SZ000678", "rk": 9}]}
        with tempfile.TemporaryDirectory() as directory:
            _, payload = capture_attention(snapshot_root=directory, codes=["000678"], fetch=fetch)
            self.assertEqual([o["rows"][0]["rank"] for o in payload["observations"]], [9, 27, 9])
            self.assertTrue(all("raw_response" in o for o in payload["observations"]))

    def test_wrong_single_stock_is_unavailable(self):
        def fetch(url, body, headers):
            if "getCurrentLatest" in url:
                return {"code": 0, "data": {"srcSecurityCode": "SZ000001", "rank": 9}}
            if "zizizaizai" in url:
                return {"code": 20000, "data": [{"symbol_code": "000678", "rank": 27}]}
            return {"code": 0, "data": [{"sc": "SZ000678", "rk": 9}]}
        with tempfile.TemporaryDirectory() as directory:
            _, payload = capture_attention(snapshot_root=directory, codes=["000678"], fetch=fetch)
            self.assertEqual(payload["observations"][-1]["status"], "unavailable")

    def test_daily_guard_sessions_holidays_hours_and_calendar_expiry(self):
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'calendar.json'
            path.write_text(json.dumps(dict(schema_version='exchange-trading-calendar.v1',
                coverage_start='2026-01-01',coverage_end='2026-12-31',sessions=['2026-10-23'])))
            self.assertTrue(is_post_close_session(path,datetime.fromisoformat('2026-10-23T18:40:00+08:00')))
            self.assertTrue(is_post_close_session(path,datetime.fromisoformat('2026-10-23T10:40:00+00:00')))
            self.assertFalse(is_post_close_session(path,datetime.fromisoformat('2026-10-24T18:40:00+08:00')))
            self.assertFalse(is_post_close_session(path,datetime.fromisoformat('2026-10-01T18:40:00+08:00')))
            for stamp in ('2026-10-23T14:40:00+08:00','2027-01-04T18:40:00+08:00','2026-10-23T18:40:00'):
                with self.assertRaises(ValueError):
                    is_post_close_session(path,datetime.fromisoformat(stamp))

    def test_scheduled_non_session_does_not_fetch_or_write(self):
        from yifei_platform.attention_capture import main
        with patch('sys.argv',['attention_capture','--snapshot-root','unused','--exchange-calendar','calendar']), \
             patch('yifei_platform.attention_capture.is_post_close_session',return_value=False), \
             patch('yifei_platform.attention_capture.capture_attention') as capture:
            self.assertEqual(main(),0)
            capture.assert_not_called()

    def test_launch_agent_is_one_daily_post_close_trigger_without_notifications(self):
        root=Path(__file__).resolve().parents[1]
        job=plistlib.loads((root/'ops/launchd/com.yplus.yifei-platform.stock-attention.plist').read_bytes())
        self.assertEqual(job['StartCalendarInterval'],dict(Hour=18,Minute=40))
        self.assertNotIn('RunAtLoad',job)
        self.assertNotIn('KeepAlive',job)
        self.assertEqual(job['EnvironmentVariables']['TZ'],'Asia/Shanghai')
        self.assertTrue(job['ProgramArguments'][1].endswith('/scripts/run_attention_daily.sh'))
        script=(root/'scripts/run_attention_daily.sh').read_text()
        self.assertIn('--exchange-calendar',script)
        self.assertIn('yifei_platform.attention_capture',script)
        self.assertNotIn('shortline',script)

    def test_confirmed_identities_are_frozen_and_get_individual_rank_updates(self):
        manifest=dict(schema_version='human-stock-identities.v1',source='user_confirmation',source_version='test.v1',
            confirmed_at='2020-01-01T00:00:00+08:00',stocks=[dict(stock_code='300750',stock_name='宁德时代',
                direction='锂电池',sub_direction='动力/储能电池',role='绝对容量核心')])
        requests=[]
        def fetch(url,body,headers):
            requests.append((url,body))
            if 'getCurrentLatest' in url:
                return dict(code=0,data=dict(srcSecurityCode=body['srcSecurityCode'],rank=255))
            if 'zizizaizai' in url:return dict(code=20000,data=[dict(symbol_code='000678',rank=2)])
            return dict(code=0,data=[dict(sc='SZ000678',rk=1)])
        with tempfile.TemporaryDirectory() as directory:
            _,payload=capture_attention(snapshot_root=directory,representatives=manifest,fetch=fetch)
            self.assertEqual(payload['representative_assertions'],manifest)
            self.assertEqual(payload['observations'][-1]['rows'][0]['rank'],255)
            self.assertEqual(requests[-1][1]['srcSecurityCode'],'SZ300750')

    def test_confirmation_must_exist_before_capture(self):
        manifest=dict(schema_version='human-stock-identities.v1',source='user_confirmation',source_version='test.v1',
            confirmed_at='9999-01-01T00:00:00+08:00',stocks=[])
        with tempfile.TemporaryDirectory() as directory,patch('yifei_platform.attention_capture.request_json') as fetch:
            with self.assertRaisesRegex(ValueError,'not_yet_confirmed'):
                capture_attention(snapshot_root=directory,representatives=manifest,fetch=fetch)
            fetch.assert_not_called()

    def test_manifest_contains_exactly_24_user_confirmed_identities_and_daily_entry_loads_it(self):
        root=Path(__file__).resolve().parents[1]
        manifest=json.loads((root/'config/stock_representatives.v1.json').read_text())
        self.assertEqual(len(manifest['stocks']),24)
        self.assertEqual(len({r['stock_code'] for r in manifest['stocks']}),24)
        self.assertEqual(len({r['direction'] for r in manifest['stocks']}),8)
        byd=next(r for r in manifest['stocks'] if r['stock_code']=='002594')
        self.assertEqual(byd['sub_direction'],'电池 + 整车')
        self.assertEqual(byd['role'],'产业核心 / 容量核心')
        self.assertIn('--representatives "$root/config/stock_representatives.v1.json"',
                      (root/'scripts/run_attention_daily.sh').read_text())


if __name__ == "__main__":
    unittest.main()
