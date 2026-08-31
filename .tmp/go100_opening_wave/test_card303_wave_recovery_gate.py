"""#303 1-minute wave gate recovery contract tests."""

from __future__ import annotations

from collections import defaultdict, deque
from datetime import datetime

from backend.app.services.go100.live_trading import scalping_entry_engine as engine_mod
from backend.app.services.go100.live_trading import db_tick_feeder


def _engine():
    engine = engine_mod.ScalpingEntryEngine.__new__(engine_mod.ScalpingEntryEngine)
    engine._minute_bars = defaultdict(lambda: deque(maxlen=30))
    engine._minute_ohlc_bars = defaultdict(lambda: deque(maxlen=60))
    engine._minute_bar_current = {}
    engine._minute_ohlc_db_cache = {}
    engine._wave_recovery_cooldown = {}
    engine._audit_decision = lambda **kwargs: None
    return engine


def test_wave_recovery_force_refreshes_after_backfill(monkeypatch):
    """A failed #303 wave gate must block the current tick and refresh DB bars before retry."""

    class _FakeFiller:
        def backfill_missing_bars(self, stock_code):
            assert stock_code == "005930"
            return 1

        def close(self):
            return None

    from backend.app.services.go100.data import realtime_data_gap_filler

    monkeypatch.setattr(realtime_data_gap_filler, "DataGapFiller", _FakeFiller)
    engine = _engine()
    force_refresh_values = []

    def _fake_hydrate(stock_code, min_bars=12, limit=60, *, force_refresh=False):
        force_refresh_values.append(force_refresh)
        return 12

    monkeypatch.setattr(engine, "_hydrate_minute_ohlc_from_db", _fake_hydrate)
    metrics = {}

    engine._trigger_wave_data_recovery(
        stock_code="005930",
        card={"card_id": 303},
        status="warmup_blocked",
        metrics=metrics,
    )

    assert force_refresh_values == [True]
    assert metrics["wave_recovery_status"] == "attempted"
    assert metrics["wave_recovery_result"] == "recovered"
    assert metrics["wave_reentry_policy"] == "blocked_this_tick_retry_next_tick_after_recovery"


def test_minute_ohlc_hydrate_cache_can_be_bypassed(monkeypatch):
    """force_refresh=True must ignore a fresh cache so a just-backfilled bar is visible."""

    engine = _engine()
    engine._minute_ohlc_db_cache["005930"] = (
        engine_mod.time_module.monotonic(),
        [{"minute": "0900", "o": 1.0, "h": 1.0, "l": 1.0, "c": 1.0, "v": 1.0}],
    )
    calls = {"connect": 0}

    class _Cursor:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def execute(self, *args, **kwargs):
            return None

        def fetchall(self):
            return [
                (datetime(2026, 8, 24, 9, 0), 10, 11, 9, 10, 100),
                (datetime(2026, 8, 24, 9, 1), 10, 12, 10, 12, 200),
            ]

    class _Conn:
        def cursor(self):
            return _Cursor()

        def close(self):
            return None

    def _connect(**kwargs):
        calls["connect"] += 1
        return _Conn()

    monkeypatch.setattr(engine_mod.psycopg2, "connect", _connect)

    assert engine._hydrate_minute_ohlc_from_db("005930") == 1
    assert calls["connect"] == 0

    assert engine._hydrate_minute_ohlc_from_db("005930", force_refresh=True) == 2
    assert calls["connect"] == 1
    assert [bar["minute"] for bar in engine._minute_ohlc_bars["005930"]] == ["0900", "0901"]


def _wave_bar(minute: str, *, o: float, h: float, l: float, c: float, v: float = 100.0) -> dict:
    return {"minute": minute, "o": o, "h": h, "l": l, "c": c, "v": v}


def _card303_wave_rule() -> dict:
    return {
        "wave_min_bars": 4,
        "wave_lookback_bars": 6,
        "wave_session_origin_enabled": False,
        "wave_mtf_gate_enabled": False,
        "wave_min_gain_pct": 1.0,
        "wave_min_pullback_pct": 0.8,
        "wave_max_pullback_pct": 5.0,
        "wave_min_rebound_pct": 0.1,
        "wave_require_volume_contraction": False,
        "wave_require_rebound_candle": False,
        "wave_require_w2_low_confirmed": True,
        "wave_w2_low_confirm_bars": 1,
    }


def _disable_external_wave_filters(monkeypatch):
    monkeypatch.setattr(engine_mod, "_WAVE_MTF_ANALYZER", None)
    monkeypatch.setattr(engine_mod, "_DAILY_TREND_FILTER", None)
    monkeypatch.setattr(engine_mod, "_BEARISH_WAVE_ANALYZER", None)
    # These tests isolate the DB/session wave contract; a four-bar fixture
    # cannot establish the independent Williams-fractal gate.
    monkeypatch.setattr(engine_mod, "detect_fractal_pivot_lows", None)
    monkeypatch.setattr(engine_mod, "_WAVE_BAR_SOURCE_MODE", "ws_memory_legacy")


def test_card303_wave_prefers_collector_shard_db(monkeypatch):
    _disable_external_wave_filters(monkeypatch)
    monkeypatch.setattr(engine_mod, "_WAVE_BAR_SOURCE_MODE", "db_shard_preferred")
    engine = _engine()
    engine._minute_ohlc_bars["005930"].extend([
        _wave_bar("0900", o=100.0, h=100.4, l=99.8, c=100.0),
        _wave_bar("0901", o=100.0, h=103.0, l=100.0, c=102.5),
        _wave_bar("0902", o=102.1, h=102.3, l=101.4, c=101.6),
        _wave_bar("0903", o=101.6, h=102.0, l=101.6, c=101.9),
    ])
    hydrate_calls = []

    def _hydrate(stock_code, min_bars=12, limit=60, *, force_refresh=False):
        hydrate_calls.append((stock_code, min_bars, limit, force_refresh))
        return 4

    monkeypatch.setattr(engine, "_hydrate_minute_ohlc_from_db", _hydrate)

    ok, metrics = engine._evaluate_1min_wave_pullback(
        "005930", 101.9, _card303_wave_rule()
    )

    assert ok is True
    assert hydrate_calls and hydrate_calls[0][0] == "005930"
    assert metrics["wave_data_source_mode"] == "db_shard_preferred"
    assert metrics["wave_db_hydrated_bars"] == 4


def test_card303_opening_fast_wave_accepts_current_bar_w2_rebound(monkeypatch):
    _disable_external_wave_filters(monkeypatch)
    engine = _engine()
    engine._minute_ohlc_bars["005930"].extend([
        _wave_bar("0900", o=100.0, h=100.4, l=99.9, c=100.2, v=1000),
        _wave_bar("0901", o=100.2, h=102.2, l=100.2, c=101.9, v=1800),
        _wave_bar("0902", o=101.9, h=102.0, l=101.0, c=101.2, v=900),
        _wave_bar("0903", o=101.1, h=101.8, l=100.9, c=101.7, v=850),
    ])
    rule = _card303_wave_rule()
    rule.update({
        "opening_fast_wave_enabled": True,
        "opening_fast_wave_min_bars": 4,
        "opening_fast_wave_lookback_bars": 8,
        "opening_fast_wave_min_pullback_pct": 0.25,
        "opening_fast_wave_w2_low_confirm_bars": 0,
    })

    ok, metrics = engine._evaluate_1min_wave_pullback("005930", 101.7, rule)

    assert ok is True
    assert metrics["opening_wave_active"] is True
    assert metrics["opening_fast_wave_detected"] is True
    assert metrics["wave_peak_source"] == "opening_fast_wave_w1"
    assert metrics["pullback_low_source"] == "opening_fast_wave_w2"
    assert metrics["wave_w2_low_confirm_bars_effective"] == 0
    assert metrics["entry_gate"] == "opening_fast_w1_w2_reversal_mtf"
    assert metrics["wave_status"] == "wave_pullback_ok"


def test_direct_ws_subscription_sync_is_disabled_by_default(monkeypatch):
    class _LegacyWs:
        _stock_codes = ["000660"]

        def set_stock_codes(self, codes):
            raise AssertionError("legacy WS must not be mutated")

    engine = _engine()
    engine._universe = {"005930"}
    engine._kiwoom_ws = _LegacyWs()
    monkeypatch.setattr(engine_mod, "_DIRECT_WS_SUBSCRIPTION_SYNC_ENABLED", False)

    engine._sync_subscription_targets()


def test_db_bar_and_current_tick_are_deduplicated_by_minute():
    engine = _engine()
    engine._minute_ohlc_bars["005930"].append(
        _wave_bar("0900", o=100.0, h=101.0, l=99.5, c=100.5, v=500)
    )
    engine._minute_bar_current["005930"] = _wave_bar(
        "0900", o=100.4, h=101.5, l=100.2, c=101.2, v=100
    )

    bars = engine._get_minute_ohlc_series("005930")

    assert len(bars) == 1
    assert bars[0] == {
        "minute": "0900", "o": 100.0, "h": 101.5,
        "l": 99.5, "c": 101.2, "v": 500.0,
    }


def test_collector_shards_promote_card303_discovery_codes(monkeypatch):
    from backend.app.services.data import kiwoom_ws_market_collector as collector

    class _Cursor:
        rows = []

        def execute(self, sql, params=None):
            query = str(sql)
            if "AS trading_value_krw" in query:
                assert "관리종목" in query
                assert "정리매매" in query
                self.rows = [("005930",), ("000660",)]
            elif "GO100_LIMIT_UP" in query:
                self.rows = []
            elif "go100_data_backfill_queue" in query and "WITH recent" in query:
                self.rows = [("123456",)]
            elif "FROM stock_universe" in query:
                self.rows = [("035720",)]
            else:
                raise AssertionError(f"unexpected collector SQL: {query[:120]}")

        def fetchall(self):
            return self.rows

    class _Conn:
        def __init__(self):
            self.cur = _Cursor()

        def cursor(self):
            return self.cur

        def close(self):
            return None

    monkeypatch.setattr(collector.psycopg2, "connect", lambda **kwargs: _Conn())

    codes = collector._load_sharded_stock_codes(0, 1, 0)

    assert codes[:4] == ["123456", "005930", "000660", "035720"]


def test_card303_one_share_live_override_defers_stale_db_cash_gate():
    card = {
        "card_id": 303,
        "account_is_mock": False,
        "risk_params": {
            "position_sizing_mode": "fixed_quantity",
            "fixed_quantity": 1,
            "live_test_limit_override": True,
        },
    }

    assert engine_mod._is_card303_one_share_live_override(card) is True
    assert engine_mod._defer_fixed_quantity_cash_gate(card) is True


def test_fixed_quantity_without_live_override_keeps_db_cash_gate():
    card = {
        "card_id": 303,
        "account_is_mock": False,
        "risk_params": {
            "position_sizing_mode": "fixed_quantity",
            "fixed_quantity": 1,
            "live_test_limit_override": False,
        },
    }

    assert engine_mod._is_card303_one_share_live_override(card) is False
    assert engine_mod._defer_fixed_quantity_cash_gate(card) is False


def test_card303_wave_blocks_when_w2_low_is_current_bar(monkeypatch):
    _disable_external_wave_filters(monkeypatch)
    engine = _engine()
    engine._minute_ohlc_bars["005930"].extend([
        _wave_bar("0900", o=100.0, h=100.4, l=99.8, c=100.0),
        _wave_bar("0901", o=100.0, h=103.0, l=100.0, c=102.5),
        _wave_bar("0902", o=102.4, h=102.6, l=102.0, c=102.2),
        _wave_bar("0903", o=101.6, h=101.9, l=101.4, c=101.8),
    ])

    ok, metrics = engine._evaluate_1min_wave_pullback("005930", 101.8, _card303_wave_rule())

    assert ok is False
    assert metrics["wave_status"] == "w2_low_not_confirmed"
    assert metrics["pullback_depth_pct"] >= 0.8
    assert metrics["bars_after_pullback_low"] == 0
    assert metrics["w2_low_confirmed"] is False


def test_card303_wave_passes_after_w2_low_has_following_bar(monkeypatch):
    _disable_external_wave_filters(monkeypatch)
    engine = _engine()
    engine._minute_ohlc_bars["005930"].extend([
        _wave_bar("0900", o=100.0, h=100.4, l=99.8, c=100.0),
        _wave_bar("0901", o=100.0, h=103.0, l=100.0, c=102.5),
        _wave_bar("0902", o=102.1, h=102.3, l=101.4, c=101.6),
        _wave_bar("0903", o=101.6, h=102.0, l=101.6, c=101.9),
    ])

    ok, metrics = engine._evaluate_1min_wave_pullback("005930", 101.9, _card303_wave_rule())

    assert ok is True
    assert metrics["wave_status"] == "wave_pullback_ok"
    assert metrics["bars_after_pullback_low"] == 1
    assert metrics["w2_low_confirmed"] is True
    assert metrics["wave_sequence_confirmed"] is True


def test_card303_shallow_pullback_passes_when_w2_low_confirmed(monkeypatch):
    """Regression: a shallow W2 low after a confirmed W1 high must not be blocked
    by the min_pullback threshold when require_w2_low_confirmed=True."""
    _disable_external_wave_filters(monkeypatch)
    engine = _engine()
    # peak at bar 0901 (h=103.0), pullback to 102.5 (only ~0.49% depth < 0.8% threshold)
    engine._minute_ohlc_bars["005930"].extend([
        _wave_bar("0900", o=100.0, h=100.4, l=99.8, c=100.0),
        _wave_bar("0901", o=100.0, h=103.0, l=100.0, c=102.5),
        _wave_bar("0902", o=102.4, h=102.6, l=102.5, c=102.5),
        _wave_bar("0903", o=102.5, h=102.8, l=102.5, c=102.7),
    ])
    rule = _card303_wave_rule()
    rule["wave_min_pullback_pct"] = 0.8

    ok, metrics = engine._evaluate_1min_wave_pullback("005930", 102.7, rule)

    # pullback_depth_pct is recorded as a diagnostic metric (not a blocker)
    assert metrics["pullback_depth_pct"] < 0.8, "fixture must have shallow pullback"
    assert metrics["w2_low_confirmed"] is True
    assert metrics["wave_sequence_confirmed"] is True
    assert ok is True, (
        f"shallow W2 must not be hard-blocked; wave_status={metrics.get('wave_status')}"
    )
    assert metrics["wave_status"] == "wave_pullback_ok"


def test_card303_opening_wave_relaxes_upper_mtf_warmup_after_high_low(monkeypatch):
    """09:20 opening wave can enter after W1 high -> W2 low even when 5/10m are still neutral."""

    class _DummyMtfResult:
        aligned = False
        confidence_boost = 0.0
        override_action = ""
        tf_results = {
            "1m": {"trend": "BULLISH"},
            "3m": {"trend": "BULLISH"},
            "5m": {"trend": "NEUTRAL"},
            "10m": {"trend": "NEUTRAL"},
        }

    class _DummyMtfAnalyzer:
        def analyze(self, bars):
            return _DummyMtfResult()

    monkeypatch.setattr(engine_mod, "_WAVE_COUNTER", None)
    monkeypatch.setattr(engine_mod, "_WAVE_MTF_ANALYZER", _DummyMtfAnalyzer())
    monkeypatch.setattr(engine_mod, "_DAILY_TREND_FILTER", None)
    monkeypatch.setattr(engine_mod, "_BEARISH_WAVE_ANALYZER", None)
    monkeypatch.setattr(engine_mod, "_TREND_CONTINUITY_TRACKER", None)
    monkeypatch.setattr(engine_mod, "detect_fractal_pivot_lows", None)
    monkeypatch.setattr(engine_mod, "_WAVE_BAR_SOURCE_MODE", "ws_memory_legacy")
    engine = _engine()
    engine._minute_ohlc_bars["000880"].extend([
        _wave_bar("0900", o=100.0, h=100.4, l=99.8, c=100.0),
        _wave_bar("0905", o=100.0, h=103.0, l=100.0, c=102.8),
        _wave_bar("0910", o=102.6, h=102.7, l=101.8, c=102.0),
        _wave_bar("0915", o=102.0, h=102.4, l=101.9, c=102.3),
        _wave_bar("0920", o=102.3, h=102.6, l=102.2, c=102.4),
    ])
    rule = _card303_wave_rule()
    rule.update({
        "wave_mtf_gate_enabled": True,
        "wave_mtf_min_bullish_count": 3,
        "opening_fast_wave_enabled": True,
        "opening_fast_wave_regular_end": "0930",
        "opening_fast_wave_mtf_min_upper_bullish": 1,
    })

    ok, metrics = engine._evaluate_1min_wave_pullback("000880", 102.4, rule)

    assert ok is True
    assert metrics["opening_wave_active"] is True
    assert metrics["opening_wave_mtf_relaxed"] is True
    assert metrics["wave_upper_bullish_count"] == 1
    assert metrics["wave_upper_min_required"] == 1
    assert metrics["wave_sequence_confirmed"] is True
    assert metrics["wave_status"] == "wave_pullback_ok"


def test_card303_opening_wave_relaxes_upper_mtf_warmup_after_high_low(monkeypatch):
    """09:20 opening wave can enter after W1 high -> W2 low even when 5/10m are still neutral."""

    class _DummyMtfResult:
        aligned = False
        confidence_boost = 0.0
        override_action = ""
        tf_results = {
            "1m": {"trend": "BULLISH"},
            "3m": {"trend": "BULLISH"},
            "5m": {"trend": "NEUTRAL"},
            "10m": {"trend": "NEUTRAL"},
        }

    class _DummyMtfAnalyzer:
        def analyze(self, bars):
            return _DummyMtfResult()

    monkeypatch.setattr(engine_mod, "_WAVE_COUNTER", None)
    monkeypatch.setattr(engine_mod, "_WAVE_MTF_ANALYZER", _DummyMtfAnalyzer())
    monkeypatch.setattr(engine_mod, "_DAILY_TREND_FILTER", None)
    monkeypatch.setattr(engine_mod, "_BEARISH_WAVE_ANALYZER", None)
    monkeypatch.setattr(engine_mod, "_TREND_CONTINUITY_TRACKER", None)
    monkeypatch.setattr(engine_mod, "detect_fractal_pivot_lows", None)
    monkeypatch.setattr(engine_mod, "_WAVE_BAR_SOURCE_MODE", "ws_memory_legacy")
    engine = _engine()
    engine._minute_ohlc_bars["000880"].extend([
        _wave_bar("0900", o=100.0, h=100.4, l=99.8, c=100.0),
        _wave_bar("0905", o=100.0, h=103.0, l=100.0, c=102.8),
        _wave_bar("0910", o=102.6, h=102.7, l=101.8, c=102.0),
        _wave_bar("0915", o=102.0, h=102.4, l=101.9, c=102.3),
        _wave_bar("0920", o=102.3, h=102.6, l=102.2, c=102.4),
    ])
    rule = _card303_wave_rule()
    rule.update({
        "wave_mtf_gate_enabled": True,
        "wave_mtf_min_bullish_count": 3,
        "opening_fast_wave_enabled": True,
        "opening_fast_wave_regular_end": "0930",
        "opening_fast_wave_mtf_min_upper_bullish": 1,
    })

    ok, metrics = engine._evaluate_1min_wave_pullback("000880", 102.4, rule)

    assert ok is True
    assert metrics["opening_wave_active"] is True
    assert metrics["opening_wave_mtf_relaxed"] is True
    assert metrics["wave_upper_bullish_count"] == 1
    assert metrics["wave_upper_min_required"] == 1
    assert metrics["wave_sequence_confirmed"] is True
    assert metrics["wave_status"] == "wave_pullback_ok"


def test_card303_deep_pullback_still_blocked_in_w2_confirmed_mode(monkeypatch):
    """max_pullback protection remains active regardless of W2-confirmed mode."""
    _disable_external_wave_filters(monkeypatch)
    engine = _engine()
    # peak at 103.0, pullback to 97.0 → depth ≈ 5.83% > max 5.0%
    engine._minute_ohlc_bars["005930"].extend([
        _wave_bar("0900", o=100.0, h=100.4, l=99.8, c=100.0),
        _wave_bar("0901", o=100.0, h=103.0, l=100.0, c=102.5),
        _wave_bar("0902", o=102.0, h=102.2, l=97.0, c=97.5),
        _wave_bar("0903", o=97.5, h=98.5, l=97.5, c=98.0),
    ])
    rule = _card303_wave_rule()  # wave_max_pullback_pct=5.0

    ok, metrics = engine._evaluate_1min_wave_pullback("005930", 98.0, rule)

    assert ok is False
    assert metrics["wave_status"] == "pullback_too_deep"
    assert metrics["pullback_depth_pct"] > 5.0


def test_card303_discovery_gate_fails_closed_when_snapshot_rank_is_unavailable():
    engine = _engine()
    card = {"card_id": 303}

    allowed, reason, metrics = engine._check_card303_discovery(card, "005930")

    assert allowed is False
    assert reason == "card303_discovery_unavailable"
    assert metrics["discovery_required"] is True
    assert metrics["discovery_candidate_count"] == 0


def test_card303_discovery_gate_rejects_broad_universe_code_outside_top50():
    engine = _engine()
    engine._mahaseven_top50_codes = {"000660"}

    allowed, reason, metrics = engine._check_card303_discovery({"card_id": 303}, "005930")

    assert allowed is False
    assert reason == "card303_discovery_not_top50"
    assert metrics["discovery_candidate_count"] == 1


def test_card303_discovery_gate_allows_top50_code_and_ignores_other_cards():
    engine = _engine()
    engine._mahaseven_top50_codes = {"005930"}

    assert engine._check_card303_discovery({"card_id": 303}, "005930")[0] is True
    assert engine._check_card303_discovery({"card_id": 119}, "005930")[0] is True


def test_db_feeder_session_windows_match_krx_and_nxt_contract():
    kst = db_tick_feeder.KST
    assert db_tick_feeder._is_market_hours(datetime(2026, 8, 27, 8, 0, tzinfo=kst)) is True
    assert db_tick_feeder._is_market_hours(datetime(2026, 8, 27, 8, 50, tzinfo=kst)) is False
    assert db_tick_feeder._is_market_hours(datetime(2026, 8, 27, 9, 0, tzinfo=kst)) is True
    assert db_tick_feeder._is_market_hours(datetime(2026, 8, 27, 15, 30, tzinfo=kst)) is False
    assert db_tick_feeder._is_market_hours(datetime(2026, 8, 27, 15, 40, tzinfo=kst)) is True
    assert db_tick_feeder._is_market_hours(datetime(2026, 8, 27, 20, 0, tzinfo=kst)) is False


def test_legacy_direct_ws_requires_explicit_runner_opt_in(monkeypatch):
    from backend.app.services.go100.live_trading import kiwoom_scalping_runner

    monkeypatch.delenv("GO100_SCALPING_ALLOW_LEGACY_DIRECT_WS", raising=False)
    assert kiwoom_scalping_runner._legacy_direct_ws_allowed() is False
    monkeypatch.setenv("GO100_SCALPING_ALLOW_LEGACY_DIRECT_WS", "true")
    assert kiwoom_scalping_runner._legacy_direct_ws_allowed() is True
