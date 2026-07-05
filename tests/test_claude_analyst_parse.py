from __future__ import annotations

import unittest

from test_claude_analyst_support import import_claude_analyst


class ClaudeAnalystParseTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.mod = import_claude_analyst()

    def test_parse_extracts_json_from_fenced_block(self):
        raw = """
설명 텍스트
```json
{
  "signal": "buy",
  "confidence": 0.72,
  "invest_ratio": 0.4,
  "target_price": 82000,
  "stop_loss": 76000,
  "reasoning": "돌파 확인",
  "long_term_reason": ""
}
```
추가 텍스트
"""
        result = self.mod._BaseAnalyst._parse(raw)
        self.assertEqual(result["signal"], "buy")
        self.assertEqual(result["confidence"], 0.72)
        self.assertEqual(result["target_price"], 82000)

    def test_parse_handles_braces_inside_reasoning_string(self):
        raw = """
prefix
{
  "signal": "sell",
  "confidence": 0.63,
  "invest_ratio": 0.2,
  "target_price": 0,
  "stop_loss": 0,
  "reasoning": "패턴 {failed breakout} 확인, 손실 회피",
  "long_term_reason": ""
}
suffix
"""
        result = self.mod._BaseAnalyst._parse(raw)
        self.assertEqual(result["signal"], "sell")
        self.assertIn("{failed breakout}", result["reasoning"])

    def test_parse_recovers_partial_fields_from_broken_json(self):
        raw = """
{
  "signal": "hold",
  "confidence": 0.38,
  "reasoning": "json closing quote missing
"""
        result = self.mod._BaseAnalyst._parse(raw)
        self.assertEqual(result["signal"], "hold")
        self.assertEqual(result["confidence"], 0.38)
        self.assertIn("JSON 복구", result["reasoning"])

    def test_parse_returns_hold_when_no_json_object_exists(self):
        raw = "no structured output available"
        result = self.mod._BaseAnalyst._parse(raw)
        self.assertEqual(result["signal"], "hold")
        self.assertEqual(result["confidence"], 0)
        self.assertIn("regex", result["reasoning"])

    def test_parse_normalizes_null_numeric_fields_to_zero(self):
        #: LLM이 stop_loss/target_price/confidence를 null로 반환하면
        # 하류 _judge에서 None>0 / None*w TypeError 발생 (Dell 크래시). 파서가 0으로 정규화해야 함.
        raw = """
{
  "signal": "buy",
  "confidence": null,
  "invest_ratio": null,
  "target_price": null,
  "stop_loss": null,
  "reasoning": "손절가 미산정",
  "long_term_reason": ""
}
"""
        result = self.mod._BaseAnalyst._parse(raw)
        self.assertEqual(result["signal"], "buy")
        for field in ("confidence", "invest_ratio", "target_price", "stop_loss"):
            self.assertEqual(result[field], 0, f"{field} 가 None→0 으로 정규화돼야 함")


if __name__ == "__main__":
    unittest.main()
