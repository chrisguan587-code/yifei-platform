from copy import deepcopy
from datetime import date, timedelta
import json
from pathlib import Path
import unittest

from yifei_platform.consolidation_center import trace, recent_center, _overlap, MIN_LEG_INTERVALS


class CenterAutomationTest(unittest.TestCase):
    def bars(self, prices):
        return [dict(trade_date=(date(2026,1,1)+timedelta(days=i)).isoformat(),
                     open=p,close=p,high=p+.01,low=p-.01,
                     preclose=prices[max(i-1,0)],amount=100,volume=10)
                for i,p in enumerate(prices)]

    def sample(self):
        return self.bars([10]*25+[11,12,13,14,13,12,11,10,9,10,11,12,13,12,11,10,
            9.5,10,11,12,12.5,12,11,10.5,10,10.5,11,11.5,12,11,10.5,10,9,8,7,8,9,10,9,8,7,6,7,8,9])

    def test_every_prefix_is_causal_and_input_unmodified(self):
        bars=self.sample()
        before=deepcopy(bars)
        full=trace(bars,bars[-1]['trade_date'])
        self.assertEqual(full['status'],'ready')
        for n in range(1,len(bars)+1):
            cutoff=bars[n-1]['trade_date']
            prefix=trace(bars[:n],cutoff)
            self.assertEqual(prefix,trace(bars,cutoff))
            self.assertEqual(prefix['events'],[e for e in full['events'] if e['known_at']<=cutoff])
            self.assertEqual(prefix['centers'],[c for c in full['centers'] if c['known_at']<=cutoff])
        self.assertEqual(bars,before)

    def test_same_scale_turns_are_confirmed_later_with_prior_volatility(self):
        bars=self.sample()
        full=trace(bars,bars[-1]['trade_date'])
        for p in full['pivots']:
            self.assertLess(p['atr_known_at'],p['date'])
            self.assertLess(p['date'],p['known_at'])
            self.assertGreater(p['reversal_amount'],0)
        for a,b in zip(full['pivots'],full['pivots'][1:]):
            self.assertNotEqual(a['kind'],b['kind'])
            self.assertGreaterEqual(b['index']-a['index'],MIN_LEG_INTERVALS)
        center=full['centers'][0]
        before=trace(bars,center['points'][-1]['date'])
        self.assertEqual(before['centers'],[])

    def test_latest_confirmed_three_legs_define_recent_center(self):
        bars=self.sample()
        result=trace(bars,bars[55]['trade_date'])
        latest=next(c for c in reversed(result['candidates']) if c['lower']<c['upper'])
        self.assertEqual(result['center']['points'],latest['points'])
        self.assertEqual(result['center']['lower'],latest['lower'])
        self.assertEqual(result['center']['upper'],latest['upper'])

    def test_new_center_requires_completed_major_waves_and_keeps_old_break(self):
        bars=self.sample()
        full=trace(bars,bars[-1]['trade_date'])
        self.assertGreaterEqual(len(full['centers']),3)
        old,new=full['centers'][-2:]
        self.assertLess(new['lower'],old['lower'])
        self.assertEqual(new['previous_center_id'],old['center_id'])
        self.assertTrue(any(e['type']=='close_departure' and e['direction']=='below'
                            and e['center_id']==old['center_id'] for e in full['events']))
        earlier=trace(bars,new['points'][-1]['date'])
        self.assertEqual(earlier['center'],old)

    def test_pending_last_leg_keeps_previous_completed_center(self):
        bars=self.sample()
        checked=0
        for n in range(1,len(bars)+1):
            result=trace(bars[:n],bars[n-1]['trade_date'])
            if result['center'] and result.get('pending_extreme'):
                latest=next(c for c in reversed(result['candidates']) if c['lower']<c['upper'])
                self.assertEqual(result['status'],'ready')
                self.assertEqual(result['center']['known_at'],latest['known_at'])
                self.assertNotIn(result['pending_extreme']['date'],
                                 [p['date'] for p in result['center']['points']])
                checked+=1
        self.assertGreater(checked,0)

    def test_nonoverlapping_latest_three_legs_keep_older_center_and_break(self):
        # A long monotone descent/rebounds produces three completed legs with no
        # shared range. It must not erase the older valid zone.
        prices=[30]*25
        for target in (40,30,38,28,34,20,25,10,15,3,8):
            start=prices[-1]
            prices.extend(start+(target-start)*i/5 for i in range(1,6))
        bars=self.bars(prices)
        found=False
        for n in range(1,len(bars)+1):
            result=trace(bars[:n],bars[n-1]['trade_date'])
            if result['center'] and result['candidates'][-1]['lower']>=result['candidates'][-1]['upper']:
                latest=next(c for c in reversed(result['candidates']) if c['lower']<c['upper'])
                self.assertEqual(result['center']['known_at'],latest['known_at'])
                self.assertEqual(result['status'],'ready')
                self.assertTrue(any(e['type']=='close_departure' for e in result['events']))
                found=True
        self.assertTrue(found)

    def test_overlap_uses_full_leg_ranges_and_not_only_endpoint_prices(self):
        bars=self.bars([10,8,11,13,12,9,10,11,12,14])
        points=[dict(index=i,date=bars[i]['trade_date']) for i in (0,3,6,9)]
        overlap=_overlap(points,bars,'2026-02-01')
        self.assertEqual(overlap['lower'],9.99)
        self.assertEqual(overlap['upper'],13.01)

    def test_price_chain_fault_is_unknown_not_new_safe_platform(self):
        bars=self.sample()
        cutoff=bars[-2]['trade_date']
        before=trace(bars,cutoff)
        bars[-1]['preclose']=1
        self.assertEqual(trace(bars,cutoff),before)
        broken=trace(bars,bars[-1]['trade_date'])
        self.assertEqual(broken['status'],'price_lineage_invalid')
        self.assertIsNone(broken['center'])
        self.assertEqual(broken['position'],'unknown')

    def test_actual_pause_has_close_but_cannot_confirm_more_turns(self):
        bars=self.sample()
        old=trace(bars,bars[-1]['trade_date'])
        bar=bars[-1]
        day=(date.fromisoformat(bar['trade_date'])+timedelta(days=1)).isoformat()
        pause=dict(trade_date=day,close=bar['close'],preclose=bar['close'],high=None,low=None,
                   open=None,volume=0,amount=0,pct_chg=0)
        newer=trace([*bars,pause],day)
        self.assertEqual(newer['center'],old['center'])
        self.assertEqual(newer['pivots'],old['pivots'])
        self.assertEqual(newer['active_history_days'],old['active_history_days'])
        self.assertEqual(trace([*bars,dict(pause,close=1)],day)['status'],'invalid_nontrading_bar')

    def test_missing_duplicate_short_or_invalid_daily_bars_not_safe(self):
        bars=self.sample()
        self.assertEqual(trace(bars[:10],bars[9]['trade_date'])['status'],'history_insufficient')
        self.assertEqual(trace(bars,'2026-12-31')['status'],'as_of_close_missing')
        self.assertEqual(trace([*bars,bars[-1]],bars[-1]['trade_date'])['status'],'duplicate_daily_rows')
        bars[-1]['high']=0
        self.assertEqual(trace(bars,bars[-1]['trade_date'])['status'],'invalid_price')

    def test_published_session_gap_is_not_a_price_basis_event(self):
        bars=self.sample()
        sessions=[r['trade_date'] for r in bars]
        missing=bars[30]['trade_date']
        incomplete=bars[:30]+bars[31:]
        result=trace(incomplete,bars[-1]['trade_date'],sessions=sessions)
        self.assertEqual(result['status'],'source_sessions_missing')
        self.assertEqual(result['missing_sessions'],[missing])
        self.assertIsNone(result['center'])
        self.assertEqual(result['position'],'unknown')
        cutoff=bars[29]['trade_date']
        self.assertEqual(trace(incomplete,cutoff,sessions=sessions),trace(bars,cutoff,sessions=sessions))
        # An actual non-session is not a missing daily bar.
        self.assertNotEqual(trace(incomplete,bars[-1]['trade_date'],
            sessions=[day for day in sessions if day!=missing])['status'],'source_sessions_missing')

    def confirmed_cases(self):
        return json.loads((Path(__file__).parent/'fixtures/recent_center_confirmed_20261021.json').read_text())

    def test_user_confirmed_recent_centers_and_positions(self):
        fixture=self.confirmed_cases()
        for case in fixture['cases']:
            with self.subTest(stock=case['stock_name']):
                result=recent_center(case['rows'],fixture['as_of'],sessions=fixture['sessions'])
                self.assertEqual(result['status'],'ready')
                self.assertEqual(result['window_start'],'2026-07-21')
                self.assertEqual(result['warmup_sessions'],20)
                self.assertEqual([result['center']['lower'],result['center']['upper']],case['expected_bounds'])
                self.assertEqual(result['position'],case['expected_position'])
                self.assertTrue(all(p['date']>=result['window_start'] for p in result['center']['points']))

    def test_old_missing_date_and_future_rows_do_not_block_recent_window(self):
        fixture=self.confirmed_cases();case=fixture['cases'][0]
        expected=recent_center(case['rows'],fixture['as_of'],sessions=fixture['sessions'])
        # Old corruption/gaps are outside both the center window and volatility warm-up.
        rows=[dict(trade_date='2026-05-26',close=0),*case['rows'],dict(trade_date='2026-10-22',close=0)]
        sessions=['2026-05-26','2026-05-27',*fixture['sessions'],'2026-10-22']
        self.assertEqual(recent_center(rows,fixture['as_of'],sessions=sessions),expected)
        incomplete=[r for r in case['rows'] if r['trade_date']!='2026-09-03']
        self.assertEqual(recent_center(incomplete,fixture['as_of'],sessions=sessions)['status'],'source_sessions_missing')

    def test_three_calendar_month_boundary_and_no_empty_daily_crash(self):
        result=recent_center([], '2026-05-31',sessions=[])
        self.assertEqual(result['window_start'],'2026-02-28')
        self.assertEqual(result['status'],'history_insufficient')


if __name__=='__main__':
    unittest.main()
