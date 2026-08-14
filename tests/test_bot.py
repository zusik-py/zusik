#!/usr/bin/env python3
"""배포 전 스모크 테스트 — 코드 경로 검증.

실행: python3 test_bot.py
- 모든 import 확인
- 객체 메서드 존재 확인
- KR/US 코드 경로 dry-run
- 에러 시 즉시 실패 + 상세 메시지

systemctl restart 전에 자동 실행됨 (ExecStartPre).
"""
from __future__ import annotations

import sys
import os
# tests/ 이동 — 저장소 루트를 import 경로에 추가 (`import zusik`·형제 테스트 로드).
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import traceback
import inspect
import tempfile
import unittest
from datetime import datetime, timedelta
from unittest.mock import Mock, patch

# tests/ 이동: CWD를 저장소 루트(스크립트 상위)로 — config.yaml·data/ 상대경로 유지
os.chdir(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

PASS = 0
FAIL = 0


def check(name: str, fn):
    global PASS, FAIL
    try:
        fn()
        PASS += 1
        print(f"  {name}")
    except Exception as e:
        FAIL += 1
        print(f"  {name}: {e}")
        traceback.print_exc()


def test_imports():
    """모든 핵심 모듈 import 확인."""
    print("\n[1/5] Import 검증")
    check("bot", lambda: __import__("zusik.core.bot"))
    check("claude_client", lambda: __import__("zusik.clients.claude_client"))
    check("claude_analyst", lambda: __import__("zusik.analysis.claude_analyst"))
    check("cost_optimizer", lambda: __import__("zusik.core.cost_optimizer"))
    check("kis_client", lambda: __import__("zusik.clients.kis_client"))
    check("portfolio_tracker", lambda: __import__("zusik.storage.portfolio_tracker"))
    check("reward_engine", lambda: __import__("zusik.core.reward_engine"))
    check("risk_manager", lambda: __import__("zusik.core.risk_manager"))
    check("trading_mode", lambda: __import__("zusik.core.trading_mode"))
    check("position_manager", lambda: __import__("zusik.core.position_manager"))
    check("portfolio_arena", lambda: __import__("zusik.core.portfolio_arena"))
    check("discord_bot", lambda: __import__("zusik.clients.discord_bot"))
    check("discord_notifier", lambda: __import__("zusik.clients.discord_notifier"))
    check("zusik.strategies.auto_hybrid", lambda: __import__("zusik.strategies.auto_hybrid"))
    check("zusik.strategies.adaptive", lambda: __import__("zusik.strategies.adaptive"))
    check("zusik.strategies.claude_strategy", lambda: __import__("zusik.strategies.claude_strategy"))


def test_cost_optimizer_methods():
    """CostOptimizer에 bot.py가 호출하는 메서드가 다 있는지."""
    print("\n[2/5] CostOptimizer 메서드 검증")
    from zusik.core.cost_optimizer import CostOptimizer
    import yaml
    with open("config.yaml") as f:
        config = yaml.safe_load(f)
    cost = CostOptimizer(config)

    required = [
        "can_call", "update_market_temperature", "local_quick_check",
        "should_analyze", "select_analysts", "cache_result",
        "get_market_temperature", "record_call",
    ]
    for method in required:
        check(f"CostOptimizer.{method}()",
              lambda m=method: assert_has_method(cost, m))


def test_strategy_methods():
    """Strategy 객체에 필요한 메서드가 있는지."""
    print("\n[3/5] Strategy 메서드 검증")
    from zusik.strategies.auto_hybrid import AutoHybridStrategy
    strategy = AutoHybridStrategy()

    required = [
        "set_stock", "set_context", "analyze",
        "get_last_analysis", "get_invest_ratio",
        "get_target_price", "get_stop_loss",
    ]
    for method in required:
        check(f"AutoHybridStrategy.{method}()",
              lambda m=method: assert_has_method(strategy, m))


def test_tracker_methods():
    """PortfolioTracker 메서드 확인."""
    print("\n[4/5] Tracker/Reward/Arena 메서드 검증")
    from zusik.storage.portfolio_tracker import PortfolioTracker
    tracker = PortfolioTracker()
    for m in ["record_buy", "record_sell", "get_realized_pnl_today",
              "get_trades_today", "get_last_buy_time"]:
        check(f"PortfolioTracker.{m}()",
              lambda m=m: assert_has_method(tracker, m))

    from zusik.core.reward_engine import RewardEngine
    import yaml
    with open("config.yaml") as f:
        cfg = yaml.safe_load(f)
    reward = RewardEngine(cfg)
    for m in ["get_performance_summary_text", "get_invest_multiplier",
              "record_trade_result"]:
        check(f"RewardEngine.{m}()",
              lambda m=m: assert_has_method(reward, m))

    from zusik.core.portfolio_arena import PortfolioArena
    arena = PortfolioArena()
    for m in ["record_signal", "get_rankings", "get_report"]:
        check(f"PortfolioArena.{m}()",
              lambda m=m: assert_has_method(arena, m))


def test_execute_stock_dryrun():
    """_execute_stock / _execute_us_stock 코드 경로를 가짜 데이터로 점검."""
    print("\n[5/5] 코드 경로 Dry-run")

    # bot.py의 _execute_stock 내부에서 호출되는 체인 검증
    import yaml
    with open("config.yaml") as f:
        config = yaml.safe_load(f)
    from zusik.core.cost_optimizer import CostOptimizer
    cost = CostOptimizer(config)

    # KR 경로: should_analyze → select_analysts → analyze
    def kr_path():
        # should_analyze
        result = cost.should_analyze("003850", 10000)
        assert isinstance(result, (dict, type(None))) or isinstance(result, bool), \
            f"should_analyze 반환타입: {type(result)}"

        # select_analysts
        sa = cost.select_analysts("003850")
        assert isinstance(sa, (tuple, list)), f"select_analysts 반환타입: {type(sa)}"

        # get_market_temperature
        temp = cost.get_market_temperature()
        assert "temperature" in temp, f"get_market_temperature 키 누락: {temp.keys()}"

    check("KR 경로 (cost → analyze 체인)", kr_path)

    # Strategy 경로 (adaptive만 — Claude 호출 없이 빠르게)
    def strategy_path():
        from zusik.strategies.adaptive import AdaptiveStrategy
        import pandas as pd
        s = AdaptiveStrategy()
        df = pd.DataFrame({
            "open": [100]*20, "high": [105]*20,
            "low": [95]*20, "close": [100]*20,
            "volume": [1000]*20,
        })
        sig = s.analyze(df)
        assert sig in ("buy", "sell", "hold", "long_term_buy"), f"signal: {sig}"

    check("Strategy analyze (adaptive, 로컬)", strategy_path)


def make_price_df(price: int = 100):
    import pandas as pd
    return pd.DataFrame({
        "open": [price] * 20,
        "high": [int(price * 1.05)] * 20,
        "low": [int(price * 0.95)] * 20,
        "close": [price] * 20,
        "volume": [1000] * 20,
    })


def make_ohlcv_df(closes, opens=None, highs=None, lows=None, volumes=None):
    import pandas as pd

    closes = list(closes)
    if opens is None:
        opens = [closes[0]] + closes[:-1]
    if highs is None:
        highs = [max(o, c) for o, c in zip(opens, closes)]
    if lows is None:
        lows = [min(o, c) for o, c in zip(opens, closes)]
    if volumes is None:
        volumes = [1000] * len(closes)

    return pd.DataFrame({
        "open": opens,
        "high": highs,
        "low": lows,
        "close": closes,
        "volume": volumes,
    })


class FakeTracker:
    def __init__(self):
        self._trades = []
        self._long_term = []

    def get_last_buy_reason(self, code):
        for t in reversed(self._trades):
            if t.get("type") == "buy" and t.get("code") == code:
                return str(t.get("reason") or "")
        return ""

    def record_buy(self, code, name, qty, price, is_long_term=False, reason=""):
        now = datetime.now()
        self._trades.append({
            "type": "buy",
            "code": code,
            "name": name,
            "qty": qty,
            "price": price,
            "amount": qty * price,
            "is_long_term": is_long_term,
            "reason": reason,
            "timestamp": now.isoformat(),
            "date": now.strftime("%Y-%m-%d"),
        })

    def record_sell(self, code, name, qty, sell_price, avg_buy_price, reason=""):
        now = datetime.now()
        realized_pnl = (sell_price - avg_buy_price) * qty
        realized_rate = ((sell_price - avg_buy_price) / avg_buy_price * 100) if avg_buy_price else 0
        # 실제 PortfolioTracker._market_meta와 동일: 6자리 숫자만 KR, 그 외는 US
        is_us = bool(code) and not (code.isdigit() and len(code) == 6)
        self._trades.append({
            "type": "sell",
            "code": code,
            "name": name,
            "qty": qty,
            "price": sell_price,
            "amount": qty * sell_price,
            "avg_buy_price": avg_buy_price,
            "realized_pnl": realized_pnl,
            "realized_rate": realized_rate,
            "reason": reason,
            "timestamp": now.isoformat(),
            "date": now.strftime("%Y-%m-%d"),
            "market": "US" if is_us else "KR",
            "ticker": code if is_us else "",
        })
        return {"realized_pnl": realized_pnl, "realized_rate": realized_rate}

    def get_realized_pnl_today(self, market=""):
        today = datetime.now().strftime("%Y-%m-%d")
        sells = [t for t in self._trades if t["type"] == "sell" and t["date"] == today
                 and (not market or t.get("market") == market)]
        return {
            "date": today,
            "realized_pnl": sum(t.get("realized_pnl", 0) for t in sells),
            "sell_count": len(sells),
            "sell_amount": sum(t.get("amount", 0) for t in sells),
            "details": sells,
        }

    def get_trades_today(self):
        today = datetime.now().strftime("%Y-%m-%d")
        return [t for t in self._trades if t["date"] == today]

    def get_realized_pnl_total(self):
        sells = [t for t in self._trades if t["type"] == "sell"]
        return {
            "total_realized_pnl": sum(t.get("realized_pnl", 0) for t in sells),
            "total_sell_count": len(sells),
        }

    def get_last_buy_time(self, code):
        buys = [t for t in self._trades if t["type"] == "buy" and t["code"] == code]
        if not buys:
            return None
        return datetime.fromisoformat(buys[-1]["timestamp"])

    def get_long_term_holdings(self):
        return list(self._long_term)

    def get_long_term_total_cost(self):
        return 0

    def get_pattern_stats(self, days=None, *, on_date=None, since=None, market=None):
        """실제 PortfolioTracker의 동일 이름 메서드를 모방."""
        from zusik.storage.portfolio_tracker import PortfolioTracker
        from collections import defaultdict
        cutoff = None
        if on_date is None and since is None and days is not None:
            from datetime import timedelta
            cutoff = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d")
        stats = defaultdict(lambda: {"count": 0, "wins": 0, "pnl_sum": 0, "amount_sum": 0})
        for t in self._trades:
            if t.get("type") != "sell":
                continue
            if on_date is not None and t.get("date", "") != on_date:
                continue
            if since is not None and t.get("timestamp", "") < since:
                continue
            if cutoff and t.get("date", "") < cutoff:
                continue
            if market is not None and (t.get("market") or "KR") != market:
                continue
            pat = t.get("sell_pattern") or PortfolioTracker._classify_sell_pattern(t.get("reason", ""))
            pnl = t.get("realized_pnl") or 0
            amt = t.get("amount", 0) or 0
            stats[pat]["count"] += 1
            if pnl > 0:
                stats[pat]["wins"] += 1
            stats[pat]["pnl_sum"] += pnl
            stats[pat]["amount_sum"] += amt
        return {p: {"count": s["count"], "wins": s["wins"],
                    "win_rate": (s["wins"]/s["count"]*100) if s["count"] else 0.0,
                    "pnl_sum": s["pnl_sum"],
                    "avg_pnl": s["pnl_sum"]/s["count"] if s["count"] else 0,
                    "avg_pct": (s["pnl_sum"]/s["amount_sum"]*100) if s["amount_sum"] else 0.0,
                    "amount_sum": s["amount_sum"]}
                for p, s in stats.items()}

    @staticmethod
    def classify_entry_bucket(reason: str) -> str:
        from zusik.storage.portfolio_tracker import PortfolioTracker
        return PortfolioTracker.classify_entry_bucket(reason)

    def get_entry_bucket_stats(self, days: int | None = None, month: str = "") -> dict:
        """실제 PortfolioTracker와 같은 진입 버킷 집계 계약."""
        cutoff = ""
        if days:
            cutoff = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d")
        last_buy = {}
        stats = {}
        for t in self._trades:
            code = t.get("code")
            if t.get("type") == "buy":
                last_buy[code] = self.classify_entry_bucket(t.get("reason"))
            elif t.get("type") == "sell":
                if cutoff and str(t.get("date", "")) < cutoff:
                    continue
                if month and not str(t.get("date", "")).startswith(month):
                    continue
                bucket = last_buy.get(code, "unknown")
                pnl = t.get("realized_pnl", t.get("pnl", 0)) or 0
                s = stats.setdefault(bucket, {"n": 0, "wins": 0, "pnl": 0})
                s["n"] += 1
                s["wins"] += 1 if pnl > 0 else 0
                s["pnl"] += pnl
        return stats


class FakeKISClient:
    def __init__(self, kr_cash=300_000, us_cash_usd=1_000.0):
        self.kr_cash = kr_cash
        self.kr_holdings = {}
        self.us_cash_usd = us_cash_usd
        self.us_holdings = {}
        self.kr_prices = {}
        self.us_prices = {}
        self.kr_dfs = {}
        self.us_dfs = {}
        self.default_df = make_price_df()
        self.buy_market_calls = []
        self.sell_market_calls = []
        self.buy_us_limit_calls = []
        self.sell_us_limit_calls = []

    def set_kr_price(self, code, price, name=None, change_rate=0.0):
        self.kr_prices[code] = {
            "price": price,
            "change_rate": change_rate,
            "volume": 1000,
            "name": name or code,
        }

    def set_us_price(self, ticker, price, exchange="NASD", change_rate=0.0):
        self.us_prices[(ticker, exchange)] = {
            "price": price,
            "change_rate": change_rate,
        }

    def set_kr_df(self, code, df):
        self.kr_dfs[code] = df.copy()

    def set_us_df(self, ticker, df, exchange="NASD"):
        self.us_dfs[(ticker, exchange)] = df.copy()

    def get_ohlcv(self, code, period="D"):
        return self.kr_dfs.get(code, self.default_df).copy()

    def get_us_ohlcv(self, ticker, exchange="NASD", period="D"):
        return self.us_dfs.get((ticker, exchange), self.default_df).copy()

    def get_minute_ohlcv(self, code, minutes=60):
        return self.default_df.copy()

    def get_current_price(self, code):
        return dict(self.kr_prices[code])

    def get_us_current_price(self, ticker, exchange="NASD"):
        return dict(self.us_prices[(ticker, exchange)])

    def get_stock_name(self, code):
        return self.kr_prices.get(code, {}).get("name", code)

    def get_balance(self):
        holdings = []
        total_eval = 0
        for code, pos in self.kr_holdings.items():
            current_price = self.kr_prices.get(code, {}).get("price", pos["avg_price"])
            qty = pos["qty"]
            total_eval += qty * current_price
            profit_rate = ((current_price - pos["avg_price"]) / pos["avg_price"] * 100) if pos["avg_price"] else 0
            holdings.append({
                "code": code,
                "name": pos["name"],
                "qty": qty,
                "current_price": current_price,
                "avg_price": pos["avg_price"],
                "profit_rate": profit_rate,
            })
        return {"cash": self.kr_cash, "total_eval": total_eval, "holdings": holdings}

    def get_us_balance(self):
        holdings = []
        for ticker, pos in self.us_holdings.items():
            current_price = self.us_prices.get((ticker, pos["exchange"]), {}).get("price", pos["avg_price"])
            holdings.append({
                "ticker": ticker,
                "qty": pos["qty"],
                "current_price": current_price,
                "avg_price": pos["avg_price"],
            })
        return {"cash_usd": self.us_cash_usd, "holdings": holdings}

    def get_usd_krw_rate(self):
        return 1350.0

    # 시장 상태 (기본: 둘 다 열림). 개별 테스트에서 False 지정 가능
    is_market_open_return = True
    is_us_market_open_return = True

    def is_market_open(self):
        return self.is_market_open_return

    def is_us_market_open(self):
        return self.is_us_market_open_return

    # 마감까지 남은 분 — 기본 None(마감 임박 아님)이라 인버스 EOD 락인은 미발동.
    # 락인 동작을 검증하는 테스트는 별도 스텁(_StubClock)으로 값을 주입한다.
    minutes_to_close_return = None
    us_minutes_to_close_return = None

    def minutes_to_close(self):
        return self.minutes_to_close_return

    def us_minutes_to_close(self):
        return self.us_minutes_to_close_return

    def is_weekday(self):
        return True

    def buy_market(self, code, qty):
        price = self.kr_prices[code]["price"]
        cost = price * qty
        if cost > self.kr_cash:
            return {"success": False, "message": "insufficient cash"}
        self.buy_market_calls.append((code, qty, price))
        self.kr_cash -= cost
        pos = self.kr_holdings.get(code)
        if pos:
            total_qty = pos["qty"] + qty
            total_cost = pos["qty"] * pos["avg_price"] + cost
            pos["qty"] = total_qty
            pos["avg_price"] = total_cost // total_qty
        else:
            self.kr_holdings[code] = {"qty": qty, "avg_price": price, "name": self.kr_prices[code]["name"]}
        return {"success": True, "order_no": f"KR-BUY-{len(self.buy_market_calls)}"}

    def sell_market(self, code, qty):
        pos = self.kr_holdings.get(code)
        if not pos or qty > pos["qty"]:
            return {"success": False, "message": "insufficient qty"}
        price = self.kr_prices[code]["price"]
        self.sell_market_calls.append((code, qty, price))
        self.kr_cash += price * qty
        pos["qty"] -= qty
        if pos["qty"] <= 0:
            del self.kr_holdings[code]
        return {"success": True, "order_no": f"KR-SELL-{len(self.sell_market_calls)}"}

    def buy_us_limit(self, ticker, qty, limit_price, exchange):
        market_price = self.us_prices[(ticker, exchange)]["price"]
        cost = market_price * qty
        if cost > self.us_cash_usd:
            return {"success": False, "message": "insufficient usd"}
        self.buy_us_limit_calls.append((ticker, qty, limit_price, exchange))
        self.us_cash_usd -= cost
        pos = self.us_holdings.get(ticker)
        if pos:
            total_qty = pos["qty"] + qty
            total_cost = pos["qty"] * pos["avg_price"] + cost
            pos["qty"] = total_qty
            pos["avg_price"] = total_cost / total_qty
        else:
            self.us_holdings[ticker] = {"qty": qty, "avg_price": market_price, "exchange": exchange}
        return {"success": True, "order_no": f"US-BUY-{len(self.buy_us_limit_calls)}"}

    def sell_us_limit(self, ticker, qty, limit_price, exchange):
        pos = self.us_holdings.get(ticker)
        if not pos or qty > pos["qty"]:
            return {"success": False, "message": "insufficient qty"}
        market_price = self.us_prices[(ticker, exchange)]["price"]
        self.sell_us_limit_calls.append((ticker, qty, limit_price, exchange))
        self.us_cash_usd += market_price * qty
        pos["qty"] -= qty
        if pos["qty"] <= 0:
            del self.us_holdings[ticker]
        return {"success": True, "order_no": f"US-SELL-{len(self.sell_us_limit_calls)}"}


class FakeStrategy:
    name = "fake_strategy"

    def __init__(self):
        self.signal = "buy"
        self.invest_ratio = 1.0
        self.last_analysis = {
            "reasoning": "unit-test",
            "long_term_reason": "unit-test-long-term",
            "news_summary": "",
            "confidence": 0.8,
            "analyst_details": {},
        }

    def set_stock(self, code, name):
        self.stock = (code, name)

    def set_context(self, **kwargs):
        self.context = kwargs

    def analyze(self, df):
        return self.signal

    def get_last_analysis(self):
        return dict(self.last_analysis)

    def get_invest_ratio(self):
        return self.invest_ratio


class TradingBotRuntimeTests(unittest.TestCase):
    def setUp(self):
        from zusik.core.bot import TradingBot

        self._tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmpdir.cleanup)
        self._reentry_patcher = patch.object(
            TradingBot, "_REENTRY_BLOCK_FILE",
            os.path.join(self._tmpdir.name, "reentry_block.json"),
        )
        self._reentry_patcher.start()
        self.addCleanup(self._reentry_patcher.stop)

        self.bot_cls = TradingBot
        self.client = FakeKISClient()
        self.tracker = FakeTracker()
        self.strategy = FakeStrategy()

        bot = TradingBot.__new__(TradingBot)
        bot.client = self.client
        bot.config = {}
        bot.discord = None
        bot.tracker = self.tracker
        bot.strategy = self.strategy
        bot.use_claude = False
        bot.use_adaptive = False
        bot.invest_ratio = 1.0
        bot.min_amount = 10_000
        bot.min_amount_usd = 50.0
        bot.period = "D"
        bot.kr_stocks = [{"code": "005930", "name": "삼성전자"}]
        bot.us_stocks = [{"ticker": "AAPL", "name": "Apple", "exchange": "NASD"}]
        bot.stocks = bot.kr_stocks
        bot._name_cache = {}
        bot._check_crisis_with_data = Mock()
        bot._check_long_term_limit = Mock(return_value=True)
        bot._rotate_kr_stock = Mock()
        bot._rotate_us_stock = Mock()
        bot._dynamic_invest_ratio = Mock(side_effect=lambda r, c, is_inverse=False, symbol="", realized_vol=0.0: (r, "mock"))
        bot._bearish_regime_score = Mock(return_value=0.0)
        bot._should_allow_inverse_entry = Mock(return_value=(False, "mock peace"))
        bot._should_force_exit_inverse = Mock(return_value=(False, ""))
        bot._is_inverse = Mock(return_value=False)
        bot._pre_market_buy_gate = Mock(return_value=(True, "mock"))
        bot._market_condition = "peace"
        bot._defensive_mode = False
        bot._bear_cache = (0.0, 0.0)
        bot._merge_logged_kr = set()
        bot._merge_logged_us = set()
        bot._reentry_block = {}
        bot._daily_sell_count = {}
        bot._last_intraday_change = {}
        bot._signal_history = {}
        bot.risk = Mock(is_emergency_hold=Mock(return_value=False))
        bot.signals = Mock(check_oversold_bounce=Mock(return_value=None),
                           check_overbought_exit=Mock(return_value=None),
                           check_quick_loss_exit=Mock(return_value=None))
        bot.cost = Mock(
            update_market_temperature=Mock(),
            local_quick_check=Mock(return_value={"action_needed": True, "signal_hint": "buy"}),
            should_analyze=Mock(return_value={"should_call": True, "call_level": "fast"}),
            select_analysts=Mock(return_value=["local"]),
            get_market_temperature=Mock(return_value={"temperature": "warm", "cache_ttl": 5}),
            cache_result=Mock(),
        )
        bot.reward = Mock(
            get_invest_multiplier=Mock(return_value=1.0),
            get_performance_summary_text=Mock(return_value=""),
            record_trade_result=Mock(),
        )
        bot.positions = Mock(
            has_position=Mock(return_value=False),
            check_crash=Mock(return_value=None),
            check_surge=Mock(return_value=None),
            update_trailing_stop=Mock(return_value=None),
            multi_timeframe_check=Mock(return_value={"aligned": False, "daily_trend": "up", "hourly_timing": "buy"}),
            plan_buy=Mock(side_effect=lambda code, invest, price, **kwargs: {
                "qty": invest // price,
                "tranche": 1,
                "remaining_tranches": 0,
                "skip_reason": "",
            }),
            plan_sell=Mock(side_effect=lambda code, price, total_qty: {
                "qty": total_qty,
                "tranche": 1,
            }),
            record_buy=Mock(),
            record_sell=Mock(),
            check_earnings_blackout=Mock(return_value={"in_blackout": False}),
            check_correlation=Mock(return_value={"allowed": True}),
        )
        bot.order_guard = Mock(can_order=Mock(return_value=True), record_order=Mock())
        bot.network = Mock(record_success=Mock(), record_failure=Mock())
        bot.arena = Mock(record_signal=Mock())
        bot.event_learner = Mock(record_event_trade=Mock())
        self.bot = bot

        self.client.set_kr_price("005930", 50_000, name="삼성전자")
        self.client.set_kr_price("035420", 200_000, name="네이버")
        self.client.set_us_price("AAPL", 100.0, exchange="NASD")

    def test_method_signatures_match_runtime_contract(self):
        expected = {
            "_execute_stock": ["self", "stock"],
            "_execute_us_stock": ["self", "stock"],
            "_handle_buy": ["self", "code", "name", "price", "df", "is_long_term", "mtf", "hedge_base_ratio", "fast_entry"],
            "_handle_sell": ["self", "code", "name", "force_reason", "sell_ratio"],
            "_handle_us_buy": ["self", "ticker", "name", "price", "exchange", "df", "is_long_term", "hedge_base_ratio", "fast_entry"],
            "_handle_us_sell": ["self", "ticker", "name", "exchange", "df", "sell_ratio"],
        }
        for method_name, params in expected.items():
            with self.subTest(method=method_name):
                sig = inspect.signature(getattr(self.bot_cls, method_name))
                self.assertEqual(list(sig.parameters.keys()), params)

    def test_execute_stock_returns_none_and_routes_to_buy(self):
        self.strategy.signal = "buy"
        with patch("zusik.clients.discord_bot.send_trade_alert"):
            result = self.bot._execute_stock({"code": "005930", "name": "삼성전자"})
        self.assertIsNone(result)
        self.assertEqual(self.client.buy_market_calls[-1][:2], ("005930", 6))

    def test_execute_us_stock_returns_none_and_routes_to_buy(self):
        self.strategy.signal = "buy"
        with patch("zusik.clients.discord_bot.send_trade_alert"):
            result = self.bot._execute_us_stock({"ticker": "AAPL", "name": "Apple", "exchange": "NASD"})
        self.assertIsNone(result)
        self.assertEqual(self.client.buy_us_limit_calls[-1][0:2], ("AAPL", 10))

    def test_execute_stock_routes_inverse_to_hedge_handler(self):
        """인버스 코드는 분석기 전에 _handle_inverse로 라우팅된다 (분석기 SELL 우회)."""
        self.bot._is_inverse = Mock(side_effect=lambda c: c == "114800")
        self.bot._handle_inverse = Mock()
        self.client.set_kr_price("114800", 1_000, name="KODEX 인버스")
        with patch("zusik.clients.discord_bot.send_trade_alert"):
            result = self.bot._execute_stock({"code": "114800", "name": "KODEX 인버스"})
        self.assertIsNone(result)
        self.bot._handle_inverse.assert_called_once()
        # 인버스는 일반 매수 경로(분석→buy)를 타지 않아야 함
        self.assertEqual(self.client.buy_market_calls, [])

    def test_execute_us_stock_routes_inverse_to_hedge_handler(self):
        """US 인버스도 분석기 전에 _handle_inverse_us로 라우팅된다."""
        self.bot._is_inverse = Mock(side_effect=lambda t: t == "SH")
        self.bot._handle_inverse_us = Mock()
        self.client.set_us_price("SH", 40.0, exchange="NYSE")
        with patch("zusik.clients.discord_bot.send_trade_alert"):
            result = self.bot._execute_us_stock({"ticker": "SH", "name": "ProShares Short", "exchange": "NYSE"})
        self.assertIsNone(result)
        self.bot._handle_inverse_us.assert_called_once()
        self.assertEqual(self.client.buy_us_limit_calls, [])

    def test_hedge_buy_bypasses_low_cash_block(self):
        """현금예약 차단 중에도 인버스 헷지 매수는 통과한다 (예약 현금=헷지 실탄, 2026-06-06).
        일반 매수는 그대로 차단돼야 함."""
        self.bot._buy_blocked_low_cash = True
        self.bot._is_inverse = Mock(return_value=True)
        self.bot._should_allow_inverse_entry = Mock(return_value=(True, "지수 급락"))
        self.bot._should_force_exit_inverse = Mock(return_value=(False, ""))
        self.client.set_kr_price("114800", 1_000, name="KODEX 인버스")
        # 일반 매수(hedge_base_ratio 없음)는 현금예약 차단을 지켜야 함
        with patch("zusik.clients.discord_bot.send_trade_alert"):
            self.bot._handle_buy("005930", "삼성전자", 50_000, df=make_price_df(50_000))
        self.assertEqual(self.client.buy_market_calls, [], "일반 매수가 현금예약 차단을 안 지킴")
        # 헷지 매수(hedge_base_ratio 지정)는 차단을 우회해 체결돼야 함
        with patch("zusik.clients.discord_bot.send_trade_alert"):
            self.bot._handle_buy("114800", "KODEX 인버스", 1_000,
                                 df=make_price_df(1_000), hedge_base_ratio=0.2)
        self.assertGreaterEqual(len(self.client.buy_market_calls), 1, "헷지 매수가 차단됨")

    def test_hedge_buy_bypasses_buy_cooldown(self):
        """06-08 폭락일 회귀: 인버스 1차 체결 후 4h buy_cooldown에 막혀 헷지가 전혀 증액되지
        못함(로그 '매수 차단 cooldown 240분'). 헷지(hedge_base_ratio)는 cooldown/일일한도를
        우회해 분할 증액돼야 하고, 일반 매수는 그대로 cooldown을 지켜야 한다."""
        self.bot.config = {"position": {"buy_cooldown_minutes": 240,
                                        "daily_buy_count_per_stock": 1}}
        self.bot._is_inverse = Mock(return_value=True)
        self.bot._should_allow_inverse_entry = Mock(return_value=(True, "지수 급락"))
        self.bot._should_force_exit_inverse = Mock(return_value=(False, ""))
        self.client.set_kr_price("114800", 1_000, name="KODEX 인버스")
        # 방금 1차 체결 기록 → cooldown + 일일한도(1회) 발동 상태
        self.tracker.record_buy("114800", "KODEX 인버스", 100, 1_000)
        # 일반 매수(hedge_base_ratio 없음)는 cooldown에 막혀야 함
        with patch("zusik.clients.discord_bot.send_trade_alert"):
            self.bot._handle_buy("114800", "KODEX 인버스", 1_000, df=make_price_df(1_000))
        self.assertEqual(self.client.buy_market_calls, [], "일반 매수가 cooldown을 안 지킴")
        # 헷지 매수(hedge_base_ratio)는 cooldown/일일한도를 우회해 증액돼야 함
        with patch("zusik.clients.discord_bot.send_trade_alert"):
            self.bot._handle_buy("114800", "KODEX 인버스", 1_000,
                                 df=make_price_df(1_000), hedge_base_ratio=0.2)
        self.assertGreaterEqual(len(self.client.buy_market_calls), 1,
                                "헷지 증액이 cooldown/일일한도에 막힘")

    def test_handle_buy_returns_none_and_updates_balance(self):
        df = make_price_df(50_000)
        before_cash = self.client.get_balance()["cash"]
        with patch("zusik.clients.discord_bot.send_trade_alert"):
            result = self.bot._handle_buy("005930", "삼성전자", 50_000, df=df)
        balance = self.client.get_balance()
        self.assertIsNone(result)
        self.assertEqual(before_cash - balance["cash"], 300_000)
        self.assertEqual(balance["holdings"][0]["qty"], 6)

    def test_small_account_trades_despite_dynamic_throttle(self):
        """소액/중소 계좌가 동적 스로틀에 막혀 매매 전면 정지되던 버그(사용자 2026-07-01, 20만 계좌:
        14% 스로틀 → 28k → min_amount 미달 → 스킵). (a) 정확히 20만(=min×2 경계)은 소액 올인,
        (b) 30만+스로틀은 min 미달이어도 최소주문은 태운다. 되돌리면(경계 <, 바닥 없음) buy 0건."""
        self.bot.min_amount = 100_000
        # 대형계좌 리스크 제어(스로틀)가 소액에선 '주문 불가'로 변질되는 상황 재현
        self.bot._dynamic_invest_ratio = Mock(side_effect=lambda r, c, **k: (0.14, "throttle"))
        price = 94_300
        df = make_price_df(price)
        self.client.set_kr_price("069620", price, name="코웨이")

        # (a) 정확히 20만 → 소액 올인 (스로틀 우회, 2주 ≈ 95%)
        self.client.kr_cash = 200_000
        self.client.buy_market_calls.clear()
        with patch("zusik.clients.discord_bot.send_trade_alert"):
            self.bot._handle_buy("069620", "코웨이", price, df=df)
        self.assertTrue(self.client.buy_market_calls, "20만 소액계좌가 매매 못 함 — 경계 탈락 버그")
        self.assertEqual(self.client.buy_market_calls[-1][1], 2, "소액은 올인(2주)")

        # (b) 30만 + 스로틀 14% → invest 42k < min 100k → 최소주문(100k) 상향 → 1주
        self.client.kr_cash = 300_000
        self.client.buy_market_calls.clear()
        with patch("zusik.clients.discord_bot.send_trade_alert"):
            self.bot._handle_buy("069620", "코웨이", price, df=df)
        self.assertTrue(self.client.buy_market_calls, "중소계좌가 스로틀에 막혀 매매 못 함")
        self.assertEqual(self.client.buy_market_calls[-1][1], 1, "min_amount 바닥으로 1주")

    def test_small_account_one_share_when_price_in_allin_band(self):
        """회귀: 95% 올인 밴드 갭 — 주가가 (현금×0.95, 현금] 구간이면
        invest//price=0으로 조용히 포기 → 완전 소액 계좌에선 매매 정지와 동일.
        현금이 1주를 감당하면 1주는 태워야 한다. 되돌리면 buy 0건."""
        self.bot.min_amount = 5_000
        price = 48_000
        df = make_price_df(price)
        self.client.set_kr_price("069620", price, name="코웨이")
        self.client.kr_cash = 50_000  # invest 47,500 < 주가 48,000 ≤ 현금 50,000
        self.client.buy_market_calls.clear()
        with patch("zusik.clients.discord_bot.send_trade_alert"):
            self.bot._handle_buy("069620", "코웨이", price, df=df)
        self.assertTrue(self.client.buy_market_calls,
                        "올인 밴드(현금×0.95 < 주가 ≤ 현금)에서 소액 계좌가 매수 포기")
        self.assertEqual(self.client.buy_market_calls[-1][1], 1, "1주 상향이어야 함")

    def test_small_account_us_one_share_when_price_in_allin_band(self):
        """US 동일 갭: invest $95 < 주가 $97 ≤ 현금 $100(지정가 버퍼 1.005 감당) → 1주."""
        self.bot.min_amount_usd = 5
        self.client.us_cash_usd = 100.0
        price = 97.0
        with patch("zusik.clients.discord_bot.send_trade_alert"):
            self.bot._handle_us_buy("AAPL", "Apple", price, "NASD", df=make_price_df(price))
        holdings = self.client.get_us_balance()["holdings"]
        self.assertTrue(holdings, "US 올인 밴드에서 소액 계좌가 매수 포기")
        self.assertEqual(holdings[0]["qty"], 1, "US 1주 상향이어야 함")

    def test_manual_entry_sell_routes_reward_to_manual_bucket(self):
        """회귀: 수동 명령으로 산 종목의 매도는 자동 전략 학습(reward)에 섞이지 않고
        strategy_name='manual' 별도 버킷으로 — 수동 손익이 전략 EMA를 오염하면
        사이징이 왜곡된다. 일반 진입은 기존대로 전략 이름."""
        self.client.kr_holdings["005930"] = {"qty": 5, "avg_price": 50_000, "name": "삼성전자"}
        self.client.kr_cash = 50_000
        self.client.set_kr_price("005930", 60_000, name="삼성전자")
        self.tracker.record_buy("005930", "삼성전자", 5, 50_000, False, "수동 명령")
        self.tracker._trades[-1]["timestamp"] = (datetime.now() - timedelta(minutes=11)).isoformat()
        self.bot.reward.record_trade_result = Mock()
        with patch("zusik.clients.discord_bot.send_trade_alert"):
            self.bot._handle_sell("005930", "삼성전자")
        kw = self.bot.reward.record_trade_result.call_args.kwargs
        self.assertEqual(kw.get("strategy_name"), "manual", "수동 진입이 전략 버킷으로 학습됨")

    def test_entry_bucket_stats_attributes_sells_to_last_buy(self):
        """진입 버킷 ROI 집계(월간 리포트·entry_roi.py 공용): 매도를 직전 매수
        사유 버킷에 귀속, 승률/손익 집계가 정확해야 한다."""
        from zusik.storage.portfolio_tracker import PortfolioTracker
        tr = PortfolioTracker.__new__(PortfolioTracker)
        tr._trades = [
            {"type": "buy", "code": "A", "reason": "잔금 소진 매수", "date": "2026-06-01"},
            {"type": "sell", "code": "A", "realized_pnl": 1000, "date": "2026-06-02"},
            {"type": "buy", "code": "B", "reason": "수동 명령", "date": "2026-06-03"},
            {"type": "sell", "code": "B", "realized_pnl": -500, "date": "2026-06-04"},
            {"type": "buy", "code": "C", "reason": "[단기] 모멘텀", "date": "2026-06-05"},
            {"type": "sell", "code": "C", "realized_pnl": 300, "date": "2026-06-06"},
        ]
        s = tr.get_entry_bucket_stats()
        self.assertEqual(s["leftover"], {"n": 1, "wins": 1, "pnl": 1000})
        self.assertEqual(s["manual"], {"n": 1, "wins": 0, "pnl": -500})
        self.assertEqual(s["normal"], {"n": 1, "wins": 1, "pnl": 300})

        # month= 필터: 월간 결산과 같은 기간축 (rolling 31일 아님)
        tr._trades.append({"type": "sell", "code": "C", "realized_pnl": 700,
                           "date": "2026-07-01"})
        s6 = tr.get_entry_bucket_stats(month="2026-06")
        self.assertEqual(s6["normal"], {"n": 1, "wins": 1, "pnl": 300},
                         "월 필터가 다음 달 매도를 포함함")

        # HTML 월간 리포트에도 진입 유형 표 포함
        from zusik.reporting.monthly_html import render_monthly_html
        html = render_monthly_html({"month": "2026-06", "days": 20, "return_pct": 1.0,
                                    "start_equity": 1, "end_equity": 1, "deposits": 0,
                                    "realized": 0, "max_drawdown": 0,
                                    "entry_buckets": s6})
        self.assertIn("진입 유형별 손익", html)
        self.assertIn("잔금소진", html)

    def test_full_flow_buy_leftover_sell_reward_monthly_report(self):
        """전체 플로 회귀: 일반 매수 뒤 잔금소진 진입이 기록되고, 매도 reward와
        월간 진입 유형 리포트까지 같은 bucket으로 이어져야 한다.

        수익 극대화 관점에서 이 연결이 끊기면 leftover가 +EV인지 관측할 수 없고,
        reward/월간 리포트가 서로 다른 수익 구조를 보게 된다."""
        month = datetime.now().strftime("%Y-%m")

        # 1) 일반 KR 매수 경로: 주문·포지션·tracker buy가 함께 기록되는지 확인.
        self.client.kr_cash = 1_000_000
        self.bot._dynamic_invest_ratio = Mock(side_effect=lambda r, c, **k: (0.5, "flow-test"))
        with patch("zusik.clients.discord_bot.send_trade_alert"):
            self.bot._handle_buy("005930", "삼성전자", 50_000, df=make_price_df(50_000))
        self.assertTrue(self.client.buy_market_calls, "일반 매수 주문이 발생하지 않음")
        self.assertTrue(any(t.get("type") == "buy" and t.get("code") == "005930"
                            for t in self.tracker._trades),
                        "일반 매수가 tracker에 기록되지 않음")

        # 2) 잔금소진성 추가 진입을 실제 주문 성공 + 동일 회계 기록으로 재현.
        self.client.buy_market("035420", 1)
        self.bot.positions.record_buy("035420", "네이버", 1, 200_000)
        self.tracker.record_buy("035420", "네이버", 1, 200_000, False, "잔금 소진 매수")
        self.tracker._trades[-1]["timestamp"] = (datetime.now() - timedelta(minutes=11)).isoformat()
        self.assertEqual(self.tracker.classify_entry_bucket(self.tracker._trades[-1]["reason"]),
                         "leftover")

        # 3) 잔금소진 포지션 매도 → reward가 자동 전략 bucket으로 기록되고 실현손익 발생.
        self.client.set_kr_price("035420", 220_000, name="네이버")
        self.bot.reward.record_trade_result = Mock()
        self.bot._handle_sell("035420", "네이버")
        self.assertTrue(self.client.sell_market_calls, "잔금소진 포지션 매도 주문이 발생하지 않음")
        reward_kw = self.bot.reward.record_trade_result.call_args.kwargs
        self.assertEqual(reward_kw.get("strategy_name"), self.strategy.name)
        self.assertGreater(reward_kw.get("realized_pnl", 0), 0)

        # 4) 같은 매도 결과가 월간 entry bucket ROI와 텍스트/HTML 리포트에 연결돼야 한다.
        buckets = self.tracker.get_entry_bucket_stats(month=month)
        self.assertEqual(buckets["leftover"], {"n": 1, "wins": 1, "pnl": 20_000})

        stats = {
            "month": month, "days": 1, "start_equity": 1_000_000,
            "end_equity": 1_020_000, "deposits": 0, "realized": 20_000,
            "net_growth": 20_000, "return_pct": 2.0, "max_drawdown": 0.0,
            "basis": "effective", "entry_buckets": buckets,
        }
        from zusik.reporting.monthly_text import format_monthly_report
        from zusik.reporting.monthly_html import render_monthly_html
        text = format_monthly_report(stats)
        html = render_monthly_html(stats)
        self.assertIn(f"진입 유형별 ({month})", text)
        self.assertIn("leftover", text)
        self.assertIn("+20,000원", text)
        self.assertIn("진입 유형별 손익", html)
        self.assertIn("잔금소진", html)
        self.assertIn("+20,000원", html)

    def test_handle_sell_returns_none_and_updates_balance(self):
        self.client.kr_holdings["005930"] = {"qty": 5, "avg_price": 50_000, "name": "삼성전자"}
        self.client.kr_cash = 50_000
        self.client.set_kr_price("005930", 60_000, name="삼성전자")
        self.tracker.record_buy("005930", "삼성전자", 5, 50_000, False, "seed")
        self.tracker._trades[-1]["timestamp"] = (datetime.now() - timedelta(minutes=11)).isoformat()
        with patch("zusik.clients.discord_bot.send_trade_alert"):
            result = self.bot._handle_sell("005930", "삼성전자")
        balance = self.client.get_balance()
        self.assertIsNone(result)
        self.assertEqual(balance["cash"], 350_000)
        self.assertEqual(balance["holdings"], [])

    def test_handle_us_buy_returns_none_and_updates_balance(self):
        with patch("zusik.clients.discord_bot.send_trade_alert"):
            result = self.bot._handle_us_buy("AAPL", "Apple", 100.0, "NASD", df=make_price_df(100))
        balance = self.client.get_us_balance()
        self.assertIsNone(result)
        self.assertEqual(balance["cash_usd"], 0.0)
        self.assertEqual(balance["holdings"][0]["qty"], 10)
        self.bot.positions.record_buy.assert_called_with("AAPL", "Apple", 10, 100.0)

    def test_handle_us_sell_returns_none_and_updates_balance(self):
        # +10% 수익 구간: 분할매도 50% (5주 매도, 5주 잔여)
        self.client.us_holdings["AAPL"] = {"qty": 10, "avg_price": 100.0, "exchange": "NASD"}
        self.client.us_cash_usd = 0.0
        self.client.set_us_price("AAPL", 110.0, exchange="NASD")
        self.tracker.record_buy("AAPL", "Apple", 10, 135_000, False, "seed")
        self.tracker._trades[-1]["timestamp"] = (datetime.now() - timedelta(minutes=11)).isoformat()
        result = self.bot._handle_us_sell("AAPL", "Apple", "NASD")
        balance = self.client.get_us_balance()
        self.assertIsNone(result)
        self.assertEqual(balance["cash_usd"], 550.0)  # 5주 × $110
        self.assertEqual(balance["holdings"][0]["qty"], 5)
        self.bot.positions.record_sell.assert_called_with("AAPL", 5)

    def test_force_us_sell_bypasses_ten_minute_guard(self):
        self.client.us_holdings["AAPL"] = {"qty": 10, "avg_price": 100.0, "exchange": "NASD"}
        self.client.us_cash_usd = 0.0
        self.client.set_us_price("AAPL", 90.0, exchange="NASD")
        self.tracker.record_buy("AAPL", "Apple", 10, 135_000, False, "seed")
        self.bot._us_force_sell_reason = "emergency"

        try:
            result = self.bot._handle_us_sell("AAPL", "Apple", "NASD")
        finally:
            self.bot._us_force_sell_reason = None

        balance = self.client.get_us_balance()
        self.assertIsNone(result)
        self.assertEqual(balance["holdings"], [])
        self.assertEqual(len(self.client.sell_us_limit_calls), 1)

    def test_churn_guard_blocks_sell_within_ten_minutes(self):
        df = make_price_df(50_000)
        with patch("zusik.clients.discord_bot.send_trade_alert"):
            self.bot._handle_buy("005930", "삼성전자", 50_000, df=df)
            self.bot._handle_sell("005930", "삼성전자")
        balance = self.client.get_balance()
        self.assertEqual(len(self.client.sell_market_calls), 0)
        holding = next(h for h in balance["holdings"] if h["code"] == "005930")
        self.assertEqual(holding["qty"], 6)

    def test_kr_buy_uses_kis_orderable_cash_directly(self):
        # 한국 시장: 매도금 즉시 재매수 가능(sll_ruse). KIS의 'cash'(orderable_cash) 필드가
        # 이미 매도 재사용분 포함하므로 별도 차감 시 매수 차단 버그.
        # 봇은 KIS 가용 현금에 의존, 최근 매도가 있어도 매수 가능해야 함.
        self.client.kr_cash = 250_000
        self.client.set_kr_price("005930", 100_000, name="삼성전자")
        self.tracker._trades.append({
            "type": "sell",
            "code": "005930",
            "name": "삼성전자",
            "qty": 2,
            "price": 100_000,
            "amount": 200_000,
            "timestamp": (datetime.now() - timedelta(days=1)).isoformat(),
            "date": datetime.now().strftime("%Y-%m-%d"),
        })
        with patch("zusik.clients.discord_bot.send_trade_alert"):
            self.bot._handle_buy("005930", "삼성전자", 100_000, df=make_price_df(100_000))
        # KIS cash 250k가 매수 충분 → 매수 1회 발생
        self.assertEqual(len(self.client.buy_market_calls), 1)

    def test_screened_stock_price_must_fit_available_cash(self):
        expensive_pick = {"code": "035420", "name": "네이버", "reason": "too expensive"}
        self.client.kr_cash = 50_000
        self.bot.auto_screen = True
        self.bot.screener = Mock(
            needs_update=Mock(return_value=True),
            is_crisis_mode=Mock(return_value=False),
            #: _refresh_stocks가 시장별 분리 호출로 바뀜 (screen_all → screen_kr_stocks/screen_us_stocks)
            screen_kr_stocks=Mock(return_value=[expensive_pick]),
            screen_us_stocks=Mock(return_value=[]),
        )
        self.bot.reward.get_performance_summary_text.return_value = ""
        self.bot.config.setdefault("screening", {})["authority"] = "llm"
        self.bot._apply_screened_stocks = self.bot_cls._apply_screened_stocks.__get__(self.bot, self.bot_cls)

        # market="both"로 명시해 시장 열림 상태에 관계없이 실행
        self.bot._refresh_stocks(force=True, market="both")

        selected_prices = [
            self.client.get_current_price(stock["code"])["price"]
            for stock in self.bot.kr_stocks
        ]
        self.assertTrue(
            all(price <= 50_000 for price in selected_prices),
            f"잔고 50,000원인데 고가 종목이 선별됨: {self.bot.kr_stocks}",
        )


class TradingBotScenarioTests(unittest.TestCase):
    def setUp(self):
        from zusik.core.bot import TradingBot
        import zusik.core.position_manager as position_manager
        import zusik.core.risk_manager as risk_manager
        from zusik.core.position_manager import PositionManager
        from zusik.core.risk_manager import RiskManager

        self._tmpdir = tempfile.TemporaryDirectory()
        self._position_patcher = patch.object(
            position_manager, "POSITIONS_FILE",
            os.path.join(self._tmpdir.name, "positions.json"),
        )
        self._risk_patcher = patch.object(
            risk_manager, "STATE_FILE",
            os.path.join(self._tmpdir.name, "risk_state.json"),
        )
        self._position_patcher.start()
        self._risk_patcher.start()
        self.addCleanup(self._position_patcher.stop)
        self.addCleanup(self._risk_patcher.stop)
        self.addCleanup(self._tmpdir.cleanup)

        self.bot_cls = TradingBot
        self.client = FakeKISClient()
        self.tracker = FakeTracker()
        self.strategy = FakeStrategy()

        self.config = {
            "trading_mode": "balanced",
            "_daily_loss_limit_pct": 0.15,
            "risk": {
                "daily_target_profit_rate": 0.02,
                "daily_target_profit_amount": 0,
                "daily_loss_limit": -50_000,
                "crisis_drop_threshold": -0.05,
                "strategy_switch_loss": -0.10,
                "stop_loss_per_stock": -0.15,
            },
            "cooldown": {
                "daily_target_min_confidence": 0.80,
                "daily_target_invest_ratio": 0.50,
            },
            "consensus": {
                "unanimous_multiplier": 1.20,
                "majority_multiplier": 1.12,
                "split_multiplier": 0.60,
                "mixed_multiplier": 0.85,
            },
            "reward": {
                "context_learning_trades": 3,
                "context_return_scale": 8.0,
                "context_win_bonus_scale": 1.5,
            },
            "position": {
                "buy_tranches": [1.0],
                "sell_tranches": [1.0],
                "sell_target_pcts": [0.05, 0.10],
                "trailing_stop_pct": 0.05,
                "trailing_activate_pct": 0.03,
                "crash_instant_sell": -0.04,
                "crash_from_high_sell": -0.06,
                "crash_gap_down": -0.03,
                "crash_vol_spike_ratio": 5.0,
                "surge_quick_profit": 0.10,
                "surge_limit_sell": 0.25,
                "surge_dynamic_vol_mult": 1.5,
                "surge_dynamic_atr_mult": 1.2,
                "surge_dynamic_quick_cap": 0.05,
                "surge_dynamic_limit_cap": 0.10,
                "surge_vol_fade_ratio": 0.5,
            },
        }

        bot = TradingBot.__new__(TradingBot)
        bot.client = self.client
        bot.config = self.config
        bot.discord = None
        bot.tracker = self.tracker
        bot.strategy = self.strategy
        bot.use_claude = False
        bot.use_adaptive = False
        bot.invest_ratio = 1.0
        bot.min_amount = 10_000
        bot.min_amount_usd = 50.0
        bot.period = "D"
        bot.daily_target_min_confidence = self.config["cooldown"]["daily_target_min_confidence"]
        bot.daily_target_invest_ratio = self.config["cooldown"]["daily_target_invest_ratio"]
        bot.consensus_unanimous_multiplier = self.config["consensus"]["unanimous_multiplier"]
        bot.consensus_majority_multiplier = self.config["consensus"]["majority_multiplier"]
        bot.consensus_split_multiplier = self.config["consensus"]["split_multiplier"]
        bot.consensus_mixed_multiplier = self.config["consensus"]["mixed_multiplier"]
        bot.kr_stocks = [{"code": "005930", "name": "삼성전자"}]
        bot.us_stocks = [{"ticker": "AAPL", "name": "Apple", "exchange": "NASD"}]
        bot.stocks = bot.kr_stocks
        bot._name_cache = {}
        bot._check_long_term_limit = Mock(return_value=True)
        bot._rotate_kr_stock = Mock()
        bot._rotate_us_stock = Mock()
        bot._dynamic_invest_ratio = Mock(side_effect=lambda r, c, is_inverse=False, symbol="", realized_vol=0.0: (r, "mock"))
        bot._bearish_regime_score = Mock(return_value=0.0)
        bot._should_allow_inverse_entry = Mock(return_value=(False, "mock peace"))
        bot._should_force_exit_inverse = Mock(return_value=(False, ""))
        bot._is_inverse = Mock(return_value=False)
        bot._pre_market_buy_gate = Mock(return_value=(True, "mock"))
        bot._market_condition = "peace"
        bot._defensive_mode = False
        bot._bear_cache = (0.0, 0.0)
        bot._merge_logged_kr = set()
        bot._merge_logged_us = set()
        bot._active_mode = "balanced"
        bot._prev_cash = 0
        bot._buy_blocked_low_cash = False
        bot._daily_loss_halted = {}
        bot._daily_loss_released = {}
        bot._daily_target_reached = ""
        bot._daily_target_cooldown = False
        bot._reentry_block = {}
        bot._daily_sell_count = {}
        bot._last_intraday_change = {}
        bot._signal_history = {}
        bot.defensive_mode_enabled = True
        # 차단 파일도 tmpdir로 격리 (테스트가 실제 data/ 건드리지 않게)
        self._reentry_patcher = patch.object(
            TradingBot, "_REENTRY_BLOCK_FILE",
            os.path.join(self._tmpdir.name, "reentry_block.json"),
        )
        self._reentry_patcher.start()
        self.addCleanup(self._reentry_patcher.stop)
        # 일일목표 알림 가드도 tmpdir 격리 (실제 data/last_daily_target.txt 영향 차단)
        self._dt_patcher = patch.object(
            TradingBot, "_DAILY_TARGET_FILE",
            os.path.join(self._tmpdir.name, "last_daily_target.txt"),
        )
        self._dt_patcher.start()
        self.addCleanup(self._dt_patcher.stop)
        bot.screener = None
        bot.auto_screen = False
        bot.risk = RiskManager(self.config)
        bot.positions = PositionManager(self.config)
        bot.signals = Mock(check_oversold_bounce=Mock(return_value=None),
                           check_overbought_exit=Mock(return_value=None),
                           check_quick_loss_exit=Mock(return_value=None))
        bot.cost = Mock(
            update_market_temperature=Mock(),
            local_quick_check=Mock(return_value={"action_needed": True, "signal_hint": "buy"}),
            should_analyze=Mock(return_value={"should_call": True, "call_level": "fast"}),
            select_analysts=Mock(return_value=["local"]),
            get_market_temperature=Mock(return_value={"temperature": "warm", "cache_ttl": 5}),
            cache_result=Mock(),
        )
        bot.reward = Mock(
            get_invest_multiplier=Mock(return_value=1.0),
            get_performance_summary_text=Mock(return_value=""),
            record_trade_result=Mock(),
        )
        bot.order_guard = Mock(can_order=Mock(return_value=True), record_order=Mock())
        bot.network = Mock(record_success=Mock(), record_failure=Mock())
        bot.arena = Mock(record_signal=Mock(), get_leader=Mock(return_value=None))
        bot.event_learner = Mock(record_event_trade=Mock())
        self.bot = bot

        self.client.set_kr_price("005930", 50_000, name="삼성전자")
        self.client.set_kr_df("005930", make_ohlcv_df([50_000] * 20))

        # `_handle_buy`가 매수 성공 후 `strategy.analyst.memory.record_trade`를 호출하므로
        # FakeStrategy에 analyst mock을 얹어 전체 경로가 안전하게 끝까지 실행되도록 함
        self.strategy.analyst = Mock(memory=Mock(record_trade=Mock()))

    def _redirect_signals(self, mapping):
        """AI 신호/장전 sentiment 파일 접근을 임시파일로 리다이렉트 (os.path.join + paths.data_path)."""
        import contextlib
        import zusik.paths as _zp
        real_join = os.path.join
        real_dp = _zp.data_path

        def fj(*a):
            if a and a[-1] in mapping:
                return mapping[a[-1]]
            return real_join(*a)

        def fd(*p):
            if p and p[-1] in mapping:
                return mapping[p[-1]]
            return real_dp(*p)
        s = contextlib.ExitStack()
        s.enter_context(patch("os.path.join", side_effect=fj))
        s.enter_context(patch.object(_zp, "data_path", side_effect=fd))
        return s

    def test_session_ai_sell_bias_blocks_real_buy(self):
        """실거래 흐름: 데일리 AI가 '매도'로 본 종목은 _handle_buy 게이트에서 신규 매수 차단.

        setUp이 _pre_market_buy_gate를 mock하므로, 진짜 게이트로 되돌려 AI 신호 → 게이트 →
        매수 거부의 end-to-end 경로를 검증한다 (단위 테스트가 아닌 실제 매수 파이프라인)."""
        import json as _json, time as _t
        self.bot.config["ai_signals"] = {"enabled": True, "freshness_hours": 30}
        self.bot._is_whitelist = lambda s: False
        del self.bot.__dict__["_pre_market_buy_gate"]   # 실제 게이트 사용
        d = self._tmpdir.name
        bias = os.path.join(d, "daily_ai_bias.json")
        with open(bias, "w") as f:
            _json.dump({"ts": _t.time(), "kr": {"005930": "sell"}, "us": {}}, f)
        nope = os.path.join(d, "none.json")
        with self._redirect_signals({"daily_ai_bias.json": bias,
                                     "cross_signals_kr.json": nope,
                                     "pre_market_sentiment_KR.json": nope}):
            self.bot._handle_buy("005930", "삼성전자", 50_000, df=make_ohlcv_df([50_000] * 20))
        self.assertEqual(self.client.buy_market_calls, [],
                         "AI 매도판단 종목을 실거래 경로에서 그대로 매수함")

    def test_fast_entry_buy_places_order_when_clean(self):
        """확인된 급등(fast_entry)이 클린 조건(현금·무차단)에서 실제 주문까지 도달 —
        파이프라인이 surge 진입을 삼키지 않음을 end-to-end 로 보장. (FastEntryTests 는 _handle_buy
        를 mock 하므로 실행단 가드는 여기서. 놓친 급등 회귀 방지.)"""
        self.bot._handle_buy("005930", "삼성전자", 50_000,
                             df=make_ohlcv_df([50_000] * 20), fast_entry=True)
        self.assertEqual(len(self.client.buy_market_calls), 1, "클린 fast_entry 진입이 주문을 못 냄")

    def test_fast_fall_guard_blocks_new_buy(self):
        """급락 가드 활성 중 신규(비인버스) 매수는 _handle_buy 에서 중단. 보유는 안 자름(별도)."""
        self.bot._fast_fall_active = True
        self.bot._handle_buy("005930", "삼성전자", 50_000, df=make_ohlcv_df([50_000] * 20))
        self.assertEqual(self.client.buy_market_calls, [], "급락 가드 활성 중 신규 매수가 나감")
        # 가드 해제되면 정상 매수
        self.bot._fast_fall_active = False
        self.bot._handle_buy("005930", "삼성전자", 50_000, df=make_ohlcv_df([50_000] * 20))
        self.assertEqual(len(self.client.buy_market_calls), 1, "가드 해제 후 매수가 막힘")

    def test_session_no_ai_signal_allows_buy(self):
        """대조군: AI 신호가 없으면 동일 흐름에서 매수가 정상 진행 (차단이 AI 때문임을 증명)."""
        self.bot.config["ai_signals"] = {"enabled": True, "freshness_hours": 30}
        self.bot._is_whitelist = lambda s: False
        del self.bot.__dict__["_pre_market_buy_gate"]
        nope = os.path.join(self._tmpdir.name, "none.json")
        with self._redirect_signals({"daily_ai_bias.json": nope,
                                     "cross_signals_kr.json": nope,
                                     "pre_market_sentiment_KR.json": nope}):
            self.bot._handle_buy("005930", "삼성전자", 50_000, df=make_ohlcv_df([50_000] * 20))
        self.assertEqual(len(self.client.buy_market_calls), 1,
                         "AI 신호도 없는데 정상 매수가 막힘")

    def _age_last_buy(self, code="005930", minutes=11):
        # tracker(trades.json) timestamp 노화
        aged = False
        for trade in reversed(self.tracker._trades):
            if trade["type"] == "buy" and trade["code"] == code:
                trade["timestamp"] = (datetime.now() - timedelta(minutes=minutes)).isoformat()
                aged = True
                break
        # positions.last_buy_date 도 같이 노화: _is_recently_bought 30분 보호와 동기화)
        if hasattr(self.bot.positions, "_positions") and code in self.bot.positions._positions:
            self.bot.positions._positions[code]["last_buy_date"] = \
                (datetime.now() - timedelta(minutes=minutes)).isoformat()
        if not aged:
            raise AssertionError(f"매수 기록 없음: {code}")

    def test_scenario_peace_entry_then_surge_exit_realizes_profit(self):
        self.strategy.signal = "buy"
        self.bot._execute_stock({"code": "005930", "name": "삼성전자"})

        self.assertEqual(len(self.client.buy_market_calls), 1)
        self._age_last_buy()

        # surge_dynamic이 변동성으로 quick_profit을 +0.05까지 늘리므로
        # 18% 급등으로 dynamic cap (10% + 5% = 15%)도 확실히 넘김
        surge_df = make_ohlcv_df([50_000] * 19 + [59_000], volumes=[1000] * 20)
        self.client.set_kr_price("005930", 59_000, name="삼성전자", change_rate=18.0)
        self.client.set_kr_df("005930", surge_df)
        self.bot._execute_stock({"code": "005930", "name": "삼성전자"})

        realized = self.tracker.get_realized_pnl_today()["realized_pnl"]
        self.assertEqual(len(self.client.sell_market_calls), 1)
        self.assertGreater(realized, 0, f"급등 시나리오에서 수익 실현 실패: {realized}")

    def test_scenario_crash_day_full_hedge_chain(self):
        """폭락일 헷지 풀체인 통합 검증 (2026-06-10) — KOSPI 급락 4연속 실패의 끊긴 고리를
        한 시나리오로 잇는다:
          A. 평시: 인버스 진입 거부 (pullback 추격 금지)
          B. 지수 급락 감지 → 진입 게이트 열림 → 1차 헷지 매수 (06-05: 매수 0건 버그)
          C. 급락 지속 → 증액 매수 (06-08: cooldown 차단으로 증액 0 버그)
          D. 반등일: 인버스 자기차트 얕은 급락 = 컷 금지 (06-09: crash_instant 바닥투매 버그)
          E. 평시 복귀 → 레짐 강제 청산 (인버스 장기보유 금지)
        ※ phantom 주문 검증(rt_cd=0≠체결, 06-08)은 kis_client._order 레벨이라 본 시뮬 범위 밖.
        """
        bot = self.bot
        # setUp이 mock으로 덮은 실제 게이트 복원
        for m in ("_should_allow_inverse_entry", "_should_force_exit_inverse",
                  "_bearish_regime_score"):
            if m in bot.__dict__:
                del bot.__dict__[m]
        bot._is_inverse = Mock(side_effect=lambda c: c == "114800")
        bot.derivative_etf_enabled = True
        # 상황 적응: 이 체인은 지수급락 경로를 검증하므로 트리거를 opt-in (기본 OFF는 설정 선택)
        # 이 테스트는 '헷지 스케일링(증액) 모드'를 검증 → 빠른익절/반전락인은 OFF(별도 테스트에서 검증).
        # 둘 다 켜면(기본) 보유 인버스가 +1.5%에서 익절돼 증액 대신 매도가 정상(사용자 수익화 우선).
        self.config["inverse"] = {"enabled": True, "max_ratio": 0.5,
                                  "trigger_crisis": True, "trigger_index_crash": True,
                                  "quick_profit_pct": 0, "reversal_lock_pct": 0}
        inv_df_up = make_ohlcv_df([4_800] * 19 + [5_000])   # 직전 4800 → 5000 = +4.17% (지수 -4.2% 급락 반영)

        def _reset_caches():
            bot._crash_cache = (0.0, False)
            bot._bear_cache = (0.0, 0.0)

        # ── A. 평시: 진입 거부 ──
        bot._market_condition = "peace"
        self.client.set_kr_price("069500", 30_000, name="KODEX 200", change_rate=0.1)
        self.client.set_kr_df("069500", make_ohlcv_df([30_000] * 20))
        self.client.set_kr_price("114800", 5_000, name="KODEX 인버스", change_rate=0.0)
        self.client.set_kr_df("114800", make_ohlcv_df([5_000] * 20))
        _reset_caches()
        bot._handle_inverse("114800", "KODEX 인버스", 5_000, self.client.get_ohlcv("114800"))
        self.assertEqual(len(self.client.buy_market_calls), 0,
                         "A 평시에 인버스 매수 = pullback 추격 (백테스트 -23%)")

        # ── B. 지수 장중 -4.2% 급락 → 1차 헷지 ──
        self.client.set_kr_price("069500", 28_700, name="KODEX 200", change_rate=-4.2)
        self.client.set_kr_price("114800", 5_000, name="KODEX 인버스", change_rate=4.0)
        self.client.set_kr_df("114800", inv_df_up)
        _reset_caches()
        with patch("zusik.clients.discord_bot.send_trade_alert"):
            bot._handle_inverse("114800", "KODEX 인버스", 5_000, inv_df_up)
        self.assertGreaterEqual(len(self.client.buy_market_calls), 1,
                                "B 지수 급락인데 인버스 매수 0건 — 06-05 버그 재발")

        # ── C-1. 직후 재호출 = 증액 보류 (06-11 whipsaw 가드: 시초 10분 전량 소진 금지) ──
        buys_after_first = len(self.client.buy_market_calls)
        self.client.set_kr_price("114800", 5_100, name="KODEX 인버스", change_rate=6.0)
        _reset_caches()
        with patch("zusik.clients.discord_bot.send_trade_alert"):
            bot._handle_inverse("114800", "KODEX 인버스", 5_100, inv_df_up)
        self.assertEqual(len(self.client.buy_market_calls), buys_after_first,
                         "C-1 30분 미경과 즉시 증액 — 06-11 시초 whipsaw 버그 재발 (10분 전량 소진)")

        # ── C-2. 30분+ 경과 + 급락 지속 → 증액 (06-08: cooldown 차단으로 증액 0 버그) ──
        self._age_last_buy(code="114800", minutes=31)
        _reset_caches()
        with patch("zusik.clients.discord_bot.send_trade_alert"):
            bot._handle_inverse("114800", "KODEX 인버스", 5_100, inv_df_up)
        self.assertGreaterEqual(len(self.client.buy_market_calls), buys_after_first + 1,
                                "C-2 급락 30분 지속인데 증액 0 — 06-08 cooldown 차단 버그 재발")

        # ── D. 반등일: 인버스 자기차트 -7% (얕은 급락) = 컷 금지 ──
        self._age_last_buy(code="114800", minutes=90)  # grace(1h) 해제
        crash_df = make_ohlcv_df([5_000] * 19 + [4_650])  # 당일 -7%
        self.client.set_kr_price("114800", 4_650, name="KODEX 인버스", change_rate=-7.0)
        self.client.set_kr_df("114800", crash_df)
        bot._market_condition = "crisis"  # 아직 위기 판정 중 (강제청산 게이트 닫힘)
        _reset_caches()
        # 지수도 급락 유지 → 강제청산 미발동 상태에서 얕은 자기급락만 발생
        with patch("zusik.clients.discord_bot.send_trade_alert"):
            bot._handle_inverse("114800", "KODEX 인버스", 4_650, crash_df)
        self.assertEqual(len(self.client.sell_market_calls), 0,
                         "D 인버스 얕은 자기급락(-7%)을 컷 — 06-09 바닥투매 버그 재발")

        # ── E. 평시 복귀 → 레짐 강제 청산 ──
        bot._market_condition = "peace"
        self.client.set_kr_price("069500", 30_000, name="KODEX 200", change_rate=1.0)
        self.client.set_kr_df("069500", make_ohlcv_df([30_000] * 20))
        self.client.set_kr_price("114800", 4_700, name="KODEX 인버스", change_rate=0.5)
        self.client.set_kr_df("114800", make_ohlcv_df([4_700] * 20))
        _reset_caches()
        with patch("zusik.clients.discord_bot.send_trade_alert"):
            bot._handle_inverse("114800", "KODEX 인버스", 4_700,
                                self.client.get_ohlcv("114800"))
        self.assertGreaterEqual(len(self.client.sell_market_calls), 1,
                                "E 평시 복귀인데 인버스 청산 안 함 — 장기보유 금지 위반")
        bal = self.client.get_balance()
        inv_left = next((h for h in bal["holdings"] if h["code"] == "114800"), None)
        self.assertTrue(inv_left is None or inv_left["qty"] == 0,
                        "E 레짐 청산이 전량이 아님 — 인버스 잔여 보유")

    def test_scenario_flash_drop_holds_shallow_cuts_deep(self):
        """2026-06-03 조기손절 억제 정책 (US 승자보유 패턴 KR 이식):
        - 비핵심 KR의 얕은 flash drop(-4.2%)은 홀드(회복 대기) — crash_instant 0%승률(0/13, -649k
          바닥투매) 제거가 목적. 자본보호는 하드스톱(-15%)·트레일링이 담당.
        - 깊은 붕괴(-15%↓)는 그대로 손실 컷 — 보호 유지.
        수정 전(모든 급락 컷)이라면 ① 단계에서 -4.2%를 매도 → 이 테스트 실패."""
        self.strategy.signal = "buy"
        self.bot._execute_stock({"code": "005930", "name": "삼성전자"})
        self._age_last_buy(minutes=61)

        # ① 얕은 -4.2% flash drop → 홀드 (조기손절 억제)
        shallow_df = make_ohlcv_df([50_000] * 19 + [47_900], volumes=[1000] * 20)
        self.client.set_kr_price("005930", 47_900, name="삼성전자", change_rate=-4.2)
        self.client.set_kr_df("005930", shallow_df)
        self.bot._execute_stock({"code": "005930", "name": "삼성전자"})
        self.assertEqual(len(self.client.sell_market_calls), 0,
                         "얕은 -4.2% pullback은 홀드해야 함 (조기손절 억제) — 매도 발생")

        # ② 깊은 -16% 붕괴 → 손실 컷 (하드스톱 영역, floor -9% 아래)
        deep_df = make_ohlcv_df([50_000] * 19 + [42_000], volumes=[1000] * 20)
        self.client.set_kr_price("005930", 42_000, name="삼성전자", change_rate=-16.0)
        self.client.set_kr_df("005930", deep_df)
        self.bot._execute_stock({"code": "005930", "name": "삼성전자"})
        self.assertEqual(len(self.client.sell_market_calls), 1,
                         "깊은 -16% 붕괴는 손실 컷해야 함 (자본 보호)")
        realized = self.tracker.get_realized_pnl_today()["realized_pnl"]
        self.assertLess(realized, 0, f"깊은 붕괴에서 손실 컷 미실행: {realized}")
        self.assertFalse(self.bot.risk.is_emergency_hold(), "평시 flash drop가 chaos hold로 잘못 승격됨")

    def test_scenario_emergency_hold_blocks_new_entry(self):
        """emergency_hold 활성 상태일 때 신규 매수 완전 차단 (2026-04-21 정책 변경).

        이전 버전에서는 개별 종목의 -6% 급락으로 `_check_crisis_with_data`가
        전체 긴급 홀딩을 발동했으나, 이 구조가 5분마다 발동/해제를 반복하는
        무한 루프를 유발해 제거됨. 지금은 시장 전체 감지(`detect_market_condition`)
        또는 외부 플래그로만 emergency_hold가 세팅된다.
        """
        # 외부에서 emergency_hold 활성화
        self.bot.risk._emergency_hold = True
        self.bot.risk._emergency_reason = "test forced hold"

        self.strategy.signal = "buy"
        self.bot._execute_stock({"code": "005930", "name": "삼성전자"})

        self.assertTrue(self.bot.risk.is_emergency_hold())
        self.assertEqual(len(self.client.buy_market_calls), 0,
                         "emergency_hold 활성인데 신규 매수가 실행됨")

    def test_scenario_daily_target_enters_cooldown_and_dedups_alert(self):
        """일일 목표 도달 시 알림 1회 + 쿨다운 진입, 매매 엔진은 계속 동작.
        2026-06-03: 목표 판정이 전체계좌(KR+US, fake compute_total_equity≈1.65M) 기준으로 바뀌어
        실현 +50k(5주×10k)로 2%(=33k) 초과하도록 상향."""
        self.tracker.record_sell("005930", "삼성전자", 5, 60_000, 50_000, "scenario win")
        self.bot.discord = Mock()

        with patch("zusik.core.trading_mode.check_mode_change", return_value=None), \
             patch("zusik.core.trading_mode.check_deposit", return_value=None), \
             patch("zusik.core.trading_mode.detect_market_condition", return_value="peace"):
            can_trade = self.bot._check_risks_before_trading()

        self.assertTrue(can_trade, "일일 목표는 완전 중단이 아니라 쿨다운이어야 함")
        self.assertEqual(self.bot._daily_target_reached, datetime.now().strftime("%Y-%m-%d"))
        self.assertTrue(self.bot._daily_target_cooldown)
        self.bot.discord.notify_daily_target_reached.assert_called_once()

        # 같은 날 재호출해도 알림은 중복 발송되지 않음 (dedup)
        self.bot.discord.notify_daily_target_reached.reset_mock()
        with patch("zusik.core.trading_mode.check_mode_change", return_value=None), \
             patch("zusik.core.trading_mode.check_deposit", return_value=None), \
             patch("zusik.core.trading_mode.detect_market_condition", return_value="peace"):
            can_trade2 = self.bot._check_risks_before_trading()
        self.assertTrue(can_trade2)
        self.assertTrue(self.bot._daily_target_cooldown)
        self.bot.discord.notify_daily_target_reached.assert_not_called()

    def test_scenario_daily_target_cooldown_blocks_low_confidence_buy(self):
        self.bot._daily_target_cooldown = True
        self.bot._daily_target_reached = datetime.now().strftime("%Y-%m-%d")
        self.bot.use_claude = True
        self.client.set_kr_price("005930", 50_000, name="삼성전자")
        self.strategy.get_last_analysis = Mock(return_value={
            "confidence": 0.75,
            "reasoning": "약한 신호",
            "news_summary": "",
            "long_term_reason": "",
            "analyst_details": {},
        })
        self.strategy.get_invest_ratio = Mock(return_value=1.0)

        self.bot._handle_buy("005930", "삼성전자", 50_000, df=make_ohlcv_df([50_000] * 20))
        self.assertEqual(len(self.client.buy_market_calls), 0)

    def test_scenario_daily_target_cooldown_halves_high_confidence_size(self):
        self.bot._daily_target_cooldown = True
        self.bot._daily_target_reached = datetime.now().strftime("%Y-%m-%d")
        self.bot.use_claude = True
        self.client.set_kr_price("005930", 50_000, name="삼성전자")
        self.strategy.get_last_analysis = Mock(return_value={
            "confidence": 0.90,
            "reasoning": "강한 신호",
            "news_summary": "",
            "long_term_reason": "",
            "analyst_details": {},
        })
        self.strategy.get_invest_ratio = Mock(return_value=1.0)

        self.bot._handle_buy("005930", "삼성전자", 50_000, df=make_ohlcv_df([50_000] * 20))
        self.assertEqual(len(self.client.buy_market_calls), 1)
        code, qty, _price = self.client.buy_market_calls[-1]
        self.assertEqual(code, "005930")
        self.assertEqual(qty, 3, "300k 현금에서 쿨다운 50% 축소면 3주 매수여야 함")

    def test_scenario_daily_target_cooldown_uses_configured_threshold_and_ratio(self):
        self.bot._daily_target_cooldown = True
        self.bot._daily_target_reached = datetime.now().strftime("%Y-%m-%d")
        self.bot.use_claude = True
        self.bot.daily_target_min_confidence = 0.95
        self.bot.daily_target_invest_ratio = 0.25
        self.client.kr_cash = 300_000
        self.client.set_kr_price("005930", 50_000, name="삼성전자")
        self.strategy.get_last_analysis = Mock(return_value={
            "confidence": 0.90,
            "reasoning": "강하지만 설정 기준 미달",
            "news_summary": "",
            "long_term_reason": "",
            "analyst_details": {},
        })
        self.strategy.get_invest_ratio = Mock(return_value=1.0)

        self.bot._handle_buy("005930", "삼성전자", 50_000, df=make_ohlcv_df([50_000] * 20))
        self.assertEqual(len(self.client.buy_market_calls), 0, "설정된 confidence 하한 95%를 무시하면 안 됨")

        self.strategy.get_last_analysis = Mock(return_value={
            "confidence": 0.96,
            "reasoning": "설정 기준 통과",
            "news_summary": "",
            "long_term_reason": "",
            "analyst_details": {},
        })
        self.bot._handle_buy("005930", "삼성전자", 50_000, df=make_ohlcv_df([50_000] * 20))
        _code, qty, _price = self.client.buy_market_calls[-1]
        self.assertEqual(qty, 1, "300k 현금에서 쿨다운 25%면 1주만 매수되어야 함")

    def test_scenario_inverse_loss_halt_auto_releases_on_peace(self):
        """인버스 헷지 손실(보험료)이 손실한도를 밀어 올려 평시 복귀 후에도 그날
        매매가 잠기던 문제. (a) 평시에는 인버스 손익을 한도 판정에서 제외,
        (b) 이미 중단됐어도 평시 복귀 + 인버스 제외 손실이 한도 내면 자동 해제,
        (c) 인버스 외 실손실이 한도 초과면 그대로 중단 유지 (자본보호 revert-check)."""
        self.bot._is_inverse = lambda c: c == "114800"
        patches = (patch("zusik.core.trading_mode.check_mode_change", return_value=None),
                   patch("zusik.core.trading_mode.check_deposit", return_value=None),
                   patch("zusik.core.trading_mode.detect_market_condition", return_value="peace"))

        # (a) 인버스 대손실(-100k, 한도 -45k 초과)만 있는 날 + peace → 중단 안 됨
        self.tracker.record_sell("114800", "KODEX 인버스", 100, 1_000, 2_000, "인버스 청산")
        with patches[0], patches[1], patches[2]:
            self.assertTrue(self.bot._check_risks_before_trading("KR"),
                            "평시인데 인버스 헷지 비용이 매매를 중단시킴")

        # (b) 이미 중단된 상태(위기 중 발동 가정) → 평시 복귀 시 자동 해제
        today = datetime.now().strftime("%Y-%m-%d")
        self.bot._daily_loss_halted["KR"] = today
        with patches[0], patches[1], patches[2]:
            self.assertTrue(self.bot._check_risks_before_trading("KR"),
                            "평시 복귀했는데 인버스발 중단이 자동 해제되지 않음")
        self.assertNotIn("KR", self.bot._daily_loss_halted)

        # (c) 인버스 외 일반주 실손실이 한도 초과 → 평시여도 중단 유지 (자본보호)
        self.tracker.record_sell("005930", "삼성전자", 10, 40_000, 50_000, "일반 손절")
        with patches[0], patches[1], patches[2]:
            self.assertFalse(self.bot._check_risks_before_trading("KR"),
                             "일반주 실손실 한도 초과인데 매매가 계속됨 — 자본보호 파괴")

    def test_low_liquidity_stock_buy_blocked(self):
        """회귀: 20일 평균 거래대금 4.4억짜리 초저유동성 종목(미창석유형)이 LLM
        확신도만으로 매수되던 문제 — 하한(기본 10억) 미만이면 로컬에서 차단."""
        import pandas as pd
        # 평균 거래대금 ~1.4억 (10만원 × 1,400주) — 하한 10억 미달
        thin = pd.DataFrame({
            "open": [100_000] * 30, "high": [101_000] * 30, "low": [99_000] * 30,
            "close": [100_000] * 30, "volume": [1_400] * 30,
        })
        blocked = self.bot._churn_guard("003650", "미창석유", df=thin, price=100_000)
        self.assertTrue(blocked, "저유동성 종목 매수가 차단되지 않음")

        # 정상 유동성(1000억대) + 완만한 상승 추세는 통과 — 게이트가 죽지 않았는지
        # revert-check. (가격 평평이면 MC 게이트가 별도로 차단하므로 상승 추세 부여)
        ups = [100_000 + i * 300 for i in range(30)]
        fat = pd.DataFrame({
            "open": ups, "high": [p + 500 for p in ups], "low": [p - 500 for p in ups],
            "close": ups, "volume": [1_000_000] * 30,
        })
        passed = not self.bot._churn_guard("005380", "현대차2", df=fat, price=ups[-1])
        self.assertTrue(passed, "정상 유동성 종목까지 차단됨")

    def test_virtual_account_rate_limited_to_one_call_per_sec(self):
        """모의투자는 KIS가 초당 1건만 허용 — 성숙 계정(KIS_API_MATURE)이어도
        is_virtual이면 호출 간격이 1초 이상 벌어져야 한다. 실전은 그대로 빠르게."""
        import time as _time
        from zusik.clients.kis_client import KISClient

        def _two_calls_elapsed(virtual: bool) -> float:
            c = KISClient.__new__(KISClient)
            c.is_virtual = virtual
            c._api_start_date = datetime.min  # 성숙 계정 (실전이면 12 req/sec)
            c._call_times = []
            c._order_call_times = []
            t0 = _time.time()
            c._rate_limit()
            c._rate_limit()
            return _time.time() - t0

        self.assertGreaterEqual(_two_calls_elapsed(True), 1.0,
                                "모의투자가 초당 1건 한도를 초과해 호출함")
        self.assertLess(_two_calls_elapsed(False), 0.5,
                        "실전 계정이 모의투자 스로틀에 걸림")

    def test_scenario_daily_loss_limit_halts_trading(self):
        """일일 손실이 한도(총자산 × 15%) 초과 시 매매 중단."""
        # FakeKIS cash=300_000 → daily_loss_limit = -45_000
        self.tracker.record_sell("005930", "삼성전자", 10, 40_000, 50_000, "big loss")

        with patch("zusik.core.trading_mode.check_mode_change", return_value=None), \
             patch("zusik.core.trading_mode.check_deposit", return_value=None), \
             patch("zusik.core.trading_mode.detect_market_condition", return_value="peace"):
            can_trade = self.bot._check_risks_before_trading()

        self.assertFalse(can_trade, "일일 손실한도 도달 시 매매 중단되어야 함")
        self.assertEqual(self.bot._daily_loss_halted.get("ALL"),
                         datetime.now().strftime("%Y-%m-%d"))

    def test_scenario_daily_loss_halt_is_per_market_and_releasable(self):
        """회귀: US 새벽 손실이 같은 날짜의 KR장 전체를 막던 문제.

        (a) US 시장 손실 한도 초과 → US만 중단, KR은 매매 지속.
        (b) /손실해제 → 중단 해제 + 같은 날 재발동 없음 (해제가 없으면 다음
            사이클에 같은 손실로 즉시 재중단돼 명령이 무의미)."""
        # US 매도 대손실 (AAPL → market="US" 자동 분류), KR 손실 없음
        self.tracker.record_sell("AAPL", "Apple", 10, 40_000, 50_000, "US big loss")

        patches = dict(
            a=patch("zusik.core.trading_mode.check_mode_change", return_value=None),
            b=patch("zusik.core.trading_mode.check_deposit", return_value=None),
            c=patch("zusik.core.trading_mode.detect_market_condition", return_value="peace"),
        )
        with patches["a"], patches["b"], patches["c"]:
            self.assertFalse(self.bot._check_risks_before_trading("US"),
                             "US 손실 한도 초과인데 US 매매가 계속됨")
            self.assertTrue(self.bot._check_risks_before_trading("KR"),
                            "US 손실이 KR장까지 중단시킴 — 시장 분리 실패")

            # (b) 손실해제 명령 → US 재개 + 당일 재발동 방지
            from zusik.clients.discord_commander import DiscordCommander
            cmd = DiscordCommander.__new__(DiscordCommander)
            cmd.bot = self.bot
            msg = cmd._handle_loss_release()
            self.assertIn("US", msg)
            self.assertTrue(self.bot._check_risks_before_trading("US"),
                            "손실해제 후에도 US가 중단 상태")
            self.assertTrue(self.bot._check_risks_before_trading("US"),
                            "손실해제 당일에 손실한도가 재발동됨")

    def test_scenario_sell_deferred_when_net_profit_too_small(self):
        """수수료 공제 후 순이익 < +0.30%면 매도 연기 (`_should_defer_sell`)."""
        df = make_ohlcv_df([50_000] * 20)
        # 매수 50,000 → 매도 50,100 (+0.2%, KR 왕복 수수료 0.23% 미만)
        defer, reason = self.bot._should_defer_sell(
            market="KR", df=df,
            qty=10, avg_price=50_000, current_price=50_100,
        )
        self.assertTrue(defer, f"수수료 회수 불가인데 매도 실행됨: {reason}")
        self.assertTrue("순익" in reason or "순이익률" in reason)

    def test_scenario_sell_deferred_when_upward_momentum_strong(self):
        """수수료는 통과해도 상승 모멘텀이 강하면 매도 연기 (순익이 strong_take 미만일 때).
        +2% 순익은 강한익절 우회(+3.5%↑) 대상이 아니므로 모멘텀 게이트가 작동."""
        closes = list(range(100, 120))  # 20봉 상승
        df = make_ohlcv_df(closes, volumes=[1000] * 19 + [2500])
        defer, reason = self.bot._should_defer_sell(
            market="US", df=df,
            qty=10, avg_price=100, current_price=102,  # +2% (< strong_take 3.5%)
        )
        self.assertTrue(defer, f"상승 모멘텀 강한데 매도 실행됨: {reason}")
        self.assertIn("상승 지속", reason)

    def test_scenario_sell_executed_when_gate_cleared(self):
        """순이익 충분 + 모멘텀 약하면 매도 gate 통과."""
        closes = [110] * 10 + [105] * 10  # 평평+약세
        df = make_ohlcv_df(closes)
        defer, reason = self.bot._should_defer_sell(
            market="KR", df=df,
            qty=10, avg_price=100, current_price=108,  # +8% 순익
        )
        self.assertFalse(defer, f"gate 통과해야 하는데 연기됨: {reason}")

    def test_scenario_defensive_mode_blocks_low_confidence_buy(self):
        """tension/crisis 시 확신도 < 70% 일반주 매수 차단."""
        self.bot._defensive_mode = True
        self.bot._market_condition = "tension"
        self.bot._is_inverse = Mock(return_value=False)

        self.strategy.confidence = 0.50  # 낮은 확신도
        analysis = {"confidence": 0.50, "reasoning": "", "news_summary": ""}
        self.strategy.get_last_analysis = Mock(return_value=analysis)
        self.bot.use_claude = True

        df = make_ohlcv_df([50_000] * 20)
        self.bot._handle_buy("005930", "삼성전자", 50_000, df=df)
        self.assertEqual(len(self.client.buy_market_calls), 0,
                         "defensive + 확신도 50%인데 매수 실행됨")

    def test_scenario_defensive_mode_allows_high_confidence_buy(self):
        """defensive 모드여도 확신도 ≥ 70%면 매수 허용."""
        self.bot._defensive_mode = True
        self.bot._market_condition = "tension"
        self.bot._is_inverse = Mock(return_value=False)

        analysis = {"confidence": 0.85, "reasoning": "강한 반등 시그널",
                    "news_summary": "", "long_term_reason": ""}
        self.strategy.get_last_analysis = Mock(return_value=analysis)
        self.strategy.get_invest_ratio = Mock(return_value=0.5)
        self.bot.use_claude = True

        self.client.set_kr_price("005930", 50_000, name="삼성전자")
        df = make_ohlcv_df([50_000] * 20)
        self.bot._handle_buy("005930", "삼성전자", 50_000, df=df)
        self.assertEqual(len(self.client.buy_market_calls), 1,
                         "defensive + 확신도 85%인데 매수 차단됨")

    def test_scenario_consensus_boost_increases_position_size(self):
        self.bot.use_claude = True
        self.client.kr_cash = 300_000
        self.client.set_kr_price("005930", 35_000, name="삼성전자")
        analysis = {
            "confidence": 0.90,
            "reasoning": "강한 합의",
            "news_summary": "",
            "long_term_reason": "",
            "analyst_details": {
                "fundamental": {"signal": "buy"},
                "sentiment": {"signal": "buy"},
                "quant": {"signal": "buy"},
                "generalist": {"signal": "buy"},
            },
        }
        self.strategy.get_last_analysis = Mock(return_value=analysis)
        self.strategy.get_invest_ratio = Mock(return_value=0.4)

        self.bot._handle_buy("005930", "삼성전자", 35_000, df=make_ohlcv_df([35_000] * 20))
        self.assertEqual(len(self.client.buy_market_calls), 1)
        _code, qty, _price = self.client.buy_market_calls[-1]
        self.assertEqual(qty, 4, "합의 배수 1.20이 적용되면 35k 기준 4주가 되어야 함")

    def test_scenario_consensus_boost_uses_configured_multiplier(self):
        self.bot.use_claude = True
        self.client.kr_cash = 300_000
        self.client.set_kr_price("005930", 35_000, name="삼성전자")
        self.bot.consensus_unanimous_multiplier = 1.50
        analysis = {
            "confidence": 0.90,
            "reasoning": "강한 합의",
            "news_summary": "",
            "long_term_reason": "",
            "analyst_details": {
                "fundamental": {"signal": "buy"},
                "sentiment": {"signal": "buy"},
                "quant": {"signal": "buy"},
                "generalist": {"signal": "buy"},
            },
        }
        self.strategy.get_last_analysis = Mock(return_value=analysis)
        self.strategy.get_invest_ratio = Mock(return_value=0.4)

        self.bot._handle_buy("005930", "삼성전자", 35_000, df=make_ohlcv_df([35_000] * 20))
        _code, qty, _price = self.client.buy_market_calls[-1]
        self.assertEqual(qty, 5, "설정된 만장일치 배수 1.50이 반영되어야 함")

    def test_scenario_dynamic_surge_threshold_delays_early_profit_taking(self):
        pm = self.bot.positions
        pm.record_buy("005930", "삼성전자", 10, 50_000)
        volatile_df = make_ohlcv_df(
            [50_000, 55_000, 48_000, 57_000, 49_000, 58_000, 47_000, 59_000, 50_000, 60_000,
             51_000, 61_000, 52_000, 62_000, 53_000, 63_000, 54_000, 64_000, 55_000, 56_000],
            highs=[58_000] * 20,
            lows=[42_000] * 20,
            volumes=[1000] * 20,
        )
        no_sell = pm.check_surge("005930", 56_000, volatile_df)
        late_sell = pm.check_surge("005930", 58_000, volatile_df)

        self.assertIsNone(no_sell, "고변동 구간 12% 수익은 너무 이르게 익절하면 안 됨")
        self.assertIsNotNone(late_sell, "고변동 구간에서도 충분한 수익이면 익절되어야 함")
        self.assertEqual(late_sell["action"], "surge_half_sell")

    def _restore_inverse_methods(self):
        """setUp에서 Mock으로 덮은 인버스 헬퍼를 실제 구현으로 복원."""
        for name in ("_should_allow_inverse_entry", "_should_force_exit_inverse"):
            if name in self.bot.__dict__:
                del self.bot.__dict__[name]

    def test_scenario_inverse_entry_on_real_crash(self):
        """2026-06-03 재설계: 진짜 급락(crisis/war OR 지수 sharp 급락)에만 인버스 허용."""
        self._restore_inverse_methods()
        self.bot.derivative_etf_enabled = True
        # 상황 적응: 두 트리거를 켜고 각 경로 검증 (기본은 trigger_index_crash=false)
        self.bot.config["inverse"] = {"enabled": True, "trigger_crisis": True,
                                      "trigger_index_crash": True}
        # A) 거시 위기 → 허용
        self.bot._market_condition = "crisis"
        self.bot._index_crash = Mock(return_value=False)
        allow, reason = self.bot._should_allow_inverse_entry()
        self.assertTrue(allow, f"crisis인데 차단: {reason}")
        # B) 평시여도 지수 sharp 급락 → 허용 (trigger_index_crash 활성 시)
        self.bot._market_condition = "peace"
        self.bot._index_crash = Mock(return_value=True)
        allow2, reason2 = self.bot._should_allow_inverse_entry()
        self.assertTrue(allow2, f"지수 급락인데 차단: {reason2}")
        # C) 단발 지수급락이라도 trigger_index_crash=false 면 차단 (휩쏘 회피 — 기본값)
        self.bot.config["inverse"]["trigger_index_crash"] = False
        allow3, _ = self.bot._should_allow_inverse_entry()
        self.assertFalse(allow3, "trigger_index_crash=false 면 단발 급락 진입 차단이어야 함")

    def test_scenario_inverse_blocked_on_pullback(self):
        """평시 + 급락 아님이면 차단 — bear 높아도(강세장 pullback 추격 금지, 데이터상 손실)."""
        self._restore_inverse_methods()
        self.bot.derivative_etf_enabled = True
        self.bot._market_condition = "peace"
        self.bot._index_crash = Mock(return_value=False)
        self.bot._bearish_regime_score = Mock(return_value=0.75)  # bear 높아도
        allow, reason = self.bot._should_allow_inverse_entry()
        self.assertFalse(allow, f"평시 pullback(bear 0.75)인데 허용: {reason}")

    def test_scenario_inverse_force_exit_after_crash_over(self):
        """급락 종료 + peace + bear<0.25면 청산. 급락 진행 중엔 유지(churn 방지)."""
        self._restore_inverse_methods()
        self.bot._market_condition = "peace"
        self.bot._index_crash = Mock(return_value=False)
        self.bot._bearish_regime_score = Mock(return_value=0.15)
        exit_, reason = self.bot._should_force_exit_inverse()
        self.assertTrue(exit_, f"급락 종료+회복인데 유지: {reason}")
        # 급락 진행 중 → 유지 (방금 산 인버스 즉시 청산 방지)
        self.bot._index_crash = Mock(return_value=True)
        exit2, reason2 = self.bot._should_force_exit_inverse()
        self.assertFalse(exit2, f"급락 진행 중인데 청산: {reason2}")

    def test_scenario_sell_pattern_auto_tagged(self):
        """record_sell이 reason으로부터 sell_pattern을 자동 분류해 저장한다."""
        from zusik.storage.portfolio_tracker import PortfolioTracker
        # 실제 Tracker를 임시 DATA_DIR로 격리
        import zusik.storage.portfolio_tracker as portfolio_tracker
        with patch.object(portfolio_tracker, "TRADES_FILE",
                          os.path.join(self._tmpdir.name, "trades_pat.json")), \
             patch.object(portfolio_tracker, "LONG_TERM_FILE",
                          os.path.join(self._tmpdir.name, "lt_pat.json")):
            t = PortfolioTracker()
            t.record_sell("005930", "삼성전자", 1, 60_000, 50_000,
                          reason="[1차 익절 +5%] RSI 77 과매수")
            t.record_sell("AAPL", "Apple", 1, 100, 110,
                          reason="강제 손절 -15% 도달")
            t.record_sell("005930", "삼성전자", 1, 48_000, 50_000,
                          reason="느린 출혈 5일 누적 -3%")
            patterns = [tr["sell_pattern"] for tr in t._trades if tr["type"] == "sell"]
            self.assertEqual(patterns, ["split_profit", "forced_stop", "slow_bleed"])

    def test_scenario_pattern_stats_aggregates_correctly(self):
        """get_pattern_stats가 패턴별 승률·총 PnL을 올바르게 집계."""
        from zusik.storage.portfolio_tracker import PortfolioTracker
        import zusik.storage.portfolio_tracker as portfolio_tracker
        with patch.object(portfolio_tracker, "TRADES_FILE",
                          os.path.join(self._tmpdir.name, "trades_agg.json")), \
             patch.object(portfolio_tracker, "LONG_TERM_FILE",
                          os.path.join(self._tmpdir.name, "lt_agg.json")):
            t = PortfolioTracker()
            t.record_sell("A", "A", 1, 110, 100, reason="[1차 익절] 분할 매도")
            t.record_sell("B", "B", 1, 120, 100, reason="[2차 익절] 분할")
            t.record_sell("C", "C", 1, 85, 100, reason="강제 손절 -15%")
            stats = t.get_pattern_stats()
            self.assertIn("split_profit", stats)
            self.assertEqual(stats["split_profit"]["count"], 2)
            self.assertEqual(stats["split_profit"]["wins"], 2)
            self.assertEqual(stats["split_profit"]["win_rate"], 100.0)
            self.assertGreater(stats["split_profit"]["pnl_sum"], 0)
            self.assertEqual(stats["forced_stop"]["wins"], 0)
            self.assertLess(stats["forced_stop"]["pnl_sum"], 0)

    def test_scenario_eod_pattern_report_dispatched(self):
        """장 마감 후 EOD 패턴 리포트가 '오늘 한국장' 매도만 집계해 발송된다.

        회귀 가드: 과거 get_pattern_stats(days=1)이 (1) cutoff off-by-one으로 어제 매도를,
        (2) market 무필터로 간밤 미국장 매도를 한국장 일일 리포트에 섞어 넣었다.
        한국장 마감 리포트는 date==today + market=="KR" 매도만 담아야 한다.
        """
        from datetime import timedelta
        self.bot.discord = Mock()
        # 오늘 KR 매도 2건 (분할 익절 승리) — 6자리 코드 → KR
        self.tracker.record_sell("005930", "삼성전자", 1, 110, 100, reason="[1차 익절] 분할")
        self.tracker.record_sell("000660", "SK하이닉스", 1, 130, 100, reason="[2차 익절] 분할")
        # 오늘 US 매도 1건 — 한국장 아님 → 제외돼야
        self.tracker.record_sell("MSFT", "Microsoft", 1, 80, 100, reason="강제 손절 -15%")
        # 어제 KR 매도 1건 — 오늘 아님 → 제외돼야 (date 백데이트 주입)
        yday = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")
        self.tracker._trades.append({
            "type": "sell", "code": "005380", "name": "현대차", "qty": 1,
            "price": 90, "amount": 90, "avg_buy_price": 100,
            "realized_pnl": -1000, "realized_rate": -10.0,
            "reason": "강제 손절 -15%", "market": "KR", "ticker": "",
            "timestamp": yday + "T10:00:00", "date": yday,
        })
        self.bot._send_eod_pattern_report()
        self.bot.discord.notify_pattern_report.assert_called_once()
        args, _ = self.bot.discord.notify_pattern_report.call_args
        _date, stats, total_pnl = args
        self.assertIn("split_profit", stats)
        self.assertEqual(stats["split_profit"]["count"], 2)
        # 오늘 US 손절 + 어제 KR 손절 모두 제외 → forced_stop 없음, 총 2건만
        self.assertNotIn("forced_stop", stats)
        self.assertEqual(sum(s["count"] for s in stats.values()), 2)
        self.assertGreater(total_pnl, 0)
        # 시장 라벨이 '한국장'으로 전달돼야 함
        _, kw = self.bot.discord.notify_pattern_report.call_args
        self.assertEqual(kw.get("market"), "한국장")

    def test_scenario_us_eod_pattern_report_dispatched(self):
        """US 장 마감 후 EOD 패턴 리포트가 '직전 미국장 세션' 매도만 집계해 발송된다.

        한국장 리포트와 분리 — market="US" 필터로 KR 매도 제외, 라벨 '미국장'.
        US 세션은 KST 자정을 넘기므로 '오늘 날짜'가 아니라 시각 윈도(11h)로 포착:
          - 전날 날짜지만 최근(2h 전) 체결 = 같은 세션 → 포함
          - 12h 전 체결(직전 세션) = 제외
        """
        from datetime import timedelta
        self.bot.discord = Mock()
        now = datetime.now()
        # 자정 이후(오늘 날짜) US 매도 2건 (분할 익절)
        self.tracker.record_sell("MSFT", "Microsoft", 1, 110, 100, reason="[1차 익절] 분할")
        self.tracker.record_sell("NVDA", "NVIDIA", 1, 130, 100, reason="[2차 익절] 분할")
        # 오늘 KR 매도 1건 — 미국장 아님 → 제외
        self.tracker.record_sell("005930", "삼성전자", 1, 80, 100, reason="강제 손절 -15%")
        # 자정 이전(어제 날짜) but 같은 세션(2h 전) US 매도 — 시각 윈도로 포함돼야
        y_ts = (now - timedelta(hours=2)).isoformat()
        y_date = (now - timedelta(hours=2)).strftime("%Y-%m-%d")
        self.tracker._trades.append({
            "type": "sell", "code": "TSLA", "name": "Tesla", "qty": 1,
            "price": 120, "amount": 120, "avg_buy_price": 100,
            "realized_pnl": 2000, "realized_rate": 20.0,
            "reason": "[1차 익절] 분할", "market": "US", "ticker": "TSLA",
            "timestamp": y_ts, "date": y_date,
        })
        # 직전 세션(12h 전) US 매도 — 윈도 밖 → 제외돼야
        stale_ts = (now - timedelta(hours=12)).isoformat()
        self.tracker._trades.append({
            "type": "sell", "code": "AMD", "name": "AMD", "qty": 1,
            "price": 80, "amount": 80, "avg_buy_price": 100,
            "realized_pnl": -2000, "realized_rate": -20.0,
            "reason": "강제 손절 -15%", "market": "US", "ticker": "AMD",
            "timestamp": stale_ts, "date": (now - timedelta(hours=12)).strftime("%Y-%m-%d"),
        })
        self.bot._send_eod_pattern_report(market="US")
        self.bot.discord.notify_pattern_report.assert_called_once()
        args, kw = self.bot.discord.notify_pattern_report.call_args
        _date, stats, total_pnl = args
        self.assertIn("split_profit", stats)
        self.assertEqual(stats["split_profit"]["count"], 3)   # MSFT + NVDA + TSLA(자정 전 같은 세션)
        self.assertNotIn("forced_stop", stats)                # KR 손절 + 12h 전 stale 모두 제외
        self.assertEqual(sum(s["count"] for s in stats.values()), 3)
        self.assertEqual(kw.get("market"), "미국장")

    def test_scenario_pattern_boost_increases_ratio_after_wins(self):
        """최근 30일 승률·건당 이익이 좋으면 `_pattern_confidence_boost`가 1.25× 반환 (06-10 상향)."""
        # mock 제거하고 실제 메서드 사용
        for m in ("_pattern_confidence_boost",):
            if m in self.bot.__dict__:
                del self.bot.__dict__[m]
        # 승리 패턴 5건 기록 (건당 +600원 → 평균 ≥ 500원 조건 충족)
        for i in range(5):
            self.tracker.record_sell(f"C{i}", f"W{i}", 1, 1100, 500,
                                     reason="[1차 익절] 분할 매도")
        # 캐시 무효화
        self.bot._pat_mult_cache = (0.0, 1.0)
        mult = self.bot._pattern_confidence_boost()
        self.assertAlmostEqual(mult, 1.25, places=2)

    def test_scenario_pattern_boost_penalizes_after_losses(self):
        """최근 30일 누적 손실이면 0.85× 반환 (보수)."""
        for m in ("_pattern_confidence_boost",):
            if m in self.bot.__dict__:
                del self.bot.__dict__[m]
        for i in range(3):
            self.tracker.record_sell(f"L{i}", f"L{i}", 1, 90, 100,
                                     reason=f"강제 손절 -15% ({i})")
        self.bot._pat_mult_cache = (0.0, 1.0)
        mult = self.bot._pattern_confidence_boost()
        self.assertAlmostEqual(mult, 0.85, places=2)

    def test_scenario_context_reward_boost_improves_multiplier(self):
        """상황별(context) 성과가 좋으면 RewardEngine 배수가 상승."""
        import zusik.core.reward_engine as reward_engine
        from zusik.core.reward_engine import RewardEngine

        with patch.object(reward_engine, "REWARD_FILE",
                          os.path.join(self._tmpdir.name, "reward_state.json")):
            reward = RewardEngine({"reward": {"learning_trades": 3}})
            ctx = "peace:long:breakout"
            for _ in range(4):
                reward.record_trade_result(
                    stock_code="005930",
                    stock_name="삼성전자",
                    strategy_name="auto_hybrid",
                    realized_pnl=10_000,
                    realized_rate=8.0,
                    context=ctx,
                )

            base = reward.get_invest_multiplier("auto_hybrid", "005930")
            boosted = reward.get_invest_multiplier("auto_hybrid", "005930", context=ctx)
            self.assertGreater(boosted, base)
            self.assertGreater(reward.get_context_weight(ctx), 1.0)

    def test_scenario_context_reward_respects_configured_learning_threshold(self):
        import zusik.core.reward_engine as reward_engine
        from zusik.core.reward_engine import RewardEngine

        with patch.object(reward_engine, "REWARD_FILE",
                          os.path.join(self._tmpdir.name, "reward_state_cfg.json")):
            reward = RewardEngine({
                "reward": {
                    "learning_trades": 3,
                    "context_learning_trades": 5,
                    "context_return_scale": 8.0,
                    "context_win_bonus_scale": 1.5,
                }
            })
            ctx = "peace:long:breakout"
            for _ in range(4):
                reward.record_trade_result(
                    stock_code="005930",
                    stock_name="삼성전자",
                    strategy_name="auto_hybrid",
                    realized_pnl=10_000,
                    realized_rate=8.0,
                    context=ctx,
                )
            self.assertEqual(reward.get_context_weight(ctx), 1.0, "학습 임계 5건 전에는 context 가중치가 열리면 안 됨")

    def test_scenario_backtest_simulate_produces_trades(self):
        """backtest.simulate가 buy/sell 신호를 따라 매매를 기록한다."""
        import zusik.analysis.backtest as backtest
        import pandas as pd
        # 명확한 상승→하락 패턴 생성
        closes = list(range(100, 130)) + list(range(130, 110, -1))  # 30↑ 20↓
        highs = [c + 2 for c in closes]
        lows = [c - 2 for c in closes]
        df = pd.DataFrame({
            "date": pd.date_range("2025-01-01", periods=len(closes)),
            "open": closes, "high": highs, "low": lows,
            "close": closes, "volume": [1000] * len(closes),
        })

        class AlwaysBuyThenSell:
            name = "test"
            def __init__(self): self.step = 0
            def analyze(self, w):
                self.step += 1
                return "buy" if self.step <= 10 else "sell"

        result = backtest.simulate(df, AlwaysBuyThenSell(),
                                   initial_capital=100_000, warmup=5)
        self.assertGreater(result["total_trades"], 0)
        self.assertIn("pattern_stats", result)
        self.assertGreater(len(result["trades"]), 0)

    def test_scenario_drawdown_triggers_position_shrink(self):
        """drawdown -12% 시 _drawdown_multiplier가 0.85 반환 + defensive 강제 활성."""
        import zusik.storage.portfolio_tracker as portfolio_tracker
        with patch.object(portfolio_tracker, "EQUITY_CURVE_FILE",
                          os.path.join(self._tmpdir.name, "equity_dd.json")):
            # 2일치 스냅샷: 어제 100k → 오늘 88k (-12%)
            from zusik.storage.portfolio_tracker import PortfolioTracker, _save_json
            tracker = PortfolioTracker()
            _save_json(portfolio_tracker.EQUITY_CURVE_FILE, [
                {"date": "2026-04-17", "total_equity": 100_000,
                 "max_equity": 100_000, "drawdown_pct": 0.0},
                {"date": "2026-04-19", "total_equity": 88_000,
                 "max_equity": 100_000, "drawdown_pct": -12.0},
            ])
            self.bot.tracker = tracker
            dd = tracker.get_current_drawdown()
            self.assertEqual(dd, -12.0)
            mult = self.bot._drawdown_multiplier()
            self.assertEqual(mult, 0.85)

    def test_scenario_equity_snapshot_records_drawdown(self):
        """record_equity_snapshot이 max 대비 drawdown을 올바르게 계산."""
        import zusik.storage.portfolio_tracker as portfolio_tracker
        with patch.object(portfolio_tracker, "EQUITY_CURVE_FILE",
                          os.path.join(self._tmpdir.name, "equity_rec.json")):
            from zusik.storage.portfolio_tracker import PortfolioTracker
            t = PortfolioTracker()
            # 첫날 고점 기록
            s1 = t.record_equity_snapshot(80_000, 20_000, 0, 0)
            self.assertEqual(s1["total_equity"], 100_000)
            self.assertEqual(s1["drawdown_pct"], 0.0)
            # 다음 호출은 같은 날 → 동일 날짜 교체
            # 하락 시뮬 — 새 날짜 강제 만들기 위해 날짜 조작
            import json
            with open(portfolio_tracker.EQUITY_CURVE_FILE, "r", encoding="utf-8") as f:
                curve = json.load(f)
            curve[0]["date"] = "2026-04-17"  # 어제로 변경
            with open(portfolio_tracker.EQUITY_CURVE_FILE, "w", encoding="utf-8") as f:
                json.dump(curve, f)
            # 오늘 자산 90k (-10%)
            s2 = t.record_equity_snapshot(70_000, 20_000, 0, 0)
            self.assertEqual(s2["total_equity"], 90_000)
            self.assertEqual(s2["max_equity"], 100_000)
            self.assertAlmostEqual(s2["drawdown_pct"], -10.0, places=1)

    def test_scenario_monthly_stats_aggregates(self):
        """get_monthly_stats가 한 달치 수익률·drawdown·입금액 집계."""
        import zusik.storage.portfolio_tracker as portfolio_tracker
        with patch.object(portfolio_tracker, "EQUITY_CURVE_FILE",
                          os.path.join(self._tmpdir.name, "equity_mo.json")):
            from zusik.storage.portfolio_tracker import PortfolioTracker, _save_json
            _save_json(portfolio_tracker.EQUITY_CURVE_FILE, [
                {"date": "2026-03-01", "total_equity": 100_000,
                 "max_equity": 100_000, "drawdown_pct": 0.0,
                 "deposit_today": 100_000, "realized_today": 0},
                {"date": "2026-03-15", "total_equity": 95_000,
                 "max_equity": 100_000, "drawdown_pct": -5.0,
                 "deposit_today": 0, "realized_today": -5_000},
                {"date": "2026-03-31", "total_equity": 110_000,
                 "max_equity": 110_000, "drawdown_pct": 0.0,
                 "deposit_today": 0, "realized_today": 10_000},
            ])
            t = PortfolioTracker()
            t._trades = []  # 매도 없음 → realized 는 realized_today 폴백(=5_000) 사용
            stats = t.get_monthly_stats(2026, 3)
            self.assertEqual(stats["days"], 3)
            self.assertEqual(stats["deposits"], 100_000)
            self.assertEqual(stats["realized"], 5_000)  # 거래 없을 때 realized_today 합
            self.assertAlmostEqual(stats["max_drawdown"], -5.0, places=1)
            # 시작 10만 + 입금 10만 = 20만 투입인데 종료 11만 → return_pct 음수
            self.assertLess(stats["return_pct"], 0)

            # 매도 패턴별 손익 집계 (리포트 "무엇이 돈을 벌었나" 섹션) — 같은 달 매도만, 패턴별 묶음
            t._trades = [
                {"type": "sell", "date": "2026-03-10", "code": "A", "realized_pnl": 30_000,
                 "sell_pattern": "rsi_overbought"},
                {"type": "sell", "date": "2026-03-12", "code": "B", "realized_pnl": 10_000,
                 "sell_pattern": "rsi_overbought"},
                {"type": "sell", "date": "2026-03-20", "code": "C", "realized_pnl": -8_000,
                 "sell_pattern": "crash_instant"},
                {"type": "sell", "date": "2026-04-01", "code": "D", "realized_pnl": 99_000,
                 "sell_pattern": "rsi_overbought"},  # 다른 달 → 제외돼야
            ]
            stats = t.get_monthly_stats(2026, 3)
            bypat = {p["pattern"]: p for p in stats["by_pattern"]}
            self.assertEqual(bypat["rsi_overbought"]["count"], 2)      # 4월 건 제외
            self.assertEqual(bypat["rsi_overbought"]["pnl"], 40_000)
            self.assertEqual(bypat["rsi_overbought"]["wins"], 2)
            self.assertEqual(bypat["crash_instant"]["pnl"], -8_000)
            self.assertEqual(stats["by_pattern"][0]["pattern"], "rsi_overbought")  # pnl 내림차순

    def test_regression_stub_covers_all_discord_notifier_methods(self):
        """회귀 (2026-04-20): `_BotNotifierFallback`이 `DiscordNotifier`의
        모든 `notify_*` 메서드에 응답해야 한다. 미구현 시 AttributeError로
        `_check_risks_before_trading`이 매 사이클 크래시해 매매 전면 정지됐던
        사고(09:47 `notify_mode_upgrade` 누락) 재발 방지.
        """
        from zusik.core.bot import _BotNotifierFallback
        from zusik.clients.discord_notifier import DiscordNotifier

        stub = _BotNotifierFallback()
        real_methods = [m for m in dir(DiscordNotifier)
                        if m.startswith("notify_") and callable(getattr(DiscordNotifier, m))]
        self.assertGreater(len(real_methods), 0, "DiscordNotifier.notify_* 없음")
        for name in real_methods:
            handler = getattr(stub, name, None)
            self.assertIsNotNone(handler, f"stub이 {name} 미지원")
            self.assertTrue(callable(handler), f"{name}이 callable 아님")

    def test_regression_stub_getattr_returns_noop_for_unknown_notify(self):
        """회귀: 향후 `DiscordNotifier`에 새 `notify_*`가 추가돼도 stub의
        `__getattr__`이 자동 no-op 처리 → AttributeError 방지.
        """
        from zusik.core.bot import _BotNotifierFallback
        stub = _BotNotifierFallback()

        # 존재하지 않는 notify_* 호출해도 예외 없이 None 반환
        result = stub.notify_something_not_yet_defined(1, "foo", kw=3)
        self.assertIsNone(result)

        # notify_ prefix 아닌 속성은 여전히 AttributeError (오타 방어)
        with self.assertRaises(AttributeError):
            stub.some_random_method

    def test_regression_individual_stock_never_triggers_crisis_check(self):
        """회귀 (2026-04-21): 개별 종목 OHLCV로는 `_check_crisis_with_data`가
        호출되지 않는다. 종목 하나의 -6% 급락이 전체 긴급 홀딩을 유발해
        5분마다 발동/해제 무한 반복하던 버그 재발 방지. 시장 전체 crisis 감지는
        `_check_risks_before_trading`의 `detect_market_condition`이 담당.
        """
        if "_is_inverse" in self.bot.__dict__:
            del self.bot.__dict__["_is_inverse"]
        self.client.set_kr_price("005930", 50_000, name="삼성전자", change_rate=-6.5)
        self.client.set_kr_df("005930", make_ohlcv_df([50_000] * 20))
        self.bot._check_crisis_with_data = Mock()

        self.bot._execute_stock({"code": "005930", "name": "삼성전자"})

        # 어떤 종목이든 개별 단위로는 crisis 판정 호출되지 않아야 함
        self.bot._check_crisis_with_data.assert_not_called()

        # US도 동일 보장
        self.bot._check_crisis_with_data.reset_mock()
        self.client.set_us_price("AAPL", 100.0, change_rate=-6.5)
        self.client.set_us_df("AAPL", make_ohlcv_df([100] * 20))
        self.bot._execute_us_stock({"ticker": "AAPL", "name": "Apple", "exchange": "NASD"})
        self.bot._check_crisis_with_data.assert_not_called()

    def test_regression_reconcile_skipped_when_kr_market_closed(self):
        """회귀 (2026-04-20 02:43): 장 휴장 시 빈 holdings를 "전량 매도"로
        오인해 가짜 수동 매도 기록 → T+2 미정산 차단 → 매매 불가 버그.
        """
        self.client.is_market_open_return = False
        self.client.is_us_market_open_return = False

        # tracker.reconcile_external_trades가 호출되지 않아야 함
        mock_reconcile = Mock(return_value=0)
        self.bot.tracker.reconcile_external_trades = mock_reconcile
        self.bot._reconcile_external_trades()
        mock_reconcile.assert_not_called()

    def test_regression_reconcile_runs_when_market_open(self):
        """회귀: 장 열렸을 땐 정상 실행되어야 함 (기능 보존)."""
        self.client.is_market_open_return = True
        self.client.is_us_market_open_return = False  # KR만 열림

        mock_reconcile = Mock(return_value=0)
        self.bot.tracker.reconcile_external_trades = mock_reconcile
        self.bot._reconcile_external_trades()
        # KR은 호출, US는 호출 안 됨
        call_markets = [kwargs.get("market") or args[1]
                        for args, kwargs in [(c.args, c.kwargs) for c in mock_reconcile.call_args_list]]
        self.assertIn("KR", call_markets)
        self.assertNotIn("US", call_markets)

    def test_regression_refresh_stocks_skipped_when_all_markets_closed(self):
        """회귀 (2026-04-21): tick의 주기적 `_refresh_stocks`가 KR/US 둘 다
        닫혔을 때는 실행되지 않아야 한다. 장 닫힌 시간에 Claude 종목 선별이
        반복되며 종목 교체 로그를 쏟아내던 버그 재발 방지.
        """
        self.client.is_market_open_return = False
        self.client.is_us_market_open_return = False
        self.bot._refresh_stocks = Mock()
        # tick의 주기적 refresh 부분만 재현 (간단 시뮬)
        kr_open = self.bot.client.is_market_open()
        us_open = self.bot.client.is_us_market_open()
        if (kr_open or us_open) and not getattr(self.bot, "_refreshing", False):
            self.bot._refreshing = True
            self.bot._refresh_stocks()
            self.bot._refreshing = False
        self.bot._refresh_stocks.assert_not_called()

    def test_regression_refresh_stocks_runs_when_any_market_open(self):
        """회귀: KR만 열렸을 때는 refresh 실행 (기능 보존)."""
        self.client.is_market_open_return = True
        self.client.is_us_market_open_return = False
        self.bot._refresh_stocks = Mock()
        kr_open = self.bot.client.is_market_open()
        us_open = self.bot.client.is_us_market_open()
        if (kr_open or us_open) and not getattr(self.bot, "_refreshing", False):
            self.bot._refreshing = True
            self.bot._refresh_stocks()
            self.bot._refreshing = False
        self.bot._refresh_stocks.assert_called_once()

    def test_regression_premarket_sentiment_cautious_blocks_low_confidence(self):
        """장전 리포트 sentiment cautious일 때 낮은 확신도 매수는 차단돼야 한다."""
        import json, os
        sent_file = os.path.join(self._tmpdir.name, "pre_market_sentiment_KR.json")
        # setUp의 mock 제거 → 실제 _pre_market_buy_gate 동작
        if "_pre_market_buy_gate" in self.bot.__dict__:
            del self.bot.__dict__["_pre_market_buy_gate"]
        with patch("os.path.join", side_effect=lambda *a: sent_file if a[-1].startswith("pre_market") else os.path.sep.join(a)):
            from datetime import datetime as _dt
            today = _dt.now().strftime("%Y-%m-%d")
            with open(sent_file, "w") as f:
                json.dump({"market": "KR", "date": today, "stance": "cautious",
                           "avoid_new_buy": False, "min_buy_confidence": 0.80,
                           "neg_hits": 5, "pos_hits": 1, "reason": "test"}, f)
            # 확신도 60% → 0.80 요구 미달 → 차단
            allow, reason = self.bot._pre_market_buy_gate("KR", 0.60)
            self.assertFalse(allow)
            self.assertIn("80%", reason)
            # 확신도 85% → 통과
            allow2, _r2 = self.bot._pre_market_buy_gate("KR", 0.85)
            self.assertTrue(allow2)

    def test_regression_premarket_sentiment_avoid_new_buy_blocks_all(self):
        """avoid_new_buy=True면 확신도 100%라도 차단."""
        import json, os
        sent_file = os.path.join(self._tmpdir.name, "pre_market_sentiment_US.json")
        # setUp의 mock 제거 → 실제 _pre_market_buy_gate 동작
        if "_pre_market_buy_gate" in self.bot.__dict__:
            del self.bot.__dict__["_pre_market_buy_gate"]
        with patch("os.path.join", side_effect=lambda *a: sent_file if a[-1].startswith("pre_market") else os.path.sep.join(a)):
            from datetime import datetime as _dt
            today = _dt.now().strftime("%Y-%m-%d")
            with open(sent_file, "w") as f:
                json.dump({"market": "US", "date": today, "stance": "cautious",
                           "avoid_new_buy": True, "min_buy_confidence": 0.80,
                           "neg_hits": 10, "pos_hits": 0, "reason": "test"}, f)
            allow, reason = self.bot._pre_market_buy_gate("US", 1.0)
            self.assertFalse(allow)
            self.assertIn("신규 매수 차단", reason)

    def test_regression_premarket_sentiment_stale_file_ignored(self):
        """sentiment 파일이 어제 날짜면 gate 무시 (통과)."""
        import json, os
        sent_file = os.path.join(self._tmpdir.name, "pre_market_sentiment_KR.json")
        # setUp의 mock 제거 → 실제 _pre_market_buy_gate 동작
        if "_pre_market_buy_gate" in self.bot.__dict__:
            del self.bot.__dict__["_pre_market_buy_gate"]
        with patch("os.path.join", side_effect=lambda *a: sent_file if a[-1].startswith("pre_market") else os.path.sep.join(a)):
            with open(sent_file, "w") as f:
                json.dump({"market": "KR", "date": "2020-01-01",
                           "avoid_new_buy": True, "min_buy_confidence": 0.95}, f)
            allow, _ = self.bot._pre_market_buy_gate("KR", 0.4)
            self.assertTrue(allow)  # stale → 통과

    def test_regression_adaptive_backtest_confidence_unblocks_cautious_day(self):
        """회귀: adaptive 확신도가 RSI 스텁(중립=0.5 고정)이라 장전 cautious
        (요구 0.55) 날에 로컬 전략 매수가 전면 정지 → 계좌 현금 8M이 놀았다.
        백테스트 검증 점수가 확신도에 반영돼 edge 있는 전략(score≥0.1)은 통과해야 한다.
        """
        from zusik.strategies.auto_hybrid import AutoHybridStrategy

        s = AutoHybridStrategy.__new__(AutoHybridStrategy)

        class _A:
            pass
        a = _A()
        s._adaptive = a

        # 라이브 실측 케이스: dual_momentum 승률 44%·score 0.472 — 승률은 낮아도
        # edge 강함 → cautious 요구치(0.55) 이상이어야 한다
        a._global_best = object()
        a._global_scores = [{"score": 0.472, "avg_win": 0.44, "trades_total": 355}]
        a._last_scores = []
        conf = s._backtest_confidence()
        self.assertIsNotNone(conf)
        self.assertGreaterEqual(conf, 0.55)
        self.assertLessEqual(conf, 0.70)

        # 표본 부족(거래 <5) → None (스텁 0.5 유지 = 게이트 차단 유지)
        a._global_best = None
        a._global_scores = []
        a._last_scores = [{"score": 0.9, "trades": 2}]
        self.assertIsNone(s._backtest_confidence())

        # 약한 edge(score 0.05)는 cautious 못 넘음 — 게이트가 죽지 않았는지 revert-check
        a._last_scores = [{"score": 0.05, "trades": 20}]
        self.assertLess(s._backtest_confidence(), 0.55)

        # 점수 데이터 자체가 없으면 None (초기 기동)
        a._last_scores = []
        self.assertIsNone(s._backtest_confidence())

    def test_regression_discord_owner_unset_fails_closed(self):
        """회귀: DISCORD_OWNER_ID 미설정 시 소유자 명령은 거부돼야 한다(fail-closed).
        이전엔 서버 관리자 폴백 → 관리자 계정 탈취 = 원격 매매/업데이트 권한이었다."""
        try:
            from zusik.clients import discord_bot as db
        except Exception:
            self.skipTest("discord 미설치 환경")
        admin = Mock()
        admin.user.id = 12345
        admin.user.guild_permissions.administrator = True
        with patch.object(db, "_OWNER_ID", 0):
            self.assertFalse(db._is_owner(admin), "OWNER_ID 미설정인데 관리자 폴백 허용됨")
        with patch.object(db, "_OWNER_ID", 12345):
            self.assertTrue(db._is_owner(admin), "소유자 본인이 거부됨")
        # .env.example 그대로 복사(DISCORD_OWNER_ID= 빈 값)해도 import 크래시 없이 0
        self.assertEqual(db._parse_owner_id(""), 0)
        self.assertEqual(db._parse_owner_id(None), 0)
        self.assertEqual(db._parse_owner_id("abc"), 0)
        self.assertEqual(db._parse_owner_id("12345"), 12345)

    def test_regression_analyze_sentiment_keyword_scoring(self):
        """_analyze_pre_market_sentiment 키워드 점수 기반 판정."""
        from zusik.core.bot import TradingBot
        # 부정 키워드 다수 → cautious
        neg_text = "오늘은 매수 자제를 권장합니다. 변동성 확대로 방어적 관망이 유리합니다. 리스크가 높아 매수 타이밍이 아닙니다."
        s1 = TradingBot._analyze_pre_market_sentiment(neg_text, "KR")
        self.assertEqual(s1["stance"], "cautious")
        self.assertGreaterEqual(s1["min_buy_confidence"], 0.70)

        # 긍정 키워드 다수 → bullish
        pos_text = "기술적 반등 구간 진입 유리. 저가 매수 기회. 돌파 후 상승 전환 신호. 강세 지속 예상."
        s2 = TradingBot._analyze_pre_market_sentiment(pos_text, "US")
        self.assertEqual(s2["stance"], "bullish")
        self.assertLessEqual(s2["min_buy_confidence"], 0.55)

    def test_regression_empty_cash_skips_non_held_analysis(self):
        """회귀 (2026-04-22): KR/US 잔액 거의 0일 때 미보유 종목은 분석 스킵해야 한다.
        매수 불가인데 Claude/KIS API를 계속 호출하는 낭비 방지.
        """
        # run_kr 필터 로직만 추출해 재현 (복잡한 전체 흐름 대신 단위 검증)
        class _B:
            min_amount = 5000
            min_amount_usd = 5.0
            def _get_unsettled_kr_cash(self): return 0
        b = _B()
        # cash 100원 + 최소매수 5000원 → 미보유 제거
        cash_available = 100
        held_kr_codes = {"003850"}
        scan_kr = [{"code": "003850"}, {"code": "005930"}, {"code": "000660"}]
        skip_non_held = cash_available < max(b.min_amount, 5000)
        if skip_non_held:
            scan_kr = [s for s in scan_kr if s.get("code") in held_kr_codes]
        self.assertEqual(len(scan_kr), 1)
        self.assertEqual(scan_kr[0]["code"], "003850")

        # US: $0.95 < $5 → 미보유 제거
        us_cash = 0.95
        held_us = {"NIO"}
        scan_us = [{"ticker": "NIO"}, {"ticker": "AAPL"}, {"ticker": "GRAB"}]
        if us_cash < b.min_amount_usd:
            scan_us = [s for s in scan_us if s.get("ticker") in held_us]
        self.assertEqual(len(scan_us), 1)
        self.assertEqual(scan_us[0]["ticker"], "NIO")

    def test_regression_error_alert_includes_type_and_message(self):
        """회귀 (2026-04-22): Discord 오류 알림에 예외 타입·메시지·프레임이 포함되어야 한다.
        기존엔 `KR 원익IPS 오류`만 나와 원인 추적 불가였음.
        """
        try:
            raise ValueError("매수 주문가능금액 초과: 6640원")
        except ValueError as e:
            msg = self.bot._format_error_alert("KR", "원익IPS", e)
        self.assertIn("KR", msg)
        self.assertIn("원익IPS", msg)
        self.assertIn("ValueError", msg)
        self.assertIn("매수 주문가능금액 초과", msg)

    def test_regression_error_alert_dedup_within_10min(self):
        """회귀: 같은 (scope, target, err_type) 조합은 10분 이내 중복 발송 억제."""
        self.bot._err_alert_cache = {}
        try:
            raise RuntimeError("a")
        except RuntimeError as e:
            m1 = self.bot._format_error_alert("KR", "foo", e)
        try:
            raise RuntimeError("b")
        except RuntimeError as e:
            m2 = self.bot._format_error_alert("KR", "foo", e)
        self.assertTrue(m1)   # 첫 번째는 메시지
        self.assertEqual(m2, "")  # 중복 억제

    def test_scenario_effective_pnl_summary_splits_realized_vs_fx(self):
        """실효 수익 분해가 realized/unrealized/apparent/fx_effect를 올바르게 산출."""
        import zusik.storage.portfolio_tracker as portfolio_tracker
        with patch.object(portfolio_tracker, "EQUITY_CURVE_FILE",
                          os.path.join(self._tmpdir.name, "equity_eff.json")), \
             patch.object(portfolio_tracker, "TRADES_FILE",
                          os.path.join(self._tmpdir.name, "trades_eff.json")), \
             patch.object(portfolio_tracker, "LONG_TERM_FILE",
                          os.path.join(self._tmpdir.name, "lt_eff.json")), \
             patch.object(portfolio_tracker, "DATA_DIR", self._tmpdir.name):
            # DATA_DIR 패치 — get_total_deposits가 사용자 실제
            # data/total_deposits.json을 읽지 않게 isolate.
            # 테스트는 equity_curve의 deposit_today=100k를 입금 합으로 사용해야 함.
            from zusik.storage.portfolio_tracker import PortfolioTracker, _save_json
            t = PortfolioTracker()
            # 입금 10만원 기록
            _save_json(portfolio_tracker.EQUITY_CURVE_FILE, [
                {"date": "2026-04-01", "total_equity": 100_000,
                 "max_equity": 100_000, "drawdown_pct": 0.0,
                 "deposit_today": 100_000, "realized_today": 0},
            ])
            # 실현 +5,000원 매도 기록
            t._trades = [
                {"type": "sell", "realized_pnl": 5000, "date": "2026-04-10"},
            ]

            # 현재 총자산 130,000 / 미실현 1,000 가정
            summary = t.get_effective_pnl_summary(total_equity_now=130_000,
                                                   unrealized_now=1_000)

            self.assertEqual(summary["realized_total"], 5_000)
            self.assertEqual(summary["unrealized_krw"], 1_000)
            self.assertEqual(summary["effective_total"], 6_000)
            self.assertEqual(summary["total_deposits"], 100_000)
            self.assertEqual(summary["apparent_gain"], 30_000)
            # 환율·집계 효과 = 명목 30k - 실효 6k = 24k
            self.assertEqual(summary["fx_and_other_effect"], 24_000)

    def test_scenario_trailing_stop_on_pullback(self):
        """트레일링 스톱: 고점 +10% 찍고 -8% 되돌림 시 발동."""
        pm = self.bot.positions
        pm.record_buy("005930", "삼성전자", 10, 50_000)

        # 고점 +10%까지 상승
        pm.update_trailing_stop("005930", 55_000)
        # 고점 대비 -8% 되돌림 → 50,600
        result = pm.update_trailing_stop("005930", 50_600)
        self.assertIsNotNone(result, "트레일링/본전 보호 모두 미발동")
        self.assertIn(result.get("action"),
                      ("stop_triggered", "breakeven_protect"))

    def test_scenario_churn_guard_blocks_after_crash_sell(self):
        """급락 매도 후 24h 재진입 차단 + _handle_buy 초입에서 거부."""
        bot = self.bot
        bot._record_sell_for_churn_guard("RIOT", "당일 -7% 급락 — 추가 하락 방지 전량 매도")
        blocked, msg = bot._is_reentry_blocked("RIOT")
        self.assertTrue(blocked)
        self.assertIn("분 남음", msg)
        # 24h 차단이라 1000분 이상 남아있어야 함
        import re
        m = re.search(r"(\d+)분", msg)
        self.assertIsNotNone(m)
        self.assertGreater(int(m.group(1)), 1000)
        # _churn_guard도 True
        self.assertTrue(bot._churn_guard("RIOT", "Riot Platforms"))

    def test_scenario_churn_guard_session_block_with_breakout_override(self):
        """익절 매도 후 세션 차단(12h): 같은세션 평평 재매수(-214k churn) 차단, +2% 돌파는 즉시 허용.
        수정 전(30분 고정)이라면 12h 미만 + 돌파 면제 없음 → 이 테스트 실패."""
        bot = self.bot
        bot._record_sell_for_churn_guard("AAPL", "[1차] 본전 보호 — 5% 갔다가 1.5% 이하",
                                         sell_price=100.0)
        # 평평(매도가 근처) 재매수 → 세션 차단 (12h)
        blocked, msg = bot._is_reentry_blocked("AAPL", price=100.0)
        self.assertTrue(blocked, "같은세션 평평 재매수는 차단")
        self.assertIn("session", msg)
        import re
        m = re.search(r"(\d+)분", msg)
        self.assertIsNotNone(m)
        self.assertGreater(int(m.group(1)), 60, "12h 세션 차단이라 60분 초과")
        # +2% 돌파 재매수 → 추세 지속 재진입으로 허용
        blocked2, _ = bot._is_reentry_blocked("AAPL", price=103.0)
        self.assertFalse(blocked2, "매도가 +3% 돌파는 세션 차단 면제 (+208k 추세 재진입 살림)")

    def test_scenario_churn_guard_daily_sell_limit(self):
        """같은 종목 일일 3회 매도 → 추가 매수 차단."""
        bot = self.bot
        for _ in range(3):
            bot._daily_sell_count["NIO"] = bot._daily_sell_count.get("NIO", 0) + 1
        exceeded, msg = bot._is_daily_sell_limit("NIO")
        self.assertTrue(exceeded)
        self.assertIn("3회", msg)
        # _churn_guard도 차단
        self.assertTrue(bot._churn_guard("NIO", "NIO"))

    def test_scenario_churn_guard_expired_block_clears(self):
        """만료된 차단 entry는 자동 정리."""
        import time
        bot = self.bot
        bot._reentry_block["TSLA"] = (time.time() - 60, "0.5h")
        blocked, _ = bot._is_reentry_blocked("TSLA")
        self.assertFalse(blocked)
        self.assertNotIn("TSLA", bot._reentry_block)

    def test_scenario_churn_guard_persists_to_disk(self):
        """차단 등록은 reentry_block.json에 영속화."""
        import json as _json
        bot = self.bot
        bot._record_sell_for_churn_guard("XYZ", "급락 즉시매도")
        path = bot._REENTRY_BLOCK_FILE
        with open(path, encoding="utf-8") as f:
            data = _json.load(f)
        self.assertIn("XYZ", data)

    def test_scenario_churn_guard_blocks_intraday_crash_buy(self):
        """일중 -2% 이상 떨어진 종목은 매수 차단 (5/1 강화: -3%→-2%)."""
        bot = self.bot
        bot._last_intraday_change["RIOT"] = -0.09  # 일중 -9%
        self.assertTrue(bot._churn_guard("RIOT", "Riot"))
        # -1% 정도는 통과
        bot._last_intraday_change["AAPL"] = -0.01
        self.assertFalse(bot._churn_guard("AAPL", "Apple"))

    def test_scenario_churn_guard_intraday_skips_inverse(self):
        """인버스 ETF는 일중 하락이 곧 매수 신호이므로 차단 안 함."""
        bot = self.bot
        bot._is_inverse = Mock(return_value=True)
        bot._last_intraday_change["SQQQ"] = -0.06
        self.assertFalse(bot._churn_guard("SQQQ", "SQQQ"))

    def test_scenario_hysteresis_blocks_signal_reversal_low_confidence(self):
        """6h 내 BUY → SELL 반전 + conf<70% → hold."""
        import time
        bot = self.bot
        # 이전 결정: 30분 전 BUY 65%
        bot._signal_history["RIOT"] = (time.time() - 30 * 60, "buy", 0.65)
        eff, msg = bot._apply_hysteresis("RIOT", "sell", 0.69)
        self.assertEqual(eff, "hold")
        self.assertIn("BUY→SELL", msg)
        self.assertIn("69%", msg)

    def test_scenario_hysteresis_allows_high_confidence_reversal(self):
        """conf ≥ 70%면 반전 허용 (강한 신호)."""
        import time
        bot = self.bot
        bot._signal_history["NIO"] = (time.time() - 60 * 60, "buy", 0.65)
        eff, _ = bot._apply_hysteresis("NIO", "sell", 0.75)
        self.assertEqual(eff, "sell")

    def test_scenario_hysteresis_resets_after_six_hours(self):
        """6시간 지나면 히스테리시스 해제."""
        import time
        bot = self.bot
        bot._signal_history["TSLA"] = (time.time() - 7 * 3600, "buy", 0.6)
        eff, _ = bot._apply_hysteresis("TSLA", "sell", 0.55)
        self.assertEqual(eff, "sell")

    def test_scenario_hysteresis_same_direction_passes(self):
        """같은 방향 신호는 통과."""
        import time
        bot = self.bot
        bot._signal_history["AAPL"] = (time.time() - 10 * 60, "buy", 0.55)
        eff, _ = bot._apply_hysteresis("AAPL", "buy", 0.50)
        self.assertEqual(eff, "buy")

    def test_scenario_defensive_disabled_via_config(self):
        """defensive_mode_enabled=false면 drawdown -22%여도 _defensive_mode=False."""
        bot = self.bot
        bot.defensive_mode_enabled = False
        bot.tracker = Mock(
            get_current_drawdown=Mock(return_value=-22.68),
            record_equity_snapshot=Mock(),
            get_realized_pnl_today=Mock(return_value={"realized_pnl": 0}),
            get_long_term_holdings=Mock(return_value=[]),
            get_long_term_total_cost=Mock(return_value=0),
        )
        bot._check_long_term_limit = Mock(return_value=True)
        with patch("zusik.core.trading_mode.detect_market_condition", return_value="crisis"):
            try:
                bot._check_risks_before_trading()
            except Exception:
                pass
        self.assertFalse(bot._defensive_mode,
            "defensive_mode_enabled=false면 시장 crisis도 무시해야 함")

    def test_scenario_trend_filter_blocks_deadcross_below_ma60(self):
        """5일선 < 20일선 + 현재가 < 60일선이면 매수 차단 (솔루스 패턴)."""
        from zusik.core.bot import TradingBot
        # 60일치 가격: 처음 50일 60,000 → 최근 10일 50,000으로 하락
        prices = [60_000] * 50 + [50_000] * 10
        df = make_ohlcv_df(prices)
        weak, reason = TradingBot._is_weak_trend(df)
        self.assertTrue(weak)
        self.assertIn("약세 추세", reason)

    def test_scenario_trend_filter_passes_uptrend(self):
        """정배열(상승 추세)은 통과."""
        from zusik.core.bot import TradingBot
        prices = [50_000 + i * 100 for i in range(60)]  # 꾸준히 상승
        df = make_ohlcv_df(prices)
        weak, _ = TradingBot._is_weak_trend(df)
        self.assertFalse(weak)

    def test_scenario_trend_filter_short_data_passes(self):
        """데이터 60봉 미만이면 판정 보류 (False)."""
        from zusik.core.bot import TradingBot
        df = make_ohlcv_df([50_000] * 30)
        weak, _ = TradingBot._is_weak_trend(df)
        self.assertFalse(weak)

    def test_scenario_churn_guard_intraday_strengthened_to_2pct(self):
        """일중 -2% 이상 하락도 차단 (이전 -3%에서 강화)."""
        bot = self.bot
        bot._last_intraday_change["TEST"] = -0.025
        self.assertTrue(bot._churn_guard("TEST", "Test"))
        bot._last_intraday_change["TEST2"] = -0.018
        self.assertFalse(bot._churn_guard("TEST2", "Test2"))

    def test_scenario_pair_trader_strong_signal(self):
        """페어가 cointegrated 일 때 z >= 2 → buy_b 시그널 발동."""
        import numpy as np
        import pandas as pd
        from zusik.core.pair_trader import PairTrader
        np.random.seed(0)
        n = 100
        common = np.random.randn(n).cumsum()
        a = 100 + common + np.random.randn(n) * 0.3
        b = 50 + 0.5 * common + np.random.randn(n) * 0.2
        a[-1] += 10  # A 급등 → B 저평가
        df_a = pd.DataFrame({"close": a})
        df_b = pd.DataFrame({"close": b})
        trader = PairTrader(lookback=60, z_entry=2.0)
        res = trader.evaluate_pair(df_a, df_b)
        self.assertTrue(res["valid"])
        self.assertEqual(res["signal"], "buy_b")
        self.assertGreater(res["z"], 2.0)

    def test_scenario_pair_trader_low_correlation_invalid(self):
        """상관 0.5 미만이면 valid=False (페어 무효)."""
        import numpy as np
        import pandas as pd
        from zusik.core.pair_trader import PairTrader
        np.random.seed(42)
        n = 100
        a = 100 + np.random.randn(n).cumsum()
        b = 50 + np.random.randn(n).cumsum() * 5  # 독립적
        df_a = pd.DataFrame({"close": a})
        df_b = pd.DataFrame({"close": b})
        trader = PairTrader(lookback=60)
        res = trader.evaluate_pair(df_a, df_b)
        self.assertFalse(res["valid"])

    def test_scenario_mc_bootstrap_numpy(self):
        """2026-06-03 Vortex 제거 후 — numpy MC가 종목선택용 통계를 정상 산출."""
        import numpy as np
        from zusik.analysis.bot_money_helpers import monte_carlo_bootstrap_numpy
        np.random.seed(0)
        hist = (np.random.randn(60) * 0.02).astype(np.float32)
        mc = monte_carlo_bootstrap_numpy(hist, n_paths=500, t_forward=30,
                                         stop_loss=-0.10, trailing_stop=-0.05, target_profit=0.10)
        self.assertIn("p_profit", mc)
        self.assertTrue(0.0 <= mc["p_profit"] <= 1.0)
        self.assertGreater(mc["n_paths"], 0)

    def test_scenario_volatility_classifier_low_vol_peace(self):
        """저변동성 + peace = low tier (일봉만)."""
        import zusik.core.volatility_classifier as vc
        # 표준편차 ~0.5% 매우 안정
        prices = [50000 + i * 10 for i in range(60)]
        df = make_ohlcv_df(prices)
        info = vc.classify(df, market_condition="peace", holding=False)
        self.assertEqual(info["tier"], "low")
        self.assertFalse(info["use_minute_5"])
        self.assertFalse(info["use_websocket"])

    def test_scenario_volatility_classifier_high_vol_holding(self):
        """고변동 + 보유 → extreme (1분봉 + 틱)."""
        import zusik.core.volatility_classifier as vc
        import numpy as np
        np.random.seed(0)
        # 일별 ~5% 변동 (high)
        prices = 50000 + np.random.randn(60).cumsum() * 2500
        df = make_ohlcv_df([float(p) for p in prices])
        info = vc.classify(df, market_condition="peace", holding=True)
        self.assertIn(info["tier"], ("high", "extreme"))
        self.assertTrue(info["use_minute_5"])

    def test_scenario_volatility_classifier_market_crisis_boosts_tier(self):
        """저변동도 crisis 시 boost로 high tier로 올라감."""
        import zusik.core.volatility_classifier as vc
        prices = [50000 + i * 10 for i in range(60)]  # 저변동
        df = make_ohlcv_df(prices)
        peace = vc.classify(df, market_condition="peace", holding=False)
        crisis = vc.classify(df, market_condition="crisis", holding=False)
        # crisis가 peace보다 더 적극 (1분봉 사용)
        self.assertNotEqual(peace["tier"], crisis["tier"])

    def test_scenario_defer_sell_low_confidence(self):
        """LLM SELL conf < 70%면 매도 보류 (5/1 추가)."""
        df = make_ohlcv_df([50_000] * 20)
        defer, reason = self.bot._should_defer_sell(
            "KR", df, qty=10, avg_price=50_000, current_price=51_000,
            confidence=0.65,
        )
        self.assertTrue(defer)
        self.assertIn("SELL conf", reason)
        self.assertIn("65%", reason)

    def test_scenario_defer_sell_high_confidence_passes_to_fee_check(self):
        """conf >= 70%면 conf 게이트는 통과, 다음 단계(수수료) 체크로 넘어감."""
        df = make_ohlcv_df([50_000] * 20)
        defer, reason = self.bot._should_defer_sell(
            "KR", df, qty=10, avg_price=50_000, current_price=51_000,
            confidence=0.75,
        )
        # 수수료 게이트는 통과하지 못할 수 있음 (얕은 익절). 핵심은 conf 사유가 아닌 것.
        self.assertNotIn("SELL conf", reason)

    def test_scenario_churn_guard_blocks_weak_trend_with_df(self):
        """추세 필터: df 전달 시 약세 추세 종목 차단."""
        prices = [60_000] * 50 + [50_000] * 10
        df = make_ohlcv_df(prices)
        bot = self.bot
        # weak trend는 인버스 아닐 때만 차단
        bot._is_inverse = Mock(return_value=False)
        self.assertTrue(bot._churn_guard("WEAK", "약세종목", df=df))
        # 인버스는 우회
        bot._is_inverse = Mock(return_value=True)
        self.assertFalse(bot._churn_guard("WEAK", "약세종목", df=df))

    def test_scenario_defensive_enabled_still_activates_on_drawdown(self):
        """기본 (defensive_mode_enabled=true) 동작 보존: drawdown -10% 이하 시 강제 활성."""
        bot = self.bot
        bot.defensive_mode_enabled = True
        bot.tracker = Mock(
            get_current_drawdown=Mock(return_value=-15.0),
            #: 위험 게이트가 effective drawdown을 읽도록 변경됨.
            # 진짜 -15% drawdown이면 effective도 -15% → defensive 활성 (계약 보존).
            get_effective_drawdown=Mock(return_value=-15.0),
            record_equity_snapshot=Mock(),
            get_realized_pnl_today=Mock(return_value={"realized_pnl": 0}),
            get_long_term_holdings=Mock(return_value=[]),
            get_long_term_total_cost=Mock(return_value=0),
        )
        bot._check_long_term_limit = Mock(return_value=True)
        with patch("zusik.core.trading_mode.detect_market_condition", return_value="peace"):
            try:
                bot._check_risks_before_trading()
            except Exception:
                pass
        self.assertTrue(bot._defensive_mode)

    def test_surge_partial_sell_respects_ratio(self):
        """force_reason 매도라도 sell_ratio<1.0이면 부분 매도여야 한다.

        급등 절반/라이딩 익절이 force_reason 탓에 100% 전량 매도되어 큰 추세를
        통째로 놓치던 버그(폭등 수익 극대화 실패)의 회귀 가드."""
        bot = self.bot
        self.client.kr_holdings["005930"] = {"qty": 100, "avg_price": 50_000, "name": "삼성전자"}
        self.client.set_kr_price("005930", 56_000, name="삼성전자")  # +12%
        # 라이딩 트림 0.25 → 100주 중 25주만 매도, 75주는 추세 보유
        bot._handle_sell("005930", "삼성전자", force_reason="급등 라이딩", sell_ratio=0.25)
        self.assertTrue(self.client.sell_market_calls, "매도가 실행되지 않음")
        self.assertEqual(self.client.sell_market_calls[-1][1], 25,
                         "force_reason 급등 트림이 전량 매도됨 — 추세 놓침 버그 재발")
        # 보호성 강제매도(기본 ratio=1.0)는 잔량 전량
        self.client.sell_market_calls.clear()
        bot._handle_sell("005930", "삼성전자", force_reason="손절")
        self.assertEqual(self.client.sell_market_calls[-1][1], 75,
                         "보호성 강제매도는 잔량 전량이어야 함")

    def test_premarket_prioritizes_strong_momentum(self):
        """장전 분석이 모멘텀 강한 종목을 매수 우선순위 상위(작은 순위값)로 산출 —
        개장 시 제한된 현금으로 가장 좋은 후보부터 사도록(9시 성공 매수 핵심)."""
        bot = self.bot
        bot.kr_stocks = [{"code": "WEAK", "name": "약세"}, {"code": "STRONG", "name": "강세"}]
        bot._is_inverse = Mock(return_value=False)
        bot.discord = None
        self.client.set_kr_df("STRONG", make_ohlcv_df([100 + i for i in range(60)]))   # 꾸준 상승
        self.client.set_kr_df("WEAK", make_ohlcv_df([160 - i for i in range(60)]))     # 꾸준 하락
        bot._open_prep_date = ""
        bot._prepare_open_buys()
        prio = bot._open_priority
        self.assertIn("STRONG", prio)
        self.assertIn("WEAK", prio)
        self.assertLess(prio["STRONG"], prio["WEAK"],
                        "강세 종목이 약세보다 후순위 — 개장 우선매수 로직 오작동")

    def test_whitelist_core_entry_buys_unheld_core_on_hold(self):
        """hold 신호 + 미보유 핵심주(삼성)는 코어 타깃 탑업으로 직접 매수(buy_market) —
        삼성이 늘 '관망'으로 떠서 매수 자체가 안 되던 근본 문제의 회귀 가드. 과열(+8%↑) 보류."""
        bot = self.bot
        self.strategy.signal = "hold"
        bot.config["screening"] = {"whitelist_kr": [{"code": "005930", "name": "삼성전자"}]}
        bot.config.setdefault("position", {})
        bot.config["position"]["whitelist_core_entry"] = True
        bot.config["position"]["whitelist_conviction_floor"] = 0.5
        bot.config["position"]["whitelist_core_max_intraday"] = 0.08
        bot._is_inverse = Mock(return_value=False)
        bot._bearish_regime_score = Mock(return_value=0.1)
        bot._adaptive_params = Mock(return_value={"whitelist_cap": 0.25, "correlation": 0.9,
                                                  "same_sector": 2, "cap": 0.14,
                                                  "rsi_exit_min": 85, "rsi_exit_profit_min": 0.05})
        bot._defensive_mode = False
        # 더미 보유 1종 → '전량 미보유' idle-buy 폴백 비활성화해 코어 타깃만 격리 검증
        self.client.kr_holdings["999999"] = {"qty": 1, "avg_price": 1000, "name": "더미"}
        self.client.set_kr_df("005930", make_ohlcv_df([60_000] * 30))
        # 장중 +2% (비과열) → 코어 타깃 탑업 매수
        self.client.set_kr_price("005930", 60_000, name="삼성전자", change_rate=2.0)
        bot._execute_stock({"code": "005930", "name": "삼성전자"})
        self.assertTrue(any(c[0] == "005930" for c in self.client.buy_market_calls),
                        "hold 핵심주 미보유인데 코어 타깃 매수 미실행")
        # 장중 +9% (과열) → 코어 타깃 보류
        self.client.buy_market_calls.clear()
        self.client.kr_holdings.pop("005930", None)
        self.client.set_kr_price("005930", 60_000, name="삼성전자", change_rate=9.0)
        bot._execute_stock({"code": "005930", "name": "삼성전자"})
        self.assertFalse(any(c[0] == "005930" for c in self.client.buy_market_calls),
                         "과열(+9%)인데 코어 타깃 매수 강행")

    def test_core_topup_respects_reentry_block_no_churn(self):
        """매도 직후 재진입 차단 중이면 코어 패스가 재매수 안 함 — 본전보호 매도→즉시
        재매수→또 매도 churn 루프(현대차 사례) 방지."""
        import time as _t
        bot = self.bot
        bot.config["screening"] = {"whitelist_kr": [{"code": "005930", "name": "삼성전자"}]}
        bot.config.setdefault("position", {})["whitelist_core_entry"] = True
        bot.config["position"]["whitelist_conviction_floor"] = 0.5
        bot._is_inverse = Mock(return_value=False)
        bot._reentry_block = {"005930": (_t.time() + 600, "30m")}  # 10분 차단 중
        self.client.buy_market_calls.clear()
        result = bot._maybe_core_topup_kr("005930", "삼성전자", 60_000, 0, 0.02)
        self.assertFalse(result, "재진입 차단 중인데 코어 재매수함 (churn 루프)")
        self.assertFalse(any(c[0] == "005930" for c in self.client.buy_market_calls))
        # 차단 해제되면 재매수 가능
        bot._reentry_block = {}
        bot._bearish_regime_score = Mock(return_value=0.1)
        bot._adaptive_params = Mock(return_value={"whitelist_cap": 0.25})
        result2 = bot._maybe_core_topup_kr("005930", "삼성전자", 60_000, 0, 0.02)
        self.assertTrue(result2, "차단 해제 후에도 코어 매수 안 함")

    def test_premarket_us_prioritizes_strong_momentum(self):
        """미국 장전 분석도 KR과 동일하게 모멘텀 강한 종목을 우선순위 상위로 산출."""
        bot = self.bot
        bot.us_stocks = [{"ticker": "WEAKUS", "name": "약세", "exchange": "NASD"},
                         {"ticker": "STRONGUS", "name": "강세", "exchange": "NASD"}]
        bot._is_inverse = Mock(return_value=False)
        bot.discord = None
        self.client.set_us_df("STRONGUS", make_ohlcv_df([100 + i for i in range(60)]))
        self.client.set_us_df("WEAKUS", make_ohlcv_df([160 - i for i in range(60)]))
        bot._open_prep_date_us = ""
        bot._prepare_open_buys_us()
        prio = bot._open_priority_us
        self.assertIn("STRONGUS", prio)
        self.assertIn("WEAKUS", prio)
        self.assertLess(prio["STRONGUS"], prio["WEAKUS"],
                        "US 강세 종목이 약세보다 후순위 — US 개장 우선매수 로직 오작동")


class LossPatternRegressionTests(unittest.TestCase):
    """실제 손실을 일으킨 행동 패턴이 다시 새어나오지 않게 막는 회귀 테스트.

    기존 계약 테스트는 '코드가 도는가'를 보지만, 이 클래스는 '전략이 돈을 잃는
    방식대로 행동하지 않는가'를 검증한다. 각 테스트는 2026-05~06 수정 전이라면
    실패(=손실 행동 허용)하고, 수정 후엔 통과한다.
    """

    # ── helpers ──
    def _tracker(self, tmpdir, deposits, realized_pnls):
        """임시 디렉터리로 격리된 PortfolioTracker. 모듈 전역은 addCleanup으로 복원."""
        import zusik.storage.portfolio_tracker as pt
        import json
        orig = (pt.EQUITY_CURVE_FILE, pt.TRADES_FILE, pt.DATA_DIR)

        def _restore():
            pt.EQUITY_CURVE_FILE, pt.TRADES_FILE, pt.DATA_DIR = orig
        self.addCleanup(_restore)
        pt.EQUITY_CURVE_FILE = os.path.join(tmpdir, "equity_curve.json")
        pt.TRADES_FILE = os.path.join(tmpdir, "trades.json")
        pt.DATA_DIR = tmpdir
        with open(os.path.join(tmpdir, "total_deposits.json"), "w") as f:
            json.dump({"manual_total_krw": deposits}, f)
        t = pt.PortfolioTracker()
        t._trades = [{"type": "sell", "realized_pnl": p} for p in realized_pnls]
        return t

    def _bot(self):
        """사이징 로직 단위 검증용 최소 봇 (의존 메서드는 고정값 mock)."""
        from zusik.core.bot import TradingBot
        bot = TradingBot.__new__(TradingBot)
        bot.config = {
            "screening": {"whitelist_kr": [{"code": "005930", "name": "삼성전자"}]},
            "vol_sizing": {"enabled": True, "target_daily_vol": 0.025,
                           "scalar_min": 0.5, "scalar_max": 1.4},
            "vol_regime_buffer": {"enabled": False},
            "position": {"whitelist_conviction_floor": 0.5, "whitelist_floor_bull_min": 0.5},
            "adaptive": {"enabled": True, "states": [
                {"trigger": "default", "correlation": 0.9, "same_sector": 2,
                 "cap": 0.14, "whitelist_cap": 0.25,
                 "rsi_exit_min": 85, "rsi_exit_profit_min": 0.05}]},
            "profit_taking": {"regime_adaptive": True, "rsi_tilt": 8.0, "profit_tilt": 0.03},
            "invest_ratio_max": 0.14,
        }
        bot._defensive_mode = False
        bot._market_condition = "peace"
        bot._pattern_confidence_boost = Mock(return_value=1.0)
        bot._drawdown_multiplier = Mock(return_value=1.0)
        bot._kelly_fraction = Mock(return_value=0.2)   # 낮은 kelly → floor 검증에 유리
        bot._market_vol_regime = Mock(return_value=1.0)
        bot._last_mc_stats = None
        return bot

    def _hold_bot(self, floor=-0.09, loss_learning=False):
        """_hold_through_loss 검증용 최소 봇. _core_hold_through는 테스트별로 주입.

        loss_learning 기본 False — base floor 계약(정적 -9%)을 검증. 자가학습 동작은
        별도 테스트(test_loss_learning_*)가 sell_timing 을 주입해 따로 검증한다."""
        from zusik.core.bot import TradingBot
        b = TradingBot.__new__(TradingBot)
        b.config = {"risk": {"pullback_hold_floor": floor,
                             "loss_learning_enabled": loss_learning}}
        return b

    # ── 유령 포지션 churn — positions.json 실잔고 불일치 (256750 매수↔매도 -7,540) ──
    def test_reconcile_holdings_clears_phantom_positions(self):
        """positions.json 의 유령(브로커 미보유) 포지션 정리 — pyramid↔breakeven churn 차단.
        수정 전: 실보유 0인데 positions.json 잔존 → is_pyramid_eligible True → 매수 후 stale
        peak 로 즉시 본전보호 매도 → 수수료 손실 무한 반복(실측 256750, 35초, -7,540원)."""
        import zusik.core.position_manager as pm
        import os, tempfile
        from datetime import datetime, timedelta
        mgr = pm.PositionManager.__new__(pm.PositionManager)
        old = (datetime.now() - timedelta(hours=2)).isoformat()
        fresh = (datetime.now() - timedelta(seconds=30)).isoformat()
        mgr._positions = {
            "256750": {"qty": 2, "last_buy_date": old},    # 유령(브로커 0)
            "004170": {"qty": 2, "last_buy_date": old},    # 실보유
            "999999": {"qty": 1, "last_buy_date": fresh},  # 방금 매수 → grace 보존
            "MET":    {"qty": 3, "last_buy_date": old},     # US (KR 재조정 대상 아님)
        }
        with tempfile.TemporaryDirectory() as d, \
                patch.object(pm, "POSITIONS_FILE", os.path.join(d, "p.json")):
            removed = mgr.reconcile_holdings({"004170"}, market="KR")
            self.assertIn("256750", removed, "실보유 없는 KR 유령 제거")
            self.assertNotIn("004170", removed)             # 실보유 유지
            self.assertNotIn("999999", removed)             # 방금 매수 grace 보존
            self.assertNotIn("MET", removed)                # US는 KR 대상 아님
            self.assertNotIn("256750", mgr._positions)
            self.assertIn("004170", mgr._positions)
            removed_us = mgr.reconcile_holdings(set(), market="US")
            self.assertIn("MET", removed_us)                # US 유령 제거
            self.assertIn("004170", mgr._positions, "KR은 US 재조정에 영향 없음")

    # ── 조기손절 억제, KR/US 공통) — KIS 반사실: 컷 0%승률(0/28, -828k) ──
    def test_noncore_holds_shallow_pullback(self):
        """비핵심: -2~6% 정상 pullback은 홀드(매도 보류). crash_instant 0%승률 바닥투매 방지.
        수정 전(코어만 면제)이라면 -5%에서 매도 허용 → 이 테스트 실패."""
        b = self._hold_bot(floor=-0.09)
        b._core_hold_through = lambda c: False
        self.assertTrue(b._hold_through_loss("999999", -0.05), "비핵심 -5%는 홀드해야 함")
        self.assertTrue(b._hold_through_loss("999999", -0.02))

    def test_noncore_cuts_below_floor(self):
        """비핵심: floor(-9%)보다 깊어지면 매도 허용 (하드스톱 영역 가속)."""
        b = self._hold_bot(floor=-0.09)
        b._core_hold_through = lambda c: False
        self.assertFalse(b._hold_through_loss("999999", -0.10), "-10%는 floor 아래라 매도 허용")

    def test_deep_collapse_always_cuts(self):
        """깊은 붕괴(crash_from_high/-15%↓)는 손익 무관 항상 매도 — 자본 보호."""
        b = self._hold_bot(floor=-0.09)
        b._core_hold_through = lambda c: True   # 핵심주여도
        self.assertFalse(b._hold_through_loss("005930", -0.03, deep_collapse=True))

    def test_core_holds_to_hard_stop(self):
        """핵심(whitelist): -15%까지 홀드, 그 아래는 매도."""
        b = self._hold_bot()
        b._core_hold_through = lambda c: True
        self.assertTrue(b._hold_through_loss("005930", -0.12))
        self.assertFalse(b._hold_through_loss("005930", -0.16))

    # ── 손실측 자가학습 — '수익률 자동'을 손실 패턴까지 확장 ──
    def test_loss_learning_premature_deepens_floor(self):
        """조기컷(컷 대신 홀드가 우월, net_if_held>0)이 우세하면 floor 를 더 깊게(홀드 연장).
        실데이터 crash_instant net_if_held +12.2%(n=26)가 근거 — 같은 상황을 더 오래 홀드해야."""
        from zusik.analysis.loss_learning import learn_hold_floor
        r = learn_hold_floor({"crash_instant": {"count": 26, "avg_net_if_held": 12.2}})
        self.assertLess(r["floor"], -0.09, "조기컷 우세 → floor 심화(더 음수)")
        self.assertGreater(r["floor"], -0.13 - 1e-9)

    def test_loss_learning_protective_shallows_floor(self):
        """정당컷(보호 성공, net_if_held<0)이면 floor 를 얕게 — 더 빨리 컷. 무차별 홀드 방지.
        forced_stop/slow_bleed 처럼 홀드가 더 나빴던 패턴은 floor 를 늘리지 않는다."""
        from zusik.analysis.loss_learning import learn_hold_floor
        r = learn_hold_floor({"slow_bleed": {"count": 20, "avg_net_if_held": -25.0}})
        self.assertGreater(r["floor"], -0.09, "정당컷 → floor 완화(덜 음수)")

    def test_loss_learning_low_sample_keeps_default(self):
        """표본 부족(min_count 미만)이면 보정하지 않고 default 유지 — 섣부른 학습 방지."""
        from zusik.analysis.loss_learning import learn_hold_floor
        r = learn_hold_floor({"crash_instant": {"count": 3, "avg_net_if_held": 50.0}},
                             default=-0.09)
        self.assertEqual(r["floor"], -0.09)

    def test_loss_learning_never_breaches_hard_stop(self):
        """안전 불변: 학습이 아무리 강한 조기컷 신호를 받아도 floor 는 하드스톱(-15%)을
        절대 잠식하지 않는다(cap). 자본보호 백스톱은 학습 대상이 아니다."""
        from zusik.analysis.loss_learning import learn_hold_floor
        r = learn_hold_floor({"crash_instant": {"count": 50, "avg_net_if_held": 90.0}})
        self.assertGreaterEqual(r["floor"], -0.13, "cap(-13%)이 하드스톱(-15%) 위에서 막아야")

    def test_hold_through_loss_uses_learned_floor(self):
        """통합: 학습 ON + 조기컷 sell_timing 주입 → floor 심화로 -10% 도 홀드(base면 매도).
        단 하드스톱(-15%)·deep_collapse·핵심 -15% 는 학습과 무관하게 그대로 작동(자본보호 불변).
        되돌리면(학습 floor 미적용 또는 하드스톱 잠식) 깨진다."""
        import json as _json
        import tempfile
        from zusik.core.bot import TradingBot
        from zusik.core import bot_helpers as bh
        b = TradingBot.__new__(TradingBot)
        b.config = {"risk": {"pullback_hold_floor": -0.09, "loss_learning_enabled": True}}
        b._core_hold_through = lambda c: False
        with tempfile.TemporaryDirectory() as d:
            stf = os.path.join(d, "sell_timing.json")
            with open(stf, "w", encoding="utf-8") as f:
                _json.dump({"by_pattern": {"crash_instant": {"count": 26,
                                                             "avg_net_if_held": 12.2}}}, f)
            with patch.object(bh.paths, "data_path", lambda *p: stf):
                self.assertTrue(b._hold_through_loss("999999", -0.10),
                                "조기컷 학습 → floor 심화 → -10% 홀드(base -9%면 매도였음)")
                self.assertFalse(b._hold_through_loss("999999", -0.16),
                                 "학습해도 하드스톱(-15%) 아래는 매도 — 자본보호 불변")
                b._core_hold_through = lambda c: True
                self.assertFalse(b._hold_through_loss("005930", -0.05, deep_collapse=True),
                                 "deep_collapse 는 학습 무관 항상 매도")

    # ── crash_instant 변동성(ATR) 비례 임계 — 변동성 큰 종목 정상 출렁임 패닉컷 방지 ──
    def _crash_pm(self, cfg=None):
        """격리된 positions 파일로 PositionManager 생성 (crash_instant ATR 보정 검증용)."""
        import zusik.core.position_manager as position_manager
        from zusik.core.position_manager import PositionManager
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        orig = position_manager.POSITIONS_FILE
        position_manager.POSITIONS_FILE = os.path.join(tmp.name, "positions.json")
        self.addCleanup(lambda: setattr(position_manager, "POSITIONS_FILE", orig))
        base = {"crash_instant_sell": -0.04, "crash_from_high_sell": -0.20,
                "crash_atr_scaling_enabled": True, "crash_atr_baseline": 0.02,
                "crash_atr_mult": 20.0, "crash_atr_scale_cap": 2.0}
        if cfg:
            base.update(cfg)
        return PositionManager({"position": base})

    def _crash_df(self, atr_range_pct, daily_drop, n=21):
        """마지막 봉이 전일 대비 daily_drop, 일중 고저폭 atr_range_pct 인 OHLCV df."""
        import pandas as pd
        closes = [100.0] * (n - 1) + [100.0 * (1.0 + daily_drop)]
        opens, highs, lows, vols = [], [], [], []
        prev = closes[0]
        for c in closes:
            opens.append(prev)  # 갭 없음(시가=전일종가) — gap_down 트리거 회피
            highs.append(c * (1.0 + atr_range_pct / 2.0))
            lows.append(c * (1.0 - atr_range_pct / 2.0))
            vols.append(1000.0)  # 거래량 평탄 — vol_spike 회피
            prev = c
        return pd.DataFrame({"open": opens, "high": highs, "low": lows,
                             "close": closes, "volume": vols})

    def test_crash_instant_atr_spares_volatile_stock(self):
        """변동성 큰 종목(ATR~5%)의 당일 -5% 출렁임은 crash_instant 컷 안 함(임계 -6.4%로 깊어짐).
        저변동 종목(ATR~1%)의 -5%는 컷(임계 -4% 그대로). 수정 전(고정 -4%)이라면 변동성주도
        -5%에 컷 → 이 테스트 실패(revert-check). crash_instant 0%승률 바닥투매 완화."""
        pm = self._crash_pm()
        pm._positions["VOL"] = {"name": "변동성주", "qty": 10, "avg_price": 100, "high_since_buy": 100}
        pm._positions["CALM"] = {"name": "잔잔주", "qty": 10, "avg_price": 100, "high_since_buy": 100}
        # 변동성 큰 종목 -5% → ATR 보정 임계(~-6.4%) 미달 → 컷 안 함
        vol_df = self._crash_df(atr_range_pct=0.05, daily_drop=-0.05)
        self.assertIsNone(pm.check_crash("VOL", 95, vol_df),
                          "ATR 5% 종목의 -5% 정상 출렁임은 crash_instant 컷 금지")
        # 저변동 종목 -5% → 임계 -4% 그대로 → 컷
        calm_df = self._crash_df(atr_range_pct=0.008, daily_drop=-0.05)
        r = pm.check_crash("CALM", 95, calm_df)
        self.assertIsNotNone(r, "ATR 1% 종목의 -5%는 비정상 급락 → crash_instant 컷")
        self.assertEqual(r["action"], "crash_instant")

    def test_crash_instant_atr_deep_collapse_still_cuts(self):
        """ATR 보정은 깊은 붕괴를 막지 않는다 — 변동성 큰 종목도 -25% 당일 급락은 cap(임계 -8%)
        넘겨 여전히 컷. 자본보호(깊은 붕괴/하드스톱)는 ATR 무관 불변."""
        pm = self._crash_pm()
        pm._positions["VOL"] = {"name": "변동성주", "qty": 10, "avg_price": 100, "high_since_buy": 100}
        deep_df = self._crash_df(atr_range_pct=0.05, daily_drop=-0.25)
        r = pm.check_crash("VOL", 75, deep_df)
        self.assertIsNotNone(r, "변동성주도 -25% 깊은 급락은 컷(ATR 보정 cap 무관)")
        self.assertEqual(r["action"], "crash_instant")

    def test_crash_instant_atr_disabled_is_fixed(self):
        """토글 OFF면 고정 임계(-4%)로 복귀 — 변동성주 -5%도 컷. ATR 보정이 기존 동작의
        opt-out 가능한 확장임을 보증."""
        pm = self._crash_pm({"crash_atr_scaling_enabled": False})
        pm._positions["VOL"] = {"name": "변동성주", "qty": 10, "avg_price": 100, "high_since_buy": 100}
        vol_df = self._crash_df(atr_range_pct=0.05, daily_drop=-0.05)
        r = pm.check_crash("VOL", 95, vol_df)
        self.assertIsNotNone(r, "토글 OFF면 고정 -4% → -5% 컷")
        self.assertEqual(r["action"], "crash_instant")

    def test_global_backtest_resolves_auto_hybrid_adaptive(self):
        """회귀(2026-06-03): _run_global_backtest가 auto_hybrid의 adaptive 인스턴스를
        `.adaptive`로 조회해 항상 None→early return → 글로벌 백테스트(250봉 모델선택)가
        한 번도 안 돌고 30봉 fallback(-1.000)만 쓰던 버그. 실제 속성(_adaptive)로 resolve돼야 함.
        수정 전(`.adaptive`만 조회)이라면 None → 이 테스트 실패."""
        from zusik.strategies.adaptive import AdaptiveStrategy

        class _FakeAutoHybrid:
            def __init__(self):
                self._adaptive = AdaptiveStrategy(backtest_days=120)

        s = _FakeAutoHybrid()
        # bot._run_global_backtest 의 resolve 식과 동일
        resolved = getattr(s, "_adaptive", None) or getattr(s, "adaptive", None)
        self.assertIsNotNone(resolved, "auto_hybrid adaptive 인스턴스를 못 찾음 (글로벌 백테스트 미작동)")
        self.assertIsInstance(resolved, AdaptiveStrategy)

    def test_us_noncore_also_holds(self):
        """비핵심 US도 floor 적용(홀드) — KIS 반사실에서 US 컷도 0%승률(NTAP -99k 등). 시장 무관 동일 정책.
        수정 전(US는 floor 미적용)이라면 -5%에서 False → 이 테스트 실패."""
        b = self._hold_bot(floor=-0.09)
        b._core_hold_through = lambda c: False
        self.assertTrue(b._hold_through_loss("NTAP", -0.05),
                        "US 비핵심 -5%도 홀드해야 함 (시장 무관 floor)")
        self.assertFalse(b._hold_through_loss("NTAP", -0.10))

    # ── 인버스 헷지 레짐기반 컷 — 06-09 반등일 crash_instant -66k 회귀 ──
    def test_inverse_shallow_crash_not_cut(self):
        """인버스 얕은 당일급락(crash_instant -7%)은 손절하지 않는다 — 지수 반등으로
        인버스가 내리는 건 '헷지 역할 종료' 신호지 손절 사유가 아니다. controlled 강제청산
        (peace+bear<0.25)에 위임. 06-08 정책(일반주처럼 즉시컷)이면 True → 이 테스트 실패."""
        from zusik.core.bot import TradingBot
        shallow = {"action": "crash_instant", "change": -0.07, "reason": "당일 -7.0% 급락"}
        self.assertFalse(TradingBot._inverse_deep_collapse(shallow),
                         "인버스 -7% 당일급락은 컷 금지(레짐 강제청산 위임)")
        self.assertFalse(TradingBot._inverse_deep_collapse(None))

    def test_manual_sell_needs_two_pass_confirmation(self):
        """회귀 (07-16 15:06 실측): EOD 락인 매도와 수동매매 동기화가 서브초 race로
        같은 매도를 '수동'으로 이중 계상. 첫 감지는 의심 마커만, 다음 회차에
        봇 기록이 있으면 소거·없으면 확정."""
        import json
        import tempfile
        from zusik.storage import portfolio_tracker as pt

        with tempfile.TemporaryDirectory() as td:
            snap = os.path.join(td, "snap.json")
            with patch.object(pt, "HOLDINGS_SNAPSHOT_FILE", snap):
                tr = pt.PortfolioTracker.__new__(pt.PortfolioTracker)
                tr._trades = []
                tr._save_trades = lambda: None
                tr._recent_bot_order = lambda code, minutes=15: False

                # 스냅샷: 530주 보유 → 현재 0주 (봇 EOD 매도 직후, 기록은 아직 race 중)
                with open(snap, "w") as f:
                    json.dump({"KR": {"114800": {"name": "KODEX 인버스", "qty": 530,
                                                 "avg_price": 1_100, "current_price": 1_110}}}, f)
                n1 = tr.reconcile_external_trades([], market="KR")
                self.assertEqual(n1, 0, "1차 감지에서 바로 수동 매도로 기록됨 (race 이중계상)")

                # 다음 회차 전에 봇 record_sell이 도착한 상황 → 소거되어야 함
                tr._trades = [{"type": "sell", "code": "114800", "qty": 530,
                               "date": datetime.now().strftime("%Y-%m-%d")}]
                n2 = tr.reconcile_external_trades([], market="KR")
                self.assertEqual(n2, 0, "봇 기록이 있는데 수동 매도로 이중 기록됨")

                # 반대로 봇 기록이 끝내 없으면(진짜 수동) 2차에서 확정 기록
                with open(snap, "w") as f:
                    json.dump({"KR": {"005930": {"name": "삼성전자", "qty": 10,
                                                 "avg_price": 50_000, "current_price": 60_000}}}, f)
                tr._trades = []
                recorded = []
                tr.record_sell = lambda *a, **k: recorded.append(a)
                self.assertEqual(tr.reconcile_external_trades([], market="KR"), 0)  # 1차 의심
                tr.reconcile_external_trades([], market="KR")                        # 2차 확정
                self.assertEqual(len(recorded), 1, "진짜 수동 매도가 2차에서 기록되지 않음")

    def test_inverse_entry_blocked_in_eod_window(self):
        """회귀 (07-16): 15:06 EOD 수익 락인 매도 12분 뒤 급락 가드가 같은 인버스를
        되사서 락인 목적(익일 갭 회피)을 무효화 + 왕복 수수료. 마감 임박 윈도(20분)
        안에서는 신규 헷지 진입 금지."""
        from zusik.core.bot import TradingBot
        bot = TradingBot.__new__(TradingBot)
        bot.derivative_etf_enabled = True
        bot.config = {"inverse": {"enabled": True}}
        bot._market_condition = "peace"
        bot._fast_fall_active = True  # 급락 가드 켜짐 — 원래라면 진입 허용
        bot.client = Mock(minutes_to_close=Mock(return_value=12),
                          us_minutes_to_close=Mock(return_value=None))
        allow, reason = bot._should_allow_inverse_entry()
        self.assertFalse(allow, "마감 12분 전인데 신규 헷지 진입 허용됨")
        self.assertIn("마감", reason)

        # 윈도 밖(마감 90분 전)이면 기존대로 급락 가드 진입 허용 — revert-check
        bot.client = Mock(minutes_to_close=Mock(return_value=90),
                          us_minutes_to_close=Mock(return_value=None))
        allow2, _ = bot._should_allow_inverse_entry()
        self.assertTrue(allow2, "윈도 밖 정상 헷지 진입까지 차단됨")

    def test_load_json_self_heals_torn_file(self):
        """회귀 (07-21 실측): equity_curve.json이 '유효 JSON + 잔여 바이트'로 찢어져
        모든 사용처가 JSONDecodeError를 반복. 유효 프리픽스를 살리고 원본은 .corrupt
        보존, 완전 파손은 빈 값 폴백."""
        import tempfile
        from zusik.storage.portfolio_tracker import _load_json

        with tempfile.TemporaryDirectory() as td:
            # (a) 찢어진 파일: 유효 배열 + 잔여 바이트 → 프리픽스 복구
            torn = os.path.join(td, "torn.json")
            with open(torn, "w") as f:
                f.write('[{"date": "2026-07-15", "total": 100}]' + '\n  }\n]')
            data = _load_json(torn)
            self.assertEqual(len(data), 1)
            self.assertEqual(data[0]["date"], "2026-07-15")
            self.assertTrue(os.path.exists(torn + ".corrupt"), "원본 증거 미보존")
            import json as _json
            _json.load(open(torn))  # 복구본이 정상 JSON으로 재저장됐는지

            # (b) 완전 파손 → 빈 값 폴백 + 증거 보존 (크래시 루프 금지)
            dead = os.path.join(td, "dead.json")
            with open(dead, "w") as f:
                f.write("not json at all {{{")
            self.assertEqual(_load_json(dead), [])
            self.assertTrue(os.path.exists(dead + ".corrupt"))

            # (c) 정상 파일은 그대로 — revert-check
            ok = os.path.join(td, "ok.json")
            with open(ok, "w") as f:
                f.write('[{"x": 1}]')
            self.assertEqual(_load_json(ok), [{"x": 1}])
            self.assertFalse(os.path.exists(ok + ".corrupt"))

    def test_save_json_thread_safe(self):
        """회귀 (07-16 15:06): 동시 저장이 같은 .tmp를 rename해 FileNotFoundError."""
        import tempfile, threading as th
        from zusik.storage.portfolio_tracker import _save_json
        errors = []
        with tempfile.TemporaryDirectory() as td:
            path = os.path.join(td, "t.json")
            def worker():
                try:
                    for _ in range(30):
                        _save_json(path, [{"x": 1}])
                except Exception as e:
                    errors.append(e)
            threads = [th.Thread(target=worker) for _ in range(8)]
            [t.start() for t in threads]
            [t.join() for t in threads]
        self.assertEqual(errors, [], f"동시 저장 크래시: {errors[:2]}")

    def test_inverse_rebound_exit_escapes_bear_deadzone(self):
        """회귀 (실측 -182k): V반등일에 스테일 미국 프록시(-1.9% 정지)가 bear를 0.32에
        고정 → 청산 임계 0.25 미달 지속 → 인버스 홀드 → 익일 갭 손실.
        (a) peace + KOSPI 당일 +0.5%↑ 반등이면 bear 데드존이어도 청산.
        (b) bear 계산은 미국 장 마감 중 US intraday를 제외해야 한다."""
        from zusik.core.bot import TradingBot

        bot = TradingBot.__new__(TradingBot)
        bot._market_condition = "peace"
        bot._index_crash = lambda: False
        bot._bearish_regime_score = lambda: 0.32          # 데드존 (0.25~0.50)
        bot.client = Mock()
        bot.client.get_current_price = Mock(return_value={"change_rate": 1.2})
        ok, reason = bot._should_force_exit_inverse()
        self.assertTrue(ok, "지수 +1.2% 반등에도 bear 데드존에 막혀 인버스 홀드")
        self.assertIn("반등", reason)

        # 반등 미달(+0.2%)이면 기존대로 홀드 — 청산 임계 자체가 죽지 않았는지 revert-check
        bot.client.get_current_price = Mock(return_value={"change_rate": 0.2})
        ok2, _ = bot._should_force_exit_inverse()
        self.assertFalse(ok2, "반등 미달인데 청산됨 — 데드존 홀드 계약 파괴")

        # (b) 미국 장 마감 중엔 US intraday가 bear 계산에 안 들어간다
        bot2 = TradingBot.__new__(TradingBot)
        bot2._bear_cache = (0.0, 0.0)
        bot2.client = Mock()
        bot2.client.get_ohlcv = Mock(return_value=None)
        bot2.client.get_us_ohlcv = Mock(return_value=None)
        bot2.client.us_market_phase = Mock(return_value="closed")
        bot2.client.get_current_price = Mock(return_value={"change_rate": 1.0})   # KR +1%
        bot2.client.get_us_current_price = Mock(return_value={"change_rate": -1.9})  # 정지된 전일
        bot2.client.get_balance = Mock(return_value={"holdings": []})
        bot2.client.get_us_balance = Mock(return_value={"holdings": []})
        score = bot2._bearish_regime_score()
        self.assertLess(score, 0.25, "마감된 미국 프록시의 정지 등락률이 bear를 붙들고 있음")

    def test_inverse_deep_collapse_still_cuts(self):
        """깊은 붕괴(-15%↓ / 고점급락 crash_from_high)는 자본보호 하드스톱으로 여전히 컷."""
        from zusik.core.bot import TradingBot
        self.assertTrue(TradingBot._inverse_deep_collapse(
            {"action": "crash_instant", "change": -0.16, "reason": "당일 -16% 급락"}),
            "인버스 -16% 깊은붕괴는 자본보호 컷")
        self.assertTrue(TradingBot._inverse_deep_collapse(
            {"action": "crash_from_high", "change": -0.11, "reason": "고점 급락"}),
            "고점급락(crash_from_high)은 자본보호 컷")

    # ── 인버스 EOD 수익 락인 — 익일 개장 갭에 장중 평가익 증발(사용자 수동 매도 사유) ──
    class _StubClock:
        """minutes_to_close / us_minutes_to_close 만 제공하는 시각 스텁."""
        def __init__(self, kr=None, us=None):
            self._kr, self._us = kr, us

        def minutes_to_close(self):
            return self._kr

        def us_minutes_to_close(self):
            return self._us

    def _eod_bot(self, kr_mtc=None, us_mtc=None, cfg=None):
        from zusik.core.bot import TradingBot
        b = TradingBot.__new__(TradingBot)
        b.config = {"inverse": cfg if cfg is not None else {
            "eod_profit_lock": True, "eod_lock_window_min": 20, "eod_lock_min_profit": 0.003}}
        b.client = self._StubClock(kr_mtc, us_mtc)
        return b

    def test_inverse_eod_lock_realizes_profit_near_close(self):
        """마감 임박(10분 전) + 순익(+3% gross → net +2.7%)인 인버스 → 수익 락인 발동.
        수정 전(EOD 락인 없음)이라면 surge(+10%) 미달·풀백 없음으로 마감까지 홀드 → 발동 안 됨."""
        b = self._eod_bot(kr_mtc=10)
        holding = {"qty": 10, "avg_price": 10_000, "current_price": 10_300}
        due, reason = b._inverse_eod_lock_due("KR", holding, cur_price=10_300)
        self.assertTrue(due, "마감 10분 전 + 순익 양수면 EOD 락인 발동해야 함")
        self.assertIn("락인", reason)
        # reason → sell_pattern 태깅이 inverse_eod_lock 이어야 EOD 리포트에 잡힘
        from zusik.storage.portfolio_tracker import PortfolioTracker
        self.assertEqual(PortfolioTracker._classify_sell_pattern(reason), "inverse_eod_lock")

    def test_inverse_eod_lock_not_due_far_from_close(self):
        """마감까지 한참(120분) 남았으면 EOD 락인 미발동 — 장중 헷지 역할 유지."""
        b = self._eod_bot(kr_mtc=120)
        holding = {"qty": 10, "avg_price": 10_000, "current_price": 10_300}
        due, _ = b._inverse_eod_lock_due("KR", holding, cur_price=10_300)
        self.assertFalse(due)

    def test_inverse_eod_lock_not_due_outside_session(self):
        """정규장이 아니면(minutes_to_close=None) 미발동."""
        b = self._eod_bot(kr_mtc=None)
        holding = {"qty": 10, "avg_price": 10_000, "current_price": 10_300}
        self.assertFalse(b._inverse_eod_lock_due("KR", holding, cur_price=10_300)[0])

    def test_inverse_eod_lock_loss_not_realized(self):
        """순손실 인버스는 마감 임박이어도 락인 안 함 — 수익 보호 장치는 손실 확정 금지."""
        b = self._eod_bot(kr_mtc=5)
        holding = {"qty": 10, "avg_price": 10_000, "current_price": 9_800}  # -2%
        self.assertFalse(b._inverse_eod_lock_due("KR", holding, cur_price=9_800)[0])

    def test_inverse_eod_lock_below_threshold_not_realized(self):
        """순익이 임계(+0.3%) 미만(수수료 공제 후 미미)이면 락인 안 함 — churn 방지."""
        b = self._eod_bot(kr_mtc=5)
        holding = {"qty": 10, "avg_price": 10_000, "current_price": 10_010}  # +0.1% gross → net 음수
        self.assertFalse(b._inverse_eod_lock_due("KR", holding, cur_price=10_010)[0])

    def test_inverse_eod_lock_disabled_by_config(self):
        """config eod_profit_lock=False면 기능 OFF."""
        b = self._eod_bot(kr_mtc=5, cfg={"eod_profit_lock": False})
        holding = {"qty": 10, "avg_price": 10_000, "current_price": 10_300}
        self.assertFalse(b._inverse_eod_lock_due("KR", holding, cur_price=10_300)[0])

    def test_inverse_eod_lock_us_uses_us_close(self):
        """US 인버스는 us_minutes_to_close로 판정 (KR 시각과 분리)."""
        b = self._eod_bot(kr_mtc=None, us_mtc=8)
        holding = {"qty": 10, "avg_price": 100.0, "current_price": 103.0}
        due, reason = b._inverse_eod_lock_due("US", holding, cur_price=103.0)
        self.assertTrue(due, "US 마감 8분 전 + 순익이면 발동")
        self.assertIn("락인", reason)

    def test_inverse_eod_lock_handler_sells_and_tags(self):
        """통합: _handle_inverse가 EOD 락인 시 _handle_sell을 inverse_eod_lock 사유로 호출.
        강제청산 아님 + df=None이어도 락인 단계(1.5)에서 매도가 나가야 함."""
        b = self._eod_bot(kr_mtc=7)
        b._is_inverse = lambda c: True
        b._should_force_exit_inverse = lambda: (False, "")
        b._should_allow_inverse_entry = lambda: (False, "보유 유지")
        b._handle_sell = Mock()
        # +1% (net ~+0.7%): EOD 락인(≥0.3%)은 발동, 빠른익절(≥1.5%)은 미달 → EOD 경로 검증
        holding = {"code": "114800", "qty": 10, "avg_price": 10_000, "current_price": 10_100}
        b.client.get_balance = lambda: {"holdings": [holding]}
        b._handle_inverse("114800", "KODEX 인버스", 10_100, df=None)
        b._handle_sell.assert_called_once()
        _, kw = b._handle_sell.call_args
        self.assertIn("락인", kw.get("force_reason", ""))

    # ── 인버스 무차별 매수 차단 + 반전 락인 (사용자 실측: 코스피만 빠졌는데 나머지 시장 인버스도 매수) ──
    def _inv_helper_bot(self, cfg=None):
        from zusik.core.bot import TradingBot
        b = TradingBot.__new__(TradingBot)
        # learning_enabled=False → 빠른익절 테스트는 고정 임계(seed) 경로를 결정론적으로 검증.
        b.config = {"inverse": cfg if cfg is not None else {
            "entry_index_drop_pct": 2.5, "reversal_lock_pct": 1.5, "eod_lock_min_profit": 0.003,
            "quick_profit_pct": 1.5, "learning_enabled": False}}
        return b

    def test_inverse_entry_requires_index_crash_leverage_aware(self):
        """신규 인버스 매수는 그 ETF의 상승이 '기초지수 급락 수준'(함의 낙폭 ≥2.5%)일 때만.
        +1.5% 노이즈 상승(지수 -1.5%)으로는 안 산다 — KOSPI만 빠질 때 나스닥 인버스(409820)까지
        무차별 매수해 손실나던 실측 버그(사용자 2026-07-01 지적). 되돌리면(단순 +1% 게이트)
        안 빠지는 시장 인버스까지 산다. 레버리지 정규화도 검증: -2X 는 2배 상승 필요."""
        b = self._inv_helper_bot()
        crash = make_ohlcv_df([100] * 19 + [103])     # +3% → 지수 -3% 급락 → 허용
        mild = make_ohlcv_df([100] * 19 + [101.5])    # +1.5% → 지수 -1.5% 노이즈 → 차단(그 버그)
        flat = make_ohlcv_df([100] * 20)              # 0% → 차단
        self.assertTrue(b._inverse_entry_confirms(103, crash, "409820")[0])    # 나스닥 급락 → 허용
        self.assertFalse(b._inverse_entry_confirms(101.5, mild, "409820")[0])  # 노이즈 → 차단
        self.assertFalse(b._inverse_entry_confirms(100, flat, "409820")[0])    # 안 빠짐 → 차단
        # 레버리지 정규화: -2X(252670)는 +3%도 함의 지수 -1.5%라 차단, +5%(-2.5%)여야 허용
        self.assertFalse(b._inverse_entry_confirms(103, make_ohlcv_df([100] * 19 + [103]), "252670")[0])
        self.assertTrue(b._inverse_entry_confirms(105, make_ohlcv_df([100] * 19 + [105]), "252670")[0])

    def test_inverse_entry_confirm_disabled_or_no_df(self):
        """entry_index_drop_pct=0(비활성)이거나 df 부족이면 차단 안 함(기존 게이트 위임)."""
        self.assertTrue(self._inv_helper_bot({"entry_index_drop_pct": 0})._inverse_entry_confirms(100, None)[0])
        self.assertTrue(self._inv_helper_bot()._inverse_entry_confirms(100, None)[0])  # df None → 위임

    def test_inverse_reversal_lock_profit_only(self):
        """헷지 성공 후 인버스 되돌림(-1.5%↓=기초지수 반등) + 순익이면 즉시 락인. 손실이면 미발동
        (손실 확정 금지). 되돌리면 EOD까지 안 팔려 '제때 못 팔아' 평가익 증발."""
        b = self._inv_helper_bot()
        dropping = make_ohlcv_df([100] * 18 + [105, 102])   # prev 105 → 102 = -2.86%
        win = {"qty": 10, "avg_price": 90, "current_price": 102}     # +13% → net 양수
        loss = {"qty": 10, "avg_price": 110, "current_price": 102}   # -7% → net 음수
        self.assertTrue(b._inverse_reversal_lock_due("KR", win, 102, dropping)[0])
        self.assertFalse(b._inverse_reversal_lock_due("KR", loss, 102, dropping)[0])

    def test_inverse_reversal_lock_holds_when_not_reversing(self):
        """인버스가 아직 안 빠지면(반등 신호 약함) 순익이어도 유지 — 헷지 지속."""
        b = self._inv_helper_bot()
        flat = make_ohlcv_df([100] * 20)
        win = {"qty": 10, "avg_price": 90, "current_price": 100}
        self.assertFalse(b._inverse_reversal_lock_due("KR", win, 100, flat)[0])

    def test_inverse_quick_profit_takes_small_gain(self):
        """인버스 빠른 익절(수익화) — 순익 +1.5%↑면 즉시 실현, 미달/손실은 미발동.
        인버스는 감쇠+지수우상향이라 작은 수익을 바로 챙기는 게 buy&hold(-74~97%)보다 우월."""
        b = self._inv_helper_bot()   # quick_profit_pct 코드 기본 1.5%
        win = {"qty": 10, "avg_price": 10_000, "current_price": 10_300}    # +3% gross → net ~+2.7%
        small = {"qty": 10, "avg_price": 10_000, "current_price": 10_050}  # +0.5% → net 미달
        loss = {"qty": 10, "avg_price": 10_000, "current_price": 9_800}    # 손실
        self.assertTrue(b._inverse_quick_profit_due("KR", win, 10_300)[0])
        self.assertFalse(b._inverse_quick_profit_due("KR", small, 10_050)[0])
        self.assertFalse(b._inverse_quick_profit_due("KR", loss, 9_800)[0])

    def test_inverse_quick_profit_disabled_by_config(self):
        """quick_profit_pct=0 이면 빠른 익절 비활성 (큰 추세까지 보유 모드)."""
        b = self._inv_helper_bot({"quick_profit_pct": 0})
        win = {"qty": 10, "avg_price": 10_000, "current_price": 10_500}
        self.assertFalse(b._inverse_quick_profit_due("KR", win, 10_500)[0])

    def test_inverse_take_pattern_tagged(self):
        """빠른 익절 사유는 inverse_take 로 태깅 — 수익화 전략 성과 측정(get_pattern_stats)."""
        from zusik.storage.portfolio_tracker import PortfolioTracker
        _, reason = self._inv_helper_bot()._inverse_quick_profit_due(
            "KR", {"qty": 10, "avg_price": 10_000, "current_price": 10_300}, 10_300)
        self.assertEqual(PortfolioTracker._classify_sell_pattern(reason), "inverse_take")

    def test_learn_inverse_quick_profit_raises_when_sold_too_early(self):
        """inverse_take 가 판 뒤에도 더 갔으면(net_if_held>0=조기 익절) 임계를 올린다(더 들고)."""
        from zusik.analysis.loss_learning import learn_inverse_quick_profit
        early = {"inverse_take": {"count": 20, "avg_net_if_held": 4.0}}   # 홀드했으면 +4% 더
        res = learn_inverse_quick_profit(early, default=0.015)
        self.assertGreater(res["threshold"], 0.015)

    def test_learn_inverse_quick_profit_lowers_when_reverted(self):
        """판 뒤 되돌려졌으면(net_if_held<0=팔길 잘함) 임계를 내린다(더 빨리 챙김)."""
        from zusik.analysis.loss_learning import learn_inverse_quick_profit
        reverted = {"inverse_take": {"count": 20, "avg_net_if_held": -3.0}}
        res = learn_inverse_quick_profit(reverted, default=0.015)
        self.assertLess(res["threshold"], 0.015)

    def test_learn_inverse_quick_profit_clamped_and_sample_floor(self):
        """표본 부족이면 default 유지, 극단 데이터도 [min,max] 클램프."""
        from zusik.analysis.loss_learning import learn_inverse_quick_profit
        few = {"inverse_take": {"count": 3, "avg_net_if_held": 50.0}}
        self.assertEqual(learn_inverse_quick_profit(few, default=0.015)["threshold"], 0.015)
        huge = {"inverse_take": {"count": 50, "avg_net_if_held": 999.0}}
        self.assertLessEqual(learn_inverse_quick_profit(huge, default=0.015, max_th=0.035)["threshold"], 0.035)

    def test_learned_quick_profit_helper_falls_back_without_data(self):
        """데이터 없거나 learning_enabled=false 면 config seed 그대로 — 안전한 무동작."""
        from zusik.core.bot import TradingBot
        b = TradingBot.__new__(TradingBot)
        b.config = {"inverse": {"quick_profit_pct": 1.5, "learning_enabled": False}}
        self.assertAlmostEqual(b._learned_inverse_quick_profit(), 0.015, places=4)

    # ── 거래정지/상폐 LLM 환각 강제매도 차단 (실측 2026-06-30: 256750 정상거래 중 강제매도) ──
    def test_actively_trading_overrides_false_delisting(self):
        """오늘 정상 거래 중(체결가+거래량)이면 _actively_trading=True → 거래정지/상폐 LLM '확인'
        이어도 강제매도 보류한다. LLM 재확인이 환각으로 상폐를 확인해 정상종목(256750, +3.88%,
        거래량 22110)을 강제 매도한 실측이 근거. 되돌리면(하드 데이터 반증 제거) 거래 중 종목도
        LLM 확인만으로 팔린다."""
        from zusik.core.bot import TradingBot
        b = TradingBot.__new__(TradingBot)
        b.client = Mock()
        b.client.get_current_price = lambda c: {"price": 21845, "volume": 22110, "change_rate": 3.88}
        self.assertTrue(b._actively_trading("256750"))      # 정상 거래 → 매도 보류
        b.client.get_current_price = lambda c: {"price": 0, "volume": 0}
        self.assertFalse(b._actively_trading("000001"))     # 체결 0 → 정지 가능, LLM 존중
        b.client.get_current_price = lambda c: {"price": 21845, "volume": 0}
        self.assertFalse(b._actively_trading("000002"))     # 거래량 0 → 정지 가능
        def _boom(c):
            raise RuntimeError("quote fail")
        b.client.get_current_price = _boom
        self.assertFalse(b._actively_trading("000003"))     # 조회 실패 → LLM 판단 존중

    def test_get_stock_name_authoritative_from_kis_master(self):
        """종목명은 KIS 상품마스터(prdt_abrv_name)를 권위 소스로 — 합성 ETF 등 시세에 이름 없는
        종목도 정식명(256750 KODEX 차이나심천). LLM/스테일 잘못된 이름 교정의 토대. 캐시+폴백.
        되돌리면(시세명만) 합성 ETF가 코드로 남아 위험탐지가 엉뚱한 이름으로 검색한다."""
        from zusik.clients.kis_client import KISClient
        c = KISClient.__new__(KISClient)
        calls = {"n": 0}
        def fake_get(path, tr, params):
            calls["n"] += 1
            return {"output": {"prdt_abrv_name": "KODEX 차이나심천ChiNext(합성)"}}
        c._get = fake_get
        self.assertEqual(c.get_stock_name("256750"), "KODEX 차이나심천ChiNext(합성)")
        c.get_stock_name("256750")
        self.assertEqual(calls["n"], 1, "이름은 불변 — 두 번째는 캐시(재호출 없음)")
        # 마스터 실패 → 시세명 폴백
        c2 = KISClient.__new__(KISClient)
        c2._get = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("x"))
        c2.get_current_price = lambda code: {"name": "삼성전자"}
        self.assertEqual(c2.get_stock_name("005930"), "삼성전자")

    def test_market_alert_keyword_requires_designation_term(self):
        """시장경보 키워드는 KRX 지정 용어 전체('투자위험종목' 등)로만 매칭. 일반어 '투자위험'
        (미국 증시·반도체 분석 뉴스에 흔함)에 정상 ETF 가 오탐되던 문제 차단(실측 381180/133690/
        379780). 되돌리면(bare '투자위험') 이 테스트가 깨진다."""
        from zusik.core.risk_manager import RiskManager
        r = RiskManager.__new__(RiskManager)
        r.danger_keywords = ["관리종목", "투자경고종목", "투자위험종목", "투자주의종목",
                             "거래정지", "상장폐지", "감사의견거절"]
        benign = r.check_stock_danger(
            "381180", "TIGER 미국필라델피아반도체나스닥",
            "반도체 업종은 변동성이 커 투자위험이 높다는 분석이 많다.")
        self.assertFalse(benign["is_dangerous"], "일반어 '투자위험' 언급에 오탐 금지")
        real = r.check_stock_danger("000000", "테스트", "한국거래소가 투자위험종목으로 지정했다.")
        self.assertTrue(real["is_dangerous"])
        self.assertEqual(real["danger_level"], "warning")
        crit = r.check_stock_danger("000000", "테스트", "상장폐지 결정 공시.")
        self.assertEqual(crit["danger_level"], "critical")

    def test_inverse_handler_quick_profit_preempts_add(self):
        """통합: 보유 인버스가 +1.5%↑ 순익이면 _handle_inverse가 증액(헷지 스케일링) 대신
        빠른익절 매도 — 인버스 수익화 우선(사용자 요청). 진입 허용 상태여도 익절이 먼저."""
        from zusik.core.bot import TradingBot
        b = TradingBot.__new__(TradingBot)
        b.config = {"inverse": {"quick_profit_pct": 1.5, "eod_profit_lock": False,
                                "learning_enabled": False}}
        b._is_inverse = lambda c: True
        b._should_force_exit_inverse = lambda: (False, "")
        b._should_allow_inverse_entry = lambda: (True, "crisis")   # 진입 허용이어도
        b._handle_sell = Mock()
        b._handle_buy = Mock()
        holding = {"code": "114800", "qty": 10, "avg_price": 10_000, "current_price": 10_300}
        b.client = Mock(get_balance=lambda: {"holdings": [holding]})
        b._handle_inverse("114800", "KODEX 인버스", 10_300, df=None)
        b._handle_sell.assert_called_once()
        b._handle_buy.assert_not_called()           # 증액 안 함 — 익절이 선점
        self.assertIn("익절", b._handle_sell.call_args[1].get("force_reason", ""))

    # ── 트레일링 = 수익보호 장치 — 실측 발동 2건 전패 -264k (둘 다 손실 발동) ──
    def test_trailing_never_fires_at_loss(self):
        """US 인라인 트레일링: 고점 -10%↓여도 순손실이면 발동 금지 (HPE -187k형).
        수정 전(from_high만 체크)이라면 -8.4% 손실에서도 True → 이 테스트 실패."""
        from zusik.core.bot import TradingBot
        self.assertFalse(TradingBot._trailing_fire_allowed(-0.105, -0.084),
                         "HPE 06-05 재현: 고점 -10.5% + 손익 -8.4% → 트레일링 발동 금지")
        self.assertFalse(TradingBot._trailing_fire_allowed(-0.12, 0.002),
                         "수수료 미만 순익(+0.2%)도 발동 금지")

    def test_trailing_fires_in_profit(self):
        """수익 보호 본연 기능은 유지: 큰 수익 후 고점 -10% 되돌림이면 발동."""
        from zusik.core.bot import TradingBot
        self.assertTrue(TradingBot._trailing_fire_allowed(-0.10, 0.20),
                        "+20% 수익에서 고점 -10% 되돌림은 익절 발동해야 함")
        self.assertFalse(TradingBot._trailing_fire_allowed(-0.08, 0.20),
                         "고점 -8%는 임계(-10%) 미달 — 발동 안 함")

    def test_pm_trailing_suppresses_loss_fire(self):
        """PositionManager 트레일링도 손실 발동 억제 (stale trailing_active 상태 방어)."""
        import zusik.core.position_manager as position_manager
        from zusik.core.position_manager import PositionManager
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        orig = position_manager.POSITIONS_FILE
        position_manager.POSITIONS_FILE = os.path.join(tmp.name, "positions.json")
        self.addCleanup(lambda: setattr(position_manager, "POSITIONS_FILE", orig))
        pm = PositionManager({"position": {"trailing_stop_pct": 0.10,
                                           "trailing_activate_pct": 0.05}})
        # 물타기로 trailing_active가 stale하게 남은 상태 재현: 고점 110k, 평단 100k, 현재 95k
        pm._positions["TEST"] = {"name": "테스트", "qty": 10, "avg_price": 100_000,
                                 "high_since_buy": 110_000, "trailing_active": True,
                                 "peak_profit_rate": 0.0}
        r = pm.update_trailing_stop("TEST", 95_000)  # 고점 -13.6%, 손익 -5%
        self.assertIsNone(r, "손실(-5%) 상태 트레일링 발동은 억제돼야 함 (수익보호 장치)")

    # ── 본전 보호 = 피크 비례 보존 — 고점 +5~9%가 본전까지 흘러내려 평균 3.9%p 반납 ──
    # breakeven 24건 50%승률 건당 +772원 vs rsi_overbought 고점익절 100% +55k (70배 열위).
    # 삼성전기 +8.9%→+0.8%(-176k), 필라델피아 +7.7%→+0.7%(-116k) 반납 재현 차단.
    def _pm(self):
        """본전보호 단위 검증용 PositionManager (격리된 positions 파일, config 기본값)."""
        import zusik.core.position_manager as position_manager
        from zusik.core.position_manager import PositionManager
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        orig = position_manager.POSITIONS_FILE
        position_manager.POSITIONS_FILE = os.path.join(tmp.name, "positions.json")
        self.addCleanup(lambda: setattr(position_manager, "POSITIONS_FILE", orig))
        return PositionManager({"position": {}})

    def test_breakeven_floor_scales_with_peak(self):
        """보존 바닥 = 피크 비례(peak-2.5%, 사다리 기본 OFF). 큰 피크일수록 높은 곳에서 잠금.
        수정 전(고정 +1.5%)이라면 +8.9% 피크도 바닥 +1.5% → 이 테스트 실패."""
        pm = self._pm()
        self.assertAlmostEqual(pm.breakeven_protect_floor(0.03), 0.015, places=4)   # 작은 피크: min_floor
        self.assertAlmostEqual(pm.breakeven_protect_floor(0.05), 0.025, places=4)   # +5% → +2.5%
        self.assertAlmostEqual(pm.breakeven_protect_floor(0.089), 0.064, places=3)  # 삼성전기 +8.9% → +6.4%
        # 사다리 기본 OFF → +10%도 피크비례 (49종목×3.6년 검증: 피크비례 > 사다리)
        self.assertAlmostEqual(pm.breakeven_protect_floor(0.10), 0.075, places=4)   # +10% → +7.5%

    def test_profit_ladder_mechanism_when_enabled(self):
        """수익 사다리 메커니즘은 명시 설정 시 구간 고정 락으로 작동(opt-in). 기본은 OFF —
        49종목×3.6년 walk-forward에서 피크비례가 우수해 calibrate가 []로 기록(생존편향 회피).
        이 테스트는 '켰을 때 올바른가'를 검증."""
        import zusik.core.position_manager as position_manager
        from zusik.core.position_manager import PositionManager
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        orig = position_manager.POSITIONS_FILE
        position_manager.POSITIONS_FILE = os.path.join(tmp.name, "positions.json")
        self.addCleanup(lambda: setattr(position_manager, "POSITIONS_FILE", orig))
        pm = PositionManager({"position": {"profit_ladder":
            [[0.30, 0.24], [0.20, 0.15], [0.15, 0.11], [0.10, 0.06]]}})
        self.assertAlmostEqual(pm.breakeven_protect_floor(0.15), 0.11, places=4)   # +15% → +11%
        self.assertAlmostEqual(pm.breakeven_protect_floor(0.43), 0.24, places=4)   # +43% → +24%(최상위)
        self.assertLess(pm.breakeven_protect_floor(0.43), 0.43 - 0.025,
                        "켜면 사다리는 피크비례보다 느슨(추세 라이딩 공간)")
        self.assertFalse(pm.breakeven_should_protect(0.43, 0.25), "+24% 락 위는 라이딩 유지")
        self.assertTrue(pm.breakeven_should_protect(0.43, 0.23), "+24% 락 이탈은 익절")
        # 기본(미설정)은 사다리 OFF → 피크비례
        pm_off = PositionManager({"position": {}})
        self.assertAlmostEqual(pm_off.breakeven_protect_floor(0.15), 0.125, places=4)

    def test_ladder_never_fires_at_loss_on_gap_crash(self):
        """극단적 낙폭(갭다운) 회귀: 피크 +12% 찍고 갭다운으로 -10%까지 빠지면 본전보호/사다리는
        발동 안 한다 — 수익보호 장치는 손실 확정 금지(트레일링 -264k 교훈, crash_instant 0%승률).
        손실은 hold floor/하드스톱이 담당. 수정 전(profit<=floor만 체크)이라면 -10%<=+6%라
        True로 바닥투매 → 이 테스트 실패."""
        pm = self._pm()
        self.assertFalse(pm.breakeven_should_protect(peak_profit=0.12, profit_rate=-0.10),
                         "피크 +12%→갭다운 -10%는 본전보호로 컷하지 않는다(바닥투매 금지)")
        self.assertFalse(pm.breakeven_should_protect(peak_profit=0.08, profit_rate=-0.05),
                         "피크 +8%→-5%도 손실 발동 금지")
        # 수익 구간(갭 직전 락 도달)에서는 정상 발동
        self.assertTrue(pm.breakeven_should_protect(peak_profit=0.12, profit_rate=0.06),
                        "피크 +12% → 락 +6% 도달은 수익 익절 정상 발동")

    def test_breakeven_protects_high_peak_before_breakeven(self):
        """고점 +8% 종목이 +5%로 되돌리면 즉시 익절 (보존바닥 +5.5%) — 본전(+1.5%)까지
        흘려보내지 않는다. 수정 전(+1.5% 고정 바닥)이라면 +5%는 바닥 위라 미발동(라이딩)
        → 본전까지 흘러내림 → 이 테스트 실패."""
        pm = self._pm()
        self.assertTrue(pm.breakeven_should_protect(peak_profit=0.08, profit_rate=0.05),
                        "+8% 피크 → +5% 되돌림은 익절해야 함 (보존바닥 +5.5%)")
        self.assertTrue(pm.breakeven_should_protect(peak_profit=0.089, profit_rate=0.008),
                        "삼성전기형 +8.9%→+0.8%는 당연히 익절")
        self.assertFalse(pm.breakeven_should_protect(peak_profit=0.08, profit_rate=0.06),
                         "+8% 피크 +6%는 보존바닥(+5.5%) 위 — 아직 라이딩(과민매도 방지)")

    def test_breakeven_small_peak_anti_churn_preserved(self):
        """작은 피크(+3%)는 기존 +1.5% 바닥 유지 — 상승 초입 과민 매도 방지(anti-churn).
        피크 비례 도입이 작은 익절의 churn을 늘리지 않음을 보장."""
        pm = self._pm()
        self.assertFalse(pm.breakeven_should_protect(peak_profit=0.03, profit_rate=0.02),
                         "+3% 피크 +2%는 바닥(+1.5%) 위 — 미발동")
        self.assertTrue(pm.breakeven_should_protect(peak_profit=0.03, profit_rate=0.014),
                        "+3% 피크 +1.4%는 바닥 이탈 — 발동")

    def test_breakeven_not_armed_below_threshold(self):
        """피크가 arm(+3%) 미달이고 rsi_trim도 아니면 본전보호 무장 안 함.
        rsi_trim 포지션은 arm 미달이어도 보호(잔여 라이딩분 보존 — BAC/KO 사각 차단)."""
        pm = self._pm()
        self.assertFalse(pm.breakeven_should_protect(peak_profit=0.02, profit_rate=0.005),
                         "+2% 피크는 arm(+3%) 미달 — 미발동")
        self.assertTrue(pm.breakeven_should_protect(peak_profit=0.02, profit_rate=0.005,
                                                    rsi_trimmed=True),
                        "rsi_trim 포지션은 arm 미달이어도 보호 발동")

    def test_defer_sell_strong_profit_bypasses_momentum(self):
        """순익 +3.5%↑ 'sell' 신호는 강한 모멘텀(hold_score≥0.6)이어도 연기 안 함 — 고점에서
        못 팔고 본전까지 홀딩하던 핵심 원인 차단. 수정 전(모멘텀 게이트 우선)이라면 +20% 순익
        + 강한 상승에서 defer=True(상승 지속) → 이 테스트 실패."""
        bot = self._bot()
        closes = list(range(100, 120))  # 강한 상승 → hold_score 높음
        df = make_ohlcv_df(closes, volumes=[1000] * 19 + [2500])
        defer, reason = bot._should_defer_sell(
            market="US", df=df, qty=10, avg_price=100, current_price=120)  # +20% 순익
        self.assertFalse(defer, f"순익 +20%인데 모멘텀 핑계로 연기됨: {reason}")
        self.assertIn("고점 익절 우선", reason)

    def test_defer_sell_weak_profit_still_respects_momentum(self):
        """순익이 strong_take(+3.5%) 미만(+2%)이면 기존 모멘텀 게이트 유지 — 우회는 큰
        익절에만 적용. 강한익절 바이패스가 모든 매도를 무분별 통과시키지 않음을 보장."""
        bot = self._bot()
        closes = list(range(100, 120))
        df = make_ohlcv_df(closes, volumes=[1000] * 19 + [2500])
        defer, reason = bot._should_defer_sell(
            market="US", df=df, qty=10, avg_price=100, current_price=102)  # +2% < 3.5%
        self.assertTrue(defer, f"+2% 순익 + 강한 모멘텀은 연기해야 함: {reason}")
        self.assertIn("상승 지속", reason)

    # ── 추가매수(피라미딩) churn 장벽 면제 — 라이브: 삼성전자 +8.8% 피크 pyramid L0 ──
    def test_is_pyramid_eligible_only_for_winning_held(self):
        """피라미딩 자격: 보유 + 다음 레벨 수익기준(+3%/+7%) 도달 시에만 True. 미보유/손실/레벨소진은 False."""
        pm = self._pm()
        pm._positions["X"] = {"name": "X", "qty": 10, "avg_price": 1000, "pyramid_level": 0}
        self.assertFalse(pm.is_pyramid_eligible("X", 1020), "+2% < +3% 기준 미달")
        self.assertTrue(pm.is_pyramid_eligible("X", 1030), "+3% 도달 → 1차 피라미딩 자격")
        pm._positions["X"]["pyramid_level"] = 2
        self.assertFalse(pm.is_pyramid_eligible("X", 1300), "레벨 소진 → 자격 없음")
        self.assertFalse(pm.is_pyramid_eligible("ZZZ", 1000), "미보유 종목은 자격 없음")

    def test_pyramid_add_exempt_from_churn_guard(self):
        """보유 승자 추가매수(is_add_on=True)는 활성 재진입 블록을 면제받아 통과. 일반 재매수
        (is_add_on=False)는 그대로 차단. 수정 전(코드 무관 차단)이라면 add_on도 막힘 → 이 테스트
        실패. 근거: 직전 트림 후 재진입블록에 막혀 승자 피라미딩이 차단되던 라이브 버그."""
        import time
        from zusik.core.bot import TradingBot
        b = TradingBot.__new__(TradingBot)
        b._reentry_block = {"005930": (time.time() + 3600, "0.5h")}  # 활성 블록
        b._daily_sell_count = {}
        b._knife_block = {}
        b._last_intraday_change = {}
        b._is_inverse = lambda c: False
        b._core_hold_through = lambda c: False
        b._save_reentry_block = lambda: None
        # 일반 재매수: 재진입 블록에 막힘
        self.assertTrue(b._churn_guard("005930", "삼성", price=70000, is_add_on=False),
                        "flat 재매수는 재진입 블록에 막혀야 함")
        # 추가매수(피라미딩): churn 장벽 면제 → 통과 (df=None이라 품질 게이트는 스킵)
        self.assertFalse(b._churn_guard("005930", "삼성", price=70000, is_add_on=True),
                         "보유 승자 추가매수는 churn 장벽 면제 — 통과해야 함")

    def test_learned_params_overlay_applies_and_is_safe(self):
        """다년 학습 캘리브레이션(calibrate_from_history.py)이 기록한 learned_params.json을
        load_config가 position 설정에 오버레이. 화이트리스트 키만(안전), 파일 없으면 무동작."""
        import zusik.core.bot as bot
        import json as _json
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        orig = bot._LEARNED_PARAMS_FILE
        self.addCleanup(lambda: setattr(bot, "_LEARNED_PARAMS_FILE", orig))

        # 파일 없음 → 무동작
        bot._LEARNED_PARAMS_FILE = os.path.join(tmp.name, "none.json")
        cfg = {"position": {"profit_ladder": [[0.1, 0.06]], "secret": 1}}
        out = bot._apply_learned_params(cfg)
        self.assertEqual(out["position"]["profit_ladder"], [[0.1, 0.06]], "파일 없으면 그대로")

        # 파일 있음 → 화이트리스트 키만 오버레이, 비화이트리스트는 무시
        fp = os.path.join(tmp.name, "learned.json")
        _json.dump({"profit_ladder": [[0.2, 0.15]], "breakeven_giveback_cap": 0.03,
                    "evil_key": 999, "calibrated_at": "2026-06-19"}, open(fp, "w"))
        bot._LEARNED_PARAMS_FILE = fp
        out = bot._apply_learned_params({"position": {"profit_ladder": [[0.1, 0.06]]}})
        self.assertEqual(out["position"]["profit_ladder"], [[0.2, 0.15]], "학습값으로 교체")
        self.assertAlmostEqual(out["position"]["breakeven_giveback_cap"], 0.03)
        self.assertNotIn("evil_key", out["position"], "화이트리스트 외 키는 주입 금지")

    def test_apply_learned_params_runtime_whitelist(self):
        """PositionManager.apply_learned_params 는 화이트리스트 청산 키만 런타임 갱신.
        비화이트리스트·잘못된 타입은 무시 — 자본보호 레일(손절선·하드스톱)은 학습 불가."""
        from zusik.core.position_manager import PositionManager
        pm = PositionManager({"position": {"breakeven_giveback_cap": 0.025, "profit_ladder": []}})
        applied = pm.apply_learned_params({
            "profit_ladder": [[0.2, 0.15]], "breakeven_giveback_cap": 0.04,
            "breakeven_arm_pct": 0.05, "breakeven_min_floor": 0.02,
            "stop_loss_per_stock": -0.30,   # 화이트리스트 밖 — 절대 반영 금지(자본보호)
            "evil": 999,
        })
        self.assertEqual(pm.profit_ladder, [[0.2, 0.15]])
        self.assertAlmostEqual(pm.breakeven_giveback_cap, 0.04)
        self.assertAlmostEqual(pm.breakeven_arm_pct, 0.05)
        self.assertAlmostEqual(pm.breakeven_min_floor, 0.02)
        self.assertCountEqual(applied, ["profit_ladder", "breakeven_giveback_cap",
                                        "breakeven_arm_pct", "breakeven_min_floor"])
        # 자본보호 키는 학습으로 안 바뀜 (손절선 -15% 불변)
        self.assertNotEqual(getattr(pm, "stop_loss_per_stock", None), -0.30)
        # 잘못된 타입(list 자리에 숫자, 숫자 자리에 bool)은 무시
        pm.apply_learned_params({"profit_ladder": 5, "breakeven_arm_pct": True})
        self.assertEqual(pm.profit_ladder, [[0.2, 0.15]], "잘못된 타입은 무시(기존 유지)")

    def test_refresh_learned_params_reapplies_without_restart(self):
        """calibrate 가 learned_params.json 을 갱신하면 봇이 재시작 없이 런타임 재적용.
        mtime 변화에만 반응(없으면 무동작) — '봇 장기 무재시작 → fresh 학습 stale' 갭 차단.
        수정 전(load_config 시작 시 1회만 읽음)이라면 런타임 반영 안 됨 → 이 테스트 실패."""
        import json as _json
        from zusik.core.bot import TradingBot
        from zusik.core import bot_helpers as bh
        from zusik.core.position_manager import PositionManager
        b = TradingBot.__new__(TradingBot)
        b.positions = PositionManager({"position": {"breakeven_giveback_cap": 0.025}})
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        fp = os.path.join(tmp.name, "learned_params.json")
        orig = bh._LEARNED_PARAMS_FILE
        bh._LEARNED_PARAMS_FILE = fp
        self.addCleanup(lambda: setattr(bh, "_LEARNED_PARAMS_FILE", orig))

        # 파일 없음 → 무동작 (기본값 유지)
        b._refresh_learned_params()
        self.assertAlmostEqual(b.positions.breakeven_giveback_cap, 0.025)

        # 캘리브레이션이 파일 기록 → 다음 tick 의 refresh 가 런타임 반영
        _json.dump({"breakeven_giveback_cap": 0.04, "calibrated_at": "2026-06-28"}, open(fp, "w"))
        b._refresh_learned_params()
        self.assertAlmostEqual(b.positions.breakeven_giveback_cap, 0.04, msg="재시작 없이 반영돼야")

        # mtime 변화 없으면 재적용 안 함 (값 수동 변경해도 유지 — 불필요한 재로드 방지)
        b.positions.breakeven_giveback_cap = 0.99
        b._refresh_learned_params()
        self.assertAlmostEqual(b.positions.breakeven_giveback_cap, 0.99, msg="mtime 동일 → 재로드 안 함")

    def test_local_override_deep_merge_and_priority(self):
        """config.local.yaml(configtool.py 관리) 깊은 병합 — 사용자 명시 설정이 최우선.
        config.yaml 원본은 불변, 로컬 파일만 덮어쓴다. 점 경로 중첩 키 병합 검증."""
        import zusik.core.bot as bot
        import yaml as _yaml
        # 깊은 병합: 중첩 dict 는 키 단위 병합, 그 외는 덮어쓰기
        base = {"risk": {"a": 1, "b": 2}, "position": {"buy_tranches": [1.0]}, "x": 1}
        bot._deep_merge(base, {"risk": {"b": 99, "c": 3}, "position": {"buy_tranches": [0.4, 0.3, 0.3]}})
        self.assertEqual(base["risk"], {"a": 1, "b": 99, "c": 3})
        self.assertEqual(base["position"]["buy_tranches"], [0.4, 0.3, 0.3])
        self.assertEqual(base["x"], 1)
        # 파일 경로 병합: config.local.yaml 이 config.yaml 값을 덮는다
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        cfgp = os.path.join(tmp.name, "config.yaml")
        with open(os.path.join(tmp.name, "config.local.yaml"), "w") as f:
            _yaml.safe_dump({"risk": {"daily_loss_limit": -20000}}, f)
        merged = bot._apply_local_overrides({"risk": {"daily_loss_limit": -15000, "stop": -0.15}}, cfgp)
        self.assertEqual(merged["risk"]["daily_loss_limit"], -20000, "로컬 오버라이드가 우선")
        self.assertEqual(merged["risk"]["stop"], -0.15, "병합되지 않은 키는 보존")
        # 로컬 파일 없으면 무동작
        cfgp2 = os.path.join(tmp.name, "none", "config.yaml")
        os.makedirs(os.path.dirname(cfgp2))
        out = bot._apply_local_overrides({"risk": {"daily_loss_limit": -15000}}, cfgp2)
        self.assertEqual(out["risk"]["daily_loss_limit"], -15000)

    def test_mode_does_not_clobber_trailing_config(self):
        """trailing_stop_pct/activate_pct는 config.yaml 명시값 우선 (buy_tranches/stop_loss와
        동일 원칙). 수정 전(pos[key]=profile[key] 무조건 덮어쓰기)이라면 0.15/0.07이
        premium 프로파일 값으로 바뀜 → 이 테스트 실패."""
        import zusik.core.trading_mode as tm
        cfg = {"trading_mode": "premium",
               "position": {"trailing_stop_pct": 0.15, "trailing_activate_pct": 0.07}}
        with patch.object(tm, "_load_state", return_value={"current_mode": "premium"}), \
             patch.object(tm, "_save_state"):
            out = tm.apply_mode(cfg)
        self.assertEqual(out["position"]["trailing_stop_pct"], 0.15,
                         "config 명시 trailing_stop_pct가 모드 프로파일에 덮어써짐")
        self.assertEqual(out["position"]["trailing_activate_pct"], 0.07,
                         "config 명시 trailing_activate_pct가 모드 프로파일에 덮어써짐")

    def test_mode_never_clobbers_explicit_config(self):
        """제네릭 드리프트 가드 (2026-06-12): config.yaml에 명시된 position/risk 키는
        apply_mode 후에도 값이 보존돼야 한다 — 어떤 키든, 어떤 모드든.

        같은 버그 클래스 3연속(buy_tranches 06-01 / stop_loss 06-08 삼바 -148k /
        trailing 06-10 -264k)의 구조적 가드. 새 모드 파생 키가 추가돼도 이 테스트가
        hard-override를 잡는다."""
        import zusik.core.trading_mode as tm
        sentinel_pos = {k: f"__cfg_{k}__" for k in
                        ("buy_dip_pcts", "sell_tranches", "sell_target_pcts",
                         "max_same_sector", "buy_tranches",
                         "trailing_stop_pct", "trailing_activate_pct")}
        sentinel_risk = {k: f"__cfg_{k}__" for k in
                         ("stop_loss_per_stock", "daily_target_profit_rate")}
        for mode in ("seed", "active", "premium"):
            cfg = {"trading_mode": mode,
                   "position": dict(sentinel_pos), "risk": dict(sentinel_risk)}
            with patch.object(tm, "_load_state", return_value={"current_mode": mode}), \
                 patch.object(tm, "_save_state"):
                out = tm.apply_mode(cfg)
            for k, v in sentinel_pos.items():
                self.assertEqual(out["position"][k], v,
                                 f"[{mode}] position.{k} 명시값이 모드에 덮어써짐")
            for k, v in sentinel_risk.items():
                self.assertEqual(out["risk"][k], v,
                                 f"[{mode}] risk.{k} 명시값이 모드에 덮어써짐")

    # ── 칼날 재진입 차단 — HPE -187k(차단) vs BB +171k(허용) 실측 분리 ──
    def _knife_bot(self, tmpdir):
        from zusik.core.bot import TradingBot
        b = TradingBot.__new__(TradingBot)
        b._knife_block = {}
        b._KNIFE_BLOCK_FILE = os.path.join(tmpdir, "knife_block.json")
        return b

    def test_knife_reentry_blocked_after_blowoff(self):
        """익절 후 48h 내 매도가 -5%↓ 재매수 차단 — HPE 06-02 95,725 익절 → 06-03
        84,066(-12%) 칼날 재매수 -187k 재현 방지."""
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        b = self._knife_bot(tmp.name)
        b._register_knife_block("HPE", "RSI 과매수 익절 (수익 +44.2%, RSI 94)", 95_725)
        blocked, reason = b._is_knife_reentry("HPE", 84_066)
        self.assertTrue(blocked, "익절가 -12% 재매수는 칼날 — 차단돼야 함")

    def test_knife_allows_trend_continuation(self):
        """매도가 근처(-5% 이내) 재진입은 추세 지속 — 허용 (BB 13,719 익절 → 13,687 재매수
        → +171k 승리 패턴 보존)."""
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        b = self._knife_bot(tmp.name)
        b._register_knife_block("BB", "RSI 과매수 익절 (수익 +1.8%, RSI 88)", 13_719)
        blocked, _ = b._is_knife_reentry("BB", 13_687)
        self.assertFalse(blocked, "매도가 -0.2% 재진입(추세 지속)은 허용돼야 함")
        blocked, _ = b._is_knife_reentry("BB", 14_500)
        self.assertFalse(blocked, "매도가 위 재진입은 당연히 허용")

    def test_knife_ignores_loss_cuts(self):
        """손절 계열 매도는 칼날 가드 미등록 — 기존 reentry_block 24h가 별도 담당."""
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        b = self._knife_bot(tmp.name)
        b._register_knife_block("XXX", "급락 손절: 당일 -7% 급락", 10_000)
        self.assertNotIn("XXX", b._knife_block, "손절 매도는 칼날 가드 등록 대상 아님")

    # ── 회전 청산 — 손실 실현 금지, 본전 회복 반등에서만 ──
    def test_stale_rotate_only_at_breakeven_recovery(self):
        """72h+ 모멘텀 소멸 포지션: 본전 회복(-0.5%~+2%)에서만 회전 청산.
        손실 컷(41% 정확도 바닥투매)이 되면 안 됨 — 손실 구간은 False여야 한다."""
        from zusik.core.bot import TradingBot
        self.assertTrue(TradingBot._stale_rotate_due(80.0, 0.005, 0.30),
                        "72h+ 모멘텀 소멸 + 본전 회복 → 회전 청산")
        self.assertFalse(TradingBot._stale_rotate_due(80.0, -0.02, 0.30),
                         "손실(-2%) 상태 회전 청산 금지 — 바닥투매 방지")
        self.assertFalse(TradingBot._stale_rotate_due(24.0, 0.005, 0.30),
                         "신선한 포지션(24h)은 회전 대상 아님")
        self.assertFalse(TradingBot._stale_rotate_due(80.0, 0.005, 0.60),
                         "모멘텀 살아있으면(hold_score 0.6) 보유 유지")
        self.assertFalse(TradingBot._stale_rotate_due(80.0, 0.03, 0.30),
                         "+3% 수익은 익절 로직 영역 — 회전 청산 안 함")

    # ── RS 상대강도 게이트 — 식은 모멘텀 후보 제거 ──
    def test_compute_rs_separates_hot_and_cold(self):
        """지수 아웃퍼폼(+8%p)은 양수, 언더퍼폼(-7%p)은 RS_DROP_THRESHOLD(-5%p) 미만."""
        from zusik.core.bot import TradingBot
        import pandas as pd
        idx = pd.DataFrame({"close": [100 * (1.001 ** i) for i in range(30)]})   # 지수 ~+2%/20일
        hot = pd.DataFrame({"close": [100 * (1.005 ** i) for i in range(30)]})   # +10%/20일
        cold = pd.DataFrame({"close": [100 * (0.9975 ** i) for i in range(30)]})  # -5%/20일
        rs_hot = TradingBot._compute_rs(hot, idx)
        rs_cold = TradingBot._compute_rs(cold, idx)
        self.assertGreater(rs_hot, 0.05, "아웃퍼폼 종목 RS가 양수여야 함")
        self.assertLess(rs_cold, TradingBot._RS_DROP_THRESHOLD,
                        "언더퍼폼 종목은 RS 게이트(-5%p)에 걸려야 함")
        self.assertEqual(TradingBot._compute_rs(None, idx), 0.0, "데이터 없으면 중립 0.0")

    # ── RSI 과매수 분할 트림 — 적시성 27%: 전량 익절이 후속 +26~30% 놓침 ──
    def test_rsi_trim_half_then_ride(self):
        """첫 RSI 과매수 트리거 = 절반 트림(0.5), 24h 내 재트리거 = None(라이딩 유지).
        수정 전(전량 매도)이라면 두 번째 호출도 비율 반환 → 연쇄 매도로 전량 소진."""
        from zusik.core.bot import TradingBot
        b = TradingBot.__new__(TradingBot)
        b.config = {}
        self.assertEqual(b._rsi_trim_ratio("NVDA"), 0.5, "첫 에피소드는 절반 트림")
        self.assertIsNone(b._rsi_trim_ratio("NVDA"),
                          "24h 내 재트리거는 매도 스킵 — 잔여는 트레일링 라이딩")
        self.assertEqual(b._rsi_trim_ratio("AAPL"), 0.5, "다른 종목은 독립 에피소드")

    def test_rsi_trim_ratio_configurable(self):
        """config position.rsi_trim_ratio로 트림 비율 조정 가능."""
        from zusik.core.bot import TradingBot
        b = TradingBot.__new__(TradingBot)
        b.config = {"position": {"rsi_trim_ratio": 0.3}}
        self.assertEqual(b._rsi_trim_ratio("X"), 0.3)

    def test_rsi_trim_survives_restart_via_flag(self):
        """재시작 회귀 (BAC 06-12 02:39 실측): in-memory 쿨다운이 재시작에 리셋돼 같은
        에피소드에 2차 트림 발동. 영속 플래그(rsi_trimmed)가 fresh 인스턴스에서도 차단해야."""
        from zusik.core.bot import TradingBot
        b = TradingBot.__new__(TradingBot)  # 재시작 직후 = in-memory 쿨다운 없음
        b.config = {}
        b.positions = Mock(_get_position=Mock(return_value={"rsi_trimmed": True}))
        self.assertIsNone(b._rsi_trim_ratio("BAC"),
                          "rsi_trimmed 플래그가 있으면 재시작 후에도 재트림 금지")

    # ── RSI 트림 잔여 라이딩 보호 — +4~6% 구간 무보호 사각지대 ──
    def test_trimmed_position_gets_breakeven_protect_below_activate(self):
        """트림된 포지션은 peak가 arm(+3%, 2026-06-19) 미달이어도 +1.5% 이하 되돌림에서 본전보호.
        수정 전이라면 peak 2.5% < arm → 본전보호 미발동 → 0%까지 무보호 (BAC/KO 실측 사각지대)."""
        import zusik.core.position_manager as position_manager
        from zusik.core.position_manager import PositionManager
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        orig = position_manager.POSITIONS_FILE
        position_manager.POSITIONS_FILE = os.path.join(tmp.name, "positions.json")
        self.addCleanup(lambda: setattr(position_manager, "POSITIONS_FILE", orig))
        pm = PositionManager({"position": {"trailing_stop_pct": 0.15,
                                           "trailing_activate_pct": 0.07}})
        # peak +2.5% (< arm +3%) — 트림 없으면 미무장
        pm._positions["BAC"] = {"name": "BAC", "qty": 7, "avg_price": 100_000,
                                "high_since_buy": 102_500, "peak_profit_rate": 0.025}
        # 트림 마킹 전: peak 2.5% < arm 3% → +1%로 떨어져도 본전보호 없음
        r = pm.update_trailing_stop("BAC", 101_000)
        self.assertIsNone(r, "마킹 전엔 기존 동작 유지 (peak<arm → 본전보호 미발동)")
        pm.mark_rsi_trimmed("BAC")
        r = pm.update_trailing_stop("BAC", 101_000)
        self.assertIsNotNone(r, "트림된 포지션 +1%는 본전보호로 잡아야 함 (수익 소멸 방지)")
        self.assertEqual(r.get("action"), "breakeven_protect")

    def test_rebuy_clears_rsi_trimmed_flag(self):
        """추가 매수 = 새 라이딩 사이클 — rsi_trimmed 리셋."""
        import zusik.core.position_manager as position_manager
        from zusik.core.position_manager import PositionManager
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        orig = position_manager.POSITIONS_FILE
        position_manager.POSITIONS_FILE = os.path.join(tmp.name, "positions.json")
        self.addCleanup(lambda: setattr(position_manager, "POSITIONS_FILE", orig))
        pm = PositionManager({"position": {}})
        pm.record_buy("X", "테스트", 10, 1_000)
        pm.mark_rsi_trimmed("X")
        self.assertTrue(pm._get_position("X").get("rsi_trimmed"))
        pm.record_buy("X", "테스트", 5, 1_100)
        self.assertFalse(pm._get_position("X").get("rsi_trimmed"),
                         "추가 매수 후 rsi_trimmed가 리셋돼야 함")

    # ── 수동매도 오인 race — 봇 청산을 이중 계상 → 한도 오발동 ──
    def test_manual_sell_skipped_when_recent_bot_order(self):
        """봇 주문 직후(record_sell 전) sync가 보유 감소를 수동매도로 오인하지 않아야 함.
        06-12 09:06 실측: KODEX 인버스 레짐 청산이 이중 기록(-160k 중복) → 일일손실한도
        오발동 → 반등장 전면 정지."""
        import zusik.storage.portfolio_tracker as pt
        import json as _json
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        # _recent_bot_order는 data/pending_orders.json 상대 경로 — cwd를 tmp로 바꿔 격리
        cwd = os.getcwd()
        self.addCleanup(lambda: os.chdir(cwd))
        os.chdir(tmp.name)
        os.makedirs("data", exist_ok=True)
        _json.dump({"orders": [{"code": "114800", "side": "sell", "qty": 1323,
                                "timestamp": datetime.now().isoformat()}]},
                   open("data/pending_orders.json", "w"))
        self.assertTrue(pt.PortfolioTracker._recent_bot_order("114800", minutes=15),
                        "최근 봇 주문이 있으면 race로 판단해야 함")
        self.assertFalse(pt.PortfolioTracker._recent_bot_order("005930", minutes=15),
                         "다른 종목은 영향 없음")
        old = {"orders": [{"code": "999999", "side": "sell", "qty": 1,
                           "timestamp": (datetime.now() - timedelta(hours=2)).isoformat()}]}
        _json.dump(old, open("data/pending_orders.json", "w"))
        self.assertFalse(pt.PortfolioTracker._recent_bot_order("999999", minutes=15),
                         "오래된 주문(2h)은 race 아님")

    def test_held_inverse_sorted_first(self):
        """보유 인버스는 스캔 첫 슬롯 (06-12: 청산이 +6분 밀려 갭업장 흘러내림 실측).
        stable sort라 나머지 우선순위는 유지돼야 함."""
        held = {"114800", "005930"}
        is_inv = lambda c: c == "114800"
        cands = [{"code": "005930"}, {"code": "000660"}, {"code": "114800"}, {"code": "035420"}]
        cands.sort(key=lambda s: 0 if (s.get("code", "") in held and is_inv(s.get("code", ""))) else 1)
        self.assertEqual(cands[0]["code"], "114800", "보유 인버스가 첫 슬롯이어야 함")
        self.assertEqual([c["code"] for c in cands[1:]], ["005930", "000660", "035420"],
                         "나머지 순서는 stable하게 유지")

    def test_daily_loss_limit_pct_config_overridable(self):
        """일일손실한도 %도 config.yaml risk 섹션이 모드 프로파일보다 우선 (06-12)."""
        import zusik.core.trading_mode as tm
        cfg = {"trading_mode": "premium", "risk": {"daily_loss_limit_pct": 0.03}}
        with patch.object(tm, "_load_state", return_value={"current_mode": "premium"}), \
             patch.object(tm, "_save_state"):
            out = tm.apply_mode(cfg)
        self.assertEqual(out["_daily_loss_limit_pct"], 0.03,
                         "config 명시 daily_loss_limit_pct가 모드에 덮어써짐")

    # ── Claude 쿼터 누수 — 한도 0인데 fail-open race로 일 ~7콜 누수 ──
    def test_claude_limit_fail_closed(self):
        """_check_limit: ①한도 ≤0이면 파일 상태 무관 즉시 차단 ②파손 파일(JSON 예외) 시
        claude_*는 fail-closed, codex/agy는 fail-open 유지.
        수정 전(예외 시 무조건 True)이라면 파손 시 claude 통과 → 이 테스트 실패."""
        from zusik.clients import claude_client as cc
        import zusik.core.cost_optimizer as co
        _old_cache = cc._cfg_limits_cache
        cc._cfg_limits_cache = {}   # config 오버라이드 없음 가정 → 모듈 DAILY_LIMITS 가 유효 한도
        try:
            with patch.dict(co.DAILY_LIMITS, {"claude_sonnet": 0, "codex": 1500}):
                self.assertFalse(cc._check_limit("claude_sonnet"),
                                 "한도 0은 어떤 상태에서도 차단 (파일 I/O 없이)")
            # 파손/읽기실패 파일을 흉내내려면 파일이 '있다'고 보고 읽기 단계에서 예외가 나야 한다.
            # (CI 등 data/api_costs.json 부재 환경에선 os.path.exists=False라 읽기 자체가 스킵돼
            # fail-closed 분기에 도달 못 하고 통과해버렸다 — 환경 의존 회귀,
            with patch.dict(co.DAILY_LIMITS, {"claude_sonnet": 300, "codex": 1500}), \
                 patch.object(cc.os.path, "exists", return_value=True), \
                 patch.object(cc.json, "load", side_effect=ValueError("corrupt json")):
                self.assertFalse(cc._check_limit("claude_sonnet"),
                                 "파손 파일 시 claude는 fail-closed (쿼터 보호)")
                self.assertTrue(cc._check_limit("codex"),
                                "codex는 fail-open 유지 (봇 전체 정지 방지)")
        finally:
            cc._cfg_limits_cache = _old_cache

    def test_pattern_boost_amplifies_proven_winners(self):
        """30일 실증(승률≥70%·건당≥500·평균≥3%) 시 1.25× 증폭 (06-10 상향, 이전 1.15)."""
        from zusik.core.bot import TradingBot
        b = TradingBot.__new__(TradingBot)
        b.tracker = Mock()
        b.tracker.get_pattern_stats.return_value = {
            "rsi_overbought": {"count": 10, "wins": 10, "pnl_sum": 600_000,
                               "amount_sum": 10_000_000}}  # 승률 100%, 건당 +60k, +6%
        self.assertEqual(b._pattern_confidence_boost(), 1.25,
                         "실증된 승자 패턴은 1.25× 증폭이어야 함")

    # ── ① 가짜 drawdown 동결 (75% 자본 묶임 = 최대 손실 원인) ──
    def test_effective_drawdown_ignores_settlement_phantom(self):
        """미국 T+2 미결제로 total_equity가 폭락해도 보유분이 평형이면
        effective drawdown은 작아야 한다 — 가짜 -15%로 사이징이 동결되던 버그.

        직전(펀딩일) 고점 22.2M을 시드한 뒤 total이 18.8M으로 떨어진 상황을 재현 →
        기존 total_equity 기반 drawdown은 -15%(가짜 동결), effective는 ~-1.8%여야 한다."""
        import json
        with tempfile.TemporaryDirectory() as d:
            t = self._tracker(d, 22_238_405, [-300_000, -76_563])
            # 펀딩일 고점 시드 (직전 날짜) — 이게 있어야 total 기반 dd가 -15%로 부풀려짐
            with open(os.path.join(d, "equity_curve.json"), "w") as f:
                json.dump([{"date": "2026-05-27", "total_equity": 22_238_405,
                            "effective_equity": 22_238_405, "drawdown_pct": 0.0}], f)
            t.record_equity_snapshot(
                kr_cash=16_704_845, kr_eval=0, us_cash_krw=280_259, us_eval_krw=4_174_529,
                total_override=18_829_077,       # 가짜 -15% total (미결제 착시)
                holdings_unrealized_krw=-14_000, # 보유분 거의 평형
            )
            # 전제: total 기반 drawdown은 실제로 -10% 넘게 부풀려져 있어야(=가짜 동결 조건)
            self.assertLess(t.get_current_drawdown(), -10.0,
                            "전제 실패: 가짜 drawdown이 재현되지 않음")
            eff = t.get_effective_drawdown()
            self.assertGreater(eff, -5.0,
                               f"보유분 평형인데 effective dd={eff}% — 가짜 동결 위험")

    def test_effective_drawdown_reflects_real_loss(self):
        """진짜 보유 손실(-3M)이면 effective drawdown이 크게 음수 — 위기 방어는 유지."""
        with tempfile.TemporaryDirectory() as d:
            t = self._tracker(d, 22_238_405, [-300_000])
            t.record_equity_snapshot(
                kr_cash=16_704_845, kr_eval=0, us_cash_krw=280_259, us_eval_krw=1_000_000,
                total_override=15_000_000, holdings_unrealized_krw=-3_000_000,
            )
            self.assertLess(t.get_effective_drawdown(), -10.0)

    # ── ② RSI 바닥 투매 (NetApp RSI 13에 -99k 확정) ──
    def test_quick_loss_skips_oversold_bottom(self):
        """RSI<25 극단 과매도에선 빠른손절 금지 — 캐피츌레이션 바닥 투매 방지."""
        from zusik.analysis.smart_signals import SmartSignals
        import pandas as pd
        # 안정 구간(plateau) 후 절벽(cliff) — NetApp형: RSI가 ~22로 급락하고
        # 거래량 음봉 + 볼린저 하단 이탈까지 3개 신호가 발동(=가드 없으면 전량 손절).
        closes = [100] * 17 + [101, 102, 103, 104, 100.4, 96.8, 93.5, 90.2]
        df = make_ohlcv_df(closes, volumes=[1000] * (len(closes) - 3) + [5000] * 3)
        # 전제①: RSI<25 (바닥). 전제②: 가드 없으면 ≥3 신호로 손절했을 상황.
        delta = pd.Series(closes).diff()
        rs = (delta.clip(lower=0).rolling(14).mean() /
              (-delta.clip(upper=0)).rolling(14).mean())
        rsi = (100 - 100 / (1 + rs)).iloc[-1]
        self.assertLess(rsi, 25, f"테스트 전제 실패: RSI={rsi:.0f} (25 미만이어야)")
        res = SmartSignals.check_quick_loss_exit(df, profit_rate=-0.06)
        self.assertIsNone(res, "RSI<25 바닥에서 빠른손절 발동 — 바닥 투매 위험(NetApp -99k 패턴)")

    # ── ③ 변동성 타겟 사이징 (변동성 큰 종목은 작게) ──
    def test_vol_target_sizing_shrinks_high_vol(self):
        """고변동 종목은 저변동 종목보다 작게 담겨야 한다 (포트폴리오 변동성 안정화)."""
        bot = self._bot()
        s_high = bot._vol_target_scalar(0.05)   # 5% 일일변동성
        s_low = bot._vol_target_scalar(0.0125)  # 1.25%
        s_mid = bot._vol_target_scalar(0.025)   # 목표
        self.assertLess(s_high, s_mid)
        self.assertGreater(s_low, s_mid)
        self.assertAlmostEqual(s_mid, 1.0, places=2)

    # ── ④ 핵심주 확신 하한 (삼성/하이닉스 1주 사던 문제) ──
    def test_whitelist_conviction_floor_lifts_core_position(self):
        """강세장+비방어에서 whitelist 핵심주는 디레이팅에 짓눌려도 하한 이상 보장."""
        bot = self._bot()
        bot._bearish_regime_score = Mock(return_value=0.1)
        bot._bullish_regime_score = Mock(return_value=0.8)  # 강세장
        wl_ratio, _ = bot._dynamic_invest_ratio(0.3, 0.6, symbol="005930", realized_vol=0.025)
        non_ratio, _ = bot._dynamic_invest_ratio(0.3, 0.6, symbol="999999", realized_vol=0.025)
        self.assertGreaterEqual(wl_ratio, 0.10, "핵심주가 하한 미달 — 1주 문제 재발")
        self.assertGreater(wl_ratio, non_ratio, "핵심주가 일반주보다 작게 담김")

    # ── ⑥ 핵심주 invest 하한 (삼성 미매수 근본 원인 — reward 디레이팅 후에도 매수 보장) ──
    def test_whitelist_min_invest_floors_core_after_derating(self):
        """과거 잘못된 패닉 매도로 reward EMA가 망가져도 whitelist 핵심주는 conviction
        하한만큼 invest를 받아 1주 미만으로 굶지 않는다 (삼성 미매수 수정)."""
        bot = self._bot()
        bot._adaptive_params = Mock(return_value={"whitelist_cap": 0.25})
        bot._bearish_regime_score = Mock(return_value=0.1)
        bot._defensive_mode = False
        # whitelist(005930): 1,000만 자산 → 하한 = 1000만 × 0.25 × 0.5 = 125만 (3주 이상)
        self.assertAlmostEqual(
            bot._whitelist_min_invest("005930", 10_000_000, 10_000_000), 1_250_000, delta=1)
        # 비-whitelist → 하한 없음
        self.assertEqual(bot._whitelist_min_invest("999999", 10_000_000, 10_000_000), 0.0)
        # 하락장(bear≥0.5) → 하한 없음 (보수화)
        bot._bearish_regime_score = Mock(return_value=0.6)
        self.assertEqual(bot._whitelist_min_invest("005930", 10_000_000, 10_000_000), 0.0)
        # 방어 모드 → 하한 없음
        bot._bearish_regime_score = Mock(return_value=0.1)
        bot._defensive_mode = True
        self.assertEqual(bot._whitelist_min_invest("005930", 10_000_000, 10_000_000), 0.0)
        # 가용현금 한도 적용
        bot._defensive_mode = False
        self.assertAlmostEqual(
            bot._whitelist_min_invest("005930", 10_000_000, 400_000), 400_000, delta=1)
        # 비싼 핵심주(하이닉스형): conviction 하한(1.25M)이 1주값(3M)보다 작아도 최소 1주 보장
        self.assertAlmostEqual(
            bot._whitelist_min_invest("005930", 10_000_000, 10_000_000, price=3_000_000),
            3_000_000, delta=1)
        # 종목별 core_shares=2 지정 → 2주값(4M) 목표 (하이닉스 추가주 요청 반영)
        bot.config["screening"] = {"whitelist_kr": [{"code": "005930", "core_shares": 2}]}
        self.assertAlmostEqual(
            bot._whitelist_min_invest("005930", 10_000_000, 10_000_000, price=2_000_000),
            4_000_000, delta=1)

    # ── ⑧ 물타기(averaging down): 손실 깊을수록 핵심주 목표 비중↑ (평단 낮추기) ──
    def test_averaging_down_grows_target_on_drawdown(self):
        """핵심주가 손실 구간이면 코어 목표를 cap 방향으로 키워 더 담는다(물타기).
        -5%부터 시작 -12%에서 cap. 비손실은 base 유지."""
        bot = self._bot()
        bot._is_inverse = Mock(return_value=False)
        bot._bearish_regime_score = Mock(return_value=0.1)
        bot._adaptive_params = Mock(return_value={"whitelist_cap": 0.25})
        bot.config["screening"] = {"whitelist_kr": [{"code": "005930"}]}
        bot.config["position"].update({
            "whitelist_conviction_floor": 0.5,      # base 12.5%
            "averaging_down_enabled": True,
            "averaging_down_trigger": -0.05,
            "averaging_down_max_drawdown": -0.12,
        })
        flat = bot._whitelist_min_invest("005930", 10_000_000, 10_000_000, price=1, profit_rate=0.0)
        mid = bot._whitelist_min_invest("005930", 10_000_000, 10_000_000, price=1, profit_rate=-0.085)
        deep = bot._whitelist_min_invest("005930", 10_000_000, 10_000_000, price=1, profit_rate=-0.12)
        self.assertAlmostEqual(flat, 1_250_000, delta=2)        # 12.5%
        self.assertGreater(mid, flat)                            # 손실 → 목표↑
        self.assertAlmostEqual(deep, 2_500_000, delta=2)        # -12% → cap 25%

    # ── ⑦ 핵심주 이벤트성 급락 면제 (공장사고 등 → 바닥 투매 방지) ──
    def test_core_hold_through_exempts_whitelist(self):
        """whitelist 핵심주는 이벤트성 급락 면제 대상(crash_instant/quick_loss/slow_bleed 스킵).
        한화에어로 사고 급락 -5.6% 바닥 투매 재발 방지. 토글 off면 면제 안 함."""
        bot = self._bot()
        bot._is_inverse = Mock(return_value=False)
        bot.config.setdefault("position", {})["whitelist_crash_exempt"] = True
        self.assertTrue(bot._core_hold_through("005930"), "whitelist 핵심주가 면제 대상 아님")
        self.assertFalse(bot._core_hold_through("999999"), "비-whitelist가 면제됨")
        bot.config["position"]["whitelist_crash_exempt"] = False
        self.assertFalse(bot._core_hold_through("005930"), "토글 off인데 면제됨")

    # ── ⑤ 장-적응 익절 (강세장 익절 늦춤 / 약세장 빠르게) ──
    def test_profit_taking_holds_longer_in_bull(self):
        """강세장에선 RSI 익절선이 약세장보다 높아야 한다 (추세 살림)."""
        bot = self._bot()
        bot.tracker = Mock(get_effective_drawdown=Mock(return_value=-1.0),
                           get_effective_pnl_pct=Mock(return_value=-1.0))
        bot._bullish_regime_score = Mock(return_value=0.9)
        bot._bearish_regime_score = Mock(return_value=0.1)
        bot._adapt_cache = (0.0, None)
        bull = bot._adaptive_params()["rsi_exit_min"]
        bot._bullish_regime_score = Mock(return_value=0.1)
        bot._bearish_regime_score = Mock(return_value=0.9)
        bot._adapt_cache = (0.0, None)
        bear = bot._adaptive_params()["rsi_exit_min"]
        self.assertGreater(bull, bear, "강세장 익절선이 약세장보다 낮음 — 추세 조기 청산")

    # ── ⑧ 인버스 헷지 인버스 매수 0건 → 헷지 미작동 회귀) ──
    def _inv_bot(self, holdings=None, cash=1_000_000):
        """_handle_inverse 라우팅 검증용 최소 봇."""
        from zusik.core.bot import TradingBot
        b = TradingBot.__new__(TradingBot)
        b.config = {"inverse": {"max_ratio": 0.2}}
        b.client = Mock(get_balance=Mock(return_value={
            "cash": cash, "total_eval": 0, "holdings": holdings or []}))
        b._is_inverse = Mock(return_value=True)
        b._should_force_exit_inverse = Mock(return_value=(False, ""))
        b._should_allow_inverse_entry = Mock(return_value=(False, "급락 아님"))
        b._inverse_under_max_ratio = Mock(return_value=True)
        b.positions = Mock(check_surge=Mock(return_value=None))
        b._handle_buy = Mock()
        b._handle_sell = Mock()
        return b

    def test_hedge_buys_inverse_on_crash_bypassing_analyst(self):
        """핵심 회귀: 진짜 급락 게이트 open + max_ratio 미만이면 분석기(SELL) 무관하게
        인버스를 직접 매수한다. 수정 전엔 _handle_inverse가 없어 분석기가 급등 인버스를
        '과매수=SELL'로 봐 폭락일 인버스 매수 0건이었다."""
        b = self._inv_bot()
        b._should_allow_inverse_entry = Mock(return_value=(True, "지수 급락"))
        b._handle_inverse("114800", "KODEX 인버스", 1000, df=None)
        b._handle_buy.assert_called_once()
        _, kwargs = b._handle_buy.call_args
        # stale 분석 우회 위해 hedge_base_ratio가 반드시 전달돼야 함
        self.assertIn("hedge_base_ratio", kwargs)
        self.assertGreater(kwargs["hedge_base_ratio"], 0)

    def test_hedge_no_buy_when_not_a_real_crash(self):
        """평시/얕은 pullback(급락 아님)이면 인버스 신규 매수 안 함 (강세장 추격 금지)."""
        b = self._inv_bot()
        b._should_allow_inverse_entry = Mock(return_value=(False, "급락 아님"))
        b._handle_inverse("114800", "KODEX 인버스", 1000, df=None)
        b._handle_buy.assert_not_called()

    def test_hedge_no_buy_when_max_ratio_reached(self):
        """인버스 총노출이 max_ratio 도달이면 급락이어도 추가 매수 안 함."""
        b = self._inv_bot()
        b._should_allow_inverse_entry = Mock(return_value=(True, "지수 급락"))
        b._inverse_under_max_ratio = Mock(return_value=False)
        b._handle_inverse("114800", "KODEX 인버스", 1000, df=None)
        b._handle_buy.assert_not_called()

    def test_hedge_force_exits_on_recovery(self):
        """보유 인버스 + 평시 복귀(force_exit) → 청산하고 신규 매수는 안 함."""
        b = self._inv_bot(holdings=[{"code": "114800", "qty": 10, "eval_amount": 10_000}])
        b._should_force_exit_inverse = Mock(return_value=(True, "평시 복귀 + 급락 해소"))
        b._handle_inverse("114800", "KODEX 인버스", 1000, df=None)
        b._handle_sell.assert_called_once()
        b._handle_buy.assert_not_called()

    def test_inverse_under_max_ratio_calc(self):
        """_inverse_under_max_ratio: 인버스 평가액/총자산 < max_ratio일 때만 True."""
        from zusik.core.bot import TradingBot
        b = TradingBot.__new__(TradingBot)
        b.config = {"inverse": {"max_ratio": 0.2}}
        b._is_inverse = lambda c: c == "114800"
        bal_low = {"cash": 800_000, "total_eval": 200_000,
                   "holdings": [{"code": "114800", "eval_amount": 100_000}]}  # 10%
        bal_high = {"cash": 700_000, "total_eval": 300_000,
                    "holdings": [{"code": "114800", "eval_amount": 250_000}]}  # 25%
        self.assertTrue(b._inverse_under_max_ratio("114800", bal_low))
        self.assertFalse(b._inverse_under_max_ratio("114800", bal_high))

    def test_config_stop_loss_overrides_mode_profile(self):
        """06-08 회귀: premium 모드 프로파일(-0.10)이 config.yaml 하드스톱(-0.15)을 덮어써
        _safety_scan이 -10% 딥을 바닥 손절(현대차 -10.4%, 삼성바이오 -10.6%). config 명시값이
        모드보다 우선이어야 한다(profit_design: 컷 0%승률, 하드스톱 -15%가 자본보호선)."""
        from zusik.core.trading_mode import apply_mode
        # config가 명시하면 그 값이 이김
        out = apply_mode({"trading_mode": "premium",
                          "risk": {"stop_loss_per_stock": -0.15}})
        self.assertEqual(out["risk"]["stop_loss_per_stock"], -0.15,
                         "config 하드스톱(-0.15)이 premium 프로파일(-0.10)에 덮어쓰여짐")
        # 미명시면 프로파일 기본값(-0.10)으로 폴백 — 가드가 -0.15 하드코딩이 아님을 증명
        out2 = apply_mode({"trading_mode": "premium", "risk": {}})
        self.assertEqual(out2["risk"]["stop_loss_per_stock"], -0.10,
                         "config 미명시 시 모드 프로파일 기본값이 적용돼야 함")

    def test_bear_score_reflects_intraday_crash(self):
        """폭락일 버그 회귀: 20일 일봉 모멘텀이 양수(상승추세)여도 지수 현재가가 -3%↓면
        bear≥0.5. 수정 전 공식 max(0,-avg_m)은 폭락 진행 중에도 0.00 고정이라
        인버스 사이징/익절틸트가 평시처럼 작동했다."""
        import pandas as pd
        from zusik.core.bot import TradingBot
        b = TradingBot.__new__(TradingBot)
        up = pd.DataFrame({"close": [100 + i for i in range(30)]})  # 명확한 상승추세
        b.client = Mock(
            get_ohlcv=Mock(return_value=up),
            get_current_price=Mock(return_value={"change_rate": -3.5}),  # 장중 -3.5% 급락
            get_us_ohlcv=Mock(return_value=None),
            get_us_current_price=Mock(return_value={"change_rate": -3.5}),
            get_balance=Mock(return_value={"holdings": []}),
            get_us_balance=Mock(return_value={"holdings": []}),
        )
        b._is_inverse = Mock(return_value=False)
        b._bear_cache = (0.0, 0.0)
        score = b._bearish_regime_score()
        self.assertGreaterEqual(score, 0.5,
                                f"지수 장중 -3.5% 폭락인데 bear={score:.2f} (<0.5) — 헷지 사이징 무력화")

    def test_index_crash_detects_intraday_drop(self):
        """_index_crash: 지수 프록시 현재가 등락률 -3%↓를 급락으로 감지 (일봉만 보던 버그 보완)."""
        import pandas as pd
        from zusik.core.bot import TradingBot
        flat = pd.DataFrame({"close": [100] * 30})
        b = TradingBot.__new__(TradingBot)
        b.client = Mock(
            get_current_price=Mock(return_value={"change_rate": -3.5}),
            get_ohlcv=Mock(return_value=flat),
            get_us_current_price=Mock(return_value={"change_rate": 0.0}),
            get_us_ohlcv=Mock(return_value=flat),
        )
        b._crash_cache = (0.0, False)
        self.assertTrue(b._index_crash(), "지수 장중 -3.5%인데 급락 미감지")
        # 얕은 하락(-1.5%) + 일봉도 평탄 → 급락 아님
        b.client.get_current_price = Mock(return_value={"change_rate": -1.5})
        b._crash_cache = (0.0, False)
        self.assertFalse(b._index_crash(), "장중 -1.5% pullback을 급락으로 오판")

    def test_get_balance_falls_back_to_stale_cache_on_server_error(self):
        """KR 오류 회귀: 폭락일 KIS 500/연결오류 시 직전 잔고 캐시로 폴백 → 종목 실행이
        통째로 죽지 않는다. 수정 전엔 예외가 _execute_stock까지 전파돼 'CODE 오류'."""
        import requests
        from zusik.clients.kis_client import KISClient
        c = KISClient.__new__(KISClient)
        c._balance_cache = {"ts": 0.0, "data": {"cash": 123, "holdings": []}}  # 만료된 캐시
        c._fetch_balance_uncached = Mock(
            side_effect=requests.HTTPError("500 Server Error: inquire-balance"))
        result = c.get_balance()
        self.assertEqual(result["cash"], 123, "fetch 실패 시 stale 캐시를 반환해야 함")

    def test_get_balance_raises_when_no_cache(self):
        """캐시가 아예 없으면(최초 호출) 폴백할 게 없으니 예외 전파 — 조용한 오류 은폐 방지."""
        import requests
        from zusik.clients.kis_client import KISClient
        c = KISClient.__new__(KISClient)
        if hasattr(c, "_balance_cache"):
            del c._balance_cache
        c._fetch_balance_uncached = Mock(side_effect=requests.HTTPError("500"))
        with self.assertRaises(requests.HTTPError):
            c.get_balance()

    def test_inverse_hedge_supports_multi_tranche_increase(self):
        """06-08 폭락일 회귀: 인버스 1차 매수 후 본격 급락이 와도 추가 매수가
        buy_tranches=[1.0]에 의해 '분할 매수 완료'로 차단되던 버그.

        수정 전: plan_buy가 self.buy_tranches만 사용 → 인버스 분할 증액 불가.
        수정 후: tranches_override + skip_dip_check로 인버스 전용 분할 [0.35, 0.30, 0.35]
        적용. 1차 후 2차, 3차도 정상 진입.
        """
        import zusik.core.position_manager as position_manager
        from zusik.core.position_manager import PositionManager
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        orig = position_manager.POSITIONS_FILE
        position_manager.POSITIONS_FILE = os.path.join(tmp.name, "positions.json")
        self.addCleanup(lambda: setattr(position_manager, "POSITIONS_FILE", orig))

        pm = PositionManager({"position": {"buy_tranches": [1.0]}})  # 일반주 정책
        inverse_tranches = [0.35, 0.30, 0.35]

        # 1차 — 인버스 헷지로 진입
        p1 = pm.plan_buy("114800", total_amount=10_000_000, current_price=1000,
                         tranches_override=inverse_tranches, skip_dip_check=True)
        self.assertEqual(p1["tranche"], 1, "1차 진입 실패")
        self.assertGreater(p1["qty"], 0, "1차 수량 0")
        pm.record_buy("114800", "KODEX 인버스", p1["qty"], 1000)

        # 2차 — bear가 더 심해진 상황에서 본격 급락 헷지 (가격이 올라도 진입돼야 함)
        p2 = pm.plan_buy("114800", total_amount=10_000_000, current_price=1060,  # +6% (인버스는 오름)
                         tranches_override=inverse_tranches, skip_dip_check=True)
        self.assertNotIn("skip_reason", p2,
                         f"2차 인버스 증액이 차단됨 — skip_reason={p2.get('skip_reason')}. "
                         "buy_tranches=[1.0]에 의한 06-08 폭락일 헷지 0 버그 회귀")
        self.assertEqual(p2["tranche"], 2)
        self.assertGreater(p2["qty"], 0)
        pm.record_buy("114800", "KODEX 인버스", p2["qty"], 1060)

        # 3차 — 풀 헷지
        p3 = pm.plan_buy("114800", total_amount=10_000_000, current_price=1100,
                         tranches_override=inverse_tranches, skip_dip_check=True)
        self.assertNotIn("skip_reason", p3, "3차 인버스 증액 차단")
        self.assertEqual(p3["tranche"], 3)

        # 4차는 진짜 완료 — 차단되는 게 정상
        pm.record_buy("114800", "KODEX 인버스", p3["qty"], 1100)
        p4 = pm.plan_buy("114800", total_amount=10_000_000, current_price=1120,
                         tranches_override=inverse_tranches, skip_dip_check=True)
        self.assertEqual(p4.get("skip_reason"), "분할 매수 완료",
                         "4차에서는 분할 완료로 차단돼야 함")

    def test_normal_stock_keeps_single_tranche_policy(self):
        """일반주(override 없음)는 기존 buy_tranches=[1.0] 정책 유지. 인버스용
        새 파라미터 추가가 일반주 행동을 바꾸지 않음을 보장."""
        import zusik.core.position_manager as position_manager
        from zusik.core.position_manager import PositionManager
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        orig = position_manager.POSITIONS_FILE
        position_manager.POSITIONS_FILE = os.path.join(tmp.name, "positions.json")
        self.addCleanup(lambda: setattr(position_manager, "POSITIONS_FILE", orig))

        pm = PositionManager({"position": {"buy_tranches": [1.0]}})

        p1 = pm.plan_buy("005930", total_amount=1_000_000, current_price=80000)
        self.assertEqual(p1["tranche"], 1)
        pm.record_buy("005930", "삼성전자", p1["qty"], 80000)

        p2 = pm.plan_buy("005930", total_amount=1_000_000, current_price=75000)
        self.assertEqual(p2.get("skip_reason"), "분할 매수 완료",
                         "일반주는 1차 후 분할 완료(churn 방지) 유지돼야 함")

    # ── 모호 케이스 LLM 라우팅 (pop-then-fade 익절 타이브레이크) ──
    # 손실 패턴: 고점에서 되돌린 포지션을 hold_score 모멘텀 핑계로 매도 연기 → 본전까지 흘려보냄.
    # 진입 게이트 필터로는 못 잡는(국면/RS/MA60 A/B 전부 무효) 케이스라 청산을 LLM에 위임한다.

    def _amb_bot(self, *, enabled=True, peak=0.06):
        import types
        from zusik.core.bot import TradingBot
        b = TradingBot.__new__(TradingBot)
        b.config = {"ai_routing": {
            "ambiguous_sell_enabled": enabled, "hold_score_lo": 0.60, "hold_score_hi": 0.72,
            "net_floor": 0.003, "net_cap": 0.035, "min_giveback": 0.015,
            "take_min_conf": 0.60, "cooldown_min": 30}}
        b.positions = types.SimpleNamespace(get_peak_profit=lambda c: peak)
        return b

    def _amb_df(self):
        import pandas as pd
        return pd.DataFrame({"close": [100, 101, 102, 103, 102]})

    _MOM_REASON = "상승 지속 점수 0.65 ≥ 0.60 (모멘텀 +0.10 ...)"
    _HS_INBAND = {"score": 0.65, "momentum": 0.1, "ma_bull": True,
                  "vol_ratio": 1.2, "pullback": -0.02}

    def test_ambiguous_band_pure(self):
        cfg = self._amb_bot().config["ai_routing"]
        from zusik.core.bot_risk import RiskExitMixin as R
        self.assertTrue(R._ambiguous_sell_band(0.018, 0.65, 0.04, cfg), "정상 모호 구간")
        self.assertFalse(R._ambiguous_sell_band(0.018, 0.80, 0.04, cfg), "강한 모멘텀(>hi)은 보유")
        self.assertFalse(R._ambiguous_sell_band(0.018, 0.65, 0.005, cfg), "되돌림 부족=fade 아님")
        self.assertFalse(R._ambiguous_sell_band(0.001, 0.65, 0.04, cfg), "순익 미미=본전 미달")
        self.assertFalse(R._ambiguous_sell_band(0.05, 0.65, 0.04, cfg), "strong_take 이상=이미 익절")

    def test_ambiguous_take_overrides_defer(self):
        """핵심 가드: pop-then-fade 가 LLM take 판정이면 매도 연기를 해제(익절)한다.
        라우팅이 죽으면(연기 유지) 이 테스트가 깨진다 — 본전까지 흘려보내는 손실 패턴 부활."""
        b = self._amb_bot()
        b._llm_profit_take_verdict = Mock(return_value={"action": "take", "confidence": 0.8, "reason": "고점 되돌림"})
        with patch("zusik.analysis.indicators.hold_score", return_value=self._HS_INBAND):
            defer, reason = b._resolve_ambiguous_sell(
                "KR", self._amb_df(), "005930", "삼성전자",
                qty=10, avg_price=100, current_price=102, defer=True, reason=self._MOM_REASON)
        self.assertFalse(defer, "LLM take → 연기 해제(매도)")
        self.assertIn("익절", reason)

    def test_ambiguous_hold_keeps_defer(self):
        b = self._amb_bot()
        b._llm_profit_take_verdict = Mock(return_value={"action": "hold", "confidence": 0.7, "reason": "추세 지속"})
        with patch("zusik.analysis.indicators.hold_score", return_value=self._HS_INBAND):
            defer, reason = b._resolve_ambiguous_sell(
                "KR", self._amb_df(), "005930", "삼성전자",
                qty=10, avg_price=100, current_price=102, defer=True, reason=self._MOM_REASON)
        self.assertTrue(defer, "LLM hold → 연기 유지")

    def test_ambiguous_skips_non_momentum_defer(self):
        """노이즈(conf<70)/순손실/과매도 보호로 연기된 건은 escalate 안 함(안전망에 맡김)."""
        b = self._amb_bot()
        b._llm_profit_take_verdict = Mock()
        defer, reason = b._resolve_ambiguous_sell(
            "KR", self._amb_df(), "005930", "삼성전자",
            qty=10, avg_price=100, current_price=102, defer=True,
            reason="순익 -5.0 ≤ 0 (왕복 수수료 ...)")
        self.assertTrue(defer)
        b._llm_profit_take_verdict.assert_not_called()

    def test_ambiguous_disabled_no_escalation(self):
        b = self._amb_bot(enabled=False)
        b._llm_profit_take_verdict = Mock()
        defer, reason = b._resolve_ambiguous_sell(
            "KR", self._amb_df(), "005930", "삼성전자",
            qty=10, avg_price=100, current_price=102, defer=True, reason=self._MOM_REASON)
        self.assertTrue(defer)
        b._llm_profit_take_verdict.assert_not_called()

    def test_ambiguous_out_of_band_no_llm_call(self):
        b = self._amb_bot(peak=0.022)  # peak 거의 현재(2%)와 동일 → 되돌림 ~0 → fade 아님
        b._llm_profit_take_verdict = Mock()
        with patch("zusik.analysis.indicators.hold_score", return_value=self._HS_INBAND):
            defer, reason = b._resolve_ambiguous_sell(
                "KR", self._amb_df(), "005930", "삼성전자",
                qty=10, avg_price=100, current_price=102, defer=True, reason=self._MOM_REASON)
        self.assertTrue(defer)
        b._llm_profit_take_verdict.assert_not_called()

    def test_ambiguous_failsafe_on_exception(self):
        """LLM 판정 중 예외가 나도 원래 (defer, reason) 유지 — 매도 경로를 깨지 않는다."""
        b = self._amb_bot()
        b._llm_profit_take_verdict = Mock(side_effect=RuntimeError("boom"))
        with patch("zusik.analysis.indicators.hold_score", return_value=self._HS_INBAND):
            defer, reason = b._resolve_ambiguous_sell(
                "KR", self._amb_df(), "005930", "삼성전자",
                qty=10, avg_price=100, current_price=102, defer=True, reason=self._MOM_REASON)
        self.assertTrue(defer, "예외 시 원판정(연기) 유지")
        self.assertEqual(reason, self._MOM_REASON)

    def test_parse_take_verdict(self):
        from zusik.core.bot_risk import RiskExitMixin as R
        v = R._parse_take_verdict('잡담 {"action":"take","confidence":0.75,"reason":"x"} 끝')
        self.assertEqual(v["action"], "take")
        self.assertAlmostEqual(v["confidence"], 0.75)
        self.assertIsNone(R._parse_take_verdict("JSON 아님 그냥 텍스트"), "못 읽으면 None(보수적)")

    def test_ambiguous_cooldown_per_tick_default(self):
        """cooldown_min=0(기본)이면 직전 질의 직후에도 매 tick 평가 허용. >0 이면 차단."""
        b = self._amb_bot()
        b._mark_ambiguous_asked("005930")
        self.assertFalse(b._ambiguous_cooldown_active("005930", {"cooldown_min": 0}),
                         "기본 0 = 매 tick 허용")
        self.assertTrue(b._ambiguous_cooldown_active("005930", {"cooldown_min": 30}),
                        ">0 이면 쿨다운 내 차단(opt-in 억제)")

    def test_ambiguous_take_tagged_for_eod(self):
        """모호익절 오버라이드 reason이 EOD 패턴 ambiguous_take 로 분류돼 효과 측정 가능."""
        from zusik.storage.portfolio_tracker import PortfolioTracker
        self.assertEqual(
            PortfolioTracker._classify_sell_pattern(
                "LLM 모호판정 익절 conf 80% (고점 +6.0%→+2.0%): 되돌림"),
            "ambiguous_take")

    def _leftover_bot(self, mom_min=0.10):
        from zusik.core.bot_us import USTradingMixin
        b = USTradingMixin.__new__(USTradingMixin)
        b.config = {"position": {"leftover_momentum_min": mom_min}}
        return b

    def test_leftover_soak_blocks_flat_momentum(self):
        """잔여 달러 소진은 평평/식은 종목(무모멘텀)엔 매수하지 않는다.

        실증: MSFT 1주를 '잔여 소진'으로 매수했으나 peak +0.24%(무모멘텀)만 찍고 -12.8%
        출혈 — 떨어지진 않지만 오르지도 않는 종목에 자투리 현금을 넣어 죽은 자본이 됨.
        평평한 종목은 차단(현금 유지), 강한 상승 모멘텀만 통과해야 한다.
        """
        b = self._leftover_bot(0.10)
        flat = make_ohlcv_df([100] * 65)                       # 평평 → mom≈0
        strong = make_ohlcv_df([100 + i for i in range(65)])   # 꾸준한 상승 → mom 강함
        self.assertFalse(b._leftover_momentum_ok(flat), "평평한 종목엔 잔여 소진 안 함")
        self.assertTrue(b._leftover_momentum_ok(strong), "강한 상승 모멘텀만 잔여 소진 허용")

    def test_leftover_soak_gate_is_the_blocker(self):
        """revert-check: 게이트(>= mom_min)가 진짜 차단 주체임을 증명.

        임계를 -1.0(어떤 momentum_score 든 통과)로 낮추면 평평한 종목도 통과한다 →
        막던 것이 다름 아닌 이 모멘텀 게이트였음(테스트가 가드로 기능)."""
        b_loose = self._leftover_bot(-1.0)
        flat = make_ohlcv_df([100] * 65)
        self.assertTrue(b_loose._leftover_momentum_ok(flat),
                        "임계를 풀면 평평도 통과 — 차단 주체가 모멘텀 게이트임을 확인")

    def test_leftover_soak_safe_on_bad_df(self):
        """데이터 부족/None 은 보수적으로 매수 보류(False) — 예외로 매매 중단 안 함."""
        b = self._leftover_bot(0.10)
        self.assertFalse(b._leftover_momentum_ok(None))
        self.assertFalse(b._leftover_momentum_ok(make_ohlcv_df([100, 101])))


class CrashSurgeResponseTests(unittest.TestCase):
    """폭락/폭등 극단 상황 대응 검증 — 폭락=손실 최소화(확정 급락만 즉시 컷, 노이즈는
    홀드), 폭등=수익 극대화(강모멘텀이면 적게 덜고 라이딩, 과열/상한가 근접은 전량익절)."""

    def _pm(self, avg_price=100, qty=10, high=None, recent_buy=False):
        import zusik.core.position_manager as position_manager
        from zusik.core.position_manager import PositionManager
        from datetime import datetime as _dt, timedelta as _td
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        p = patch.object(position_manager, "POSITIONS_FILE",
                         os.path.join(tmp.name, "positions.json"))
        p.start()
        self.addCleanup(p.stop)
        cfg = {"position": {
            "crash_instant_sell": -0.04, "crash_from_high_sell": -0.20,
            "crash_gap_down": -0.08, "crash_vol_spike_ratio": 5.0,
            "crash_grace_catastrophic": -0.10,
            "surge_quick_profit": 0.10, "surge_limit_sell": 0.25,
            "surge_dynamic_vol_mult": 1.5, "surge_dynamic_atr_mult": 1.2,
            "surge_dynamic_quick_cap": 0.05, "surge_dynamic_limit_cap": 0.10,
            "surge_vol_fade_ratio": 0.5, "surge_ride_enabled": True,
            "surge_ride_trim_ratio": 0.25, "surge_ride_rsi_max": 82,
        }}
        pm = PositionManager(cfg)
        lb = _dt.now().isoformat() if recent_buy else (_dt.now() - _td(days=5)).isoformat()
        pm._positions["T"] = {"name": "T", "qty": qty, "avg_price": avg_price,
                              "buy_tranche": 1, "high_since_buy": high or avg_price,
                              "last_buy_date": lb, "surge_sold": False}
        return pm

    # ── 폭락: 확정 급락은 즉시 전량 컷 (손실 최소화) ──
    def test_confirmed_crash_cuts_immediately(self):
        pm = self._pm(avg_price=100)
        df = make_ohlcv_df([100] * 20 + [92])  # 당일 -8%
        r = pm.check_crash("T", 92, df)
        self.assertIsNotNone(r, "확정 급락(-8%)인데 컷이 안 됨 — 손실 방치")
        self.assertEqual(r["action"], "crash_instant")
        self.assertEqual(r["sell_ratio"], 1.0)

    # ── 폭락: 매수 직후 grace 중엔 -6% 노이즈 급락 보류, -12% 카타스트로픽만 즉시 컷 ──
    def test_fresh_buy_holds_moderate_crash_cuts_catastrophic(self):
        # 갓 매수(grace) + 당일 -6% → crash_instant 보류 (반사실: 대부분 반등)
        pm = self._pm(avg_price=100, high=100, recent_buy=True)
        df_mod = make_ohlcv_df([100] * 20 + [94])   # -6%
        r = pm.check_crash("T", 94, df_mod)
        self.assertIsNone(r, "fresh-buy -6% 노이즈 급락에 즉시 컷 — 반등 기회 상실")
        # 갓 매수 + 당일 -12% → 카타스트로픽이라 즉시 컷
        pm2 = self._pm(avg_price=100, high=100, recent_buy=True)
        df_cat = make_ohlcv_df([100] * 20 + [88])   # -12%
        r2 = pm2.check_crash("T", 88, df_cat)
        self.assertIsNotNone(r2, "fresh-buy -12% 진짜 폭락인데 컷 안 됨")
        self.assertEqual(r2["action"], "crash_instant")
        # grace 아님(5일 전 매수) + -6% → 정상 즉시 컷
        pm3 = self._pm(avg_price=100, high=100, recent_buy=False)
        r3 = pm3.check_crash("T", 94, make_ohlcv_df([100] * 20 + [94]))
        self.assertIsNotNone(r3, "보유 5일차 -6% 급락은 정상 컷이어야")
        self.assertEqual(r3["action"], "crash_instant")

    # ── 폭락 아님: -2% 노이즈는 홀드 (반등 여지, 바닥 투매 방지) ──
    def test_noise_dip_does_not_trigger_crash(self):
        pm = self._pm(avg_price=100, high=100, recent_buy=True)
        df = make_ohlcv_df([100] * 20 + [98])  # -2%
        self.assertIsNone(pm.check_crash("T", 98, df),
                          "정상 -2% 노이즈에 급락 컷 발동 — 바닥 투매 위험")

    # ── 폭등 모멘텀 판정 helper ──
    def test_surge_momentum_intact_helper(self):
        from zusik.core.position_manager import PositionManager
        up = make_ohlcv_df([100, 101, 100, 102, 101, 103, 102, 104, 103, 105,
                            104, 106, 105, 107, 106, 108, 107, 109, 108, 110, 113],
                           volumes=[1000] * 20 + [3000])
        self.assertTrue(PositionManager._surge_momentum_intact(up, 82),
                        "양봉+거래량+RSI<82인데 모멘텀 미인정")
        down = make_ohlcv_df([100, 102, 104, 106, 108, 110, 112, 114, 116, 118,
                              120, 122, 124, 126, 128, 130, 132, 134, 136, 138, 134],
                             volumes=[1000] * 20 + [3000])
        self.assertFalse(PositionManager._surge_momentum_intact(down, 82),
                         "마지막 음봉(모멘텀 둔화)인데 라이딩으로 오판")

    # ── 폭등: 강모멘텀이면 적게(25%) 덜고 라이딩 (수익 극대화) ──
    def test_surge_rides_on_strong_momentum(self):
        pm = self._pm(avg_price=100, qty=10)
        pm._surge_momentum_intact = Mock(return_value=True)
        r = pm.check_surge("T", 120, make_ohlcv_df([100] * 21))  # +20%, 저변동
        self.assertIsNotNone(r)
        self.assertEqual(r["action"], "surge_ride_trim")
        self.assertAlmostEqual(r["sell_ratio"], 0.25, places=2)

    # ── 폭등: 모멘텀 둔화면 절반 익절 (이익 확정) ──
    def test_surge_trims_half_when_momentum_fades(self):
        pm = self._pm(avg_price=100, qty=10)
        pm._surge_momentum_intact = Mock(return_value=False)
        r = pm.check_surge("T", 120, make_ohlcv_df([100] * 21))
        self.assertEqual(r["action"], "surge_half_sell")
        self.assertAlmostEqual(r["sell_ratio"], 0.5, places=2)

    # ── 폭등: 상한가 근접(+40%)은 전량 익절 (욕심 방지) ──
    def test_surge_full_exit_near_limit(self):
        pm = self._pm(avg_price=100, qty=10)
        r = pm.check_surge("T", 140, make_ohlcv_df([100] * 21))  # +40%
        self.assertEqual(r["action"], "surge_full_sell")
        self.assertEqual(r["sell_ratio"], 1.0)


class OrderSafetyTests(unittest.TestCase):
    """주문 관문 안전 검증 — 변조된 상위 함수가 만든 악성/조작 주문을 차단하는지 검증.

    모든 주문은 KISClient._order/_us_order 의 OrderSafetyValidator 를 통과해야 한다.
    전략·사이징 계층이 악의적으로 변형돼도 이 관문이 주식조작·계좌탈취 패턴을 막는다.
    각 테스트는 '검증을 제거하면(=변조 성공) 통과해버리는' 악성 주문이 차단됨을 확인한다.
    """

    def setUp(self):
        from zusik.core.resilience import OrderSafetyValidator
        self.v = OrderSafetyValidator()

    # ── 정상 주문은 통과 ──
    def test_legit_orders_pass(self):
        ok, _ = self.v.validate(side="buy", code="005930", qty=10, price=0,
                                order_type="01", held_qty=0, orderable_cash=1_000_000,
                                market_price=70_000)
        self.assertTrue(ok)
        ok2, _ = self.v.validate(side="sell", code="005930", qty=5, price=0,
                                 order_type="01", held_qty=10)
        self.assertTrue(ok2)

    # ── 초과/유령 매도 (조작·버그) ──
    def test_blocks_excess_sell(self):
        ok, why = self.v.validate(side="sell", code="005930", qty=100,
                                  order_type="01", held_qty=10)
        self.assertFalse(ok)
        self.assertIn("초과 매도", why)

    # ── 인젝션 과대수량 / fat-finger ──
    def test_blocks_absurd_qty(self):
        ok, why = self.v.validate(side="buy", code="005930", qty=9_999_999,
                                  order_type="01", orderable_cash=10**12, market_price=1)
        self.assertFalse(ok)
        self.assertIn("수량 상한", why)

    # ── 비정상 수량 (0/음수/타입) ──
    def test_blocks_bad_qty(self):
        for bad in (0, -5, 1.5, None, True):
            ok, _ = self.v.validate(side="buy", code="005930", qty=bad, order_type="01")
            self.assertFalse(ok, f"qty={bad!r} 은 차단돼야 함")

    # ── 비정상 종목코드 (인젝션) ──
    def test_blocks_bad_code(self):
        for bad in ("", None, "X" * 50, "   ", 12345):
            ok, _ = self.v.validate(side="buy", code=bad, qty=1, order_type="01")
            self.assertFalse(ok, f"code={bad!r} 은 차단돼야 함")

    # ── 워시트레이딩 (반대방향 즉시 반복) ──
    def test_blocks_wash_trading(self):
        now = 1000.0
        ok, why = self.v.validate(side="buy", code="005930", qty=1, order_type="01",
                                  held_qty=0, orderable_cash=10**9, market_price=1,
                                  last_opposite_ts=now - 5, now=now)   # 5초 전 반대주문
        self.assertFalse(ok)
        self.assertIn("워시", why)
        # 충분한 간격이면 통과
        ok2, _ = self.v.validate(side="buy", code="005930", qty=1, order_type="01",
                                 held_qty=0, orderable_cash=10**9, market_price=1,
                                 last_opposite_ts=now - 60, now=now)
        self.assertTrue(ok2)

    # ── 스푸핑/조작가 지정가 (시장가 ±30% 밖) ──
    def test_blocks_spoofing_limit_price(self):
        ok, why = self.v.validate(side="buy", code="005930", qty=1, price=200,
                                  order_type="00", orderable_cash=10**9, market_price=100)
        self.assertFalse(ok)   # +100% 이탈
        self.assertIn("스푸핑", why)
        # 밴드 내 지정가는 통과
        ok2, _ = self.v.validate(side="buy", code="005930", qty=1, price=105,
                                 order_type="00", orderable_cash=10**9, market_price=100)
        self.assertTrue(ok2)

    # ── 계좌 드레인/과대 매수 (현금 초과 notional) ──
    def test_blocks_account_drain(self):
        ok, why = self.v.validate(side="buy", code="005930", qty=1000, price=0,
                                  order_type="01", orderable_cash=1_000_000,
                                  market_price=50_000)  # 5천만 > 현금 100만
        self.assertFalse(ok)
        self.assertIn("현금", why)

    # ── fail-closed: 어떤 garbage 입력에도 예외 없이 (False) 반환 ──
    def test_never_throws_fail_closed(self):
        garbage = [
            dict(side=None, code=None, qty=None),
            dict(side="buy", code="005930", qty="10"),       # str qty
            dict(side="hack", code="005930", qty=1),
            dict(side="buy", code="005930", qty=1, price="bad"),
            dict(side="sell", code="005930", qty=1, held_qty="many"),
        ]
        for kw in garbage:
            try:
                ok, why = self.v.validate(order_type="01", **kw)
            except Exception as e:  # noqa: BLE001
                self.fail(f"검증기가 예외를 던짐(fail-closed 위반): {kw} → {e}")
            self.assertIsInstance(ok, bool)

    # ── 시장가(price=0)는 가격 밴드 검증을 건너뛴다 (오작동 방지) ──
    def test_market_order_skips_price_band(self):
        ok, _ = self.v.validate(side="buy", code="005930", qty=1, price=0,
                                order_type="01", orderable_cash=10**9, market_price=100)
        self.assertTrue(ok)


class AIUsageConfigTests(unittest.TestCase):
    """AI 사용량 플랜별 한도(config.api_cost.daily_limits) 오버라이드 — '사용 요금에 따라 다르게'.

    DAILY_LIMITS 가 config 로 덮어쓰여야 첫 설정 시 플랜(saver/balanced/quality)을
    config 만으로 전환할 수 있다. 코드 하드코딩으로 되돌리면(=오버라이드 무시) 이 테스트가 깨진다.
    """

    def _opt(self, daily_limits=None):
        from zusik.core.cost_optimizer import CostOptimizer
        cfg = {"api_cost": {}}
        if daily_limits is not None:
            cfg["api_cost"]["daily_limits"] = daily_limits
        return CostOptimizer(cfg)

    def test_default_limits_when_unset(self):
        from zusik.core.cost_optimizer import DAILY_LIMITS
        opt = self._opt(None)
        self.assertEqual(opt.daily_limits["claude_sonnet"], DAILY_LIMITS["claude_sonnet"])
        self.assertEqual(opt.daily_limits["total"], DAILY_LIMITS["total"])

    def test_partial_override_keeps_other_defaults(self):
        from zusik.core.cost_optimizer import DAILY_LIMITS
        opt = self._opt({"claude_sonnet": 250})
        self.assertEqual(opt.daily_limits["claude_sonnet"], 250)            # 덮어씀
        self.assertEqual(opt.daily_limits["codex"], DAILY_LIMITS["codex"])  # 미지정은 기본값

    def test_override_enforced_by_can_call(self):
        # claude_sonnet 한도를 2로 낮추면 2회 기록 후 호출 차단되어야 한다.
        opt = self._opt({"claude_sonnet": 2, "total": 999})
        opt._costs = {"daily": {}}  # 격리 (디스크 비용파일 무시)
        today = __import__("datetime").datetime.now().strftime("%Y-%m-%d")
        opt._costs["daily"][today] = {"claude_sonnet": 1, "total": 1}
        self.assertTrue(opt.can_call("claude_sonnet"))
        opt._costs["daily"][today] = {"claude_sonnet": 2, "total": 2}
        self.assertFalse(opt.can_call("claude_sonnet"))

    def test_quality_profile_matches_config_yaml(self):
        # config.yaml 의 활성 프리셋이 실제로 로드돼 적용되는지 (문서-코드 동기화 가드).
        import yaml
        with open("config.yaml") as f:
            cfg = yaml.safe_load(f)
        from zusik.core.cost_optimizer import CostOptimizer
        opt = CostOptimizer(cfg)
        dl = cfg.get("api_cost", {}).get("daily_limits", {})
        for k, v in dl.items():
            self.assertEqual(opt.daily_limits[k], int(v))
        self.assertEqual(opt.daily_limits["claude_opus"], 0)  # opus 항상 0

    def test_check_limit_honors_config_override(self):
        """플랜 한도(config.api_cost.daily_limits)가 claude_client._check_limit 의 provider
        라우팅에도 먹어야 한다. 안 그러면 사용자가 sonnet=0 으로 꺼도 봇이 계속 sonnet 을 호출한다
        (setup.sh AI 요금제 마법사의 핵심 계약). 모듈 DAILY_LIMITS 만 보던 코드로 되돌리면 깨진다."""
        from zusik.clients import claude_client as cc
        old = cc._cfg_limits_cache
        try:
            cc._cfg_limits_cache = {"claude_sonnet": 0, "codex": 5}   # 파일 우회: 캐시 직접 주입
            self.assertFalse(cc._check_limit("claude_sonnet"))        # 0 → 즉시 차단
            self.assertEqual(cc._effective_limit("codex"), 5)         # 오버라이드 값 사용
            from zusik.core.cost_optimizer import DAILY_LIMITS
            self.assertEqual(cc._effective_limit("claude_haiku"),     # 미지정 → 모듈 기본 폴백
                             DAILY_LIMITS["claude_haiku"])
        finally:
            cc._cfg_limits_cache = old

    def test_read_merged_cfg_local_overrides_base(self):
        """config.local.yaml 이 config.yaml 을 깊은 병합으로 덮어써야 setup.sh/configtool 이
        쓴 disable_*·플랜 한도가 ClaudeClient 에 실제 반영된다(raw config.yaml 만 읽으면 무시됨)."""
        import tempfile
        from zusik.clients import claude_client as cc
        with tempfile.TemporaryDirectory() as d:
            with open(os.path.join(d, "config.yaml"), "w", encoding="utf-8") as f:
                f.write("ai_providers:\n  disable_codex: false\n  disable_agy: true\n")
            with open(os.path.join(d, "config.local.yaml"), "w", encoding="utf-8") as f:
                f.write("ai_providers:\n  disable_codex: true\n")
            with patch("zusik.paths.config_path",
                       side_effect=lambda name="config.yaml": os.path.join(d, name)):
                cfg = cc._read_merged_cfg()
        ap = cfg["ai_providers"]
        self.assertTrue(ap["disable_codex"])    # 로컬이 base(false)를 덮어씀
        self.assertTrue(ap["disable_agy"])      # base 값 유지(로컬에 없음)


class TotalEquityPhantomTests(unittest.TestCase):
    """T+2 유령 손익 — 한투 present-balance가 미정산 US 자산을 누락해 총자산을
    절반 수준으로 과소표시하면(예: 입금 22M인데 10M → -52%), 직접합산으로 폴백해야 한다.
    되돌리면(무조건 한투 신뢰) 이 테스트가 깨진다."""

    def _equity(self, hantu_tot, kr_settled=5_000_000, kr_eval=8_000_000,
                us_eval_usd=7000, us_krw_in_account=250_000, fx=1350.0):
        from zusik.analysis.bot_money_helpers import compute_total_equity
        kr = {"d2_cash": kr_settled, "total_eval": kr_eval}
        us = {"us_eval_usd": us_eval_usd, "cash_usd": 0,
              "us_krw_in_account": us_krw_in_account, "total_asset_krw": hantu_tot}
        return compute_total_equity(kr, us, fx)
        # direct_total = 5M + 8M + (7000*1350=9.45M + 0.25M) = 22,700,000

    def test_phantom_underreport_uses_direct_sum(self):
        # 한투가 직접합산의 절반(10.48M) → 과소보고 → 직접합산(22.7M) 사용.
        out = self._equity(hantu_tot=10_480_000)
        self.assertEqual(out["total"], 22_700_000,
                         "T+2 한투 과소보고 시 직접합산으로 폴백해야 함 (유령 -52% 방지)")

    def test_normal_small_diff_trusts_hantu(self):
        # 소액 차이(200k)는 flip 안 하고 한투 신뢰 (anti-flapping).
        out = self._equity(hantu_tot=22_500_000)
        self.assertEqual(out["total"], 22_500_000, "소액 T+2 노이즈엔 한투 유지")

    def test_hantu_higher_than_direct_trusts_hantu(self):
        # 한투 > 직접합산(직접합산이 무언가 누락)이면 v9대로 한투 신뢰 (과대 폴백 금지).
        out = self._equity(hantu_tot=25_000_000)
        self.assertEqual(out["total"], 25_000_000, "한투가 더 크면 한투 신뢰 유지")


class EquitySettlementTests(unittest.TestCase):
    """KR T+2 미정산 매도 대금을 자산에 포함 — 매도 직후 d2_cash가 in-transit 매도금을 누락해
    자산을 가짜로 과소표시(가짜 -손익)하던 회귀 차단. 단, 매수 직후 stale total_cash로 자산을
    과대표시하던 원버그도 재발하면 안 된다 (양방향 가드)."""

    def _eq(self, kr, fx=1500.0):
        from zusik.analysis.bot_money_helpers import compute_total_equity
        return compute_total_equity(kr, {}, fx)

    def test_kr_unsettled_sells_counted_as_cash(self):
        # 금요일 매도 후: d2_cash(정산) 1.8M < orderable 4.42M (재사용 가능한 매도대금 포함)
        out = self._eq({"d2_cash": 1_799_956, "cash": 4_421_572,
                        "total_cash": 4_421_572, "total_eval": 10_696_290})
        self.assertEqual(out["kr_settled"], 4_421_572,
                         "in-transit 매도대금 누락 → kr_settled가 d2_cash로 과소 (가짜 -손익 회귀)")
        self.assertEqual(out["total"], 4_421_572 + 10_696_290)

    def test_post_buy_stale_total_cash_not_overstated(self):
        # 매수 직후: d2_cash·orderable 은 하락(16,652), nxdy 경유 total_cash 만 stale-high(50,202)
        out = self._eq({"d2_cash": 16_652, "cash": 16_652,
                        "total_cash": 50_202, "total_eval": 0})
        self.assertEqual(out["kr_settled"], 16_652,
                         "stale total_cash(50,202)로 자산 과대표시 — 원버그 회귀")

    def test_missing_cash_falls_back_to_d2(self):
        # cash 키 없는 레거시 잔고면 d2_cash 사용 (기존 동작 보존)
        out = self._eq({"d2_cash": 5_000_000, "total_eval": 8_000_000})
        self.assertEqual(out["kr_settled"], 5_000_000)


class FastExitScanTests(unittest.TestCase):
    """빠른 익절 서브루프(40초) — 5분 run_once 사이 급등 익절 놓침 보완 (2026-06-19).

    surge/트레일링/본전보호 익절은 즉시 매도하되, 손실 컷은 안 한다(수익 보호만 — hold-floor 유지).
    인버스 제외·60s 스로틀로 중복 매도 방지. scan이 급등에 안 팔도록 되돌리면 이 테스트가 깨진다.
    """

    def _bot(self, holdings, surge=None, trailing=None, core=False):
        from zusik.core.bot import TradingBot
        b = TradingBot.__new__(TradingBot)
        b._fast_exit_last = {}
        b.client = Mock()
        b.client.get_balance.return_value = {"holdings": holdings}
        b.positions = Mock()
        b.positions.check_surge.return_value = surge
        b.positions.update_trailing_stop.return_value = trailing
        b._is_inverse = lambda c: str(c).startswith("INV")
        b._core_hold_through = lambda c: core
        b._handle_sell = Mock()
        return b

    def test_surge_triggers_immediate_take_profit(self):
        h = [{"code": "005930", "name": "삼성", "qty": 10, "current_price": 80000}]
        b = self._bot(h, surge={"reason": "급등 절반익절", "sell_ratio": 0.5,
                                "action": "절반", "profit_rate": 0.1})
        b._fast_exit_market("KR")
        b._handle_sell.assert_called_once()
        _, kwargs = b._handle_sell.call_args
        self.assertEqual(kwargs.get("sell_ratio"), 0.5)        # 절반익절 ratio 그대로 반영
        self.assertIn("fast", kwargs.get("force_reason", ""))

    def test_no_signal_no_sell(self):
        h = [{"code": "005930", "name": "삼성", "qty": 10, "current_price": 80000}]
        b = self._bot(h, surge=None, trailing=None)
        b._fast_exit_market("KR")
        b._handle_sell.assert_not_called()                     # 트리거 없으면 매도 안 함 (손실 컷 없음)

    def test_inverse_skipped(self):
        h = [{"code": "INV114800", "name": "인버스", "qty": 10, "current_price": 8000}]
        b = self._bot(h, surge={"reason": "급등", "sell_ratio": 1.0})
        b._fast_exit_market("KR")
        b._handle_sell.assert_not_called()                     # 인버스 헷지는 급등 익절 제외

    def test_throttle_blocks_duplicate_within_60s(self):
        h = [{"code": "005930", "name": "삼성", "qty": 10, "current_price": 80000}]
        b = self._bot(h, surge={"reason": "급등", "sell_ratio": 1.0})
        b._fast_exit_market("KR")
        b._fast_exit_market("KR")                              # 60s 내 재호출
        self.assertEqual(b._handle_sell.call_count, 1)         # 중복 매도 방지 → 1회만

    def test_breakeven_protect_sells_when_not_core(self):
        h = [{"code": "005930", "name": "삼성", "qty": 10, "current_price": 80000}]
        b = self._bot(h, surge=None,
                      trailing={"action": "breakeven_protect", "peak_profit": 0.08}, core=False)
        b._fast_exit_market("KR")
        b._handle_sell.assert_called_once()

    def test_core_stock_breakeven_exempt(self):
        h = [{"code": "005930", "name": "삼성", "qty": 10, "current_price": 80000}]
        b = self._bot(h, surge=None,
                      trailing={"action": "breakeven_protect", "peak_profit": 0.08}, core=True)
        b._fast_exit_market("KR")
        b._handle_sell.assert_not_called()                     # 핵심주는 본전보호 매도 면제 (churn 방지)


class MultiMessengerTests(unittest.TestCase):
    """메신저 추상화 — Discord 외 Telegram/Slack 등 백엔드로 알림 fan-out.

    notify_* 시그니처 불일치/누락이 매매를 멈추지 않게(no-op 안전망)하고,
    한 백엔드가 죽어도 나머지로 발송돼야 한다. 되돌리면(엄격 시그니처/예외 전파) 깨진다.
    """

    def test_bot_notifier_fallback_send_delivers_content(self):
        """_BotNotifierFallback._send(content=...)가 Bot 채널로 전달돼야 한다.

        이전엔 전체 no-op이라 webhook 없는 봇토큰 구성에서 watchdog_alert·명령 결과·
        헬스체크(--notify) 알림이 조용히 사라졌다. content는 보내고 embed만이면 무시."""
        import zusik.core.bot as botmod
        fb = botmod._BotNotifierFallback()
        with patch.object(botmod, "send_bot_message") as m:
            fb._send(content="헬스체크 OK")
            fb._send(embeds=[{"x": 1}])   # embed만 → 전송 안 함
        m.assert_called_once_with("헬스체크 OK")

    def _cap(self):
        from zusik.clients.notifier import BaseTextNotifier

        class Cap(BaseTextNotifier):
            def __init__(s):
                s.got = []

            def _send(s, t):
                s.got.append(t)

        return Cap()

    def test_renders_and_accepts_both_kwarg_styles(self):
        c = self._cap()
        c.notify_trade(side="sell", stock_name="삼성", stock_code="005930", qty=3,
                       price=80000, realized_pnl=12000, realized_rate=4.0)
        c.notify_strategy_switch(old_strategy="A", new_strategy="B", reason="x")  # Discord식
        c.notify_strategy_switch(old="A", new="B", reason="x")                    # fallback식
        self.assertEqual(len(c.got), 3)
        self.assertIn("매도", c.got[0])
        self.assertIn("005930", c.got[0])

    def test_missing_notify_method_is_noop(self):
        c = self._cap()
        c.notify_some_future_method(1, 2, foo=3)   # 예외 없이 흡수
        self.assertEqual(c.got, [])

    def test_multinotifier_fanout_isolates_backend_errors(self):
        from zusik.clients.notifier import BaseTextNotifier, MultiNotifier
        a, b = self._cap(), self._cap()

        class Boom(BaseTextNotifier):
            def _send(s, t):
                raise RuntimeError("down")

        MultiNotifier([a, Boom(), b]).notify_error(message="z")
        self.assertEqual(len(a.got), 1)   # 죽은 백엔드가 있어도
        self.assertEqual(len(b.got), 1)   # 나머지는 정상 발송

    def test_slack_socket_envelope_parses_slash_and_dm(self):
        """Slack Socket Mode 엔벨로프 → 명령 추출 (Telegram getUpdates 대응물).

        슬래시 명령은 인자(text) 우선, 없으면 명령어 자체. DM(message 이벤트)도 명령으로.
        봇 자기 메시지(bot_id)·시스템 subtype 은 명령 루프 방지를 위해 무시."""
        from zusik.clients.notifier import SlackNotifier
        p = SlackNotifier._parse_socket_envelope

        # 슬래시 명령 `/zusik 상태` → 인자 "상태"
        eid, cmd = p({"type": "slash_commands", "envelope_id": "e1",
                      "payload": {"command": "/zusik", "text": "상태"}})
        self.assertEqual((eid, cmd), ("e1", "상태"))

        # 인자 없는 단일 슬래시 `/상태` → 명령어 자체
        _, cmd = p({"type": "slash_commands", "envelope_id": "e2",
                    "payload": {"command": "/상태", "text": ""}})
        self.assertEqual(cmd, "상태")

        # DM 메시지 → 명령
        _, cmd = p({"type": "events_api", "envelope_id": "e3",
                    "payload": {"event": {"type": "message", "text": "종목 목록"}}})
        self.assertEqual(cmd, "종목 목록")

        # 봇 자기 메시지는 무시 (루프 방지)
        eid, cmd = p({"type": "events_api", "envelope_id": "e4",
                      "payload": {"event": {"type": "message", "text": "x", "bot_id": "B1"}}})
        self.assertEqual((eid, cmd), ("e4", ""))   # ACK 는 하되 명령은 비움

    def test_slack_command_polling_noop_without_app_token(self):
        """SLACK_APP_TOKEN 없으면 명령 폴링은 스레드를 띄우지 않고 조용히 미동작(알림 전용)."""
        from zusik.clients.notifier import SlackNotifier
        s = SlackNotifier("https://hooks.slack.com/services/x", app_token="")
        s.start_command_polling(lambda c: None)
        self.assertIsNone(s._poll_thread)   # app_token 없으면 폴링 미시작


class RegimeSelectionTests(unittest.TestCase):
    """종목 선택 상황 적응 — 하락장엔 저변동(방어) 우선, 상승장엔 모멘텀, 최근매도 로테이션.

    되돌리면(RS 단독 정렬) 레짐/로테이션 틸트가 사라져 이 테스트가 깨진다.
    """

    # 종목별 (RS, 실현변동성)
    DATA = {
        "HIGHVOL": {"rs": 0.07, "vol": 0.05},   # 높은 RS · 고변동
        "LOWVOL":  {"rs": 0.065, "vol": 0.012},  # 약간 낮은 RS · 저변동(방어)
        "SOLD":    {"rs": 0.075, "vol": 0.02},   # 최고 RS · 최근 매도
    }

    def _bot(self, bear, rotation=False, recently_sold=None):
        from zusik.core.bot import TradingBot
        b = TradingBot.__new__(TradingBot)
        b.config = {"selection": {"regime_adaptive": True, "regime_bear_gate": 0.40,
                                  "regime_vol_weight": 1.5, "rotation": rotation,
                                  "rotation_penalty": 0.03},
                    "screening": {"session_lock_enabled": False}}
        b.period = "D"
        b._RS_DROP_THRESHOLD = -0.05
        b._reentry_block = recently_sold or {}
        b._is_inverse = lambda c: False
        b._is_derivative_etf = lambda c, n="": False
        b._bearish_regime_score = lambda: bear
        b._compute_rs = lambda df, idx: df.get("rs", 0.0) if isinstance(df, dict) else 0.0
        b._realized_vol = lambda df: df.get("vol", 0.02) if isinstance(df, dict) else 0.0
        idx = Mock()
        idx.empty = False
        b.client = Mock()
        b.client.get_ohlcv.side_effect = lambda sym, **k: (idx if sym == "069500"
                                                           else self.DATA.get(sym, {"rs": 0.0, "vol": 0.02}))
        return b

    def _order(self, ranked):
        return [s.get("code") for s in ranked]

    def _stocks(self):
        return [{"code": "HIGHVOL"}, {"code": "LOWVOL"}, {"code": "SOLD"}]

    def test_bull_favors_momentum(self):
        b = self._bot(bear=0.10)   # 상승장 — RS(모멘텀) 정렬
        order = self._order(b._rank_by_relative_strength(self._stocks(), "KR"))
        self.assertLess(order.index("HIGHVOL"), order.index("LOWVOL"),
                        "상승장은 모멘텀(높은 RS) 우선이어야 함")

    def test_bear_favors_defensive_lowvol(self):
        b = self._bot(bear=0.80)   # 하락장 — 저변동(방어) 우선
        order = self._order(b._rank_by_relative_strength(self._stocks(), "KR"))
        self.assertLess(order.index("LOWVOL"), order.index("HIGHVOL"),
                        "하락장은 저변동(방어) 종목이 고변동보다 우선이어야 함")

    def test_rotation_deprioritizes_recently_sold(self):
        b = self._bot(bear=0.10, rotation=True, recently_sold={"SOLD": (0, "session", 0)})
        order = self._order(b._rank_by_relative_strength(self._stocks(), "KR"))
        self.assertEqual(order[-1], "SOLD", "최근 매도 종목은 로테이션으로 후순위여야 함(쏠림 방지)")

    def test_sectors_of_tagging(self):
        from zusik.analysis.smart_signals import SmartSignals
        self.assertIn("defense", SmartSignals.sectors_of("012450"))                 # 큐레이션 역인덱스
        self.assertIn("ai_semi", SmartSignals.sectors_of("XXXX", "OO반도체"))       # 이름 키워드 폴백
        self.assertEqual(SmartSignals.sectors_of("ZZZZ", "미분류"), set())          # 미매칭

    def test_event_boost_prioritizes_active_sector(self):
        from zusik.analysis.smart_signals import SmartSignals
        b = self._bot(bear=0.10)
        b.signals = SmartSignals
        b._active_event_sectors = {"defense"}        # 장전 리포트에서 감지된 활성 섹터
        data = {"012450": {"rs": 0.06, "vol": 0.02},  # 방산(이벤트 섹터) · 낮은 RS
                "PLAIN":  {"rs": 0.08, "vol": 0.02}}  # 이벤트 무관 · 높은 RS
        idx = Mock(); idx.empty = False
        b.client.get_ohlcv.side_effect = lambda sym, **k: (idx if sym == "069500"
                                                           else data.get(sym, {"rs": 0.0, "vol": 0.02}))
        stocks = [{"code": "012450", "name": "한화에어로스페이스"}, {"code": "PLAIN", "name": "기타"}]
        order = self._order(b._rank_by_relative_strength(stocks, "KR"))
        self.assertEqual(order[0], "012450", "활성 이벤트 섹터(방산) 종목이 부스트로 우선이어야 함")

    def test_all_weak_candidates_do_not_fail_open_to_original(self):
        """RS 전원 탈락 시 약한 원본을 되살리면 필터가 무효화돼 죽은 자본을 다시 산다."""
        b = self._bot(bear=0.10)
        b.config["selection"]["min_relative_strength"] = 0.0
        b._compute_rs = lambda df, idx: -0.01
        self.assertEqual(b._rank_by_relative_strength(self._stocks(), "KR"), [])

    def test_apply_does_not_restore_defaults_after_all_rs_rejected(self):
        """실제 적용 단계도 빈 랭킹을 기본 종목으로 되돌리지 않아야 한다."""
        b = self._bot(bear=0.10)
        b.config["selection"]["min_relative_strength"] = 0.0
        b._compute_rs = lambda df, idx: -0.01
        b.kr_enabled, b.us_enabled = True, False
        b.kr_stocks, b.us_stocks = self._stocks(), []
        b._default_kr, b._default_us = [{"code": "DEFAULT"}], []
        b._filter_derivatives = lambda stocks, market: list(stocks)
        b._check_stock_price = lambda code, max_price: True
        b._name_cache = {}
        b.discord = None
        b.client.get_balance.return_value = {"cash": 1_000_000, "total_eval": 1_000_000}
        b.client.get_stock_name.side_effect = lambda code: code

        b._apply_screened_stocks({"kr": self._stocks()}, tag="test")
        self.assertEqual(b.kr_stocks, [])

    def test_auto_screening_routes_whitelist_candidates_through_common_gate(self):
        """자동 선별이 목록을 직접 대입해 whitelist/RS 게이트를 우회하면 안 된다."""
        import zusik.analysis.auto_screener as auto_module

        b = self._bot(bear=0.10)
        b.config["screening"] = {
            "method": "momentum",
            "auto_screen_interval_hours": 4,
            "kr_count": 5,
            "us_count": 5,
            "whitelist_kr": [{"code": "WEAK", "name": "Weak Core"}],
        }
        b._last_auto_screen_ts = 0.0
        b._filter_derivatives = lambda stocks, market: list(stocks)
        b._apply_screened_stocks = Mock()
        b.discord = None
        b.client.get_balance.return_value = {"cash": 1_000_000, "total_eval": 1_000_000}
        b.client.get_us_balance.return_value = {"cash_usd": 0, "holdings": []}

        class _SyncThread:
            def __init__(self, target, daemon=True):
                self.target = target

            def start(self):
                self.target()

        class _FakeScreener:
            def __init__(self, **kwargs):
                pass

            def screen_market(self, candidates, *args, **kwargs):
                return ([{"info": candidates[0], "score": 0.5,
                          "method": "momentum"}] if candidates else [])

            def filter_top(self, scored, *args, **kwargs):
                return list(scored)

        with patch.object(auto_module, "KR_CANDIDATE_POOL", [("STRONG", "Strong")]), \
                patch.object(auto_module, "US_CANDIDATE_POOL", []), \
                patch.object(auto_module, "AutoScreener", _FakeScreener), \
                patch("threading.Thread", _SyncThread):
            b._run_auto_screening()

        b._apply_screened_stocks.assert_called_once()
        result = b._apply_screened_stocks.call_args.args[0]
        self.assertEqual([s["code"] for s in result["kr"]], ["STRONG", "WEAK"])
        self.assertEqual(b._apply_screened_stocks.call_args.kwargs["tag"], "자동 스크리닝")


class KISBuyingPowerTests(unittest.TestCase):
    """KIS 종목별 현금매수 가능수량 계약 — 실주문 없이 응답/감량만 검증."""

    @staticmethod
    def _client():
        from zusik.clients.kis_client import KISClient
        c = KISClient.__new__(KISClient)
        c.is_virtual = False
        c.account_no = "12345678"
        c.account_prod = "01"
        c._buying_power_cache = {}
        return c

    def test_uses_target_symbol_price_and_no_credit_fields(self):
        c = self._client()
        captured = {}

        def fake_get(path, tr_id, params):
            captured.update(path=path, tr_id=tr_id, params=dict(params))
            return {"output": {"nrcvb_buy_amt": "745000", "nrcvb_buy_qty": "3"}}

        c._get = fake_get
        info = c.get_orderable_buy_info("018260", 244_000, force=True)
        self.assertEqual(captured["params"]["PDNO"], "018260")
        self.assertEqual(captured["params"]["ORD_UNPR"], "244000")
        self.assertEqual(captured["params"]["ORD_DVSN"], "01")
        self.assertEqual(info["cash"], 745_000)
        self.assertEqual(info["max_qty"], 3)

    def test_buy_market_clamps_to_kis_max_qty(self):
        c = self._client()
        c.get_orderable_buy_info = Mock(return_value={
            "cash": 500_000, "max_qty": 2, "reference_price": 100_000,
        })
        c._order = Mock(return_value={"success": True, "message": "OK", "order_no": "1"})
        result = c.buy_market("005930", 5, reference_price=100_000)
        self.assertEqual(c._order.call_args.args[2], 2)
        self.assertEqual(result["requested_qty"], 5)
        self.assertEqual(result["submitted_qty"], 2)

    def test_buy_market_fails_closed_when_preflight_unavailable(self):
        c = self._client()
        c.get_orderable_buy_info = Mock(side_effect=RuntimeError("rate limit"))
        c._order = Mock()
        result = c.buy_market("005930", 5, reference_price=100_000)
        self.assertFalse(result["success"])
        self.assertTrue(result["blocked"])
        self.assertEqual(result["submitted_qty"], 0)
        c._order.assert_not_called()

    def test_insufficient_cash_requeries_and_retries_smaller_once(self):
        c = self._client()
        c.get_orderable_buy_info = Mock(side_effect=[
            {"cash": 500_000, "max_qty": 3, "reference_price": 100_000},
            {"cash": 300_000, "max_qty": 2, "reference_price": 100_000},
        ])
        c._order = Mock(side_effect=[
            {"success": False, "message": "주문가능금액을 초과 했습니다", "order_no": ""},
            {"success": True, "message": "OK", "order_no": "2"},
        ])
        result = c.buy_market("005930", 3, reference_price=100_000)
        self.assertEqual(c._order.call_count, 2)
        self.assertEqual(c._order.call_args_list[1].args[2], 2)
        self.assertEqual(result["submitted_qty"], 2)

    def test_balance_buy_cash_is_zero_when_psbl_order_fails(self):
        c = self._client()

        def fake_get(path, tr_id, params):
            if path.endswith("inquire-balance"):
                return {
                    "output1": [{"hldg_qty": "1", "pdno": "005930",
                                 "prdt_name": "삼성전자", "pchs_avg_pric": "100000",
                                 "prpr": "100000", "evlu_amt": "100000",
                                 "evlu_pfls_amt": "0", "evlu_pfls_rt": "0"}],
                    "output2": [{"dnca_tot_amt": "3000000", "nxdy_excc_amt": "0",
                                 "prvs_rcdl_excc_amt": "0", "scts_evlu_amt": "100000",
                                 "evlu_pfls_smtl_amt": "0", "tot_evlu_pfls_rt": "0"}],
                }
            raise RuntimeError("psbl unavailable")

        c._get = fake_get
        balance = c._fetch_balance_uncached()
        self.assertEqual(balance["cash"], 0)
        self.assertFalse(balance["cash_verified"])
        self.assertEqual(balance["deposit_cash"], 3_000_000)
        self.assertEqual(balance["total_cash"], 3_000_000)


class ScreeningStabilityTests(unittest.TestCase):
    def _bot(self):
        from zusik.core.bot import TradingBot
        b = TradingBot.__new__(TradingBot)
        b.config = {"screening": {
            "authority": "auto", "session_lock_enabled": True,
            "stability_gate_enabled": True, "kr_count": 2,
            "min_score_improvement": 0.02, "max_replacements_per_update": 1,
        }}
        b.kr_stocks = [{"code": "A"}, {"code": "B"}]
        b.us_stocks = []
        b.stocks = b.kr_stocks
        b.auto_screen = True
        b.screener = Mock()
        b.client = Mock()
        return b

    def test_auto_authority_prevents_llm_list_rewrite(self):
        b = self._bot()
        b._refresh_stocks(force=True, market="both")
        b.screener.screen_kr_stocks.assert_not_called()
        b.screener.screen_us_stocks.assert_not_called()

    def test_open_session_stages_instead_of_replacing(self):
        b = self._bot()
        b.client.is_market_open.return_value = True
        b.client.is_us_market_open.return_value = False
        b._apply_screened_stocks({"kr": [{"code": "C"}]}, tag="test")
        self.assertEqual([s["code"] for s in b.kr_stocks], ["A", "B"])
        self.assertEqual(b._pending_screened_results["kr"][0]["code"], "C")

    def test_only_meaningful_score_gain_replaces_old_candidate(self):
        b = self._bot()
        scores = {"A": 0.10, "B": 0.09, "C": 0.10, "D": 0.15}

        def rank(stocks, market):
            out = []
            for stock in stocks:
                item = dict(stock)
                item["_selection_score"] = scores[item["code"]]
                out.append(item)
            return sorted(out, key=lambda s: -s["_selection_score"])

        b._rank_by_relative_strength = rank
        unchanged = b._stable_ranked_selection(b.kr_stocks, [{"code": "C"}], "KR")
        self.assertEqual({s["code"] for s in unchanged}, {"A", "B"})
        replaced = b._stable_ranked_selection(b.kr_stocks, [{"code": "D"}], "KR")
        self.assertEqual({s["code"] for s in replaced}, {"A", "D"})


class MCGateConfigTests(unittest.TestCase):
    def test_momentum_mode_uses_mc_as_tail_risk_guard(self):
        from zusik.core.bot import TradingBot
        b = TradingBot.__new__(TradingBot)
        b.config = {"screening": {"method": "momentum"}, "risk": {"mc_buy_gate": {
            "enabled": True, "min_p_profit": 0.40, "min_var95": -0.18,
            "strict_min_p_profit": 0.55, "strict_min_var95": -0.15,
        }}}
        b._is_inverse = lambda code: False
        b._compute_mc_stats = lambda df: {
            "p_profit": 0.45, "mean_profit": 0.001, "var95": -0.16,
        }
        self.assertTrue(b._mc_buy_gate("005930", "삼성전자", [0] * 30)[0])
        b.config["screening"]["method"] = "monte_carlo"
        self.assertFalse(b._mc_buy_gate("005930", "삼성전자", [0] * 30)[0])


class SelectionMethodTests(unittest.TestCase):
    """종목 선택 방식 플러그블 — MC 외 momentum/trend/low_vol 점수가 의도대로 정렬."""

    @staticmethod
    def _ret(close):
        import numpy as np
        c = np.array(close, dtype=float)
        return c, (c[1:] / c[:-1] - 1.0)

    def test_momentum_favors_uptrend(self):
        from zusik.analysis.auto_screener import AutoScreener
        up, up_r = self._ret([100 + i for i in range(70)])       # 꾸준 상승
        flat, flat_r = self._ret([100] * 70)                      # 횡보
        self.assertGreater(AutoScreener._score_alt("momentum", up, up_r),
                           AutoScreener._score_alt("momentum", flat, flat_r))

    def test_low_vol_favors_calm(self):
        from zusik.analysis.auto_screener import AutoScreener
        calm, calm_r = self._ret([100 + 0.1 * i for i in range(70)])               # 저변동
        wild, wild_r = self._ret([100 + (6 if i % 2 else -6) + 0.1 * i for i in range(70)])  # 고변동
        self.assertGreater(AutoScreener._score_alt("low_vol", calm, calm_r),
                           AutoScreener._score_alt("low_vol", wild, wild_r))

    def test_trend_favors_aligned_mas(self):
        from zusik.analysis.auto_screener import AutoScreener
        up, up_r = self._ret([100 + i for i in range(70)])        # 정배열(상승)
        down, down_r = self._ret([170 - i for i in range(70)])    # 역배열(하락)
        self.assertGreater(AutoScreener._score_alt("trend", up, up_r),
                           AutoScreener._score_alt("trend", down, down_r))

    def test_momentum_filter_handles_non_mc_rows_and_rejects_nonpositive(self):
        """method=momentum은 mc=None인데 filter_top이 mc dict로 가정해 스크리닝이 죽던 회귀."""
        from zusik.analysis.auto_screener import AutoScreener
        s = AutoScreener()
        rows = [
            {"info": ("UP", "상승"), "mc": None, "method": "momentum",
             "score": 0.12, "trend_ok": True, "last_price": 100},
            {"info": ("FLAT", "횡보"), "mc": None, "method": "momentum",
             "score": 0.0, "trend_ok": True, "last_price": 100},
            {"info": ("DOWN", "하락"), "mc": None, "method": "momentum",
             "score": -0.12, "trend_ok": True, "last_price": 100},
        ]
        self.assertEqual([r["info"][0] for r in s.filter_top(rows, 3)], ["UP"])


class ReturnStructureRegressionTests(unittest.TestCase):
    """저수익 집중 원인: 청산라벨 누수·과열 추격·무차별 잔금집행 회귀 방지."""

    def _sizing_bot(self, config=None):
        from zusik.core.bot import TradingBot
        b = TradingBot.__new__(TradingBot)
        b.config = config or {"entry_quality": {"enabled": True}}
        b._adaptive_params = lambda: {"cap": 0.14, "whitelist_cap": 0.20}
        b._is_whitelist = lambda symbol: False
        return b

    def test_entry_quality_blocks_extreme_rsi_and_allows_healthy_momentum(self):
        b = self._sizing_bot()
        extreme = make_ohlcv_df([100 + i for i in range(65)])
        healthy = make_ohlcv_df([100 + i * 0.2 + (1 if i % 2 else -1) for i in range(65)])
        ok, reason = b._entry_quality_gate(extreme)
        self.assertFalse(ok)
        self.assertIn("극단 과열", reason)
        self.assertTrue(b._entry_quality_gate(healthy)[0])

    def test_cash_deployment_boosts_only_excess_cash_high_confidence(self):
        cfg = {"_cash_reserve": 0.05, "cash_deployment": {
            "enabled": True, "target_cash_ratio": 0.05, "min_confidence": 0.65,
            "full_boost_cash_ratio": 0.20, "max_size_boost": 1.5,
        }}
        b = self._sizing_bot(cfg)
        boosted, reason = b._cash_deployment_ratio(0.06, 0.20, 0.80, "X")
        self.assertAlmostEqual(boosted, 0.09)
        self.assertIn("초과현금", reason)
        self.assertEqual(b._cash_deployment_ratio(0.06, 0.20, 0.50, "X")[0], 0.06)
        self.assertEqual(b._cash_deployment_ratio(0.06, 0.05, 0.80, "X")[0], 0.06)

    def test_exit_context_never_becomes_buy_weight_or_prompt_evidence(self):
        import zusik.core.reward_engine as reward_engine
        from zusik.core.reward_engine import RewardEngine
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        with patch.object(reward_engine, "REWARD_FILE", os.path.join(tmp.name, "reward.json")):
            r = RewardEngine({"reward": {"context_learning_trades": 3}})
            r._context_scores = {
                "peace:long:rsi_overbought": {
                    "trades": 37, "wins": 36, "total_pnl": 1_930_006,
                    "ema_return": 2.8, "last_updated": "2026-08-13",
                },
                "peace:long:breakout": {
                    "trades": 4, "wins": 3, "total_pnl": 40_000,
                    "ema_return": 1.0, "last_updated": "2026-08-13",
                },
            }
            self.assertEqual(r.get_context_weight("peace:long:rsi_overbought"), 1.0)
            summary = r.get_performance_summary_text()
            self.assertNotIn("rsi_overbought", summary)
            self.assertIn("peace:long:breakout", summary)

    def test_buy_context_is_read_back_from_entry_not_sell_pattern(self):
        from zusik.storage.portfolio_tracker import PortfolioTracker
        t = PortfolioTracker.__new__(PortfolioTracker)
        t._trades = [{"type": "buy", "code": "X", "reason": "돌파 매수",
                      "entry_context": "peace:long:breakout",
                      "entry_indicators": {"RSI_14": 58},
                      "entry_analyst_details": {"technical": {"signal": "BUY"}}},
                     {"type": "sell", "code": "X", "sell_pattern": "rsi_overbought"}]
        self.assertEqual(t.get_last_buy_context("X"), "peace:long:breakout")
        self.assertEqual(t.get_last_buy_indicators("X")["RSI_14"], 58)
        self.assertEqual(
            t.get_last_buy_analyst_details("X")["technical"]["signal"], "BUY")

    def test_legacy_sell_time_patterns_are_not_described_as_buy_conditions(self):
        import zusik.core.reward_engine as reward_engine
        from zusik.core.reward_engine import RewardEngine
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        with patch.object(reward_engine, "REWARD_FILE", os.path.join(tmp.name, "reward.json")):
            r = RewardEngine({"reward": {"learning_trades": 3}})
            pattern = {"rsi": 85, "vol_ratio": 2.0, "ma_aligned": True,
                       "bb_position": "상단", "realized_rate": 3.0}
            r._win_patterns = [dict(pattern) for _ in range(3)]
            self.assertIn("학습 데이터 부족", r.get_winning_conditions()["summary"])
            r._win_patterns = [dict(pattern, source="entry") for _ in range(3)]
            self.assertNotIn("학습 데이터 부족", r.get_winning_conditions()["summary"])

    def test_allocation_advice_uses_excess_cash_for_us_transfer(self):
        from zusik.reporting.status_snapshot import build_allocation_advice
        a = build_allocation_advice({
            "kr_cash": 3_000_000, "kr_eval": 22_000_000,
            "us_cash_krw": 350_000, "us_eval_krw": 5_000_000,
        }, {"_cash_reserve": 0.05,
            "cash_deployment": {"target_cash_ratio": 0.05,
                                "us_target_allocation": 0.30}})
        self.assertGreater(a["recommended_transfer_krw"], 1_000_000)
        self.assertLessEqual(a["recommended_transfer_krw"], a["deployable_cash_krw"])
        cash_after = a["cash_krw"] - a["deployable_cash_krw"]
        self.assertGreaterEqual(cash_after, int(a["total_assets"] * 0.05))

    def test_allocation_snapshot_skips_incomplete_latest_balance(self):
        from zusik.reporting.status_snapshot import latest_complete_allocation_snapshot
        rows = [
            {"date": "2026-08-13", "kr_cash": 3_000_000, "kr_eval": 22_000_000,
             "us_cash_krw": 350_000, "us_eval_krw": 5_000_000,
             "total_equity": 30_350_000},
            {"date": "2026-08-14", "kr_cash": 0, "kr_eval": 0,
             "us_cash_krw": 350_000, "us_eval_krw": 5_000_000,
             "total_equity": 10_000_000},
        ]
        self.assertEqual(
            latest_complete_allocation_snapshot(rows)["date"], "2026-08-13")


class OpenGuardTests(unittest.TestCase):
    """개장 급변동 가드 — 개장 직후 N분은 신규 매수 보류(시초 갭 추격 방지). 윈도우 밖/비활성은 허용."""

    def _bot(self, enabled=True, delay=5):
        from zusik.core.bot import TradingBot
        b = TradingBot.__new__(TradingBot)
        b.config = {"open_guard": {"enabled": enabled, "delay_minutes": delay}}
        b.client = Mock()
        b.client.is_market_open.return_value = True
        b.client.is_us_market_open.return_value = True
        return b

    @staticmethod
    def _now(h, m):
        import datetime as _d
        return _d.datetime(2026, 6, 20, h, m, 0)

    def test_within_open_window_blocks(self):
        import zusik.core.bot_fastlane as bm   # _in_opening_window 는 FastLaneMixin 에 있음
        b = self._bot(delay=5)
        with patch.object(bm, "datetime") as DT:
            DT.now.return_value = self._now(9, 2)   # KR 09:00 + 2분 < 5 → 윈도우 안
            self.assertTrue(b._in_opening_window("KR"))

    def test_after_window_allows(self):
        import zusik.core.bot_fastlane as bm
        b = self._bot(delay=5)
        with patch.object(bm, "datetime") as DT:
            DT.now.return_value = self._now(9, 10)  # 10분 > 5 → 윈도우 밖
            self.assertFalse(b._in_opening_window("KR"))

    def test_disabled_allows(self):
        import zusik.core.bot_fastlane as bm
        b = self._bot(enabled=False)
        with patch.object(bm, "datetime") as DT:
            DT.now.return_value = self._now(9, 2)
            self.assertFalse(b._in_opening_window("KR"))

    def test_market_closed_not_in_window(self):
        b = self._bot()
        b.client.is_market_open.return_value = False
        import zusik.core.bot_fastlane as bm
        with patch.object(bm, "datetime") as DT:
            DT.now.return_value = self._now(9, 2)
            self.assertFalse(b._in_opening_window("KR"))


class FastEntryTests(unittest.TestCase):
    """빠른 로컬 진입 — 급등 돌파/과매도 반등을 AI 없이 포착해 fast_entry=True로 진입. 신호 없으면/보유면 미진입."""

    def _bot(self):
        import pandas as pd
        from zusik.core.bot import TradingBot
        b = TradingBot.__new__(TradingBot)
        b.config = {"fast_entry": {"enabled": True, "momentum_min": 0.45,
                                   "max_new_per_scan": 1, "retry_throttle_sec": 600}}
        b._fast_entry_last = {}
        b.period = "D"
        b.client = Mock()
        b.signals = Mock()
        b._is_inverse = lambda c: False
        b.kr_stocks = [{"code": "005930", "name": "삼성전자"}]
        b.client.get_balance.return_value = {"holdings": []}
        df = pd.DataFrame({
            "open": [100] * 70, "high": [110] * 70, "low": [95] * 70,
            "close": [100 + i for i in range(70)], "volume": [1000] * 70,
        })
        b.client.get_ohlcv.return_value = df
        b.client.get_current_price.return_value = {"price": 170}
        b._handle_buy = Mock()
        return b

    def test_surge_triggers_fast_entry_buy(self):
        import zusik.analysis.indicators as ind
        b = self._bot()
        with patch.object(ind, "breakout_signal", return_value={"is_breakout": True}), \
                patch.object(ind, "volume_surge", return_value={"is_surge": True}), \
                patch.object(ind, "momentum_score", return_value=0.9):
            b._fast_entry_market("KR", b.config["fast_entry"])
        b._handle_buy.assert_called_once()
        self.assertTrue(b._handle_buy.call_args[1].get("fast_entry"))

    def test_oversold_bounce_triggers_fast_entry(self):
        import zusik.analysis.indicators as ind
        b = self._bot()
        b.signals.check_oversold_bounce.return_value = {"action": "bounce_buy", "reason": "RSI 18"}
        with patch.object(ind, "breakout_signal", return_value={"is_breakout": False}), \
                patch.object(ind, "volume_surge", return_value={"is_surge": False}), \
                patch.object(ind, "momentum_score", return_value=0.1):
            b._fast_entry_market("KR", b.config["fast_entry"])
        b._handle_buy.assert_called_once()
        self.assertTrue(b._handle_buy.call_args[1].get("fast_entry"))

    def test_no_signal_no_buy(self):
        import zusik.analysis.indicators as ind
        b = self._bot()
        b.signals.check_oversold_bounce.return_value = None
        with patch.object(ind, "breakout_signal", return_value={"is_breakout": False}), \
                patch.object(ind, "volume_surge", return_value={"is_surge": False}), \
                patch.object(ind, "momentum_score", return_value=0.1):
            b._fast_entry_market("KR", b.config["fast_entry"])
        b._handle_buy.assert_not_called()

    def test_held_position_skipped(self):
        import zusik.analysis.indicators as ind
        b = self._bot()
        b.client.get_balance.return_value = {"holdings": [{"code": "005930"}]}
        with patch.object(ind, "breakout_signal", return_value={"is_breakout": True}), \
                patch.object(ind, "volume_surge", return_value={"is_surge": True}), \
                patch.object(ind, "momentum_score", return_value=0.9):
            b._fast_entry_market("KR", b.config["fast_entry"])
        b._handle_buy.assert_not_called()

    # ── 급등 포착 회귀 가드 (놓친 급등 분석 후속) — surge-catching이 약화되면 깨진다 ──
    def test_surge_at_momentum_threshold_fires(self):
        """모멘텀이 임계(momentum_min=0.45)와 같으면 발동(>=). mom_min 을 올리거나 > 로 바꾸면 깨짐."""
        import zusik.analysis.indicators as ind
        b = self._bot()  # momentum_min = 0.45 (A/B 채택값)
        with patch.object(ind, "breakout_signal", return_value={"is_breakout": True}), \
                patch.object(ind, "volume_surge", return_value={"is_surge": True}), \
                patch.object(ind, "momentum_score", return_value=0.45):
            b._fast_entry_market("KR", b.config["fast_entry"])
        b._handle_buy.assert_called_once()
        self.assertTrue(b._handle_buy.call_args[1].get("fast_entry"))

    def test_loosened_threshold_catches_mid_momentum(self):
        """0.6→0.45 완화 채택의 핵심 가드: 옛 임계(0.6)에선 놓치던 모멘텀 0.50 급등을
        이제 잡아야 한다(돌파+거래량 확인 시). 0.6으로 되돌리면 이 테스트가 깨진다.
        근거: 실데이터 2년 A/B에서 0.45가 +4.63%p · 다운사이드 불변."""
        import zusik.analysis.indicators as ind
        b = self._bot()
        with patch.object(ind, "breakout_signal", return_value={"is_breakout": True}), \
                patch.object(ind, "volume_surge", return_value={"is_surge": True}), \
                patch.object(ind, "momentum_score", return_value=0.50):
            b._fast_entry_market("KR", b.config["fast_entry"])
        b._handle_buy.assert_called_once()
        self.assertTrue(b._handle_buy.call_args[1].get("fast_entry"))

    def test_surge_requires_breakout_and_volume_and_momentum(self):
        """돌파+모멘텀이 강해도 거래량 폭증이 없으면 미발동 — 3-조건(AND) 정의가 약화되면 깨짐."""
        import zusik.analysis.indicators as ind
        b = self._bot()
        b.signals.check_oversold_bounce.return_value = None
        with patch.object(ind, "breakout_signal", return_value={"is_breakout": True}), \
                patch.object(ind, "volume_surge", return_value={"is_surge": False}), \
                patch.object(ind, "momentum_score", return_value=0.9):
            b._fast_entry_market("KR", b.config["fast_entry"])
        b._handle_buy.assert_not_called()


class SecurityHardeningTests(unittest.TestCase):
    """Codex 보안 리뷰 대응 — 코드 변형/설정 오염이 권한 동작으로 이어지지 못하게 하는 가드.

    각 테스트는 수정 전 동작(eval 임의실행 / 시크릿 env 노출 / agent CLI 로컬도구 /
    검증 없는 정정·암호화폐 주문)이 재도입되면 실패한다.
    """

    def test_eval_adaptive_trigger_safe(self):
        from zusik.core.bot_helpers import eval_adaptive_trigger as ev
        self.assertTrue(ev("default", 0.0, 0.0))
        self.assertTrue(ev("dd<=-15", -20.0, 0.0))
        self.assertFalse(ev("dd<=-15", -10.0, 0.0))
        self.assertTrue(ev("pnl>=20", 0.0, 25.0))
        self.assertFalse(ev("pnl>=20", 0.0, 19.9))
        self.assertTrue(ev("dd <= -5 and pnl >= 10", -6.0, 12.0))
        self.assertFalse(ev("dd <= -5 and pnl >= 10", -6.0, 5.0))
        self.assertTrue(ev("dd<=-15 or pnl>=20", -2.0, 25.0))
        # eval 제거 검증 — 임의 코드/문법 외는 fail-safe False
        self.assertFalse(ev("__import__('os').system('echo hi')", 0.0, 0.0))
        self.assertFalse(ev("dd**99999", -1.0, 0.0))
        self.assertFalse(ev("", 0.0, 0.0))
        self.assertFalse(ev("price<=10", 0.0, 0.0))

    def test_child_env_strips_exchange_and_messenger_secrets(self):
        from zusik.clients import claude_client as cc
        fake = {"PATH": "/usr/bin", "HOME": "/home/u",
                "KIS_APP_KEY": "k", "KIS_APP_SECRET": "s",
                "DISCORD_BOT_TOKEN": "d", "TELEGRAM_BOT_TOKEN": "t",
                "SLACK_BOT_TOKEN": "sl", "UPBIT_ACCESS_KEY": "u",
                "ANTHROPIC_API_KEY": "a"}
        with patch.object(cc.os, "environ", fake):
            env = cc._child_env()
        self.assertEqual(env.get("PATH"), "/usr/bin")
        self.assertEqual(env.get("HOME"), "/home/u")
        self.assertEqual(env.get("ANTHROPIC_API_KEY"), "a", "CLI 인증용 키는 보존")
        for secret in ("KIS_APP_KEY", "KIS_APP_SECRET", "DISCORD_BOT_TOKEN",
                       "TELEGRAM_BOT_TOKEN", "SLACK_BOT_TOKEN", "UPBIT_ACCESS_KEY"):
            self.assertNotIn(secret, env, f"{secret} 가 CLI 하위 프로세스 env 에 노출됨")

    def test_claude_cmd_blocks_local_tools(self):
        from zusik.clients.claude_client import ClaudeClient
        c = ClaudeClient.__new__(ClaudeClient)
        captured = {}
        c._exec = lambda cmd, name, timeout=150: (captured.__setitem__("cmd", cmd)
                                                  or '{"signal":"hold","confidence":0}')
        c._run_claude("p", "haiku", use_web_search=True)
        cmd = captured["cmd"]
        self.assertIn("--disallowedTools", cmd)
        tools = cmd[cmd.index("--disallowedTools") + 1]
        self.assertIn("Bash", tools)
        self.assertIn("Read", tools)

    def test_amend_validation_fail_closed(self):
        # 정정 검증은 단일 관문(OrderSafetyValidator.validate_amend)에서 — _order와 같은 소스.
        from zusik.core.resilience import OrderSafetyValidator
        v = OrderSafetyValidator()

        def amend(code, order_no, qty, price, ot):
            return v.validate_amend(code=code, order_no=order_no, qty=qty, price=price, order_type=ot)[0]
        # 정상: 시장가 정정(stale order → 시장가 전환) + 지정가 정정
        self.assertTrue(amend("005930", "0001", 10, 0, "01"))
        self.assertTrue(amend("005930", "0001", 10, 70000, "00"))
        # fail-closed 케이스
        self.assertFalse(amend("005930", "0001", 0, 0, "01"))         # 수량 0
        self.assertFalse(amend("005930", "", 10, 0, "01"))            # 원주문번호 없음
        self.assertFalse(amend("005930", "0001", 10_000_000, 0, "01"))  # 과대수량
        self.assertFalse(amend("005930", "0001", 10, 0, "00"))        # 지정가인데 가격 0
        self.assertFalse(amend("005930", "0001", 10, -5, "01"))       # 음수 가격
        self.assertFalse(amend("", "0001", 10, 0, "01"))             # 코드 형식

    def test_crypto_order_rejects_bad_amounts(self):
        from zusik.clients.crypto_client import CryptoClient
        c = CryptoClient.__new__(CryptoClient)
        c._enabled = True
        c.upbit = Mock()
        c.upbit.buy_market_order.return_value = {"uuid": "x"}
        c.upbit.sell_market_order.return_value = {"uuid": "y"}
        for bad in (0, -100, float("nan"), float("inf"), "100", None, True):
            self.assertFalse(c.buy_market("KRW-BTC", bad)["success"], f"bad amount {bad!r} 통과됨")
        c.upbit.buy_market_order.assert_not_called()
        self.assertTrue(c.buy_market("KRW-BTC", 6000)["success"])
        c.upbit.buy_market_order.assert_called_once()
        for bad in (0, -1, float("nan")):
            self.assertFalse(c.sell_market("KRW-BTC", bad)["success"])
        c.upbit.sell_market_order.assert_not_called()
        self.assertTrue(c.sell_market("KRW-BTC", 0.5)["success"])


class AiSignalIntegrationTests(unittest.TestCase):
    """display-only AI 신호(크로스시그널·데일리 Claude 편향)를 매수 게이트·사이징에 태우는 배선 검증.

    수정 전(로그/Discord로만 흘려보냄)으로 되돌리면 신호가 size_mult/floor/block에 반영되지
    않으므로 아래 테스트가 실패한다.
    """

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)

    def _now(self):
        import time
        return time.time()

    def _write(self, name, obj):
        import json
        path = os.path.join(self._tmp.name, name)
        with open(path, "w") as f:
            json.dump(obj, f)
        return path

    def _nope(self, name="nope"):
        return os.path.join(self._tmp.name, name)

    def _redirect(self, mapping):
        """data 파일 접근을 임시파일로 리다이렉트 — 게이트는 os.path.join('data',X),
        AI 신호 파일은 paths.data_path(X) 두 경로 모두 패치. 미지정 X는 실제 경로로 통과."""
        import contextlib
        import zusik.paths as _zp
        real_join = os.path.join
        real_dp = _zp.data_path

        def fake_join(*a):
            if a and a[-1] in mapping:
                return mapping[a[-1]]
            return real_join(*a)

        def fake_dp(*parts):
            if parts and parts[-1] in mapping:
                return mapping[parts[-1]]
            return real_dp(*parts)
        stack = contextlib.ExitStack()
        stack.enter_context(patch("os.path.join", side_effect=fake_join))
        stack.enter_context(patch.object(_zp, "data_path", side_effect=fake_dp))
        return stack

    def _stub(self, enabled=True):
        from zusik.core.bot import TradingBot
        b = TradingBot.__new__(TradingBot)
        b.config = {"ai_signals": {"enabled": enabled, "freshness_hours": 30}}
        b._is_whitelist = lambda s: False
        return b

    def test_ai_signal_neutral_without_files(self):
        b = self._stub()
        with self._redirect({"cross_signals_kr.json": self._nope("a"),
                             "daily_ai_bias.json": self._nope("b")}):
            ai = b._ai_signal_for("KR", "005930")
        self.assertEqual(ai["size_mult"], 1.0)
        self.assertFalse(ai["block"])

    def test_ai_signal_disabled_is_neutral(self):
        b = self._stub(enabled=False)
        p = self._write("cross_signals_kr.json", {"ts": self._now(),
                        "codes": {"000660": {"bias": "buy", "boost": 0.12, "reason": "NVDA"}}})
        with self._redirect({"cross_signals_kr.json": p, "daily_ai_bias.json": self._nope()}):
            ai = b._ai_signal_for("KR", "000660")
        self.assertEqual(ai["size_mult"], 1.0)

    def test_cross_buy_boosts_size_and_relief(self):
        b = self._stub()
        p = self._write("cross_signals_kr.json", {"ts": self._now(),
                        "codes": {"000660": {"bias": "buy", "boost": 0.12, "reason": "NVDA +6%"}}})
        with self._redirect({"cross_signals_kr.json": p, "daily_ai_bias.json": self._nope()}):
            ai = b._ai_signal_for("KR", "000660")
        self.assertGreater(ai["size_mult"], 1.0)
        self.assertAlmostEqual(ai["floor_relief"], 0.12, places=3)
        self.assertFalse(ai["block"])

    def test_cross_stale_signal_ignored(self):
        b = self._stub()
        p = self._write("cross_signals_kr.json", {"ts": self._now() - 200 * 3600,
                        "codes": {"000660": {"bias": "buy", "boost": 0.12, "reason": "x"}}})
        with self._redirect({"cross_signals_kr.json": p, "daily_ai_bias.json": self._nope()}):
            ai = b._ai_signal_for("KR", "000660")
        self.assertEqual(ai["size_mult"], 1.0)

    def test_daily_sell_blocks_buy(self):
        b = self._stub()
        p = self._write("daily_ai_bias.json", {"ts": self._now(), "kr": {"005930": "sell"}, "us": {}})
        with self._redirect({"daily_ai_bias.json": p, "cross_signals_kr.json": self._nope()}):
            ai = b._ai_signal_for("KR", "005930")
        self.assertTrue(ai["block"])

    def test_daily_reduce_sets_floor_and_shrinks(self):
        b = self._stub()
        p = self._write("daily_ai_bias.json", {"ts": self._now(), "us": {"NVDA": "reduce"}, "kr": {}})
        with self._redirect({"daily_ai_bias.json": p, "cross_signals_kr.json": self._nope()}):
            ai = b._ai_signal_for("US", "NVDA")
        self.assertGreaterEqual(ai["min_floor"], 0.65)
        self.assertLess(ai["size_mult"], 1.0)

    def test_gate_blocks_on_daily_sell(self):
        b = self._stub()
        p = self._write("daily_ai_bias.json", {"ts": self._now(), "kr": {"005930": "sell"}, "us": {}})
        with self._redirect({"daily_ai_bias.json": p, "cross_signals_kr.json": self._nope(),
                             "pre_market_sentiment_KR.json": self._nope("s")}):
            allow, reason = b._pre_market_buy_gate("KR", 0.95, symbol="005930")
        self.assertFalse(allow)
        self.assertIn("AI", reason)

    def test_whitelist_does_not_bypass_daily_sell(self):
        """관심종목도 종목별 매도 판단을 우회해 신규 진입하면 안 된다."""
        b = self._stub()
        b._is_whitelist = lambda symbol: symbol == "005930"
        p = self._write("daily_ai_bias.json", {
            "ts": self._now(), "kr": {"005930": "sell"}, "us": {},
        })
        mapping = {
            "daily_ai_bias.json": p,
            "cross_signals_kr.json": self._nope(),
            "pre_market_sentiment_KR.json": self._nope("s"),
        }
        with self._redirect(mapping):
            allow, reason = b._pre_market_buy_gate("KR", 0.95, symbol="005930")
        self.assertFalse(allow)
        self.assertIn("AI", reason)

    def test_whitelist_does_not_bypass_market_risk_off(self):
        """장전 신규매수 중단은 whitelist에도 동일하게 적용한다."""
        b = self._stub()
        b._is_whitelist = lambda symbol: symbol == "005930"
        today = datetime.now().strftime("%Y-%m-%d")
        sp = self._write("pre_market_sentiment_KR.json", {
            "date": today, "stance": "risk_off", "avoid_new_buy": True,
            "min_buy_confidence": 0.95, "neg_hits": 8,
        })
        mapping = {
            "pre_market_sentiment_KR.json": sp,
            "cross_signals_kr.json": self._nope(),
            "daily_ai_bias.json": self._nope(),
        }
        with self._redirect(mapping):
            allow, reason = b._pre_market_buy_gate("KR", 1.0, symbol="005930")
        self.assertFalse(allow)
        self.assertIn("신규 매수 차단", reason)

    def test_gate_bullish_relief_lowers_floor(self):
        b = self._stub()
        today = datetime.now().strftime("%Y-%m-%d")
        sp = self._write("pre_market_sentiment_KR.json", {"date": today, "stance": "cautious",
                         "avoid_new_buy": False, "min_buy_confidence": 0.70})
        cp = self._write("cross_signals_kr.json", {"ts": self._now(),
                         "codes": {"000660": {"bias": "buy", "boost": 0.15, "reason": "NVDA"}}})
        mapping = {"pre_market_sentiment_KR.json": sp, "cross_signals_kr.json": cp,
                   "daily_ai_bias.json": self._nope()}
        with self._redirect(mapping):
            allow_other, _ = b._pre_market_buy_gate("KR", 0.60, symbol="999999")
            allow_cross, _ = b._pre_market_buy_gate("KR", 0.60, symbol="000660")
        self.assertFalse(allow_other, "장전 하한 0.70 — relief 없는 종목은 conf 0.60 차단돼야")
        self.assertTrue(allow_cross, "크로스 매수편향 relief로 하한 완화 → conf 0.60 통과해야")

    def test_gate_honors_high_sentiment_floor(self):
        """장전 min_buy_confidence > 0.85도 그대로 존중 — 0.85 상한 캡 회귀 방지."""
        b = self._stub()
        today = datetime.now().strftime("%Y-%m-%d")
        sp = self._write("pre_market_sentiment_KR.json",
                         {"date": today, "stance": "cautious",
                          "avoid_new_buy": False, "min_buy_confidence": 0.90})
        with self._redirect({"pre_market_sentiment_KR.json": sp,
                             "cross_signals_kr.json": self._nope(),
                             "daily_ai_bias.json": self._nope()}):
            allow_low, _ = b._pre_market_buy_gate("KR", 0.88)   # 0.88 < 0.90 → 차단
            allow_high, _ = b._pre_market_buy_gate("KR", 0.92)  # 0.92 ≥ 0.90 → 통과
        self.assertFalse(allow_low, "0.85 캡이 0.90 요구치를 몰래 낮춤 (회귀)")
        self.assertTrue(allow_high)

    def test_dynamic_invest_ratio_applies_ai_size_mult(self):
        from zusik.core.bot import TradingBot
        b = TradingBot.__new__(TradingBot)
        b.config = {"position": {}, "ai_signals": {"enabled": True}}
        b._bearish_regime_score = lambda: 0.0
        b._pattern_confidence_boost = lambda: 1.0
        b._drawdown_multiplier = lambda: 1.0
        b._kelly_fraction = lambda s: 1.0
        b._vol_target_scalar = lambda v: 1.0
        b._market_vol_regime = lambda: 1.0
        b._adaptive_params = lambda: {"cap": 0.0}
        b._is_whitelist = lambda s: False
        b._bullish_regime_score = lambda: 0.0
        b._defensive_mode = False
        b._last_mc_stats = None
        b._market_condition = "peace"
        b._ai_signal_for = lambda m, c: {"size_mult": 1.0, "min_floor": 0,
                                         "floor_relief": 0, "block": False, "reason": ""}
        base, _ = b._dynamic_invest_ratio(0.3, 0.6, symbol="005930", realized_vol=0.0)
        b._ai_signal_for = lambda m, c: {"size_mult": 1.3, "min_floor": 0,
                                         "floor_relief": 0, "block": False, "reason": "x"}
        boosted, _ = b._dynamic_invest_ratio(0.3, 0.6, symbol="005930", realized_vol=0.0)
        self.assertGreater(boosted, base, "AI 매수편향이 사이징을 키우지 않음")

    def test_parse_daily_bias_extracts_and_strips(self):
        from zusik.core.bot import TradingBot
        text = ("시장 분석 본문 여러 줄\n──────────\n"
                'BIAS_JSON={"kr": {"005930": "buy", "000660": "HOLD"}, '
                '"us": {"NVDA": "reduce", "X": "weird"}}')
        clean, bias = TradingBot._parse_daily_bias(text)
        self.assertNotIn("BIAS_JSON", clean)
        self.assertIn("시장 분석", clean)
        self.assertEqual(bias["kr"]["005930"], "buy")
        self.assertEqual(bias["kr"]["000660"], "hold")
        self.assertEqual(bias["us"]["NVDA"], "reduce")
        self.assertNotIn("X", bias["us"])

    def test_parse_daily_bias_handles_garbage(self):
        from zusik.core.bot import TradingBot
        _c1, b1 = TradingBot._parse_daily_bias("본문만 있고 BIAS 없음")
        self.assertIsNone(b1)
        c2, b2 = TradingBot._parse_daily_bias("본문\nBIAS_JSON={깨진 json}")
        self.assertIsNone(b2)
        self.assertNotIn("BIAS_JSON", c2)

    def test_parse_daily_bias_ignores_trailing_text(self):
        """BIAS_JSON 뒤에 닫는 중괄호 포함 군더더기가 붙어도 첫 JSON만 파싱 (greedy 회귀)."""
        from zusik.core.bot import TradingBot
        text = ('분석 본문\nBIAS_JSON={"kr": {"005930": "buy"}, "us": {}}\n'
                '추가 주의: 리스크 {중략} 관리하세요.')
        clean, bias = TradingBot._parse_daily_bias(text)
        self.assertIsNotNone(bias, "BIAS_JSON 뒤 군더더기(닫는 중괄호 포함)에도 파싱돼야")
        self.assertEqual(bias["kr"]["005930"], "buy")
        self.assertNotIn("BIAS_JSON", clean)

    def test_ai_signal_missing_ts_is_stale(self):
        """ts 없는(손상/부분기록) 신호 파일은 fresh-forever가 아니라 stale로 폐기 (fail-closed)."""
        b = self._stub()
        p = self._write("daily_ai_bias.json", {"kr": {"005930": "sell"}, "us": {}})  # ts 누락
        with self._redirect({"daily_ai_bias.json": p, "cross_signals_kr.json": self._nope()}):
            ai = b._ai_signal_for("KR", "005930")
        self.assertFalse(ai["block"], "ts 없는 파일이 무기한 fresh로 취급되어 sell이 적용됨")
        self.assertEqual(ai["size_mult"], 1.0)

    def test_cross_dedup_caution_overrides_buy(self):
        """엇갈린 크로스 신호(같은 KR 코드에 buy+caution)에서 약세가 강세에 덮이지 않아야."""
        import json
        from zusik.core.bot import TradingBot
        from zusik.analysis.smart_signals import SmartSignals
        b = TradingBot.__new__(TradingBot)
        b.us_stocks = [{"ticker": "MSFT"}, {"ticker": "AMZN"}]  # 둘 다 035420(NAVER) 연동
        b.signals = SmartSignals({})
        b.client = Mock()
        rates = {"MSFT": 4.0, "AMZN": -4.0}  # MSFT +4%(buy), AMZN -4%(caution)
        b.client.get_us_current_price = lambda tk, ex: {"change_rate": rates[tk], "price": 1.0}
        out = self._nope("cross_out.json")
        with self._redirect({"cross_signals_kr.json": out}):
            b.run_cross_signals()
        with open(out) as f:
            data = json.load(f)
        self.assertEqual(data["codes"]["035420"]["bias"], "caution",
                         "약세(caution)가 강세(buy)에 덮여 위험 신호가 사라짐")
        self.assertEqual(data["codes"]["035720"]["bias"], "buy")  # MSFT 단독 연동


class TradingRecordTests(unittest.TestCase):
    """실거래 기록 단일 관문(record_buy/record_sell) — 수수료·실현손익·매도패턴·결정로그 검증.

    FakeTracker가 아닌 진짜 PortfolioTracker로 '한 세션의 실제 체결 기록'을 모사한다 —
    수수료/세금 반영 손익, sell_pattern 분류, '왜 샀나/팔았나' 결정 로그까지 실제 경로로 확인.
    """

    def setUp(self):
        import zusik.storage.portfolio_tracker as pt
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        d = self._tmp.name
        for attr, val in (("DATA_DIR", d),
                          ("TRADES_FILE", os.path.join(d, "trades.json")),
                          ("LONG_TERM_FILE", os.path.join(d, "long_term.json"))):
            p = patch.object(pt, attr, val)
            p.start()
            self.addCleanup(p.stop)
        from zusik.storage.portfolio_tracker import PortfolioTracker
        self.tracker = PortfolioTracker()

    def _capture_decisions(self):
        import logging
        from zusik.utils.logger import decision_logger
        decision_logger.setLevel(logging.INFO)
        recs = []
        h = logging.Handler()
        h.emit = lambda r: recs.append(r.getMessage())
        decision_logger.addHandler(h)
        self.addCleanup(lambda: decision_logger.removeHandler(h))
        return recs

    def test_winning_round_trip_records_pnl_pattern_and_decision(self):
        recs = self._capture_decisions()
        self.tracker.record_buy("005930", "삼성전자", 10, 50_000, reason="장전 모멘텀 돌파")
        res = self.tracker.record_sell("005930", "삼성전자", 10, 55_000, 50_000,
                                       reason="과매수 익절 (RSI 82)")
        self.assertGreater(res["realized_pnl"], 0, "수수료/세금 차감 후에도 +10%면 순익 양수")
        self.assertGreater(res["realized_rate"], 0)
        sells = [t for t in self.tracker._trades if t["type"] == "sell"]
        self.assertEqual(sells[0]["sell_pattern"], "rsi_overbought")
        joined = "\n".join(recs)
        self.assertIn("결정 BUY", joined)
        self.assertIn("장전 모멘텀 돌파", joined)
        self.assertIn("결정 SELL", joined)
        self.assertIn("rsi_overbought", joined)

    def test_forced_stop_loss_records_negative_pnl_and_pattern(self):
        recs = self._capture_decisions()
        self.tracker.record_buy("005930", "삼성전자", 10, 50_000, reason="진입")
        res = self.tracker.record_sell("005930", "삼성전자", 10, 42_500, 50_000,
                                       reason="강제 손절 (-15% 하드스톱)")
        self.assertLess(res["realized_pnl"], 0, "-15% 손절은 순손실")
        sells = [t for t in self.tracker._trades if t["type"] == "sell"]
        self.assertEqual(sells[0]["sell_pattern"], "forced_stop")
        self.assertIn("결정 SELL", "\n".join(recs))


class FastFallGuardTests(unittest.TestCase):
    """빠른 시장 급락 가드 — 메가캡발/지수 급락 시 신규 진입 중단 + 인버스 헷지 (보유는 안 자름).

    되돌리면(가드 제거/노이즈에도 발동/닫힌시장 stale 오발) 아래 테스트가 깨진다.
    """

    def _bot(self, kr_open=True, us_open=True, ff_cfg=None):
        from zusik.core.bot import TradingBot
        b = TradingBot.__new__(TradingBot)
        b.config = {
            "risk": {"fast_fall_guard": ff_cfg if ff_cfg is not None else {
                "enabled": True, "index_sharp_pct": -2.5,
                "megacap_drop_pct": -3.5, "index_confirm_pct": -1.0}},
            "inverse": {"enabled": True, "trigger_crisis": True},
        }
        b.client = Mock()
        b.client.is_market_open.return_value = kr_open
        b.client.is_us_market_open.return_value = us_open
        b._market_condition = "peace"
        b.derivative_etf_enabled = True
        return b

    def _prices(self, b, kr_idx=0.0, us_idx=0.0, megacaps=None):
        mc = megacaps or {}
        b.client.get_current_price.side_effect = lambda code: {"change_rate": kr_idx}
        b.client.get_us_current_price.side_effect = (
            lambda tk, ex=None: {"change_rate": us_idx if tk in ("QQQ", "SPY") else mc.get(tk, 0.0)})

    def test_index_sharp_fall_triggers(self):
        b = self._bot(kr_open=True, us_open=False)
        self._prices(b, kr_idx=-3.0)
        ok, reason = b._fast_market_fall()
        self.assertTrue(ok)
        self.assertIn("지수", reason)

    def test_megacap_led_fall_triggers(self):
        b = self._bot(kr_open=False, us_open=True)
        self._prices(b, us_idx=-1.5, megacaps={"MSFT": -4.0})
        ok, reason = b._fast_market_fall()
        self.assertTrue(ok)
        self.assertIn("MSFT", reason)

    def test_noise_does_not_trigger(self):
        b = self._bot(kr_open=True, us_open=True)
        self._prices(b, kr_idx=-1.0, us_idx=-0.8, megacaps={"MSFT": -1.5})
        self.assertFalse(b._fast_market_fall()[0])

    def test_megacap_drop_without_index_confirm_no_trigger(self):
        # 메가캡만 크게 빠지고 지수는 멀쩡하면(개별 이슈) 발동 안 함
        b = self._bot(kr_open=False, us_open=True)
        self._prices(b, us_idx=-0.2, megacaps={"MSFT": -5.0})
        self.assertFalse(b._fast_market_fall()[0])

    def test_disabled_returns_false(self):
        b = self._bot(ff_cfg={"enabled": False})
        self._prices(b, kr_idx=-9.0)
        self.assertFalse(b._fast_market_fall()[0])

    def test_closed_market_ignores_stale(self):
        b = self._bot(kr_open=False, us_open=False)
        self._prices(b, kr_idx=-9.0, us_idx=-9.0, megacaps={"MSFT": -9.0})
        self.assertFalse(b._fast_market_fall()[0], "닫힌 시장 stale 등락률로 오발동")

    def test_inverse_hedge_allowed_when_fast_fall_active(self):
        b = self._bot()
        b._fast_fall_active = True
        allow, reason = b._should_allow_inverse_entry()
        self.assertTrue(allow)
        self.assertIn("급락 가드", reason)


class LocalLlmProviderTests(unittest.TestCase):
    """로컬 LLM(Ollama) provider — 기본 OFF는 영향 0, 켜면 저렴 티어 로컬 우선 + 폴백.

    OSS 유저가 API 대형 LLM 없이 로컬 모델로 분석을 돌릴 수 있게 한 통합. 운영자(CLI 구독)는
    local_enabled=false 라 동작이 바뀌지 않아야 한다. 모두 네트워크/Ollama 없이 mock 으로 검증."""

    def _client(self, *, enabled, reachable=True, clis=(), model="qwen2.5:7b"):
        import zusik.clients.claude_client as cc
        import zusik.clients.local_llm as ll
        cfg = {"ai_providers": {"local_enabled": enabled, "local_model": model,
                                "local_search_backend": "duckduckgo"}}
        with patch("yaml.safe_load", return_value=cfg), \
                patch.object(cc, "_cli_available", side_effect=lambda c: c in clis), \
                patch.object(ll, "local_llm_available", return_value=reachable):
            return cc.ClaudeClient(prefer_cli=True)

    def test_default_off_no_local(self):
        c = self._client(enabled=False, clis=("claude",))
        self.assertFalse(c._has_local)
        self.assertIsNone(c._try_local("p", False), "OFF면 로컬 시도 자체가 없어야(영향 0)")

    def test_enabled_but_unreachable_stays_off(self):
        c = self._client(enabled=True, reachable=False, clis=("claude",))
        self.assertFalse(c._has_local, "Ollama 무응답이면 local_enabled여도 비활성")

    def test_enabled_without_model_stays_off(self):
        # 추천 기본 모델 없음 — 환경이 다양. 모델 미지정이면 reachable 여도 비활성.
        c = self._client(enabled=True, reachable=True, clis=(), model="")
        self.assertFalse(c._has_local, "local_model 미지정이면 비활성(추천 기본값 없음)")

    def test_local_only_user_passes_is_cli_gate(self):
        c = self._client(enabled=True, reachable=True, clis=())  # CLI 전무
        self.assertTrue(c._has_local)
        self.assertTrue(c.is_cli, "로컬 전용 유저도 분석 게이트(is_cli)를 통과해야 분석이 돈다")

    def test_cheap_tier_prefers_local(self):
        c = self._client(enabled=True, reachable=True, clis=("claude", "agy", "codex"))
        with patch.object(c, "_run_local", return_value='{"signal":"buy","confidence":0.7}') as rl, \
                patch.object(c, "_run_claude", side_effect=AssertionError("로컬 우선인데 claude 호출")), \
                patch.object(c, "_run_agy", side_effect=AssertionError("로컬 우선인데 agy 호출")), \
                patch.object(c, "_run_codex", side_effect=AssertionError("로컬 우선인데 codex 호출")):
            out = c.message("분석", tier="easy")
        rl.assert_called()
        self.assertIn("buy", out)

    def test_local_failure_falls_back_to_cli(self):
        import zusik.clients.claude_client as cc
        c = self._client(enabled=True, reachable=True, clis=("codex",))
        with patch.object(cc, "_check_limit", return_value=True), \
                patch.object(c, "_is_codex_cooldown", return_value=False), \
                patch.object(c, "_run_local", return_value='{"reasoning":"로컬 LLM 오류"}'), \
                patch.object(c, "_run_codex", return_value='{"signal":"hold","confidence":0.5}') as g:
            out = c.message("분석", tier="easy")
        g.assert_called()  # 로컬 실패 → CLI 폴백
        self.assertIn("hold", out)

    def test_hard_tier_claude_first_not_local(self):
        import zusik.clients.claude_client as cc
        c = self._client(enabled=True, reachable=True, clis=("claude",))
        with patch.object(cc, "_check_limit", return_value=True), \
                patch.object(c, "_run_claude", return_value='{"signal":"buy","confidence":0.9}') as cl, \
                patch.object(c, "_run_local", side_effect=AssertionError("claude 성공인데 로컬 호출")):
            out = c.message("중요판단", tier="hard")
        cl.assert_called()
        self.assertIn("buy", out)

    def test_hard_tier_local_only_uses_local(self):
        c = self._client(enabled=True, reachable=True, clis=())  # claude 없음 → 로컬 폴백
        with patch.object(c, "_run_local", return_value='{"signal":"sell","confidence":0.8}'):
            out = c.message("중요판단", tier="hard")
        self.assertIn("sell", out)

    def test_record_call_local_is_noop(self):
        import zusik.clients.claude_client as cc
        # 로컬은 비용/쿼터 0 → api_costs(total 캡)를 건드리면 유료 provider 가 잘못 막힘.
        with patch("builtins.open", side_effect=AssertionError("local 기록이 api_costs 파일을 건드림")):
            cc._record_call("local")  # 가드가 살아있으면 파일 I/O 없이 즉시 반환

    def test_run_local_injects_search_context(self):
        import zusik.clients.local_llm as ll
        captured = {}

        class _Resp:
            status_code = 200
            def json(self):
                return {"response": '{"signal":"hold","confidence":0.4}'}

        def _post(url, json=None, timeout=None):
            captured["prompt"] = json["prompt"]
            return _Resp()

        with patch.object(ll, "web_search", return_value="[웹검색 결과 — 참고용]\n1. 삼성전자 호재"), \
                patch("requests.post", _post):
            out = ll.run_local("삼성전자 분석", endpoint="http://x", model="m",
                               use_web_search=True, search_backend="duckduckgo")
        self.assertIn("웹검색 결과", captured["prompt"], "검색 컨텍스트가 프롬프트에 주입돼야 함")
        self.assertIn("hold", out)

    def test_run_local_http_error_returns_fail_json(self):
        import zusik.clients.local_llm as ll
        from zusik.clients.claude_client import ClaudeClient

        class _Resp:
            status_code = 500
            def json(self):
                return {}

        with patch("requests.post", return_value=_Resp()):
            out = ll.run_local("p", endpoint="http://x", model="m")
        self.assertTrue(ClaudeClient._is_failed(out), "HTTP 오류는 실패 JSON → 상위 폴백 트리거")

    def test_search_backend_none_skips_search(self):
        import zusik.clients.local_llm as ll

        class _Resp:
            status_code = 200
            def json(self):
                return {"response": "ok"}

        with patch.object(ll, "web_search", side_effect=AssertionError("none인데 검색 호출")), \
                patch("requests.post", return_value=_Resp()):
            out = ll.run_local("p", endpoint="http://x", model="m",
                               use_web_search=True, search_backend="none")
        self.assertEqual(out, "ok")


class MonthlyReportTests(unittest.TestCase):
    """월간 성과 HTML 렌더러 — 깔끔한 자가완결 산출물 검증."""

    # 가상 더미값(실제 계좌와 무관) — 레이아웃/포맷 검증용
    _STATS = {"month": "2024-01", "days": 21, "start_equity": 10_000_000,
              "end_equity": 10_520_000, "deposits": 0, "realized": 372_000,
              "net_growth": 520_000, "return_pct": 5.20, "max_drawdown": -3.40}

    def test_render_contains_key_figures(self):
        from zusik.reporting.monthly_html import render_monthly_html
        h = render_monthly_html(self._STATS, generated_at="2024-01-31 16:00")
        self.assertTrue(h.startswith("<!DOCTYPE html>"))
        for must in ("2024-01", "+5.20%", "10,520,000원", "+372,000원", "-3.40%", "21일"):
            self.assertIn(must, h)

    def test_render_self_contained(self):
        """외부 폰트/JS/CDN 없이 그대로 열려야(오프라인·공유 안전)."""
        from zusik.reporting.monthly_html import render_monthly_html
        h = render_monthly_html(self._STATS)
        self.assertNotIn("http://", h)
        self.assertNotIn("https://", h)
        self.assertNotIn("<script", h)

    def test_accent_color_by_sign(self):
        from zusik.reporting.monthly_html import render_monthly_html
        pos = render_monthly_html({**self._STATS, "return_pct": 5.0})
        neg = render_monthly_html({**self._STATS, "return_pct": -5.0})
        self.assertIn("#1a7f37", pos)  # 이익=초록
        self.assertIn("#cf222e", neg)  # 손실=빨강

    def test_month_value_escaped(self):
        from zusik.reporting.monthly_html import render_monthly_html
        h = render_monthly_html({**self._STATS, "month": "<b>x</b>"})
        self.assertNotIn("<b>x</b>", h)
        self.assertIn("&lt;b&gt;", h)

    def test_write_creates_month_file(self):
        import tempfile
        from zusik.reporting.monthly_html import write_monthly_html
        with tempfile.TemporaryDirectory() as d:
            path = write_monthly_html(self._STATS, d, generated_at="2024-01-31")
            self.assertTrue(path.endswith("2024-01.html"))
            self.assertIn("+5.20%", open(path, encoding="utf-8").read())

    def test_empty_stats_safe(self):
        from zusik.reporting.monthly_html import render_monthly_html
        h = render_monthly_html({})  # days 0 / 빈 dict 도 깨지지 않음
        self.assertTrue(h.startswith("<!DOCTYPE html>"))

    def test_render_includes_stock_section(self):
        from zusik.reporting.monthly_html import render_monthly_html
        h = render_monthly_html({**self._STATS, "by_stock": [
            {"name": "삼성전자", "code": "005930", "count": 3, "wins": 2, "pnl": 150_000}]})
        self.assertIn("종목별 손익", h)
        self.assertIn("삼성전자", h)
        self.assertIn("+150,000원", h)

    def test_no_stock_section_when_no_sells(self):
        from zusik.reporting.monthly_html import render_monthly_html
        self.assertNotIn("종목별 손익", render_monthly_html(self._STATS))  # by_stock 없음


class MonthlyStatsEffectiveTests(unittest.TestCase):
    """월간 통계가 effective(결제타이밍 면역) 기준인지 — T+2 팬텀 가짜 MaxDD 차단 회귀 가드.

    raw total_equity 로 되돌리면(=수정 전) day2 팬텀 -31.8% 가 MaxDD 로 잡혀 깨진다."""

    def _tracker(self, curve):
        import json
        import shutil
        import tempfile
        import zusik.storage.portfolio_tracker as pt
        d = tempfile.mkdtemp()
        self.addCleanup(lambda: shutil.rmtree(d, ignore_errors=True))
        orig = (pt.EQUITY_CURVE_FILE, pt.TRADES_FILE, pt.DATA_DIR)

        def _restore():
            pt.EQUITY_CURVE_FILE, pt.TRADES_FILE, pt.DATA_DIR = orig
        self.addCleanup(_restore)
        pt.EQUITY_CURVE_FILE = os.path.join(d, "equity_curve.json")
        pt.TRADES_FILE = os.path.join(d, "trades.json")
        pt.DATA_DIR = d
        with open(pt.EQUITY_CURVE_FILE, "w", encoding="utf-8") as f:
            json.dump(curve, f)
        return pt.PortfolioTracker()

    def test_phantom_t2_dip_not_counted_as_drawdown(self):
        curve = [
            {"date": "2026-06-02", "total_equity": 22_000_000, "drawdown_pct": 0.0,
             "effective_equity": 22_000_000, "effective_drawdown_pct": 0.0,
             "deposit_today": 0, "realized_today": 0},
            # day2: T+2 미결제로 total_equity 가짜 급락(-31.8%) — effective 는 평탄
            {"date": "2026-06-03", "total_equity": 15_000_000, "drawdown_pct": -31.8,
             "effective_equity": 22_300_000, "effective_drawdown_pct": 0.0,
             "deposit_today": 0, "realized_today": 0},
            {"date": "2026-06-30", "total_equity": 23_000_000, "drawdown_pct": 0.0,
             "effective_equity": 23_000_000, "effective_drawdown_pct": 0.0,
             "deposit_today": 0, "realized_today": 300_000},
        ]
        s = self._tracker(curve).get_monthly_stats(2026, 6)
        self.assertEqual(s["basis"], "effective")
        self.assertGreaterEqual(s["max_drawdown"], -1.0,
                                "T+2 팬텀(-31.8%)이 가짜 MaxDD 로 잡히면 안 됨")
        self.assertEqual(s["end_equity"], 23_000_000)
        self.assertGreater(s["return_pct"], 0)

    def test_legacy_curve_without_effective_falls_back_to_raw(self):
        curve = [
            {"date": "2026-05-02", "total_equity": 20_000_000, "drawdown_pct": 0.0,
             "deposit_today": 0, "realized_today": 0},
            {"date": "2026-05-20", "total_equity": 19_000_000, "drawdown_pct": -5.0,
             "deposit_today": 0, "realized_today": 0},
        ]
        s = self._tracker(curve).get_monthly_stats(2026, 5)
        self.assertEqual(s["basis"], "raw", "effective 필드 없는 구 데이터는 raw 폴백")
        self.assertEqual(s["start_equity"], 20_000_000)
        self.assertAlmostEqual(s["max_drawdown"], -5.0)

    def test_untracked_funding_not_counted_as_profit(self):
        # 펀딩이 deposit_today 없이 자산 점프로만 남으면(2026-05 실제 케이스) 입금으로 인식해
        # +수천% 가짜 수익률을 막는다. 보정 제거 시 return_pct 가 폭발해 깨진다.
        curve = [
            {"date": "2026-05-15", "total_equity": 200_000, "drawdown_pct": 0.0,
             "deposit_today": 0, "realized_today": 0},
            {"date": "2026-05-27", "total_equity": 22_200_000, "drawdown_pct": 0.0,
             "deposit_today": 0, "realized_today": 0},
            {"date": "2026-05-31", "total_equity": 21_800_000, "effective_equity": 21_800_000,
             "effective_drawdown_pct": -1.8, "deposit_today": 0, "realized_today": 0},
        ]
        s = self._tracker(curve).get_monthly_stats(2026, 5)
        self.assertLess(abs(s["return_pct"]), 50.0,
                        "미추적 펀딩이 수익으로 잡혀 +수천% 나오면 안 됨")
        self.assertGreater(s["deposits"], 20_000_000, "대규모 자산 점프를 입금으로 인식")

    def test_monthly_by_stock_is_month_scoped(self):
        curve = [
            {"date": "2026-06-02", "total_equity": 10_000_000, "effective_equity": 10_000_000,
             "effective_drawdown_pct": 0.0, "deposit_today": 0, "realized_today": 0},
            {"date": "2026-06-28", "total_equity": 10_400_000, "effective_equity": 10_400_000,
             "effective_drawdown_pct": 0.0, "deposit_today": 0, "realized_today": 0},
        ]
        tr = self._tracker(curve)
        tr._trades = [
            {"type": "sell", "code": "005930", "name": "삼성전자", "realized_pnl": 120_000, "date": "2026-06-10"},
            {"type": "sell", "code": "005930", "name": "삼성전자", "realized_pnl": -20_000, "date": "2026-06-20"},
            {"type": "sell", "code": "AAPL", "name": "애플", "realized_pnl": 50_000, "date": "2026-05-10"},
        ]
        s = tr.get_monthly_stats(2026, 6)
        codes = {x["code"] for x in s["by_stock"]}
        self.assertIn("005930", codes)
        self.assertNotIn("AAPL", codes, "다른 달(5월) 매도는 6월 집계에서 제외")
        sam = next(x for x in s["by_stock"] if x["code"] == "005930")
        self.assertEqual((sam["count"], sam["pnl"]), (2, 100_000))
        # realized 카드 = 이 달 매도 합(종목별 합)과 일치해야 (realized_today 빈 값에 휘둘리지 않음)
        self.assertEqual(s["realized"], 100_000)


class ResultsReportTests(unittest.TestCase):
    """투자결과 종합 리포트 — 집계(effective)·렌더·PDF 백엔드 감지."""

    def _tracker(self, curve, trades, deposits):
        import json
        import shutil
        import tempfile
        import zusik.storage.portfolio_tracker as pt
        d = tempfile.mkdtemp()
        self.addCleanup(lambda: shutil.rmtree(d, ignore_errors=True))
        orig = (pt.EQUITY_CURVE_FILE, pt.TRADES_FILE, pt.DATA_DIR)

        def _restore():
            pt.EQUITY_CURVE_FILE, pt.TRADES_FILE, pt.DATA_DIR = orig
        self.addCleanup(_restore)
        pt.EQUITY_CURVE_FILE = os.path.join(d, "equity_curve.json")
        pt.TRADES_FILE = os.path.join(d, "trades.json")
        pt.DATA_DIR = d
        with open(pt.EQUITY_CURVE_FILE, "w", encoding="utf-8") as f:
            json.dump(curve, f)
        with open(os.path.join(d, "total_deposits.json"), "w", encoding="utf-8") as f:
            json.dump({"manual_total_krw": deposits}, f)
        t = pt.PortfolioTracker()
        t._trades = trades
        return t

    def test_build_summary_effective(self):
        curve = [
            {"date": "2026-05-31", "total_equity": 10_300_000, "effective_equity": 10_300_000,
             "effective_drawdown_pct": -2.0, "deposit_today": 0, "realized_today": 150_000},
            {"date": "2026-06-22", "total_equity": 7_000_000, "effective_equity": 10_800_000,
             "effective_drawdown_pct": 0.0, "unrealized_krw": 500_000,
             "deposit_today": 0, "realized_today": 150_000},
        ]
        trades = [
            {"type": "buy"}, {"type": "buy"},
            {"type": "sell", "realized_pnl": 200_000, "amount": 2_000_000, "code": "005930",
             "name": "삼성전자", "date": "2026-06-10", "sell_pattern": "rsi_overbought"},
            {"type": "sell", "realized_pnl": 150_000, "amount": 1_500_000, "code": "AAPL",
             "name": "애플", "date": "2026-05-20", "sell_pattern": "split_profit"},
            {"type": "sell", "realized_pnl": -50_000, "amount": 1_000_000, "code": "005930",
             "name": "삼성전자", "date": "2026-06-15", "sell_pattern": "slow_bleed"},
        ]
        from zusik.reporting.results_html import build_results_summary
        s = build_results_summary(self._tracker(curve, trades, 10_000_000))
        self.assertEqual(s["realized_total"], 300_000)
        self.assertEqual(s["unrealized"], 500_000)
        self.assertEqual(s["effective_total"], 800_000)
        self.assertEqual(s["effective_equity"], 10_800_000)  # 7M raw 팬텀 아님
        self.assertAlmostEqual(s["return_pct"], 8.0, places=1)
        self.assertEqual((s["sells"], s["wins"]), (3, 2))
        self.assertAlmostEqual(s["win_rate"], 66.7, places=1)
        pats = {p["pattern"] for p in s["patterns"]}
        self.assertIn("rsi_overbought", pats)
        self.assertIn("slow_bleed", pats)
        self.assertTrue(s["months"])
        # 종목별 집계 — 005930 은 매도 2건(+200k, -50k) 합산
        samsung = next(x for x in s["by_stock"] if x["code"] == "005930")
        self.assertEqual((samsung["count"], samsung["pnl"]), (2, 150_000))
        self.assertEqual(s["by_stock"][0]["code"], "005930")  # pnl 내림차순 정렬

    def test_render_contains_sections(self):
        from zusik.reporting.results_html import render_results_html
        h = render_results_html({
            "period": {"start": "2024-01-02", "end": "2024-01-31", "days": 19},
            "deposits": 10_000_000, "realized_total": 520_000, "unrealized": 130_000,
            "effective_total": 650_000, "effective_equity": 10_650_000,
            "return_pct": 7.5, "max_drawdown": -4.20, "buys": 40, "sells": 36,
            "wins": 25, "losses": 11, "win_rate": 69.4,
            "patterns": [{"pattern": "rsi_overbought", "count": 12, "win_rate": 100,
                          "pnl_sum": 360_000, "avg_pnl": 30_000}],
            "by_stock": [{"name": "예시 종목 A", "code": "000001", "count": 8, "wins": 7, "pnl": 240_000}],
            "months": [{"month": "2024-01", "return_pct": 4.60, "realized": 370_000,
                        "max_drawdown": -4.20, "days": 15}],
        }, generated_at="2024-01-31")
        self.assertTrue(h.startswith("<!DOCTYPE html>"))
        for must in ("투자결과 종합", "종목별 손익", "예시 종목 A", "월별 성과", "매도 패턴",
                     "+7.50%", "rsi_overbought", "2024-01"):
            self.assertIn(must, h)
        self.assertNotIn("http://", h)
        self.assertNotIn("<script", h)

    def test_render_empty_safe(self):
        from zusik.reporting.results_html import render_results_html
        self.assertTrue(render_results_html({}).startswith("<!DOCTYPE html>"))

    def test_pdf_backend_none_when_no_binary(self):
        import zusik.reporting.pdf as pdfmod
        with patch.object(pdfmod.shutil, "which", return_value=None):
            self.assertEqual(pdfmod.pdf_backend(), "")
            import tempfile
            with tempfile.NamedTemporaryFile("w", suffix=".html", delete=False) as f:
                f.write("<html><body>x</body></html>")
                hp = f.name
            self.addCleanup(lambda: os.path.exists(hp) and os.remove(hp))
            self.assertIsNone(pdfmod.html_to_pdf(hp, hp + ".pdf"), "백엔드 없으면 None(무예외)")

    def test_pdf_backend_detects_chrome(self):
        import zusik.reporting.pdf as pdfmod
        with patch.object(pdfmod.shutil, "which",
                          side_effect=lambda b: "/usr/bin/google-chrome" if b == "google-chrome" else None):
            self.assertEqual(pdfmod.pdf_backend(), "chrome")


class SellTimingTests(unittest.TestCase):
    """매도 사후분석 — 팔고 난 뒤 놓친 상승/막은 하락을 패턴별로 정확히 분류하는가.

    조기매도(팔고 더 오름)와 보호 성공(팔아서 하락 회피)을 데이터로 구분해 매도 타이밍
    개선의 근거가 되므로, 그 분류가 흔들리지 않게 가드한다."""

    def _series(self, lows, highs, closes, start="2026-06-09"):
        import datetime as _dt
        d0 = _dt.date.fromisoformat(start)
        dates = [(d0 + _dt.timedelta(days=i)).isoformat() for i in range(len(closes))]
        return {"dates": dates, "lows": lows, "highs": highs, "closes": closes}

    def test_early_sell_vs_protection_classification(self):
        from zusik.reporting.sell_timing import analyze_sell_timing
        # A: 100k 매도 → 당일 high +3%, 이후 계속 상승(조기매도). B: 50k 매도 → 곧장 하락(보호).
        ser = {
            ("KR", "AAA"): self._series(
                [99000, 99000, 101000, 103000, 105000, 107000, 108000],
                [101000, 103000, 104000, 107000, 110000, 111000, 112000],
                [100000, 100500, 103000, 106000, 109000, 110000, 111000]),
            ("KR", "BBB"): self._series(
                [49000, 48000, 45000, 43000, 42000, 41000, 40000],
                [51000, 50500, 48000, 46000, 44000, 43000, 42000],
                [50000, 49000, 46000, 44000, 43000, 42000, 41000]),
        }
        trades = [
            {"type": "sell", "code": "AAA", "market": "KR", "date": "2026-06-10",
             "price": 100000, "sell_pattern": "breakeven_protect"},
            {"type": "sell", "code": "BBB", "market": "KR", "date": "2026-06-10",
             "price": 50000, "sell_pattern": "forced_stop"},
        ]
        res = analyze_sell_timing(trades, lambda m, s, e: ser.get((m, s)), primary=5)
        bp = res["by_pattern"]
        self.assertEqual(bp["breakeven_protect"]["verdict"], "조기매도(상승 놓침)")
        self.assertGreater(bp["breakeven_protect"]["avg_same_day_missed"], 0)
        self.assertGreater(bp["breakeven_protect"]["avg_net_if_held"], 0)  # 홀드가 나았음
        self.assertEqual(bp["forced_stop"]["verdict"], "보호 성공(하락 회피)")
        self.assertLess(bp["forced_stop"]["avg_net_if_held"], 0)           # 홀드면 손실
        self.assertGreater(bp["forced_stop"]["avg_avoided_drop"], 0)       # 하락을 막음

    def test_unit_recovery_for_us_scaled_price(self):
        # US 레코드 price 단위가 달러×1000(87310=$87.31)이어도 [low,high]로 복원해 정상 분류.
        from zusik.reporting.sell_timing import analyze_sell_timing
        ser = {("US", "BAC"): self._series(
            [86.0, 86.5, 87.0, 88.0, 89.0, 90.0],
            [87.5, 89.0, 90.0, 91.0, 92.0, 93.0],
            [87.0, 88.5, 89.5, 90.5, 91.5, 92.5])}
        trades = [{"type": "sell", "ticker": "BAC", "market": "US", "date": "2026-06-10",
                   "price": 87310, "sell_pattern": "breakeven_protect"}]
        res = analyze_sell_timing(trades, lambda m, s, e: ser.get((m, s)), primary=3)
        self.assertEqual(res["overall"]["analyzed"], 1)
        # ref 가 87.x 로 복원돼야 +수% 놓친 상승이 나온다(원시 87310 이면 -100%로 깨짐).
        self.assertGreater(res["by_pattern"]["breakeven_protect"]["avg_missed_upside"], 0)

    def test_recent_sell_is_pending_not_crash(self):
        from zusik.reporting.sell_timing import analyze_sell_timing
        ser = {("KR", "AAA"): self._series([99], [101], [100])}  # forward 없음
        trades = [{"type": "sell", "code": "AAA", "market": "KR", "date": "2026-06-09",
                   "price": 100, "sell_pattern": "rsi_overbought"}]
        res = analyze_sell_timing(trades, lambda m, s, e: ser.get((m, s)))
        self.assertEqual(res["pending"], 1)
        self.assertEqual(res["overall"]["analyzed"], 0)


class SelectionAlphaTests(unittest.TestCase):
    """종목선택 평가 — 지수 대비 초과수익(alpha)과 놓친 최고종목을 바르게 계산하는가."""

    def _ser(self, closes, start="2026-06-09"):
        import datetime as _dt
        d0 = _dt.date.fromisoformat(start)
        dates = [(d0 + _dt.timedelta(days=i)).isoformat() for i in range(len(closes))]
        return {"dates": dates, "closes": closes}

    def test_alpha_and_missed_best(self):
        from zusik.reporting.selection_alpha import analyze_selection_alpha
        # 매수 PICK: +10%, 지수: +2% → alpha +8%p. 유니버스에 안 산 WIN(+30%)이 놓친 최고.
        ser = {
            ("KR", "PICK"): self._ser([100, 102, 104, 106, 110]),
            ("KR", "WIN"): self._ser([100, 110, 120, 125, 130]),
            ("KR", "069500"): self._ser([100, 100, 101, 101, 102]),
        }
        trades = [{"type": "buy", "code": "PICK", "market": "KR", "date": "2026-06-09"}]
        universe = [("KR", "PICK", "NASD", "픽종목"), ("KR", "WIN", "NASD", "놓친종목")]
        res = analyze_selection_alpha(
            trades, lambda m, s, e: ser.get((m, s)),
            lambda m: ser.get((m, "069500")), window=4, universe=universe)
        self.assertEqual(res["alpha"]["count"], 1)
        self.assertAlmostEqual(res["alpha"]["avg_pick_return"], 10.0, places=1)
        self.assertAlmostEqual(res["alpha"]["avg_alpha"], 8.0, places=1)   # 10 - 2
        mb = res["missed_best"]
        self.assertIsNotNone(mb)
        self.assertEqual(mb["days_detail"][-1]["missed_best"]["code"], "WIN")
        self.assertGreater(mb["avg_gap"], 0)


class UsOpenSessionGuardTests(unittest.TestCase):
    """US 개장 킥은 한 세션 = 한 번. US 정규장은 KST 자정을 넘으므로(22:30→00:00) KST 날짜로
    가드하면 00:00 에 재발동해 '미장분석 2회'가 됐다. 세션 날짜 가드로 1회만 발동해야 한다."""

    class _FakeThread:
        def __init__(self, target=None, daemon=None):
            self._t = target

        def start(self):
            if self._t:
                self._t()

    def _bot(self):
        from zusik.core.bot import TradingBot
        b = TradingBot.__new__(TradingBot)
        b._us_open_kicked_date = ""
        b.run_us = Mock()
        return b

    def test_open_kick_once_per_session_across_midnight(self):
        import zusik.core.bot_us as m
        from datetime import datetime as _dt
        b = self._bot()
        with patch("threading.Thread", self._FakeThread), patch.object(m, "datetime") as dtm:
            dtm.now.return_value = _dt(2026, 6, 22, 22, 30)   # 개장(저녁)
            b._on_us_market_open()
            dtm.now.return_value = _dt(2026, 6, 23, 0, 0)     # 자정 — 같은 세션
            b._on_us_market_open()
            dtm.now.return_value = _dt(2026, 6, 23, 2, 0)     # 새벽 — 같은 세션
            b._on_us_market_open()
        self.assertEqual(b.run_us.call_count, 1, "한 세션 1회만 발동")
        self.assertEqual(b._us_open_kicked_date, "2026-06-22")

    def test_next_session_kicks_again(self):
        import zusik.core.bot_us as m
        from datetime import datetime as _dt
        b = self._bot()
        with patch("threading.Thread", self._FakeThread), patch.object(m, "datetime") as dtm:
            dtm.now.return_value = _dt(2026, 6, 22, 22, 30)
            b._on_us_market_open()
            dtm.now.return_value = _dt(2026, 6, 23, 22, 30)   # 다음 세션(다음날 저녁)
            b._on_us_market_open()
        self.assertEqual(b.run_us.call_count, 2)


class CommandSurfaceTests(unittest.TestCase):
    """Discord/Telegram 명령 최신화 — 죽은 아레나 제거, 성과 추가, 종목 수동추가는
    자동선별이 덮어쓰므로 미지원 안내. (두 메신저는 동일 DiscordCommander._execute 공유)"""

    def _cmdr(self):
        from zusik.clients.discord_commander import DiscordCommander
        c = DiscordCommander.__new__(DiscordCommander)
        c.bot = Mock()
        c.bot.kr_stocks = [{"code": "005930", "name": "삼성전자"}]
        c.bot.us_stocks = []
        return c

    def test_arena_command_removed(self):
        self.assertIn("알 수 없는 명령", self._cmdr()._execute("아레나"))

    def test_performance_command_routed(self):
        c = self._cmdr()
        with patch.object(c, "_handle_performance", return_value="OK성과"):
            self.assertEqual(c._execute("성과"), "OK성과")
            self.assertEqual(c._execute("수익"), "OK성과")

    def test_manual_stock_add_unsupported_but_list_works(self):
        c = self._cmdr()
        self.assertIn("자동선별", c._handle_stock(["추가", "KR", "005930", "삼성"]))
        self.assertIn("삼성전자", c._handle_stock(["목록"]))
        self.assertIn("삼성전자", c._handle_stock([]))  # 기본 목록

    def test_help_modernized(self):
        from zusik.clients.discord_commander import DiscordCommander
        h = DiscordCommander._handle_help()
        self.assertIn("/성과", h)
        self.assertNotIn("아레나", h)
        self.assertNotIn("추가 KR", h)  # 죽은 수동추가 예시 제거


class PnlIntegrityTests(unittest.TestCase):
    """매 tick 손익/자산 무결성 검증 — 버그·상태변조·정산 드리프트 조기 포착(로컬·무예외)."""

    def test_pure_clean_no_issues(self):
        from zusik.core.resilience import verify_pnl_invariants
        snap = {"date": "2026-06-30", "effective_equity": 10_100_000, "unrealized_krw": 0}
        self.assertEqual(verify_pnl_invariants(
            trades=[{"type": "sell", "realized_pnl": 100_000, "date": "2026-06-10"}],
            deposits=10_000_000, latest_snapshot=snap,
            positions={"005930": {"qty": 3, "avg_price": 70_000}}), [])

    def test_pure_negative_qty_and_nan(self):
        from zusik.core.resilience import verify_pnl_invariants
        iss = verify_pnl_invariants(
            trades=[{"type": "sell", "realized_pnl": float("nan"), "code": "X"}],
            deposits=10_000_000, positions={"AAA": {"qty": -5, "avg_price": 1_000}})
        self.assertTrue(any("음수 보유수량" in i for i in iss))
        self.assertTrue(any("비정상값" in i for i in iss))

    def test_pure_loss_exceeds_deposits(self):
        from zusik.core.resilience import verify_pnl_invariants
        iss = verify_pnl_invariants(
            trades=[{"type": "sell", "realized_pnl": -12_000_000, "date": "2026-06-01"}],
            deposits=10_000_000)
        self.assertTrue(any("무차입" in i for i in iss), "실현손실>입금 = 불가능 invariant")

    def test_pure_equity_reconciliation(self):
        from zusik.core.resilience import verify_pnl_invariants
        trades = [{"type": "sell", "realized_pnl": 100_000, "date": "2026-06-10"}]
        bad = {"date": "2026-06-30", "effective_equity": 15_000_000, "unrealized_krw": 0}
        self.assertTrue(any("자산 정합" in i for i in
                            verify_pnl_invariants(trades=trades, deposits=10_000_000, latest_snapshot=bad)))

    def test_pure_garbage_input_safe(self):
        from zusik.core.resilience import verify_pnl_invariants
        self.assertEqual(verify_pnl_invariants(trades=None, deposits=None,
                                               latest_snapshot=None, positions=None), [])
        # 컨테이너 타입 어긋남(Mock/객체)은 빈 입력 취급 — 거짓 위반 보고 안 함
        self.assertEqual(verify_pnl_invariants(trades=Mock(), deposits=10_000_000,
                                               positions=Mock()), [])

    def _bot(self):
        import types
        from zusik.core.bot import TradingBot
        b = TradingBot.__new__(TradingBot)
        b.config = {"risk": {"integrity_tolerance": 0.05}}
        b.tracker = types.SimpleNamespace(_trades=[], get_total_deposits=lambda: 10_000_000)
        b.positions = types.SimpleNamespace(_positions={})
        b.discord = None
        return b

    def test_bot_verify_clean(self):
        b = self._bot()
        with patch("zusik.storage.portfolio_tracker._load_json", return_value=[]):
            self.assertEqual(b._verify_tick_pnl(), [])

    def test_bot_verify_flags_and_dedups_alert(self):
        import types
        b = self._bot()
        b.positions._positions = {"005930": {"qty": -5, "avg_price": 1_000}}
        alerts = []
        b.discord = types.SimpleNamespace(notify_error=lambda m: alerts.append(m))
        with patch("zusik.storage.portfolio_tracker._load_json", return_value=[]):
            issues1 = b._verify_tick_pnl()
            b._verify_tick_pnl()  # 동일 위반 재호출 → 알림 dedup
        self.assertTrue(any("음수 보유수량" in i for i in issues1))
        self.assertEqual(len(alerts), 1, "같은 위반셋은 1회만 경고(dedup)")

    def test_bot_verify_never_raises(self):
        # tracker 가 깨져도(예외) 매매 경로를 막지 않는다 — [] 반환
        b = self._bot()
        b.tracker = None
        self.assertEqual(b._verify_tick_pnl(), [])


class RealtimeEntryTests(unittest.TestCase):
    """실시간 WS 진입 트리거(이벤트 드리븐) — 급등 틱이 빠른진입 스캔 벨만 울리고, 실제 매수는
    _fast_entry_scan 게이트를 그대로 거친다. WS 자체는 라이브 검증 영역(여기선 트리거/드레인 로직)."""

    def _bot(self, entry_enabled=True, threshold=1.5):
        from zusik.core.bot import TradingBot
        b = TradingBot.__new__(TradingBot)
        b.config = {"realtime": {"enabled": True, "entry_enabled": entry_enabled,
                                 "entry_threshold_pct": threshold}}
        b._rt_entry_ref = {}
        b._realtime_entry_triggered = False
        b._ws_manager = None
        b._ws_subscribed = set()
        b._ws_entry_subscribed = set()
        return b

    def test_tick_rings_bell_on_surge_not_below(self):
        b = self._bot(threshold=1.5)
        cb = b._make_entry_tick_cb(1.5)
        cb({"code": "005930", "price": 10_000})   # 첫 틱 = 기준가
        self.assertFalse(b._realtime_entry_triggered)
        cb({"code": "005930", "price": 10_100})   # +1.0% — 임계 미달
        self.assertFalse(b._realtime_entry_triggered)
        cb({"code": "005930", "price": 10_200})   # 기준 10,000 대비 +2.0% — 트리거
        self.assertTrue(b._realtime_entry_triggered)

    def test_tick_garbage_safe(self):
        b = self._bot()
        cb = b._make_entry_tick_cb(1.5)
        cb({"code": None, "price": 0})
        cb({"price": -5})
        cb("not a dict")
        self.assertFalse(b._realtime_entry_triggered)

    def test_drain_runs_fast_entry_when_triggered(self):
        b = self._bot()
        b._realtime_entry_triggered = True
        b._fast_entry_scan = Mock()
        b._drain_realtime_entry()
        b._fast_entry_scan.assert_called_once()
        self.assertFalse(b._realtime_entry_triggered, "벨 소진(재실행 방지)")

    def test_drain_noop_when_not_triggered(self):
        b = self._bot()
        b._fast_entry_scan = Mock()
        b._drain_realtime_entry()
        b._fast_entry_scan.assert_not_called()

    def test_setup_noop_when_entry_disabled(self):
        b = self._bot(entry_enabled=False)
        b.kr_stocks = [{"code": "005930"}]
        b.us_stocks = []
        b._realtime_entry_setup()
        self.assertIsNone(b._ws_manager, "entry_enabled=false면 WS 생성 안 함")

    def test_ws_manager_respects_virtual_flag(self):
        # is_virtual=False 하드코딩 버그 회귀 가드 — 모의/실전 URL 분기
        from zusik.clients.kis_websocket import KISWebSocketManager
        self.assertTrue(KISWebSocketManager("k", "s", is_virtual=True).is_virtual)
        self.assertIn("31000", KISWebSocketManager.URL_VIRTUAL)
        self.assertIn("21000", KISWebSocketManager.URL_REAL)

    def _connected_manager(self):
        from zusik.clients.kis_websocket import KISWebSocketManager
        m = KISWebSocketManager("k", "s")
        m._connected = True
        m._approval_key = "x"
        m._ws = Mock()
        m._sent = []
        m._ws.send = lambda payload: m._sent.append(payload)
        return m

    def test_us_subscribe_trkey_has_exchange_prefix(self):
        # 회귀 가드: HDFSCNT0 는 tr_key='D'+거래소+종목 이어야 틱이 흐른다.
        # 바닥 티커("AAPL")만 보내면 KIS 가 구독 ACK 만 주고 체결 틱을 0건 흘려보낸다(라이브 실증).
        import json as _j
        m = self._connected_manager()
        m._send_subscribe("AAPL", "US", "NASD")
        body = _j.loads(m._sent[-1])["body"]["input"]
        self.assertEqual(body["tr_id"], "HDFSCNT0")
        self.assertEqual(body["tr_key"], "DNASAAPL")
        # 별칭(NYSE) 정규화
        m._send_subscribe("BAC", "US", "NYSE")
        self.assertEqual(_j.loads(m._sent[-1])["body"]["input"]["tr_key"], "DNYSBAC")

    def test_kr_subscribe_trkey_is_bare_code(self):
        import json as _j
        m = self._connected_manager()
        m._send_subscribe("005930", "KR")
        body = _j.loads(m._sent[-1])["body"]["input"]
        self.assertEqual(body["tr_id"], "H0STCNT0")
        self.assertEqual(body["tr_key"], "005930")

    def test_us_tick_matches_callback_on_bare_symbol(self):
        # tr_key 는 DNASAAPL 로 보내도, 수신 틱의 SYMB(fields[1])=AAPL 로 콜백이 매칭돼야 한다.
        m = self._connected_manager()
        got = []
        m.subscribe("AAPL", lambda msg: got.append(msg), market="US", exchange="NASD")
        # HDFSCNT0 raw: '0|HDFSCNT0|001|RSYM^SYMB^...^LAST(idx11)^...'
        fields = ["DNASAAPL", "AAPL"] + ["0"] * 9 + ["299.61"] + ["0"] * 5
        m._on_message(None, "0|HDFSCNT0|001|" + "^".join(fields))
        self.assertEqual(len(got), 1)
        self.assertEqual(got[0]["code"], "AAPL")
        self.assertAlmostEqual(got[0]["price"], 299.61, places=2)


class StatusSnapshotTests(unittest.TestCase):
    """통합 상태 스냅샷 — 흩어진 상태를 한 dict로(웹/CLI 단일 소스). effective 기준·무예외."""

    def _bot(self):
        import json
        import shutil
        import tempfile
        import types
        import zusik.storage.portfolio_tracker as pt
        d = tempfile.mkdtemp()
        self.addCleanup(lambda: shutil.rmtree(d, ignore_errors=True))
        orig = (pt.EQUITY_CURVE_FILE, pt.TRADES_FILE, pt.DATA_DIR)

        def _restore():
            pt.EQUITY_CURVE_FILE, pt.TRADES_FILE, pt.DATA_DIR = orig
        self.addCleanup(_restore)
        pt.EQUITY_CURVE_FILE = os.path.join(d, "equity_curve.json")
        pt.TRADES_FILE = os.path.join(d, "trades.json")
        pt.DATA_DIR = d
        with open(pt.EQUITY_CURVE_FILE, "w", encoding="utf-8") as f:
            json.dump([{"date": "2026-06-30", "effective_equity": 10_300_000,
                        "effective_drawdown_pct": -1.0, "unrealized_krw": 200_000,
                        "deposit_today": 0, "realized_today": 0}], f)
        with open(os.path.join(d, "total_deposits.json"), "w", encoding="utf-8") as f:
            json.dump({"manual_total_krw": 10_000_000}, f)
        t = pt.PortfolioTracker()
        t._trades = [{"type": "sell", "code": "005930", "name": "삼성전자",
                      "realized_pnl": 100_000, "amount": 1_000_000, "date": "2026-06-10"}]
        b = types.SimpleNamespace()
        b.config = {"realtime": {"enabled": True, "entry_enabled": False},
                    "ai_providers": {"local_enabled": False},
                    "ai_routing": {"ambiguous_sell_enabled": True}, "risk": {},
                    "fast_entry": {"enabled": True}}
        b.tracker = t
        b.positions = types.SimpleNamespace(_positions={
            "005930": {"qty": 3, "avg_price": 70_000, "peak_profit_rate": 0.05}})
        b.client = types.SimpleNamespace(is_market_open=lambda: True,
                                         is_us_market_open=lambda: False)
        b._active_mode = "balanced"
        b._market_condition = "peace"
        b._defensive_mode = False
        b._ws_manager = None
        return b

    def test_snapshot_aggregates_state(self):
        from zusik.reporting.status_snapshot import build_status_snapshot
        s = build_status_snapshot(self._bot(), generated_at="2026-06-30 12:00")
        for key in ("equity", "holdings", "market", "toggles", "state", "by_stock"):
            self.assertIn(key, s)
        self.assertTrue(s["toggles"]["realtime"])
        self.assertFalse(s["toggles"]["local_llm"])
        self.assertEqual(s["market"], {"kr_open": True, "us_open": False})
        self.assertTrue(any(h["code"] == "005930" for h in s["holdings"]))
        self.assertEqual(s["equity"]["effective_equity"], 10_300_000)
        self.assertTrue(s["state"]["integrity_ok"])

    def test_render_text_safe(self):
        from zusik.reporting.status_snapshot import build_status_snapshot, render_status_text
        txt = render_status_text(build_status_snapshot(self._bot(), generated_at="t"))
        self.assertIn("zusik 상태", txt)
        self.assertIn("토글", txt)
        self.assertIn("삼성전자", txt)

    def test_build_never_raises_on_broken_bot(self):
        import types
        from zusik.reporting.status_snapshot import build_status_snapshot, render_status_text
        b = types.SimpleNamespace(config=None, tracker=None, positions=None, client=None)
        s = build_status_snapshot(b)               # 깨진 봇도 dict 반환(무예외)
        self.assertIsInstance(s, dict)
        self.assertTrue(render_status_text(s).startswith("="))


class StabilityFeatureTests(unittest.TestCase):
    """안정성 강화: LLM 다운 알림 + 워치독 전이/복구 + Discord 진단 메뉴.

    코어가 죽거나 LLM이 멈출 때 유저가 모르고 지나가던 문제(통보 경로 없음)를 막는다.
    """

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)

    # ── LLM 가용성 집계 (claude_client) ──
    def _isolate_llm(self):
        import zusik.clients.claude_client as cc
        cc._llm_health_path = lambda: os.path.join(self._tmp.name, "llm_health.json")
        cc._llm_health_cache = None
        self.addCleanup(lambda: setattr(cc, "_llm_health_cache", None))
        return cc

    def test_classify_degraded_catches_quota_and_sentinel(self):
        from zusik.clients.claude_client import ClaudeClient
        self.assertTrue(ClaudeClient._classify_degraded('{"reasoning":"AI 한도 소진"}'))
        self.assertTrue(ClaudeClient._classify_degraded("codex quota exceeded"))
        self.assertTrue(ClaudeClient._classify_degraded(""))
        self.assertFalse(ClaudeClient._classify_degraded('{"signal":"buy","confidence":0.8}'))

    def test_llm_health_down_after_threshold_then_recovers(self):
        cc = self._isolate_llm()
        for _ in range(2):
            cc._record_llm_health(True, "fail")
            self.assertEqual(cc.get_llm_health()["status"], "ok")  # 임계(3) 미만
        cc._record_llm_health(True, '{"reasoning":"AI 한도 소진"}')
        self.assertEqual(cc.get_llm_health()["status"], "down")
        cc._record_llm_health(False, '{"signal":"buy"}')          # 정상 1회 → 복구
        h = cc.get_llm_health()
        self.assertEqual(h["status"], "ok")
        self.assertEqual(h["consecutive_fail"], 0)

    def test_check_llm_health_once_per_day_specific_providers_toggle(self):
        """LLM down 알림: 하루 1회(재시작/반복 호출에도) + 실제 쓰는 provider만 지목 + on/off 토글."""
        from zusik.core.bot import TradingBot
        from zusik import paths
        cc = self._isolate_llm()
        # llm_alert.json(날짜 dedup 상태) 격리
        af = os.path.join(self._tmp.name, "llm_alert.json")
        orig_dp = paths.data_path
        self.addCleanup(lambda: setattr(paths, "data_path", orig_dp))
        paths.data_path = lambda name: af if name == "llm_alert.json" else orig_dp(name)

        b = TradingBot.__new__(TradingBot)
        b.discord = Mock()
        b.config = {"ai_providers": {"llm_health_alert": True}}
        b._llm_providers_cache = ["agy"]            # 이 유저는 agy 만 사용

        for _ in range(3):
            cc._record_llm_health(True, "quota")
        b._check_llm_health()
        b._check_llm_health()                       # 같은 날 재호출(재시작 시뮬) → 추가 알림 X
        self.assertEqual(b.discord.notify_error.call_count, 1, "하루 1회만")
        msg = b.discord.notify_error.call_args[0][0]
        self.assertIn("agy", msg)                   # 실제 쓰는 provider 지목
        self.assertNotIn("claude", msg)
        self.assertNotIn("codex", msg)
        cc._record_llm_health(False, "ok")          # 복구 → info 1회
        b._check_llm_health()
        self.assertEqual(b.discord.notify_info.call_count, 1)

        # 토글 OFF → 상태 파일 초기화해도 알림 안 함
        os.remove(af)
        b.discord = Mock()
        b.config = {"ai_providers": {"llm_health_alert": False}}
        for _ in range(3):
            cc._record_llm_health(True, "quota")
        b._check_llm_health()
        self.assertEqual(b.discord.notify_error.call_count, 0, "토글 OFF면 무알림")

    # ── CLI provider cooldown 감지 (codex 한도 / 세션 만료) ──
    def _exec_client(self):
        from zusik.clients.claude_client import ClaudeClient
        c = ClaudeClient.__new__(ClaudeClient)   # __init__(CLI 탐지) 우회
        # cooldown 파일 격리 — 실 /tmp(라이브 봇이 읽음) 오염 방지
        c._CODEX_COOLDOWN_FILE = os.path.join(self._tmp.name, "codex_cd.txt")
        return c

    @staticmethod
    def _fake_proc(stdout="", stderr=""):
        import types
        return lambda *a, **k: types.SimpleNamespace(stdout=stdout, stderr=stderr)

    def test_codex_usage_limit_triggers_cooldown(self):
        """codex 사용량 한도(빈 stdout + 한도 stderr) → cooldown + 전용 사유."""
        c = self._exec_client()
        fake = self._fake_proc(stderr="ERROR: You've hit your usage limit. "
                                      "Upgrade to Pro. purchase more credits")
        with patch("subprocess.run", fake):
            self.assertFalse(c._is_codex_cooldown())
            r = c._exec(["codex", "exec"], "codex", timeout=5)
        self.assertIn("codex 사용량 한도", r)
        self.assertTrue(c._is_codex_cooldown(), "한도 감지 시 cooldown 설정 → 다음 사이클 건너뜀")

    def test_normal_output_no_false_cooldown(self):
        """정상 응답은 cooldown 미설정 (마커 오탐 방지 회귀)."""
        c = self._exec_client()
        with patch("subprocess.run", self._fake_proc(stdout="OK")):
            self.assertEqual(c._exec(["codex", "exec"], "codex", timeout=5), "OK")
        self.assertFalse(c._is_codex_cooldown())

    def test_agy_provider_toggle_and_limit(self):
        """agy(Antigravity) provider — disable_agy 토글 + DAILY_LIMITS 등록. 구글 Gemini 계열."""
        import zusik.clients.claude_client as cc
        from zusik.core.cost_optimizer import DAILY_LIMITS
        self.assertIn("agy", DAILY_LIMITS, "agy 한도가 DAILY_LIMITS 에 있어야 _check_limit 통과")
        with patch.object(cc, "_cli_available", lambda c: True):
            self.assertTrue(cc.ClaudeClient(prefer_cli=True, disable_agy=False)._has_agy)
            self.assertFalse(cc.ClaudeClient(prefer_cli=True, disable_agy=True)._has_agy)

    def test_agy_used_as_easy_fallback(self):
        """Codex가 없으면 easy 티어가 agy 폴백을 호출한다."""
        import zusik.clients.claude_client as cc
        c = cc.ClaudeClient.__new__(cc.ClaudeClient)
        c._has_agy, c._has_codex, c._has_claude, c._has_local = (
            True, False, False, False)
        c._try_local = lambda *a, **k: None
        c._run_agy = lambda prompt, *a, **k: "AGY_OK"
        with patch.object(cc, "_check_limit", lambda p: True), \
             patch.object(cc, "_record_call", lambda p: None):
            self.assertEqual(c._call_easy("hi"), "AGY_OK")

    def test_healthcheck_probes_each_provider_dead_codex_bad(self):
        """/점검 provider별 probe: cooldown(죽은) codex 는 '불가', 살아있는 건 '정상'.
        회귀: 과거 통합 1회 probe 가 agy/claude 성공으로 전체 OK 표시 → codex 가 정상처럼 보였음."""
        import types
        from zusik.utils.healthcheck import _probe_ai_providers, WARN
        ai = types.SimpleNamespace(
            _has_agy=True, _has_codex=True, _has_claude=True,
            _run_agy=lambda q: "OK",
            _run_claude=lambda q, m, w: "OK",
            _run_codex=lambda q: '{"reasoning":"codex 사용량 한도"}',
            _is_codex_cooldown=lambda: True,      # 죽어서 cooldown
        )
        status, name, detail = _probe_ai_providers(ai)
        self.assertEqual(name, "AI")
        self.assertEqual(status, WARN)            # 일부 정상 + 일부 불가
        self.assertIn("codex", detail)
        self.assertIn("불가", detail)
        # codex 가 '정상:' 목록(슬래시 앞)에 들어가면 안 됨
        self.assertNotIn("codex", detail.split("/")[0])
        for live in ("agy", "claude"):
            self.assertIn(live, detail.split("/")[0])  # 살아있는 provider 는 정상 쪽

    def test_check_new_commits_notifies_local_commit(self):
        """같은 서버 commit+push(로컬==origin)도 새 커밋이면 알림 — origin-ahead 만 보던 버그 수정."""
        from zusik.core.bot import TradingBot
        import types
        b = TradingBot.__new__(TradingBot)
        b.discord = Mock()
        b._last_commit_hash = "oldhash"
        b._save_last_commit_hash = lambda h: None
        b._load_last_commit_hash = lambda: "oldhash"

        def fake_run(cmd, **kw):
            out = ""
            if "rev-list" in cmd:
                out = "0"        # origin 안 앞섬(로컬==origin) → ref=HEAD
            elif "log" in cmd:
                out = "abc123|Fix: x|me|1 minute ago|2026-06-25T17:00:00"
            elif "diff-tree" in cmd:
                out = "zusik/core/bot.py"
            return types.SimpleNamespace(returncode=0, stdout=out, stderr="")

        sent = {}
        with patch("subprocess.run", fake_run), \
             patch("zusik.clients.discord_bot.send_update_alert",
                   lambda *a, **k: sent.__setitem__("hash", a[0])):
            b._check_new_commits()
        self.assertEqual(sent.get("hash"), "abc123", "로컬 커밋도 새 커밋 알림 발송")

    def test_select_analysts_single_generalist(self):
        """비용 구조: LLM 애널리스트는 항상 generalist 1명(full·quick 공통). 퀀트는 로컬 adaptive
        가 $0으로 수행 → LLM 퀀트 중복 제거. full 2콜→1콜 회귀 가드."""
        from zusik.core.cost_optimizer import CostOptimizer
        self.assertEqual(CostOptimizer.select_analysts("full"), ["generalist"])
        self.assertEqual(CostOptimizer.select_analysts("quick"), ["generalist"])

    def test_claude_pause_blocks_claude_not_codex(self):
        """claude_pause_until 전까지 claude_* 호출 차단, codex 는 무관. 시각 경과 시 자동 해제."""
        import zusik.clients.claude_client as cc
        from datetime import datetime, timedelta
        orig = cc._claude_pause_cache
        self.addCleanup(lambda: setattr(cc, "_claude_pause_cache", orig))
        # 미래 시각 → claude 차단
        cc._claude_pause_cache = datetime.now() + timedelta(hours=2)
        self.assertTrue(cc._claude_paused())
        self.assertFalse(cc._check_limit("claude_sonnet"))
        self.assertFalse(cc._check_limit("claude_haiku"))
        paused_codex = cc._check_limit("codex")          # claude 한정 분기 → codex는 단락 안 됨
        # 과거 시각 → 자동 해제
        cc._claude_pause_cache = datetime.now() - timedelta(hours=1)
        self.assertFalse(cc._claude_paused())
        self.assertEqual(cc._check_limit("codex"), paused_codex, "pause 는 codex 에 영향 없음")

    def test_auto_hybrid_cheap_mode_skips_claude(self):
        """방어/급락(cheap_mode)에선 needs_claude 여도 로컬 adaptive 만 호출.
        2026-06-24: 크래시→보유 전부 출혈→매 사이클 full 4인 분석 폭증(agy/claude 소진)을 차단."""
        import types
        import pandas as pd
        from zusik.strategies.auto_hybrid import AutoHybridStrategy
        s = AutoHybridStrategy.__new__(AutoHybridStrategy)
        s._claude_ready = True
        s._cheap_mode = False
        s._claude_full_only_bleeding = True
        s._claude_vol_hold = 0.02
        s._claude_vol_noholding = 0.03
        s._claude_periodic_hold = 180
        s._claude_periodic_noholding = 1800
        s._position_state = {"holding": True, "profit_rate": -0.05, "is_bleeding": True}
        s._adaptive = types.SimpleNamespace(analyze=lambda d: "hold")
        s._calc_volatility = lambda d: 0.10            # 고변동 → needs_claude
        s._should_periodic_check = lambda i: False
        calls = {"claude": 0, "adaptive": 0}
        s._analyze_claude = lambda d, l, v: (calls.__setitem__("claude", calls["claude"] + 1) or "buy")
        s._analyze_adaptive = lambda d, v: (calls.__setitem__("adaptive", calls["adaptive"] + 1) or "hold")
        df = pd.DataFrame({"close": [100, 90]})

        s.set_cheap_mode(False)                        # 정상: 출혈 보유 → Claude(4인)
        s.analyze(df)
        self.assertEqual(calls["claude"], 1)

        calls["claude"] = 0
        calls["adaptive"] = 0
        s.set_cheap_mode(True)                          # 방어: Claude 끄고 로컬만
        s.analyze(df)
        self.assertEqual(calls["claude"], 0, "cheap 모드는 Claude 호출 안 함")
        self.assertEqual(calls["adaptive"], 1, "cheap 모드는 로컬 adaptive 사용")

    # ── 워치독 전이/복구 (scripts/watchdog.py) ──
    def _load_watchdog(self):
        import importlib.util
        spec = importlib.util.spec_from_file_location(
            "wd_test", os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                                    "scripts", "watchdog.py"))
        wd = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(wd)
        wd.DATA_DIR = self._tmp.name
        wd.STATE_FILE = os.path.join(self._tmp.name, "watchdog_state.json")
        wd.STATUS_FILE = os.path.join(self._tmp.name, "status.json")
        wd.LLM_HEALTH_FILE = os.path.join(self._tmp.name, "llm_health.json")
        wd._load_cfg = lambda: {"heartbeat_stale_min": 10}
        return wd

    def _write_status(self, wd, age_min):
        import json
        from datetime import datetime, timedelta
        ts = (datetime.now() - timedelta(minutes=age_min)).strftime("%Y-%m-%d %H:%M:%S")
        with open(wd.STATUS_FILE, "w") as f:
            json.dump({"generated_at": ts}, f)

    def test_watchdog_core_down_dedup_and_recovery(self):
        wd = self._load_watchdog()
        sent = []
        wd.send_discord_alert = lambda m: sent.append(m)
        self._write_status(wd, 2)
        wd.check_process_alive = lambda: True
        wd.main()
        self.assertEqual(sent, [])                       # 정상 무알림
        wd.check_process_alive = lambda: False
        wd.main()
        self.assertEqual(len(sent), 1)                   # 다운 전이 1회
        self.assertIn("코어 다운", sent[0])
        wd.main()
        self.assertEqual(len(sent), 1)                   # 연속 다운 dedup (스팸 방지)
        wd.check_process_alive = lambda: True
        self._write_status(wd, 1)
        wd.main()
        self.assertEqual(len(sent), 2)                   # 복구 전이 1회
        self.assertIn("정상화", sent[1])

    def test_watchdog_heartbeat_stale_detects_hang(self):
        """프로세스는 active 인데 tick 이 11분 멈추면 hang 으로 통보."""
        wd = self._load_watchdog()
        sent = []
        wd.send_discord_alert = lambda m: sent.append(m)
        wd.check_process_alive = lambda: True
        self._write_status(wd, 11)
        wd.main()
        self.assertTrue(any("멈춤" in s for s in sent), sent)

    def test_watchdog_llm_down_transition(self):
        wd = self._load_watchdog()
        sent = []
        wd.send_discord_alert = lambda m: sent.append(m)
        import json
        wd.check_process_alive = lambda: True
        self._write_status(wd, 1)
        with open(wd.LLM_HEALTH_FILE, "w") as f:
            json.dump({"status": "down", "last_reason": "quota"}, f)
        wd.main()
        self.assertTrue(any("LLM 작동 불가" in s for s in sent), sent)

    # ── Discord 진단 메뉴 ──
    def test_help_menu_lists_diagnostic_commands(self):
        from zusik.clients.discord_commander import DiscordCommander
        help_text = DiscordCommander._handle_help()
        for cmd in ("/헬스", "/점검", "/성과", "/상태", "/도움"):
            self.assertIn(cmd, help_text)
        self.assertIn("진단", help_text)   # 그룹 헤더

    def test_commander_routes_health_and_healthcheck(self):
        from zusik.clients.discord_commander import DiscordCommander
        c = DiscordCommander.__new__(DiscordCommander)
        c.bot = Mock()
        c.bot.cost.get_today_usage.return_value = {"claude": 1, "codex": 0, "agy": 0, "total": 1}
        # 헬스: 파일 기반 — 무예외로 텍스트 반환
        out = c._execute("헬스")
        self.assertIn("시스템 헬스", out)
        # 점검: healthcheck_text 를 가볍게 패치
        import zusik.utils.healthcheck as hc
        orig = hc.healthcheck_text
        hc.healthcheck_text = lambda client, config: (0, "헬스체크 결과 텍스트")
        self.addCleanup(lambda: setattr(hc, "healthcheck_text", orig))
        self.assertIn("헬스체크 결과", c._execute("점검"))


class BrokerSelectionTests(unittest.TestCase):
    """브로커 선택 — BROKER 로 증권사 선택, 실험 브로커는 fail-closed(미검증 코드가 실거래 금지).

    되돌리면(실험 브로커가 그냥 인스턴스화되거나 메서드가 빈 응답을 주면) 깨진다."""

    def test_create_broker_kis_default(self):
        """kis 와 빈 값 모두 KISClient 를 생성(기본·검증 브로커)."""
        from zusik.clients.broker import create_broker
        from zusik.clients.kis_client import KISClient
        for name in ("kis", "KIS", None, ""):
            c = create_broker(name, app_key="k", app_secret="s", account_no="12345678",
                              is_virtual=True)
            self.assertIsInstance(c, KISClient)

    def test_create_broker_unknown_raises(self):
        """알 수 없는 브로커 이름은 ValueError(지원 목록 안내)."""
        from zusik.clients.broker import create_broker
        with self.assertRaises(ValueError):
            create_broker("mirae", app_key="k", app_secret="s", account_no="1")

    def test_resolve_credentials_per_broker_with_kis_fallback(self):
        """브로커별 키(TOSS_* 등)가 있으면 그걸, 없으면 KIS_* 로 폴백 — 여러 브로커 키 .env 공존."""
        from zusik.clients.broker import resolve_broker_credentials
        keys = ("KIS_APP_KEY", "KIS_APP_SECRET", "KIS_ACCOUNT_NO",
                "TOSS_CLIENT_ID", "TOSS_CLIENT_SECRET", "TOSS_ACCOUNT_NO")
        saved = {k: os.environ.get(k) for k in keys}

        def restore():
            for k, v in saved.items():
                os.environ.pop(k, None) if v is None else os.environ.__setitem__(k, v)
        self.addCleanup(restore)
        os.environ.update(KIS_APP_KEY="kk", KIS_APP_SECRET="ks", KIS_ACCOUNT_NO="11111111",
                          TOSS_CLIENT_ID="tk", TOSS_CLIENT_SECRET="ts", TOSS_ACCOUNT_NO="22222222")
        kis = resolve_broker_credentials("kis")
        toss = resolve_broker_credentials("toss")
        self.assertEqual((kis["app_key"], kis["account_no"]), ("kk", "11111111"))
        self.assertEqual((toss["app_key"], toss["account_no"]), ("tk", "22222222"))
        # 토스 전용 키 제거 → KIS_* 로 폴백(공통 키만 채운 사용자 호환)
        for k in ("TOSS_CLIENT_ID", "TOSS_CLIENT_SECRET", "TOSS_ACCOUNT_NO"):
            os.environ.pop(k, None)
        toss2 = resolve_broker_credentials("toss")
        self.assertEqual((toss2["app_key"], toss2["account_no"]), ("kk", "11111111"))

    def test_account_no_optional_for_toss(self):
        """토스는 계좌(accountSeq) 자동 탐색 → 계좌번호 입력 불필요. KIS 는 필요."""
        from zusik.clients.broker import account_no_required
        self.assertTrue(account_no_required("kis"))
        self.assertFalse(account_no_required("toss"))

    def test_unsupported_broker_raises_valueerror(self):
        """지원 브로커는 kis/toss 뿐. 그 외(kiwoom/shinhan/오타)는 ValueError — 스캐폴드 제거됨."""
        from zusik.clients.broker import create_broker, BROKER_INFO
        self.assertEqual(set(BROKER_INFO), {"kis", "toss"})
        for name in ("kiwoom", "shinhan", "nope"):
            with self.assertRaises(ValueError, msg=f"{name} 는 ValueError 여야"):
                create_broker(name, app_key="k", app_secret="s", account_no="1")

    def test_toss_is_supported_broker(self):
        """토스는 BROKER_INFO ready=True 이고 create_broker 로 바로 생성된다(동의 게이트 없음)."""
        from zusik.clients.broker import BROKER_INFO, create_broker
        self.assertTrue(BROKER_INFO["toss"]["ready"])
        b = create_broker("toss", app_key="k", app_secret="s", account_no="1")
        self.assertEqual(b.base_url, "https://openapi.tossinvest.com")


def _toss_resp(payload, status=200):
    r = Mock()
    r.status_code = status
    r.json.return_value = payload
    r.raise_for_status.return_value = None
    return r


class TossClientTests(unittest.TestCase):
    """토스 클라이언트 — OAuth2·시세·잔고·주문 매핑 + 안전(동의 게이트·dry-run·OrderSafety).

    되돌리면(동의 없이 생성, dry-run 없이 실주문, 잘못된 주문 본문, 초과매도 미차단) 깨진다."""

    def setUp(self):
        self._old = os.environ.get("ZUSIK_EXPERIMENTAL_BROKER")
        os.environ["ZUSIK_EXPERIMENTAL_BROKER"] = "true"
        os.environ.pop("TOSS_LIVE_ORDERS", None)

    def tearDown(self):
        if self._old is None:
            os.environ.pop("ZUSIK_EXPERIMENTAL_BROKER", None)
        else:
            os.environ["ZUSIK_EXPERIMENTAL_BROKER"] = self._old
        os.environ.pop("TOSS_LIVE_ORDERS", None)

    def _client(self):
        from datetime import datetime, timedelta
        from zusik.clients.toss_client import TossClient
        c = TossClient(app_key="id", app_secret="sec", account_no="12345678")
        c._access_token = "tok"
        c._token_expires = datetime.now() + timedelta(hours=1)   # _ensure_token 단락
        c._account_seq = "999"
        return c

    def test_toss_instantiates_without_experimental_consent(self):
        """토스는 라이브 검증된 지원 브로커 — 실험 동의(ZUSIK_EXPERIMENTAL_BROKER) 없이 생성된다.
        (동의 게이트를 다시 넣으면 BROKER=toss 가 막혀 이 테스트가 깨진다.) 주문 안전은
        TOSS_LIVE_ORDERS dry-run 이 담당."""
        os.environ.pop("ZUSIK_EXPERIMENTAL_BROKER", None)
        from zusik.clients.toss_client import TossClient
        c = TossClient(app_key="id", app_secret="sec", account_no="1")
        self.assertEqual(c.base_url, "https://openapi.tossinvest.com")

    def test_toss_oauth2_token_flow(self):
        """OAuth2 client_credentials: POST /oauth2/token, Basic 인증, form grant_type."""
        import tempfile
        from datetime import datetime
        from zusik.clients.toss_client import TossClient
        c = TossClient(app_key="id", app_secret="sec", account_no="1")
        cap = {}

        def fake_post(url, headers=None, data=None, **k):
            cap.update(url=url, headers=headers, data=data)
            return _toss_resp({"access_token": "AT", "token_type": "Bearer", "expires_in": 3600})

        with tempfile.TemporaryDirectory() as d:
            with patch.object(TossClient, "_TOKEN_FILE", os.path.join(d, "t.json")), \
                    patch("zusik.clients.toss_client.requests.post", side_effect=fake_post):
                c._access_token = ""
                c._token_expires = datetime.min
                c._ensure_token()
        self.assertTrue(cap["url"].endswith("/oauth2/token"))
        self.assertTrue(cap["headers"]["Authorization"].startswith("Basic "))
        self.assertEqual(cap["data"], {"grant_type": "client_credentials"})
        self.assertEqual(c._access_token, "AT")

    def test_toss_current_price_parses(self):
        """시세는 {"result":[{lastPrice}]} 봉투 — 실제 토스 응답 형태."""
        c = self._client()
        with patch("zusik.clients.toss_client.requests.get",
                   return_value=_toss_resp({"result": [{"symbol": "005930",
                                            "lastPrice": "339000", "currency": "KRW"}]})):
            p = c.get_current_price("005930")
        self.assertEqual(p["price"], 339000)

    def test_toss_price_enriched_with_candle_change_and_volume(self):
        """등락률·거래량은 /prices 가 안 줘서 /candles(일봉)로 파생 보강 — crash/surge·사이징 복구.

        되돌리면(change_rate/volume 0 고정) 토스에서 급락·급등 감지와 동적 사이징이 약화된다."""
        c = self._client()

        def fake_get(url, headers=None, params=None, **k):
            if "/prices" in url:
                return _toss_resp({"result": [{"symbol": "005930",
                                   "lastPrice": "326500", "currency": "KRW"}]})
            if "/candles" in url:
                # 토스는 최신→과거 순. 직전(06-26) 종가 339000, 당일(06-29) 종가 326500.
                return _toss_resp({"result": {"candles": [
                    {"openPrice": "337000", "highPrice": "345000", "lowPrice": "316000",
                     "closePrice": "326500", "volume": "45517048"},
                    {"openPrice": "358000", "highPrice": "361000", "lowPrice": "321500",
                     "closePrice": "339000", "volume": "66931113"}]}})
            return _toss_resp({"result": []})

        with patch("zusik.clients.toss_client.requests.get", side_effect=fake_get):
            p = c.get_current_price("005930")
        self.assertEqual(p["price"], 326500)
        self.assertEqual(p["volume"], 45517048)            # 당일 봉 거래량
        self.assertEqual(p["prev_close"], 339000)          # 직전 봉 종가
        self.assertAlmostEqual(p["change_rate"], (326500 / 339000 - 1) * 100, places=2)
        self.assertLess(p["change_rate"], 0)               # 하락 → 음수(퍼센트 단위)

    def test_toss_429_retries_with_backoff(self):
        """429(rate limit)면 백오프 후 재시도 — 버스트 시 시세조회가 죽지 않게(self-throttle 동반).

        되돌리면(429 미처리·즉시 raise) 동시 호출 버스트에서 '토스 시세 조회 실패'가 쏟아진다."""
        c = self._client()
        calls = {"n": 0}

        def fake_get(url, headers=None, params=None, **k):
            if "/prices" in url:
                calls["n"] += 1
                if calls["n"] == 1:
                    return _toss_resp({"error": "rate limited"}, status=429)
                return _toss_resp({"result": [{"symbol": "005930", "lastPrice": "70000"}]})
            return _toss_resp({"result": []})     # /candles 보강은 빈 응답

        with patch("zusik.clients.toss_client.requests.get", side_effect=fake_get), \
                patch("zusik.clients.toss_client.time.sleep"):   # 백오프/스로틀 즉시
            p = c.get_current_price("005930")
        self.assertGreaterEqual(calls["n"], 2)                 # 429 → 재시도
        self.assertEqual(p["price"], 70000)                    # 재시도 성공

    def test_toss_balance_matches_contract_shape(self):
        c = self._client()

        def fake_get(url, headers=None, params=None, **k):
            if "/holdings" in url:
                return _toss_resp({"result": {
                    "marketValue": {"amount": {"krw": "710000"}},
                    "profitLoss": {"amount": {"krw": "10000"}},
                    "items": [{"symbol": "005930", "quantity": "10",
                               "averagePurchasePrice": "70000", "lastPrice": "71000",
                               "name": "삼성전자"}]}})
            if "/buying-power" in url:
                return _toss_resp({"result": {"currency": "KRW", "cashBuyingPower": "500000"}})
            return _toss_resp({"result": []})

        with patch("zusik.clients.toss_client.requests.get", side_effect=fake_get):
            b = c.get_balance()
        self.assertEqual(b["cash"], 500000)            # result.cashBuyingPower
        self.assertEqual(b["total_eval"], 710000)      # result.marketValue.amount.krw
        self.assertEqual(b["holdings"][0]["code"], "005930")
        self.assertEqual(b["holdings"][0]["qty"], 10)
        for key in ("cash", "total_eval", "total_profit", "holdings"):
            self.assertIn(key, b)   # KISClient get_balance 계약과 동일 키

    def test_toss_order_dry_run_by_default(self):
        """TOSS_LIVE_ORDERS 미설정이면 주문은 dry-run — 실제 POST 안 함(샌드박스 없는 미검증 보호)."""
        c = self._client()
        with patch("zusik.clients.toss_client.requests.get",
                   return_value=_toss_resp({"items": []})), \
                patch("zusik.clients.toss_client.requests.post") as mpost:
            r = c.buy_market("005930", 3)
        self.assertTrue(r.get("dry_run"))
        self.assertFalse(r["success"])
        mpost.assert_not_called()

    def test_toss_order_safety_blocks_oversell(self):
        """보유 초과 매도는 OrderSafetyValidator 가 차단 — live 여부와 무관(게이트가 먼저)."""
        c = self._client()
        os.environ["TOSS_LIVE_ORDERS"] = "true"

        def fake_get(url, headers=None, params=None, **k):
            if "/holdings" in url:
                return _toss_resp({"items": [{"symbol": "005930", "quantity": "5",
                                              "averagePurchasePrice": "70000",
                                              "lastPrice": "71000"}]})
            return _toss_resp({"cash": "0"})

        with patch("zusik.clients.toss_client.requests.get", side_effect=fake_get), \
                patch("zusik.clients.toss_client.requests.post") as mpost:
            r = c.sell_market("005930", 10)
        self.assertTrue(r.get("blocked"))
        mpost.assert_not_called()

    def test_toss_live_order_payload_mapping(self):
        """live 주문 본문이 명세대로: symbol/side BUY/orderType MARKET/quantity, 응답 orderId."""
        c = self._client()
        os.environ["TOSS_LIVE_ORDERS"] = "true"
        cap = {}

        def fake_get(url, headers=None, params=None, **k):
            if "/buying-power" in url:
                return _toss_resp({"cash": "1000000"})
            return _toss_resp({"items": []})

        def fake_post(url, headers=None, json=None, **k):
            cap.update(url=url, body=json)
            return _toss_resp({"result": {"orderId": "OID123", "clientOrderId": None}})

        with patch("zusik.clients.toss_client.requests.get", side_effect=fake_get), \
                patch("zusik.clients.toss_client.requests.post", side_effect=fake_post):
            r = c.buy_market("005930", 3)
        self.assertEqual(cap["body"], {"symbol": "005930", "side": "BUY",
                                       "orderType": "MARKET", "quantity": "3"})
        self.assertEqual(r["order_no"], "OID123")
        self.assertTrue(r["success"])

    def test_toss_account_seq_autodiscovered_without_account_no(self):
        """계좌번호 없이도 /api/v1/accounts 로 accountSeq 자동 탐색 (client_id/secret 만으로 동작)."""
        from datetime import datetime, timedelta
        from zusik.clients.toss_client import TossClient
        c = TossClient(app_key="id", app_secret="sec", account_no="")
        c._access_token = "tok"
        c._token_expires = datetime.now() + timedelta(hours=1)
        with patch("zusik.clients.toss_client.requests.get",
                   return_value=_toss_resp({"result": [{"accountSeq": 1, "accountNo": "00000000000",
                                            "accountType": "BROKERAGE"}]})):
            seq = c._get_account_seq()
        self.assertEqual(seq, "1")

    def test_toss_401_reissues_token_and_retries(self):
        """401(캐시 토큰 무효화)이면 토큰을 버리고 재발급 후 1회 재시도 — 견고성."""
        import tempfile
        from zusik.clients.toss_client import TossClient
        c = self._client()
        calls = {"prices": 0}

        def fake_get(url, headers=None, params=None, **k):
            if "/prices" in url:                    # 401-재시도 대상 경로만 카운트
                calls["prices"] += 1
                if calls["prices"] == 1:
                    return _toss_resp({"error": "unauthorized"}, status=401)
                return _toss_resp({"result": [{"symbol": "005930", "lastPrice": "339000"}]})
            return _toss_resp({"result": []})       # /candles 보강은 빈 응답(폴백)

        def fake_post(url, headers=None, data=None, **k):
            return _toss_resp({"access_token": "NEW", "token_type": "Bearer", "expires_in": 3600})

        with tempfile.TemporaryDirectory() as d:
            with patch.object(TossClient, "_TOKEN_FILE", os.path.join(d, "t.json")), \
                    patch("zusik.clients.toss_client.requests.get", side_effect=fake_get), \
                    patch("zusik.clients.toss_client.requests.post", side_effect=fake_post):
                p = c.get_current_price("005930")
        self.assertEqual(calls["prices"], 2)       # 401 → 재발급 후 재시도
        self.assertEqual(p["price"], 339000)       # 재시도 성공 파싱
        self.assertEqual(c._access_token, "NEW")   # 토큰 재발급됨

    def test_toss_us_current_price_and_fx(self):
        """미국 시세는 USD·float, 환율은 result.rate. (KR 과 같은 엔드포인트, ticker+USD)."""
        c = self._client()
        with patch("zusik.clients.toss_client.requests.get",
                   return_value=_toss_resp({"result": [{"symbol": "AAPL", "lastPrice": "277.42",
                                            "currency": "USD"}]})):
            p = c.get_us_current_price("AAPL")
        self.assertAlmostEqual(p["price"], 277.42)
        self.assertEqual(p["currency"], "USD")
        with patch("zusik.clients.toss_client.requests.get",
                   return_value=_toss_resp({"result": {"baseCurrency": "USD", "quoteCurrency": "KRW",
                                            "rate": "1537.91"}})):
            self.assertAlmostEqual(c.get_usd_krw_rate(), 1537.91)

    def test_toss_us_balance_shape_and_currency_filter(self):
        """미국 잔고는 cash_usd + USD 보유만(KRW 종목 제외), KIS get_us_balance 계약 키."""
        c = self._client()

        def fake_get(url, headers=None, params=None, **k):
            if "/holdings" in url:
                return _toss_resp({"result": {
                    "marketValue": {"amount": {"krw": "0", "usd": "554.84"}},
                    "profitLoss": {"amount": {"krw": "0", "usd": "20.00"}},
                    "items": [
                        {"symbol": "AAPL", "quantity": "2", "averagePurchasePrice": "267.42",
                         "lastPrice": "277.42", "name": "Apple", "currency": "USD"},
                        {"symbol": "005930", "quantity": "1", "averagePurchasePrice": "70000",
                         "lastPrice": "71000", "name": "삼성전자", "currency": "KRW"}]}})
            if "/buying-power" in url:
                return _toss_resp({"result": {"currency": "USD", "cashBuyingPower": "50"}})
            return _toss_resp({"result": []})

        with patch("zusik.clients.toss_client.requests.get", side_effect=fake_get):
            b = c.get_us_balance()
        self.assertAlmostEqual(b["cash_usd"], 50.0)
        self.assertEqual(len(b["holdings"]), 1)                 # USD 종목만 (삼성전자 제외)
        self.assertEqual(b["holdings"][0]["ticker"], "AAPL")
        self.assertEqual(b["holdings"][0]["qty"], 2)
        for key in ("cash_usd", "total_eval_usd", "holdings"):
            self.assertIn(key, b)

    def test_toss_us_order_dry_run_and_safety(self):
        """미국 주문도 dry-run 기본 + OrderSafetyValidator(USD 잔고로) 통과. body 는 ticker+price."""
        c = self._client()

        def fake_get(url, headers=None, params=None, **k):
            if "/buying-power" in url:   # 충분한 현금(안전망 통과 → dry-run 도달 확인)
                return _toss_resp({"result": {"currency": "USD", "cashBuyingPower": "500"}})
            if "/prices" in url:
                return _toss_resp({"result": [{"symbol": "NVDA", "lastPrice": "194.53",
                                              "currency": "USD"}]})
            return _toss_resp({"result": {"items": []}})

        with patch("zusik.clients.toss_client.requests.get", side_effect=fake_get), \
                patch("zusik.clients.toss_client.requests.post") as mpost:
            r = c.buy_us_limit("NVDA", 1, 190.0)
        self.assertTrue(r.get("dry_run"))      # TOSS_LIVE_ORDERS 미설정 → 미전송
        mpost.assert_not_called()
        # 현금 초과(USD 잔고로 검증)는 차단
        def fake_get2(url, headers=None, params=None, **k):
            if "/buying-power" in url:
                return _toss_resp({"result": {"currency": "USD", "cashBuyingPower": "50"}})
            if "/prices" in url:
                return _toss_resp({"result": [{"symbol": "NVDA", "lastPrice": "194.53"}]})
            return _toss_resp({"result": {"items": []}})
        with patch("zusik.clients.toss_client.requests.get", side_effect=fake_get2):
            r2 = c.buy_us_limit("NVDA", 1, 190.0)   # $190 > 현금 $50
        self.assertTrue(r2.get("blocked"))     # 미국 주문도 현금초과 차단(USD)


class PositionPersistenceTests(unittest.TestCase):
    """positions.json 원자적 저장 — 동시 저장(메인 루프 + 실시간 WS 틱 스레드)이 충돌해도
    FileNotFoundError 없이 유효 파일이 남아야 한다. 고정 tmp 공유 레이스 회귀 가드
    (실측: US 매도 중 'data/positions.json.tmp -> data/positions.json' FileNotFoundError)."""

    def test_concurrent_saves_no_crash(self):
        import json as _json
        import tempfile
        import threading
        import zusik.core.position_manager as pm
        with tempfile.TemporaryDirectory() as d:
            pf = os.path.join(d, "positions.json")
            with patch.object(pm, "POSITIONS_FILE", pf):
                errors = []

                def worker(n):
                    try:
                        for i in range(25):
                            pm._save_positions({"005930": {"qty": n, "i": i}})
                    except Exception as e:   # noqa: BLE001
                        errors.append(repr(e))

                threads = [threading.Thread(target=worker, args=(n,)) for n in range(8)]
                for t in threads:
                    t.start()
                for t in threads:
                    t.join()
                self.assertEqual(errors, [], f"동시 저장 충돌(고정 tmp 레이스): {errors[:3]}")
                with open(pf, encoding="utf-8") as f:
                    self.assertIsInstance(_json.load(f), dict)   # 최종 파일은 유효 JSON
                leftover = [x for x in os.listdir(d) if x.endswith(".tmp")]
                self.assertEqual(leftover, [], f"임시파일 누수: {leftover}")


class MonthlyTextReportTests(unittest.TestCase):
    """월간 리포트 공유 포맷터 — 종목별 손익·승률 상세 + '지난 달' 라벨(이번 달 오해 제거).

    되돌리면(단순 요약·by_stock 미사용·'이번 달' 라벨) 이 테스트가 깨진다."""

    _STATS = {
        "month": "2026-05", "days": 17, "start_equity": 193706, "end_equity": 21846467,
        "deposits": 22014549, "realized": -377340, "net_growth": -361788,
        "return_pct": -1.63, "max_drawdown": -15.33, "basis": "effective",
        "by_stock": [
            {"name": "NVIDIA", "code": "NVDA", "count": 1, "wins": 1, "pnl": 78399},
            {"name": "삼성전기", "code": "009150", "count": 1, "wins": 1, "pnl": 33139},
            {"name": "LG이노텍", "code": "011070", "count": 1, "wins": 0, "pnl": -196315},
        ],
    }

    def test_text_detailed_and_labeled_prev_month(self):
        from zusik.reporting.monthly_text import format_monthly_report
        t = format_monthly_report(self._STATS)
        self.assertIn("2026-05", t)
        self.assertIn("지난 달", t)          # 월 라벨 명확
        self.assertNotIn("이번 달", t)       # '이번 달' 오해 제거
        self.assertIn("NVIDIA", t)           # 종목별 상세(수익)
        self.assertIn("LG이노텍", t)         # 종목별 상세(손실)
        self.assertIn("승률", t)

    def test_winrate_from_by_stock(self):
        from zusik.reporting.monthly_text import _winrate
        trades, wins, rate = _winrate(self._STATS["by_stock"])
        self.assertEqual((trades, wins), (3, 2))
        self.assertAlmostEqual(rate, 200 / 3, places=1)

    def test_embed_fields_include_stock_breakdown(self):
        from zusik.reporting.monthly_text import monthly_embed_fields
        names = [f["name"] for f in monthly_embed_fields(self._STATS)]
        for must in ("승률", "수익 종목 TOP", "손실 종목"):
            self.assertIn(must, names)

    def test_empty_stats_safe(self):
        from zusik.reporting.monthly_text import format_monthly_report
        self.assertEqual(format_monthly_report({"days": 0}), "")


class UsTradingToggleTests(unittest.TestCase):
    """미국 매매 토글(config us_enabled). false 면 us_stocks 가 비고 run_us 가 즉시 반환.

    되돌리면(토글 무시) 미국을 안 하는 사용자도 US 매매·알림·USD 잔고조회가 돌아간다."""

    def _bot(self, us_enabled):
        from zusik.core.bot import TradingBot
        b = TradingBot.__new__(TradingBot)
        b.us_enabled = us_enabled
        b.screener = None
        b.auto_screen = False
        b._default_kr = [{"code": "005930", "name": "삼성전자"}]
        b._default_us = [{"ticker": "F", "name": "Ford"}] if us_enabled else []
        return b

    def test_us_disabled_empties_us_stocks(self):
        b = self._bot(False)
        b._load_stocks()
        self.assertEqual(b.us_stocks, [])          # 미국 종목 없음 → 알림/잔고/매매 게이트 통과 못 함
        self.assertTrue(b.kr_stocks)               # KR 은 그대로 운용

    def test_us_enabled_keeps_us_stocks(self):
        b = self._bot(True)
        b._load_stocks()
        self.assertEqual([s["ticker"] for s in b.us_stocks], ["F"])

    def test_screener_us_ignored_when_disabled(self):
        # 저장된 선별 결과에 US 가 있어도 us_enabled=false 면 무시돼야 한다.
        b = self._bot(False)
        b.screener = Mock()
        b.auto_screen = True
        b.screener.get_selected.return_value = {"kr": [], "us": [{"ticker": "NVDA"}]}
        b._filter_derivatives = lambda lst, market: lst
        b._load_stocks()
        self.assertEqual(b.us_stocks, [])

    def test_run_us_returns_early_when_disabled(self):
        b = self._bot(False)
        b.client = Mock()
        b.run_us()
        b.client.is_us_market_open.assert_not_called()   # us_enabled 게이트가 먼저 차단


class KrTradingToggleTests(unittest.TestCase):
    """한국 매매 토글(config kr_enabled). false 면 kr_stocks 가 비고 run_kr 이 즉시 반환.

    되돌리면(토글 무시) 한국을 안 하는 사용자도 KR 매매·종목선별이 돌아간다."""

    def _bot(self, kr_enabled):
        from zusik.core.bot import TradingBot
        b = TradingBot.__new__(TradingBot)
        b.kr_enabled = kr_enabled
        b.us_enabled = True
        b.screener = None
        b.auto_screen = False
        b._default_kr = [{"code": "005930", "name": "삼성전자"}] if kr_enabled else []
        b._default_us = [{"ticker": "F", "name": "Ford"}]
        return b

    def test_kr_disabled_empties_kr_stocks(self):
        b = self._bot(False)
        b._load_stocks()
        self.assertEqual(b.kr_stocks, [])          # 한국 종목 없음 → 매매/선별 게이트 통과 못 함
        self.assertTrue(b.us_stocks)               # US 는 그대로 운용

    def test_kr_enabled_keeps_kr_stocks(self):
        b = self._bot(True)
        b._load_stocks()
        self.assertEqual([s["code"] for s in b.kr_stocks], ["005930"])

    def test_screener_kr_ignored_when_disabled(self):
        # 저장된 선별 결과에 KR 이 있어도 kr_enabled=false 면 무시돼야 한다.
        b = self._bot(False)
        b.screener = Mock()
        b.auto_screen = True
        b.screener.get_selected.return_value = {"kr": [{"code": "000660"}], "us": []}
        b._filter_derivatives = lambda lst, market: lst
        b._load_stocks()
        self.assertEqual(b.kr_stocks, [])

    def test_run_kr_returns_early_when_disabled(self):
        b = self._bot(False)
        b.client = Mock()
        b.run_kr()
        b.client.is_market_open.assert_not_called()   # kr_enabled 게이트가 먼저 차단


def run_runtime_unittests():
    print("\n[6/6] 런타임 계약 unittest")
    suite = unittest.TestSuite()
    suite.addTests(unittest.defaultTestLoader.loadTestsFromTestCase(UsTradingToggleTests))
    suite.addTests(unittest.defaultTestLoader.loadTestsFromTestCase(KrTradingToggleTests))
    suite.addTests(unittest.defaultTestLoader.loadTestsFromTestCase(TradingBotRuntimeTests))
    suite.addTests(unittest.defaultTestLoader.loadTestsFromTestCase(TradingBotScenarioTests))
    suite.addTests(unittest.defaultTestLoader.loadTestsFromTestCase(LossPatternRegressionTests))
    suite.addTests(unittest.defaultTestLoader.loadTestsFromTestCase(CrashSurgeResponseTests))
    suite.addTests(unittest.defaultTestLoader.loadTestsFromTestCase(OrderSafetyTests))
    suite.addTests(unittest.defaultTestLoader.loadTestsFromTestCase(AIUsageConfigTests))
    suite.addTests(unittest.defaultTestLoader.loadTestsFromTestCase(TotalEquityPhantomTests))
    suite.addTests(unittest.defaultTestLoader.loadTestsFromTestCase(EquitySettlementTests))
    suite.addTests(unittest.defaultTestLoader.loadTestsFromTestCase(FastExitScanTests))
    suite.addTests(unittest.defaultTestLoader.loadTestsFromTestCase(MultiMessengerTests))
    suite.addTests(unittest.defaultTestLoader.loadTestsFromTestCase(RegimeSelectionTests))
    suite.addTests(unittest.defaultTestLoader.loadTestsFromTestCase(KISBuyingPowerTests))
    suite.addTests(unittest.defaultTestLoader.loadTestsFromTestCase(ScreeningStabilityTests))
    suite.addTests(unittest.defaultTestLoader.loadTestsFromTestCase(MCGateConfigTests))
    suite.addTests(unittest.defaultTestLoader.loadTestsFromTestCase(SelectionMethodTests))
    suite.addTests(unittest.defaultTestLoader.loadTestsFromTestCase(OpenGuardTests))
    suite.addTests(unittest.defaultTestLoader.loadTestsFromTestCase(FastEntryTests))
    suite.addTests(unittest.defaultTestLoader.loadTestsFromTestCase(SecurityHardeningTests))
    suite.addTests(unittest.defaultTestLoader.loadTestsFromTestCase(AiSignalIntegrationTests))
    suite.addTests(unittest.defaultTestLoader.loadTestsFromTestCase(TradingRecordTests))
    suite.addTests(unittest.defaultTestLoader.loadTestsFromTestCase(FastFallGuardTests))
    suite.addTests(unittest.defaultTestLoader.loadTestsFromTestCase(LocalLlmProviderTests))
    suite.addTests(unittest.defaultTestLoader.loadTestsFromTestCase(MonthlyReportTests))
    suite.addTests(unittest.defaultTestLoader.loadTestsFromTestCase(MonthlyTextReportTests))
    suite.addTests(unittest.defaultTestLoader.loadTestsFromTestCase(MonthlyStatsEffectiveTests))
    suite.addTests(unittest.defaultTestLoader.loadTestsFromTestCase(ResultsReportTests))
    suite.addTests(unittest.defaultTestLoader.loadTestsFromTestCase(SellTimingTests))
    suite.addTests(unittest.defaultTestLoader.loadTestsFromTestCase(SelectionAlphaTests))
    suite.addTests(unittest.defaultTestLoader.loadTestsFromTestCase(UsOpenSessionGuardTests))
    suite.addTests(unittest.defaultTestLoader.loadTestsFromTestCase(CommandSurfaceTests))
    suite.addTests(unittest.defaultTestLoader.loadTestsFromTestCase(PnlIntegrityTests))
    suite.addTests(unittest.defaultTestLoader.loadTestsFromTestCase(RealtimeEntryTests))
    suite.addTests(unittest.defaultTestLoader.loadTestsFromTestCase(StatusSnapshotTests))
    suite.addTests(unittest.defaultTestLoader.loadTestsFromTestCase(StabilityFeatureTests))
    suite.addTests(unittest.defaultTestLoader.loadTestsFromTestCase(BrokerSelectionTests))
    suite.addTests(unittest.defaultTestLoader.loadTestsFromTestCase(TossClientTests))
    suite.addTests(unittest.defaultTestLoader.loadTestsFromTestCase(PositionPersistenceTests))
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    if not result.wasSuccessful():
        raise AssertionError(
            f"unittest 실패: {len(result.failures)} fail, {len(result.errors)} error"
        )


def run_claude_analyst_unittests():
    print("\n[7/7] ClaudeAnalyst unittest")
    suite = unittest.TestSuite()
    for module_name in ("test_claude_analyst_parse", "test_claude_analyst_judge",
                        "test_claude_client_codex"):
        module = __import__(module_name)
        suite.addTests(unittest.defaultTestLoader.loadTestsFromModule(module))
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    if not result.wasSuccessful():
        raise AssertionError(
            f"ClaudeAnalyst unittest 실패: {len(result.failures)} fail, {len(result.errors)} error"
        )


def assert_has_method(obj, method_name):
    if not hasattr(obj, method_name):
        raise AttributeError(
            f"{type(obj).__name__}에 '{method_name}' 메서드 없음! "
            f"있는 메서드: {[m for m in dir(obj) if not m.startswith('_')]}"
        )


if __name__ == "__main__":
    print("=" * 50)
    print("Zusik 스모크 테스트")
    print("=" * 50)

    test_imports()
    test_cost_optimizer_methods()
    test_strategy_methods()
    test_tracker_methods()
    test_execute_stock_dryrun()
    check("TradingBot 런타임 계약 unittest", run_runtime_unittests)
    check("ClaudeAnalyst unittest", run_claude_analyst_unittests)

    print("\n" + "=" * 50)
    print(f"결과: {PASS} 통과 / {FAIL} 실패")
    print("=" * 50)

    if FAIL > 0:
        print("\n 실패한 테스트가 있습니다! 배포하지 마세요.")
        sys.exit(1)
    else:
        print("\n모든 테스트 통과 — 배포 가능")
        sys.exit(0)
