from __future__ import annotations

import unittest

from yifei_platform.eligibility import EligibilityPrimitiveV1, FactState, MarketSegment
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

    @staticmethod
    def _fact(code: str, *, is_st: bool | None) -> StockDailyFactV1:
        return StockDailyFactV1(
            stock_code=code, stock_name="测试", trade_date="2026-07-10",
            open=1.0, high=1.0, low=1.0, close=1.0, preclose=1.0,
            volume=100.0, amount=123456.0, pct_chg=0.0, turnover=1.2, is_st=is_st,
        )
