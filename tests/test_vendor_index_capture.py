import tempfile
import sqlite3
import unittest
from pathlib import Path
from datetime import date,timedelta
from yifei_platform.vendor_index_capture import publish_vendor_index_capture_v1,read_vendor_index_capture_v1
from yifei_platform.artifacts import ArtifactStoreV1
from yifei_platform.supplemental_facts import initialize_supplemental_database_v1


class IndexCaptureTest(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name)
        self.market=self.root/'market.db'
        self.target=self.root/'supplemental.db'
        self.snapshots=self.root/'snapshots'
        self.days=[]
        day=date(2026,8,1)
        while day<=date(2026,10,8):
            if day.weekday()<5:self.days.append(day.isoformat())
            day+=timedelta(days=1)
        with sqlite3.connect(self.market) as c:
            c.executescript('CREATE TABLE trading_calendar(trade_date TEXT); CREATE TABLE stock_daily(stock_code TEXT,trade_date TEXT);')
            c.executemany('INSERT INTO trading_calendar VALUES(?)',[(d,) for d in self.days])
            c.execute("INSERT INTO stock_daily VALUES('000001','2026-10-08')")
        initialize_supplemental_database_v1(self.target)
        with sqlite3.connect(self.target) as c:
            c.execute("INSERT INTO sector_membership_history VALUES ('000001','测试','881100','行业0','THS_L2',"
                "'2026-01-01',NULL,'fixture','fixture.v1','2026-08-22T18:00:00+08:00')")
        outer=self
        class Client:
            def list_boards(self):
                return [dict(board_code=str(881100+i),board_name='行业'+str(i)) for i in range(90)]
            def read_history(self,name,start,end):
                return [dict(日期=d,开盘价=100.+i,最高价=102.+i,最低价=99.+i,收盘价=101.+i,
                    成交量=100.,成交额=1000.) for i,d in enumerate(outer.days) if start<=d.replace('-','')<=end]
        self.client=Client()

    def publish(self,at='2026-10-08T18:00:00+08:00'):
        return publish_vendor_index_capture_v1(client=self.client,market_database_path=self.market,
            target_path=self.target,snapshot_root=self.snapshots,as_of='2026-10-08',captured_at=at)

    def test_capture_projection_revisions_are_immutable_and_visible(self):
        first=self.publish()
        ref,e=read_vendor_index_capture_v1(database_path=self.target,snapshot_root=self.snapshots,
            as_of='2026-10-08',visible_at='2026-10-08T18:15:00+08:00')
        self.assertEqual(first.artifact_ref,ref)
        self.assertEqual(1080,len(e.payload['rows']))
        self.assertEqual('captured_current_revision_at_fetch',e.payload['input_semantics'])
        self.assertEqual(first,self.publish())
        second=self.publish('2026-10-08T18:05:00+08:00')
        self.assertNotEqual(first.artifact_id,second.artifact_id)
        self.assertEqual(e,ArtifactStoreV1(self.snapshots).read(first.artifact_ref))
        for day,at in [('2026-10-07','2026-10-08T18:15:00+08:00'),('2026-10-08','2026-10-08T17:00:00+08:00')]:
            with self.assertRaises(ValueError):
                read_vendor_index_capture_v1(database_path=self.target,snapshot_root=self.snapshots,as_of=day,visible_at=at)

    def test_historical_capture_not_live_and_incomplete_no_publication(self):
        r=self.publish('2026-10-09T18:00:00+08:00')
        self.assertEqual('historical_reconstructed/revised_input',ArtifactStoreV1(self.snapshots).read(r.artifact_ref).payload['input_semantics'])
        original=self.client.read_history
        self.client.read_history=lambda name,start,end: original(name,start,end)[:-1] if name=='行业0' else original(name,start,end)
        before=self.target.read_bytes()
        with self.assertRaisesRegex(ValueError,'window_incomplete'): self.publish()
        self.assertEqual(before,self.target.read_bytes())


if __name__=='__main__':unittest.main()
