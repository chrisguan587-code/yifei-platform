from __future__ import annotations

import unittest

from yifei_platform.eligibility import (
    EligibilityPrimitiveV1,
    FactState,
    MarketSegment,
    derive_historical_st_v1,
)
from yifei_platform.market_data import StockDailyFactV1


class EligibilityContractTest(unittest.TestCase):
    def test_returns_facts_without_eligibility_decision_or_threshold(self) -> None:
        primitive = EligibilityPrimitiveV1(market_rules_version="cn-equity-segments.2026.v1")
        facts = primitive.evaluate(self._fact("688001", is_st=False), source_version="market.2026-07-10.v1")
        self.assertEqual(MarketSegment.STAR, facts.segment)
        self.assertEqual(FactState.FALSE, facts.st_state)
        self.assertEqual(FactState.UNKNOWN, facts.delisting_state)
        self.assertEqual(123456.0, facts.amount)
        self.assertFalse(hasattr(facts, "eligible"))
        self.assertFalse(hasattr(facts, "liquidity_ok"))

    def test_unknown_input_stays_unknown(self) -> None:
        facts = EligibilityPrimitiveV1(market_rules_version="rules.v1").evaluate(
            self._fact("600001", is_st=None), source_version="market.v1"
        )
        self.assertEqual(FactState.UNKNOWN, facts.st_state)

    def test_segments_cover_current_market_prefixes(self) -> None:
        primitive = EligibilityPrimitiveV1(market_rules_version="rules.v1")
        cases = {
            "600001": MarketSegment.MAINBOARD_SH,
            "000001": MarketSegment.MAINBOARD_SZ,
            "300001": MarketSegment.CHINEXT,
            "689001": MarketSegment.STAR,
            "920001": MarketSegment.BSE,
            "830001": MarketSegment.BSE,
            "400001": MarketSegment.OTHER,
        }
        for code, expected in cases.items():
            with self.subTest(code=code):
                result = primitive.evaluate(self._fact(code, is_st=False), source_version="market.v1")
                self.assertEqual(expected, result.segment)

    def test_historical_st_preserves_raw_and_uses_daily_name_prefix(self) -> None:
        polluted = self._fact("000001", is_st=False, stock_name="*ST测试")
        derived = derive_historical_st_v1(polluted)
        self.assertFalse(derived.raw_is_st)
        self.assertTrue(derived.name_st_signal)
        self.assertTrue(derived.derived_is_st)
        self.assertEqual(("historical_name_st_prefix",), derived.reason_codes)

        raw = derive_historical_st_v1(
            self._fact("000002", is_st=True, stock_name="正常名称")
        )
        self.assertTrue(raw.derived_is_st)
        self.assertEqual(("raw_is_st_true",), raw.reason_codes)

    @staticmethod
    def _fact(
        code: str, *, is_st: bool | None, stock_name: str = "测试"
    ) -> StockDailyFactV1:
        return StockDailyFactV1(
            stock_code=code, stock_name=stock_name, trade_date="2026-07-10",
            open=1.0, high=1.0, low=1.0, close=1.0, preclose=1.0,
            volume=100.0, amount=123456.0, pct_chg=0.0, turnover=1.2, is_st=is_st,
        )
