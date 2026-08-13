from __future__ import annotations

import importlib
import os
import tempfile
import unittest
from unittest.mock import Mock, patch

# OS 중립 임시 경로 (Windows엔 /tmp 없음 — 하드코딩 시 쓰기 실패로 cooldown 미적용 → CI 실패)
_TMP = tempfile.gettempdir()
_CODEX_CD = os.path.join(_TMP, "_test_codex_cd.txt")


def _make_client(**flags):
    """__init__(=CLI 탐지 subprocess)를 우회하고 플래그만 세팅한 ClaudeClient."""
    mod = importlib.import_module("zusik.clients.claude_client")
    c = mod.ClaudeClient.__new__(mod.ClaudeClient)
    c._has_claude = flags.get("claude", True)
    c._has_codex = flags.get("codex", True)
    c._has_agy = flags.get("agy", False)
    c._has_local = flags.get("local", False)
    c._primary_provider = flags.get("primary", "codex")
    c._codex_model = flags.get("codex_model", "gpt-5.6-sol")
    c._codex_effort = flags.get("codex_effort", "low")
    c._claude_effort = flags.get("claude_effort", "low")
    # 테스트 격리: cooldown 파일을 임시 경로로
    c._CODEX_COOLDOWN_FILE = flags.get("codex_file", _CODEX_CD)
    if os.path.exists(c._CODEX_COOLDOWN_FILE):
        os.remove(c._CODEX_COOLDOWN_FILE)
    return c, mod


class CodexCooldownTests(unittest.TestCase):
    def tearDown(self):
        if os.path.exists(_CODEX_CD):
            os.remove(_CODEX_CD)

    def test_cooldown_roundtrip(self):
        c, _ = _make_client()
        self.assertFalse(c._is_codex_cooldown())
        c._set_codex_cooldown(15.0)
        self.assertTrue(c._is_codex_cooldown())


class CodexPrimaryRoutingTests(unittest.TestCase):
    def tearDown(self):
        if os.path.exists(_CODEX_CD):
            os.remove(_CODEX_CD)

    def test_auto_defaults_to_codex(self):
        mod = importlib.import_module("zusik.clients.claude_client")
        with patch.object(mod, "_read_merged_cfg", return_value={}), \
                patch.object(mod, "_cli_available", return_value=False):
            c = mod.ClaudeClient()
        self.assertEqual(c._primary_provider, "codex")

    def test_codex_is_first_for_every_tier(self):
        c, mod = _make_client(claude=True, codex=True, agy=True)
        c._run_codex = Mock(return_value='{"signal":"buy","confidence":0.8}')
        c._run_claude = Mock(side_effect=AssertionError("Codex 성공 전에 Claude 호출"))
        c._run_agy = Mock(side_effect=AssertionError("Codex 성공 전에 agy 호출"))

        tiers = (
            lambda: c._call_easy("p"),
            lambda: c._call_medium("p"),
            lambda: c._call_hard("p", False),
            lambda: c._call_premium("p", False),
            lambda: c._call_balanced("p", False),
            lambda: c._call_cheap_web("p", False),
        )
        with patch.object(mod, "_check_limit", return_value=True), \
                patch.object(mod, "_record_call"), \
                patch.object(c, "_is_codex_cooldown", return_value=False):
            for call_tier in tiers:
                c._run_codex.reset_mock()
                out = call_tier()
                self.assertIn("buy", out)
                c._run_codex.assert_called_once()

    def test_hard_falls_back_to_claude_after_codex_failure(self):
        c, mod = _make_client(claude=True, codex=True, agy=False)
        order = []
        c._run_codex = lambda *a, **k: (
            order.append("codex") or '{"reasoning":"오류"}'
        )
        c._run_claude = lambda *a, **k: (
            order.append("claude") or '{"signal":"hold","confidence":0.6}'
        )
        with patch.object(mod, "_check_limit", return_value=True), \
                patch.object(mod, "_record_call"), \
                patch.object(c, "_is_codex_cooldown", return_value=False):
            out = c._call_hard("p", False)
        self.assertEqual(order[:2], ["codex", "claude"])
        self.assertIn("hold", out)

    def test_explicit_claude_primary_remains_supported(self):
        c, mod = _make_client(claude=True, codex=True, agy=False, primary="claude")
        c._run_claude = Mock(return_value='{"signal":"hold","confidence":0.6}')
        c._run_codex = Mock(side_effect=AssertionError("명시적 Claude 주 설정 무시"))
        with patch.object(mod, "_check_limit", return_value=True), \
                patch.object(mod, "_record_call"), \
                patch.object(c, "_is_codex_cooldown", return_value=False):
            out = c._call_hard("p", False)
        self.assertIn("hold", out)
        c._run_claude.assert_called_once()

    def test_run_codex_is_ephemeral_read_only_and_isolated(self):
        c, _ = _make_client()
        c._exec = Mock(return_value='{"signal":"hold"}')
        out = c._run_codex("분석", use_web_search=True)

        cmd = c._exec.call_args.args[0]
        self.assertEqual(out, '{"signal":"hold"}')
        self.assertNotIn("--full-auto", cmd)
        self.assertIn("--ephemeral", cmd)
        self.assertEqual(cmd[cmd.index("--sandbox") + 1], "read-only")
        self.assertIn("--ignore-user-config", cmd)
        self.assertIn("--ignore-rules", cmd)
        self.assertEqual(cmd[cmd.index("--model") + 1], "gpt-5.6-sol")
        self.assertIn("model_reasoning_effort=low", cmd)
        workdir = cmd[cmd.index("-C") + 1]
        self.assertFalse(os.path.exists(workdir), "격리용 임시 디렉터리는 호출 후 정리")
        self.assertEqual(cmd[-1], "분석")
        self.assertIn("features.web_search=true", cmd)

    def test_run_claude_uses_latest_alias_and_isolated_print_contract(self):
        c, _ = _make_client()
        c._exec = Mock(return_value='{"signal":"hold"}')
        out = c._run_claude("분석", "sonnet", use_web_search=False)

        cmd = c._exec.call_args.args[0]
        self.assertEqual(out, '{"signal":"hold"}')
        self.assertEqual(cmd[cmd.index("--model") + 1], "sonnet")
        self.assertEqual(cmd[cmd.index("--effort") + 1], "low")
        self.assertEqual(cmd[cmd.index("--output-format") + 1], "text")
        self.assertIn("--no-session-persistence", cmd)
        self.assertIn("--safe-mode", cmd)
        self.assertEqual(cmd[cmd.index("--tools") + 1], "")


class CodexFallbackAndCooldownTests(unittest.TestCase):
    def tearDown(self):
        if os.path.exists(_CODEX_CD):
            os.remove(_CODEX_CD)

    def test_balanced_skips_codex_when_cooldown(self):
        #: codex 세션 만료로 cooldown이면 balanced 라우팅이
        # codex를 건드리지 않고 claude로 가야 함 (죽은 CLI 두드리기 방지).
        #: DAILY_LIMITS Claude=0 변경에 대비해 _check_limit 패치로 격리.
        c, mod = _make_client(claude=True, codex=True)
        c._set_codex_cooldown(15.0)

        def _boom(*a, **k):
            raise AssertionError("cooldown 중에는 codex를 호출하면 안 됨")

        c._run_codex = _boom
        c._run_claude = lambda prompt, model, web: '{"signal":"buy","confidence":0.6}'
        with patch.object(mod, "_check_limit", return_value=True):
            out = c._call_balanced("prompt")
        self.assertIn("buy", out)

    def test_exec_detects_codex_session_expired(self):
        # codex가 "session has ended / failed to refresh token"을 stderr로 뱉으면
        # _exec이 cooldown을 걸고 명확한 stub을 반환해야 함.
        c, mod = _make_client()

        class _CP:
            stdout = ""
            stderr = ("ERROR: Failed to refresh token: 400 Bad Request: "
                      "Your session has ended. Please log in again.")

        with patch.object(mod.subprocess, "run", return_value=_CP()):
            out = c._exec(["codex", "exec", "x"], "codex", timeout=5)
        self.assertIn("세션 만료", out)
        self.assertTrue(c._is_codex_cooldown())

    def test_exec_normal_codex_output_no_cooldown(self):
        # 정상 응답이면 cooldown을 걸지 않는다 (오탐 방지).
        c, mod = _make_client()

        class _CP:
            stdout = '{"signal":"hold","confidence":0.4}'
            stderr = "Reading additional input from stdin..."

        with patch.object(mod.subprocess, "run", return_value=_CP()):
            out = c._exec(["codex", "exec", "x"], "codex", timeout=5)
        self.assertIn("hold", out)
        self.assertFalse(c._is_codex_cooldown())


if __name__ == "__main__":
    unittest.main()
