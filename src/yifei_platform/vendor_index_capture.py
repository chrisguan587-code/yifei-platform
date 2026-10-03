"""Immutable vendor industry inputs; no strategy or application semantics."""
from dataclasses import asdict
from datetime import datetime
from pathlib import Path
import argparse
import json
import os
import shutil
import sqlite3
import tempfile
from zoneinfo import ZoneInfo

from .artifacts import ArtifactEnvelopeV1, ArtifactStoreV1
from .board_daily_ingestion import (AkshareThsBoardDailyClientV1, _collect_rows,
    _valid_boards, _prior_calendar_date)
from .bootstrap import load_trading_sessions
from .calendar import TradingCalendarV1
from .supplemental_facts import (SectorMembershipReaderV1,
    serialized_supplemental_publication_v1, BOARD_DAILY_MINIMUM_ROWS)

VERSION = 'ths-industry-index-http.v1'
SCHEMA = 'vendor-industry-index.v1'


@serialized_supplemental_publication_v1
def publish_vendor_index_capture_v1(*, client, market_database_path, target_path,
        snapshot_root, as_of, captured_at=None):
    calendar = TradingCalendarV1(load_trading_sessions(market_database_path), source_version='published-calendar')
    days = [calendar.offset_session(as_of, n).isoformat() for n in range(-11, 1)]
    with sqlite3.connect(f'file:{market_database_path.resolve()}?mode=ro',uri=True) as con:
        if not con.execute('SELECT 1 FROM stock_daily WHERE trade_date=? LIMIT 1',(as_of,)).fetchone():
            raise ValueError('index_market_date_missing')
    boards = _valid_boards(client.list_boards())
    if len(boards) < BOARD_DAILY_MINIMUM_ROWS or any(not code.startswith('881') for code,name in boards):
        raise ValueError('industry_directory_incomplete_or_wrong_taxonomy')
    rows = _collect_rows(client=client,boards=boards,
        start_date=_prior_calendar_date(market_database_path,days[0]),end_date=as_of,expected_dates=set(days))
    if {(r[0],r[2]) for r in rows} != {(code,day) for code,name in boards for day in days}:
        raise ValueError('industry_index_window_incomplete')
    membership = SectorMembershipReaderV1(target_path,sector_level='THS_L2').read_as_of(as_of)
    if not membership.ok:
        raise ValueError('industry_membership_unavailable')
    names = {name for code,name in boards}
    if any(m.sector_name not in names for m in membership.facts):
        raise ValueError('industry_membership_identity_mismatch')
    at = captured_at or datetime.now().astimezone().isoformat()
    stamp = datetime.fromisoformat(at)
    if stamp.tzinfo is None or stamp < datetime.fromisoformat(as_of+'T15:00:00+08:00'):
        raise ValueError('industry_capture_before_close_or_timezone_missing')
    if any(datetime.fromisoformat(m.fetched_at) > stamp for m in membership.facts):
        raise ValueError('industry_membership_not_yet_visible')
    fields = ('board_code','board_name','trade_date','open','high','low','close','volume','amount','pct_chg')
    payload = dict(schema_version=SCHEMA,as_of=as_of,source_version=VERSION,days=days,
        captured_at=at,boards=[dict(board_code=code,board_name=name) for code,name in boards],
        rows=[dict(zip(fields,r)) for r in sorted(rows)],memberships=[asdict(m) for m in membership.facts],
        input_semantics=('captured_current_revision_at_fetch' if
            stamp.astimezone(ZoneInfo('Asia/Shanghai')).date().isoformat() == as_of
            else 'historical_reconstructed/revised_input'))
    envelope = ArtifactEnvelopeV1.create(artifact_type='vendor-industry-index',producer='yifei-platform',
        producer_version=VERSION,payload_schema='vendor-industry-index',payload_schema_version='v1',
        as_of=as_of,created_at=at,source_refs=('ths:d.10jqka.com.cn/v4/line',),payload=payload)
    receipt = ArtifactStoreV1(snapshot_root).write(envelope)
    # The table is a current projection. The immutable capture is the PIT source.
    descriptor, filename = tempfile.mkstemp(prefix='.vendor-index.',dir=target_path.parent)
    os.close(descriptor)
    temp = Path(filename)
    try:
        shutil.copy2(target_path,temp)
        with sqlite3.connect(temp) as con:
            con.executemany('INSERT OR REPLACE INTO ths_board_daily VALUES (?,?,?,?,?,?,?,?,?,?)',rows)
            con.executemany('INSERT INTO supplemental_metadata(key,value) VALUES(?,?) '
                'ON CONFLICT(key) DO UPDATE SET value=excluded.value',(
                    ('board_source_version',VERSION),('board_published_at',at),
                    ('board_capture_ref',receipt.artifact_ref)))
            con.commit()
            if con.execute('PRAGMA quick_check').fetchone()[0] != 'ok':
                raise ValueError('index_publication_integrity_failed')
        os.replace(temp,target_path)
    finally:
        temp.unlink(missing_ok=True)
    return receipt


def read_vendor_index_capture_v1(*, database_path, snapshot_root, as_of, visible_at):
    with sqlite3.connect(f'file:{database_path.resolve()}?mode=ro',uri=True) as con:
        row = con.execute("SELECT value FROM supplemental_metadata WHERE key='board_capture_ref'").fetchone()
    if not row:
        raise ValueError('vendor_index_capture_missing')
    envelope = ArtifactStoreV1(snapshot_root).read(row[0])
    if (envelope.as_of != as_of or envelope.payload_schema != 'vendor-industry-index'
            or envelope.producer_version != VERSION or envelope.payload.get('schema_version') != SCHEMA):
        raise ValueError('vendor_index_capture_date_or_schema_mismatch')
    stamp = datetime.fromisoformat(visible_at)
    if stamp.tzinfo is None or datetime.fromisoformat(envelope.created_at) > stamp:
        raise ValueError('vendor_index_capture_not_yet_visible')
    return row[0],envelope


def main():
    p=argparse.ArgumentParser(description='Publish original THS industry index with immutable capture')
    p.add_argument('--market-db',type=Path,required=True)
    p.add_argument('--target-db',type=Path,required=True)
    p.add_argument('--snapshot-root',type=Path,required=True)
    p.add_argument('--as-of',required=True)
    a=p.parse_args()
    try:
        receipt=publish_vendor_index_capture_v1(client=AkshareThsBoardDailyClientV1(),
            market_database_path=a.market_db,target_path=a.target_db,snapshot_root=a.snapshot_root,as_of=a.as_of)
    except Exception as e:
        print(json.dumps({'status':'unavailable','as_of':a.as_of,'reason':str(e)},ensure_ascii=False))
        return 75
    print(json.dumps({'status':'published','as_of':a.as_of,**asdict(receipt)},ensure_ascii=False))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
