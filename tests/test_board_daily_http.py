import unittest
import json
from datetime import datetime, date
from unittest.mock import patch

from yifei_platform.board_daily_ingestion import AkshareThsBoardDailyClientV1, _parse_ths_today


class BoardHttpTests(unittest.TestCase):
    def setUp(self):
        self.client = AkshareThsBoardDailyClientV1()
        self.client._boards = {'881272':'汽车零部件'}

    def payload(self, row, code='881272'):
        import json
        return 'quotebridge_v4_line_bk_'+code+'_01_2026('+json.dumps({'data':row})+')'

    def test_original_vendor_values_and_window(self):
        text = self.payload('20260929,10,12,9,11,100,1000,,,,0;20260930,11,13,10,12,200,2000,,,,0')
        with patch('yifei_platform.concept_http._get',return_value=text) as get:
            rows = self.client.read_history('汽车零部件','20260930','20260930')
        self.assertEqual(1,len(rows))
        self.assertEqual({'日期':'2026-09-30','开盘价':11.,'最高价':13.,'最低价':10.,
            '收盘价':12.,'成交量':200.,'成交额':2000.},rows[0])
        self.assertIn('/bk_881272/01/2026.js',get.call_args.args[0])

    def test_wrong_identity_malformed_and_invalid_prices_rejected(self):
        for text in (self.payload('20260930,11,13,10,12,200,2000,,,,0','881157'),
                     self.payload('20260930,11,9,10,12,200,2000,,,,0'),
                     self.payload('20260930,11,13,10,NaN,200,2000,,,,0'),
                     'not JSONP'):
            with self.subTest(text=text),patch('yifei_platform.concept_http._get',return_value=text):
                with self.assertRaises(ValueError):
                    self.client.read_history('汽车零部件','20260930','20260930')

    def test_directory_identity_and_completeness(self):
        html=''.join(f'<a href="/thshy/detail/code/{881100+i}/">行业{i}</a>' for i in range(90))
        with patch('yifei_platform.concept_http._get',return_value=html):
            boards=self.client.list_boards()
        self.assertEqual(90,len(boards))
        with patch('yifei_platform.concept_http._get',return_value=html+'<a href="/thshy/detail/code/881100/">其他</a>'):
            with self.assertRaises(ValueError): self.client.list_boards()
        with patch('yifei_platform.concept_http._get',return_value='access denied'):
            with self.assertRaises(ValueError): self.client.list_boards()

    def test_latest_day_missing_from_year_file_uses_same_vendor_recent_file(self):
        annual=self.payload('20260929,10,12,9,11,100,1000,,,,0')
        recent=self.payload('20260929,10,12,9,11,100,1000,,,,0;20260930,11,13,10,12,200,2000,,,,0')
        recent=recent.replace('_2026(', '_last(')
        with patch('yifei_platform.concept_http._get',side_effect=[annual,recent]) as get:
            rows=self.client.read_history('汽车零部件','20260929','20260930')
        self.assertEqual(['2026-09-29','2026-09-30'],[r['日期'] for r in rows])
        self.assertEqual(12.,rows[-1]['收盘价'])
        self.assertIn('/bk_881272/01/last.js',get.call_args.args[0])

    def test_recent_file_wrong_identity_invalid_or_missing_latest_day_is_not_fabricated(self):
        annual=self.payload('20260929,10,12,9,11,100,1000,,,,0')
        for recent in (self.payload('20260930,11,13,10,12,200,2000,,,,0','881157').replace('_2026(', '_last('),
                       self.payload('20260930,11,9,10,12,200,2000,,,,0').replace('_2026(', '_last(')):
            with self.subTest(recent=recent),patch('yifei_platform.concept_http._get',side_effect=[annual,recent]):
                with self.assertRaises(ValueError):
                    self.client.read_history('汽车零部件','20260929','20260930')
        with patch('yifei_platform.concept_http._get',side_effect=[annual,annual.replace('_2026(', '_last(')]):
            rows=self.client.read_history('汽车零部件','20260929','20260930')
        self.assertEqual(['2026-09-29'],[r['日期'] for r in rows])

    def today(self, **changes):
        row = dict(name='汽车零部件', dt='1500', **{'1':'20260930','7':'11',
            '8':'13','9':'10','11':'12','13':200,'19':'2000'})
        row.update(changes)
        return 'quotebridge_v4_line_bk_881272_01_today('+json.dumps({'bk_881272':row})+')'

    def test_current_close_is_read_from_same_index_today_when_both_history_files_lag(self):
        annual=self.payload('20260929,10,12,9,11,100,1000,,,,0')
        recent=annual.replace('_2026(', '_last(')
        with patch('yifei_platform.board_daily_ingestion.datetime', wraps=datetime) as clock, \
             patch('yifei_platform.concept_http._get',side_effect=[annual,recent,self.today()]) as get:
            clock.now.return_value=datetime.fromisoformat('2026-09-30T18:00:00+08:00')
            rows=self.client.read_history('汽车零部件','20260929','20260930')
        self.assertEqual(['2026-09-29','2026-09-30'],[r['日期'] for r in rows])
        self.assertEqual(12,rows[-1]['收盘价'])
        self.assertIn('/bk_881272/01/today.js',get.call_args.args[0])

    def test_today_requires_exact_identity_date_final_time_and_valid_ohlc(self):
        for changes in ({'1':'20260929'}, {'name':'其他行业'}, {'dt':'1459'},
                        {'dt':'1560'}, {'8':'9'}, {'11':'NaN'}, {'13':-1}):
            with self.subTest(changes=changes),self.assertRaises(ValueError):
                _parse_ths_today(self.today(**changes),'881272','汽车零部件',date(2026,9,30))
        with self.assertRaises(ValueError):
            _parse_ths_today(self.today().replace('881272','881157'),
                             '881272','汽车零部件',date(2026,9,30))

    def test_today_is_not_used_for_past_dates_or_before_close(self):
        annual=self.payload('20260929,10,12,9,11,100,1000,,,,0')
        for now in ('2026-10-01T18:00:00+08:00','2026-09-30T14:59:00+08:00'):
            with self.subTest(now=now), \
                 patch('yifei_platform.board_daily_ingestion.datetime',wraps=datetime) as clock, \
                 patch('yifei_platform.concept_http._get',side_effect=[annual,annual.replace('_2026(', '_last(')]) as get:
                clock.now.return_value=datetime.fromisoformat(now)
                rows=self.client.read_history('汽车零部件','20260929','20260930')
            self.assertEqual(2,get.call_count)
            self.assertEqual(['2026-09-29'],[r['日期'] for r in rows])


if __name__ == '__main__':
    unittest.main()
