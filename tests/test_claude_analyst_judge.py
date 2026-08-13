from __future__ import annotations

import unittest
from unittest.mock import Mock, patch

from test_claude_analyst_support import make_claude_analyst


def _result(signal: str, confidence: float = 0.5, invest_ratio: float = 0.3,
            reasoning: str = "ok", target_price: int = 100, stop_loss: int = 90,
            long_term_reason: str = "", alternative_pick: str = "") -> dict:
    return {
        "signal": signal,
        "confidence": confidence,
        "invest_ratio": invest_ratio,
        "target_price": target_price,
        "stop_loss": stop_loss,
        "reasoning": reasoning,
        "long_term_reason": long_term_reason,
        "alternative_pick": alternative_pick,
    }


class ClaudeAnalystJudgeTests(unittest.TestCase):
    def setUp(self):
        self.analyst, self.mod = make_claude_analyst()

    def test_judge_adds_unanimous_bonus(self):
        results = {
            "fundamental": _result("buy"),
            "sentiment": _result("buy"),
            "quant": _result("buy"),
            "generalist": _result("buy"),
        }
        final = self.analyst._judge(results, indicators={})
        self.assertEqual(final["signal"], "buy")
        self.assertEqual(final["confidence"], 0.7)
        self.assertIn("만장일치 +20%", final["reasoning"])

    def test_judge_two_analyst_unanimity_gets_half_bonus(self):
        """회귀: quick 모드(2명)의 2/2 만장일치가 +20%p를 받아 확신도가 부풀던 문제
        (실측 0.74→0.94, 7일간 8회). 표본 2개 합의는 +10%만."""
        results = {
            "fundamental": _result("buy", confidence=0.6),
            "sentiment": _result("buy", confidence=0.6),
            "quant": _result("hold", reasoning="미호출"),
            "generalist": _result("hold", reasoning="미호출"),
        }
        final = self.analyst._judge(results, indicators={})
        self.assertEqual(final["signal"], "buy")
        self.assertEqual(final["confidence"], 0.7)  # 0.6 + 0.1 (not 0.8)
        self.assertIn("만장일치(2인) +10%", final["reasoning"])

    def test_judge_adds_majority_bonus_without_opposition(self):
        results = {
            "fundamental": _result("buy"),
            "sentiment": _result("buy"),
            "quant": _result("buy"),
            "generalist": _result("hold"),
        }
        final = self.analyst._judge(results, indicators={})
        self.assertEqual(final["signal"], "buy")
        self.assertEqual(final["confidence"], 0.65)
        self.assertIn("다수결 매수 +15%", final["reasoning"])

    def test_judge_applies_split_penalty_on_true_division(self):
        results = {
            "fundamental": _result("buy"),
            "sentiment": _result("buy"),
            "quant": _result("sell"),
            "generalist": _result("sell"),
        }
        final = self.analyst._judge(results, indicators={})
        self.assertEqual(final["confidence"], 0.3)
        self.assertIn("의견 분열 -40%", final["reasoning"])

    def test_long_term_buy_downgrades_without_reason(self):
        results = {
            "fundamental": _result("long_term_buy"),
            "sentiment": _result("long_term_buy"),
            "quant": _result("hold"),
            "generalist": _result("hold"),
        }
        final = self.analyst._judge(results, indicators={})
        self.assertEqual(final["signal"], "buy")

    def test_collect_alternatives_deduplicates_and_preserves_order(self):
        results = {
            "fundamental": _result("hold", alternative_pick="005930 삼성전자"),
            "sentiment": _result("hold", alternative_pick="005930 삼성전자"),
            "quant": _result("hold", alternative_pick="NVDA NVIDIA"),
            "generalist": _result("hold", alternative_pick="  NVDA NVIDIA  "),
        }
        picks = self.mod.ClaudeAnalyst._collect_alternatives(results, self.analyst.ROLES)
        self.assertEqual(picks, ["005930 삼성전자", "NVDA NVIDIA"])

    def test_judge_survives_none_numeric_fields(self):
        #: 애널리스트 결과에 stop_loss/target_price/confidence/invest_ratio가
        # None으로 들어와도 _judge가 TypeError 없이 판정해야 함 (Dell '_judge' 크래시 재현).
        def _none_result(signal):
            return {
                "signal": signal,
                "confidence": None,
                "invest_ratio": None,
                "target_price": None,
                "stop_loss": None,
                "reasoning": "ok",
                "long_term_reason": "",
                "alternative_pick": "",
            }

        results = {
            "fundamental": _none_result("buy"),
            "sentiment": _none_result("buy"),
            "quant": _result("hold"),
            "generalist": _none_result("sell"),
        }
        final = self.analyst._judge(results, indicators={})  # 크래시하면 테스트 실패
        self.assertIn(final["signal"], ("buy", "sell", "hold", "long_term_buy"))
        self.assertIsInstance(final["target_price"], int)
        self.assertIsInstance(final["stop_loss"], int)


class ClaudeAnalystPerformanceTests(unittest.TestCase):
    def test_record_result_keeps_weight_unchanged_below_ten_trades(self):
        perf = {
            role: {"correct": 4, "wrong": 4, "total": 8, "weight": 1.0}
            for role in ("fundamental", "sentiment", "quant", "generalist")
        }
        analyst, mod = make_claude_analyst(perf=perf)
        predictions = {role: {"signal": "buy"} for role in analyst.ROLES}

        with patch.object(mod, "_save_perf", return_value=None):
            analyst.record_result(predictions, actual_pnl=4.0, stock_code="005930", stock_name="삼성전자")

        for role in analyst.ROLES:
            self.assertEqual(analyst._perf[role]["weight"], 1.0)
            self.assertEqual(analyst._perf[role]["total"], 9)

    def test_record_result_updates_weight_with_bayesian_prior_at_ten(self):
        perf = {
            role: {"correct": 9, "wrong": 0, "total": 9, "weight": 1.0}
            for role in ("fundamental", "sentiment", "quant", "generalist")
        }
        analyst, mod = make_claude_analyst(perf=perf)
        predictions = {role: {"signal": "buy"} for role in analyst.ROLES}

        with patch.object(mod, "_save_perf", return_value=None):
            analyst.record_result(predictions, actual_pnl=5.0, stock_code="005930", stock_name="삼성전자")

        expected_weight = 0.5 + (((10 + 2.5) / 15) * 1.5)
        for role in analyst.ROLES:
            self.assertAlmostEqual(analyst._perf[role]["weight"], expected_weight, places=3)
            self.assertEqual(analyst._perf[role]["total"], 10)

    def test_record_result_records_memory_mistake_on_large_loss(self):
        memory = Mock(record_outcome=Mock(), add_mistake=Mock())
        analyst, mod = make_claude_analyst(memory=memory)
        predictions = {
            "fundamental": {"signal": "buy"},
            "sentiment": {"signal": "long_term_buy"},
            "quant": {"signal": "sell"},
            "generalist": {"signal": "hold"},
        }

        with patch.object(mod, "_save_perf", return_value=None):
            analyst.record_result(predictions, actual_pnl=-4.5, stock_code="005930", stock_name="삼성전자")

        memory.record_outcome.assert_called_once_with("005930", -4.5)
        memory.add_mistake.assert_called_once()
        mistake_text = memory.add_mistake.call_args.args[1]
        self.assertIn("삼성전자 -4.5% 손실", mistake_text)
        self.assertIn("펀더멘털", mistake_text)
        self.assertIn("센티멘트", mistake_text)


if __name__ == "__main__":
    unittest.main()
