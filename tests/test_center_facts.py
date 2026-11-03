import json
from pathlib import Path
import sqlite3
import tempfile
import unittest

from yifei_platform.artifacts import ArtifactIntegrityError
from yifei_platform.center_facts import publish_center_facts_v1, read_center_snapshot_v1, read_stock_center_v1
from yifei_platform.quality import DataQualitySnapshotV1, DatasetQualityV1, QualityStatus
from yifei_platform.readiness import DataNotReadyError, ReadinessStoreV1


class CenterFactsTest(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name)
        self.market=self.root/'market.db'
        self.calendar=self.root/'calendar.json'
        self.output=self.root/'centers'
        self.state=self.root/'state'
        self.fixture=json.loads((Path(__file__).parent/'fixtures/recent_center_confirmed_20261021.json').read_text())
        self.day=self.fixture['as_of']
        self.published='2026-10-21T18:00:00+08:00'
        self.calendar.write_text(json.dumps(dict(schema_version='exchange-trading-calendar.v1',
            sessions=self.fixture['sessions'])))
        with sqlite3.connect(self.market) as c:
            c.execute('CREATE TABLE stock_daily (stock_code TEXT,stock_name TEXT,trade_date TEXT,'
                'open REAL,high REAL,low REAL,close REAL,preclose REAL,volume REAL,amount REAL,pct_chg REAL)')
            c.execute('CREATE TABLE platform_metadata (key TEXT PRIMARY KEY,value TEXT)')
            c.execute("INSERT INTO platform_metadata VALUES ('producer_version','fixture-market.v1')")
            for case in self.fixture['cases']:
                for r in case['rows']:
                    c.execute('INSERT INTO stock_daily VALUES (?,?,?,?,?,?,?,?,?,?,?)',
                        (case['stock_code'],case['stock_name'],r['trade_date'],r['close'],r['high'],
                         r['low'],r['close'],r['preclose'],r['volume'],r['amount'],r['pct_chg']))
        snapshot=DataQualitySnapshotV1.create(as_of=self.day,observed_at=self.published,
            producer_version='fixture-market.v1',datasets=(DatasetQualityV1(dataset='stock_daily',
                status=QualityStatus.OK,observed_as_of=self.day,source_version='fixture-market.v1'),))
        ReadinessStoreV1(self.state).publish_ready(bundle='v4-market-core',snapshot=snapshot,
            required_datasets=('stock_daily',),published_at=self.published,producer_version='fixture-market.v1')

    def publish(self, **overrides):
        args=dict(market_db=self.market,exchange_calendar=self.calendar,readiness_root=self.state,
            output_root=self.output,as_of=self.day,published_at=self.published)
        return publish_center_facts_v1(**(args|overrides))

    def test_roundtrip_all_stocks_and_read_only_source(self):
        before=self.market.read_bytes()
        payload=self.publish()
        self.assertEqual(payload['stock_count'],2)
        self.assertEqual(payload['status_counts'],{'ready':2})
        self.assertEqual(payload['window_start'],'2026-07-21')
        for case in self.fixture['cases']:
            fact=read_stock_center_v1(self.output,as_of=self.day,stock_code=case['stock_code'])
            self.assertEqual([fact['center']['lower'],fact['center']['upper']],case['expected_bounds'])
            self.assertEqual(fact['position'],case['expected_position'])
        self.assertEqual(before,self.market.read_bytes())

    def test_repeated_publication_never_changes_frozen_day(self):
        first=self.publish()
        before=(self.output/f'{self.day}.json').read_bytes()
        with sqlite3.connect(self.market) as c:
            c.execute("UPDATE stock_daily SET close=1 WHERE trade_date=?",(self.day,))
        self.assertEqual(self.publish(published_at='2026-10-22T18:00:00+08:00'),first)
        self.assertEqual((self.output/f'{self.day}.json').read_bytes(),before)

    def test_warmup_restart_policy_and_diagnostics_survive_publication(self):
        from yifei_platform.consolidation_center import WARMUP_POLICY
        code=self.fixture['cases'][0]['stock_code']
        with sqlite3.connect(self.market) as c:
            c.execute('UPDATE stock_daily SET preclose=preclose*0.5 WHERE stock_code=? AND trade_date=?',
                      (code,'2026-06-25'))
        payload=self.publish()
        self.assertEqual(payload['warmup_policy_version'],WARMUP_POLICY)
        fact=read_stock_center_v1(self.output,as_of=self.day,stock_code=code)
        self.assertEqual(fact['diagnostics']['warmup_resets'][0]['invalid_at'],'2026-06-25')
        self.assertEqual(fact['diagnostics']['effective_history_start'],'2026-06-25')
        self.assertNotEqual(fact['status'],'price_lineage_invalid')

    def test_correction_keeps_original_and_is_visible_only_after_publication(self):
        original=self.publish();path=self.output/f'{self.day}.json';before=path.read_bytes()
        corrected=self.publish(correct_warmup=True,published_at='2026-10-21T20:00:00+08:00')
        self.assertEqual(path.read_bytes(),before)
        self.assertEqual(corrected['correction']['reason'],'pre_window_warmup_restart')
        self.assertEqual(read_center_snapshot_v1(self.output,as_of=self.day),corrected)
        self.assertEqual(read_center_snapshot_v1(self.output,as_of=self.day,
            known_at='2026-10-21T19:00:00+08:00'),original)
        self.assertEqual(read_center_snapshot_v1(self.output,as_of=self.day,
            known_at='2026-10-21T20:00:00+08:00'),corrected)
        self.assertIsNone(read_center_snapshot_v1(self.output,as_of=self.day,
            known_at='2026-10-21T17:00:00+08:00'))
        self.assertEqual(self.publish(correct_warmup=True,published_at='2026-10-22T20:00:00+08:00'),corrected)

    def test_correction_cannot_hide_changed_inputs_or_missing_original(self):
        with self.assertRaises(ValueError):self.publish(correct_warmup=True)
        self.publish()
        with sqlite3.connect(self.market) as c:
            c.execute('UPDATE stock_daily SET high=high+1 WHERE trade_date=?',(self.day,))
        with self.assertRaises(DataNotReadyError):
            self.publish(correct_warmup=True,published_at='2026-10-21T20:00:00+08:00')

    def test_corrupt_correction_is_not_silently_replaced_by_original(self):
        self.publish();self.publish(correct_warmup=True,published_at='2026-10-21T20:00:00+08:00')
        path=self.output/f'{self.day}.warmup-restart-v1.json'
        broken=json.loads(path.read_text());broken['payload']['status']='wrong'
        path.write_text(json.dumps(broken))
        with self.assertRaises(ArtifactIntegrityError):read_center_snapshot_v1(self.output,as_of=self.day)

    def test_missing_stock_or_date_never_falls_back(self):
        self.publish()
        self.assertIsNone(read_center_snapshot_v1(self.output,as_of='2026-10-20'))
        self.assertEqual(read_stock_center_v1(self.output,as_of='2026-10-20',stock_code='600721')['status'],'snapshot_missing')
        self.assertEqual(read_stock_center_v1(self.output,as_of=self.day,stock_code='000001')['status'],'stock_not_found')

    def test_individual_data_gap_is_degraded_not_universal_failure(self):
        with sqlite3.connect(self.market) as c:
            c.execute("DELETE FROM stock_daily WHERE stock_code='603228' AND trade_date='2026-09-03'")
        result=self.publish()
        self.assertEqual(result['status'],'degraded')
        self.assertEqual(result['stocks']['603228']['status'],'source_sessions_missing')
        self.assertIsNone(result['stocks']['603228']['center'])
        self.assertEqual(result['stocks']['600721']['status'],'ready')

    def test_current_missing_close_and_short_history_remain_explicit(self):
        with sqlite3.connect(self.market) as c:
            c.execute("DELETE FROM stock_daily WHERE stock_code='603228' AND trade_date=?",(self.day,))
            c.execute('INSERT INTO stock_daily VALUES (?,?,?,?,?,?,?,?,?,?,?)',
                ('000001','new',self.day,10,11,9,10,10,100,1000,0))
        result=self.publish()
        self.assertEqual(result['stocks']['603228']['status'],'as_of_close_missing')
        self.assertIsNone(result['stocks']['603228']['close'])
        self.assertEqual(result['stocks']['000001']['status'],'history_insufficient')

    def test_calendar_readiness_and_post_close_boundary(self):
        with self.assertRaises(ValueError):
            self.publish(published_at='2026-10-21T14:59:59+08:00')
        with self.assertRaises(DataNotReadyError):
            self.publish(readiness_root=self.root/'no-state')
        with self.assertRaises(DataNotReadyError):
            self.publish(as_of='2026-10-18')
        self.assertFalse(self.output.exists())

    def test_corrupt_frozen_snapshot_is_not_replaced(self):
        self.publish()
        path=self.output/f'{self.day}.json'
        broken=json.loads(path.read_text());broken['payload']['stocks']['600721']['close']=1
        path.write_text(json.dumps(broken))
        with self.assertRaises(ArtifactIntegrityError):
            self.publish()

    def test_historical_reconstruction_is_explicit_and_excludes_future_rows(self):
        day='2026-10-20'
        quality=DataQualitySnapshotV1.create(as_of=day,observed_at=self.published,
            producer_version='fixture-market.v1',datasets=(DatasetQualityV1(dataset='stock_daily',
                status=QualityStatus.OK,observed_as_of=day,source_version='fixture-market.v1'),))
        ReadinessStoreV1(self.state).publish_ready(bundle='v4-market-core',snapshot=quality,
            required_datasets=('stock_daily',),published_at=self.published,producer_version='fixture-market.v1')
        with self.assertRaises(DataNotReadyError):
            self.publish(as_of=day)
        first=self.publish(as_of=day,historical_reconstruction=True)
        self.assertEqual(first['input_semantics'],'historical_reconstruction')
        self.assertEqual(first['published_at'],self.published)
        with sqlite3.connect(self.market) as c:
            c.execute('UPDATE stock_daily SET high=999999,close=999999 WHERE trade_date>?',(day,))
        second=self.publish(as_of=day,historical_reconstruction=True,output_root=self.root/'second')
        self.assertEqual(first['stocks'],second['stocks'])
        self.assertEqual(first['source']['input_rows_sha256'],second['source']['input_rows_sha256'])
