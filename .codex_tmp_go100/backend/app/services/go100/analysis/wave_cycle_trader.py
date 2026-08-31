"""
Full wave-cycle trading signal layer for GO100.

This strategy-agnostic wrapper converts WaveCounter/MTFWaveAnalyzer outputs
into BUY/SELL/HOLD/WAIT signals. Strategy cards can then decide which signals
to use.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import time
from typing import Any, Optional

try:
    from app.services.go100.analysis.mtf_wave_analyzer import MTFWaveAnalyzer, MTFWaveContext
    from app.services.go100.analysis.wave_counter import WaveCounter, WaveCountResult
    from app.services.go100.analysis.wave_measurer import WaveMeasurer, WaveMeasureResult
except ModuleNotFoundError:
    from backend.app.services.go100.analysis.mtf_wave_analyzer import MTFWaveAnalyzer, MTFWaveContext
    from backend.app.services.go100.analysis.wave_counter import WaveCounter, WaveCountResult
    from backend.app.services.go100.analysis.wave_measurer import WaveMeasurer, WaveMeasureResult


@dataclass
class WaveCyclePosition:
    stock_code: str
    entry_price: float
    entry_time: str
    entry_wave: int
    qty_ratio: float = 1.0
    peak_price: float = 0.0
    bars_held: int = 0


@dataclass
class WaveCycleState:
    stock_code: str
    prev_wave_number: int = 0
    prev_phase_label: str = "C1-W0"
    last_action: str = "WAIT"
    daily_trade_count: int = 0
    loss_streak: int = 0
    position: Optional[WaveCyclePosition] = None
    seen_peaks: list[dict[str, Any]] = field(default_factory=list)
    seen_troughs: list[dict[str, Any]] = field(default_factory=list)


@dataclass
class WaveCycleSignal:
    action: str
    reason: str
    stock_code: str
    price: float
    timestamp: str
    wave_number_1m: int
    phase_label_1m: str
    qty_ratio: float = 0.0
    stop_price: float = 0.0
    trailing_pct: float = 0.0
    mtf_alignment_score: float = 0.0
    mtf_dominant_phase: str = "W0"
    mtf_cross_tf_risk: str = "unknown"
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "action": self.action,
            "reason": self.reason,
            "stock_code": self.stock_code,
            "price": round(self.price, 4),
            "timestamp": self.timestamp,
            "wave_number_1m": self.wave_number_1m,
            "phase_label_1m": self.phase_label_1m,
            "qty_ratio": round(self.qty_ratio, 4),
            "stop_price": round(self.stop_price, 4),
            "trailing_pct": round(self.trailing_pct, 4),
            "mtf_alignment_score": round(self.mtf_alignment_score, 4),
            "mtf_dominant_phase": self.mtf_dominant_phase,
            "mtf_cross_tf_risk": self.mtf_cross_tf_risk,
            "metadata": self.metadata,
        }


class WaveCycleTrader:
    """Generate full-cycle wave trading signals from one-minute bars."""

    def __init__(self, params: Optional[dict[str, Any]] = None):
        p = params or {}
        self.min_bars = int(p.get("min_bars", 25))
        self.max_daily_trades = int(p.get("max_daily_trades", 20))
        self.stop_loss_pct = float(p.get("stop_loss_pct", -2.0))
        self.min_pullback_pct = float(p.get("min_pullback_pct", 0.4))
        self.max_pullback_pct = float(p.get("max_pullback_pct", 8.0))
        self.min_reversal_body_pct = float(p.get("min_reversal_body_pct", 0.1))
        self.min_mtf_alignment = float(p.get("min_mtf_alignment", 0.25))
        self.block_upper_w5 = bool(p.get("block_upper_w5", True))
        self.trailing_by_wave = dict(p.get("trailing_by_wave") or {1: 1.0, 3: 0.8, 5: 0.5})
        self.trailing_activation_pct = float(p.get("trailing_activation_pct", 0.8))
        self.force_exit_time = str(p.get("force_exit_time", "15:20"))
        self._counter = WaveCounter()
        self._measurer = WaveMeasurer()
        self._mtf = MTFWaveAnalyzer()
        self._states: dict[str, WaveCycleState] = {}

    def reset(self, stock_code: Optional[str] = None) -> None:
        if stock_code is None:
            self._states.clear()
        else:
            self._states.pop(stock_code, None)

    def evaluate(self, stock_code: str, bars_1m: list[dict[str, Any]]) -> WaveCycleSignal:
        state = self._states.setdefault(stock_code, WaveCycleState(stock_code=stock_code))
        if not bars_1m:
            return self._signal("WAIT", "NO_BARS", stock_code, 0.0, "", WaveCountResult())

        latest = bars_1m[-1]
        price = _bar_close(latest)
        ts = _bar_time_str(latest)

        wc = self._counter.count(bars_1m)
        if len(bars_1m) < self.min_bars:
            return self._signal("WAIT", "INSUFFICIENT_BARS", stock_code, price, ts, wc)

        measure = self._measurer.measure(bars_1m, wc)
        mtf_ctx = self._mtf.analyze(stock_code, bars_1m) if len(bars_1m) >= 60 else MTFWaveContext(stock_code=stock_code)
        self._remember_turning_points(state, wc)

        if _time_at_or_after(ts, self.force_exit_time) and state.position is not None:
            sig = self._signal("SELL", "TIME_FORCE_EXIT", stock_code, price, ts, wc, measure, mtf_ctx)
            state.position = None
            state.daily_trade_count += 1
            self._advance_state(state, wc, sig.action)
            return sig

        if state.position is not None:
            sig = self._evaluate_exit(state, wc, measure, mtf_ctx, bars_1m, price, ts)
        else:
            sig = self._evaluate_entry(state, wc, measure, mtf_ctx, bars_1m, price, ts)

        self._advance_state(state, wc, sig.action)
        return sig

    def _evaluate_entry(
        self,
        state: WaveCycleState,
        wc: WaveCountResult,
        measure: WaveMeasureResult,
        mtf_ctx: MTFWaveContext,
        bars: list[dict[str, Any]],
        price: float,
        ts: str,
    ) -> WaveCycleSignal:
        if state.daily_trade_count >= self.max_daily_trades:
            return self._signal("WAIT", "MAX_DAILY_TRADES", state.stock_code, price, ts, wc, measure, mtf_ctx)

        if wc.wave_number not in (1, 2, 4):
            return self._signal("WAIT", "NOT_BUY_WAVE", state.stock_code, price, ts, wc, measure, mtf_ctx)

        if wc.wave_number in (2, 4):
            if measure.amplitude_pct < self.min_pullback_pct:
                return self._signal("WAIT", "PULLBACK_TOO_SHALLOW", state.stock_code, price, ts, wc, measure, mtf_ctx)
            if measure.amplitude_pct > self.max_pullback_pct:
                return self._signal("WAIT", "PULLBACK_TOO_DEEP", state.stock_code, price, ts, wc, measure, mtf_ctx)
            if not _is_reversal_confirmed(bars, self.min_reversal_body_pct):
                return self._signal("WAIT", "REVERSAL_NOT_CONFIRMED", state.stock_code, price, ts, wc, measure, mtf_ctx)

        if wc.wave_number == 1 and state.prev_wave_number not in (0, 5):
            return self._signal("WAIT", "W1_NOT_NEW_CYCLE", state.stock_code, price, ts, wc, measure, mtf_ctx)

        if self.block_upper_w5 and mtf_ctx.is_upper_tf_w5(["5m", "10m", "15m"]):
            return self._signal("WAIT", "UPPER_TF_W5_BLOCK", state.stock_code, price, ts, wc, measure, mtf_ctx)

        if mtf_ctx.available_tfs and mtf_ctx.alignment_score < self.min_mtf_alignment:
            return self._signal("WAIT", "MTF_ALIGNMENT_LOW", state.stock_code, price, ts, wc, measure, mtf_ctx)

        qty_ratio = self._sizing(mtf_ctx)
        state.position = WaveCyclePosition(
            stock_code=state.stock_code,
            entry_price=price,
            entry_time=ts,
            entry_wave=wc.wave_number,
            qty_ratio=qty_ratio,
            peak_price=price,
        )
        state.daily_trade_count += 1
        return self._signal("BUY", f"BUY_W{wc.wave_number}_LOW", state.stock_code, price, ts, wc, measure, mtf_ctx, qty_ratio=qty_ratio)

    def _evaluate_exit(
        self,
        state: WaveCycleState,
        wc: WaveCountResult,
        measure: WaveMeasureResult,
        mtf_ctx: MTFWaveContext,
        bars: list[dict[str, Any]],
        price: float,
        ts: str,
    ) -> WaveCycleSignal:
        pos = state.position
        assert pos is not None
        pos.bars_held += 1
        pos.peak_price = max(pos.peak_price or price, price)
        pnl_pct = (price - pos.entry_price) / pos.entry_price * 100.0 if pos.entry_price > 0 else 0.0

        if pnl_pct <= self.stop_loss_pct:
            state.position = None
            state.loss_streak += 1
            return self._signal("SELL", "STOP_LOSS", state.stock_code, price, ts, wc, measure, mtf_ctx)

        trailing_pct = self.trailing_by_wave.get(wc.wave_number, self.trailing_by_wave.get(pos.entry_wave, 0.8))
        drawdown_from_peak = (price - pos.peak_price) / pos.peak_price * 100.0 if pos.peak_price > 0 else 0.0
        if pnl_pct >= self.trailing_activation_pct and drawdown_from_peak <= -abs(trailing_pct):
            state.position = None
            if pnl_pct <= 0:
                state.loss_streak += 1
            return self._signal("SELL", "TRAILING_BY_WAVE", state.stock_code, price, ts, wc, measure, mtf_ctx, trailing_pct=trailing_pct)

        if pos.entry_wave == 2 and wc.wave_number == 4:
            state.position = None
            return self._signal("SELL", "W3_PEAK_CONFIRMED", state.stock_code, price, ts, wc, measure, mtf_ctx)

        if pos.entry_wave in (1, 4) and wc.wave_number == 5 and _is_bearish_reversal(bars):
            state.position = None
            return self._signal("SELL", "W5_REVERSAL", state.stock_code, price, ts, wc, measure, mtf_ctx, trailing_pct=trailing_pct)

        if mtf_ctx.is_upper_tf_w5(["5m", "10m"]) and _is_bearish_reversal(bars):
            state.position = None
            return self._signal("SELL", "UPPER_TF_W5_RISK", state.stock_code, price, ts, wc, measure, mtf_ctx, trailing_pct=0.5)

        return self._signal("HOLD", "POSITION_HOLD", state.stock_code, price, ts, wc, measure, mtf_ctx, trailing_pct=trailing_pct)

    def _sizing(self, mtf_ctx: MTFWaveContext) -> float:
        if not mtf_ctx.available_tfs:
            return 0.5
        score = mtf_ctx.alignment_score
        if score >= 0.7:
            return 1.0
        if score >= 0.5:
            return 0.8
        if score >= 0.3:
            return 0.5
        return 0.3

    @staticmethod
    def _remember_turning_points(state: WaveCycleState, wc: WaveCountResult) -> None:
        state.seen_peaks = list(wc.wave_peaks or [])
        state.seen_troughs = list(wc.wave_troughs or [])

    @staticmethod
    def _advance_state(state: WaveCycleState, wc: WaveCountResult, action: str) -> None:
        state.prev_wave_number = wc.wave_number
        state.prev_phase_label = wc.phase_label
        state.last_action = action

    @staticmethod
    def _signal(
        action: str,
        reason: str,
        stock_code: str,
        price: float,
        ts: str,
        wc: WaveCountResult,
        measure: Optional[WaveMeasureResult] = None,
        mtf_ctx: Optional[MTFWaveContext] = None,
        qty_ratio: float = 0.0,
        trailing_pct: float = 0.0,
    ) -> WaveCycleSignal:
        measure = measure or WaveMeasureResult()
        mtf_ctx = mtf_ctx or MTFWaveContext(stock_code=stock_code)
        stop_price = price * 0.98 if action == "BUY" else 0.0
        return WaveCycleSignal(
            action=action,
            reason=reason,
            stock_code=stock_code,
            price=price,
            timestamp=ts,
            wave_number_1m=wc.wave_number,
            phase_label_1m=wc.phase_label,
            qty_ratio=qty_ratio,
            stop_price=stop_price,
            trailing_pct=trailing_pct,
            mtf_alignment_score=mtf_ctx.alignment_score,
            mtf_dominant_phase=mtf_ctx.dominant_wave_phase,
            mtf_cross_tf_risk=mtf_ctx.cross_tf_risk,
            metadata={
                "measure": {
                    "amplitude_pct": measure.amplitude_pct,
                    "depth_pct": measure.depth_pct,
                    "fib_ratio": measure.fib_ratio,
                    "nearest_fib": measure.nearest_fib,
                    "extension_ratio": measure.extension_ratio,
                    "time_bars": measure.time_bars,
                },
                "mtf": mtf_ctx.to_dict(),
            },
        )


def _bar_close(bar: dict[str, Any]) -> float:
    for key in ("c", "close", "close_price"):
        if bar.get(key) is not None:
            return float(bar[key])
    return 0.0


def _bar_open(bar: dict[str, Any]) -> float:
    for key in ("o", "open", "open_price"):
        if bar.get(key) is not None:
            return float(bar[key])
    return _bar_close(bar)


def _bar_time_str(bar: dict[str, Any]) -> str:
    raw = bar.get("t") or bar.get("time") or bar.get("trade_time") or bar.get("timestamp") or ""
    if isinstance(raw, time):
        return raw.strftime("%H:%M:%S")
    return str(raw)


def _time_at_or_after(ts: str, cutoff: str) -> bool:
    try:
        return ts[:5] >= cutoff[:5]
    except Exception:
        return False


def _is_reversal_confirmed(bars: list[dict[str, Any]], min_body_pct: float) -> bool:
    if len(bars) < 2:
        return False
    prev = bars[-2]
    cur = bars[-1]
    prev_close = _bar_close(prev)
    cur_open = _bar_open(cur)
    cur_close = _bar_close(cur)
    if cur_close <= cur_open or cur_close <= prev_close:
        return False
    body_pct = (cur_close - cur_open) / cur_open * 100.0 if cur_open > 0 else 0.0
    return body_pct >= min_body_pct


def _is_bearish_reversal(bars: list[dict[str, Any]]) -> bool:
    if len(bars) < 2:
        return False
    prev = bars[-2]
    cur = bars[-1]
    return _bar_close(cur) < _bar_open(cur) and _bar_close(cur) < _bar_close(prev)
