"""Manual incremental CNInfo SW-L2 capture; never replace existing history."""
import argparse
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
import json
from pathlib import Path
import shutil
import sqlite3
import subprocess
import time
from urllib.parse import urlencode
from urllib.request import Request,urlopen

from .artifacts import ArtifactEnvelopeV1,ArtifactStoreV1
from .public_data_ingestion import _cninfo_sw_l2_intervals,MEMBERSHIP_SOURCE_VERSION
from .supplemental_facts import serialized_supplemental_publication_v1


def read_cninfo_industry(code,as_of):
    # Same public AES timestamp protocol as AKShare cninfo.js getResCode1,
    # using the installed OpenSSL instead of the broken MiniRacer runtime.
    binary=shutil.which('openssl')
    if binary is None: raise RuntimeError('openssl_required_for_cninfo')
    key=b'1234567887654321'.hex()
    token=subprocess.run([binary,'enc','-aes-128-cbc','-K',key,'-iv',key,'-a','-A'],
        input=str(int(time.time())).encode(),capture_output=True,check=True,timeout=10).stdout.decode().strip()
    query=urlencode(dict(scode=code,sdate='1990-01-01',edate=as_of))
    request=Request('https://webapi.cninfo.com.cn/api/stock/p_stock2110?'+query,data=b'',
        headers={'Accept-Enckey':token,'Referer':'https://webapi.cninfo.com.cn/','User-Agent':'Mozilla/5.0'})
    with urlopen(request,timeout=25) as response: payload=json.load(response)
    rows=payload.get('records')
    if not isinstance(rows,list) or int(payload.get('total',len(rows)))!=len(rows):
        raise ValueError('cninfo_response_missing_or_truncated')
    if any(r.get('SECCODE')!=code for r in rows): raise ValueError('cninfo_stock_mismatch')
    return rows


@serialized_supplemental_publication_v1
def refresh_missing_memberships(*,market_database,target_path,snapshot_root,as_of,reader=read_cninfo_industry):
    stamp=datetime.now().astimezone().isoformat()
    if as_of>stamp[:10]: raise ValueError('future_membership_refresh')
    with sqlite3.connect(f'{market_database.resolve().as_uri()}?mode=ro',uri=True) as market:
        names=dict(market.execute('SELECT stock_code,stock_name FROM stock_daily WHERE trade_date=? AND amount>0',(as_of,)))
    if not names: raise ValueError('market_date_missing')
    with sqlite3.connect(f'{target_path.resolve().as_uri()}?mode=ro',uri=True) as db:
        known={r[0] for r in db.execute('SELECT stock_code FROM sector_membership_history WHERE sector_level=? '
            'AND valid_from<=? AND (valid_to_exclusive IS NULL OR valid_to_exclusive>?)',('L2',as_of,as_of))}
        versions={r[0] for r in db.execute("SELECT DISTINCT source_version FROM sector_membership_history WHERE sector_level='L2'")}
    if versions!={MEMBERSHIP_SOURCE_VERSION}: raise ValueError('existing_sw_contract_version_mismatch')
    codes=sorted(set(names)-known)
    def capture(code):
        try: return dict(stock_code=code,records=reader(code,as_of))
        except Exception as exc: return dict(stock_code=code,error=type(exc).__name__+':'+str(exc)[:200])
    with ThreadPoolExecutor(max_workers=4) as pool: captures=list(pool.map(capture,codes))
    stamp=datetime.now().astimezone().isoformat()
    rows=[];unavailable=[]
    for item in captures:
        code=item['stock_code']
        if 'error' in item:
            unavailable.append(code);continue
        normalized=[dict(stock_name=r.get('SECNAME'),classification_code=r.get('F001V'),
            industry_l2_name=r.get('F005V'),valid_from=r.get('VARYDATE')) for r in item['records']]
        intervals=_cninfo_sw_l2_intervals(stock_code=code,rows=normalized,fetched_at=stamp,
            source_version=MEMBERSHIP_SOURCE_VERSION)
        active=[r for r in intervals if r[5]<=as_of and (r[6] is None or as_of<r[6])]
        if len(active)>1: raise ValueError('ambiguous_cninfo_membership:'+code)
        if not active:
            unavailable.append(code);continue
        # Membership becomes locally usable no earlier than the real capture day.
        row=list(active[0]);row[5]=stamp[:10];row[6]=None
        rows.append(tuple(row))
    envelope=ArtifactEnvelopeV1.create(artifact_type='sw-membership-refresh',producer='yifei-platform',
        producer_version='cninfo-stock2110-http.v1',payload_schema='sw-membership-refresh',payload_schema_version='v1',
        as_of=as_of,created_at=stamp,source_refs=('https://webapi.cninfo.com.cn/api/stock/p_stock2110',),
        payload=dict(as_of=as_of,captured_at=stamp,captures=captures,insert_rows=rows,
            unavailable_codes=unavailable,semantics='captured_current_membership_not_historical_live'))
    receipt=ArtifactStoreV1(snapshot_root).write(envelope)
    with sqlite3.connect(target_path) as db:
        db.execute('BEGIN IMMEDIATE')
        # Existing rows are never deleted or corrected by this missing-only producer.
        db.executemany('INSERT INTO sector_membership_history (stock_code,stock_name,sector_code,sector_name,'
            'sector_level,valid_from,valid_to_exclusive,source,source_version,fetched_at) VALUES (?,?,?,?,?,?,?,?,?,?)',rows)
        db.execute('INSERT INTO supplemental_metadata(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value',
            ('sw_membership_refresh_capture_ref',receipt.artifact_ref))
        db.commit()
    return dict(status='completed',as_of=as_of,requested=len(codes),inserted=len(rows),
        unavailable_codes=unavailable,captured_at=stamp,capture_ref=receipt.artifact_ref)


def main():
    p=argparse.ArgumentParser(description='Capture only missing CNInfo SW-L2 members, with actual visibility time')
    for name in ('market-db','target-db','snapshot-root'): p.add_argument('--'+name,type=Path,required=True)
    p.add_argument('--as-of',required=True)
    a=p.parse_args()
    print(json.dumps(refresh_missing_memberships(market_database=a.market_db,target_path=a.target_db,
        snapshot_root=a.snapshot_root,as_of=a.as_of),ensure_ascii=False))

if __name__=='__main__':main()
