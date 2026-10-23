from datetime import datetime
from pathlib import Path
import sqlite3
import tempfile
import unittest

from yifei_platform.supplemental_facts import initialize_supplemental_database_v1
from yifei_platform.sw_membership_refresh import refresh_missing_memberships
from yifei_platform.artifacts import ArtifactStoreV1


class MembershipRefreshTest(unittest.TestCase):
    def test_missing_only_keeps_history_and_actual_capture_visibility(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d);market=root/'market.db';target=root/'supplemental.db'
            day=datetime.now().astimezone().date().isoformat()
            with sqlite3.connect(market) as db:
                db.execute('CREATE TABLE stock_daily(stock_code TEXT,stock_name TEXT,trade_date TEXT,amount REAL)')
                db.executemany('INSERT INTO stock_daily VALUES (?,?,?,?)',
                    [(c,c,day,100.) for c in ('000001','000002','000003')])
            initialize_supplemental_database_v1(target)
            with sqlite3.connect(target) as db:
                db.execute("INSERT INTO sector_membership_history VALUES ('000001','旧','CNINFO_SW_L2:旧行业','旧行业','L2',"
                    "'2020-01-01',NULL,'akshare.cninfo','akshare.cninfo-sw-l2.v1','2020-01-01T18:00:00+08:00')")
                before=db.execute('SELECT * FROM sector_membership_history').fetchall()
            calls=[]
            def read(code,as_of):
                calls.append(code)
                if code=='000003': raise OSError('source unavailable')
                return [dict(SECNAME='新',F001V='008003',F005V='半导体',VARYDATE='2026-01-01')]
            r=refresh_missing_memberships(market_database=market,target_path=target,snapshot_root=root/'captures',
                as_of=day,reader=read)
            self.assertEqual({'000002','000003'},set(calls))
            self.assertEqual(1,r['inserted']);self.assertEqual(['000003'],r['unavailable_codes'])
            with sqlite3.connect(target) as db:
                self.assertEqual(before,db.execute("SELECT * FROM sector_membership_history WHERE stock_code='000001'").fetchall())
                row=db.execute("SELECT valid_from,fetched_at FROM sector_membership_history WHERE stock_code='000002'").fetchone()
                self.assertEqual(day,row[0]);self.assertEqual(r['captured_at'],row[1])
            capture=ArtifactStoreV1(root/'captures').read(r['capture_ref'])
            self.assertEqual(2,len(capture.payload['captures']))
            again=refresh_missing_memberships(market_database=market,target_path=target,snapshot_root=root/'captures',
                as_of=day,reader=lambda code,as_of: [])
            self.assertEqual(0,again['inserted'])
