import unittest
from unittest.mock import patch

from yifei_platform.board_daily_ingestion import AkshareThsBoardDailyClientV1


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


if __name__ == '__main__':
    unittest.main()
