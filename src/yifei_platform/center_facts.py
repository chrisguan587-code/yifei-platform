"""Platform-owned, immutable post-close consolidation-center facts."""
from collections import Counter
from datetime import date, datetime, time
from hashlib import sha256
from itertools import groupby
import json
from math import isfinite
from pathlib import Path
import sqlite3
from zoneinfo import ZoneInfo

from .artifacts import ArtifactEnvelopeV1, ArtifactIntegrityError, ArtifactStoreV1, write_immutable_json_v1
from .consolidation_center import CONTRACT, RECENT_CONTRACT, WARMUP_POLICY, recent_center
from .readiness import DataNotReadyError, ReadinessStoreV1


SCHEMA = 'platform-consolidation-centers.daily.v1'
PRODUCER = 'platform-consolidation-centers.v1'


def _read_center_file(root, filename, day):
    if not (root/filename).exists():
        return None
    envelope=ArtifactStoreV1(root).read(filename)
    if (envelope.as_of!=day or envelope.producer!='platform-consolidation-centers'
            or envelope.payload_schema!=SCHEMA or envelope.payload_schema_version!='1'):
        raise ArtifactIntegrityError('unsupported consolidation center snapshot identity')
    payload=envelope.payload
    if (payload.get('schema_version')!=SCHEMA or payload.get('as_of')!=day
            or payload.get('algorithm_version')!=CONTRACT or payload.get('window_contract')!=RECENT_CONTRACT):
        raise ArtifactIntegrityError('center payload schema/date/algorithm mismatch')
    return payload


def read_center_snapshot_v1(root: Path, *, as_of: str, known_at: str | None = None):
    """Exact-date facts; optional cutoff prevents later corrections leaking into replay."""
    day=date.fromisoformat(as_of).isoformat()
    cutoff=datetime.fromisoformat(known_at) if known_at is not None else None
    if cutoff is not None and cutoff.tzinfo is None:
        raise ValueError('known_at requires timezone')
    original=_read_center_file(root,f'{day}.json',day)
    if original is None:
        return None
    corrected=_read_center_file(root,f'{day}.warmup-restart-v1.json',day)
    if corrected is not None:
        reference=corrected.get('correction') or {}
        original_id=ArtifactStoreV1(root).read(f'{day}.json').artifact_id
        if (reference.get('schema_version')!='platform-center-correction.v1'
                or reference.get('original_artifact_id')!=original_id
                or reference.get('reason')!='pre_window_warmup_restart'
                or corrected.get('warmup_policy_version')!=WARMUP_POLICY
                or corrected['source']['input_rows_sha256']!=original['source']['input_rows_sha256']
                or datetime.fromisoformat(corrected['published_at'])<=datetime.fromisoformat(original['published_at'])):
            raise ArtifactIntegrityError('center correction identity/input/publication mismatch')
        if cutoff is None or datetime.fromisoformat(corrected['published_at'])<=cutoff:
            return corrected
    return original if cutoff is None or datetime.fromisoformat(original['published_at'])<=cutoff else None


def read_stock_center_v1(root: Path, *, as_of: str, stock_code: str):
    snapshot=read_center_snapshot_v1(root,as_of=as_of)
    status='snapshot_missing' if snapshot is None else 'stock_not_found'
    if snapshot is not None and stock_code in snapshot['stocks']:
        return snapshot['stocks'][stock_code]
    return dict(as_of=as_of,stock_code=stock_code,status=status,center=None,position='unknown')


def publish_center_facts_v1(*, market_db: Path, exchange_calendar: Path,
                            readiness_root: Path, output_root: Path,
                            as_of: str, published_at: str, historical_reconstruction: bool = False,
                            correct_warmup: bool = False):
    date.fromisoformat(as_of)
    clock=datetime.fromisoformat(published_at)
    if clock.tzinfo is None:
        raise ValueError('published_at requires timezone')
    local=clock.astimezone(ZoneInfo('Asia/Shanghai'))
    if local.date().isoformat()<as_of or (local.date().isoformat()==as_of and local.time()<time(15)):
        raise ValueError('centers may only be published after the as_of session closes')
    original=_read_center_file(output_root,f'{as_of}.json',as_of)
    if correct_warmup and original is None:
        raise ValueError('warmup correction requires an existing original snapshot')
    filename=f'{as_of}.warmup-restart-v1.json' if correct_warmup else f'{as_of}.json'
    existing=read_center_snapshot_v1(output_root,as_of=as_of)
    if existing is not None and (not correct_warmup or existing.get('correction')):
        return existing
    if correct_warmup and clock<=datetime.fromisoformat(original['published_at']):
        raise ValueError('correction must be published after original')
    calendar=json.loads(exchange_calendar.read_text())
    if calendar.get('schema_version')!='exchange-trading-calendar.v1':
        raise ValueError('unsupported exchange calendar')
    sessions=calendar['sessions']
    if as_of not in sessions:
        raise DataNotReadyError('as_of is not a published trading session')
    ready=ReadinessStoreV1(readiness_root).read_ready(bundle='v4-market-core',as_of=as_of)
    if ready is None or 'stock_daily' not in ready.required_datasets:
        raise DataNotReadyError('exact-day market publication is not ready')
    # Derive the input range using the same frozen measurement function.
    bounds=recent_center([],as_of,sessions=sessions)
    stocks={}
    digest=sha256()
    with sqlite3.connect(f'{market_db.resolve().as_uri()}?mode=ro',uri=True) as connection:
        connection.row_factory=sqlite3.Row
        connection.execute('BEGIN')
        metadata=dict(connection.execute('SELECT key,value FROM platform_metadata').fetchall())
        latest=connection.execute('SELECT MAX(trade_date) FROM stock_daily').fetchone()[0]
        valid_date = latest == as_of or (historical_reconstruction and latest is not None and latest > as_of)
        if not valid_date or metadata.get('producer_version')!=ready.producer_version:
            raise DataNotReadyError('market database does not match exact-day readiness')
        if not connection.execute('SELECT 1 FROM stock_daily WHERE trade_date=? LIMIT 1',(as_of,)).fetchone():
            raise DataNotReadyError('no daily rows for exact as_of date')
        cursor=connection.execute('SELECT stock_code,stock_name,trade_date,open,high,low,close,preclose,'
            'volume,amount,pct_chg FROM stock_daily WHERE trade_date>=? AND trade_date<=? '
            'ORDER BY stock_code,trade_date',(bounds['history_start'],as_of))
        for code, group in groupby(cursor,key=lambda row:row['stock_code']):
            rows=[dict(row) for row in group]
            if rows[-1]['trade_date']<bounds['window_start']:
                continue
            # This checksum identifies precisely the source rows actually used,
            # not an unrelated old/future portion of the mutable shared database.
            digest.update((json.dumps(rows,sort_keys=True,separators=(',',':'))+'\n').encode())
            result=recent_center(rows,as_of,sessions=sessions)
            close=rows[-1]['close'] if rows[-1]['trade_date']==as_of else None
            if not isinstance(close,(int,float)) or not isfinite(close):
                close=None
            stocks[code]=dict(as_of=as_of,stock_code=code,stock_name=rows[-1]['stock_name'],
                status=result['status'],close=close,position=result['position'],center=result['center'],
                center_history=[c for c in result['centers'] if c['points'][0]['date']>=bounds['window_start']],
                departures=[e for e in result['events'] if e['type']=='close_departure'],
                diagnostics={k:result[k] for k in ('invalid_at','missing_sessions','active_history_days',
                    'first_source_date','pending_extreme','warmup_resets','effective_history_start',
                    'warmup_sessions','warmup_policy_version') if k in result})
    if not stocks:
        raise DataNotReadyError('no stocks in recent published universe')
    counts=dict(Counter(item['status'] for item in stocks.values()))
    expected={'ready','no_confirmed_center','history_insufficient'}
    payload=dict(schema_version=SCHEMA,as_of=as_of,published_at=published_at,
        algorithm_version=CONTRACT,window_contract=RECENT_CONTRACT,
        warmup_policy_version=WARMUP_POLICY,
        window_start=bounds['window_start'],history_start=bounds['history_start'],
        universe='all published A-share codes with daily rows in the three-calendar-month window',
        source=dict(market_db=str(market_db.resolve()),metadata=metadata,
            input_rows_sha256=digest.hexdigest(),readiness_marker_id=ready.marker_id,
            exchange_calendar=str(exchange_calendar.resolve()),
            calendar_sha256=sha256(exchange_calendar.read_bytes()).hexdigest()),
        parameters=bounds['parameters'],stock_count=len(stocks),status_counts=counts,
        status='degraded' if any(s not in expected for s in counts) else 'ready',stocks=stocks)
    if historical_reconstruction:
        payload['input_semantics']='historical_reconstruction'
    if correct_warmup:
        if payload['source']['input_rows_sha256']!=original['source']['input_rows_sha256']:
            raise DataNotReadyError('warmup correction requires unchanged original input rows')
        payload['correction']=dict(schema_version='platform-center-correction.v1',
            reason='pre_window_warmup_restart',artifact_ref=str((output_root/filename).resolve()),
            original_artifact_id=ArtifactStoreV1(output_root).read(f'{as_of}.json').artifact_id,
            original_published_at=original['published_at'])
    envelope=ArtifactEnvelopeV1.create(artifact_type='consolidation-centers',
        producer='platform-consolidation-centers',producer_version=PRODUCER,
        payload_schema=SCHEMA,payload_schema_version='1',as_of=as_of,created_at=published_at,
        source_refs=(str(market_db.resolve()),str(exchange_calendar.resolve()),ready.marker_id),payload=payload)
    write_immutable_json_v1(output_root/filename,envelope.as_dict())
    return payload


def main():
    import argparse
    parser=argparse.ArgumentParser(description='Publish/read exact-date shared consolidation centers.')
    parser.add_argument('--output-root',type=Path,required=True)
    parser.add_argument('--as-of',required=True)
    parser.add_argument('--stock-code',help='Read one stock; does not run calculations or write files.')
    parser.add_argument('--market-db',type=Path)
    parser.add_argument('--exchange-calendar',type=Path)
    parser.add_argument('--readiness-root',type=Path)
    parser.add_argument('--historical-reconstruction',action='store_true',
        help='Reconstruct past facts using rows through as_of; retain the actual publication time.')
    parser.add_argument('--correct-warmup',action='store_true',
        help='Publish immutable warmup fix beside an existing snapshot; require identical source rows.')
    args=parser.parse_args()
    if args.stock_code:
        print(json.dumps(read_stock_center_v1(args.output_root,as_of=args.as_of,
            stock_code=args.stock_code),ensure_ascii=False))
        return
    if not all((args.market_db,args.exchange_calendar,args.readiness_root)):
        parser.error('publication requires market-db, exchange-calendar and readiness-root')
    result=publish_center_facts_v1(market_db=args.market_db,exchange_calendar=args.exchange_calendar,
        readiness_root=args.readiness_root,output_root=args.output_root,as_of=args.as_of,
        published_at=datetime.now(ZoneInfo('Asia/Shanghai')).isoformat(),
        historical_reconstruction=args.historical_reconstruction,correct_warmup=args.correct_warmup)
    print(json.dumps({k:result[k] for k in ('as_of','status','stock_count','status_counts')},ensure_ascii=False))


if __name__=='__main__':
    main()
