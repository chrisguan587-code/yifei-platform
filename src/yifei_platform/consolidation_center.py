"""Neutral daily-bar consolidation centers, with causal confirmation timestamps."""
from calendar import monthrange
from copy import deepcopy
from datetime import date
from math import isfinite, isclose
from statistics import mean

from .outcomes import PriceLineageGuardV2


CONTRACT = 'platform-major-wave-center.v1'
ATR_DAYS = 20
REVERSAL_ATR = 2.0
MIN_LEG_INTERVALS = 3
RECENT_CONTRACT = 'platform-recent-center.three-calendar-months.v1'


def recent_center(rows, as_of, *, sessions):
    """Compute the latest confirmed three-leg overlap within three calendar months."""
    cutoff=date.fromisoformat(as_of)
    month_index=cutoff.year*12+cutoff.month-1-3
    year,month=month_index//12,month_index%12+1
    window_start=date(year,month,min(cutoff.day,monthrange(year,month)[1])).isoformat()
    before=sorted(day for day in sessions if day<window_start)
    # The extra 20 sessions initialize volatility only; a center cannot start there.
    history_start=before[-ATR_DAYS] if len(before)>=ATR_DAYS else before[0] if before else window_start
    bars=[r for r in rows if history_start<=r['trade_date']<=as_of]
    result=trace(bars,as_of,sessions=sessions)
    result.update(window_contract=RECENT_CONTRACT,window_start=window_start,
                  history_start=history_start,warmup_sessions=min(len(before),ATR_DAYS))
    if not result['daily']:
        return result
    if result['status'] not in ('ready','no_confirmed_center','history_insufficient'):
        return result
    eligible=[c for c in result['centers'] if c['points'][0]['date']>=window_start]
    result['center']=eligible[-1] if eligible else None
    result['position']=_position(result['daily'][-1]['close'],result['center'])
    result['status']='ready' if result['center'] else 'history_insufficient' if result['status']=='history_insufficient' else 'no_confirmed_center'
    return result


def _position(close, center):
    if center is None:
        return 'unknown'
    return 'below' if close < center['lower'] else 'above' if close > center['upper'] else 'inside'


def _overlap(points, active, known_at):
    legs = []
    for a,b in zip(points, points[1:]):
        rows = active[a['index']:b['index']+1]
        legs.append(dict(start=a['date'], end=b['date'],
                         low=min(r['low'] for r in rows), high=max(r['high'] for r in rows)))
    lower,upper = max(r['low'] for r in legs),min(r['high'] for r in legs)
    return dict(known_at=known_at, lower=lower, upper=upper, points=deepcopy(points), legs=legs)


def trace(rows, as_of, *, sessions=None):
    """One forward pass. Parameters fixed before the first real-case inspection."""
    rows = sorted((r for r in rows if r['trade_date'] <= as_of), key=lambda r:r['trade_date'])
    result = dict(contract=CONTRACT, as_of=as_of, status='history_insufficient',
        center=None, position='unknown', pivots=[], candidates=[], centers=[], events=[], daily=[],
        parameters=dict(atr_days=ATR_DAYS, reversal_atr=REVERSAL_ATR,
                        min_leg_intervals=MIN_LEG_INTERVALS),
        boundaries=dict(strict_chan_theory=False,
                        uses_future_bars=False, same_bar_reversal_not_assumed=True))
    if not rows:
        return result
    if len({r['trade_date'] for r in rows}) != len(rows):
        result['status']='duplicate_daily_rows'
        return result
    if rows[-1]['trade_date'] != as_of:
        result['status']='as_of_close_missing'
        return result
    if sessions is not None:
        supplied={r['trade_date'] for r in rows}
        missing=sorted(day for day in sessions if rows[0]['trade_date']<=day<=as_of and day not in supplied)
        if missing:
            result.update(status='source_sessions_missing', missing_sessions=missing,
                          invalid_at=missing[0])
            return result
    guard = PriceLineageGuardV2()
    previous = None
    active, ranges = [], []
    direction, pending_high, pending_low = None,None,None
    departures = {}
    for bar in rows:
        day = bar['trade_date']
        close = bar.get('close')
        if not isinstance(close,(int,float)) or not isfinite(close) or close <= 0:
            result.update(status='invalid_price', position='unknown', center=None, invalid_at=day)
            return result
        if previous and guard.check(previous_close=previous['close'], current_preclose=bar.get('preclose'),
                current_close=close, current_pct_chg=bar.get('pct_chg')).reason_code:
            result.update(status='price_lineage_invalid', position='unknown', center=None, invalid_at=day)
            return result
        traded = isinstance(bar.get('amount'),(int,float)) and bar['amount'] > 0
        if not traded:
            if (bar.get('amount') != 0 or bar.get('volume') != 0 or bar.get('high') is not None
                    or bar.get('low') is not None or not previous
                    or not isclose(close, previous['close'], abs_tol=1e-8)):
                result.update(status='invalid_nontrading_bar', position='unknown', center=None, invalid_at=day)
                return result
        elif any(not isinstance(bar.get(k),(int,float)) or not isfinite(bar[k]) or bar[k]<=0
                 for k in ('high','low')) or not bar['low'] <= close <= bar['high']:
            result.update(status='invalid_price', position='unknown', center=None, invalid_at=day)
            return result

        old_center = result['center']
        old_position = _position(close, old_center)
        if old_center and old_position in ('below','above') and old_position not in departures:
            departures[old_position]=day
            result['events'].append(dict(type='close_departure',known_at=day,
                direction=old_position, center_id=old_center['center_id'],close=close))
        confirmed = None
        if traded:
            index=len(active)
            active.append(bar)
            threshold=REVERSAL_ATR*mean(ranges[-ATR_DAYS:]) if len(ranges)>=ATR_DAYS else None
            if threshold is not None and threshold > 0:
                def point(kind):
                    return dict(date=day,index=index,kind=kind,price=bar[kind],
                                reversal_amount=threshold,atr_known_at=previous['trade_date'])
                high_extended = pending_high is None or bar['high'] > pending_high['price']
                low_extended = pending_low is None or bar['low'] < pending_low['price']
                eligible=not result['pivots'] or index-result['pivots'][-1]['index']>=MIN_LEG_INTERVALS
                if direction in (None,'up') and high_extended and eligible:
                    pending_high=point('high')
                if direction in (None,'down') and low_extended and eligible:
                    pending_low=point('low')
                if direction is None:
                    up = (index > pending_low['index'] and close-pending_low['price']>=pending_low['reversal_amount'])
                    down = (index > pending_high['index'] and pending_high['price']-close>=pending_high['reversal_amount'])
                    if up != down:
                        confirmed=deepcopy(pending_low if up else pending_high)
                elif direction=='up':
                    if (pending_high and not high_extended and index > pending_high['index']
                            and pending_high['index']-result['pivots'][-1]['index']>=MIN_LEG_INTERVALS
                            and pending_high['price']-close>=pending_high['reversal_amount']):
                        confirmed=deepcopy(pending_high)
                elif (pending_low and not low_extended and index > pending_low['index']
                        and pending_low['index']-result['pivots'][-1]['index']>=MIN_LEG_INTERVALS
                        and close-pending_low['price']>=pending_low['reversal_amount']):
                    confirmed=deepcopy(pending_low)
                if confirmed:
                    confirmed['known_at']=day
                    result['pivots'].append(confirmed)
                    result['events'].append(dict(type='major_turn_confirmed',known_at=day,point=deepcopy(confirmed)))
                    direction='up' if confirmed['kind']=='low' else 'down'
                    # Ignore extremes too close to the previous pivot; they must not
                    # permanently prevent a later valid turn from being confirmed.
                    tail=active[confirmed['index']+MIN_LEG_INTERVALS:]
                    kind='high' if direction=='up' else 'low'
                    pending=None
                    if tail:
                        extreme=max(tail,key=lambda r:r[kind]) if kind=='high' else min(tail,key=lambda r:r[kind])
                        extreme_index=next(i for i,r in enumerate(active) if r['trade_date']==extreme['trade_date'])
                        prior_ranges=ranges[:extreme_index]
                        pending=dict(date=extreme['trade_date'],index=extreme_index,kind=kind,price=extreme[kind],
                            reversal_amount=REVERSAL_ATR*mean(prior_ranges[-ATR_DAYS:]),
                            atr_known_at=active[extreme_index-1]['trade_date'])
                    pending_high=pending if kind=='high' else None
                    pending_low=pending if kind=='low' else None
            prev_close=previous['close'] if previous else close
            ranges.append(max(bar['high']-bar['low'],abs(bar['high']-prev_close),abs(bar['low']-prev_close)))

        if confirmed and len(result['pivots'])>=4:
            candidate=_overlap(result['pivots'][-4:],active,day)
            result['candidates'].append(candidate)
            reason='no_common_interval'
            # Each confirmed turn completes one three-leg window. Retaining the last
            # valid window is equivalent to searching backwards, ignoring a pending leg.
            accept=candidate['lower'] < candidate['upper']
            if accept:
                reason='latest_confirmed_three_leg_overlap'
                center=dict(deepcopy(candidate),center_id=len(result['centers'])+1,
                            previous_center_id=old_center['center_id'] if old_center else None)
                result['centers'].append(center)
                result['center']=center
                departures={}
            result['events'].append(dict(type='center_candidate',known_at=day,accepted=accept,
                                         reason=reason,candidate=deepcopy(candidate)))
        result['daily'].append(dict(trade_date=day,close=close,position=_position(close,result['center']),
            center_id=result['center']['center_id'] if result['center'] else None))
        previous=bar
    result.update(status='ready' if result['center'] else 'history_insufficient' if len(active)<=ATR_DAYS else 'no_confirmed_center',
                  position=result['daily'][-1]['position'],pending_extreme=deepcopy(
                      pending_high if direction=='up' else pending_low if direction=='down' else None),
                  active_history_days=len(active), first_source_date=rows[0]['trade_date'])
    pending=result['pending_extreme']
    if (len(result['pivots'])>=3 and pending
            and pending['index']-result['pivots'][-1]['index']>=MIN_LEG_INTERVALS):
        tentative=_overlap([*result['pivots'][-3:],dict(pending,known_at=None)],active,None)
        if tentative['lower']<tentative['upper']:
            result['tentative_center_not_usable']=tentative
    return result
