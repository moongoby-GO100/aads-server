"""
ScalpingEntryEngine: 실시간 틱 기반 스캘핑 매수 진입 엔진

- ScalpingMonitor(청산)와 동일 프로세스, 별도 Queue로 병렬 실행
- 전략카드(scalping=true)의 entry_rules를 틱 데이터로 실시간 평가
- 진입 조건 충족 시 즉시 시장가 매수 → Redis로 ScalpingMonitor에 포지션 전달
- 안전장치: 동시 보유 종목 수(카드별 max_stocks), 동시 진입 방지 Lock, 장 시간 제한
  ※ 2026-08-06 CEO 지시: 일일 매수횟수(MAX_DAILY_BUYS) 기반 차단 제거.
     max_stocks(동시 5종목)와 DUPBLOCK/재진입쿨다운 등 안전장치는 유지.

진입 조건 (틱 기반):
  - volume_spike: 최근 N틱 평균 대비 X배 이상 거래량
  - strength_threshold: 체결강도 > threshold (매수세 우위)
  - price_breakout: 직전 고가 돌파
  - momentum: 연속 상승 틱 N개 이상
"""

import asyncio
import json
import logging
import os
import sys
import time as time_module
import uuid
from collections import defaultdict, deque
from datetime import date, datetime, time as dt_time
from typing import Optional

import psycopg2
import redis as sync_redis

from backend.app.services.go100.monitoring.realtime_data_quality_gate import evaluate_realtime_data_quality
from backend.app.services.go100.data.backfill_orchestrator import (
    LIMITUP_REASON_FEATURES_SHADOW,
    orchestrate_data_backfill,
)
from backend.app.services.go100.strategies.card303_discovery import (
    CARD303_DISCOVERY_CONTRACT_VERSION,
    CARD303_DISCOVERY_LIMIT,
    CARD303_DISCOVERY_MAX_CHANGE_PCT,
    CARD303_DISCOVERY_MIN_CHANGE_PCT,
    CARD303_DISCOVERY_MIN_TRADING_VALUE_KRW,
    CARD303_DISCOVERY_REEVALUATION_SECONDS,
    CARD303_DISCOVERY_SNAPSHOT_FRESH_MINUTES,
)
from backend.app.core.strategy_config import (
    SCALPING_DEFAULT_MAX_STOCKS,
    SCALPING_DEFAULT_TP_PCT,
    SCALPING_DEFAULT_SL_PCT,
    SCALPING_DEFAULT_STRENGTH_THRESHOLD,
    SCALPING_DEFAULT_VOLUME_MULTIPLIER,
    SCALPING_DEFAULT_MIN_MOMENTUM_TICKS,
)

try:
    from backend.app.services.go100.analysis.ma_wave_engine import MAWaveEngine
    _MA_WAVE_ENGINE = MAWaveEngine()
except ImportError:
    _MA_WAVE_ENGINE = None

try:
    from backend.app.services.go100.analysis.wave_event_handler import (
        ReorderLogic,
        SlippageModel,
        WaveEventHandler,
    )
    _WAVE_EVENT_HANDLER = WaveEventHandler()
    _WAVE_SLIPPAGE_MODEL = SlippageModel()
    _WAVE_REORDER_LOGIC = ReorderLogic()
except ImportError:
    _WAVE_EVENT_HANDLER = None
    _WAVE_SLIPPAGE_MODEL = None
    _WAVE_REORDER_LOGIC = None

try:
    from backend.app.services.go100.analysis.wave_failure_detector import WaveFailureDetector
    _WAVE_FAILURE_DETECTOR = WaveFailureDetector()
except ImportError:
    _WAVE_FAILURE_DETECTOR = None

try:
    from backend.app.services.go100.analysis.trend_continuity_tracker import TrendContinuityTracker
    _TREND_CONTINUITY_TRACKER = TrendContinuityTracker()
except ImportError:
    _TREND_CONTINUITY_TRACKER = None

try:
    from backend.app.services.go100.analysis.bearish_wave_analyzer import BearishWaveAnalyzer
    _BEARISH_WAVE_ANALYZER = BearishWaveAnalyzer()
except ImportError:
    _BEARISH_WAVE_ANALYZER = None

try:
    from backend.app.services.go100.analysis.daily_trend_filter import DailyTrendFilter
    _DAILY_TREND_FILTER = DailyTrendFilter()
except ImportError:
    _DAILY_TREND_FILTER = None

try:
    from backend.app.services.go100.analysis.portfolio_risk_manager import PortfolioRiskManager
    _WAVE_PORTFOLIO_RISK_MANAGER = PortfolioRiskManager()
except ImportError:
    _WAVE_PORTFOLIO_RISK_MANAGER = None

try:
    from backend.app.services.go100.analysis.wave_counter import WaveCounter
    from backend.app.services.go100.analysis.wave_measurer import WaveMeasurer
    from backend.app.services.go100.analysis.wave_probability_model import (
        WaveProbabilityCalibrator,
        WaveProbabilityModel,
    )
    from backend.app.services.go100.analysis.wave_ml_predictor import WaveMLPredictor
    from backend.app.services.go100.analysis.mtf_analyzer import (
        MultiTimeframeAnalyzer,
        build_fractal_position_features,
        build_intraday_session_wave_features,
        confirm_rebound_candle,
        detect_fractal_pivot_lows,
        detect_mtf_fractal_confluence,
        filter_regular_session_bars,
    )
    _WAVE_COUNTER = WaveCounter()
    _WAVE_MEASURER = WaveMeasurer()
    _WAVE_PROB_MODEL = WaveProbabilityModel()
    _WAVE_MTF_ANALYZER = MultiTimeframeAnalyzer()
    _WAVE_ML_PREDICTOR = WaveMLPredictor()
    _WAVE_CALIBRATOR_CLS = WaveProbabilityCalibrator
except ImportError:
    _WAVE_CALIBRATOR_CLS = None
    _WAVE_COUNTER = None
    _WAVE_MEASURER = None
    _WAVE_PROB_MODEL = None
    _WAVE_MTF_ANALYZER = None
    _WAVE_ML_PREDICTOR = None
    build_intraday_session_wave_features = None
    filter_regular_session_bars = None

logger = logging.getLogger("scalping_entry_engine")

_WAVE_STATE_CACHE: dict[str, dict] = {}


def get_realtime_wave_state(stock_code: str) -> dict | None:
    """실시간 파동 상태 조회 (API용). 마지막 평가 시점의 캐시 반환."""
    return _WAVE_STATE_CACHE.get(stock_code)


def get_all_wave_states() -> dict[str, dict]:
    """모든 종목의 파동 상태 캐시 반환."""
    return dict(_WAVE_STATE_CACHE)


MARKET_OPEN = dt_time(9, 0, 0)   # CEO 지시: 정규장 시작부터 실매매 테스트 허용
MARKET_CLOSE = dt_time(15, 30, 0)  # CEO 지시: 정규장 종료까지 실매매 테스트 허용

# NXT 시간대 상수 (스캘핑 엔진 NXT 진입 지원)
NXT_PRE_OPEN = dt_time(8, 0, 0)
NXT_PRE_CLOSE = dt_time(8, 50, 0)
NXT_AFTER_OPEN = dt_time(15, 40, 0)
NXT_AFTER_CLOSE = dt_time(20, 0, 0)
_NXT_ORDER_MAX_TICK_AGE_SEC = 30.0
_CARD119_ENTRY_MIN_CHANGE_PCT = float(os.environ.get("GO100_CARD119_ENTRY_MIN_CHANGE_PCT", "27.0"))


def _tick_age_seconds(tick_time) -> float | None:
    """Return tick age in KST, or None when the tick has no usable timestamp."""
    if not isinstance(tick_time, datetime):
        return None
    try:
        from zoneinfo import ZoneInfo

        kst = ZoneInfo("Asia/Seoul")
        if tick_time.tzinfo is None:
            tick_time = tick_time.replace(tzinfo=kst)
        else:
            tick_time = tick_time.astimezone(kst)
        age = (datetime.now(kst) - tick_time).total_seconds()
        return max(0.0, age)
    except Exception:
        return None


def _as_bool(value, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {
        "1", "true", "yes", "y", "on",
    }


def _wave_feature_enabled(env_name: str) -> bool:
    """Read W1 rollout flags at decision time so rollback does not need code changes."""
    return _as_bool(os.environ.get(env_name), True)


_KRX_TICK_TABLE = (
    (2_000, 1), (5_000, 5), (20_000, 10), (50_000, 50),
    (200_000, 100), (500_000, 500),
)


def _krx_tick_size(price: float) -> int:
    for bound, tick in _KRX_TICK_TABLE:
        if price < bound:
            return tick
    return 1_000


def _is_card303_one_share_live_override(card: dict) -> bool:
    if int(card.get("card_id") or 0) != 303:
        return False
    if card.get("account_is_mock") is True:
        return False
    risk_params = card.get("risk_params") if isinstance(card.get("risk_params"), dict) else {}
    strategy_params = card.get("strategy_params") if isinstance(card.get("strategy_params"), dict) else {}
    metadata = card.get("metadata") if isinstance(card.get("metadata"), dict) else {}
    fixed_qty = (
        card.get("fixed_quantity")
        or risk_params.get("fixed_quantity")
        or strategy_params.get("fixed_quantity")
        or 0
    )
    try:
        fixed_qty_int = int(fixed_qty or 0)
    except (TypeError, ValueError):
        fixed_qty_int = 0
    return (
        fixed_qty_int == 1
        and (
            _as_bool(risk_params.get("live_test_limit_override"), False)
            or _as_bool(strategy_params.get("live_test_limit_override"), False)
            or _as_bool(metadata.get("live_test_limit_override"), False)
        )
    )


def _is_real_buy_hard_blocked(card: dict) -> bool:
    block_env = os.environ.get("GO100_SCALPING_REAL_BUY_BLOCK", "true").strip().lower()
    is_real_buy_target = card.get("account_is_mock") is not True
    return (
        is_real_buy_target
        and block_env != "false"
        and not _is_card303_one_share_live_override(card)
    )


def _build_buy_lock_keys(account_id, card_id, stock_code, trade_date) -> tuple[str, str]:
    """Return the account-stock global lock and the existing card-scoped lock."""
    trade_date_text = (
        trade_date.isoformat() if hasattr(trade_date, "isoformat") else str(trade_date)
    )
    return (
        f"scalping:buy_lock_global:{account_id}:{stock_code}:{trade_date_text}",
        f"scalping:buy_lock:{account_id}:{card_id}:{stock_code}:{trade_date_text}",
    )


_RELEASE_BUY_LOCK_SCRIPT = """
if redis.call('get', KEYS[1]) == ARGV[1] then
    return redis.call('del', KEYS[1])
end
return 0
"""


def _release_redis_lock_if_owned(redis_client, lock_key: str, lock_token: str) -> bool:
    """Release a lock only when its value still belongs to this buy attempt."""
    try:
        eval_fn = getattr(redis_client, "eval", None)
        if callable(eval_fn):
            try:
                eval_fn(_RELEASE_BUY_LOCK_SCRIPT, 1, lock_key, lock_token)
                return True
            except Exception:
                # Older/minimal Redis clients may expose get/delete but not EVAL.
                pass

        get_fn = getattr(redis_client, "get", None)
        delete_fn = getattr(redis_client, "delete", None)
        if not callable(get_fn) or not callable(delete_fn):
            logger.warning(
                "DUPBLOCK: Redis get/delete unavailable for global lock %s; "
                "relying on 120s TTL (residual pending-window risk)",
                lock_key,
            )
            return False
        current_value = get_fn(lock_key)
        if isinstance(current_value, bytes):
            current_value = current_value.decode("utf-8", errors="replace")
        if current_value != lock_token:
            return False
        delete_fn(lock_key)
        return True
    except Exception as release_error:
        logger.warning(
            "DUPBLOCK: could not safely release global lock %s (%s); "
            "relying on 120s TTL (residual pending-window risk)",
            lock_key,
            release_error,
        )
        return False


def _is_scalping_nxt_entry_enabled() -> bool:
    return _as_bool(os.environ.get("GO100_SCALPING_NXT_ENTRY_ENABLED"), False)


def _is_scalping_nxt_pm_entry_enabled() -> bool:
    return _as_bool(os.environ.get("GO100_SCALPING_NXT_PM_ENTRY_ENABLED"), False)


# [2026-08-06 CEO 지시] 일일 매수횟수 제한 없음 — 이 값은 감사로그 기록용으로만 보존.
# _is_entry_allowed()에서 더 이상 이 값으로 매수를 차단하지 않는다.
MAX_DAILY_BUYS = 0  # 0 = disabled (audit/monitoring only)
_CONSUME_TIMEOUT_SEC = 1.0
_PRIORITY_DRAIN_LIMIT = int(os.environ.get("SCALPING_PRIORITY_DRAIN_LIMIT", "500"))
_PRIORITY_YIELD_EVERY_TICKS = int(os.environ.get("SCALPING_PRIORITY_YIELD_EVERY_TICKS", "50"))
# [P0-DYNUNI] CEO 지시: 유니버스/카드 변경을 실시간 동적으로 반영하기 위해 300→60s 단축.
# 카드 변경은 _check_config_changed() 이벤트로 즉시 반영되지만, 새로 추가된 종목/제거된 종목의
# 키움 WS 재구독은 _load_universe() 주기에 묶여 있으므로 폴링 주기를 짧게 가져간다.
_UNIVERSE_RELOAD_SEC = CARD303_DISCOVERY_REEVALUATION_SECONDS
_POSITION_RELOAD_SEC = 30.0
_CARD_RELOAD_SEC = 60.0
_CARD_STALE_FAIL_LIMIT = 3
_REENTRY_COOLDOWN_SEC = int(os.environ.get("SCALPING_REENTRY_COOLDOWN_SEC", "1800"))

# [GO100-303] 1분봉 파동 게이트 복구 정책.
# 파동 판단에 필요한 당일 1분봉이 부족하거나 구조가 미검출되면 진입은 fail-closed로 막고,
# 같은 틱에서 best-effort 백필/DB 재수화를 시도한다. 복구되면 다음 틱 평가에서 재진입 후보가 된다.
_WAVE_DATA_RECOVERY_ENABLED = _as_bool(os.environ.get("GO100_303_WAVE_RECOVERY_ENABLED"), True)
_WAVE_DATA_RECOVERY_COOLDOWN_SEC = float(os.environ.get("GO100_303_WAVE_RECOVERY_COOLDOWN_SEC", "30"))
_WAVE_DATA_DB_CACHE_SEC = float(os.environ.get("GO100_303_WAVE_DB_CACHE_SEC", "10"))
_WAVE_BAR_SOURCE_MODE = os.environ.get(
    "GO100_303_WAVE_BAR_SOURCE_MODE", "db_shard_preferred",
).strip().lower()
if _WAVE_BAR_SOURCE_MODE not in {"db_shard_preferred", "db_shard_only", "ws_memory_legacy"}:
    _WAVE_BAR_SOURCE_MODE = "db_shard_preferred"
_WAVE_DATA_RECOVERY_STATUSES = {
    "warmup_blocked",
    "wave_peak_not_fixed",
    "invalid_wave_prices",
    "ma_wave_warmup_blocked",
    "db_shard_data_insufficient",
    "ma_wave_db_shard_insufficient",
}
# [GO100-303] 정규장 09:00~15:30 일중 파동 추적용 1분봉 보관 한도.
# 390분 정규장 + 지연/보강 여유 30분. 기존 60개 버퍼는 장 시작 원점 추적에 부족했다.
_SESSION_WAVE_BUFFER_BARS = int(os.environ.get("GO100_303_SESSION_WAVE_BUFFER_BARS", "420"))


# [P0-1b] ML 게이트 우회 하드코딩 교정.
# 기존 코드는 predictor가 계산한 gate_pass를 버리고 엔진에서 `ml_win < 25.0`을
# 하드코딩 비교했다. 그 결과 MLGateConfig.min_win_floor(기본 35%)가 실매매에서
# 전혀 적용되지 않았고, predict() 예외 시에는 기본값 50.0으로 무조건 통과(fail-open)했다.
#   - strict(기본): predictor의 gate_pass 판정을 그대로 따른다.
#   - legacy: 기존 25% 하드코딩 동작 (즉시 롤백용).
# 모델 파일 미배포(model_loaded=False)는 매매 전면 중단을 피하기 위해 통과시키되
# metrics에 사유를 남긴다. 예외 발생은 기본 차단이며 FAIL_OPEN=1로 완화할 수 있다.
_WAVE_ML_GATE_MODE = os.environ.get("GO100_WAVE_ML_GATE_MODE", "strict").strip().lower()
_WAVE_ML_GATE_FAIL_OPEN = os.environ.get("GO100_WAVE_ML_FAIL_OPEN", "0").strip() == "1"
_WAVE_ML_LEGACY_MIN_WIN = float(os.environ.get("GO100_WAVE_ML_LEGACY_MIN_WIN", "25.0"))


# [P1-2] WaveProbabilityCalibrator 실전 연동.
# go100_wave_factor_accuracy에는 2026-08-25 기준 49행/15팩터/8,808,405 샘플이
# 매일 20:30(wave_factor_stats.py) 갱신되며 축적되고 있었으나,
# WaveProbabilityCalibrator를 인스턴스화하는 호출처가 코드 전체에 한 곳도 없어
# 축적된 데이터가 전혀 소비되지 않았다.
#
# 실매매 확률을 즉시 바꾸는 것은 위험하므로 기본은 shadow 모드다.
#   shadow(기본): raw/보정 확률을 metrics에만 기록하고 판정은 바꾸지 않는다.
#   active      : 보정 확률을 실제 판정에 사용한다. (GO100_WAVE_CALIBRATOR_MODE=active)
_WAVE_CALIBRATOR_MODE = os.environ.get("GO100_WAVE_CALIBRATOR_MODE", "shadow").strip().lower()
_WAVE_CALIBRATOR_TTL_SEC = int(os.environ.get("GO100_WAVE_CALIBRATOR_TTL_SEC", "3600"))
_WAVE_CALIBRATOR_CACHE: dict = {"at": 0.0, "obj": None, "rows": 0}


def load_wave_factor_accuracy(db_params: dict) -> dict:
    """go100_wave_factor_accuracy → Calibrator가 기대하는 중첩 dict로 변환."""
    acc: dict = {}
    conn = None
    try:
        conn = psycopg2.connect(**db_params)
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT factor_name, score_bucket, sample_count,
                       actual_accuracy, current_weight
                FROM go100_wave_factor_accuracy
                WHERE sample_count > 0
                """
            )
            for factor_name, bucket, sample_count, accuracy, weight in cur.fetchall():
                acc.setdefault(factor_name, {})[bucket] = {
                    "accuracy": float(accuracy or 0.0),
                    "sample_count": int(sample_count or 0),
                    "weight": float(weight or 0.0),
                }
    except Exception as exc:
        logger.warning("wave factor accuracy load failed: %s", exc)
        return {}
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass
    return acc


def get_wave_calibrator(db_params: dict, now_ts: Optional[float] = None):
    """TTL 캐시된 Calibrator 인스턴스. 미탑재/데이터 없음이면 None."""
    if _WAVE_CALIBRATOR_CLS is None:
        return None
    ts = now_ts if now_ts is not None else time_module.time()
    if (
        _WAVE_CALIBRATOR_CACHE["obj"] is not None
        and ts - _WAVE_CALIBRATOR_CACHE["at"] < _WAVE_CALIBRATOR_TTL_SEC
    ):
        return _WAVE_CALIBRATOR_CACHE["obj"]
    acc = load_wave_factor_accuracy(db_params)
    obj = _WAVE_CALIBRATOR_CLS(acc) if acc else None
    _WAVE_CALIBRATOR_CACHE.update({"at": ts, "obj": obj, "rows": len(acc)})
    if obj is not None:
        logger.info("wave calibrator loaded: %s factors (mode=%s)",
                    len(acc), _WAVE_CALIBRATOR_MODE)
    return obj


def apply_wave_calibration(
    metrics: dict,
    raw_probability: float,
    factors: Optional[dict],
    calibrator,
    mode: Optional[str] = None,
) -> float:
    """보정 확률을 metrics에 기록하고, active 모드에서만 실제 값으로 반환한다."""
    metrics["pullback_prob_raw"] = raw_probability
    if calibrator is None or not factors:
        metrics["calibrator_status"] = "unavailable"
        return raw_probability
    try:
        calibrated = calibrator.calibrate(raw_probability, factors)
    except Exception as exc:
        metrics["calibrator_status"] = f"error:{str(exc)[:80]}"
        return raw_probability
    metrics["pullback_prob_calibrated"] = calibrated
    effective_mode = (mode or _WAVE_CALIBRATOR_MODE).lower()
    if effective_mode == "active":
        metrics["calibrator_status"] = "active"
        return calibrated
    metrics["calibrator_status"] = "shadow"
    return raw_probability


def evaluate_wave_ml_gate(metrics: dict) -> tuple[bool, str]:
    """ML 게이트 통과 여부와 사유를 판정한다.

    Returns:
        (passed, status) — status는 차단 시 metrics['ma_wave_status']에 기록된다.
    """
    mode = str(metrics.get("_ml_gate_mode") or _WAVE_ML_GATE_MODE).lower()
    fail_open = bool(metrics.get("_ml_gate_fail_open", _WAVE_ML_GATE_FAIL_OPEN))

    if mode == "legacy":
        ml_win = metrics.get("ml_win_probability", 50.0)
        if ml_win < _WAVE_ML_LEGACY_MIN_WIN:
            return False, "ml_low_win_probability"
        return True, "ml_gate_legacy_pass"

    if metrics.get("ml_gate_error"):
        if fail_open:
            return True, "ml_gate_error_fail_open"
        return False, "ml_gate_error"

    if "ml_model_loaded" not in metrics:
        # predictor 미탑재(_WAVE_ML_PREDICTOR is None) — ML 게이트 미적용 구성.
        return True, "ml_gate_skipped_no_predictor"

    if not metrics.get("ml_model_loaded"):
        return True, "ml_gate_skipped_model_not_loaded"

    if not metrics.get("ml_gate_pass"):
        return False, "ml_gate_blocked"

    return True, "ml_gate_pass"


# [GO100-303] 장초반 초강한 1파 예외.
# MA20 warmup(21봉 미만)이 먼저 차단하면 09:00 직후 1파->눌림->2파 전환을 놓친다.
# 이 시간대에는 MA 지지 판단을 통과 처리하지 않고, 뒤의 1분봉 파동 게이트가 최종 판정한다.
_OPENING_FAST_WAVE_MIN_BARS = int(os.environ.get("GO100_303_OPENING_FAST_WAVE_MIN_BARS", "4"))
_OPENING_FAST_WAVE_NXT_END = os.environ.get("GO100_303_OPENING_FAST_WAVE_NXT_END", "0812")
_OPENING_FAST_WAVE_REGULAR_END = os.environ.get("GO100_303_OPENING_FAST_WAVE_REGULAR_END", "0930")


def _parse_hhmm_time(text: str, fallback: dt_time) -> dt_time:
    raw = str(text or "").strip().replace(":", "")
    if len(raw) != 4 or not raw.isdigit():
        return fallback
    return dt_time(int(raw[:2]), int(raw[2:]), 0)


def _bar_minute_to_time(value, fallback: dt_time | None = None) -> dt_time | None:
    if isinstance(value, datetime):
        return value.time().replace(second=0, microsecond=0)
    raw = str(value or "").strip().replace(":", "")
    if len(raw) >= 4 and raw[:4].isdigit():
        return dt_time(int(raw[:2]), int(raw[2:4]), 0)
    return fallback


def _opening_fast_wave_active_for_bars(bars: list[dict], rule: dict) -> bool:
    if not _as_bool(rule.get("opening_fast_wave_enabled"), True):
        return False
    if len(bars) < int(rule.get("opening_fast_wave_min_bars", _OPENING_FAST_WAVE_MIN_BARS)):
        return False
    last_time = _bar_minute_to_time(bars[-1].get("minute") or bars[-1].get("bar_time"))
    if last_time is None:
        return False
    regular_end = _parse_hhmm_time(
        rule.get("opening_fast_wave_regular_end", _OPENING_FAST_WAVE_REGULAR_END),
        dt_time(9, 30, 0),
    )
    return dt_time(9, 0, 0) <= last_time <= regular_end


def _detect_opening_fast_wave_pair(
    bars: list[dict],
    price: float,
    rule: dict,
    min_wave_gain: float,
    min_rebound: float,
    near_peak_buffer: float,
) -> dict | None:
    """Detect W1 high -> W2 low -> bullish rebound before slow pivots settle."""
    if len(bars) < int(rule.get("opening_fast_wave_min_bars", _OPENING_FAST_WAVE_MIN_BARS)):
        return None
    min_pullback = abs(float(rule.get("opening_fast_wave_min_pullback_pct", 0.25))) / 100.0
    max_pullback = abs(float(rule.get("opening_fast_wave_max_pullback_pct", rule.get("wave_max_pullback_pct", 3.0)))) / 100.0
    best = None
    for peak_idx in range(1, len(bars) - 1):
        try:
            wave1_high = float(bars[peak_idx].get("h") or bars[peak_idx].get("high") or 0)
            starts = [
                float(bar.get("o") or bar.get("open") or bar.get("l") or bar.get("low") or bar.get("c") or 0)
                for bar in bars[:peak_idx + 1]
            ]
        except (TypeError, ValueError):
            continue
        starts = [value for value in starts if value > 0]
        if wave1_high <= 0 or not starts:
            continue
        wave1_start = min(starts)
        wave_gain = (wave1_high - wave1_start) / wave1_start
        if wave_gain < min_wave_gain:
            continue
        post_peak = list(bars[peak_idx + 1:])
        try:
            trough_offset = min(
                range(len(post_peak)),
                key=lambda i: float(post_peak[i].get("l") or post_peak[i].get("low") or post_peak[i].get("c") or 0),
            )
            trough_idx = peak_idx + 1 + trough_offset
            pullback_low = float(
                post_peak[trough_offset].get("l")
                or post_peak[trough_offset].get("low")
                or post_peak[trough_offset].get("c")
                or 0
            )
        except (TypeError, ValueError):
            continue
        if pullback_low <= 0 or pullback_low >= wave1_high:
            continue
        pullback_depth = (wave1_high - pullback_low) / wave1_high
        rebound = (float(price) - pullback_low) / pullback_low
        if pullback_depth < min_pullback or pullback_depth > max_pullback:
            continue
        if rebound < min_rebound or float(price) > wave1_high * (1 + near_peak_buffer):
            continue
        current_bar = bars[-1]
        current_open = float(current_bar.get("o") or current_bar.get("open") or price)
        previous_close = float(bars[-2].get("c") or bars[-2].get("close") or 0) if len(bars) >= 2 else 0.0
        if float(price) <= current_open or (previous_close > 0 and float(price) < previous_close):
            continue
        candidate = {
            "peak_idx": peak_idx,
            "wave1_start": wave1_start,
            "wave1_high": wave1_high,
            "pullback_low_idx": trough_idx,
            "pullback_low": pullback_low,
            "bars_after_pullback_low": len(bars) - trough_idx - 1,
            "wave_gain_pct": round(wave_gain * 100, 3),
            "pullback_depth_pct": round(pullback_depth * 100, 3),
            "rebound_from_pullback_pct": round(rebound * 100, 3),
        }
        if best is None or (candidate["pullback_low_idx"], candidate["peak_idx"]) > (
            best["pullback_low_idx"], best["peak_idx"]
        ):
            best = candidate
    return best

# [P0-FLAT-COOLDOWN] 보합(가격 무변화) 틱 반복 평가 방지 쿨다운.
# 같은 종목 + 직전 평가 가격 대비 변동률 < FLAT_BPS 이고 경과 시간 < FLAT_SEC 이면
# 카드 평가 루프 진입 전에 드롭한다. 키움 0B 체결틱은 동일가 반복이 다수 발생하므로
# 이로 인해 카드별 entry_rule 평가가 무의미하게 폭주하고 audit DB INSERT가 누적된다.
_FLAT_COOLDOWN_SEC = float(os.environ.get("SCALPING_FLAT_COOLDOWN_SEC", "1.0"))
_FLAT_COOLDOWN_BPS = float(os.environ.get("SCALPING_FLAT_COOLDOWN_BPS", "5"))  # 5bps = 0.05%

# [P2] lock_score: 진입 품질 점수 하한 (0~100). 이하이면 진입 스킵.
_MIN_LOCK_SCORE = 30.0

# [GO100-303] 계좌 인증 오류(90070000) 회로 차단기
# 동일 (card_id, account_id) 에서 90070000 계열 오류가 연속 N회 발생하면
# 해당 카드·계좌 조합의 주문 실행을 당일 프로세스 재시작 시까지 차단.
_ACCOUNT_AUTH_ERR_THRESHOLD = int(os.environ.get("SCALPING_AUTH_ERR_THRESHOLD", "3"))

# [P2-우선진입] 카드 슬롯 점유율에 비례해 lock_score 하한을 동적 상향한다.
# 슬롯이 찰수록(=남은 자리가 적을수록) 더 높은 품질 종목만 통과시켜
# 한정된 슬롯을 고품질 종목에 우선 배정한다(틱 기반 우선진입 근사).
_SLOT_PRIORITY_BONUS = float(os.environ.get("GO100_SCALPING_SLOT_PRIORITY_BONUS", "40"))

# [GO100-131] 중앙 경쟁 엔진. 기본은 shadow라 기존 first-signal 실행 흐름을 보존한다.
# enforce로 바꾸면 같은 틱에서 통과한 복수 전략카드 후보 중 최고 점수 1건만 주문 경로로 보낸다.
_COMPETITION_MODE = os.environ.get("GO100_COMPETITION_ENGINE_MODE", "shadow").strip().lower()
_COMPETITION_MIN_SCORE = float(os.environ.get("GO100_COMPETITION_MIN_SCORE", "0"))
_COMPETITION_LOG_LOSERS = _as_bool(os.environ.get("GO100_COMPETITION_LOG_LOSERS"), True)

# 직접 WS 레거시 경로에서만 구독 대상을 상위 유동성 종목으로 제한한다.
# GO100_SCALPING_UNIVERSE_LIMIT — 레거시 직접 WS 유니버스 상한 (신규 정식 이름).
#   GO100_SCALPING_WS_UNIVERSE_LIMIT(구 이름)을 fallback으로 읽는다.
#   반드시 KIWOOM_SCALPING_STABLE_MAX_CODES(WS 구독 안정 한도) 이하여야 하므로
#   min()으로 양쪽 모두 존중한다. 기본값: 50 (#303 누적 거래대금 상위 50 감시 기준).
_WS_UNIVERSE_LIMIT = min(
    int(
        os.environ.get("GO100_SCALPING_UNIVERSE_LIMIT")
        or os.environ.get("GO100_SCALPING_WS_UNIVERSE_LIMIT")
        or os.environ.get("KIWOOM_SCALPING_EFFECTIVE_MAX_CODES", "80")
    ),
    int(os.environ.get("KIWOOM_SCALPING_STABLE_MAX_CODES", "50")),
)
# The entry/wave engine must not own a broker WS account in the default path.
# Collector shards own subscriptions and persist ticks/minute bars; direct
# subscription mutation is available only as an explicit legacy rollback flag.
_DIRECT_WS_SUBSCRIPTION_SYNC_ENABLED = _as_bool(
    os.environ.get("GO100_SCALPING_DIRECT_WS_SUBSCRIPTION_SYNC_ENABLED"), False,
)
_COLLECTOR_EVALUATION_UNIVERSE_LIMIT = max(
    0, int(os.environ.get("GO100_SCALPING_COLLECTOR_EVALUATION_UNIVERSE_LIMIT", "0")),
)
# [P0-4 2026-08-06] 키움 WS 유니버스 재구독 트리거 제어값.
# _RESUB_MIN_ADDED: 신규 편입 종목이 이 수 이상일 때만 재연결을 트리거한다.
# _RESUB_COOLDOWN_SEC: 재연결 트리거 최소 간격(초). 1006 반복 종료 방지용.
_RESUB_MIN_ADDED = int(os.environ.get("GO100_KIWOOM_RESUB_MIN_ADDED", "3"))
_RESUB_COOLDOWN_SEC = float(os.environ.get("GO100_KIWOOM_RESUB_COOLDOWN_SEC", "900"))

# 상한가 추적형 카드는 카드 평가 전 단계에서 사라진 접근 종목도 감사 로그에 남긴다.
_LIMIT_UP_WATCH_FLOOR_PCT = float(os.environ.get("GO100_LIMIT_UP_AUDIT_WATCH_FLOOR_PCT", "20"))
_LIMIT_UP_SNAPSHOT_WINDOW_MIN = int(os.environ.get("GO100_LIMIT_UP_SNAPSHOT_WINDOW_MIN", "10"))
_LIMIT_UP_FORCE_INCLUDE_PCT = float(os.environ.get("GO100_LIMIT_UP_FORCE_INCLUDE_PCT", "20"))

# [GO100-119] 과거 상한가 잠김/익일 갭 공통조건 기반 실매매 게이트.
# 뉴스/호가/VI처럼 아직 수집되지 않은 변수는 차단 조건으로 쓰지 않고,
# 현재 shadow 테이블에 있는 테마·시장레짐·시간대별 체결강도만 fail-closed 평가한다.
_LIMITUP119_LEARNING_GATE_ENABLED = _as_bool(os.environ.get("GO100_119_LEARNING_GATE_ENABLED"), True)
_LIMITUP119_MIN_THEME_PEER_AVG_CHANGE_PCT = float(os.environ.get("GO100_119_MIN_THEME_PEER_AVG_CHANGE_PCT", "35"))
_LIMITUP119_MIN_VOLUME_BURST_RATIO_5M = float(os.environ.get("GO100_119_MIN_VOLUME_BURST_RATIO_5M", "10"))
_LIMITUP119_MAX_VKOSPI = float(os.environ.get("GO100_119_MAX_VKOSPI", "60"))
_LIMITUP119_MIN_REGIME_SCORE = float(os.environ.get("GO100_119_MIN_REGIME_SCORE", "45"))
_LIMITUP119_MIN_TIME_BUCKET_STRENGTH = float(os.environ.get("GO100_119_MIN_TIME_BUCKET_STRENGTH", "105"))
_LIMITUP119_REASON_FEATURE_FIELDS = (
    "theme_strength_intraday", "theme_peer_limitup_count", "theme_peer_avg_change_pct",
    "kospi_return_1d", "kosdaq_return_1d", "market_breadth", "vkospi",
    "regime_label", "regime_score",
    "strength_0900_1000", "strength_1000_1100", "strength_after_lock", "volume_burst_ratio_5m",
)
# 종목·날짜별 백필 쿨다운(초): 같은 틱 루프에서 반복 트리거 방지
_REASON_FEATURE_BACKFILL_COOLDOWN_SEC = float(
    os.environ.get("GO100_119_REASON_FEATURE_BACKFILL_COOLDOWN_SEC", "120")
)
# [GO100-119-MATERIAL-AWARE-VALUE-GATE P0]
# 1억(1e8) 이상이면 카드 min_amount_krw 하한 완화 통과.
# 1,000억(1e11) 초과이면 강한 재료 확인 게이트 통과 시에만 진입 (fail-closed).
_LIMITUP119_RELAXED_MIN_TRADE_VALUE = float(
    os.environ.get("GO100_119_RELAXED_MIN_TRADE_VALUE", "1e8")
)   # 1억원
_LIMITUP119_STRONG_MATERIAL_THRESHOLD = float(
    os.environ.get("GO100_119_STRONG_MATERIAL_THRESHOLD", "1e11")
)   # 1,000억원
_CARD119_DISCOVERY_MIN_CHANGE_PCT = max(
    _CARD119_ENTRY_MIN_CHANGE_PCT,
    float(os.environ.get("GO100_CARD119_DISCOVERY_MIN_CHANGE_PCT", str(_CARD119_ENTRY_MIN_CHANGE_PCT))),
)
_CARD119_DISCOVERY_LIMIT = int(os.environ.get("GO100_CARD119_DISCOVERY_LIMIT", "200"))

TICK_HISTORY_SIZE = 50  # 종목별 최근 틱 보관 수

_ETF_PREFIXES = (
    "KODEX", "TIGER", "ACE", "SOL", "KBSTAR", "HANARO", "ARIRANG",
    "KOSEF", "TIMEFOLIO", "RISE", "PLUS", "WON", "TREX", "마이티",
)
_EXCLUDED_NAME_TOKENS = (
    "ETF", "ETN", "레버리지", "인버스", "선물", "채권", "국채", "통안채",
    "스팩", "SPAC", "리츠", "REIT", "정리매매", "관리종목",
    "우선주", "우B", "우C", "증거금100",
)

_PREFERRED_STOCK_SUFFIXES = ("5", "7", "8", "9", "K", "L")


def _is_excluded_security_name(stock_name: str | None, sector: str | None = None, stock_code: str | None = None) -> bool:
    name = (stock_name or "").upper().replace(" ", "")
    sec = (sector or "").upper().replace(" ", "")
    code = (stock_code or "").strip()
    is_foreign_listing = code.startswith("900") and len(code) == 6 and code.isdigit()
    if any(name.startswith(prefix.upper()) for prefix in _ETF_PREFIXES):
        return True
    if any(token.upper() in name or token.upper() in sec for token in _EXCLUDED_NAME_TOKENS):
        return True
    if code and len(code) == 6 and code.isdigit() and not is_foreign_listing and code[-1] in ("5", "7", "8", "9"):
        return True
    # Unknown non-900 9xxxxx KRX codes are treated as structured/non-common-stock products.
    # CEO decision: 900xxx foreign listings stay tradable; liquidity/volume gates handle weak names.
    if (
        code
        and len(code) == 6
        and code.isdigit()
        and code.startswith("9")
        and not is_foreign_listing
        and (not name or name == code)
    ):
        return True
    if name.endswith("우") or name.endswith("우B") or name.endswith("우C"):
        return True
    return False


_ORDERBOOK_REDIS_FRESH_SEC = float(os.environ.get("GO100_SCALPING_ORDERBOOK_REDIS_FRESH_SEC", "5"))
_ORDERBOOK_DB_FRESH_SEC = float(os.environ.get("GO100_SCALPING_ORDERBOOK_DB_FRESH_SEC", "10"))


def _query_orderbook_imbalance_from_db(stock_code: str, metrics: dict) -> Optional[dict]:
    """Return latest DB orderbook imbalance snapshot when it is fresh enough for #304."""
    conn = None
    try:
        conn = psycopg2.connect(**_get_db_params())
        with conn.cursor() as cur:
            ask_cols = ", ".join(f"ask_price_{i}, ask_qty_{i}" for i in range(1, 11))
            bid_cols = ", ".join(f"bid_price_{i}, bid_qty_{i}" for i in range(1, 11))
            cur.execute(
                f"""
                WITH latest_ob AS (
                    (
                        SELECT captured_at, {ask_cols}, {bid_cols}
                        FROM go100_orderbook_snapshot
                        WHERE stock_code = %s
                          AND captured_at >= CURRENT_DATE
                          AND captured_at < CURRENT_DATE + 1
                        ORDER BY captured_at DESC
                        LIMIT 1
                    )
                    UNION ALL
                    (
                        SELECT captured_at, {ask_cols}, {bid_cols}
                        FROM v4_orderbook_realtime
                        WHERE stock_code = %s
                          AND captured_at >= CURRENT_DATE
                          AND captured_at < CURRENT_DATE + 1
                        ORDER BY captured_at DESC
                        LIMIT 1
                    )
                    ORDER BY captured_at DESC
                    LIMIT 1
                )
                SELECT *, EXTRACT(EPOCH FROM ((now() AT TIME ZONE 'Asia/Seoul') - captured_at)) AS age_sec
                FROM latest_ob
                """,
                (stock_code, stock_code),
            )
            row = cur.fetchone()
            if not row:
                metrics["orderbook_db_source"] = "db_miss"
                return None

            captured_at = row[0]
            age_sec = float(row[-1]) if row[-1] is not None else None
            metrics["orderbook_db_captured_at"] = str(captured_at)
            metrics["orderbook_db_age_sec"] = None if age_sec is None else round(age_sec, 1)
            if age_sec is None or age_sec > _ORDERBOOK_DB_FRESH_SEC:
                metrics["orderbook_db_source"] = "db_stale"
                return None

            values = row[1:-1]
            asks = [(float(values[i]), int(values[i + 1])) for i in range(0, 20, 2) if values[i] and values[i + 1]]
            bids = [(float(values[i]), int(values[i + 1])) for i in range(20, 40, 2) if values[i] and values[i + 1]]
            total_ask = sum(qty for _, qty in asks)
            total_bid = sum(qty for _, qty in bids)
            best_ask = min((price for price, _ in asks), default=0.0)
            best_bid = max((price for price, _ in bids), default=0.0)
            if total_ask <= 0 or total_bid <= 0 or best_ask <= 0 or best_bid <= 0:
                metrics["orderbook_db_source"] = "db_invalid_levels"
                return None

            ratio = total_bid / total_ask
            mid = (best_ask + best_bid) / 2.0
            spread_pct = ((best_ask - best_bid) / mid * 100.0) if mid > 0 else 0.0
            if ratio > 1.5:
                score = min((ratio - 1.0) / 0.5, 2.0)
            elif ratio > 1.2:
                score = (ratio - 1.0) / 0.5
            elif ratio < 0.67:
                score = max(-(1.0 / ratio - 1.0) / 0.5, -2.0)
            elif ratio < 0.83:
                score = -(1.0 / ratio - 1.0) / 0.5
            else:
                score = (ratio - 1.0) * 2

            metrics["orderbook_db_source"] = "db_fallback"
            return {"ratio": ratio, "spread_pct": spread_pct, "score": score}
    except Exception as exc:
        metrics["orderbook_db_source"] = "db_error"
        metrics["orderbook_db_error"] = str(exc)[:120]
        return None
    finally:
        if conn:
            try:
                conn.close()
            except Exception:
                pass


def _evaluate_orderbook_imbalance_rule(
    stock_code: str,
    rule: dict,
) -> tuple[Optional[str], str, str, dict]:
    """호가 불균형(orderbook_imbalance) entry_rule 동기 평가.

    우선순위:
      1. Redis go100:scalping:orderbook:{stock_code} (ts 필드 기준 <= FRESH_SEC)
      2. 데이터 없음 / stale → data_quality_block 반환 (감사 로그용)

    반환: (reason_or_None, reason_code, reason_text, metrics)
    """
    buy_ratio_min = float(rule.get("buy_ratio_min") or 0.0)
    spread_pct_max = float(rule.get("spread_pct_max") or 999.0)
    imbalance_score_min = float(rule.get("imbalance_score_min") or 0.0)

    metrics: dict = {
        "orderbook_rule_buy_ratio_min": buy_ratio_min,
        "orderbook_rule_spread_pct_max": spread_pct_max,
        "orderbook_rule_imbalance_score_min": imbalance_score_min,
    }

    try:
        r = sync_redis.from_url(
            os.environ.get("REDIS_URL", "redis://localhost:6379/0"),
            decode_responses=True,
            socket_timeout=0.3,
        )
        key = f"go100:scalping:orderbook:{stock_code}"
        ob = r.hgetall(key)
        orderbook_source = "redis"
        if not ob:
            metrics["orderbook_source"] = "redis_miss"
            db_ob = _query_orderbook_imbalance_from_db(stock_code, metrics)
            if db_ob:
                orderbook_source = "db_fallback"
                ob = db_ob
            else:
                return None, "data_quality_block", f"호가 불균형 데이터 없음({stock_code}): Redis/DB 최신 호가 미존재", metrics

        ts_str = ob.get("ts", "")
        if ts_str:
            try:
                ts_dt = datetime.fromisoformat(ts_str)
                age_sec = (datetime.now() - ts_dt.replace(tzinfo=None)).total_seconds()
                metrics["orderbook_age_sec"] = round(age_sec, 1)
                if age_sec > _ORDERBOOK_REDIS_FRESH_SEC:
                    metrics["orderbook_source"] = "redis_stale"
                    db_ob = _query_orderbook_imbalance_from_db(stock_code, metrics)
                    if db_ob:
                        orderbook_source = "db_fallback_after_redis_stale"
                        ob = db_ob
                    else:
                        return None, "data_quality_block", (
                            f"호가 불균형 데이터 stale({stock_code}): age={age_sec:.1f}s > {_ORDERBOOK_REDIS_FRESH_SEC}s; DB 최신 호가 미존재"
                        ), metrics
            except ValueError:
                metrics["orderbook_source"] = "redis_ts_invalid"
                db_ob = _query_orderbook_imbalance_from_db(stock_code, metrics)
                if db_ob:
                    orderbook_source = "db_fallback_after_redis_ts_invalid"
                    ob = db_ob

        ratio = float(ob.get("ratio") or 0)
        spread_pct = float(ob.get("spread_pct") or 0)
        score = float(ob.get("score") or 0)
        metrics.update({
            "orderbook_source": orderbook_source,
            "orderbook_bid_ask_ratio": ratio,
            "orderbook_spread_pct": spread_pct,
            "orderbook_imbalance_score": score,
        })

        if buy_ratio_min > 0 and ratio < buy_ratio_min:
            return None, "orderbook_ratio_below_min", (
                f"호가 매수비율 {ratio:.1f} < 기준 {buy_ratio_min:.1f}"
            ), metrics
        if spread_pct_max < 999 and spread_pct > spread_pct_max:
            return None, "orderbook_spread_too_wide", (
                f"호가 스프레드 {spread_pct:.3f}% > 기준 {spread_pct_max:.3f}%"
            ), metrics
        if imbalance_score_min > 0 and score < imbalance_score_min:
            return None, "orderbook_imbalance_score_below_min", (
                f"호가 불균형 점수 {score:.3f} < 기준 {imbalance_score_min:.3f}"
            ), metrics

        return (
            f"OB_PASS(ratio={ratio:.1f},spread={spread_pct:.3f}%,score={score:.3f})",
            "orderbook_pass",
            "호가 불균형 조건 충족",
            metrics,
        )
    except Exception as e:
        metrics["orderbook_source"] = "redis_error"
        metrics["orderbook_error"] = str(e)[:80]
        db_ob = _query_orderbook_imbalance_from_db(stock_code, metrics)
        if db_ob:
            ratio = float(db_ob.get("ratio") or 0)
            spread_pct = float(db_ob.get("spread_pct") or 0)
            score = float(db_ob.get("score") or 0)
            metrics.update({
                "orderbook_source": "db_fallback_after_redis_error",
                "orderbook_bid_ask_ratio": ratio,
                "orderbook_spread_pct": spread_pct,
                "orderbook_imbalance_score": score,
            })
            if buy_ratio_min > 0 and ratio < buy_ratio_min:
                return None, "orderbook_ratio_below_min", f"호가 매수비율 {ratio:.1f} < 기준 {buy_ratio_min:.1f}", metrics
            if spread_pct_max < 999 and spread_pct > spread_pct_max:
                return None, "orderbook_spread_too_wide", f"호가 스프레드 {spread_pct:.3f}% > 기준 {spread_pct_max:.3f}%", metrics
            if imbalance_score_min > 0 and score < imbalance_score_min:
                return None, "orderbook_imbalance_score_below_min", f"호가 불균형 점수 {score:.3f} < 기준 {imbalance_score_min:.3f}", metrics
            return (
                f"OB_PASS(ratio={ratio:.1f},spread={spread_pct:.3f}%,score={score:.3f})",
                "orderbook_pass",
                "호가 불균형 조건 충족(DB fallback)",
                metrics,
            )
        return None, "data_quality_block", f"호가 불균형 Redis 조회 오류({stock_code}): {e}; DB 최신 호가 미존재", metrics


def _get_db_params() -> dict:
    return dict(
        dbname=os.environ.get("DB_NAME", "kisautotrade"),
        user=os.environ.get("DB_USER", "kis_admin"),
        password=os.environ.get("DB_PASSWORD", ""),
        host=os.environ.get("DB_HOST", "localhost"),
        port=os.environ.get("DB_PORT", "6432"),
    )


def _pct_to_decimal(value, default: float) -> float:
    try:
        pct = abs(float(value))
    except (TypeError, ValueError):
        return default
    if pct <= 0:
        return default
    return pct / 100.0 if pct >= 1 else pct


def _iter_rule_dicts(value):
    if isinstance(value, dict):
        yield value
        for child in value.values():
            yield from _iter_rule_dicts(child)
    elif isinstance(value, list):
        for item in value:
            yield from _iter_rule_dicts(item)


def _has_limit_up_entry_rules(entry_rules) -> bool:
    """카드 entry_rules에 상한가 전략 규칙이 있는지 확인 (card_id 하드코딩 제거)."""
    if not isinstance(entry_rules, list):
        return False
    for r in entry_rules:
        if isinstance(r, dict):
            name = str(r.get("type") or r.get("name") or "").strip().lower()
            if name in {"limit_up_close_confirmation", "morning_top_mover_tracking"}:
                return True
    return False


def _parse_card_time_window(entry_rules) -> tuple:
    """카드 entry_rules에서 진입 시간창 추출. list/dict 모두 지원."""
    ds, de = "09:05", "14:50"
    if isinstance(entry_rules, list):
        for r in entry_rules:
            if isinstance(r, dict) and r.get("type") == "time_window":
                return str(r.get("start") or ds), str(r.get("end") or de)
    elif isinstance(entry_rules, dict):
        s = entry_rules.get("entry_start_time")
        e = entry_rules.get("entry_end_time")
        if s or e:
            return str(s or ds), str(e or de)
    return ds, de


def _parse_card_nxt_time_windows(entry_rules) -> list:
    """NXT 전용 시간창 목록 추출.

    {"type": "nxt_time_window", "start": "HH:MM", "end": "HH:MM"} 규칙 또는
    NXT 시간대(08:00~08:50, 15:40~20:00)와 겹치는 복수 {"type": "time_window"} 항목을
    dt_time 쌍의 리스트로 반환한다. 비어 있으면 카드 레벨 NXT 신규 진입을 허용하지 않는다.
    """
    windows = []
    if not isinstance(entry_rules, list):
        return windows
    for r in entry_rules:
        if not isinstance(r, dict):
            continue
        rtype = str(r.get("type") or "").strip().lower()
        try:
            if rtype == "nxt_time_window":
                s = str(r.get("start") or "08:00")
                e = str(r.get("end") or "08:50")
                windows.append((
                    dt_time(int(s.split(":")[0]), int(s.split(":")[1])),
                    dt_time(int(e.split(":")[0]), int(e.split(":")[1])),
                ))
            elif rtype == "time_window":
                s = str(r.get("start") or "09:00")
                e = str(r.get("end") or "15:30")
                ts = dt_time(int(s.split(":")[0]), int(s.split(":")[1]))
                te = dt_time(int(e.split(":")[0]), int(e.split(":")[1]))
                # NXT AM(08:00~08:50) 또는 NXT PM(15:40~20:00)와 겹치는 창만 포함
                nxt_am_overlap = ts < NXT_PRE_CLOSE and te > NXT_PRE_OPEN
                nxt_pm_overlap = ts < NXT_AFTER_CLOSE and te > NXT_AFTER_OPEN
                if nxt_am_overlap or nxt_pm_overlap:
                    windows.append((ts, te))
        except Exception:
            continue
    return windows


def _current_nxt_session(now_t: dt_time | None = None) -> str | None:
    if now_t is None:
        try:
            from zoneinfo import ZoneInfo
            now_t = datetime.now(ZoneInfo("Asia/Seoul")).time()
        except Exception:
            now_t = datetime.now().time()
    if NXT_PRE_OPEN <= now_t <= NXT_PRE_CLOSE:
        return "nxt_am"
    if NXT_AFTER_OPEN <= now_t <= NXT_AFTER_CLOSE:
        return "nxt_pm"
    return None


def _card_explicitly_allows_nxt(card: dict, session: str | None) -> bool:
    if not session:
        return False

    entry_rules = card.get("entry_rules", [])
    has_nxt_window = False
    if isinstance(entry_rules, list):
        has_nxt_window = any(
            isinstance(rule, dict)
            and str(rule.get("type") or "").strip().lower() == "nxt_time_window"
            for rule in entry_rules
        )

    strategy_params = _json_dict(card.get("strategy_params"))
    metadata = _json_dict(card.get("metadata"))
    if not (
        has_nxt_window
        or _as_bool(strategy_params.get("nxt_entry_enabled"), False)
        or _as_bool(metadata.get("nxt_entry_enabled"), False)
    ):
        return False

    configured_sessions = (
        strategy_params.get("nxt_entry_sessions")
        or metadata.get("nxt_entry_sessions")
        or []
    )
    if configured_sessions:
        return session in {str(value).strip().lower() for value in configured_sessions}

    return session == "nxt_am"


def _card_allows_nxt_session(card: dict, now_t: dt_time | None = None) -> bool:
    session = _current_nxt_session(now_t)
    if session == "nxt_am" and not _is_scalping_nxt_entry_enabled():
        return False
    if session == "nxt_pm" and not _is_scalping_nxt_pm_entry_enabled():
        return False
    if not _card_explicitly_allows_nxt(card, session):
        return False

    windows = _parse_card_nxt_time_windows(card.get("entry_rules", []))
    if windows and now_t is not None:
        return any(start <= now_t <= end for start, end in windows)
    return bool(windows)


def _extract_scalping_entry_rule_params(entry_rules) -> dict:
    """Return live scalping gates declared in entry_rules.

    These values must take precedence over metadata.scalping_params so the
    strategy card screen and the real entry engine cannot drift apart.
    """
    params = {}
    if not isinstance(entry_rules, list):
        return params
    for rule in entry_rules:
        if not isinstance(rule, dict):
            continue
        rtype = str(rule.get("type") or rule.get("name") or "").strip().lower()
        try:
            if rtype == "strength_threshold":
                raw = rule.get("min_strength") or rule.get("threshold") or rule.get("value")
                if raw is not None:
                    params["strength_threshold"] = float(raw)
            elif rtype == "volume_spike":
                raw = rule.get("multiplier") or rule.get("volume_multiplier") or rule.get("ratio")
                if raw is not None:
                    params["volume_multiplier"] = float(raw)
                lookback = rule.get("lookback_ticks")
                if lookback is not None:
                    params["volume_lookback_ticks"] = max(1, int(lookback))
            elif rtype == "momentum_ticks":
                raw = rule.get("min_ticks") or rule.get("ticks") or rule.get("value")
                if raw is not None:
                    params["min_momentum_ticks"] = max(1, int(raw))
        except (TypeError, ValueError):
            continue
    return params


def _is_in_nxt_hours_now() -> bool:
    """현재 KST가 NXT AM(08:00~08:50) 또는 NXT PM(15:40~20:00) 내인지 확인."""
    try:
        from zoneinfo import ZoneInfo
        now_t = datetime.now(ZoneInfo("Asia/Seoul")).time()
    except Exception:
        now_t = datetime.now().time()
    return (NXT_PRE_OPEN <= now_t <= NXT_PRE_CLOSE) or (NXT_AFTER_OPEN <= now_t <= NXT_AFTER_CLOSE)


def _json_dict(value) -> dict:
    return value if isinstance(value, dict) else {}


CARD126_NXT_POLICY = "krx_primary_nxt_buy_disabled_by_default"


def _is_card126_fixed_quantity_canary(card: dict) -> bool:
    """Return whether the bounded #126 canary policy applies.

    Card #303 also uses one-share sizing, so the policy is intentionally
    scoped to #126's overnight card instead of changing the scalping card's
    existing risk semantics.
    """
    try:
        card_id = int(card.get("card_id") or 0)
        fixed_quantity = int(card.get("fixed_quantity") or 0)
    except (TypeError, ValueError):
        return False
    sizing_mode = str(card.get("position_sizing_mode") or "").strip().lower()
    return card_id == 126 and sizing_mode == "fixed_quantity" and fixed_quantity == 1


def _defer_fixed_quantity_cash_gate(card: dict) -> bool:
    """Return whether stale DB portfolio cash must not pre-block a fixed-quantity order."""

    return _is_card126_fixed_quantity_canary(card) or _is_card303_one_share_live_override(card)


def _normalise_loss_limit_amount(value, default: float | None = None) -> float | None:
    """Normalise an amount setting to a negative loss threshold.

    Both ``30000`` and ``-30000`` are accepted as configuration input, but
    the gate always compares realised PnL to ``-amount`` semantics.
    """
    if value is None or value == "":
        return default
    try:
        amount = abs(float(value))
    except (TypeError, ValueError):
        return default
    return -amount if amount > 0 else default


def resolve_card126_daily_loss_limits(card: dict, risk_params: dict) -> dict:
    """Resolve #126's explicit loss-limit semantics without weakening gates.

    The stored #126 card currently has ``daily_loss_limit_pct=3``. For this
    bounded canary path, a positive value means a 3% loss threshold, i.e.
    ``-3%``. An amount threshold is opt-in; an absent amount must not create a
    hidden 30,000 KRW canary blocker. Non-#126 cards retain the legacy
    defaults and signs so #303 behaviour is unchanged.
    """
    rp = risk_params if isinstance(risk_params, dict) else {}
    if _is_card126_fixed_quantity_canary(card):
        raw_pct = rp.get("daily_loss_limit_pct")
        try:
            pct_value = abs(float(raw_pct)) if raw_pct is not None else 5.0
        except (TypeError, ValueError):
            pct_value = 5.0
        pct_limit = -pct_value if pct_value > 0 else -5.0
        amount_limit = _normalise_loss_limit_amount(rp.get("daily_loss_limit_amount"))
        return {
            "limit_pct": pct_limit,
            "limit_amount": amount_limit,
            "semantics": "explicit_loss_thresholds_only",
            "amount_configured": amount_limit is not None,
        }

    try:
        pct_limit = float(rp.get("daily_loss_limit_pct") or -5)
    except (TypeError, ValueError):
        pct_limit = -5.0
    try:
        amount_limit = float(rp.get("daily_loss_limit_amount") or -30000)
    except (TypeError, ValueError):
        amount_limit = -30000.0
    return {
        "limit_pct": pct_limit,
        "limit_amount": amount_limit,
        "semantics": "legacy_card_thresholds",
        "amount_configured": True,
    }


def evaluate_daily_loss_limit(
    *,
    daily_pnl: float,
    initial_capital: float,
    limit_pct: float,
    limit_amount: float | None,
) -> dict:
    """Evaluate realised daily loss and return auditable threshold metrics."""
    try:
        pnl = float(daily_pnl or 0)
    except (TypeError, ValueError):
        pnl = 0.0
    try:
        capital = float(initial_capital or 0)
    except (TypeError, ValueError):
        capital = 0.0
    current_pct = (pnl / capital * 100.0) if capital > 0 else None
    amount_breached = bool(limit_amount is not None and pnl < 0 and pnl <= float(limit_amount))
    pct_breached = bool(
        current_pct is not None and pnl < 0 and current_pct <= float(limit_pct)
    )
    return {
        "breached": amount_breached or pct_breached,
        "breach_reason": "amount" if amount_breached else ("pct" if pct_breached else None),
        "daily_pnl": pnl,
        "initial_capital": capital,
        "current_loss_pct": current_pct,
        "limit_amount": limit_amount,
        "limit_pct": float(limit_pct),
    }


def _nxt_policy_metadata(card: dict) -> dict:
    """Return the audit contract for NXT buy policy decisions."""
    if int(card.get("card_id") or 0) == 126:
        return {
            "nxt_policy": CARD126_NXT_POLICY,
            "nxt_buy_allowed_by_default": False,
            "nxt_exit_routing": "supported_by_live_engine_and_scalping_monitor",
            "policy_intentional": True,
        }
    return {
        "nxt_policy": "card_explicit_opt_in",
        "nxt_buy_allowed_by_default": False,
        "policy_intentional": True,
    }


def _rule_types(rules) -> set[str]:
    types: set[str] = set()
    for rule in _iter_rule_dicts(rules):
        rtype = str(rule.get("type") or rule.get("name") or "").strip().lower()
        if rtype:
            types.add(rtype)
    return types


def _is_overnight_card(card: dict) -> bool:
    rp = _json_dict(card.get("risk_params"))
    sp = _json_dict(card.get("strategy_params"))
    meta = _json_dict(card.get("metadata"))
    strategy_type = str(
        rp.get("strategy_type")
        or sp.get("engine_type")
        or sp.get("strategy_type")
        or sp.get("holding_period")
        or meta.get("engine_type")
        or ""
    ).strip().lower()
    if strategy_type in {"overnight_closing", "overnight", "next_day_gap", "closing"}:
        return True
    overnight_exits = {"gap_up_next_day", "gap_down_next_day", "holding_days"}
    return bool(_rule_types(card.get("exit_rules")) & overnight_exits)


def _is_scalping_strategy(card: dict) -> bool:
    if _is_overnight_card(card):
        return False
    rp = _json_dict(card.get("risk_params"))
    sp = _json_dict(card.get("strategy_params"))
    meta = _json_dict(card.get("metadata"))
    strategy_type = str(
        rp.get("strategy_type")
        or sp.get("engine_type")
        or sp.get("strategy_type")
        or sp.get("holding_period")
        or ""
    ).strip().lower()
    if strategy_type in {"scalping", "scalp"}:
        return True
    preferred_data = str(sp.get("preferred_data_source") or "").strip().lower()
    return "tick" in preferred_data or "orderbook" in preferred_data or bool(meta.get("scalping") is True)



def _extract_exit_config(exit_rules, risk_params, metadata, desk_id: int | None) -> tuple[float, float, float | None]:
    default_by_desk = {
        1: (0.02, 0.01, None),
        2: (0.03, 0.02, None),
        3: (0.08, 0.04, 0.03),
        4: (0.15, 0.07, 0.05),
        5: (0.30, 0.15, 0.10),
    }
    default_tp, default_sl, default_trail = default_by_desk.get(int(desk_id or 3), default_by_desk[3])
    rp = risk_params if isinstance(risk_params, dict) else {}
    meta = metadata if isinstance(metadata, dict) else {}
    tp_pct = _pct_to_decimal(rp.get("take_profit_pct"), default_tp)
    sl_pct = _pct_to_decimal(rp.get("stop_loss_pct"), default_sl)
    trailing_pct = _pct_to_decimal(rp.get("trailing_stop_pct"), default_trail) if rp.get("trailing_stop_pct") is not None else default_trail

    for rule in _iter_rule_dicts(exit_rules):
        rtype = str(rule.get("type") or rule.get("name") or "").lower()
        if rtype in {"profit_target", "take_profit", "target_return"}:
            tp_pct = _pct_to_decimal(rule.get("target_pct") or rule.get("take_profit_pct") or rule.get("pct"), tp_pct)
        elif rtype in {"stop_loss", "hard_stop"}:
            sl_pct = _pct_to_decimal(rule.get("stop_pct") or rule.get("stop_loss_pct") or rule.get("pct"), sl_pct)
        elif rtype == "trailing_stop":
            trailing_pct = _pct_to_decimal(rule.get("trail_pct") or rule.get("trailing_stop_pct") or rule.get("pct"), trailing_pct or default_trail)

    scalping_params = meta.get("scalping_params") if isinstance(meta.get("scalping_params"), dict) else {}
    if scalping_params:
        tp_pct = _pct_to_decimal(scalping_params.get("tp_pct"), tp_pct)
        sl_pct = _pct_to_decimal(scalping_params.get("sl_pct"), sl_pct)
    return tp_pct, sl_pct, trailing_pct


# ── [2026-08-19 P1/D3] R(리스크 단위) 기반 포지션 사이징 ──────────────────
# 근거: reports/20260819_card119_stoploss_design_and_payoff.md L3
#   - 갭다운은 손절선으로 못 막음(갭 -3%↓ 시작 180건 실현손실 평균 -9.30%, 최악 -28.74%)
#   - 따라서 갭 리스크는 "손절폭"이 아니라 "사이징"으로 관리해야 함
#   - 종목당 리스크 = 계좌자본 × risk_per_trade_pct(기본 0.3%)
#   - 1주당 가정손실 = price × assumed_gap_loss_pct(기본 10%)
RISK_UNIT_DEFAULT_RISK_PCT = 0.3      # 계좌자본 대비 종목당 리스크 예산(%)
RISK_UNIT_DEFAULT_GAP_LOSS_PCT = 10.0  # 갭다운 가정 손실률(%)


def calc_risk_based_qty(
    equity: float,
    price: float,
    risk_per_trade_pct: float = RISK_UNIT_DEFAULT_RISK_PCT,
    assumed_gap_loss_pct: float = RISK_UNIT_DEFAULT_GAP_LOSS_PCT,
    cash_cap: float | None = None,
) -> int:
    """R 기반 매수 수량 계산(순수 함수).

    qty = floor( (equity * risk_pct/100) / (price * gap_loss_pct/100) )
    - equity/price가 유효하지 않으면 0
    - cash_cap이 주어지면 현금으로 살 수 있는 수량으로 상한
    - 결과가 0이지만 1주는 살 수 있는 경우에도 0을 반환(리스크 예산 초과이므로 진입 금지)
    """
    try:
        equity_f = float(equity or 0)
        price_f = float(price or 0)
        risk_pct = float(risk_per_trade_pct or 0)
        gap_pct = float(assumed_gap_loss_pct or 0)
    except (TypeError, ValueError):
        return 0
    if equity_f <= 0 or price_f <= 0 or risk_pct <= 0 or gap_pct <= 0:
        return 0
    risk_budget = equity_f * (risk_pct / 100.0)
    loss_per_share = price_f * (gap_pct / 100.0)
    if loss_per_share <= 0:
        return 0
    qty = int(risk_budget // loss_per_share)
    if qty <= 0:
        return 0
    if cash_cap is not None:
        try:
            cap_qty = int(float(cash_cap) // price_f)
        except (TypeError, ValueError, ZeroDivisionError):
            cap_qty = qty
        qty = min(qty, max(0, cap_qty))
    return max(0, qty)


class ScalpingEntryEngine:
    """
    실시간 틱 기반 스캘핑 매수 진입 엔진.

    tick 구조: (stock_code, tick_time, price, volume, cum_volume, buy_sell, strength)
    """

    def __init__(self, tick_queue: asyncio.Queue) -> None:
        self._queue = tick_queue
        self._running = False

        # 전략카드 정보 (scalping=true 카드)
        self._cards: list[dict] = []
        self._card_reload_fail_count: int = 0
        self._card_positions: dict[int, int] = {}  # portfolio_id → open position count
        self._card_held_stocks: dict[int, set[str]] = {}  # portfolio_id → open stock codes

        # 스캘핑 유니버스 (감시 대상 종목)
        self._universe: set[str] = set()
        self._universe_meta: dict[str, dict] = {}
        self._card119_discovery_universe: set[str] = set()
        # #303 discovery is a hard entry prerequisite. Keep this separate from
        # the broad monitoring universe so a stale/base universe cannot bypass
        # the same-day snapshot + Top50 contract.
        self._mahaseven_top50_codes: set[str] = set()
        self._mahaseven_top30_codes: set[str] = set()

        # 종목별 틱 히스토리 (최근 N틱)
        self._tick_history: dict[str, deque] = defaultdict(lambda: deque(maxlen=TICK_HISTORY_SIZE))

        # 종목별 세션 고가 (당일)
        self._session_high: dict[str, float] = {}

        # 일일 매수 카운터
        self._daily_buy_count = 0
        self._last_reset_date: Optional[date] = None

        # 진입 Lock (동일 종목 중복 매수 방지)
        self._entry_locks: dict[str, asyncio.Lock] = {}

        # 이미 매수한 종목 (당일 중복 방지)
        self._bought_today: set[str] = set()

        # Executor 캐시
        self._executor_cache: dict[int, object] = {}

        # 타이밍
        self._last_universe_load = 0.0
        self._last_position_load = 0.0
        self._last_kiwoom_resub_ts = 0.0

        # 감사 로그 throttling: 카드/종목/사유 단위로 과다 적재 방지
        self._audit_throttle: dict[tuple, float] = {}

        # VWAP 누적 데이터 (당일 틱 기반 실시간 산출)
        self._vwap_data: dict[str, dict] = {}  # stock_code → {"cum_pv": float, "cum_vol": float}

        # 1분봉 MA 데이터 (ma_pullback 진입 조건용)
        self._minute_bars: dict[str, deque] = defaultdict(lambda: deque(maxlen=30))
        self._minute_ohlc_bars: dict[str, deque] = defaultdict(lambda: deque(maxlen=_SESSION_WAVE_BUFFER_BARS))
        self._minute_bar_current: dict[str, dict] = {}
        self._minute_bar_db_cache: dict[str, tuple[float, list[float]]] = {}
        self._minute_ohlc_db_cache: dict[str, tuple[float, list[dict]]] = {}
        self._wave_recovery_cooldown: dict[str, tuple[float, str]] = {}
        self._external_minute_bars_ingested: int = 0
        self._last_external_minute_bar_log: float = 0.0

        # [개선1] 실패 쿨다운: 동일종목 buy 실패 후 60초간 재시도 차단 (호가 변동 시 해제)
        self._failed_cooldown: dict[str, tuple[float, float]] = {}  # stock_code → (monotonic_ts, price)
        self._failed_count: dict[str, int] = {}  # stock_code → 누적 실패 횟수
        self._FAIL_COOLDOWN_SEC = 60.0
        self._FAIL_PRICE_CHANGE_PCT = 0.003
        self._FAIL_MAX_DAILY = 5

        # [P0-C] 최근 손실 종목 재진입 쿨다운
        self._loss_cooldown_stocks: set[str] = set()
        self._last_loss_cooldown_load: float = 0.0

        # [P0/P2] 수동 제외 및 과열 제외: 스크리너 전역 제외 + 카드별 제외 + 3연속 상한가 차단
        self._manual_excluded_stocks: set[str] = set()
        self._card_excluded_stocks: dict[int, set[str]] = {}
        self._overheated_stocks: set[str] = set()
        self._last_exclusion_load: float = 0.0
        self._last_card_reload: float = 0.0

        # [P0] 키움 WS 동적 구독 연동
        self._kiwoom_ws = None

        # [P0] 보합 반복 방지: 당일 매수 종목 재진입 쿨다운 (DB 영속)
        self._bought_today_ts: dict[str, float] = {}

        # [P2] 우선진입 정렬 버퍼: 큐 배치 드레인 후 품질순 정렬된 대기 틱
        self._priority_buffer: list = []
        self._priority_processed_since_yield = 0

        # [P0-FLAT-COOLDOWN] 보합 반복 방지: 종목별 마지막 카드평가 (가격, monotonic_ts)
        # 동일가 반복 체결틱은 entry_rules 평가가 동일 결과(skip)를 반복 생성한다.
        # _FLAT_COOLDOWN_SEC 이내에 |Δprice|/price < _FLAT_COOLDOWN_BPS/10000 이면 카드 평가 진입 전 드롭.
        self._last_eval_tick: dict[str, tuple[float, float]] = {}  # stock_code → (price, monotonic_ts)
        self._flat_skip_count: int = 0  # 진단용 카운터

        # [GO100-303] 계좌 인증 오류(90070000) 회로 차단기
        # key: (card_id, account_id) → 연속 오류 횟수
        self._account_auth_error_count: dict[tuple, int] = {}
        # 차단된 (card_id, account_id) 세트 — 프로세스 재시작 시 초기화
        self._account_auth_blocked: set[tuple] = set()

        # [2026-08-19 P0] L0 진입필터: 25% 도달 시점 추적 (종목→KST datetime)
        self._time_25pct: dict[str, datetime] = {}
        # 엔진 기동 시각(KST). 장중 재시작 시 25% 도달시각을 알 수 없는 종목을
        # 잘못 차단(fail-closed)하지 않도록 판정 기준으로 사용한다.
        try:
            from zoneinfo import ZoneInfo as _ZI

            self._engine_started_at: datetime = datetime.now(_ZI("Asia/Seoul"))
        except Exception:
            self._engine_started_at = datetime.now()

        # [2026-08-19 P0] 카드별 리스크 게이트: 연속손실/일일손실 매수차단
        self._consecutive_loss_count: dict[int, int] = {}
        self._daily_pnl_by_card: dict[int, float] = {}
        self._card_initial_capital: dict[int, float] = {}
        self._last_risk_state_load: float = 0.0

        # [2026-08-19 P0] 손절 종목 당일 재진입 금지
        self._stopped_out_today: set[str] = set()

        # [GO100 W1 P1-1] CRITICAL 파동 실패 종목은 당일 신규 진입 금지.
        self._wave_failure_blacklist: set[str] = set()

    def set_kiwoom_ws(self, ws) -> None:
        """Set the optional legacy WS reference used only for subscription sync."""
        if ws is not None and not _DIRECT_WS_SUBSCRIPTION_SYNC_ENABLED:
            logger.warning(
                "ScalpingEntryEngine: ignoring direct WS reference; "
                "collector-shard/DB path is active (set GO100_SCALPING_DIRECT_WS_SUBSCRIPTION_SYNC_ENABLED=true for legacy mode)"
            )
            self._kiwoom_ws = None
            return
        self._kiwoom_ws = ws

    def _check_card303_discovery(self, card: dict, stock_code: str) -> tuple[bool, str, dict]:
        """Enforce #303's external discovery result before signal evaluation.

        ``_universe`` is intentionally broader because it also serves other
        cards and open-position monitoring. Card #303 must therefore check the
        separately loaded same-day Top50 set and fail closed when that set is
        unavailable or the candidate is outside it.
        """
        try:
            card_id = int(card.get("card_id") or card.get("go100_card_id") or 0)
        except (TypeError, ValueError):
            card_id = 0
        if card_id != 303:
            return True, "", {"discovery_required": False}

        discovery_codes = getattr(self, "_mahaseven_top50_codes", set()) or set()
        metrics = {
            "discovery_required": True,
            "discovery_contract_version": CARD303_DISCOVERY_CONTRACT_VERSION,
            "discovery_candidate_count": len(discovery_codes),
        }
        if not discovery_codes:
            return False, "card303_discovery_unavailable", metrics
        if str(stock_code or "").strip() not in discovery_codes:
            return False, "card303_discovery_not_top50", metrics
        return True, "", metrics

    def _load_bought_today_from_db(self) -> None:
        """당일 매수 이력을 DB에서 로드 (프로세스 재시작 시 재진입 쿨다운 유지)."""
        try:
            conn = psycopg2.connect(**_get_db_params())
            cur = conn.cursor()
            cur.execute("""
                SELECT stock_code, EXTRACT(EPOCH FROM MAX(created_at)) as last_buy_epoch
                FROM go100_positions
                WHERE created_at::date = CURRENT_DATE
                GROUP BY stock_code
            """)
            rows = cur.fetchall()
            cur.close()
            conn.close()
            loaded = 0
            for stock_code, last_buy_epoch in rows:
                if stock_code and last_buy_epoch:
                    self._bought_today.add(stock_code)
                    self._bought_today_ts[stock_code] = float(last_buy_epoch)
                    loaded += 1
            logger.info("_load_bought_today_from_db: %d stocks loaded into reentry cooldown", loaded)
        except Exception as e:
            logger.error("_load_bought_today_from_db error: %s", e)

    def _maybe_load_daily_risk_state(self) -> None:
        """5분 간격으로 오늘 청산 실적을 DB에서 로드하여 리스크 게이트 갱신."""
        if time_module.monotonic() - self._last_risk_state_load < 300:
            return
        self._last_risk_state_load = time_module.monotonic()
        try:
            conn = psycopg2.connect(**_get_db_params())
            cur = conn.cursor()
            cur.execute("""
                SELECT go100_card_id, stock_code, pnl_amount, pnl_pct,
                       ROW_NUMBER() OVER (
                           PARTITION BY go100_card_id ORDER BY updated_at DESC
                       ) as rn
                FROM go100_positions
                WHERE status IN ('CLOSED', 'FORCE_CLOSED')
                  AND exit_date = CURRENT_DATE
                ORDER BY go100_card_id, updated_at DESC
            """)
            rows = cur.fetchall()
            consec: dict[int, int] = {}
            counted_done: set[int] = set()  # 최신부터 세다가 이익을 만나면 카운트 확정
            daily_pnl: dict[int, float] = {}
            stopped: set[str] = set()
            for card_id, stock_code, pnl_amt, pnl_pct, rn in rows:
                daily_pnl[card_id] = daily_pnl.get(card_id, 0.0) + float(pnl_amt or 0)
                if card_id not in consec:
                    consec[card_id] = 0
                # [2026-08-19 P1/D4 fix] 기존 로직은 이익 1건을 만나면 누적 카운트를
                # -1로 덮어써서 "당일 전부 손실"인 경우에만 브레이커가 발동했다.
                # 최신부터 연속된 손실 구간만 세고, 이익을 만나면 그대로 확정한다.
                if card_id not in counted_done:
                    if float(pnl_amt or 0) < 0:
                        consec[card_id] += 1
                    else:
                        counted_done.add(card_id)
                if float(pnl_amt or 0) < 0:
                    stopped.add(stock_code)
            self._consecutive_loss_count = {k: v for k, v in consec.items() if v > 0}
            self._daily_pnl_by_card = daily_pnl
            self._stopped_out_today = stopped
            cur.execute("""
                SELECT go100_card_id, allocated_amount
                FROM go100_strategy_cards
                WHERE card_status IN ('LIVE', 'PAPER_LIVE')
            """)
            for cid, alloc in cur.fetchall():
                self._card_initial_capital[cid] = float(alloc or 0)
            cur.close()
            conn.close()
            logger.info(
                "_maybe_load_daily_risk_state: consec=%s daily_pnl=%s stopped=%d",
                self._consecutive_loss_count, self._daily_pnl_by_card, len(self._stopped_out_today),
            )
        except Exception as e:
            logger.warning("_maybe_load_daily_risk_state failed: %s", e)

    # ── 데이터 로드 ────────────────────────────────────────────────────

    def load_scalping_cards(self) -> int:
        """모든 활성 LIVE 전략카드 + 포트폴리오 정보 로드 (카드별 조건으로 실시간 매매)."""
        try:
            conn = psycopg2.connect(**_get_db_params())
            cur = conn.cursor()
            cur.execute("""
                SELECT
                    gsc.go100_card_id,
                    gsc.strategy_name,
                    gsc.entry_rules,
                    gsc.metadata,
                    gsc.risk_params,
                    gsc.user_id,
                    gsc.exit_rules,
                    gsc.desk_id,
                    gpf.portfolio_id,
                    gpf.account_id,
                    gpf.initial_capital,
                    gpf.current_cash,
                    gpf.available_for_buy,
                    gsc.strategy_params,
                    gsc.universe_filter,
                    gsc.allocated_amount,
                    gsc.max_stocks,
                    CASE
                        WHEN COALESCE(gsc.strategy_params->>'live_priority', '') ~ '^[0-9]+$'
                        THEN (gsc.strategy_params->>'live_priority')::int
                        ELSE 1000
                    END AS live_priority,
                    a.kis_config_id AS config_id,
                    COALESCE(a.broker_type, 'KIS') AS broker_type,
                    gsc.card_status,
                    COALESCE(a.is_mock, false) AS account_is_mock,
                    gsc.trigger_tactic
                FROM go100_strategy_cards gsc
                JOIN go100_portfolios gpf
                    ON gpf.go100_card_id = gsc.go100_card_id
                    AND gpf.user_id = gsc.user_id
                    AND gpf.account_id = gsc.account_id
                    AND gpf.status = 'ACTIVE'
                    AND gpf.is_live = true
                JOIN accounts a
                    ON a.account_id = gpf.account_id
                    AND a.account_id = gsc.account_id
                    AND a.user_id = gsc.user_id
                    AND a.is_active = true
                    AND COALESCE(a.buy_blocked, false) = false
                WHERE gsc.is_active = true
                  AND (
                      LOWER(COALESCE(gsc.metadata->>'scalping', '')) IN ('true', '1', 'yes', 'y')
                      OR LOWER(COALESCE(gsc.metadata->>'trade_engine', '')) IN ('scalping', 'go100_scalping', 'kiwoom_scalping')
                  )
                  AND (
                      (COALESCE(a.is_mock, false) = true AND gsc.card_status IN ('LIVE', 'PAPER_LIVE'))
                      OR (COALESCE(a.is_mock, false) = false AND gsc.card_status = 'LIVE' AND COALESCE(gsc.is_live, false) = true)
                  )
                ORDER BY live_priority ASC, gsc.go100_card_id ASC, gpf.portfolio_id ASC
            """)
            rows = cur.fetchall()
            cur.close()
            conn.close()
        except Exception as e:
            logger.error("load_scalping_cards DB error: %s", e)
            self._card_reload_fail_count += 1
            if self._card_reload_fail_count >= _CARD_STALE_FAIL_LIMIT:
                logger.warning(
                    "load_scalping_cards: %d consecutive failures → clearing stale cards",
                    self._card_reload_fail_count,
                )
                self._cards = []
            return 0

        self._cards = []
        for competition_sequence, row in enumerate(rows, start=1):
            (card_id, name, entry_rules, metadata, risk_params,
             user_id, exit_rules, desk_id,
             portfolio_id, account_id, initial_capital, current_cash,
             available_for_buy, strategy_params, universe_filter, allocated_amount,
             card_max_stocks, live_priority, config_id, broker_type, card_status,
             account_is_mock, trigger_tactic) = row

            if not config_id and broker_type != 'KIWOOM':
                continue

            # [종가매매 지원] 오버나이트 exit_rules 카드(#126 등)도 로딩 허용.
            # 진입: 카드 time_window로 제한. 청산: 익일 live_engine evaluate_go100_exit().
            _overnight_rule_types = {"gap_up_next_day", "gap_down_next_day", "holding_days"}
            _exit_rule_list_for_guard = exit_rules if isinstance(exit_rules, list) else []
            if isinstance(exit_rules, dict):
                _exit_rule_list_for_guard = exit_rules.get("rules", [exit_rules])
            _detected_overnight = [
                r.get("type") for r in _exit_rule_list_for_guard
                if isinstance(r, dict) and r.get("type") in _overnight_rule_types
            ]
            _is_overnight_card = bool(_detected_overnight)
            if _is_overnight_card:
                logger.info(
                    "[OVERNIGHT] card_id=%s '%s' loaded with overnight exit rules %s. "
                    "Entry via card time_window, exit via live_engine.",
                    card_id, name, _detected_overnight,
                )

            # entry_rules 파싱
            rules = entry_rules if isinstance(entry_rules, list) else []
            if isinstance(entry_rules, dict):
                rules = entry_rules.get("rules", [entry_rules])

            # metadata에서 스캘핑 파라미터 추출 (있으면 사용, 없으면 카드 기본값)
            meta = metadata if isinstance(metadata, dict) else {}
            scalping_params = meta.get("scalping_params", {})

            # risk_params/strategy_params에서 매수 한도와 종목당 금액을 적용한다.
            rp = risk_params if isinstance(risk_params, dict) else {}
            sp = strategy_params if isinstance(strategy_params, dict) else {}
            card_excluded = self._extract_excluded_codes(sp)
            # DB 카드 컬럼(max_stocks)을 우선한다. risk_params는 과거 값이 남아 UI 설정과 충돌할 수 있다.
            max_stocks_raw = card_max_stocks if card_max_stocks is not None else rp.get("max_stocks", rp.get("max_concurrent", SCALPING_DEFAULT_MAX_STOCKS))
            try:
                max_stocks = max(1, int(max_stocks_raw or SCALPING_DEFAULT_MAX_STOCKS))
            except (TypeError, ValueError):
                max_stocks = SCALPING_DEFAULT_MAX_STOCKS
            per_position_amount = rp.get("per_position_amount") or sp.get("per_position_amount")
            fixed_quantity_raw = rp.get("fixed_quantity") or sp.get("fixed_quantity")
            try:
                card_fixed_quantity = max(0, int(fixed_quantity_raw or 0))
            except (TypeError, ValueError):
                card_fixed_quantity = 0
            _sizing_mode_raw = str(rp.get("position_sizing_mode") or sp.get("position_sizing_mode") or "").strip().lower()

            # TP/SL/Trailing: 카드 exit_rules + risk_params를 우선 적용한다.
            tp_pct, sl_pct, trailing_pct = _extract_exit_config(exit_rules, risk_params, meta, desk_id)

            no_trade_windows = []
            for ntw_raw in (rp.get("no_trade_window") or []):
                if isinstance(ntw_raw, str) and "-" in ntw_raw:
                    time_part = ntw_raw.split()[0]
                    tw_parts = time_part.split("-")
                    if len(tw_parts) == 2:
                        try:
                            s_h, s_m = tw_parts[0].split(":")
                            e_h, e_m = tw_parts[1].split(":")
                            no_trade_windows.append((dt_time(int(s_h), int(s_m)), dt_time(int(e_h), int(e_m))))
                        except (ValueError, IndexError):
                            pass

            # 카드별 실시간 감시 조건 (scalping_params가 없으면 범용 기본값)
            self._cards.append({
                "card_id": card_id,
                "user_id": user_id,
                "strategy_name": name,
                "desk_id": desk_id,
                "entry_rules": rules,
                "exit_rules": exit_rules,
                "risk_params": rp,
                "strategy_params": sp,
                "trigger_tactics": trigger_tactic,
                "metadata": meta,
                "scalping_params": scalping_params or {
                    "strength_threshold": SCALPING_DEFAULT_STRENGTH_THRESHOLD,
                    "volume_multiplier": SCALPING_DEFAULT_VOLUME_MULTIPLIER,
                    "min_momentum_ticks": SCALPING_DEFAULT_MIN_MOMENTUM_TICKS,
                },
                "portfolio_id": portfolio_id,
                "account_id": account_id,
                "config_id": config_id or 0,
                "broker_type": broker_type,
                "card_status": card_status,
                "account_is_mock": bool(account_is_mock),
                "initial_capital": float(initial_capital or 0),
                "current_cash": float(current_cash or 0),
                "available_for_buy": float(available_for_buy or 0),
                "allocated_amount": float(allocated_amount or 0),
                "per_position_amount": float(per_position_amount or 0),
                "fixed_quantity": card_fixed_quantity,
                "position_sizing_mode": _sizing_mode_raw,
                # [2026-08-19 P1/D3] R 기반 사이징 파라미터
                "risk_per_trade_pct": float(
                    rp.get("risk_per_trade_pct")
                    or sp.get("risk_per_trade_pct")
                    or RISK_UNIT_DEFAULT_RISK_PCT
                ),
                "assumed_gap_loss_pct": float(
                    rp.get("assumed_gap_loss_pct")
                    or sp.get("assumed_gap_loss_pct")
                    or RISK_UNIT_DEFAULT_GAP_LOSS_PCT
                ),
                "max_stocks": max_stocks,
                "universe_filter": universe_filter,
                "no_trade_windows": no_trade_windows,
                "live_priority": int(live_priority or 1000),
                "competition_sequence": competition_sequence,
                "competition_policy": "FIRST_SIGNAL_FIRST_CARD_UNTIL_DB_PRIORITY",
                "tp_pct": tp_pct,
                "sl_pct": sl_pct,
                "trailing_pct": trailing_pct,
                "live_only_filters": meta.get("live_only_filters", {}),
                "manual_excluded_stocks": card_excluded,
                "is_overnight": _is_overnight_card,
            })

        self._card_reload_fail_count = 0
        self._load_manual_exclusions()
        logger.info("ScalpingEntryEngine: %d scalping card(s) loaded", len(self._cards))
        return len(self._cards)

    def _extract_excluded_codes(self, params: dict) -> set[str]:
        """strategy_params의 카드별 제외종목 정의를 6자리 종목코드 set으로 정규화."""
        codes: set[str] = set()
        for key in ("excluded_stock_codes", "card_excluded_stock_codes", "excluded_stocks"):
            raw = params.get(key) if isinstance(params, dict) else None
            if isinstance(raw, str):
                raw = [x.strip() for x in raw.replace(";", ",").split(",")]
            if isinstance(raw, dict):
                raw = raw.keys()
            if isinstance(raw, (list, tuple, set)):
                for item in raw:
                    code = str(item.get("stock_code") if isinstance(item, dict) else item).strip()
                    if len(code) == 6:
                        codes.add(code)
        return codes

    def _load_manual_exclusions(self) -> None:
        """스크리너 전역 제외와 카드별 선택 제외를 로드한다."""
        manual: set[str] = set()
        card_map: dict[int, set[str]] = {
            int(c["card_id"]): set(c.get("manual_excluded_stocks") or set())
            for c in self._cards
            if c.get("card_id") is not None
        }
        try:
            conn = psycopg2.connect(**_get_db_params())
            cur = conn.cursor()
            try:
                cur.execute("SELECT stock_code FROM v4_excluded_stocks")
                manual.update(str(row[0]).strip() for row in cur.fetchall() if str(row[0]).strip())
            except Exception as e:
                logger.debug("ScalpingEntryEngine v4_excluded_stocks load skipped: %s", e)
            try:
                cur.execute("""
                    SELECT stock_code
                    FROM go100_data_backfill_queue
                    WHERE status = 'source_unavailable'
                      AND missing_type IN ('snapshot_today', 'daily_ohlcv_10d', 'minute_ohlcv_365d')
                """)
                manual.update(str(row[0]).strip() for row in cur.fetchall() if str(row[0]).strip())
            except Exception as e:
                logger.debug("ScalpingEntryEngine source-unavailable exclusions load skipped: %s", e)
            try:
                cur.execute("""
                    SELECT to_regclass('public.go100_strategy_card_excluded_stocks')::text
                """)
                if cur.fetchone()[0]:
                    cur.execute("""
                        SELECT go100_card_id, stock_code
                        FROM go100_strategy_card_excluded_stocks
                        WHERE COALESCE(is_active, true) = true
                    """)
                    for card_id, stock_code in cur.fetchall():
                        code = str(stock_code or "").strip()
                        if len(code) == 6:
                            card_map.setdefault(int(card_id), set()).add(code)
            except Exception as e:
                logger.debug("ScalpingEntryEngine card exclusions load skipped: %s", e)
            cur.close()
            conn.close()
        except Exception as e:
            logger.error("_load_manual_exclusions error: %s", e)
        self._manual_excluded_stocks = {c for c in manual if len(c) == 6}
        self._card_excluded_stocks = card_map
        logger.info(
            "ScalpingEntryEngine exclusions loaded: global=%d card_scoped=%d",
            len(self._manual_excluded_stocks),
            sum(len(v) for v in self._card_excluded_stocks.values()),
        )

    def _load_overheated_stocks(self) -> None:
        """최근 3거래일 연속 상한가(전일 대비 +29% 이상) 종목을 과열 제외로 로드."""
        overheated: set[str] = set()
        try:
            conn = psycopg2.connect(**_get_db_params())
            cur = conn.cursor()
            cur.execute("""
                WITH recent AS (
                    SELECT stock_code, date, close,
                           LAG(close) OVER (PARTITION BY stock_code ORDER BY date) AS prev_close,
                           ROW_NUMBER() OVER (PARTITION BY stock_code ORDER BY date DESC) AS rn
                    FROM ohlcv_daily
                    WHERE date >= to_char(CURRENT_DATE - INTERVAL '10 days', 'YYYYMMDD')
                ), latest3 AS (
                    SELECT stock_code,
                           SUM(CASE WHEN prev_close > 0 AND close >= prev_close * 1.29 THEN 1 ELSE 0 END) AS limit_up_days,
                           COUNT(*) AS row_count
                    FROM recent
                    WHERE rn <= 3
                    GROUP BY stock_code
                )
                SELECT stock_code
                FROM latest3
                WHERE row_count >= 3 AND limit_up_days >= 3
            """)
            overheated = {str(row[0]).strip() for row in cur.fetchall() if str(row[0]).strip()}
            cur.close()
            conn.close()
        except Exception as e:
            logger.error("_load_overheated_stocks error: %s", e)
        self._overheated_stocks = overheated
        logger.info("ScalpingEntryEngine overheated stocks loaded: %d", len(self._overheated_stocks))

    def _load_open_positions_count(self) -> None:
        """포트폴리오별 오픈 포지션 수 로드."""
        if not self._cards:
            return
        try:
            conn = psycopg2.connect(**_get_db_params())
            cur = conn.cursor()
            pids = [c["portfolio_id"] for c in self._cards]
            cur.execute("""
                SELECT portfolio_id, COUNT(*) as cnt
                FROM go100_positions
                WHERE portfolio_id = ANY(%s) AND status = 'OPEN'
                GROUP BY portfolio_id
            """, (pids,))
            self._card_positions = {row[0]: row[1] for row in cur.fetchall()}
            cur.execute("""
                SELECT portfolio_id, stock_code
                FROM go100_positions
                WHERE portfolio_id = ANY(%s) AND status = 'OPEN'
            """, (pids,))
            held: dict[int, set[str]] = {}
            for pid, sc in cur.fetchall():
                held.setdefault(pid, set()).add(sc)
            self._card_held_stocks = held
            cur.close()
            conn.close()
        except Exception as e:
            logger.error("_load_open_positions_count error: %s", e)

    def _load_loss_cooldown_stocks(self) -> None:
        """최근 3일 내 -3% 이하 손절된 종목을 재진입 차단 목록에 로드."""
        try:
            conn = psycopg2.connect(**_get_db_params())
            cur = conn.cursor()
            cur.execute("""
                SELECT DISTINCT stock_code
                FROM go100_positions
                WHERE status = 'CLOSED'
                  AND pnl_pct IS NOT NULL
                  AND pnl_pct::numeric < -3.0
                  AND entry_date >= CURRENT_DATE - INTERVAL '3 days'
            """)
            self._loss_cooldown_stocks = {row[0] for row in cur.fetchall()}
            cur.close()
            conn.close()
            logger.info("ScalpingEntry: loss cooldown stocks: %d", len(self._loss_cooldown_stocks))
        except Exception as e:
            logger.error("_load_loss_cooldown_stocks error: %s", e)

    def _load_card119_preopen_expected_rows(self, cur) -> list[tuple]:
        """키움 0H 예상체결 등락률이 #119 진입 하한 이상인 장전 후보를 독립 발굴 후보로 적재."""
        try:
            from zoneinfo import ZoneInfo

            now_t = datetime.now(ZoneInfo("Asia/Seoul")).time()
        except Exception:
            now_t = datetime.now().time()
        if now_t >= MARKET_OPEN:
            return []

        ranked: list[tuple[str, float, float]] = []
        try:
            redis_client = sync_redis.from_url(
                os.environ.get("REDIS_URL", "redis://localhost:6379/0"),
                decode_responses=True,
                socket_timeout=0.5,
            )
            for key in redis_client.scan_iter("go100:kiwoom:0H:*", count=300):
                raw = redis_client.get(key)
                if not raw:
                    continue
                try:
                    payload = json.loads(raw)
                except Exception:
                    continue
                code = str(payload.get("stock_code") or key.rsplit(":", 1)[-1] or "").strip()
                if len(code) != 6 or not code.isdigit():
                    continue
                try:
                    expected_change = float(
                        payload.get("expected_change_rate")
                        if payload.get("expected_change_rate") is not None
                        else payload.get("change_rate")
                    )
                except (TypeError, ValueError):
                    continue
                if expected_change >= _CARD119_DISCOVERY_MIN_CHANGE_PCT:
                    ranked.append((code, expected_change, float(payload.get("expected_volume") or 0)))
        except Exception as exc:
            logger.debug("ScalpingEntryEngine #119 preopen expected Redis load skipped: %s", exc)
            return []

        if not ranked:
            return []
        ranked.sort(key=lambda item: (-item[1], item[0]))
        codes = [code for code, _change, _volume in ranked[:_CARD119_DISCOVERY_LIMIT]]
        expected_by_code = {code: (change, volume) for code, change, volume in ranked}
        cur.execute(
            """
            SELECT
                su.stock_code,
                COALESCE(su.stock_name, su.stock_code) AS stock_name,
                COALESCE((
                    SELECT od.close
                    FROM ohlcv_daily od
                    WHERE od.stock_code = su.stock_code
                      AND od.date < to_char(CURRENT_DATE, 'YYYYMMDD')
                    ORDER BY od.date DESC
                    LIMIT 1
                ), 0) AS prev_close,
                COALESCE(su.market_cap, 0) AS market_cap
            FROM stock_universe su
            WHERE su.stock_code = ANY(%s)
              AND COALESCE(su.is_active, true) = true
            """,
            (codes,),
        )
        rows: list[tuple] = []
        for code, stock_name, prev_close, market_cap in cur.fetchall():
            code = str(code or "").strip()
            if not code or _is_excluded_security_name(stock_name, stock_code=code):
                continue
            expected_change, expected_volume = expected_by_code.get(code, (0.0, 0.0))
            rows.append((
                code,
                stock_name,
                float(prev_close or 0),
                0,
                expected_volume,
                float(market_cap or 0),
                2500.0 + expected_change,
            ))
        return rows

    def _load_card119_snapshot_discovery_rows(self, cur) -> list[tuple]:
        """당일 실제 등락률/고가 등락률이 진입 하한 이상이고 거래대금 1억원 이상인 #119 독립 발굴 후보."""
        cur.execute(
            """
            WITH latest AS (
                SELECT DISTINCT ON (sps.stock_code)
                    sps.stock_code,
                    COALESCE(su.stock_name, sps.stock_code) AS stock_name,
                    COALESCE(sps.price, 0) AS price,
                    COALESCE(sps.high_price, sps.price, 0) AS high_price,
                    COALESCE(sps.volume, 0) AS volume,
                    COALESCE(sps.trade_amount, 0) AS trade_amount,
                    COALESCE(sps.market_cap, su.market_cap, 0) AS market_cap,
                    sps.snapshot_time
                FROM stock_price_snapshot sps
                LEFT JOIN stock_universe su ON su.stock_code = sps.stock_code
                WHERE sps.snapshot_time >= CURRENT_DATE
                  AND sps.snapshot_time < CURRENT_DATE + INTERVAL '1 day'
                  AND sps.snapshot_time >= now() - (%s::text || ' minutes')::interval
                  AND sps.stock_code ~ '^[0-9]{6}$'
                  AND COALESCE(su.is_active, true) = true
                ORDER BY sps.stock_code, sps.snapshot_time DESC
            ), prev_close AS (
                SELECT DISTINCT ON (stock_code)
                    stock_code, close AS prev_close
                FROM ohlcv_daily
                WHERE date < to_char(CURRENT_DATE, 'YYYYMMDD')
                ORDER BY stock_code, date DESC
            ), scored AS (
                SELECT l.stock_code, l.stock_name, l.volume, l.trade_amount, l.market_cap,
                       pc.prev_close,
                       CASE WHEN pc.prev_close > 0 AND l.price > 0
                            THEN (l.price / pc.prev_close - 1) * 100
                            ELSE NULL END AS change_pct,
                       CASE WHEN pc.prev_close > 0 AND l.high_price > 0
                            THEN (l.high_price / pc.prev_close - 1) * 100
                            ELSE NULL END AS high_change_pct
                FROM latest l
                LEFT JOIN prev_close pc ON pc.stock_code = l.stock_code
            )
            SELECT stock_code, stock_name, prev_close, trade_amount, volume, market_cap,
                   GREATEST(COALESCE(change_pct, 0), COALESCE(high_change_pct, 0)) AS score_pct
            FROM scored
            WHERE prev_close > 0
              AND GREATEST(COALESCE(change_pct, 0), COALESCE(high_change_pct, 0)) >= %s
              AND GREATEST(COALESCE(change_pct, 0), COALESCE(high_change_pct, 0)) <= 30.5
              AND CASE WHEN COALESCE(trade_amount, 0) > 0 AND COALESCE(trade_amount, 0) < 10000000
                       THEN COALESCE(trade_amount, 0) * 1000000
                       ELSE COALESCE(trade_amount, 0)
                  END >= %s
            ORDER BY score_pct DESC, trade_amount DESC, stock_code
            LIMIT %s
            """,
            (
                _LIMIT_UP_SNAPSHOT_WINDOW_MIN,
                _CARD119_DISCOVERY_MIN_CHANGE_PCT,
                _LIMITUP119_RELAXED_MIN_TRADE_VALUE,
                _CARD119_DISCOVERY_LIMIT,
            ),
        )
        rows: list[tuple] = []
        for code, stock_name, prev_close, trade_amount, volume, market_cap, score_pct in cur.fetchall():
            code = str(code or "").strip()
            if not code or _is_excluded_security_name(stock_name, stock_code=code):
                continue
            rows.append((
                code,
                stock_name,
                float(prev_close or 0),
                float(trade_amount or 0),
                float(volume or 0),
                float(market_cap or 0),
                2000.0 + float(score_pct or 0),
            ))
        return rows

    def _load_card119_independent_discovery_rows(self, cur) -> list[tuple]:
        """#119 독립 발굴: 공통 유니버스 제외, 장전 예상/장중 실제 진입 하드게이트 후보만 사용."""
        rows: list[tuple] = []
        seen: set[str] = set()
        for source_rows in (
            self._load_card119_preopen_expected_rows(cur),
            self._load_card119_snapshot_discovery_rows(cur),
        ):
            for row in source_rows:
                code = str(row[0] or "").strip()
                if code and code not in seen:
                    rows.append(row)
                    seen.add(code)
        logger.info(
            "ScalpingEntryEngine #119 independent discovery loaded: %d stocks (min_change=%.1f)",
            len(rows),
            _CARD119_DISCOVERY_MIN_CHANGE_PCT,
        )
        return rows

    def _load_universe(self) -> None:
        """GO100 스캘핑 유니버스에서 감시 대상 종목과 전일 기준값을 로드."""
        # A failed refresh must never leave a previously loaded #303 Top50 set
        # eligible for new buys. The broad universe is retained for other
        # cards/open-position processing, while #303 fails closed below.
        self._mahaseven_top50_codes = set()
        self._mahaseven_top30_codes = set()
        try:
            conn = psycopg2.connect(**_get_db_params())
            cur = conn.cursor()
            cur.execute("""
                SELECT
                    vu.stock_code,
                    vu.stock_name,
                    vu.close_price,
                    vu.avg_trade_value_20d,
                    vu.avg_volume_20d,
                    vu.market_cap,
                    vu.scalp_score
                FROM v4_scalping_universe vu
                LEFT JOIN stock_universe su ON su.stock_code = vu.stock_code
                WHERE vu.created_date = (SELECT MAX(created_date) FROM v4_scalping_universe)
                  AND COALESCE(vu.is_active, true) = true
                  AND COALESCE(su.is_active, true) = true
                  AND NOT (
                      UPPER(REPLACE(COALESCE(vu.stock_name, su.stock_name, ''), ' ', '')) LIKE 'KODEX%%'
                      OR UPPER(REPLACE(COALESCE(vu.stock_name, su.stock_name, ''), ' ', '')) LIKE 'TIGER%%'
                      OR UPPER(REPLACE(COALESCE(vu.stock_name, su.stock_name, ''), ' ', '')) LIKE 'ACE%%'
                      OR UPPER(REPLACE(COALESCE(vu.stock_name, su.stock_name, ''), ' ', '')) LIKE 'SOL%%'
                      OR UPPER(REPLACE(COALESCE(vu.stock_name, su.stock_name, ''), ' ', '')) LIKE 'KBSTAR%%'
                      OR UPPER(REPLACE(COALESCE(vu.stock_name, su.stock_name, ''), ' ', '')) LIKE 'HANARO%%'
                      OR UPPER(REPLACE(COALESCE(vu.stock_name, su.stock_name, ''), ' ', '')) LIKE 'ARIRANG%%'
                      OR UPPER(REPLACE(COALESCE(vu.stock_name, su.stock_name, ''), ' ', '')) LIKE 'KOSEF%%'
                      OR UPPER(REPLACE(COALESCE(vu.stock_name, su.stock_name, ''), ' ', '')) LIKE 'TIMEFOLIO%%'
                      OR UPPER(REPLACE(COALESCE(vu.stock_name, su.stock_name, ''), ' ', '')) LIKE 'RISE%%'
                      OR UPPER(REPLACE(COALESCE(vu.stock_name, su.stock_name, ''), ' ', '')) LIKE 'PLUS%%'
                      OR UPPER(REPLACE(COALESCE(vu.stock_name, su.stock_name, ''), ' ', '')) LIKE 'WON%%'
                      OR UPPER(REPLACE(COALESCE(vu.stock_name, su.stock_name, ''), ' ', '')) LIKE 'TREX%%'
                      OR REPLACE(COALESCE(vu.stock_name, su.stock_name, ''), ' ', '') LIKE '마이티%%'
                      OR UPPER(COALESCE(vu.stock_name, su.stock_name, '')) LIKE '%%ETF%%'
                      OR UPPER(COALESCE(vu.stock_name, su.stock_name, '')) LIKE '%%ETN%%'
                      OR COALESCE(vu.stock_name, su.stock_name, '') LIKE '%%레버리지%%'
                      OR COALESCE(vu.stock_name, su.stock_name, '') LIKE '%%인버스%%'
                      OR COALESCE(vu.stock_name, su.stock_name, '') LIKE '%%선물%%'
                      OR COALESCE(vu.stock_name, su.stock_name, '') LIKE '%%채권%%'
                      OR COALESCE(vu.stock_name, su.stock_name, '') LIKE '%%국채%%'
                      OR COALESCE(vu.stock_name, su.stock_name, '') LIKE '%%통안채%%'
                      OR COALESCE(vu.stock_name, su.stock_name, '') LIKE '%%스팩%%'
                      OR UPPER(COALESCE(vu.stock_name, su.stock_name, '')) LIKE '%%SPAC%%'
                      OR COALESCE(vu.stock_name, su.stock_name, '') LIKE '%%리츠%%'
                      OR UPPER(COALESCE(vu.stock_name, su.stock_name, '')) LIKE '%%REIT%%'
                      OR COALESCE(vu.stock_name, su.stock_name, '') LIKE '%%관리종목%%'
                      OR COALESCE(vu.stock_name, su.stock_name, '') LIKE '%%정리매매%%'
                      OR COALESCE(vu.stock_name, su.stock_name, '') LIKE '%%우선주%%'
                      OR COALESCE(vu.stock_name, su.stock_name, '') LIKE '%%우'
                      OR COALESCE(vu.stock_name, su.stock_name, '') LIKE '%%우B'
                      OR COALESCE(vu.stock_name, su.stock_name, '') LIKE '%%우C'
                  )
                ORDER BY COALESCE(vu.scalp_score, 0) DESC,
                         COALESCE(vu.avg_trade_value_20d, 0) DESC,
                         vu.stock_code
                LIMIT %s
            """, (_WS_UNIVERSE_LIMIT,))
            rows = list(cur.fetchall())
            try:
                cur.execute("""
                    SELECT stock_code
                    FROM stock_universe
                    WHERE COALESCE(is_active, true) = true
                      AND COALESCE(is_nxt, false) = true
                """)
                nxt_eligible_codes = {str(row[0] or "").strip() for row in cur.fetchall()}
            except Exception as e:
                logger.debug("ScalpingEntryEngine NXT eligibility load skipped: %s", e)
                nxt_eligible_codes = set()
            try:
                card119_rows = self._load_card119_independent_discovery_rows(cur)
            except Exception as e:
                logger.warning("ScalpingEntryEngine #119 independent discovery load failed: %s", e)
                card119_rows = []
            self._card119_discovery_universe = {
                str(row[0] or "").strip()
                for row in card119_rows
                if str(row[0] or "").strip()
            }
            cur.execute("""
                SELECT
                    gp.stock_code,
                    COALESCE(su.stock_name, gp.stock_code) AS stock_name,
                    COALESCE((
                        SELECT od.close
                        FROM ohlcv_daily od
                        WHERE od.stock_code = gp.stock_code
                          AND od.date < to_char(CURRENT_DATE, 'YYYYMMDD')
                        ORDER BY od.date DESC
                        LIMIT 1
                    ), gp.entry_price, gp.current_price, 0) AS close_price,
                    0 AS avg_trade_value_20d,
                    0 AS avg_volume_20d,
                    0 AS market_cap,
                    999 AS scalp_score
                FROM go100_positions gp
                LEFT JOIN stock_universe su ON su.stock_code = gp.stock_code
                WHERE gp.status = 'OPEN'
            """)
            seen = {row[0] for row in rows}
            for row in cur.fetchall():
                if row[0] not in seen:
                    rows.append(row)
                    seen.add(row[0])
            # #119는 공통 v4_scalping_universe가 아니라 실매매 하드게이트와 맞춘 독립 후보에서만 매매선정한다.
            # 다만 키움 WS 구독을 위해 독립 후보를 전체 틱 유니버스에는 강제 편입한다.
            for row in card119_rows:
                code = str(row[0] or "").strip()
                if code and code not in seen:
                    rows.append(row)
                    seen.add(code)
            # 장중 급등 후보는 Redis 랭킹 캐시가 비더라도 DB snapshot으로 보강한다.
            ranking_seen = {row[0] for row in rows}
            try:
                r = sync_redis.from_url(
                    os.environ.get("REDIS_URL", "redis://localhost:6379/0"),
                    decode_responses=True, socket_timeout=0.5,
                )
                today_iso = date.today().isoformat()
                for key in (
                    "go100:ranking:fluctuation:kospi",
                    "go100:ranking:fluctuation:kosdaq",
                ):
                    raw = r.get(key)
                    if not raw:
                        continue
                    for item in json.loads(raw):
                        code = str(item.get("stock_code") or "").strip()
                        if not code or code in ranking_seen or str(item.get("trade_date") or "") != today_iso:
                            continue
                        change_rate = float(item.get("change_rate") or 0)
                        current_price = float(item.get("current_price") or 0)
                        stock_name = item.get("stock_name") or code
                        if change_rate < 5.0 or current_price <= 0:
                            continue
                        if _is_excluded_security_name(stock_name, stock_code=code):
                            continue
                        prev_close = current_price / (1 + change_rate / 100.0) if change_rate > -99 else current_price
                        rows.append((
                            code,
                            stock_name,
                            prev_close,
                            0,
                            float(item.get("volume") or 0),
                            0,
                            1000,
                        ))
                        ranking_seen.add(code)
                        if len(ranking_seen) >= _WS_UNIVERSE_LIMIT:
                            break
                    if len(ranking_seen) >= _WS_UNIVERSE_LIMIT:
                        break
            except Exception as e:
                logger.debug("ScalpingEntryEngine realtime ranking merge skipped: %s", e)

            # 장중 급등 종목은 기본 유니버스가 이미 차 있어도 강제로 병합한다.
            # 단일 MAX(snapshot_time) 배치만 보면 종목별 최신 스냅샷이 빠질 수 있어 최근 N분 윈도우에서 종목별 최신값을 쓴다.
            try:
                cur.execute("""
                    WITH recent AS (
                        SELECT DISTINCT ON (sps.stock_code)
                            sps.stock_code,
                            COALESCE(su.stock_name, sps.stock_code) AS stock_name,
                            CASE
                                WHEN COALESCE(sps.change_pct, 0) > -99 AND COALESCE(sps.price, 0) > 0
                                    THEN COALESCE(sps.price, 0) / (1 + COALESCE(sps.change_pct, 0) / 100.0)
                                ELSE COALESCE(sps.price, 0)
                            END AS prev_close,
                            0 AS avg_trade_value_20d,
                            COALESCE(sps.volume, 0) AS avg_volume_20d,
                            COALESCE(sps.market_cap, 0) AS market_cap,
                            COALESCE(sps.change_pct, 0) AS scalp_score,
                            COALESCE(sps.trade_amount, 0) AS trade_amount,
                            sps.snapshot_time
                        FROM stock_price_snapshot sps
                        LEFT JOIN stock_universe su ON su.stock_code = sps.stock_code
                        WHERE sps.snapshot_time >= now() - (%s::text || ' minutes')::interval
                          AND sps.snapshot_time >= CURRENT_DATE
                          AND sps.snapshot_time < CURRENT_DATE + INTERVAL '1 day'
                          AND COALESCE(sps.change_pct, 0) >= %s
                        ORDER BY sps.stock_code, sps.snapshot_time DESC
                    )
                    SELECT stock_code, stock_name, prev_close, avg_trade_value_20d,
                           avg_volume_20d, market_cap, scalp_score
                    FROM recent
                    ORDER BY scalp_score DESC, trade_amount DESC, stock_code
                    LIMIT %s
                """, (_LIMIT_UP_SNAPSHOT_WINDOW_MIN, _LIMIT_UP_FORCE_INCLUDE_PCT, _WS_UNIVERSE_LIMIT))
                for row in cur.fetchall():
                    code = str(row[0] or "").strip()
                    stock_name = row[1] or code
                    if not code or code in ranking_seen or not code.isdigit() or len(code) != 6:
                        continue
                    if _is_excluded_security_name(stock_name, stock_code=code):
                        continue
                    rows.append(row)
                    ranking_seen.add(code)
            except Exception as e:
                logger.debug("ScalpingEntryEngine snapshot ranking merge skipped: %s", e)

            # #303 종목발굴 정본: 당일 등락률 하한 + 누적 거래대금 상위 N.
            # 절대 최소 거래대금/최대 등락률은 측정 근거가 없어 기본 미적용이며,
            # 명시적 환경설정이 있을 때만 SQL 게이트로 추가한다.
            try:
                _card303_optional_clauses: list[str] = []
                _card303_params: list[object] = [
                    CARD303_DISCOVERY_SNAPSHOT_FRESH_MINUTES,
                    CARD303_DISCOVERY_MIN_CHANGE_PCT,
                ]
                if CARD303_DISCOVERY_MAX_CHANGE_PCT is not None:
                    _card303_optional_clauses.append("AND change_pct <= %s")
                    _card303_params.append(CARD303_DISCOVERY_MAX_CHANGE_PCT)
                if CARD303_DISCOVERY_MIN_TRADING_VALUE_KRW is not None:
                    _card303_optional_clauses.append("AND trading_value_krw >= %s")
                    _card303_params.append(CARD303_DISCOVERY_MIN_TRADING_VALUE_KRW)
                _card303_params.append(CARD303_DISCOVERY_LIMIT)
                cur.execute(f"""
                    WITH latest AS (
                        SELECT DISTINCT ON (sps.stock_code)
                            sps.stock_code,
                            COALESCE(su.stock_name, sps.stock_code) AS stock_name,
                            COALESCE(sps.price, 0) AS price,
                            COALESCE(sps.change_pct, 0) AS change_pct,
                            CASE
                                WHEN NULLIF(sps.trade_amount, 0) IS NULL THEN
                                    COALESCE(sps.price, 0)::numeric * COALESCE(sps.volume, 0)::numeric
                                WHEN COALESCE(sps.price, 0)::numeric * COALESCE(sps.volume, 0)::numeric > 0
                                     AND NULLIF(sps.trade_amount, 0)::numeric <
                                         COALESCE(sps.price, 0)::numeric * COALESCE(sps.volume, 0)::numeric * 0.01
                                THEN COALESCE(sps.price, 0)::numeric * COALESCE(sps.volume, 0)::numeric
                                ELSE COALESCE(sps.trade_amount, 0)::numeric
                            END AS trading_value_krw
                        FROM stock_price_snapshot sps
                        LEFT JOIN stock_universe su ON su.stock_code = sps.stock_code
                        WHERE (sps.snapshot_time AT TIME ZONE 'Asia/Seoul')::date =
                                  (CURRENT_TIMESTAMP AT TIME ZONE 'Asia/Seoul')::date
                          AND sps.snapshot_time >= now() - (%s::text || ' minutes')::interval
                          AND sps.stock_code ~ '^[0-9]{{6}}$'
                          AND COALESCE(su.is_active, true) = true
                        ORDER BY sps.stock_code, sps.snapshot_time DESC
                    )
                    SELECT stock_code, stock_name, price, trading_value_krw, change_pct
                    FROM latest
                    WHERE change_pct >= %s
                      {' '.join(_card303_optional_clauses)}
                      AND NOT (
                          UPPER(REPLACE(COALESCE(stock_name, stock_code, ''), ' ', '')) LIKE 'KODEX%%'
                          OR UPPER(REPLACE(COALESCE(stock_name, stock_code, ''), ' ', '')) LIKE 'TIGER%%'
                          OR UPPER(REPLACE(COALESCE(stock_name, stock_code, ''), ' ', '')) LIKE 'ACE%%'
                          OR UPPER(REPLACE(COALESCE(stock_name, stock_code, ''), ' ', '')) LIKE 'SOL%%'
                          OR UPPER(REPLACE(COALESCE(stock_name, stock_code, ''), ' ', '')) LIKE 'KBSTAR%%'
                          OR UPPER(REPLACE(COALESCE(stock_name, stock_code, ''), ' ', '')) LIKE 'HANARO%%'
                          OR UPPER(REPLACE(COALESCE(stock_name, stock_code, ''), ' ', '')) LIKE 'ARIRANG%%'
                          OR UPPER(REPLACE(COALESCE(stock_name, stock_code, ''), ' ', '')) LIKE 'KOSEF%%'
                          OR UPPER(REPLACE(COALESCE(stock_name, stock_code, ''), ' ', '')) LIKE 'TIMEFOLIO%%'
                          OR UPPER(REPLACE(COALESCE(stock_name, stock_code, ''), ' ', '')) LIKE 'RISE%%'
                          OR UPPER(REPLACE(COALESCE(stock_name, stock_code, ''), ' ', '')) LIKE 'PLUS%%'
                          OR UPPER(REPLACE(COALESCE(stock_name, stock_code, ''), ' ', '')) LIKE 'WON%%'
                          OR UPPER(REPLACE(COALESCE(stock_name, stock_code, ''), ' ', '')) LIKE 'TREX%%'
                          OR REPLACE(COALESCE(stock_name, stock_code, ''), ' ', '') LIKE '마이티%%'
                          OR UPPER(COALESCE(stock_name, stock_code, '')) LIKE '%%ETF%%'
                          OR UPPER(COALESCE(stock_name, stock_code, '')) LIKE '%%ETN%%'
                          OR COALESCE(stock_name, stock_code, '') LIKE '%%레버리지%%'
                          OR COALESCE(stock_name, stock_code, '') LIKE '%%인버스%%'
                          OR COALESCE(stock_name, stock_code, '') LIKE '%%선물%%'
                          OR COALESCE(stock_name, stock_code, '') LIKE '%%채권%%'
                          OR COALESCE(stock_name, stock_code, '') LIKE '%%국채%%'
                          OR COALESCE(stock_name, stock_code, '') LIKE '%%통안채%%'
                          OR COALESCE(stock_name, stock_code, '') LIKE '%%스팩%%'
                          OR UPPER(COALESCE(stock_name, stock_code, '')) LIKE '%%SPAC%%'
                          OR COALESCE(stock_name, stock_code, '') LIKE '%%리츠%%'
                          OR UPPER(COALESCE(stock_name, stock_code, '')) LIKE '%%REIT%%'
                          OR COALESCE(stock_name, stock_code, '') LIKE '%%관리종목%%'
                          OR COALESCE(stock_name, stock_code, '') LIKE '%%정리매매%%'
                          OR COALESCE(stock_name, stock_code, '') LIKE '%%우선주%%'
                          OR COALESCE(stock_name, stock_code, '') LIKE '%%우'
                          OR COALESCE(stock_name, stock_code, '') LIKE '%%우B'
                          OR COALESCE(stock_name, stock_code, '') LIKE '%%우C'
                      )
                    ORDER BY trading_value_krw DESC, change_pct DESC, stock_code ASC
                    LIMIT %s
                """, tuple(_card303_params))
                self._mahaseven_top50_codes = set()
                for _mh_row in cur.fetchall():
                    _mh_code = str(_mh_row[0] or "").strip()
                    _mh_name = _mh_row[1] or _mh_code
                    if (
                        _mh_code
                        and _mh_code.isdigit()
                        and len(_mh_code) == 6
                        and not _is_excluded_security_name(_mh_name, stock_code=_mh_code)
                    ):
                        self._mahaseven_top50_codes.add(_mh_code)
                        if _mh_code not in ranking_seen:
                            _mh_price = float(_mh_row[2] or 0)
                            if _mh_price > 0:
                                rows.append((
                                    _mh_code,
                                    _mh_name,
                                    _mh_price,
                                    float(_mh_row[3] or 0),
                                    0,
                                    0,
                                    500,
                                ))
                                ranking_seen.add(_mh_code)
                logger.info(
                    "ScalpingEntryEngine: mahaseven_top50 loaded: %d stocks",
                    len(self._mahaseven_top50_codes),
                )
            except Exception as e:
                logger.debug("ScalpingEntryEngine mahaseven_top50 load skipped: %s", e)
                self._mahaseven_top50_codes = set()
            # Legacy attribute remains an alias for old cards/config serializers.
            self._mahaseven_top30_codes = self._mahaseven_top50_codes

            _effective_evaluation_limit = (
                _WS_UNIVERSE_LIMIT
                if _DIRECT_WS_SUBSCRIPTION_SYNC_ENABLED
                else _COLLECTOR_EVALUATION_UNIVERSE_LIMIT
            )
            if _effective_evaluation_limit > 0 and len(rows) > _effective_evaluation_limit:
                rows = sorted(
                    rows,
                    key=lambda row: (
                        (10000.0 if str(row[0] or "").strip() in self._card119_discovery_universe else 0.0)
                        + (9000.0 if str(row[0] or "").strip() in self._mahaseven_top50_codes else 0.0)
                        + float(row[6] or 0),
                        float(row[3] or 0),
                        str(row[0] or ""),
                    ),
                    reverse=True,
                )[:_effective_evaluation_limit]

            self._universe = {row[0] for row in rows}
            self._universe_meta = {
                row[0]: {
                    "stock_name": row[1],
                    "close_price": float(row[2] or 0),
                    "avg_trade_value_20d": float(row[3] or 0),
                    "avg_volume_20d": float(row[4] or 0),
                    "market_cap": float(row[5] or 0),
                    "scalp_score": float(row[6] or 0),
                    "is_nxt": str(row[0] or "").strip() in nxt_eligible_codes,
                    "theme_count": 0,
                    "news_score": 0.0,
                    **{field: None for field in _LIMITUP119_REASON_FEATURE_FIELDS},
                }
                for row in rows
            }

            # [P2] 테마등급/뉴스점수 배치 적재 — 진입 lock_score 가중에 사용한다.
            # 틱마다 DB를 조회할 수 없으므로 유니버스 로드 시점(5분 주기)에 사전 적재한다.
            _codes = [c for c in self._universe if c]
            if _codes:
                try:
                    cur.execute("""
                        SELECT ts.stock_code, COUNT(DISTINCT ts.theme_code) AS theme_count
                        FROM v4_theme_stock ts
                        WHERE ts.stock_code = ANY(%s)
                          AND ts.mapped_date >= CURRENT_DATE - 7
                        GROUP BY ts.stock_code
                    """, (_codes,))
                    for _tc in cur.fetchall():
                        m = self._universe_meta.get(_tc[0])
                        if m is not None:
                            m["theme_count"] = int(_tc[1] or 0)
                except Exception as e:
                    logger.debug("ScalpingEntryEngine theme_count load skipped: %s", e)
                try:
                    cur.execute("""
                        SELECT stock_code1,
                               (AVG(COALESCE(llm_sentiment, sentiment_score, 0)) + 1.0) / 2.0
                        FROM go100_news_items
                        WHERE stock_code1 = ANY(%s)
                          AND data_date >= CURRENT_DATE - 1
                          AND stock_code1 IS NOT NULL AND stock_code1 != ''
                        GROUP BY stock_code1
                    """, (_codes,))
                    for _ns in cur.fetchall():
                        m = self._universe_meta.get(_ns[0])
                        if m is not None:
                            m["news_score"] = max(min(float(_ns[1] or 0), 1.0), 0.0)
                except Exception as e:
                    logger.debug("ScalpingEntryEngine news_score load skipped: %s", e)
                try:
                    cur.execute("""
                        SELECT stock_code,
                               theme_strength_intraday, theme_peer_limitup_count, theme_peer_avg_change_pct,
                               kospi_return_1d, kosdaq_return_1d, market_breadth, vkospi,
                               regime_label, regime_score,
                               strength_0900_1000, strength_1000_1100, strength_after_lock, volume_burst_ratio_5m
                        FROM go100_limitup_reason_features_shadow
                        WHERE stock_code = ANY(%s)
                          AND trade_date = CURRENT_DATE
                    """, (_codes,))
                    for _rf in cur.fetchall():
                        m = self._universe_meta.get(str(_rf[0] or "").strip())
                        if m is None:
                            continue
                        for _idx, _field in enumerate(_LIMITUP119_REASON_FEATURE_FIELDS, start=1):
                            m[_field] = _rf[_idx]
                except Exception as e:
                    logger.debug("ScalpingEntryEngine #119 reason feature load skipped: %s", e)

            cur.close()
            conn.close()
            self._sync_subscription_targets()
            logger.info(
                "ScalpingEntryEngine: universe %d stocks loaded "
                "(direct_ws_sync=%s, evaluation_limit=%d, card119_independent=%d, card303=%d)",
                len(self._universe),
                _DIRECT_WS_SUBSCRIPTION_SYNC_ENABLED,
                _effective_evaluation_limit,
                len(self._card119_discovery_universe),
                len(self._mahaseven_top50_codes),
            )
        except Exception as e:
            logger.error("_load_universe error: %s", e)

    def _sync_subscription_targets(self) -> None:
        """Optionally sync the universe to a directly owned legacy WS collector.

        The default collector-shard path deliberately returns here: collection
        services own broker accounts and persist ticks/bars, while this engine
        only consumes their DB output.
        """
        if not _DIRECT_WS_SUBSCRIPTION_SYNC_ENABLED:
            logger.debug(
                "WS subscription sync skipped: collector shards own market-data subscriptions"
            )
            return
        valid_codes = sorted(code for code in self._universe if code and code.isdigit() and len(code) == 6)

        # KIS WS 동적 구독
        try:
            from backend.app.services.data.kis_ws_collector import get_dynamic_subscription_controller
            controller = get_dynamic_subscription_controller()
            if controller is not None:
                try:
                    loop = asyncio.get_running_loop()
                    loop.create_task(controller.set_target_codes(valid_codes))
                except RuntimeError:
                    pass
        except Exception as e:
            logger.debug("KIS WS subscription sync skipped: %s", e)

        # [P0-DYNSUB-STABILITY] 키움 WS는 장중 REG/REMOVE 직후 1006 종료가 반복됐다.
        # 실행 중 연결에는 직접 subscribe/unsubscribe를 보내지 않고, 다음 재연결/login 때
        # 갱신된 코드 목록이 적용되도록 collector의 target만 교체한다.
        if self._kiwoom_ws is not None:
            try:
                old_codes = set(getattr(self._kiwoom_ws, '_stock_codes', None) or [])
                new_codes_set = set(valid_codes)
                added = new_codes_set - old_codes
                removed = old_codes - new_codes_set
                if added or removed:
                    self._kiwoom_ws.set_stock_codes(valid_codes)
                    logger.info(
                        "Kiwoom WS dynamic resub deferred: %d->%d codes (added=%d removed=%d)",
                        len(old_codes), len(new_codes_set),
                        len(added), len(removed),
                    )
                # [P0-4 2026-08-06] deferred 구독은 "다음 재연결"에만 적용된다.
                # WS 세션이 장중 내내 유지되면 신규 유니버스 종목은 끝까지 구독되지 않아
                # tick_stale_or_missing 으로 전량 차단된다(실측: card #303 15,743건).
                # 따라서 신규 편입 종목이 임계치 이상이고 쿨다운이 지났을 때만
                # WS 세션을 정상 종료시켜 재연결 경로에서 구독을 갱신한다.
                # 1006 반복 종료를 막기 위해 쿨다운/임계치는 환경변수로 제한한다.
                if added and len(added) >= _RESUB_MIN_ADDED:
                    _now_mono = time_module.monotonic()
                    if _now_mono - self._last_kiwoom_resub_ts >= _RESUB_COOLDOWN_SEC:
                        self._last_kiwoom_resub_ts = _now_mono
                        try:
                            loop = asyncio.get_running_loop()
                            loop.create_task(self._kiwoom_ws.disconnect())
                            logger.warning(
                                "Kiwoom WS graceful resubscribe triggered: added=%d (cooldown=%ds)",
                                len(added), int(_RESUB_COOLDOWN_SEC),
                            )
                        except RuntimeError:
                            pass
            except Exception as e:
                logger.debug("Kiwoom WS subscription sync skipped: %s", e)

    # ── 장 시간 / 한도 체크 ────────────────────────────────────────────

    def _is_entry_allowed(self) -> bool:
        """장 시간 내인지 확인.
        [2026-08-06 CEO 지시] 일일 매수횟수 count 기반 차단은 제거됨.
        동시 보유 종목 수(max_stocks) 제한과 DUPBLOCK은 _execute_buy에서 별도 적용.
        NXT AM/PM은 GO100_SCALPING_NXT_ENTRY_ENABLED=true일 때만 허용.
        """
        try:
            from zoneinfo import ZoneInfo
            now_t = datetime.now(ZoneInfo("Asia/Seoul")).time()
        except Exception:
            now_t = datetime.now().time()

        if MARKET_OPEN <= now_t <= MARKET_CLOSE:
            return True

        if NXT_PRE_OPEN <= now_t <= NXT_PRE_CLOSE and _is_scalping_nxt_entry_enabled():
            return True
        if NXT_AFTER_OPEN <= now_t <= NXT_AFTER_CLOSE and _is_scalping_nxt_pm_entry_enabled():
            return True

        return False

    def _reset_daily_if_needed(self) -> None:
        today = date.today()
        if self._last_reset_date != today:
            self._daily_buy_count = 0
            self._bought_today.clear()
            self._bought_today_ts.clear()
            self._session_high.clear()
            self._tick_history.clear()
            self._vwap_data.clear()
            self._minute_bars.clear()
            self._minute_bar_current.clear()
            self._failed_cooldown.clear()
            self._failed_count.clear()
            self._loss_cooldown_stocks.clear()
            self._last_loss_cooldown_load = 0.0
            # [P0-FLAT-COOLDOWN] 일일 리셋
            self._last_eval_tick.clear()
            self._flat_skip_count = 0
            # [2026-08-19 P0] L0/리스크 게이트 일일 리셋
            self._time_25pct.clear()
            self._consecutive_loss_count.clear()
            self._daily_pnl_by_card.clear()
            self._last_risk_state_load = 0.0
            self._stopped_out_today.clear()
            self._wave_failure_blacklist.clear()
            self._last_reset_date = today

    # ── 감사 로그 ──────────────────────────────────────────────────────

    def _audit_decision(
        self,
        *,
        card: dict,
        stock_code: str,
        stage: str,
        decision: str,
        reason_code: str,
        reason_text: str,
        metrics: dict | None = None,
        throttle_seconds: float = 300.0,
    ) -> None:
        """카드별 후보 발굴/탈락/주문 결과를 DB 감사 로그에 남긴다."""
        card_id = int(card.get("card_id") or 0)
        portfolio_id = int(card.get("portfolio_id") or 0)
        key = (card_id, portfolio_id, stock_code, stage, decision, reason_code)
        now = time_module.monotonic()
        if throttle_seconds > 0 and now - self._audit_throttle.get(key, 0.0) < throttle_seconds:
            return
        self._audit_throttle[key] = now

        meta = self._universe_meta.get(stock_code, {})
        payload = {
            "stock_name": meta.get("stock_name"),
            "strategy_name": card.get("strategy_name"),
            "account_id": card.get("account_id"),
            "config_id": card.get("config_id"),
            **(metrics or {}),
        }
        try:
            conn = psycopg2.connect(**_get_db_params())
            cur = conn.cursor()
            cur.execute("""
                INSERT INTO go100_trade_decision_logs (
                    event_type, source, user_id, portfolio_id, go100_card_id,
                    stock_code, trade_date, stage, decision, reason_code,
                    reason_text, metrics_json, created_at
                ) VALUES (
                    'scalping_entry_audit', 'scalping_entry_engine', %s, %s, %s,
                    %s, CURRENT_DATE, %s, %s, %s,
                    %s, CAST(%s AS jsonb), now()
                )
            """, (
                card.get("user_id"), portfolio_id, card_id, stock_code,
                stage, decision, reason_code, reason_text,
                json.dumps(payload, default=str, ensure_ascii=False),
            ))
            conn.commit()
            try:
                cur.execute("""
                    INSERT INTO go100_strategy_run_events (
                        event_type, source, user_id, portfolio_id, go100_card_id,
                        stock_code, trade_date, event_phase, stage, decision,
                        reason_code, reason_text, data_quality_status,
                        input_snapshot_json, metrics_json, raw_event_json,
                        is_paper, trade_group_id, card_version, source_table,
                        source_ts, received_at, created_at
                    ) VALUES (
                        'scalping_entry_audit', 'scalping_entry_engine', %s, %s, %s,
                        %s, CURRENT_DATE, %s, %s, %s,
                        %s, %s, %s,
                        CAST(%s AS jsonb), CAST(%s AS jsonb), CAST(%s AS jsonb),
                        %s, %s,
                        (SELECT COALESCE(card_version, version, 1) FROM go100_strategy_cards
                         WHERE go100_card_id = %s LIMIT 1),
                        'scalping_entry_engine', CAST(%s AS timestamptz), now(), now()
                    )
                """, (
                    card.get("user_id"), portfolio_id, card_id, stock_code,
                    stage, stage, decision, reason_code, reason_text,
                    payload.get("data_quality_status"),
                    json.dumps(payload.get("input_snapshot") or {}, default=str, ensure_ascii=False),
                    json.dumps(payload, default=str, ensure_ascii=False),
                    json.dumps({
                        "event_type": "scalping_entry_audit",
                        "source": "scalping_entry_engine",
                        "go100_card_id": card_id,
                        "stock_code": stock_code,
                        "stage": stage,
                        "decision": decision,
                        "reason_code": reason_code,
                        "reason_text": reason_text,
                        "metrics": payload,
                    }, default=str, ensure_ascii=False),
                    bool(card.get("is_paper", False)),
                    payload.get("trade_group_id") or f"{card_id}:{stock_code}:{date.today().isoformat()}",
                    card_id,
                    payload.get("source_ts") or datetime.now().astimezone().isoformat(),
                ))
                conn.commit()
            except Exception as event_exc:
                conn.rollback()
                logger.debug(
                    "ScalpingEntry strategy event skipped card=%s stock=%s reason=%s: %s",
                    card_id, stock_code, reason_code, event_exc,
                )
            cur.close()
            conn.close()
        except Exception as e:
            logger.debug("ScalpingEntry audit skipped card=%s stock=%s reason=%s: %s", card_id, stock_code, reason_code, e)

    def _tick_metrics(self, stock_code: str, tick: tuple) -> dict:
        price = abs(float(tick[2] or 0))
        signed_volume = float(tick[3] or 0)
        volume = abs(signed_volume)
        cum_volume = float(tick[4] or 0) if len(tick) > 4 else 0.0
        strength = float(tick[6] or 0) if len(tick) > 6 else 0.0
        meta = self._universe_meta.get(stock_code, {})
        prev_close = float(meta.get("close_price") or 0)
        intraday_pct = (price / prev_close - 1) * 100 if price > 0 and prev_close > 0 else None
        avg_volume = float(meta.get("avg_volume_20d") or 0)
        volume_ratio = (cum_volume / avg_volume) if avg_volume > 0 and cum_volume > 0 else 0.0
        trade_value = price * cum_volume if cum_volume > 0 else 0.0
        return {
            "price": price,
            "tick_volume": volume,
            "signed_tick_volume": signed_volume,
            "cum_volume": cum_volume,
            "strength": strength,
            "prev_close": prev_close,
            "intraday_pct": intraday_pct,
            "session_high": float(self._session_high.get(stock_code) or 0),
            "avg_volume_20d": avg_volume,
            "volume_ratio": volume_ratio,
            "trade_value": trade_value,
            "history_len": len(self._tick_history[stock_code]),
            "tick_age_seconds": _tick_age_seconds(tick[1] if len(tick) > 1 else None),
        }

    def _audit_limit_up_pre_card_skip(
        self,
        *,
        stock_code: str,
        tick: tuple,
        reason_code: str,
        reason_text: str,
        extra_metrics: dict | None = None,
    ) -> None:
        """카드 루프 전 필터로 사라지는 상한가 접근 후보도 카드별 감사 로그에 남긴다."""
        for card in self._cards:
            if not _has_limit_up_entry_rules(card.get("entry_rules", [])):
                continue
            metrics = self._tick_metrics(stock_code, tick)
            intraday_pct = metrics.get("intraday_pct")
            try:
                watch_floor = min(
                    20.0,
                    float(self._parse_limit_up_entry_params(card).get("entry_min_intraday_pct", 25.0)),
                )
            except Exception:
                watch_floor = 20.0
            if intraday_pct is None or float(intraday_pct) < watch_floor:
                continue
            self._audit_decision(
                card=card,
                stock_code=stock_code,
                stage="pre_entry",
                decision="skip",
                reason_code=reason_code,
                reason_text=reason_text,
                metrics={
                    **metrics,
                    **(extra_metrics or {}),
                    "audit_scope": "limit_up_watch_pre_card_filter",
                    "watch_floor_pct": watch_floor,
                },
            )

    def _audit_limit_up_snapshot_candidates(self) -> None:
        """스냅샷 기준 상한가 접근 후보가 카드 평가 전 사라진 이유를 카드별로 기록한다."""
        limit_cards = [c for c in self._cards if _has_limit_up_entry_rules(c.get("entry_rules", []))]
        if not limit_cards:
            return
        try:
            conn = psycopg2.connect(**_get_db_params())
            cur = conn.cursor()
            cur.execute("""
                WITH recent AS (
                    SELECT DISTINCT ON (sps.stock_code)
                        sps.stock_code,
                        COALESCE(su.stock_name, sps.stock_code) AS stock_name,
                        COALESCE(sps.price, 0) AS price,
                        COALESCE(sps.change_pct, 0) AS change_pct,
                        COALESCE(sps.trade_amount, 0) AS trade_amount,
                        COALESCE(sps.market_cap, 0) AS market_cap,
                        sps.snapshot_time
                    FROM stock_price_snapshot sps
                    LEFT JOIN stock_universe su ON su.stock_code = sps.stock_code
                    WHERE sps.snapshot_time >= now() - (%s::text || ' minutes')::interval
                      AND sps.snapshot_time >= CURRENT_DATE
                      AND sps.snapshot_time < CURRENT_DATE + INTERVAL '1 day'
                      AND COALESCE(sps.change_pct, 0) >= %s
                    ORDER BY sps.stock_code, sps.snapshot_time DESC
                )
                SELECT stock_code, stock_name, price, change_pct, trade_amount, market_cap, snapshot_time
                FROM recent
                ORDER BY change_pct DESC, trade_amount DESC, stock_code
                LIMIT 120
            """, (_LIMIT_UP_SNAPSHOT_WINDOW_MIN, _LIMIT_UP_WATCH_FLOOR_PCT,))
            rows = cur.fetchall()
            cur.close()
            conn.close()
        except Exception as e:
            logger.debug("ScalpingEntryEngine limit-up snapshot audit skipped: %s", e)
            return

        for stock_code, stock_name, price, change_pct, trade_amount, market_cap, snapshot_time in rows:
            code = str(stock_code or "").strip()
            if not code:
                continue
            base_metrics = {
                "audit_scope": "limit_up_watch_candidate_generation",
                "stock_name": stock_name,
                "price": float(price or 0),
                "intraday_pct": float(change_pct or 0),
                "trade_amount": float(trade_amount or 0),
                "market_cap": float(market_cap or 0),
                "snapshot_time": snapshot_time,
                "watch_floor_pct": _LIMIT_UP_WATCH_FLOOR_PCT,
                "in_entry_universe": code in self._universe,
            }
            if code in self._manual_excluded_stocks:
                reason_code = "manual_global_excluded"
                reason_text = "스크리너 전역 선택 제외종목으로 카드 평가 전 차단"
                decision = "skip"
            elif code in self._overheated_stocks:
                reason_code = "overheated_limit_up_3days"
                reason_text = "최근 3거래일 연속 상한가 과열 종목으로 카드 평가 전 차단"
                decision = "skip"
            elif code not in self._universe:
                reason_code = "snapshot_not_in_entry_universe"
                reason_text = "상한가 접근 스냅샷 후보이나 실시간 진입 유니버스에 없어 카드 평가 전 제외"
                decision = "skip"
            else:
                reason_code = "limit_up_watch_candidate"
                reason_text = "상한가 접근 스냅샷 후보로 실시간 카드 평가 대상"
                decision = "watch"

            for card in limit_cards:
                card_id = int(card.get("card_id") or 0)
                card_excluded = self._card_excluded_stocks.get(card_id, set())
                card_reason_code = reason_code
                card_reason_text = reason_text
                card_decision = decision
                if code in card_excluded:
                    card_reason_code = "manual_card_excluded"
                    card_reason_text = "전략카드별 선택 제외종목으로 카드 평가 전 차단"
                    card_decision = "skip"
                self._audit_decision(
                    card=card,
                    stock_code=code,
                    stage="candidate_generation",
                    decision=card_decision,
                    reason_code=card_reason_code,
                    reason_text=card_reason_text,
                    metrics=base_metrics,
                )

    def _compute_lock_score(self, stock_code: str, metrics: dict, card: dict) -> float:
        """실시간 메트릭 + [P2] 테마등급/뉴스점수 기반 진입 품질 점수 (0~100).

        구성:
          - 실시간 메트릭 (0~80): 거래량배수24 + 체결강도24 + 당일등락률16 + 거래대금16
          - [P2] 테마등급 (0~10): 종목이 속한 활성 테마 개수 기반
          - [P2] 뉴스점수 (0~10): go100_news_items 감성점수 기반 (0~1 정규화)
        테마/뉴스 데이터가 없으면 해당 항목 0점(실시간 메트릭만으로 최대 80점).
        """
        score = 0.0
        # 거래량 배수 (0~24): volume_ratio 1.5x=12, 3x=24 (cap)
        vr = min(float(metrics.get("volume_ratio") or 0), 3.0)
        score += (vr / 3.0) * 24.0
        # 체결강도 (0~24): 100=0, 125=12, 150+=24
        strength = float(metrics.get("strength") or 0)
        s_norm = min(max(strength - 100, 0) / 50.0, 1.0)
        score += s_norm * 24.0
        # 당일등락률 (0~16): 5%=3.2, 15%=9.6, 25%+=16
        ipct = float(metrics.get("intraday_pct") or 0)
        i_norm = min(max(ipct, 0) / 25.0, 1.0)
        score += i_norm * 16.0
        # 거래대금 (0~16): 10억=3.2, 30억=9.6, 50억+=16
        tv = float(metrics.get("trade_value") or 0)
        t_norm = min(tv / 5_000_000_000, 1.0)
        score += t_norm * 16.0

        # [P2] 테마등급/뉴스점수 — _load_universe가 universe_meta에 사전 적재한 배치값 사용
        meta = self._universe_meta.get(stock_code, {})
        # 테마등급 (0~10): 소속 활성 테마 1개=4, 2개=7, 3개+=10
        theme_count = int(meta.get("theme_count") or 0)
        if theme_count >= 3:
            theme_bonus = 10.0
        elif theme_count == 2:
            theme_bonus = 7.0
        elif theme_count == 1:
            theme_bonus = 4.0
        else:
            theme_bonus = 0.0
        score += theme_bonus
        # 뉴스점수 (0~10): composite score(0~1) * 10
        news_score = min(max(float(meta.get("news_score") or 0), 0.0), 1.0)
        score += news_score * 10.0

        # [GO100-303 P0] 수익성 우선순위 보강: 재료 + 연속성 + 리스크 패널티.
        # 1주 실매매 테스트 중이므로 신규 진입을 과도하게 막지 않고, lock_score와 감사 메트릭에 반영한다.
        material_score = theme_bonus + news_score * 10.0
        if float(metrics.get("volume_ratio") or 0) >= 3.0:
            material_score += 5.0
        if float(metrics.get("trade_value") or 0) >= 5_000_000_000:
            material_score += 5.0
        if float(metrics.get("orderbook_ratio") or 0) >= 1.5:
            material_score += 5.0
        material_score = min(material_score, 30.0)

        continuity_score = 0.0
        if metrics.get("ma_status") == "pullback_ok":
            continuity_score += 10.0
        if float(metrics.get("price_vs_ma_pct") or 999.0) <= 0.5:
            continuity_score += 5.0
        if int(metrics.get("rising_ticks_3") or 0) >= 2:
            continuity_score += 5.0
        closes = list(self._minute_bars.get(stock_code) or [])[-5:]
        if len(closes) >= 3:
            rising_closes = sum(1 for i in range(len(closes) - 1) if float(closes[i]) < float(closes[i + 1]))
            if rising_closes >= 3:
                continuity_score += 10.0
            elif rising_closes >= 2:
                continuity_score += 5.0
        continuity_score = min(continuity_score, 30.0)

        risk_penalty = 0.0
        dq_status = str(metrics.get("data_quality_status") or "").upper()
        if dq_status == "CRITICAL":
            risk_penalty = 30.0
        elif dq_status == "WARN":
            risk_penalty = 10.0

        profitability_priority_score = max(material_score + continuity_score - risk_penalty, 0.0)
        score += min(profitability_priority_score * 0.35, 20.0)

        # CEO rule: 오전장 상한가 사전 포착은 우선순위를 80% 가산한다.
        # 오후장은 별도 가산 없이 after_14_min_pct/final_price_position 조건으로 보수적으로 평가한다.
        try:
            from zoneinfo import ZoneInfo
            now_t = datetime.now(ZoneInfo("Asia/Seoul")).time()
        except Exception:
            now_t = datetime.now().time()
        time_weight_multiplier = 1.0
        time_weight_reason = "regular"
        if dt_time(9, 0, 0) <= now_t < dt_time(12, 0, 0):
            time_weight_multiplier = 1.8
            time_weight_reason = "morning_80pct_bonus"
            score *= time_weight_multiplier
        elif now_t >= dt_time(14, 0, 0):
            time_weight_reason = "afternoon_caution_no_bonus"

        # 디버깅/감사용 메트릭 노출
        metrics["theme_count"] = theme_count
        metrics["news_score"] = round(news_score, 3)
        metrics["material_score"] = round(material_score, 1)
        metrics["continuity_score"] = round(continuity_score, 1)
        metrics["risk_penalty"] = round(risk_penalty, 1)
        metrics["profitability_priority_score"] = round(profitability_priority_score, 1)
        metrics["time_weight_multiplier"] = time_weight_multiplier
        metrics["time_weight_reason"] = time_weight_reason
        return round(min(score, 100.0), 1)

    def _profitability_priority_sort_score(self, tick: tuple) -> float:
        """Queue-level tie breaker for simultaneous entries before full rule evaluation."""
        stock_code = str(tick[0] or "")
        meta = self._universe_meta.get(stock_code, {})
        price = float(tick[2] or 0)
        volume = abs(float(tick[3] or 0)) if len(tick) > 3 else 0.0
        strength = float(tick[6] or 0) if len(tick) > 6 else 0.0
        close_price = float(meta.get("close_price") or 0)
        intraday_pct = ((price - close_price) / close_price * 100.0) if close_price > 0 else 0.0
        material = (
            min(int(meta.get("theme_count") or 0), 3) * 4.0
            + min(max(float(meta.get("news_score") or 0), 0.0), 1.0) * 10.0
            + min(float(meta.get("scalp_score") or 0) * 0.02, 20.0)
        )
        continuity = 0.0
        if strength >= 120:
            continuity += 8.0
        if intraday_pct > 0:
            continuity += min(intraday_pct, 10.0)
        if volume > 0:
            continuity += min(volume / 1000.0, 8.0)
        return material * 5.0 + continuity * 4.0 + float(meta.get("avg_trade_value_20d") or 0) / 100_000_000

    def _parse_limit_up_entry_params(self, card: dict) -> dict:
        rules = card.get("entry_rules", [])
        if not isinstance(rules, list):
            rules = []
        by_name = {}
        for r in rules:
            if isinstance(r, dict):
                by_name[r.get("name", "")] = r.get("params", {})
        luc = by_name.get("limit_up_close_confirmation", {})
        tap = by_name.get("trade_amount_priority", {})
        vsp = by_name.get("volume_surge_persistence", {})
        mtm = by_name.get("morning_top_mover_tracking", {})
        # CEO rule: #119 상한가 사전포착형은 당일 +27% 이상 종목만 실매수 진입한다.
        # DB 카드 파라미터가 낮게 저장돼도 실매매 엔진에서는 27% 하한을 강제한다.
        entry_floor = max(
            _CARD119_ENTRY_MIN_CHANGE_PCT,
            float(mtm.get("min_intraday_pct", _CARD119_ENTRY_MIN_CHANGE_PCT)),
            float(luc.get("entry_min_intraday_pct", _CARD119_ENTRY_MIN_CHANGE_PCT)),
        )
        return {
            "min_intraday_pct": entry_floor,
            "entry_min_intraday_pct": entry_floor,
            "max_entry_pct": float(luc.get("max_entry_pct", 30.0)),
            "after_11_min_pct": max(entry_floor, float(luc.get("after_11_min_pct", 25.0))),
            "after_14_min_pct": max(entry_floor, float(luc.get("after_14_min_pct", 25.0))),
            "min_price_position": float(luc.get("min_price_position", 0.93)),
            "final_price_position": float(luc.get("final_price_position", 0.97)),
            "min_amount_krw": float(tap.get("min_amount_krw", 2_000_000_000)),
            # [2026-08-19 P0 L0] 거래대금 과밀 상한. 기본 500억.
            # 3개월 실측: 500억 이상 구간은 익일 갭 기대값이 유의하게 낮다.
            "max_amount_krw": float(
                tap.get("max_amount_krw")
                or luc.get("max_amount_krw")
                or 50_000_000_000
            ),
            "min_volume_ratio": float(vsp.get("min_ratio", 1.5)),
            "entry_start_time": str(luc.get("entry_start_time") or mtm.get("entry_start_time") or "09:05"),
            "entry_end_time": str(luc.get("entry_end_time") or mtm.get("entry_end_time") or "14:20"),
        }

    def _float_or_none(self, value) -> float | None:
        if value is None or value == "":
            return None
        try:
            return float(value)
        except (TypeError, ValueError):
            return None

    def _try_refresh_reason_features(self, stock_code: str, metrics: dict) -> bool:
        """Use the GO100 common orchestrator for #119 shadow-feature gaps."""
        common_status = orchestrate_data_backfill(
            stock_code,
            date.today(),
            [LIMITUP_REASON_FEATURES_SHADOW],
            context={
                "source": "scalping_entry_engine",
                "caller": "card119_learning_gate",
                "caller_missing": True,
                "trading_path": True,
            },
            enqueue=True,
            attempt_refresh=True,
            fail_policy="fail_open",
            cooldown_seconds=_REASON_FEATURE_BACKFILL_COOLDOWN_SEC,
        )
        metrics["common_backfill_status"] = common_status
        attempt = common_status.get("attempt") or {}
        legacy_attempted = bool(
            common_status.get("attempted")
            or common_status.get("queued")
            or common_status.get("refreshed")
        )
        metrics["limitup119_backfill_attempted"] = legacy_attempted
        metrics["limitup119_backfill_cooldown"] = bool(attempt.get("cooldown"))

        features = (common_status.get("resource_data") or {}).get(
            LIMITUP_REASON_FEATURES_SHADOW
        ) or {}
        refreshed = bool(features)
        if refreshed:
            target_meta = self._universe_meta.get(stock_code)
            if target_meta is not None:
                target_meta.update(features)
        metrics["limitup119_backfill_succeeded"] = refreshed
        metrics["limitup119_backfill_source"] = f"common_backfill:{common_status.get('status')}"
        if attempt.get("pid") is not None:
            metrics["limitup119_backfill_subprocess_pid"] = attempt["pid"]
        if attempt.get("reason") not in {
            None,
            "not_requested",
            "cooldown",
            "bounded_background_worker_started",
        }:
            metrics["limitup119_backfill_subprocess_error"] = str(attempt.get("reason"))[:120]
        logger.info(
            "#119 common_backfill status=%s stock=%s missing=%s queued=%s attempted=%s refreshed=%s recommendation=%s",
            common_status.get("status"),
            stock_code,
            common_status.get("missing"),
            common_status.get("queued"),
            common_status.get("attempted"),
            common_status.get("refreshed"),
            (common_status.get("recommendation") or {}).get("action"),
        )
        return refreshed

    # ─────────────────────────────────────────────────────────────────────────

    def _evaluate_strong_material_gate(
        self, stock_code: str, metrics: dict, card: dict
    ) -> tuple[bool, str, str]:
        """#119 전용: 1,000억 초과 초대형 종목 강한 재료 확인 게이트.

        현재 수집된 피처로 6개 기준 중 2개 이상 충족 시 strong_material_confirmed.
        뉴스/공시/호가/VI는 미수집이면 차단 조건으로 쓰지 않고 criteria_missing에 기록한다.
        6개 기준 중 4개 이상 데이터 미수집이면 material_data_missing(fail-closed).
        """
        meta = self._universe_meta.get(stock_code, {})
        criteria_met = []
        criteria_missing = []
        stock_name = str(meta.get("stock_name") or stock_code)

        # 기준 1: 테마 강도 (theme_strength_intraday 70 이상 또는 소속 테마 2개 이상)
        theme_strength = self._float_or_none(meta.get("theme_strength_intraday"))
        theme_count = int(meta.get("theme_count") or 0)
        if theme_strength is not None or theme_count > 0:
            if (theme_strength is not None and theme_strength >= 70.0) or theme_count >= 2:
                criteria_met.append(
                    f"theme_strong(strength={theme_strength}, count={theme_count})"
                )
        else:
            criteria_missing.append("theme_strength")

        # 기준 2: 테마/섹터 리더 (동반 상한가 2종 이상 또는 동반 평균 등락률 35% 이상)
        theme_peer_lu = self._float_or_none(meta.get("theme_peer_limitup_count"))
        theme_peer_avg = self._float_or_none(meta.get("theme_peer_avg_change_pct"))
        if theme_peer_lu is not None or theme_peer_avg is not None:
            if (theme_peer_lu is not None and theme_peer_lu >= 2.0) or (
                theme_peer_avg is not None and theme_peer_avg >= 35.0
            ):
                criteria_met.append(
                    f"theme_leader(peer_lu={theme_peer_lu}, peer_avg={theme_peer_avg})"
                )
        else:
            criteria_missing.append("theme_peer_data")

        # 기준 3: 시장 레짐 양호 (risk_off/BEAR 아님 또는 regime_score>=50 또는 market_breadth>=0)
        regime_label = str(meta.get("regime_label") or "").upper()
        regime_score = self._float_or_none(meta.get("regime_score"))
        market_breadth = self._float_or_none(meta.get("market_breadth"))
        regime_data_present = (
            regime_label not in ("", "NONE")
            or regime_score is not None
            or market_breadth is not None
        )
        if regime_data_present:
            regime_ok = (
                (regime_label and regime_label not in ("BEAR", "RISK_OFF"))
                or (regime_score is not None and regime_score >= 50.0)
                or (market_breadth is not None and market_breadth >= 0.0)
            )
            if regime_ok:
                criteria_met.append(
                    f"regime_ok(label={regime_label}, score={regime_score}, breadth={market_breadth})"
                )
        else:
            criteria_missing.append("market_regime")

        # 기준 4: 실시간 체결강도/매수우위 120 이상
        strength = float(metrics.get("strength") or 0)
        strength_after_lock = self._float_or_none(meta.get("strength_after_lock"))
        if strength > 0 or strength_after_lock is not None:
            if strength >= 120.0 or (
                strength_after_lock is not None and strength_after_lock >= 120.0
            ):
                criteria_met.append(
                    f"execution_strong(str={strength:.0f}, after_lock={strength_after_lock})"
                )
        else:
            criteria_missing.append("execution_strength")

        # 기준 5: 상한가 잠김 품질 (strength_after_lock>=120 또는 volume_burst_ratio_5m>=10)
        volume_burst = self._float_or_none(meta.get("volume_burst_ratio_5m"))
        if strength_after_lock is not None or volume_burst is not None:
            if (strength_after_lock is not None and strength_after_lock >= 120.0) or (
                volume_burst is not None and volume_burst >= 10.0
            ):
                criteria_met.append(
                    f"lock_quality(after_lock={strength_after_lock}, burst={volume_burst})"
                )
        else:
            criteria_missing.append("lock_quality_data")

        # 기준 6: 뉴스/공시 실제 존재 및 positive (미수집이면 차단하지 않고 missing 기록)
        news_score = self._float_or_none(meta.get("news_score"))
        if news_score is not None:
            if news_score >= 0.5:
                criteria_met.append(f"news_positive(score={news_score:.2f})")
        else:
            criteria_missing.append("news_score(미수집)")

        metrics["strong_material_criteria_met"] = criteria_met
        metrics["strong_material_criteria_missing"] = criteria_missing
        metrics["strong_material_stock_name"] = stock_name

        logger.info(
            "#119 strong_material_gate stock=%s(%s) trade_value=%.0f억 "
            "met=%d/%d missing=%s criteria=%s",
            stock_name,
            stock_code,
            float(metrics.get("trade_value") or 0) / 1e8,
            len(criteria_met),
            6,
            criteria_missing,
            criteria_met,
        )

        # 6개 기준 중 4개 이상 데이터 미수집 → fail-closed
        if len(criteria_missing) >= 4:
            return False, "strong_material_data_missing", (
                f"{stock_name}({stock_code}) 강한 재료 판단 데이터 부족"
                f"({len(criteria_missing)}/6 미수집): {criteria_missing}"
            )

        # 2개 이상 충족 → 통과
        if len(criteria_met) >= 2:
            return True, "strong_material_confirmed", (
                f"{stock_name}({stock_code}) 강한 재료 확인 {len(criteria_met)}개 충족: "
                f"{criteria_met[:3]}"
            )

        return False, "strong_material_not_confirmed", (
            f"{stock_name}({stock_code}) 강한 재료 미확인({len(criteria_met)}/2 필요): "
            f"충족={criteria_met}, 미수집={criteria_missing}"
        )

    def _evaluate_limitup119_learning_gate(self, stock_code: str, metrics: dict, card: dict) -> tuple[bool, str, str]:
        """Apply #119 historical commonality filters using currently collected reason features."""
        if not _LIMITUP119_LEARNING_GATE_ENABLED:
            metrics["limitup119_learning_gate"] = "disabled"
            return True, "learning_gate_disabled", "#119 학습 게이트 비활성"
        if int(card.get("card_id") or 0) != 119:
            return True, "learning_gate_not_target", "#119 외 카드"

        meta = self._universe_meta.get(stock_code, {})
        observed = {
            name: meta.get(name)
            for name in _LIMITUP119_REASON_FEATURE_FIELDS
            if meta.get(name) is not None
        }
        metrics["limitup119_learning_gate"] = "enabled"
        metrics["limitup119_reason_feature_count"] = len(observed)
        metrics["limitup119_reason_features"] = {}
        for key, value in observed.items():
            numeric_value = self._float_or_none(value)
            metrics["limitup119_reason_features"][key] = (
                round(numeric_value, 4) if numeric_value is not None else str(value)
            )
        metrics["limitup119_missing_reason_features"] = [
            name for name in _LIMITUP119_REASON_FEATURE_FIELDS if meta.get(name) is None
        ]

        # 피처가 없으면 즉시 DB 재조회 + 백필 트리거 후 재평가한다.
        if not observed:
            refreshed = self._try_refresh_reason_features(stock_code, metrics)
            if refreshed:
                observed = {
                    name: meta.get(name)
                    for name in _LIMITUP119_REASON_FEATURE_FIELDS
                    if meta.get(name) is not None
                }
            if not observed:
                reason_code = (
                    "learning_gate_no_reason_features_backfill_attempted"
                    if metrics.get("limitup119_backfill_attempted")
                    else "learning_gate_no_reason_features"
                )
                metrics["limitup119_learning_gate_mode"] = "no_reason_features_fail_open"
                return True, reason_code, "#119 이유 피처 미수집 — 기존 상한가 조건만 적용"

        theme_peer_avg = self._float_or_none(meta.get("theme_peer_avg_change_pct"))
        if theme_peer_avg is not None and theme_peer_avg < _LIMITUP119_MIN_THEME_PEER_AVG_CHANGE_PCT:
            return False, "learning_theme_peer_weak", (
                f"테마 동반 강도 {theme_peer_avg:.1f}% < {_LIMITUP119_MIN_THEME_PEER_AVG_CHANGE_PCT:.1f}%"
            )

        volume_burst = self._float_or_none(meta.get("volume_burst_ratio_5m"))
        if volume_burst is not None and volume_burst < _LIMITUP119_MIN_VOLUME_BURST_RATIO_5M:
            return False, "learning_volume_burst_weak", (
                f"5분 거래량 폭발 {volume_burst:.1f}x < {_LIMITUP119_MIN_VOLUME_BURST_RATIO_5M:.1f}x"
            )

        vkospi = self._float_or_none(meta.get("vkospi"))
        if vkospi is not None and vkospi > _LIMITUP119_MAX_VKOSPI:
            return False, "learning_market_volatility_high", (
                f"VKOSPI {vkospi:.1f} > {_LIMITUP119_MAX_VKOSPI:.1f}"
            )

        regime_score = self._float_or_none(meta.get("regime_score"))
        if regime_score is not None and regime_score < _LIMITUP119_MIN_REGIME_SCORE:
            return False, "learning_regime_weak", (
                f"시장 레짐 점수 {regime_score:.1f} < {_LIMITUP119_MIN_REGIME_SCORE:.1f}"
            )

        strength_values = [
            self._float_or_none(meta.get("strength_0900_1000")),
            self._float_or_none(meta.get("strength_1000_1100")),
            self._float_or_none(meta.get("strength_after_lock")),
            self._float_or_none(metrics.get("strength")),
        ]
        strength_values = [value for value in strength_values if value is not None]
        if strength_values and max(strength_values) < _LIMITUP119_MIN_TIME_BUCKET_STRENGTH:
            return False, "learning_execution_strength_weak", (
                f"시간대 체결강도 최대 {max(strength_values):.1f} < {_LIMITUP119_MIN_TIME_BUCKET_STRENGTH:.1f}"
            )

        return True, "learning_gate_pass", "#119 과거학습 공통조건 통과"

    def _evaluate_limit_up_entry_with_audit(self, stock_code: str, tick: tuple, card: dict) -> tuple[Optional[str], str, str, dict]:
        try:
            from zoneinfo import ZoneInfo
            now_t = datetime.now(ZoneInfo("Asia/Seoul")).time()
        except Exception:
            now_t = datetime.now().time()

        ep = self._parse_limit_up_entry_params(card)
        metrics = self._tick_metrics(stock_code, tick)
        price = metrics["price"]
        prev_close = metrics["prev_close"]
        intraday_pct = metrics["intraday_pct"]
        session_high = metrics["session_high"]
        trade_value = metrics["trade_value"]
        volume_ratio = metrics["volume_ratio"]
        strength = metrics["strength"]
        volume = metrics["tick_volume"]
        _es = ep.get("entry_start_time", "09:05")
        _ee = ep.get("entry_end_time", "14:20")
        _es_t = dt_time(int(_es.split(":")[0]), int(_es.split(":")[1]), 0)
        _ee_t = dt_time(int(_ee.split(":")[0]), int(_ee.split(":")[1]), 0)
        metrics["entry_window"] = f"{_es}-{_ee}"
        metrics["limit_up_params"] = ep
        if int(card.get("card_id") or 0) == 119:
            metrics["card119_discovery_mode"] = "independent_preopen_or_intraday_entry_gate"
            metrics["card119_discovery_min_change_pct"] = _CARD119_DISCOVERY_MIN_CHANGE_PCT
            metrics["card119_discovery_min_trade_value_krw"] = _LIMITUP119_RELAXED_MIN_TRADE_VALUE
            metrics["card119_independent_universe_size"] = len(self._card119_discovery_universe)
            if stock_code not in self._card119_discovery_universe:
                return (
                    None,
                    "card119_not_in_independent_discovery",
                    f"#119 독립 발굴 후보(+{_CARD119_DISCOVERY_MIN_CHANGE_PCT:.0f}% 장전 예상/당일 실제 등락률, 장중 거래대금 1억원 이상)에 포함되지 않아 매매선정에서 제외",
                    metrics,
                )

        if now_t < _es_t or now_t > _ee_t:
            return None, "outside_entry_window", f"진입 시간창({_es}~{_ee}) 밖", metrics
        if price <= 0 or prev_close <= 0:
            return None, "missing_price_baseline", "현재가 또는 전일 기준가 부족", metrics
        if intraday_pct is None or intraday_pct < ep["entry_min_intraday_pct"] or intraday_pct > ep["max_entry_pct"]:
            return None, "intraday_change_out_of_range", f"진입 등락률 범위 미충족(요구 {ep['entry_min_intraday_pct']:.0f}~{ep['max_entry_pct']:.0f}%)", metrics
        if now_t >= dt_time(11, 0, 0) and intraday_pct < ep["after_11_min_pct"]:
            return None, "late_change_threshold_failed", "11시 이후 최소 상승률 미충족", metrics
        if now_t >= dt_time(14, 0, 0) and intraday_pct < ep["after_14_min_pct"]:
            return None, "closing_change_threshold_failed", "14시 이후 상한가 접근 강도 미충족", metrics
        if session_high > 0 and price < session_high * ep["min_price_position"]:
            return None, "price_not_near_session_high", "세션 고가 대비 이탈", metrics
        if now_t >= dt_time(14, 0, 0) and session_high > 0 and price < session_high * ep["final_price_position"]:
            return None, "closing_high_hold_failed", "장후반 고가권 유지 실패", metrics
        # [GO100-119-MATERIAL-AWARE-VALUE-GATE P0] 거래대금 게이트
        # card119: 1억 미만 명시 차단, 1억 이상 완화 통과, 1,000억 초과는 material gate로 대체(l0 상한 생략).
        # 非card119: 기존 min_amount_krw / l0_max_trade_value 게이트 그대로 유지.
        _is_card119 = (int(card.get("card_id") or 0) == 119)
        if _is_card119:
            if trade_value < _LIMITUP119_RELAXED_MIN_TRADE_VALUE:
                metrics["limitup119_trade_value_gate"] = (
                    f"blocked_below_1억({trade_value / 1e8:.2f}억)"
                )
                return None, "limitup119_trade_value_below_min", (
                    f"#119 거래대금 {trade_value / 1e8:.1f}억 < 1억 차단"
                ), metrics
            metrics["limitup119_trade_value_gate"] = (
                f"relaxed_pass({trade_value / 1e8:.1f}억>=1억)"
            )
            if trade_value > _LIMITUP119_STRONG_MATERIAL_THRESHOLD:
                # 1,000억 초과: l0 과밀 필터 생략, material gate(fail-closed) 적용
                metrics["limitup119_material_gate_thresholds"] = {
                    "relaxed_min_won": _LIMITUP119_RELAXED_MIN_TRADE_VALUE,
                    "strong_material_won": _LIMITUP119_STRONG_MATERIAL_THRESHOLD,
                }
                sm_ok, sm_code, sm_text = self._evaluate_strong_material_gate(
                    stock_code, metrics, card
                )
                metrics["strong_material_confirmed"] = sm_ok
                metrics["strong_material_code"] = sm_code
                metrics["strong_material_text"] = sm_text
                if not sm_ok:
                    return None, sm_code, sm_text, metrics
            else:
                # 1억~1,000억: l0 과밀 필터 유지
                _l0_max_trade_value = float(ep.get("max_amount_krw") or 50_000_000_000)
                if trade_value > _l0_max_trade_value:
                    return None, "l0_high_trade_value", (
                        f"거래대금 {trade_value/100_000_000:.0f}억 > {_l0_max_trade_value/100_000_000:.0f}억 과밀 필터"
                    ), metrics
        else:
            if trade_value < ep["min_amount_krw"]:
                return None, "liquidity_threshold_failed", "거래대금 최소 기준 미충족", metrics
            _l0_max_trade_value = float(ep.get("max_amount_krw") or 50_000_000_000)
            if trade_value > _l0_max_trade_value:
                return None, "l0_high_trade_value", (
                    f"거래대금 {trade_value/100_000_000:.0f}억 > {_l0_max_trade_value/100_000_000:.0f}억 과밀 필터"
                ), metrics
        if volume_ratio < ep["min_volume_ratio"]:
            return None, "volume_ratio_threshold_failed", "거래량 배수 최소 기준 미충족", metrics

        learning_ok, learning_code, learning_text = self._evaluate_limitup119_learning_gate(stock_code, metrics, card)
        metrics["limitup119_learning_reason_code"] = learning_code
        metrics["limitup119_learning_reason_text"] = learning_text
        if not learning_ok:
            return None, learning_code, learning_text, metrics

        history = list(self._tick_history[stock_code])
        recent_prices = [float(t[2] or 0) for t in history[-3:]]
        rising_ticks = 0
        if len(recent_prices) >= 3:
            rising_ticks = sum(1 for i in range(len(recent_prices) - 1) if recent_prices[i] < recent_prices[i + 1])
        metrics["rising_ticks_3"] = rising_ticks
        if strength > 0 and strength < 110 and rising_ticks < 1:
            return None, "momentum_strength_failed", "체결강도/상승틱 모멘텀 부족", metrics

        reason = (
            "LIMIT_UP_CLOSE_ENTRY("
            f"chg={intraday_pct:.2f}%,value={trade_value/100000000:.1f}억,"
            f"vol_x={volume_ratio:.1f},str={strength:.0f},tick_vol={volume:.0f})"
        )
        return reason, "entry_signal", "상한가 접근 진입 조건 충족", metrics

    def _evaluate_overnight_entry_with_audit(self, stock_code: str, tick: tuple, card: dict) -> tuple[Optional[str], str, str, dict]:
        """오버나이트/종가매매 카드는 스캘핑 필터가 아니라 카드 entry_rules를 틱 메트릭으로 평가한다."""
        _tw_s, _tw_e = _parse_card_time_window(card.get("entry_rules", []))
        try:
            from zoneinfo import ZoneInfo
            now_t = datetime.now(ZoneInfo("Asia/Seoul")).time()
        except Exception:
            now_t = datetime.now().time()
        try:
            tw_st = dt_time(int(_tw_s.split(":")[0]), int(_tw_s.split(":")[1]), 0)
            tw_et = dt_time(int(_tw_e.split(":")[0]), int(_tw_e.split(":")[1]), 0)
        except Exception:
            tw_st, tw_et = dt_time(14, 50), dt_time(15, 20)
        metrics = self._tick_metrics(stock_code, tick)
        metrics["entry_window"] = f"{_tw_s}-{_tw_e}"

        _in_nxt_session = (NXT_PRE_OPEN <= now_t <= NXT_PRE_CLOSE) or (NXT_AFTER_OPEN <= now_t <= NXT_AFTER_CLOSE)
        if _in_nxt_session:
            nxt_windows = _parse_card_nxt_time_windows(card.get("entry_rules", []))
            if nxt_windows:
                if not any(ws <= now_t <= we for ws, we in nxt_windows):
                    _nxt_win_str = ", ".join(
                        f"{ws.strftime('%H:%M')}~{we.strftime('%H:%M')}" for ws, we in nxt_windows
                    )
                    return None, "outside_nxt_window", f"카드 NXT 진입 시간창({_nxt_win_str}) 밖", {
                        **metrics, "nxt_windows": _nxt_win_str,
                    }
            # NXT 세션 허용 — 정규장 time_window 체크 우회
        elif now_t < tw_st or now_t > tw_et:
            return None, "outside_card_window", f"카드 진입 시간창({_tw_s}~{_tw_e}) 밖", metrics

        price = float(metrics.get("price") or 0)
        prev_close = float(metrics.get("prev_close") or 0)
        cum_volume = float(metrics.get("cum_volume") or 0)
        trade_value = float(metrics.get("trade_value") or 0)
        volume_ratio = float(metrics.get("volume_ratio") or 0)
        intraday_pct = metrics.get("intraday_pct")
        session_high = float(metrics.get("session_high") or price)
        close_position = price / session_high if session_high > 0 else 0.0
        metrics["close_position"] = round(close_position, 4)

        if price <= 0 or prev_close <= 0:
            return None, "missing_price_baseline", "현재가 또는 전일 기준가 부족", metrics

        # P1-7: ETF/ETN/스팩 종목 종가매매 제외
        _stock_name = str(self._universe_meta.get(stock_code, {}).get("stock_name") or "")
        if any(kw in _stock_name.upper() for kw in ("ETF", "ETN", "스팩", "리츠", "KODEX", "TIGER", "KBSTAR")):
            return None, "overnight_etf_excluded", f"ETF/ETN/스팩 종목 종가매매 제외({_stock_name})", metrics

        # P1-8: 일봉 변동률 범위 체크 (daily_change_pct 1~6%)
        sp = _json_dict(card.get("strategy_params"))
        _change_min = float(sp.get("daily_change_pct_min") or 0)
        _change_max = float(sp.get("daily_change_pct_max") or 0)
        if intraday_pct is not None and (_change_min > 0 or _change_max > 0):
            _ipct = float(intraday_pct)
            if _change_min > 0 and _ipct < _change_min:
                return None, "daily_change_below_min", f"전일비 변동률 {_ipct:.2f}% < 최소 {_change_min}%", metrics
            if _change_max > 0 and _ipct > _change_max:
                return None, "daily_change_above_max", f"전일비 변동률 {_ipct:.2f}% > 최대 {_change_max}%", metrics

        for rule in card.get("entry_rules", []):
            if not isinstance(rule, dict):
                continue
            rtype = str(rule.get("type") or "").strip().lower()
            if rtype == "time_window":
                continue
            if rtype == "trade_value_surge":
                min_value = float(rule.get("min_trade_value") or 0)
                ratio = float(rule.get("ratio") or 0)
                avg_trade_value = float(self._universe_meta.get(stock_code, {}).get("avg_trade_value_20d") or 0)
                metrics["required_min_trade_value"] = min_value
                metrics["avg_trade_value_20d"] = avg_trade_value
                if min_value > 0 and trade_value < min_value:
                    return None, "trade_value_min_failed", "거래대금 최소 기준 미충족", metrics
                if ratio > 0 and avg_trade_value > 0 and trade_value < avg_trade_value * ratio:
                    return None, "trade_value_surge_failed", "20일 평균 대비 거래대금 증가 기준 미충족", metrics
            elif rtype == "volume_surge":
                ratio = float(rule.get("ratio") or 0)
                metrics["required_volume_ratio"] = ratio
                if ratio > 0 and volume_ratio > 0 and volume_ratio < ratio:
                    return None, "volume_surge_failed", "20일 평균 대비 누적 거래량 증가 기준 미충족", metrics
                if ratio > 0 and volume_ratio <= 0 and cum_volume <= 0:
                    return None, "volume_data_missing", "누적 거래량 기준값 부족", metrics
            elif rtype == "price_position":
                min_pos = float(rule.get("high_ratio_min") or rule.get("min_price_position") or 0)
                metrics["required_close_position"] = min_pos
                if min_pos > 0 and close_position < min_pos:
                    return None, "price_position_failed", "장중 고가권 위치 기준 미충족", metrics
            elif rtype == "candle_pattern":
                body_min = float(rule.get("body_min_pct") or 0)
                metrics["required_body_min_pct"] = body_min
                if body_min > 0 and (intraday_pct is None or float(intraday_pct) < body_min):
                    return None, "candle_strength_failed", "양봉/상승폭 기준 미충족", metrics
                _br_min = float(rule.get("body_ratio_min") or 0)
                if _br_min > 0 and price > prev_close and session_high > prev_close:
                    _approx_ratio = (price - prev_close) / (session_high - prev_close)
                    metrics["approx_body_ratio"] = round(_approx_ratio, 4)
                    if _approx_ratio < _br_min:
                        return None, "body_ratio_weak", f"양봉비율 {_approx_ratio:.2f}<{_br_min}", metrics
            elif rtype in {"price_above_ma", "ma_filter"}:
                # 현재 틱 실행기에는 MA 캐시가 없으므로 전일 대비 양수 여부로 보수 근사한다.
                if intraday_pct is None or float(intraday_pct) <= 0:
                    return None, "price_above_ma_approx_failed", "이평선 위 조건 근사 평가 실패(전일 대비 상승 아님)", metrics
            elif rtype == "consecutive_limit_up_exclude":
                if stock_code in self._overheated_stocks:
                    return None, "overheated_limit_up_excluded", "연속 상한가 과열 제외", metrics
            elif rtype == "shooting_star_exclude":
                _body = price - prev_close
                _upper_shadow = session_high - price
                _max_ratio = float(rule.get("max_shadow_ratio") or 2.0)
                if _body > 0 and _upper_shadow > _body * _max_ratio:
                    metrics["upper_shadow_body_ratio"] = round(_upper_shadow / _body, 2)
                    return None, "shooting_star_excluded", f"슈팅스타(위꼬리/몸통={_upper_shadow/_body:.1f})", metrics
                _full_range = session_high - prev_close if session_high > prev_close else 0
                _doji_thr = float(rule.get("doji_threshold") or 0.10)
                if _full_range > 0 and abs(_body) / _full_range < _doji_thr:
                    return None, "doji_excluded", f"도지(몸통비율={abs(_body)/_full_range:.2f}<{_doji_thr})", metrics

        reason = (
            "OVERNIGHT_CLOSE_ENTRY("
            f"chg={float(intraday_pct or 0):.2f}%,value={trade_value/100000000:.1f}억,"
            f"vol_x={volume_ratio:.2f},pos={close_position:.2f})"
        )
        return reason, "entry_signal", "종가매매 카드 진입 조건 충족", metrics

    # ── 1분봉 MA 눌림목 ──────────────────────────────────────────────

    def _update_minute_bars(self, stock_code: str, tick: tuple) -> None:
        price = float(tick[2] or 0)
        tick_volume = abs(float(tick[3] or 0)) if len(tick) > 3 else 0.0
        if price <= 0:
            return
        tick_time = tick[1]
        if isinstance(tick_time, str) and len(tick_time) >= 4:
            minute_key = tick_time[:4]
        elif hasattr(tick_time, 'strftime'):
            minute_key = tick_time.strftime("%H%M")
        else:
            minute_key = str(int(_time.time()) // 60)
        cur = self._minute_bar_current.get(stock_code)
        if cur and cur["minute"] == minute_key:
            cur["h"] = max(cur["h"], price)
            cur["l"] = min(cur["l"], price)
            cur["c"] = price
            cur["v"] = float(cur.get("v") or 0) + tick_volume
        else:
            if cur:
                self._minute_bars[stock_code].append(cur["c"])
                self._minute_ohlc_bars[stock_code].append(dict(cur))
            self._minute_bar_current[stock_code] = {
                "minute": minute_key, "o": price, "h": price, "l": price, "c": price,
                "v": tick_volume,
            }

    def ingest_db_minute_bars(self, bars: list[dict]) -> int:
        """Merge shard-collected 1m OHLCV bars into the live wave buffers."""
        if not bars:
            return 0

        grouped: dict[str, dict[str, dict]] = {}
        for raw in bars:
            if not isinstance(raw, dict):
                continue
            stock_code = str(raw.get("stock_code") or "").strip()
            if not stock_code:
                continue
            minute_dt = raw.get("minute_dt") or raw.get("datetime") or raw.get("time")
            if hasattr(minute_dt, "strftime"):
                minute_key = minute_dt.strftime("%H%M")
            else:
                text = str(minute_dt or raw.get("minute") or "").strip()
                minute_key = text[:4] if text else ""
            if not minute_key:
                continue
            try:
                o = float(raw.get("open", raw.get("o")) or 0)
                h = float(raw.get("high", raw.get("h")) or 0)
                l = float(raw.get("low", raw.get("l")) or 0)
                c = float(raw.get("close", raw.get("c")) or 0)
                v = float(raw.get("volume", raw.get("v")) or 0)
            except (TypeError, ValueError):
                continue
            if c <= 0:
                continue
            grouped.setdefault(stock_code, {})[minute_key] = {
                "minute": minute_key,
                "o": o or c,
                "h": max(h, o, c),
                "l": min(value for value in (l, o, c) if value > 0),
                "c": c,
                "v": max(0.0, v),
            }

        ingested = 0
        for stock_code, by_minute in grouped.items():
            existing_by_minute = {
                str(bar.get("minute") or ""): dict(bar)
                for bar in self._minute_ohlc_bars.get(stock_code, [])
                if str(bar.get("minute") or "")
            }
            existing_by_minute.update(by_minute)
            merged_bars = [existing_by_minute[key] for key in sorted(existing_by_minute)]
            self._minute_ohlc_bars[stock_code] = deque(
                merged_bars[-_SESSION_WAVE_BUFFER_BARS:],
                maxlen=_SESSION_WAVE_BUFFER_BARS,
            )
            self._minute_bars[stock_code] = deque(
                [bar["c"] for bar in merged_bars[-30:]],
                maxlen=30,
            )
            ingested += len(by_minute)

        if ingested:
            self._external_minute_bars_ingested += ingested
            now_ts = time_module.monotonic()
            if now_ts - self._last_external_minute_bar_log >= 60:
                self._last_external_minute_bar_log = now_ts
                logger.info(
                    "ScalpingEntry: external DB minute bars ingested=%d total=%d symbols=%d",
                    ingested,
                    self._external_minute_bars_ingested,
                    len(grouped),
                )
        return ingested

    async def consume_external_minute_bars(self, minute_bar_queue: asyncio.Queue) -> None:
        """Consume DbMinuteBarFeeder batches and keep wave buffers current."""
        logger.info("ScalpingEntry: external DB minute bar consumer started")
        while True:
            try:
                bars = await minute_bar_queue.get()
                self.ingest_db_minute_bars(bars)
            except Exception as exc:
                logger.error("ScalpingEntry: external DB minute bar consumer error: %s", exc)
                await asyncio.sleep(0.5)

    def _get_minute_ohlc_series(self, stock_code: str) -> list[dict]:
        bars_by_minute: dict[str, dict] = {}
        for raw_bar in self._minute_ohlc_bars.get(stock_code, []):
            bar = dict(raw_bar)
            minute = str(bar.get("minute") or "")
            if minute:
                bars_by_minute[minute] = bar
        cur = self._minute_bar_current.get(stock_code)
        if cur:
            current = dict(cur)
            minute = str(current.get("minute") or "")
            existing = bars_by_minute.get(minute)
            if existing is None:
                bars_by_minute[minute] = current
            else:
                # DB/shard bar is authoritative for open/accumulated volume;
                # the latest feeder tick may extend its current-minute range.
                existing["h"] = max(float(existing.get("h") or 0), float(current.get("h") or 0))
                positive_lows = [
                    float(value)
                    for value in (existing.get("l"), current.get("l"))
                    if float(value or 0) > 0
                ]
                if positive_lows:
                    existing["l"] = min(positive_lows)
                existing["c"] = float(current.get("c") or existing.get("c") or 0)
                existing["v"] = max(float(existing.get("v") or 0), float(current.get("v") or 0))
        return [bars_by_minute[key] for key in sorted(bars_by_minute)]

    def _hydrate_minute_ohlc_from_db(
        self,
        stock_code: str,
        min_bars: int = 12,
        limit: int = 60,
        *,
        force_refresh: bool = False,
    ) -> int:
        """Load today's 1m OHLCV from DB into the in-process wave buffers."""
        now_ts = time_module.monotonic()
        cached_ts, cached = self._minute_ohlc_db_cache.get(stock_code, (0.0, []))
        if not force_refresh and cached and now_ts - cached_ts < _WAVE_DATA_DB_CACHE_SEC:
            return len(cached)
        conn = None
        bars: list[dict] = []
        try:
            conn = psycopg2.connect(**_get_db_params())
            with conn.cursor() as cur:
                cur.execute(
                    """
                    WITH src AS (
                        SELECT minute_dt,
                               open::numeric AS o, high::numeric AS h,
                               low::numeric AS l, close::numeric AS c,
                               COALESCE(volume, 0)::numeric AS v
                        FROM go100_kiwoom_minute_ohlcv
                        WHERE stock_code = %s AND minute_dt::date = CURRENT_DATE
                        UNION ALL
                        SELECT (trade_date::timestamp + trade_time::interval) AS minute_dt,
                               open_price::numeric AS o, high_price::numeric AS h,
                               low_price::numeric AS l, close_price::numeric AS c,
                               COALESCE(volume, 0)::numeric AS v
                        FROM v4_ohlcv_minute
                        WHERE stock_code = %s AND trade_date = CURRENT_DATE
                    ), ranked AS (
                        SELECT DISTINCT ON (minute_dt) minute_dt, o, h, l, c, v
                        FROM src
                        WHERE c > 0
                        ORDER BY minute_dt DESC
                        LIMIT %s
                    )
                    SELECT minute_dt, o, h, l, c, v
                    FROM ranked
                    ORDER BY minute_dt ASC
                    """,
                    (stock_code, stock_code, max(limit, min_bars, _SESSION_WAVE_BUFFER_BARS)),
                )
                for minute_dt, o, h, l, c, v in cur.fetchall():
                    bars.append({
                        "minute": minute_dt.strftime("%H%M") if hasattr(minute_dt, "strftime") else str(minute_dt),
                        "o": float(o), "h": float(h), "l": float(l), "c": float(c), "v": float(v or 0),
                    })
        except Exception as exc:
            logger.debug("1m wave DB hydrate failed %s: %s", stock_code, exc)
            bars = []
        finally:
            if conn:
                try:
                    conn.close()
                except Exception:
                    pass
        self._minute_ohlc_db_cache[stock_code] = (now_ts, bars)
        if bars:
            existing_by_minute = {
                str(bar.get("minute") or ""): dict(bar)
                for bar in self._minute_ohlc_bars.get(stock_code, [])
                if str(bar.get("minute") or "")
            }
            # Persisted collector data wins for the same minute.  In-memory
            # feeder bars are retained only while the shard row has not arrived.
            for bar in bars:
                existing_by_minute[str(bar.get("minute") or "")] = dict(bar)
            merged_bars = [existing_by_minute[key] for key in sorted(existing_by_minute)]
            self._minute_ohlc_bars[stock_code] = deque(
                merged_bars[-_SESSION_WAVE_BUFFER_BARS:],
                maxlen=_SESSION_WAVE_BUFFER_BARS,
            )
            self._minute_bars[stock_code] = deque([b["c"] for b in merged_bars[-30:]], maxlen=30)
        return len(bars)

    def _trigger_wave_data_recovery(self, stock_code: str, card: dict, status: str, metrics: dict) -> None:
        if not _WAVE_DATA_RECOVERY_ENABLED:
            return
        now_ts = time_module.monotonic()
        cd_ts, cd_status = self._wave_recovery_cooldown.get(stock_code, (0.0, ""))
        if now_ts - cd_ts < _WAVE_DATA_RECOVERY_COOLDOWN_SEC:
            metrics["wave_recovery_status"] = "cooldown"
            metrics["wave_recovery_cooldown_status"] = cd_status
            return
        self._wave_recovery_cooldown[stock_code] = (now_ts, status)
        backfilled = 0
        try:
            from backend.app.services.go100.data.realtime_data_gap_filler import DataGapFiller
            filler = DataGapFiller()
            try:
                backfilled = int(filler.backfill_missing_bars(stock_code) or 0)
            finally:
                filler.close()
        except Exception as exc:
            metrics["wave_recovery_backfill_error"] = str(exc)[:160]
        hydrated = self._hydrate_minute_ohlc_from_db(stock_code, min_bars=4, limit=60, force_refresh=True)
        recovery_result = "recovered" if hydrated >= 4 else "still_insufficient"
        metrics.update({
            "wave_recovery_status": "attempted",
            "wave_recovery_reason": status,
            "wave_recovery_result": recovery_result,
            "wave_recovery_backfilled_bars": backfilled,
            "wave_recovery_hydrated_bars": hydrated,
            "wave_reentry_policy": "blocked_this_tick_retry_next_tick_after_recovery",
        })
        self._audit_decision(
            card=card,
            stock_code=stock_code,
            stage="wave_data_recovery",
            decision="skip",
            reason_code="wave_data_recovery_triggered",
            reason_text=f"1분봉 파동 데이터 미충족({status}) — 진입 차단 후 즉시 백필/재수화 시도",
            metrics=metrics,
            throttle_seconds=30,
        )

    def _reset_symbol_wave_state(self, stock_code: str, metrics: dict, reason: str) -> None:
        """Discard only this symbol's in-memory wave inputs after an invalidating event."""
        for attribute in (
            "_minute_bars",
            "_minute_ohlc_bars",
            "_minute_bar_current",
            "_minute_bar_db_cache",
            "_minute_ohlc_db_cache",
            "_wave_recovery_cooldown",
        ):
            getattr(self, attribute, {}).pop(stock_code, None)
        metrics["wave_state_reset"] = True
        metrics["wave_state_reset_reason"] = reason

    def _check_wave_market_time_gate(self, metrics: dict) -> bool:
        if not _wave_feature_enabled("GO100_WAVE_EVENT_GATE") or _WAVE_EVENT_HANDLER is None:
            return True
        try:
            from zoneinfo import ZoneInfo

            now = datetime.now(ZoneInfo("Asia/Seoul"))
            response = _WAVE_EVENT_HANDLER.check_market_time(
                hour=now.hour,
                minute=now.minute,
                has_position=False,
            )
            metrics["wave_market_time_event"] = response.to_dict()
            if response.action == "BLOCK_ENTRY":
                metrics["ma_wave_status"] = "wave_event_market_time_blocked"
                return False
        except Exception as exc:
            metrics["wave_event_gate_error"] = str(exc)[:160]
            logger.warning("GO100 wave market-time gate error; preserving existing decision: %s", exc)
        return True

    def _check_wave_volume_event_gate(
        self,
        stock_code: str,
        bars: list[dict],
        metrics: dict,
    ) -> bool:
        if not _wave_feature_enabled("GO100_WAVE_EVENT_GATE") or _WAVE_EVENT_HANDLER is None:
            return True
        try:
            if len(bars) < 2:
                return True
            prior_volumes = [
                float(bar.get("v", bar.get("volume", 0)) or 0)
                for bar in bars[-21:-1]
            ]
            prior_volumes = [volume for volume in prior_volumes if volume > 0]
            current_volume = float(bars[-1].get("v", bars[-1].get("volume", 0)) or 0)
            if current_volume <= 0 or not prior_volumes:
                return True
            average_volume = sum(prior_volumes) / len(prior_volumes)
            if average_volume <= 0:
                return True
            volume_ratio = current_volume / average_volume
            last_open = float(bars[-1].get("o", bars[-1].get("open", 0)) or 0)
            last_close = float(bars[-1].get("c", bars[-1].get("close", 0)) or 0)
            response = _WAVE_EVENT_HANDLER.handle_volume_spike(
                stock_code=stock_code,
                volume_ratio=volume_ratio,
                is_bearish=last_open > 0 and last_close < last_open,
            )
            metrics["wave_volume_ratio"] = round(volume_ratio, 4)
            metrics["wave_volume_event"] = response.to_dict()
            invalidates_wave = (
                response.wave_impact == "INVALIDATE"
                or response.action == "TIGHTEN_STOP"
            )
            if invalidates_wave:
                self._reset_symbol_wave_state(
                    stock_code,
                    metrics,
                    f"{response.event_type}:{response.action}:{response.wave_impact}",
                )
                metrics["ma_wave_status"] = "wave_event_volume_blocked"
                return False
        except Exception as exc:
            metrics["wave_volume_event_error"] = str(exc)[:160]
            logger.warning(
                "GO100 wave volume-event gate error for %s; preserving existing decision: %s",
                stock_code,
                exc,
            )
        return True

    def _apply_wave_slippage_gate(
        self,
        stock_code: str,
        price: float,
        rule: dict,
        wave_result,
        metrics: dict,
    ) -> bool:
        if not _wave_feature_enabled("GO100_WAVE_SLIPPAGE_ADJUST") or _WAVE_SLIPPAGE_MODEL is None:
            return True
        try:
            configured_return = rule.get("ma_wave_expected_return_pct")
            if configured_return is None:
                configured_return = rule.get("expected_return_pct")
            expected_return_pct = float(configured_return or 0)
            if expected_return_pct <= 0 and price > 0:
                resistance = float(getattr(wave_result, "resistance_level", 0) or 0)
                if resistance > price:
                    expected_return_pct = (resistance - price) / price * 100.0
            if expected_return_pct <= 0:
                expected_return_pct = float(getattr(_MA_WAVE_ENGINE, "TAKE_PROFIT_PCT", 0) or 0)

            min_expected_return_pct = float(
                rule.get(
                    "ma_wave_min_expected_return_pct",
                    rule.get("min_expected_return_pct", 0.0),
                )
                or 0.0
            )
            universe_meta = getattr(self, "_universe_meta", {}).get(stock_code, {})
            estimate = _WAVE_SLIPPAGE_MODEL.estimate(
                market_cap_krw=float(universe_meta.get("market_cap") or 0),
                daily_volume_krw=float(universe_meta.get("avg_trade_value_20d") or 0),
                expected_return_pct=expected_return_pct,
            )
            metrics.update({
                "wave_expected_return_pct": round(expected_return_pct, 4),
                "wave_min_expected_return_pct": min_expected_return_pct,
                "wave_slippage": estimate.to_dict(),
                "wave_adjusted_return_pct": estimate.adjusted_return_pct,
            })
            if estimate.adjusted_return_pct < min_expected_return_pct:
                metrics["ma_wave_status"] = "slippage_adjusted_return_too_low"
                return False
        except Exception as exc:
            metrics["wave_slippage_error"] = str(exc)[:160]
            logger.warning(
                "GO100 wave slippage gate error for %s; preserving existing decision: %s",
                stock_code,
                exc,
            )
        return True

    def _apply_wave_failure_gate(
        self,
        stock_code: str,
        bars: list[dict],
        wave_count,
        metrics: dict,
    ) -> bool:
        if not _wave_feature_enabled("GO100_WAVE_FAILURE_GATE") or _WAVE_FAILURE_DETECTOR is None:
            return True
        if stock_code in getattr(self, "_wave_failure_blacklist", set()):
            metrics["ma_wave_status"] = "wave_failure_daily_blacklist"
            return False
        if wave_count is None:
            return True
        try:
            def _failure_points(points: list) -> list[dict]:
                return [
                    {
                        "index": int(point.get("index", point.get("idx", 0)) or 0),
                        "price": float(point.get("price") or 0),
                    }
                    for point in (points or [])
                ]

            failure_peaks = _failure_points(getattr(wave_count, "wave_peaks", []))
            failure_troughs = _failure_points(getattr(wave_count, "wave_troughs", []))
            # WaveFailureDetector의 첫 trough는 W1 시작점(0선)인 반면
            # WaveCounter는 완료된 W2/W4 저점만 반환하므로 실제 W1 고점 이전
            # 최저점을 원점으로 보강한다.
            if failure_peaks:
                first_peak_index = failure_peaks[0]["index"]
                has_origin = bool(
                    failure_troughs
                    and failure_troughs[0]["index"] < first_peak_index
                )
                if not has_origin and first_peak_index >= 0:
                    origin_window = bars[:min(first_peak_index + 1, len(bars))]
                    if origin_window:
                        origin_index = min(
                            range(len(origin_window)),
                            key=lambda index: float(
                                origin_window[index].get(
                                    "l",
                                    origin_window[index].get("low", origin_window[index].get("c", 0)),
                                )
                                or 0
                            ),
                        )
                        origin_price = float(
                            origin_window[origin_index].get(
                                "l",
                                origin_window[origin_index].get("low", origin_window[origin_index].get("c", 0)),
                            )
                            or 0
                        )
                        failure_troughs.insert(
                            0,
                            {"index": origin_index, "price": origin_price},
                        )

            failure = _WAVE_FAILURE_DETECTOR.detect(
                bars,
                failure_peaks,
                failure_troughs,
                int(getattr(wave_count, "wave_number", 0) or 0),
            )
            metrics["wave_failure"] = failure.to_dict()
            if failure.severity == "CRITICAL":
                if not hasattr(self, "_wave_failure_blacklist"):
                    self._wave_failure_blacklist = set()
                self._wave_failure_blacklist.add(stock_code)
                metrics["wave_failure_daily_blacklisted"] = True
                metrics["ma_wave_status"] = "wave_failure_critical"
                return False
            if failure.should_exit:
                metrics["ma_wave_status"] = "wave_failure_exit_blocked"
                return False
        except Exception as exc:
            metrics["wave_failure_error"] = str(exc)[:160]
            logger.warning(
                "GO100 wave failure gate error for %s; preserving existing decision: %s",
                stock_code,
                exc,
            )
        return True

    def _evaluate_ma_wave_entry(self, stock_code: str, price: float, rule: dict) -> tuple[bool, dict]:
        """이평선 파동분석 기반 진입 조건 평가"""
        metrics = {"ma_wave_status": "not_evaluated"}

        if not self._check_wave_market_time_gate(metrics):
            return False, metrics

        if (
            _wave_feature_enabled("GO100_WAVE_FAILURE_GATE")
            and stock_code in getattr(self, "_wave_failure_blacklist", set())
        ):
            metrics["ma_wave_status"] = "wave_failure_daily_blacklist"
            return False, metrics

        if _MA_WAVE_ENGINE is None:
            metrics["ma_wave_status"] = "engine_not_available"
            return False, metrics

        min_bars = int(rule.get("ma_wave_min_bars", 60))
        min_confidence = float(rule.get("ma_wave_min_confidence", 0.3))
        hydrated = 0
        if _WAVE_BAR_SOURCE_MODE != "ws_memory_legacy":
            hydrated = self._hydrate_minute_ohlc_from_db(
                stock_code,
                min_bars=min_bars,
                limit=max(_SESSION_WAVE_BUFFER_BARS, min_bars),
            )
        bars = self._get_minute_ohlc_series(stock_code)
        metrics["ma_wave_data_source_mode"] = _WAVE_BAR_SOURCE_MODE
        metrics["ma_wave_db_hydrated_bars"] = hydrated

        if _WAVE_BAR_SOURCE_MODE == "db_shard_only" and hydrated < min_bars:
            metrics["ma_wave_status"] = "ma_wave_db_shard_insufficient"
            metrics["ma_wave_bar_count"] = len(bars)
            return False, metrics

        if len(bars) < min_bars:
            hydrated = self._hydrate_minute_ohlc_from_db(stock_code, min_bars=min_bars, limit=max(_SESSION_WAVE_BUFFER_BARS, min_bars))
            bars = self._get_minute_ohlc_series(stock_code)
            metrics["ma_wave_db_hydrated_bars"] = hydrated

        if len(bars) < min_bars:
            metrics["ma_wave_status"] = "ma_wave_warmup_blocked"
            metrics["ma_wave_bar_count"] = len(bars)
            return False, metrics

        if not self._check_wave_volume_event_gate(stock_code, bars, metrics):
            return False, metrics

        # bars의 키를 MAWaveEngine 형식으로 변환
        # _minute_ohlc_bars는 {o, h, l, c, v} 키 사용
        result = _MA_WAVE_ENGINE.analyze(bars)

        metrics.update({
            "ma_wave_status": result.wave_phase,
            "ma_wave_arrangement": result.ma_arrangement,
            "ma_wave_disparity_pct": result.disparity_pct,
            "ma_wave_confidence": result.confidence,
            "ma_wave_strength": result.wave_strength,
            "ma_wave_entry_signal": result.entry_signal,
            "ma_wave_support": result.support_level,
            "ma_wave_resistance": result.resistance_level,
        })

        wc = None
        if _WAVE_COUNTER is not None:
            try:
                wc = _WAVE_COUNTER.count(bars)
                def _enrich_points(points, src_bars):
                    enriched = []
                    for p in (points or []):
                        idx = p.get("idx", 0)
                        bar_time = ""
                        if 0 <= idx < len(src_bars):
                            for tk in ("t", "time", "datetime", "trade_time"):
                                if tk in src_bars[idx]:
                                    bar_time = str(src_bars[idx][tk])
                                    break
                        enriched.append({"idx": idx, "price": p["price"], "time": bar_time})
                    return enriched
                metrics.update({
                    "wave_number": wc.wave_number,
                    "cycle_number": wc.cycle_number,
                    "wave_phase_label": wc.phase_label,
                    "wave_sub_wave": wc.sub_wave,
                    "wave_start_idx": wc.wave_start_idx,
                    "wave_peaks_detail": _enrich_points(wc.wave_peaks, bars),
                    "wave_troughs_detail": _enrich_points(wc.wave_troughs, bars),
                })
            except Exception as exc:
                metrics["wave_counter_error"] = str(exc)[:160]
                logger.warning(
                    "GO100 wave counter error for %s; preserving existing decision: %s",
                    stock_code,
                    exc,
                )

        if _WAVE_ML_PREDICTOR is not None:
            try:
                wm = _WAVE_MEASURER.measure(bars, wc) if _WAVE_MEASURER and wc else None
                pb_result = None
                pk_result = None
                if _WAVE_PROB_MODEL and wm:
                    ma_vals = result.ma_values
                    volumes = [float(b.get("v", b.get("volume", 0))) for b in bars]
                    avg_v20 = sum(volumes[-20:]) / 20 if len(volumes) >= 20 else 1
                    vr = volumes[-1] / avg_v20 if avg_v20 > 0 else 1.0
                    pb_result = _WAVE_PROB_MODEL.calculate_pullback(
                        price=price, ma5=ma_vals.get("ma5", 0) or 0,
                        ma10=ma_vals.get("ma10", 0) or 0, ma20=ma_vals.get("ma20", 0) or 0,
                        fib_ratio=wm.fib_ratio, volume_ratio=vr,
                        time_bars=wm.time_bars, disparity_pct=result.disparity_pct,
                        arrangement=result.ma_arrangement,
                    )
                    pk_result = _WAVE_PROB_MODEL.calculate_peak(
                        price=price, ma5=ma_vals.get("ma5", 0) or 0,
                        ma20=ma_vals.get("ma20", 0) or 0,
                        disparity_pct=result.disparity_pct, volume_ratio=vr,
                        extension_ratio=wm.extension_ratio,
                        ma5_slope=result.metrics.get("ma5_slope_pct", 0),
                        wave_age_bars=wm.time_bars,
                        cycle_number=wc.cycle_number if wc else 1,
                    )
                mtf_kwargs = {}
                if _WAVE_MTF_ANALYZER is not None:
                    mtf_result = _WAVE_MTF_ANALYZER.analyze(bars)
                    tf_results = mtf_result.tf_results or {}

                    def _tf_trend(tf_key: str) -> str:
                        tf_result = tf_results.get(tf_key)
                        if tf_result is None:
                            return "NEUTRAL"
                        if isinstance(tf_result, dict):
                            return tf_result.get("trend", "NEUTRAL")
                        return getattr(tf_result, "trend", "NEUTRAL")

                    def _tf_strength(tf_key: str) -> int:
                        tf_result = tf_results.get(tf_key)
                        if tf_result is None:
                            return 0
                        if isinstance(tf_result, dict):
                            return int(tf_result.get("strength", 0) or 0)
                        return int(getattr(tf_result, "strength", 0) or 0)

                    trend_map = {"BULLISH": 1, "NEUTRAL": 0, "BEARISH": -1}
                    tf_order = ["1m", "3m", "5m", "10m", "15m", "30m", "60m"]
                    trend_values = [trend_map.get(_tf_trend(tf_key), 0) for tf_key in tf_order]
                    fractal_positions = build_fractal_position_features(bars)
                    mtf_kwargs = {
                        "mtf_aligned": mtf_result.aligned,
                        "mtf_confidence_boost": mtf_result.confidence_boost,
                        "mtf_consensus": round(sum(trend_values) / len(trend_values), 4),
                        "all_tf_aligned": len(set(trend_values)) == 1 and trend_values[0] != 0,
                        "mtf_override": mtf_result.override_action or "",
                        "upper_bullish_lower_bearish": trend_values[-1] == 1 and trend_values[0] == -1,
                        "lower_turning": False,
                        "trend_1m_turning": False,
                        **{f"tf_{tf_key}_trend": _tf_trend(tf_key) for tf_key in tf_order},
                        **{f"tf_{tf_key}_strength": _tf_strength(tf_key) for tf_key in tf_order},
                        **fractal_positions,
                    }
                    metrics.update({
                        "ma_wave_mtf_aligned": mtf_result.aligned,
                        "ma_wave_mtf_consensus": mtf_kwargs["mtf_consensus"],
                        "ma_wave_mtf_override": mtf_kwargs["mtf_override"],
                        "ma_wave_pos_1m_in_3m": fractal_positions.get("pos_1m_in_3m"),
                        "ma_wave_pos_1m_in_5m": fractal_positions.get("pos_1m_in_5m"),
                    })
                # [P1-2] 축적된 팩터 적중률로 눌림 확률을 보정한다.
                # 기본 shadow 모드에서는 metrics 기록만 하고 값은 바꾸지 않는다.
                pullback_prob_effective = pb_result.probability if pb_result else 0
                if pb_result is not None:
                    pullback_prob_effective = apply_wave_calibration(
                        metrics,
                        pb_result.probability,
                        pb_result.factors,
                        get_wave_calibrator(_get_db_params()),
                    )
                ml_pred = _WAVE_ML_PREDICTOR.predict(
                    wave_phase=result.wave_phase,
                    ma_arrangement=result.ma_arrangement,
                    wave_number=wc.wave_number if wc else 0,
                    cycle_number=wc.cycle_number if wc else 1,
                    pullback_probability=pullback_prob_effective,
                    peak_probability=pk_result.probability if pk_result else 0,
                    wave_strength=result.wave_strength,
                    confidence=result.confidence,
                    disparity_pct=result.disparity_pct,
                    volume_ratio=vr if wm else 1.0,
                    fib_ratio=wm.fib_ratio if wm else 0,
                    extension_ratio=wm.extension_ratio if wm else 0,
                    amplitude_pct=wm.amplitude_pct if wm else 0,
                    depth_pct=wm.depth_pct if wm else 0,
                    symmetry_ratio=wm.symmetry_ratio if wm else 0,
                    entry_signal=result.entry_signal,
                    exit_signal=result.exit_signal,
                    ma5_slope=result.metrics.get("ma5_slope_pct", 0),
                    pullback_factors=pb_result.factors if pb_result else None,
                    peak_factors=pk_result.factors if pk_result else None,
                    **mtf_kwargs,
                )
                metrics["ml_win_probability"] = ml_pred.win_probability
                metrics["ml_predicted_outcome"] = ml_pred.predicted_outcome
                metrics["ml_confidence"] = ml_pred.confidence
                # [P0-1b] predictor의 게이트 판정을 그대로 보존한다.
                metrics["ml_model_loaded"] = ml_pred.model_loaded
                metrics["ml_gate_pass"] = ml_pred.gate_pass
                metrics["ml_gate_reason"] = ml_pred.gate_reason
            except Exception as exc:
                # [P0-1b] 기존에는 예외를 삼켜 기본값 50.0으로 무조건 통과했다.
                metrics["ml_gate_error"] = str(exc)[:160]
                logger.warning(
                    "GO100 wave ML predict error for %s: %s", stock_code, exc
                )

        if not result.entry_signal:
            return False, metrics

        if result.confidence < min_confidence:
            metrics["ma_wave_status"] = "confidence_too_low"
            return False, metrics

        # [P0-1b] predictor 게이트 판정 사용 (하드코딩 25% 비교 제거).
        ml_gate_passed, ml_gate_status = evaluate_wave_ml_gate(metrics)
        metrics["ml_gate_decision"] = ml_gate_status
        if not ml_gate_passed:
            metrics["ma_wave_status"] = ml_gate_status
            return False, metrics

        if not self._apply_wave_slippage_gate(stock_code, price, rule, result, metrics):
            return False, metrics

        if not self._apply_wave_failure_gate(stock_code, bars, wc, metrics):
            return False, metrics

        return True, metrics

    def _evaluate_1min_wave_pullback(
        self, stock_code: str, price: float, rule: dict,
    ) -> tuple[bool, dict]:
        lookback = int(rule.get("wave_lookback_bars", 12))
        session_origin_enabled = _as_bool(rule.get("wave_session_origin_enabled"), True)
        session_origin = str(rule.get("wave_session_origin", "regular_0900"))
        session_buffer_bars = int(rule.get("wave_session_buffer_bars", _SESSION_WAVE_BUFFER_BARS))
        min_bars = int(rule.get("wave_min_bars", 4))
        hydrated = 0
        if _WAVE_BAR_SOURCE_MODE != "ws_memory_legacy":
            hydrated = self._hydrate_minute_ohlc_from_db(
                stock_code,
                min_bars=max(min_bars, lookback),
                limit=max(session_buffer_bars, lookback, 60),
            )
        bars = self._get_minute_ohlc_series(stock_code)
        min_wave_gain = abs(float(rule.get("wave_min_gain_pct", 0.6))) / 100.0
        min_pullback = abs(float(rule.get("wave_min_pullback_pct", 0.25))) / 100.0
        max_pullback = abs(float(rule.get("wave_max_pullback_pct", 3.0))) / 100.0
        min_rebound = abs(float(rule.get("wave_min_rebound_pct", 0.12))) / 100.0
        near_peak_buffer = abs(float(rule.get("wave_entry_peak_buffer_pct", 0.3))) / 100.0
        mtf_gate_enabled = _as_bool(rule.get("wave_mtf_gate_enabled"), True)
        mtf_min_consensus = float(rule.get("wave_mtf_min_consensus", -0.2))
        mtf_required_timeframes = ["1m", "3m", "5m", "10m"]
        mtf_min_bullish_count = int(rule.get("wave_mtf_min_bullish_count", 0))
        require_volume_contraction = _as_bool(rule.get("wave_require_volume_contraction"), False)
        max_volume_contraction_ratio = float(rule.get("wave_max_volume_contraction_ratio", 0.85))
        require_rebound_candle = _as_bool(rule.get("wave_require_rebound_candle"), False)
        require_w2_low_confirmed = _as_bool(rule.get("wave_require_w2_low_confirmed"), True)
        w2_low_confirm_bars = max(1, int(rule.get("wave_w2_low_confirm_bars", 1)))
        opening_fast_wave_enabled = _as_bool(rule.get("opening_fast_wave_enabled"), False)
        opening_fast_wave_min_bars = int(rule.get("opening_fast_wave_min_bars", _OPENING_FAST_WAVE_MIN_BARS))
        metrics = {
            "wave_status": "not_evaluated",
            "wave_bar_count": len(bars),
            "wave_timeframe": "1m",
            "wave_min_gain_pct": round(min_wave_gain * 100, 3),
            "wave_min_pullback_pct": round(min_pullback * 100, 3),
            "wave_mtf_gate_enabled": mtf_gate_enabled,
            "wave_require_volume_contraction": require_volume_contraction,
            "wave_require_rebound_candle": require_rebound_candle,
            "wave_require_w2_low_confirmed": require_w2_low_confirmed,
            "wave_w2_low_confirm_bars": w2_low_confirm_bars,
            "wave_session_origin_enabled": session_origin_enabled,
            "wave_session_origin": session_origin,
            "wave_session_buffer_bars": session_buffer_bars,
            "wave_data_source_mode": _WAVE_BAR_SOURCE_MODE,
            "wave_db_hydrated_bars": hydrated,
            "opening_wave_active": False,
            "opening_fast_wave_enabled": opening_fast_wave_enabled,
        }
        if _WAVE_BAR_SOURCE_MODE == "db_shard_only" and hydrated < min_bars:
            metrics["wave_status"] = "db_shard_data_insufficient"
            return False, metrics
        session_bars = (
            filter_regular_session_bars(bars) if session_origin_enabled and filter_regular_session_bars else []
        )
        session_origin_gap_minutes = None
        if session_bars:
            try:
                session_origin_gap_minutes = int(session_bars[0].get("session_minute") or 0)
            except (TypeError, ValueError):
                session_origin_gap_minutes = 0
        needs_session_hydrate = session_origin_enabled and (
            len(session_bars) < min_bars
            or (
                session_origin_gap_minutes is not None
                and session_origin_gap_minutes > 5
                and len(session_bars) < min(session_buffer_bars, 60)
            )
        )
        if len(bars) < min_bars or needs_session_hydrate:
            hydrated = self._hydrate_minute_ohlc_from_db(
                stock_code,
                min_bars=max(min_bars, lookback),
                limit=max(session_buffer_bars, lookback, 60),
            )
            bars = self._get_minute_ohlc_series(stock_code)
            session_bars = (
                filter_regular_session_bars(bars) if session_origin_enabled and filter_regular_session_bars else []
            )
            metrics["wave_db_hydrated_bars"] = hydrated
            metrics["wave_bar_count"] = len(bars)
        elif len(bars) < lookback and not session_origin_enabled:
            if hydrated <= 0:
                metrics["wave_db_hydrate_skipped"] = "in_memory_min_bars_available"
            else:
                metrics["wave_db_hydrate_shortfall"] = "lookback_short_minimum_bars_available"

        if session_origin_enabled and build_intraday_session_wave_features:
            metrics.update(build_intraday_session_wave_features(bars))

        source_bars = session_bars if session_origin_enabled else bars
        if len(source_bars) < min_bars:
            metrics["wave_status"] = "warmup_blocked"
            metrics["wave_bar_count"] = len(bars)
            metrics["wave_session_bar_count"] = len(session_bars)
            return False, metrics

        window = source_bars if session_origin_enabled else bars[-lookback:]
        if len(window) < min_bars:
            metrics["wave_status"] = "warmup_blocked"
            return False, metrics

        def _hhmm_int(value) -> int | None:
            if value is None:
                return None
            if hasattr(value, "strftime"):
                return int(value.strftime("%H%M"))
            text = str(value or "").strip().replace(":", "")
            if len(text) >= 4 and text[:4].isdigit():
                return int(text[:4])
            return None

        latest_bar_time = _hhmm_int(
            window[-1].get("minute")
            or window[-1].get("minute_dt")
            or window[-1].get("datetime")
            or window[-1].get("time")
        )
        regular_end = _hhmm_int(rule.get("opening_fast_wave_regular_end", _OPENING_FAST_WAVE_REGULAR_END)) or 912
        nxt_end = _hhmm_int(rule.get("opening_fast_wave_nxt_end", _OPENING_FAST_WAVE_NXT_END)) or 812
        opening_wave_active = bool(
            opening_fast_wave_enabled
            and len(window) >= opening_fast_wave_min_bars
            and latest_bar_time is not None
            and (
                800 <= latest_bar_time <= nxt_end
                or 900 <= latest_bar_time <= regular_end
            )
        )
        metrics.update({
            "opening_wave_active": opening_wave_active,
            "opening_fast_wave_min_bars": opening_fast_wave_min_bars,
            "opening_fast_wave_latest_hhmm": latest_bar_time,
        })
        opening_fast_candidate = None
        if opening_wave_active:
            fast_lookback = max(opening_fast_wave_min_bars, int(rule.get("opening_fast_wave_lookback_bars", 8)))
            fast_start_idx = max(0, len(window) - fast_lookback)
            opening_fast_candidate = _detect_opening_fast_wave_pair(
                window[fast_start_idx:],
                price,
                rule,
                min_wave_gain,
                min_rebound,
                near_peak_buffer,
            )
            metrics["opening_fast_wave_detected"] = opening_fast_candidate is not None
            metrics["opening_fast_wave_lookback_bars"] = fast_lookback
            if opening_fast_candidate is not None:
                opening_fast_candidate = dict(opening_fast_candidate)
                opening_fast_candidate["peak_idx"] += fast_start_idx
                opening_fast_candidate["pullback_low_idx"] += fast_start_idx
                metrics.update({
                    "opening_fast_wave_peak_index": opening_fast_candidate["peak_idx"],
                    "opening_fast_wave_pullback_low_index": opening_fast_candidate["pullback_low_idx"],
                    "opening_fast_wave_bars_after_low": opening_fast_candidate["bars_after_pullback_low"],
                    "opening_fast_wave_gain_pct": opening_fast_candidate["wave_gain_pct"],
                    "opening_fast_wave_pullback_depth_pct": opening_fast_candidate["pullback_depth_pct"],
                    "opening_fast_wave_rebound_pct": opening_fast_candidate["rebound_from_pullback_pct"],
                })

        provisional_peak_idx = max(range(len(window)), key=lambda i: float(window[i].get("h") or 0))
        metrics["provisional_wave_peak_index"] = provisional_peak_idx
        wave_counter_peak_idx = None
        wave_counter_peak_high = None
        wave_counter_low_idx = None
        wave_counter_low = None
        if _WAVE_COUNTER is not None:
            try:
                wc_result = _WAVE_COUNTER.count(window)
                metrics["wave_counter_number"] = wc_result.wave_number
                metrics["wave_counter_phase_label"] = wc_result.phase_label
                metrics["wave_counter_peaks"] = wc_result.wave_peaks
                metrics["wave_counter_troughs"] = wc_result.wave_troughs
                confirmed_w2_pairs = []
                for trough in (wc_result.wave_troughs or []):
                    try:
                        trough_idx = int(trough.get("idx", -1))
                        trough_price = float(trough.get("price") or 0)
                    except (TypeError, ValueError):
                        continue
                    if trough_idx <= 0 or trough_idx >= len(window) - 1 or trough_price <= 0:
                        continue
                    prior_peak = None
                    for peak in (wc_result.wave_peaks or []):
                        try:
                            peak_idx_candidate = int(peak.get("idx", -1))
                            peak_price_candidate = float(peak.get("price") or 0)
                        except (TypeError, ValueError):
                            continue
                        if peak_idx_candidate < trough_idx and peak_price_candidate > 0:
                            prior_peak = {
                                "idx": peak_idx_candidate,
                                "price": peak_price_candidate,
                            }
                    if prior_peak is not None:
                        confirmed_w2_pairs.append((prior_peak, {"idx": trough_idx, "price": trough_price}))
                metrics["wave_counter_confirmed_w2_pair_count"] = len(confirmed_w2_pairs)
                if confirmed_w2_pairs:
                    last_peak, last_w2 = confirmed_w2_pairs[-1]
                    wave_counter_peak_idx = int(last_peak["idx"])
                    wave_counter_peak_high = float(last_peak["price"])
                    wave_counter_low_idx = int(last_w2["idx"])
                    wave_counter_low = float(last_w2["price"])
            except Exception as exc:
                metrics["wave_counter_error"] = str(exc)[:120]

        wave1_start_override = None
        if (
            wave_counter_peak_idx is not None
            and wave_counter_peak_high is not None
            and wave_counter_low_idx is not None
            and wave_counter_low is not None
            and wave_counter_peak_idx < wave_counter_low_idx < len(window) - 1
            and wave_counter_peak_high > 0
            and wave_counter_low > 0
        ):
            peak_idx = wave_counter_peak_idx
            wave1_high = wave_counter_peak_high
            pullback_low_idx = wave_counter_low_idx
            pullback_low = wave_counter_low
            wave_peak_source = "wave_counter_confirmed_w1"
            pullback_low_source = "wave_counter_confirmed_w2"
        elif opening_fast_candidate is not None:
            peak_idx = int(opening_fast_candidate["peak_idx"])
            wave1_high = float(opening_fast_candidate["wave1_high"])
            pullback_low_idx = int(opening_fast_candidate["pullback_low_idx"])
            pullback_low = float(opening_fast_candidate["pullback_low"])
            wave1_start_override = float(opening_fast_candidate["wave1_start"])
            wave_peak_source = "opening_fast_wave_w1"
            pullback_low_source = "opening_fast_wave_w2"
        else:
            peak_idx = provisional_peak_idx
            if peak_idx <= 0 or peak_idx >= len(window) - 1:
                metrics["wave_status"] = "wave_peak_not_fixed"
                metrics["wave_peak_index"] = peak_idx
                metrics["wave_peak_source"] = "session_price_provisional"
                return False, metrics
            wave1_high = float(window[peak_idx].get("h") or 0)
            wave_peak_source = "session_price_provisional"
            pullback_low_source = "session_price_provisional_w2"

        if wave1_start_override is not None:
            wave1_start = wave1_start_override
        elif session_origin_enabled:
            first_session_bar = window[0]
            wave1_start = float(
                first_session_bar.get("o")
                or first_session_bar.get("open")
                or first_session_bar.get("l")
                or first_session_bar.get("c")
                or 0
            )
        else:
            wave1_start = min(float(b.get("l") or b.get("c") or 0) for b in window[:peak_idx + 1])
        post_peak_bars = list(window[peak_idx + 1:])
        if not post_peak_bars:
            metrics["wave_status"] = "wave_pullback_not_started"
            return False, metrics
        pullback_low_offset = min(
            range(len(post_peak_bars)),
            key=lambda i: float(post_peak_bars[i].get("l") or post_peak_bars[i].get("c") or 0),
        )
        provisional_pullback_low_idx = peak_idx + 1 + pullback_low_offset
        provisional_pullback_low = float(
            post_peak_bars[pullback_low_offset].get("l")
            or post_peak_bars[pullback_low_offset].get("c")
            or 0
        )
        if pullback_low_source not in ("wave_counter_confirmed_w2", "opening_fast_wave_w2"):
            pullback_low_idx = provisional_pullback_low_idx
            pullback_low = provisional_pullback_low
        bars_after_pullback_low = len(window) - pullback_low_idx - 1
        opening_fast_wave_structural = pullback_low_source == "opening_fast_wave_w2"
        effective_w2_low_confirm_bars = w2_low_confirm_bars
        if opening_fast_wave_structural:
            effective_w2_low_confirm_bars = max(0, int(rule.get("opening_fast_wave_w2_low_confirm_bars", 0)))
        if wave1_start <= 0 or wave1_high <= 0 or pullback_low <= 0:
            metrics["wave_status"] = "invalid_wave_prices"
            return False, metrics

        wave_gain = (wave1_high - wave1_start) / wave1_start
        pullback_depth = (wave1_high - pullback_low) / wave1_high
        rebound = (price - pullback_low) / pullback_low
        price_to_peak = (price - wave1_high) / wave1_high
        recent_high = max(float(b.get("h") or b.get("c") or 0) for b in window[-min(5, len(window)):])
        pre_peak_volumes = [float(b.get("v") or 0) for b in window[:peak_idx + 1] if float(b.get("v") or 0) > 0]
        pullback_volumes = [float(b.get("v") or 0) for b in window[peak_idx + 1:] if float(b.get("v") or 0) > 0]
        pre_peak_avg_volume = sum(pre_peak_volumes) / len(pre_peak_volumes) if pre_peak_volumes else 0.0
        pullback_avg_volume = sum(pullback_volumes) / len(pullback_volumes) if pullback_volumes else 0.0
        volume_contraction_ratio = (
            pullback_avg_volume / pre_peak_avg_volume
            if pre_peak_avg_volume > 0 and pullback_avg_volume > 0
            else None
        )
        current_bar = window[-1]
        previous_close = float(window[-2].get("c") or 0) if len(window) >= 2 else 0.0
        rebound_candle_confirmed = (
            price > float(current_bar.get("o") or price)
            and (previous_close <= 0 or price > previous_close)
        )
        current_open = float(current_bar.get("o") or price)
        current_low = float(current_bar.get("l") or current_bar.get("c") or price)
        opening_fast_w2_confirmed = bool(
            opening_wave_active
            and require_w2_low_confirmed
            and bars_after_pullback_low < w2_low_confirm_bars
            and pullback_low_idx == len(window) - 1
            and peak_idx < pullback_low_idx
            and current_low <= pullback_low
            and price > current_open
            and rebound >= min_rebound
        )
        w2_low_confirmed = bool(
            bars_after_pullback_low >= effective_w2_low_confirm_bars
            or (opening_fast_wave_structural and bars_after_pullback_low >= effective_w2_low_confirm_bars)
            or opening_fast_w2_confirmed
        )
        metrics.update({
            "wave1_start": round(wave1_start, 1),
            "fixed_wave_peak": round(wave1_high, 1),
            "wave_peak_index": peak_idx,
            "wave_peak_source": wave_peak_source,
            "pullback_low": round(pullback_low, 1),
            "pullback_low_index": pullback_low_idx,
            "pullback_low_source": pullback_low_source,
            "provisional_pullback_low": round(provisional_pullback_low, 1),
            "provisional_pullback_low_index": provisional_pullback_low_idx,
            "bars_after_pullback_low": bars_after_pullback_low,
            "w2_low_confirmed": w2_low_confirmed,
            "wave_w2_low_confirm_bars_effective": effective_w2_low_confirm_bars,
            "opening_fast_wave_structural": opening_fast_wave_structural,
            "opening_fast_w2_confirmed": opening_fast_w2_confirmed,
            "wave_sequence_confirmed": (
                peak_idx < pullback_low_idx
                and w2_low_confirmed
            ),
            "wave_gain_pct": round(wave_gain * 100, 3),
            "pullback_depth_pct": round(pullback_depth * 100, 3),
            "rebound_from_pullback_pct": round(rebound * 100, 3),
            "price_to_fixed_wave_peak_pct": round(price_to_peak * 100, 3),
            "recent_high": round(recent_high, 1),
            "wave_segments": {
                "wave1": {"start": round(wave1_start, 1), "high": round(wave1_high, 1)},
                "wave2": {"high": round(wave1_high, 1), "low": round(pullback_low, 1)},
                "wave3": {"low": round(pullback_low, 1), "current": round(price, 1)},
            },
            "wave_current_phase": "wave3_rebound_candidate",
            "volume_contraction_ratio": round(volume_contraction_ratio, 4) if volume_contraction_ratio is not None else None,
            "volume_contraction_status": (
                "contracted" if volume_contraction_ratio is not None and volume_contraction_ratio <= max_volume_contraction_ratio
                else "not_contracted" if volume_contraction_ratio is not None
                else "missing"
            ),
            "rebound_candle_confirmed": rebound_candle_confirmed,
        })

        if wave_gain < min_wave_gain:
            metrics["wave_status"] = "wave_gain_too_small"
            return False, metrics
        # In W2-confirmed mode the high→confirmed-low sequence is the structural
        # gate; min_pullback is kept as diagnostic only so that a shallow but
        # real W2 low is not blocked by an arbitrary depth threshold.
        if pullback_depth < min_pullback and not require_w2_low_confirmed:
            metrics["wave_status"] = "pullback_too_shallow"
            return False, metrics
        if pullback_depth > max_pullback:
            metrics["wave_status"] = "pullback_too_deep"
            return False, metrics
        if require_w2_low_confirmed and not w2_low_confirmed:
            metrics["wave_status"] = "w2_low_not_confirmed"
            return False, metrics
        if rebound < min_rebound:
            metrics["wave_status"] = "rebound_not_confirmed"
            return False, metrics
        if require_volume_contraction and (
            volume_contraction_ratio is None or volume_contraction_ratio > max_volume_contraction_ratio
        ):
            metrics["wave_status"] = "volume_contraction_not_confirmed"
            return False, metrics
        if require_rebound_candle and not rebound_candle_confirmed:
            metrics["wave_status"] = "rebound_candle_not_confirmed"
            return False, metrics
        if price > wave1_high * (1 + near_peak_buffer):
            metrics["wave_status"] = "entry_too_far_above_wave_peak"
            return False, metrics

        if _WAVE_MTF_ANALYZER is not None:
            try:
                mtf_result = _WAVE_MTF_ANALYZER.analyze(bars)
                tf_results = mtf_result.tf_results or {}

                def _tf_trend(tf_key: str) -> str:
                    tf_result = tf_results.get(tf_key)
                    if tf_result is None:
                        return "NEUTRAL"
                    if isinstance(tf_result, dict):
                        return str(tf_result.get("trend", "NEUTRAL") or "NEUTRAL")
                    return str(getattr(tf_result, "trend", "NEUTRAL") or "NEUTRAL")

                trend_map = {"BULLISH": 1, "NEUTRAL": 0, "BEARISH": -1}
                tf_order = mtf_required_timeframes
                tf_trends = {tf_key: _tf_trend(tf_key).upper() for tf_key in tf_order}
                trend_values = [trend_map.get(tf_trends.get(tf_key, "NEUTRAL"), 0) for tf_key in tf_order]
                upper_values = trend_values[1:]
                mtf_consensus = round(sum(trend_values) / len(trend_values), 4)
                bearish_upper = sum(1 for value in upper_values if value < 0)
                bullish_upper = sum(1 for value in upper_values if value > 0)
                bullish_required = sum(1 for value in trend_values if value > 0)
                selected_timeframes = [tf for tf, trend in tf_trends.items() if trend == "BULLISH"]
                mtf_alignment_score = round(max(mtf_consensus, 0.0) * 100.0, 1)
                fractal_positions = build_fractal_position_features(bars)
                metrics.update({
                    "wave_mtf_aligned": mtf_result.aligned,
                    "wave_mtf_confidence_boost": mtf_result.confidence_boost,
                    "wave_mtf_consensus": mtf_consensus,
                    "wave_mtf_alignment_score": mtf_alignment_score,
                    "mtf_alignment_score": mtf_alignment_score,
                    "selected_timeframes": selected_timeframes,
                    "wave_mtf_override": mtf_result.override_action or "",
                    "wave_tf_1m_trend": tf_trends.get("1m"),
                    "wave_tf_3m_trend": tf_trends.get("3m"),
                    "wave_tf_5m_trend": tf_trends.get("5m"),
                    "wave_tf_10m_trend": tf_trends.get("10m"),
                    "wave_required_bullish_count": bullish_required,
                    "wave_upper_bullish_count": bullish_upper,
                    "wave_upper_bearish_count": bearish_upper,
                    "wave_pos_1m_in_3m": fractal_positions.get("pos_1m_in_3m"),
                    "wave_pos_1m_in_5m": fractal_positions.get("pos_1m_in_5m"),
                    "wave_pos_1m_in_10m": fractal_positions.get("pos_1m_in_10m"),
                    "wave_pos_1m_in_30m": fractal_positions.get("pos_1m_in_30m"),
                    "mtf_consensus": mtf_consensus,
                    "mtf_confirmation": {
                        "primary": "1m",
                        "required": tf_trends,
                        "selected_timeframes": selected_timeframes,
                    },
                })
                # [B안] 추세 연속성 — 게이트 판정 전 이력 축적
                if _TREND_CONTINUITY_TRACKER is not None:
                    _TREND_CONTINUITY_TRACKER.update(stock_code, tf_trends)
                if mtf_gate_enabled:
                    override_text = str(mtf_result.override_action or "")
                    if "매수 차단" in override_text or "역추세" in override_text:
                        metrics["wave_status"] = "mtf_upper_trend_blocked"
                        return False, metrics
                    if mtf_consensus < mtf_min_consensus:
                        metrics["wave_status"] = "mtf_consensus_too_low"
                        return False, metrics
                    # [A안] 상위 TF 보존 — 1m BULLISH 강제 제거.
                    # 장초반에는 5/10분봉 warmup이 늦으므로 W1→W2 구조가 확인된
                    # opening wave에서는 상위 TF 최소 강세 수를 별도 완화값으로 적용한다.
                    if opening_wave_active:
                        upper_min = int(rule.get("opening_fast_wave_mtf_min_upper_bullish", 1))
                        metrics["opening_wave_mtf_relaxed"] = True
                    else:
                        upper_min = max(mtf_min_bullish_count - 1, 2)
                        metrics["opening_wave_mtf_relaxed"] = False
                    trend_alive = bullish_upper >= upper_min and bearish_upper == 0
                    metrics["wave_trend_alive"] = trend_alive
                    metrics["wave_upper_min_required"] = upper_min
                    if not trend_alive:
                        if bearish_upper > 0:
                            metrics["wave_status"] = "mtf_upper_bearish_blocked"
                        else:
                            metrics["wave_status"] = "mtf_upper_bullish_insufficient"
                        return False, metrics
                    # [B안] 추세 연속성 — 이력 기반 추세 사망 감지
                    if _TREND_CONTINUITY_TRACKER is not None:
                        continuity = _TREND_CONTINUITY_TRACKER.is_trend_alive(stock_code)
                        metrics["wave_trend_continuity"] = continuity
                        if continuity.get("alive") is False:
                            metrics["wave_status"] = "trend_continuity_dead"
                            return False, metrics
            except Exception as exc:
                metrics["wave_mtf_error"] = str(exc)[:120]

        # [P0] 일봉(D1) 추세 필터 — 일봉 BEARISH(강도 60+)면 진입 차단
        if _DAILY_TREND_FILTER is not None:
            try:
                _daily = _DAILY_TREND_FILTER.get_trend(stock_code, _get_db_params())
                metrics["daily_trend"] = _daily.get("trend")
                metrics["daily_trend_strength"] = _daily.get("strength", 0)
                metrics["daily_trend_source"] = _daily.get("source")
                if _daily.get("trend") == "BEARISH" and _daily.get("strength", 0) >= 60:
                    metrics["wave_status"] = "daily_trend_bearish_blocked"
                    return False, metrics
            except Exception as _dte:
                metrics["daily_trend_error"] = str(_dte)[:80]

        # [P1-1] 일봉 파동 분석 — 일봉 파동 위치(W1~W5, 사이클) 추적
        if _DAILY_TREND_FILTER is not None:
            try:
                _dw = _DAILY_TREND_FILTER.get_daily_wave(stock_code, _get_db_params())
                metrics["daily_wave_number"] = _dw.get("daily_wave_number")
                metrics["daily_cycle_number"] = _dw.get("daily_cycle_number")
                metrics["daily_phase_label"] = _dw.get("daily_phase_label")
                metrics["daily_sub_wave"] = _dw.get("daily_sub_wave")
                metrics["daily_wave_start_date"] = _dw.get("daily_wave_start_date")
                metrics["daily_wave_peaks"] = _dw.get("daily_wave_peaks")
                metrics["daily_wave_troughs"] = _dw.get("daily_wave_troughs")

                # [P1-2] 크로스 TF 연동: 일봉 상승파(W1/W3) + 분봉 눌림 = 우호적
                daily_wn = _dw.get("daily_wave_number", 0)
                if daily_wn in (1, 3):
                    metrics["daily_wave_favorable"] = True
                    metrics["daily_wave_assessment"] = "daily_impulse_wave_favorable"
                elif daily_wn in (4, 5):
                    metrics["daily_wave_favorable"] = False
                    metrics["daily_wave_assessment"] = "daily_late_cycle_caution"
                elif daily_wn == 2:
                    metrics["daily_wave_favorable"] = None
                    metrics["daily_wave_assessment"] = "daily_correction_wave_neutral"
                else:
                    metrics["daily_wave_favorable"] = None
                    metrics["daily_wave_assessment"] = "daily_wave_indeterminate"
            except Exception as _dwe:
                metrics["daily_wave_error"] = str(_dwe)[:80]

        # [P1] 하락 파동(BearishWaveAnalyzer) — ABC 조정파/하락 국면 매수 차단
        if _BEARISH_WAVE_ANALYZER is not None and bars and len(bars) >= 20:
            try:
                _bwa_closes = [float(b.get("c", 0)) for b in bars]
                _bwa_ma = {
                    "ma5": sum(_bwa_closes[-5:]) / 5 if len(_bwa_closes) >= 5 else None,
                    "ma10": sum(_bwa_closes[-10:]) / 10 if len(_bwa_closes) >= 10 else None,
                    "ma20": sum(_bwa_closes[-20:]) / 20 if len(_bwa_closes) >= 20 else None,
                    "ma60": sum(_bwa_closes[-60:]) / 60 if len(_bwa_closes) >= 60 else None,
                }
                _bwa_result = _BEARISH_WAVE_ANALYZER.analyze(bars, _bwa_ma)
                metrics["bearish_phase"] = _bwa_result.bearish_phase
                metrics["bearish_entry_allowed"] = _bwa_result.entry_allowed
                metrics["bearish_risk_level"] = _bwa_result.risk_level
                metrics["bearish_strategies"] = _bwa_result.strategies
                if not _bwa_result.entry_allowed:
                    metrics["wave_status"] = f"bearish_wave_blocked_{_bwa_result.bearish_phase}"
                    return False, metrics
            except Exception as _bwe:
                metrics["bearish_wave_error"] = str(_bwe)[:80]

        # [P0-1] 프랙탈 피벗 저점 확인 — 눌림 저점이 Williams fractal 구조적 저점인지 검증
        if detect_fractal_pivot_lows is not None:
            try:
                _fractal_n = int(rule.get("wave_fractal_n", 2))
                _fractal_tol = float(rule.get("wave_fractal_tolerance_pct", 0.3))
                _fractal_min_bars = 2 * _fractal_n + 1
                if len(window) < _fractal_min_bars:
                    metrics["fractal_gate_skipped"] = "insufficient_bars"
                    metrics["fractal_aligned"] = None
                    metrics["fractal_min_bars_required"] = _fractal_min_bars
                else:
                    _fractal_lows = detect_fractal_pivot_lows(window, n=_fractal_n)
                    metrics["fractal_pivot_count"] = len(_fractal_lows)
                    metrics["fractal_pivot_lows"] = [
                        {"idx": p["idx"], "price": round(p["price"], 1), "time": p.get("bar_time")}
                        for p in _fractal_lows[-5:]
                    ]
                    _fractal_aligned = False
                    _nearest_dist = None
                    if _fractal_lows and pullback_low > 0:
                        for _fp in _fractal_lows:
                            _dist = abs(_fp["price"] - pullback_low) / pullback_low * 100
                            if _nearest_dist is None or _dist < _nearest_dist:
                                _nearest_dist = _dist
                            if _dist <= _fractal_tol:
                                _fractal_aligned = True
                                metrics["fractal_aligned_pivot"] = {
                                    "idx": _fp["idx"],
                                    "price": round(_fp["price"], 1),
                                    "distance_pct": round(_dist, 3),
                                }
                                break
                    metrics["fractal_aligned"] = _fractal_aligned
                    metrics["fractal_nearest_distance_pct"] = (
                        round(_nearest_dist, 3) if _nearest_dist is not None else None
                    )
                    _fractal_gate_on = _as_bool(rule.get("wave_fractal_gate_enabled"), True)
                    if _fractal_gate_on and not _fractal_aligned and _fractal_lows:
                        metrics["wave_status"] = "fractal_pivot_not_confirmed"
                        return False, metrics

                # [P0-2] MTF 프랙탈 합치 판정
                _mtf_conf = detect_mtf_fractal_confluence(
                    window, timeframes=(1, 3, 5),
                    price_tolerance_pct=float(rule.get("wave_fractal_mtf_tolerance_pct", 0.5)),
                    n=_fractal_n,
                )
                metrics["fractal_confluence_score"] = _mtf_conf["confluence_score"]
                metrics["fractal_confluence_aligned"] = _mtf_conf["aligned_timeframes"]
                metrics["fractal_confluence_confirmed"] = _mtf_conf["confluence_confirmed"]
                metrics["fractal_confluence_ref_price"] = _mtf_conf["reference_price"]

                # [P0-3] 반등 캔들 확인 (양봉 + 거래량 급증)
                _reb = confirm_rebound_candle(window, pullback_low_idx)
                metrics["fractal_rebound_confirmed"] = _reb["fractal_rebound_confirmed"]
                metrics["fractal_rebound_bullish"] = _reb["bullish_candle"]
                metrics["fractal_rebound_volume_surge"] = _reb["volume_surge"]
                metrics["fractal_rebound_volume_ratio"] = _reb["volume_ratio"]
                metrics["fractal_rebound_body_pct"] = _reb["candle_body_pct"]
                _require_fractal_reb = _as_bool(rule.get("wave_require_fractal_rebound"), False)
                if _require_fractal_reb and not _reb["fractal_rebound_confirmed"]:
                    metrics["wave_status"] = "fractal_rebound_not_confirmed"
                    return False, metrics
            except Exception as _fpe:
                metrics["fractal_pivot_error"] = str(_fpe)[:120]

        metrics["wave_status"] = "wave_pullback_ok"
        metrics["wave_pattern_detected"] = True
        if opening_fast_wave_structural or opening_fast_w2_confirmed:
            metrics["wave_current_phase"] = "opening_fast_wave2_rebound_entry_trigger"
            metrics["entry_gate"] = "opening_fast_w1_w2_reversal_mtf"
        else:
            metrics["wave_current_phase"] = "wave2_rebound_entry_trigger"
            metrics["entry_gate"] = "fractal_confirmed_w2_rebound_mtf"

        # [P2] 계층 통합 — WaveCounter(MA기반) vs 세션파동(가격기반) 교차 판정
        wc_num = metrics.get("wave_number", 0) or 0
        session_phase = metrics.get("session_wave_phase", "")
        if wc_num in (1, 2) and "pullback" in str(session_phase):
            metrics["wave_cross_layer"] = "aligned_early_cycle_pullback"
            metrics["wave_cross_confidence"] = "high"
        elif wc_num == 3 and "rebound" in str(session_phase):
            metrics["wave_cross_layer"] = "aligned_impulse_rebound"
            metrics["wave_cross_confidence"] = "high"
        elif wc_num in (4, 5):
            metrics["wave_cross_layer"] = "late_cycle_caution"
            metrics["wave_cross_confidence"] = "low"
        else:
            metrics["wave_cross_layer"] = "partial_alignment"
            metrics["wave_cross_confidence"] = "medium"

        # [P0-1] 파동 상태 캐시 — API 실시간 조회용
        import time as _time_mod
        _WAVE_STATE_CACHE[stock_code] = {
            "ts": _time_mod.time(),
            "wave_number": metrics.get("wave_number"),
            "cycle_number": metrics.get("cycle_number"),
            "wave_phase_label": metrics.get("wave_phase_label"),
            "wave_sub_wave": metrics.get("wave_sub_wave"),
            "wave_start_idx": metrics.get("wave_start_idx"),
            "wave_peaks_detail": metrics.get("wave_peaks_detail"),
            "wave_troughs_detail": metrics.get("wave_troughs_detail"),
            "wave_status": metrics.get("wave_status"),
            "wave_current_phase": metrics.get("wave_current_phase"),
            "entry_gate": metrics.get("entry_gate"),
            "pullback_low": metrics.get("pullback_low"),
            "pullback_low_index": metrics.get("pullback_low_index"),
            "pullback_low_source": metrics.get("pullback_low_source"),
            "bars_after_pullback_low": metrics.get("bars_after_pullback_low"),
            "w2_low_confirmed": metrics.get("w2_low_confirmed"),
            "wave_require_w2_low_confirmed": metrics.get("wave_require_w2_low_confirmed"),
            "session_wave_phase": metrics.get("session_wave_phase"),
            "daily_trend": metrics.get("daily_trend"),
            "daily_wave_number": metrics.get("daily_wave_number"),
            "daily_phase_label": metrics.get("daily_phase_label"),
            "daily_wave_favorable": metrics.get("daily_wave_favorable"),
            "daily_wave_assessment": metrics.get("daily_wave_assessment"),
            "wave_trend_alive": metrics.get("wave_trend_alive"),
            "wave_cross_layer": metrics.get("wave_cross_layer"),
            "wave_cross_confidence": metrics.get("wave_cross_confidence"),
            "bearish_phase": metrics.get("bearish_phase"),
            "fractal_aligned": metrics.get("fractal_aligned"),
            "fractal_pivot_count": metrics.get("fractal_pivot_count"),
            "fractal_confluence_score": metrics.get("fractal_confluence_score"),
            "fractal_confluence_confirmed": metrics.get("fractal_confluence_confirmed"),
            "fractal_rebound_confirmed": metrics.get("fractal_rebound_confirmed"),
            "fractal_rebound_volume_ratio": metrics.get("fractal_rebound_volume_ratio"),
        }

        return True, metrics

    def _evaluate_ma_pullback(self, stock_code: str, price: float, rule: dict) -> tuple[bool, dict]:
        period = int(rule.get("period", 20))
        pullback_pct = abs(float(rule.get("pullback_pct", 0.5))) / 100.0
        bars = self._minute_bars.get(stock_code)
        cur = self._minute_bar_current.get(stock_code)
        closes = list(bars) if bars else []
        if cur:
            closes.append(cur["c"])
        metrics = {"ma_period": period, "ma_bar_count": len(closes), "ma_pullback_pct": pullback_pct * 100}

        if _as_bool(rule.get("opening_fast_wave_bypass_ma_warmup"), False):
            opening_min_bars = int(rule.get("opening_fast_wave_min_bars", _OPENING_FAST_WAVE_MIN_BARS))
            try:
                from zoneinfo import ZoneInfo
                now_t = datetime.now(ZoneInfo("Asia/Seoul")).time()
            except Exception:
                now_t = datetime.now().time()

            def _hhmm_to_time(text: str, fallback: dt_time) -> dt_time:
                raw = str(text or "").strip().replace(":", "")
                if len(raw) != 4 or not raw.isdigit():
                    return fallback
                return dt_time(int(raw[:2]), int(raw[2:]), 0)

            nxt_fast_end = _hhmm_to_time(_OPENING_FAST_WAVE_NXT_END, dt_time(8, 12, 0))
            regular_fast_end = _hhmm_to_time(_OPENING_FAST_WAVE_REGULAR_END, dt_time(9, 12, 0))
            in_opening_fast_wave = (
                NXT_PRE_OPEN <= now_t <= nxt_fast_end
                or dt_time(9, 0, 0) <= now_t <= regular_fast_end
            )
            if len(closes) < period and len(closes) >= opening_min_bars and in_opening_fast_wave:
                metrics.update({
                    "ma_status": "opening_fast_wave_deferred_to_wave_gate",
                    "ma_warmup_deferred": True,
                    "ma_warmup_deferred_scope": "card303_opening_fast_wave",
                    "opening_fast_wave_min_bars": opening_min_bars,
                    "opening_fast_wave_now": now_t.strftime("%H:%M:%S"),
                })
                return True, metrics

        # [GO100-303 P0] 프로세스 재시작 직후 인메모리 분봉이 부족하면 DB 1분봉으로 보강한다.
        # 그래도 MA 기간을 채우지 못하면 warmup 통과가 아니라 진입 차단으로 처리한다.
        if len(closes) < period:
            cache_ts, cached = self._minute_bar_db_cache.get(stock_code, (0.0, []))
            if not cached or time_module.time() - cache_ts > 30.0:
                try:
                    conn = psycopg2.connect(**_get_db_params())
                    with conn.cursor() as cur_db:
                        cur_db.execute(
                            """
                            SELECT close_price
                            FROM go100_minute_bars
                            WHERE stock_code = %s AND trade_date = CURRENT_DATE
                            ORDER BY trade_time DESC
                            LIMIT %s
                            """,
                            (stock_code, period),
                        )
                        cached = [float(r[0]) for r in reversed(cur_db.fetchall()) if r[0] is not None]
                    conn.close()
                    self._minute_bar_db_cache[stock_code] = (time_module.time(), cached)
                except Exception as exc:
                    metrics["ma_db_fallback_error"] = str(exc)[:120]
                    cached = []
            if cached:
                closes = list(cached[-period:]) + ([cur["c"]] if cur else [])
                metrics["ma_bar_count"] = len(closes)
                metrics["ma_db_fallback"] = True
        if len(closes) < period:
            metrics["ma_status"] = "warmup_blocked"
            return False, metrics
        ma_val = sum(closes[-period:]) / period
        metrics["ma_value"] = round(ma_val, 1)
        metrics["price_vs_ma_pct"] = round((price - ma_val) / ma_val * 100, 3) if ma_val > 0 else 0
        if price < ma_val:
            metrics["ma_status"] = "below_ma"
            return False, metrics
        if ma_val > 0 and (price - ma_val) / ma_val > pullback_pct:
            metrics["ma_status"] = "too_far_above"
            return False, metrics
        metrics["ma_status"] = "pullback_ok"
        return True, metrics

    def _evaluate_entry_with_audit(self, stock_code: str, tick: tuple, card: dict) -> tuple[Optional[str], str, str, dict]:
        if _has_limit_up_entry_rules(card.get("entry_rules", [])):
            return self._evaluate_limit_up_entry_with_audit(stock_code, tick, card)
        if _is_overnight_card(card):
            return self._evaluate_overnight_entry_with_audit(stock_code, tick, card)

        _tw_s, _tw_e = _parse_card_time_window(card.get("entry_rules", []))
        try:
            from zoneinfo import ZoneInfo
            _now_t = datetime.now(ZoneInfo("Asia/Seoul")).time()
        except Exception:
            _now_t = datetime.now().time()

        _in_nxt_am = NXT_PRE_OPEN <= _now_t <= NXT_PRE_CLOSE
        _in_nxt_pm = NXT_AFTER_OPEN <= _now_t <= NXT_AFTER_CLOSE
        _in_nxt_session = _in_nxt_am or _in_nxt_pm
        _window_meta = {"entry_window": f"{_tw_s}-{_tw_e}", "in_nxt_session": _in_nxt_session}

        if _in_nxt_session:
            # _is_entry_allowed()가 True를 반환했으므로 여기는 NXT 활성화 상태.
            # 카드에 nxt_time_window 규칙이 있으면 그것으로 세부 제한, 없으면 전역 허용.
            nxt_windows = _parse_card_nxt_time_windows(card.get("entry_rules", []))
            if nxt_windows:
                if not any(ws <= _now_t <= we for ws, we in nxt_windows):
                    _nxt_win_str = ", ".join(
                        f"{ws.strftime('%H:%M')}~{we.strftime('%H:%M')}"
                        for ws, we in nxt_windows
                    )
                    return (
                        None,
                        "outside_nxt_window",
                        f"카드 NXT 진입 시간창({_nxt_win_str}) 밖",
                        {**_window_meta, "nxt_windows": _nxt_win_str},
                    )
            if not _card_allows_nxt_session(card, _now_t):
                _nxt_policy = _nxt_policy_metadata(card)
                return (
                    None,
                    "nxt_card_not_enabled",
                    "NXT 신규진입 정책상 카드별 명시적 허용이 없어 차단(" 
                    f"{_nxt_policy['nxt_policy']})",
                    {**_window_meta, **_nxt_policy},
                )
            # NXT 세션 시간창 통과 → 일반 time_window 체크 우회하고 진입 평가 계속
        else:
            _tw_st = dt_time(int(_tw_s.split(":")[0]), int(_tw_s.split(":")[1]), 0)
            _tw_et = dt_time(int(_tw_e.split(":")[0]), int(_tw_e.split(":")[1]), 0)
            if _now_t < _tw_st or _now_t > _tw_et:
                return None, "outside_card_window", f"카드 진입 시간창({_tw_s}~{_tw_e}) 밖", _window_meta

        metrics = self._tick_metrics(stock_code, tick)
        price = metrics["price"]
        volume = metrics["tick_volume"]
        signed_volume = float(metrics.get("signed_tick_volume") or 0)
        strength = metrics["strength"]
        history = self._tick_history[stock_code]
        card_id_for_tick_gate = int(card.get("card_id") or card.get("go100_card_id") or 0)
        sell_tick_block_threshold = -5.0 if card_id_for_tick_gate == 303 else 0.0
        metrics["sell_tick_block_threshold"] = sell_tick_block_threshold
        if signed_volume < sell_tick_block_threshold:
            return None, "sell_tick_volume", "매도 우위 체결틱에서는 신규 매수 차단", metrics
        if len(history) < 5:
            return None, "tick_warmup", "틱 히스토리 부족", metrics

        # entry_rules custom_params에서 백서 조건 추출 + orderbook_imbalance 평가
        custom = {}
        for _er in card.get("entry_rules", []):
            if not isinstance(_er, dict):
                continue
            _rule_type = str(_er.get("type") or "").strip().lower()
            if _rule_type == "price_breakout":
                custom = _er.get("custom_params", {})
            elif _rule_type == "orderbook_imbalance":
                _ob_reason, _ob_code, _ob_text, _ob_metrics = _evaluate_orderbook_imbalance_rule(
                    stock_code, _er
                )
                metrics.update(_ob_metrics)
                if _ob_reason is None:
                    return None, _ob_code, _ob_text, metrics
            elif _rule_type == "ma_pullback":
                _ma_rule = dict(_er)
                if int(card.get("card_id") or 0) == 303:
                    _ma_rule.setdefault("opening_fast_wave_bypass_ma_warmup", True)
                    _ma_rule.setdefault("opening_fast_wave_min_bars", _OPENING_FAST_WAVE_MIN_BARS)
                _ma_ok, _ma_metrics = self._evaluate_ma_pullback(stock_code, float(tick[2] or 0), _ma_rule)
                metrics.update(_ma_metrics)
                metrics["ma_support_status"] = _ma_metrics.get("ma_status")
                if int(card.get("card_id") or 0) == 303 or _as_bool(_er.get("require_1min_wave"), False):
                    _wave_rule = dict(_er)
                    if int(card.get("card_id") or 0) == 303:
                        _wave_rule.setdefault("wave_min_pullback_pct", 0.8)
                        _wave_rule.setdefault("wave_require_volume_contraction", True)
                        _wave_rule.setdefault("wave_max_volume_contraction_ratio", 0.85)
                        _wave_rule.setdefault("wave_require_rebound_candle", True)
                        _wave_rule.setdefault("wave_require_w2_low_confirmed", True)
                        _wave_rule.setdefault("wave_w2_low_confirm_bars", 1)
                        _wave_rule.setdefault("wave_mtf_gate_enabled", True)
                        _wave_rule.setdefault("wave_mtf_min_bullish_count", 3)
                        _wave_rule.setdefault("opening_fast_wave_enabled", True)
                        _wave_rule.setdefault("opening_fast_wave_min_bars", _OPENING_FAST_WAVE_MIN_BARS)
                        _wave_rule.setdefault("opening_fast_wave_lookback_bars", 8)
                        _wave_rule.setdefault("opening_fast_wave_min_pullback_pct", 0.25)
                        _wave_rule.setdefault("opening_fast_wave_w2_low_confirm_bars", 0)
                        _wave_rule.setdefault("opening_fast_wave_regular_end", _OPENING_FAST_WAVE_REGULAR_END)
                        _wave_rule.setdefault("opening_fast_wave_mtf_min_upper_bullish", 1)
                        metrics["trigger_tactics"] = card.get("trigger_tactics") or []
                        metrics["entry_reason_policy"] = "wave1_pullback_to_wave2_rebound"
                        metrics["tp_policy"] = "wave2_peak_target"
                        metrics["sl_policy"] = "pullback_low_break"
                        metrics["tp_sl_fallback"] = "fixed_3pct_tp_1.5pct_sl"
                        if not _ma_ok:
                            metrics["ma_pullback_deferred_to_wave_gate"] = True
                            metrics["ma_pullback_deferred_reason"] = _ma_metrics.get("ma_status")
                    _wave_ok, _wave_metrics = self._evaluate_1min_wave_pullback(
                        stock_code, float(tick[2] or 0), _wave_rule,
                    )
                    metrics.update(_wave_metrics)
                    if not _wave_ok:
                        _wave_status = str(_wave_metrics.get("wave_status") or "")
                        if _wave_status in _WAVE_DATA_RECOVERY_STATUSES:
                            self._trigger_wave_data_recovery(stock_code, card, _wave_status, metrics)
                        return (
                            None,
                            "one_minute_wave_pullback_failed",
                            f"1분봉 파동 눌림 조건 미충족({_wave_metrics.get('wave_status', '')})",
                            metrics,
                        )
                    if int(card.get("card_id") or 0) == 303 and not _ma_ok:
                        metrics["ma_status"] = "wave_gate_override"
                        metrics["ma_support_status"] = "wave_gate_override"
                        metrics["ma_override_scope"] = "card303_wave_pullback_confirmed"
                    elif not _ma_ok:
                        return None, "ma_pullback_failed", f"MA 눌림목 조건 미충족({_ma_metrics.get('ma_status', '')})", metrics
                elif not _ma_ok:
                    return None, "ma_pullback_failed", f"MA 눌림목 조건 미충족({_ma_metrics.get('ma_status', '')})", metrics
                if int(card.get("card_id") or 0) == 307 or _as_bool(_er.get("require_ma_wave"), False):
                    _mw_ok, _mw_metrics = self._evaluate_ma_wave_entry(
                        stock_code, float(tick[2] or 0), _er,
                    )
                    metrics.update(_mw_metrics)
                    if not _mw_ok:
                        _mw_status = str(_mw_metrics.get("ma_wave_status") or "")
                        if _mw_status in _WAVE_DATA_RECOVERY_STATUSES:
                            self._trigger_wave_data_recovery(stock_code, card, _mw_status, metrics)
                        return (
                            None,
                            "ma_wave_entry_failed",
                            f"이평선 파동 진입 조건 미충족({_mw_metrics.get('ma_wave_status', '')})",
                            metrics,
                        )

        params = card.get("scalping_params", {})
        live_filters = card.get("live_only_filters", {})
        rule_params = _extract_scalping_entry_rule_params(card.get("entry_rules", []))

        # entry_rules 기준 최우선, live_only_filters/scalping_params는 fallback.
        volume_multiplier = float(
            rule_params.get(
                "volume_multiplier",
                custom.get("volume_surge_ratio", params.get("volume_multiplier", 3.0)),
            )
        )
        strength_threshold = float(
            rule_params.get(
                "strength_threshold",
                live_filters.get("trade_strength_min", params.get("strength_threshold", 120)),
            )
        )
        min_momentum_ticks = int(rule_params.get("min_momentum_ticks", params.get("min_momentum_ticks", 3)))
        volume_lookback_ticks = int(rule_params.get("volume_lookback_ticks", len(history)))

        recent_volumes = [abs(float(t[3] or 0)) for t in list(history)[-volume_lookback_ticks:]]
        avg_vol = sum(recent_volumes) / len(recent_volumes) if recent_volumes else 1
        metrics["avg_tick_volume"] = avg_vol
        metrics["volume_lookback_ticks"] = volume_lookback_ticks
        metrics["required_strength"] = strength_threshold
        metrics["required_volume_multiplier"] = volume_multiplier

        # VWAP 체크: entry_rules에 vwap_above가 있으면 가격 > VWAP 필수
        if custom.get("vwap_above"):
            _vd = self._vwap_data.get(stock_code)
            if _vd and _vd["cum_vol"] > 0:
                vwap = _vd["cum_pv"] / _vd["cum_vol"]
                metrics["vwap"] = round(vwap, 1)
                if price <= vwap:
                    return None, "vwap_below", f"현재가({price:.0f}) ≤ VWAP({vwap:.0f})", metrics

        # EMA 정배열 체크: entry_rules에 ema_alignment가 있으면 단기 > 중기 > 장기 필수
        ema_cfg = custom.get("ema_alignment", {})
        if ema_cfg and ema_cfg.get("direction") == "bullish":
            ema_periods = ema_cfg.get("periods", [])
            if ema_periods and len(history) >= max(ema_periods):
                prices_all = [float(t[2] or 0) for t in list(history)]
                emas = {}
                for p in sorted(ema_periods):
                    if len(prices_all) >= p:
                        emas[p] = sum(prices_all[-p:]) / p
                metrics["ema_values"] = emas
                sorted_p = sorted(ema_periods)
                if len(emas) == len(sorted_p) and len(sorted_p) >= 2:
                    is_aligned = all(emas[sorted_p[i]] > emas[sorted_p[i + 1]] for i in range(len(sorted_p) - 1))
                    if not is_aligned:
                        return None, "ema_alignment_failed", "EMA 정배열 미충족", metrics

        if strength <= 0:
            card_id_int = int(card.get("card_id") or 0)
            wave_pullback_ok = metrics.get("wave_status") == "wave_pullback_ok"
            allow_strength_proxy = (
                card_id_int == 303
                and wave_pullback_ok
                and signed_volume > 0
                and volume > 0
            )
            if allow_strength_proxy:
                metrics["strength_missing_or_zero"] = True
                metrics["strength_proxy_from_buy_tick"] = True
                metrics["strength_proxy_scope"] = "card303_wave_pullback_buy_tick_only"
                strength = strength_threshold
                metrics["strength"] = strength
            else:
                metrics["strength_missing_or_zero"] = True
                return None, "strength_missing_or_zero", "체결강도 데이터 없음/0 — 실매매 진입 차단", metrics
        if strength < strength_threshold:
            return None, "strength_threshold_failed", "체결강도 기준 미충족", metrics
        if avg_vol <= 0 or volume <= 0:
            metrics["volume_invalid"] = True
            return None, "volume_invalid", "틱 거래량 데이터 없음/음수 — 실매매 진입 차단", metrics
        if volume < avg_vol * volume_multiplier:
            return None, "volume_spike_failed", "틱 거래량 급증 기준 미충족", metrics

        session_high = self._session_high.get(stock_code, 0)
        if not card.get("is_overnight") and price <= session_high:
            if int(card.get("card_id") or 0) == 303 and metrics.get("wave_status") == "wave_pullback_ok":
                metrics["session_breakout_bypassed_by_1min_wave"] = True
            else:
                return None, "breakout_failed", "세션 고가 돌파 실패", metrics

        recent_prices = [float(t[2] or 0) for t in list(history)[-min_momentum_ticks:]]
        if len(recent_prices) >= min_momentum_ticks:
            is_rising = all(recent_prices[i] < recent_prices[i + 1] for i in range(len(recent_prices) - 1))
            if not is_rising:
                return None, "momentum_ticks_failed", "연속 상승틱 기준 미충족", metrics

        vwap_str = ""
        _vd2 = self._vwap_data.get(stock_code)
        if _vd2 and _vd2["cum_vol"] > 0:
            vwap_str = f",vwap={_vd2['cum_pv']/_vd2['cum_vol']:.0f}"
        wave_str = ""
        if metrics.get("wave_status") == "wave_pullback_ok":
            wave_str = (
                f",wave_peak={metrics.get('fixed_wave_peak')},"
                f"pull_low={metrics.get('pullback_low')}"
            )
        ma_wave_str = ""
        if metrics.get("ma_wave_status") == "wave2_pullback" and metrics.get("ma_wave_entry_signal"):
            ma_wave_str = f",ma_wave={metrics.get('ma_wave_status')},conf={metrics.get('ma_wave_confidence', 0):.2f}"
        reason = f"SCALP_ENTRY(str={strength},vol_x={volume/avg_vol:.1f},brk={price}>{session_high}{vwap_str}{wave_str}{ma_wave_str})"
        return reason, "entry_signal", "스캘핑 진입 조건 충족", metrics

    # ── 진입 조건 평가 ─────────────────────────────────────────────────

    def _evaluate_entry(self, stock_code: str, tick: tuple, card: dict) -> Optional[str]:
        """
        틱 데이터 기반 진입 조건 평가.
        반환: 진입 사유 문자열 (None이면 진입 안 함)
        """
        if _has_limit_up_entry_rules(card.get("entry_rules", [])):
            result = self._evaluate_limit_up_entry_with_audit(stock_code, tick, card)
            return result[0]

        price = tick[2]
        signed_volume = float(tick[3] or 0)
        volume = abs(signed_volume)
        strength = tick[6] if len(tick) > 6 else 0

        history = self._tick_history[stock_code]
        if signed_volume < 0:
            return None
        if len(history) < 5:
            return None

        for _er in card.get("entry_rules", []):
            if isinstance(_er, dict) and str(_er.get("type") or "").strip().lower() == "ma_pullback":
                _ma_ok, _ = self._evaluate_ma_pullback(stock_code, float(price), _er)
                if not _ma_ok:
                    return None

        params = card.get("scalping_params", {})
        rule_params = _extract_scalping_entry_rule_params(card.get("entry_rules", []))

        # 조건 1: 체결강도 체크 (기본 120 이상 = 매수세 우위)
        # strength=0은 WS 미수신(snapshot fallback) 데이터 부재이므로 실매매 진입 실패 처리.
        strength_threshold = rule_params.get("strength_threshold", params.get("strength_threshold", 120))
        if strength <= 0:
            return None
        if strength < strength_threshold:
            return None

        # 조건 2: 거래량 스파이크 (최근 틱 평균 대비 X배)
        volume_multiplier = rule_params.get("volume_multiplier", params.get("volume_multiplier", 3.0))
        volume_lookback_ticks = int(rule_params.get("volume_lookback_ticks", len(history)))
        recent_volumes = [abs(float(t[3] or 0)) for t in list(history)[-volume_lookback_ticks:]]
        avg_vol = sum(recent_volumes) / len(recent_volumes) if recent_volumes else 1
        if avg_vol <= 0 or volume <= 0:
            return None
        if volume < avg_vol * volume_multiplier:
            return None

        # 조건 3: 가격 돌파 (세션 고가 대비) — overnight 카드는 SKIP
        session_high = self._session_high.get(stock_code, 0)
        if not card.get("is_overnight") and price <= session_high:
            return None

        # 조건 4: 연속 상승 모멘텀 (최근 3틱 이상 연속 상승)
        min_momentum_ticks = params.get("min_momentum_ticks", 3)
        recent_prices = [t[2] for t in list(history)[-min_momentum_ticks:]]
        if len(recent_prices) >= min_momentum_ticks:
            is_rising = all(
                recent_prices[i] < recent_prices[i+1]
                for i in range(len(recent_prices) - 1)
            )
            if not is_rising:
                return None

        reason = (
            f"SCALP_ENTRY(str={strength},vol_x={volume/avg_vol:.1f},"
            f"brk={price}>{session_high})"
        )
        return reason

    def _check_global_stock_open_position(self, account_id, stock_code: str) -> int:
        """Return today's OPEN position count for an account-stock pair."""
        conn = None
        cur = None
        try:
            conn = psycopg2.connect(**_get_db_params())
            cur = conn.cursor()
            cur.execute(
                "SELECT COUNT(*) FROM go100_positions "
                "WHERE account_id = %s AND stock_code = %s "
                "AND entry_date = CURRENT_DATE AND status = 'OPEN'",
                (account_id, stock_code),
            )
            row = cur.fetchone()
            return int(row[0]) if row else 0
        finally:
            if cur is not None:
                cur.close()
            if conn is not None:
                conn.close()

    # ── 매수 실행 ──────────────────────────────────────────────────────

    def _record_failed_buy_cooldown(self, stock_code: str, price: float) -> None:
        """Rate-limit every failed buy attempt, including pre-broker guards."""
        self._failed_cooldown[stock_code] = (time_module.monotonic(), float(price))
        self._failed_count[stock_code] = self._failed_count.get(stock_code, 0) + 1

    def _apply_wave_portfolio_gate(
        self,
        stock_code: str,
        price: float,
        qty: int,
        card: dict,
        metrics: dict,
    ) -> tuple[bool, int]:
        if not _wave_feature_enabled("GO100_WAVE_PORTFOLIO_GATE") or _WAVE_PORTFOLIO_RISK_MANAGER is None:
            return True, qty
        try:
            portfolio_id = int(card.get("portfolio_id") or 0)
            held_codes = sorted(getattr(self, "_card_held_stocks", {}).get(portfolio_id, set()))
            total_capital = float(
                card.get("allocated_amount")
                or card.get("initial_capital")
                or card.get("current_cash")
                or 0
            )
            current_cash = float(card.get("current_cash") or 0)
            invested_amount = max(0.0, total_capital - current_cash)
            amount_per_position = invested_amount / len(held_codes) if held_codes else 0.0
            universe_meta = getattr(self, "_universe_meta", {})
            current_positions = [
                {
                    "stock_code": held_code,
                    "amount": amount_per_position,
                    "sector": universe_meta.get(held_code, {}).get("sector"),
                }
                for held_code in held_codes
            ]

            card_id = int(card.get("card_id") or 0)
            daily_pnl = float(getattr(self, "_daily_pnl_by_card", {}).get(card_id, 0.0))
            daily_pnl_pct = daily_pnl / total_capital * 100.0 if total_capital > 0 else 0.0
            arrangement = str(metrics.get("ma_wave_arrangement") or "BULLISH").upper()
            trend = arrangement if arrangement in {"BULLISH", "BEARISH"} else "NEUTRAL"
            confidence = float(metrics.get("ma_wave_confidence") or metrics.get("wave_mtf_alignment_score") or 0)
            trend_strength = int(round(confidence * 100)) if 0 <= confidence <= 1 else int(round(confidence))
            result = _WAVE_PORTFOLIO_RISK_MANAGER.can_enter(
                new_stock_code=stock_code,
                new_amount=float(qty) * float(price),
                current_positions=current_positions,
                total_capital=total_capital,
                daily_pnl_pct=daily_pnl_pct,
                trend=trend,
                trend_strength=trend_strength,
                new_stock_sector=universe_meta.get(stock_code, {}).get("sector"),
            )
            metrics["wave_portfolio_risk"] = result.to_dict()
            metrics["wave_portfolio_daily_pnl_pct"] = round(daily_pnl_pct, 4)
            if not result.allowed:
                return False, 0

            adjusted_qty = min(qty, int(float(result.adjusted_amount or 0) / float(price)))
            if adjusted_qty <= 0:
                metrics["wave_portfolio_risk"]["reason"] = "조정 후 1주 미만"
                return False, 0
            if adjusted_qty < qty:
                metrics["wave_portfolio_original_qty"] = qty
                metrics["wave_portfolio_adjusted_qty"] = adjusted_qty
            return True, adjusted_qty
        except Exception as exc:
            metrics["wave_portfolio_error"] = str(exc)[:160]
            logger.warning(
                "GO100 wave portfolio gate error for %s; preserving existing quantity: %s",
                stock_code,
                exc,
            )
            return True, qty

    def _decide_wave_reorder(
        self,
        stock_code: str,
        entry_price: float,
        current_price: float,
        elapsed_seconds: float,
        metrics: dict,
    ):
        if not _wave_feature_enabled("GO100_WAVE_REORDER") or _WAVE_REORDER_LOGIC is None:
            return None
        try:
            wave_changed = False
            entry_wave_number = metrics.get("wave_number")
            if entry_wave_number is not None and _WAVE_COUNTER is not None:
                current_bars = self._get_minute_ohlc_series(stock_code)
                if current_bars:
                    current_wave = _WAVE_COUNTER.count(current_bars)
                    wave_changed = int(current_wave.wave_number) != int(entry_wave_number)
            decision = _WAVE_REORDER_LOGIC.decide(
                attempt=1,
                entry_price=entry_price,
                current_price=current_price,
                elapsed_seconds=elapsed_seconds,
                wave_changed=wave_changed,
            )
            metrics["wave_reorder_decision"] = decision.to_dict()
            metrics["wave_reorder_wave_changed"] = wave_changed
            return decision
        except Exception as exc:
            metrics["wave_reorder_error"] = str(exc)[:160]
            logger.warning(
                "GO100 wave reorder decision error for %s; preserving existing fill handling: %s",
                stock_code,
                exc,
            )
            return None

    async def _execute_buy(
        self, stock_code: str, price: int, card: dict, reason: str,
        entry_metrics: dict | None = None,
    ) -> bool:
        """즉시 시장가 매수 실행 → 포지션 DB 등록 → Redis로 ScalpingMonitor에 전달."""
        if int(card.get("card_id") or card.get("go100_card_id") or 0) == 119:
            metrics = dict(entry_metrics or {})
            metrics.update({
                "authority": "go100_live_engine",
                "blocked_process": "go100-kiwoom-scalping",
                "requested_reason": reason,
                "requested_price": price,
            })
            self._audit_decision(
                card=card,
                stock_code=stock_code,
                stage="safety_gate",
                decision="reject",
                reason_code="card119_buy_authority_live_engine_only",
                reason_text=(
                    "#119 신규 BUY는 본진 라이브엔진만 최종 판단합니다. "
                    "키움 스캘핑 러너는 #119 데이터 공급/모니터 역할로 제한합니다."
                ),
                metrics=metrics,
                throttle_seconds=0,
            )
            logger.warning(
                "CARD119_BUY_AUTHORITY_BLOCK: stock=%s card_id=%s price=%s reason=%s",
                stock_code, card.get("card_id") or card.get("go100_card_id"), price, reason,
            )
            return False

        # [GO100-P0 2026-08-21] Kiwoom/KIS 실계좌 신규 BUY 하드 차단 게이트.
        # 신규 fail-safe 값 GO100_SCALPING_REAL_BUY_BLOCK은 기본 true다.
        # 기존 GO100_KIWOOM_REAL_BUY_BLOCK=false 운영값이 남아 있어도 신규 BUY를 열지 않는다.
        # SELL/monitor/reconcile/data collection 경로는 이 게이트를 통과하지 않으므로 영향 없음.
        _kiwoom_real_buy_block_env = os.environ.get("GO100_SCALPING_REAL_BUY_BLOCK", "true").strip().lower()
        _card303_live_override = _is_card303_one_share_live_override(card)
        if _card303_live_override and _kiwoom_real_buy_block_env != "false":
            logger.warning(
                "CARD303_REAL_BUY_OVERRIDE: one-share live override active account_id=%s card_id=%s stock=%s env=%r",
                card.get("account_id"), card.get("card_id"), stock_code, _kiwoom_real_buy_block_env,
            )
        if _is_real_buy_hard_blocked(card):
            _block_reason_text = (
                f"GO100_SCALPING_REAL_BUY_BLOCK={_kiwoom_real_buy_block_env!r} (기본=true) — "
                f"실계좌 신규 BUY 하드 차단. "
                f"account_id={card.get('account_id')} card_id={card.get('card_id')} "
                f"broker_type={card.get('broker_type')} account_is_mock={card.get('account_is_mock')} "
                f"stock_code={stock_code}. "
                f"#303 1주 카나리 예외가 아니면 GO100_SCALPING_REAL_BUY_BLOCK=false 설정 필요."
            )
            logger.warning(
                "KIWOOM_REAL_BUY_HARD_BLOCK: account_id=%s card_id=%s stock=%s broker=%s mock=%s env=%r",
                card.get("account_id"), card.get("card_id"), stock_code,
                card.get("broker_type"), card.get("account_is_mock"), _kiwoom_real_buy_block_env,
            )
            self._audit_decision(
                card=card,
                stock_code=stock_code,
                stage="safety_gate",
                decision="reject",
                reason_code="kiwoom_real_buy_hard_block",
                reason_text=_block_reason_text,
                throttle_seconds=0,
            )
            return False

        # SAFETY: DB에서 카드 상태 재확인 — 캐시된 LIVE 상태가 stale일 수 있음
        try:
            _conn = psycopg2.connect(**_get_db_params())
            _cur = _conn.cursor()
            _cur.execute(
                "SELECT card_status, COALESCE(is_live, false) FROM go100_strategy_cards WHERE go100_card_id = %s",
                (card["card_id"],)
            )
            _row = _cur.fetchone()
            _cur.close()
            _conn.close()
            # P0 #303 2026-08-05: 실계좌(non-mock)는 card_status='LIVE' 전용
            _live_status = _row[0] if _row else None
            _is_live_flag = bool(_row[1]) if _row else False
            _is_real_account = not card.get("account_is_mock")
            _status_blocked = (
                not _row
                or _live_status not in ("LIVE", "PAPER_LIVE")
                or (_is_real_account and _live_status != "LIVE")
                or (_is_real_account and not _is_live_flag)
            )
            if _status_blocked:
                logger.warning(
                    "SAFETY BLOCK: card %d status=%s is_live=%s account_is_mock=%s → buy blocked",
                    card["card_id"], _live_status or "N/A", _is_live_flag, card.get("account_is_mock"),
                )
                self._audit_decision(
                    card=card, stock_code=stock_code, stage="safety_gate",
                    decision="reject", reason_code="card_not_live_safety",
                    reason_text=(
                        f"DB 실시간 확인: status={_live_status or 'N/A'} "
                        f"is_live={_is_live_flag} account_is_mock={card.get('account_is_mock')} "
                        f"→ 매수 차단"
                    ),
                    throttle_seconds=0,
                )
                return False
        except Exception as _e:
            logger.error("ScalpingEntry: card status safety check error: %s", _e)

        # [P0-DUPLOCK-R3] DB OPEN-position gate: runs on EVERY buy attempt before order
        # submission, regardless of Redis availability. A short Redis TTL cannot bypass this
        # gate. Runs before the Redis global lock so there is no lock to release on rejection.
        _pre_lock_acct_id = card.get("account_id")
        _pre_lock_card_id = card.get("card_id")
        _pre_lock_date = datetime.now().date().isoformat()
        _pre_lock_global_key = (
            f"scalping:buy_lock_global:{_pre_lock_acct_id}:{stock_code}:{_pre_lock_date}"
        )
        try:
            _pre_lock_open_cnt = self._check_global_stock_open_position(
                _pre_lock_acct_id, stock_code
            )
            if _pre_lock_open_cnt > 0:
                logger.warning(
                    "DUPBLOCK: DB OPEN-position gate blocked buy before Redis lock — "
                    "account_id=%s card_id=%s stock=%s date=%s open_count=%d",
                    _pre_lock_acct_id, _pre_lock_card_id, stock_code,
                    _pre_lock_date, _pre_lock_open_cnt,
                )
                self._audit_decision(
                    card=card,
                    stock_code=stock_code,
                    stage="safety_gate",
                    decision="reject",
                    reason_code="global_stock_open_position",
                    reason_text="same account already has an OPEN position for the same stock today",
                    metrics={
                        "account_id": _pre_lock_acct_id,
                        "card_id": _pre_lock_card_id,
                        "stock_code": stock_code,
                        "global_lock_key": _pre_lock_global_key,
                        "duplicate_scope": "account_stock_daily",
                    },
                    throttle_seconds=30,
                )
                return False
        except Exception as _pre_lock_dbe:
            logger.warning(
                "DUPBLOCK: pre-lock DB OPEN-position check failed (%s) — proceeding to Redis lock",
                _pre_lock_dbe,
            )

        # P0-DUPBLOCK: account-stock global + card-scoped cross-process buy guards.
        # Two strategy cards on the same account must not enter the same stock concurrently.
        # The global account-stock lock runs first; the existing card-scoped lock remains the
        # same-card idempotency barrier. Both guards cover KIWOOM/KIS BUY paths only.
        _dup_acct_id = card.get("account_id")
        _dup_card_id = card.get("card_id")
        _dup_date = datetime.now().date().isoformat()
        _global_lock_key, _dup_lock_key = _build_buy_lock_keys(
            _dup_acct_id, _dup_card_id, stock_code, _dup_date
        )
        _global_lock_token = uuid.uuid4().hex
        _global_lock_acquired = False
        try:
            _dup_r = sync_redis.from_url(
                os.environ.get("REDIS_URL", "redis://localhost:6379/0"),
                socket_connect_timeout=2,
                socket_timeout=2,
            )
            _global_lock_acquired = bool(
                _dup_r.set(_global_lock_key, _global_lock_token, nx=True, ex=120)
            )
            if not _global_lock_acquired:
                logger.warning(
                    "DUPBLOCK: account-stock global buy lock held — buy skipped "
                    "account_id=%s card_id=%s stock=%s date=%s key=%s",
                    _dup_acct_id, _dup_card_id, stock_code, _dup_date, _global_lock_key,
                )
                self._audit_decision(
                    card=card,
                    stock_code=stock_code,
                    stage="safety_gate",
                    decision="reject",
                    reason_code="global_stock_dup_lock",
                    reason_text="same account already has/pending same stock via another strategy card",
                    metrics={
                        "account_id": _dup_acct_id,
                        "card_id": _dup_card_id,
                        "stock_code": stock_code,
                        "global_lock_key": _global_lock_key,
                        "duplicate_scope": "account_stock_daily",
                    },
                    throttle_seconds=10,
                )
                return False

            _dup_acquired = bool(_dup_r.set(_dup_lock_key, "1", nx=True, ex=120))
            if not _dup_acquired:
                _release_redis_lock_if_owned(_dup_r, _global_lock_key, _global_lock_token)
                _global_lock_acquired = False
                logger.warning(
                    "DUPBLOCK: cross-process buy lock held — buy skipped "
                    "account_id=%s card_id=%s stock=%s date=%s key=%s",
                    _dup_acct_id, _dup_card_id, stock_code, _dup_date, _dup_lock_key,
                )
                self._audit_decision(
                    card=card,
                    stock_code=stock_code,
                    stage="safety_gate",
                    decision="reject",
                    reason_code="cross_process_dup_lock",
                    reason_text="Redis NX lock held by another runner — buy slot already taken",
                    throttle_seconds=10,
                )
                return False
        except Exception as _dup_re:
            if _global_lock_acquired:
                _release_redis_lock_if_owned(_dup_r, _global_lock_key, _global_lock_token)
                _global_lock_acquired = False
            logger.warning(
                "DUPBLOCK: Redis unavailable (%s) — DB idempotency fallback "
                "account_id=%s card_id=%s stock=%s global_key=%s",
                _dup_re, _dup_acct_id, _dup_card_id, stock_code,
                _global_lock_key,
            )
            try:
                _dup_cnt = self._check_global_stock_open_position(_dup_acct_id, stock_code)
                if _dup_cnt > 0:
                    logger.warning(
                        "DUPBLOCK: DB global account-stock check found OPEN position — buy blocked "
                        "account_id=%s card_id=%s stock=%s global_key=%s",
                        _dup_acct_id, _dup_card_id, stock_code, _global_lock_key,
                    )
                    self._audit_decision(
                        card=card,
                        stock_code=stock_code,
                        stage="safety_gate",
                        decision="reject",
                        reason_code="global_stock_open_position",
                        reason_text="same account already has an OPEN position for the same stock today",
                        metrics={
                            "account_id": _dup_acct_id,
                            "card_id": _dup_card_id,
                            "stock_code": stock_code,
                            "global_lock_key": _global_lock_key,
                            "duplicate_scope": "account_stock_daily",
                        },
                        throttle_seconds=10,
                    )
                    return False
            except Exception as _dup_dbe:
                logger.error(
                    "DUPBLOCK: DB idempotency check also failed (%s) — proceeding with caution",
                    _dup_dbe,
                )

        # [2026-08-06 CEO 지시] card #303 당일 BUY 1건 제한(canary) 제거.
        # 동시 보유 종목 수 상한(max_stocks=5)과 DUPBLOCK/쿨다운으로 안전장치를 유지한다.

        config_id = card["config_id"]
        portfolio_id = card["portfolio_id"]
        account_id = card["account_id"]
        current_cash = card["current_cash"]

        # 포지션 사이징: 카드 실전 설정의 종목당 금액과 포트폴리오 매수가능액을 동시에 지킨다.
        params = card.get("scalping_params", {})
        _max_stocks = int(card.get("max_stocks") or SCALPING_DEFAULT_MAX_STOCKS)
        _default_alloc = round(1.0 / _max_stocks, 4) if _max_stocks > 0 else 0.10
        alloc_pct = params.get("position_alloc_pct", _default_alloc)
        fixed_budget = float(card.get("per_position_amount") or 0)
        available_for_buy = float(card.get("available_for_buy") or 0)

        # fixed_quantity=N 모드: 가격/예산 기반 수량 계산을 우선순위 최상위로 덮어씀.
        # CEO 지시(2026-08-06): #119는 신규 BUY 시 종목당 정확히 1주.
        _card_fixed_qty = int(card.get("fixed_quantity") or 0)
        _card_sizing_mode = str(card.get("position_sizing_mode") or "").strip().lower()
        if _card_sizing_mode == "fixed_quantity" and _card_fixed_qty > 0:
            qty = _card_fixed_qty
            estimated_min_cost = qty * price
            # Mock accounts have no production broker cash endpoint, so retain
            # the portfolio snapshot safety check there. For live accounts the
            # broker-specific guard below compares one-share cost with current
            # broker cash; per_position_amount is never a fixed-quantity gate.
            _snapshot_cash_values = [
                value for value in (current_cash, available_for_buy) if value > 0
            ]
            _snapshot_cash = min(_snapshot_cash_values) if _snapshot_cash_values else None
            _defer_fixed_cash_gate = _defer_fixed_quantity_cash_gate(card)
            _legacy_fixed_cash_block = (
                not _defer_fixed_cash_gate
                and (
                    estimated_min_cost > current_cash
                    or estimated_min_cost > (
                        available_for_buy if available_for_buy > 0 else current_cash
                    )
                )
            )
            _card126_mock_cash_block = (
                _defer_fixed_cash_gate
                and bool(card.get("account_is_mock"))
                and _snapshot_cash is not None
                and estimated_min_cost > _snapshot_cash
            )
            if _legacy_fixed_cash_block or _card126_mock_cash_block:
                self._audit_decision(
                    card=card, stock_code=stock_code, stage="capital_guard",
                    decision="reject", reason_code="fixed_quantity_cost_exceeds_cash",
                    reason_text=(
                        f"fixed_quantity={qty} 주문 예상금액({estimated_min_cost:.0f})이 "
                        f"가용현금({_snapshot_cash if _snapshot_cash is not None else current_cash:.0f}) 초과"
                    ),
                    metrics={
                        "price": price, "fixed_quantity": qty,
                        "estimated_cost": estimated_min_cost,
                        "portfolio_cash": current_cash,
                        "available_for_buy": available_for_buy,
                        "cash_source": (
                            "portfolio_snapshot_mock_account"
                            if _defer_fixed_cash_gate
                            else "portfolio_snapshot_legacy_card_gate"
                        ),
                    },
                    throttle_seconds=60,
                )
                return False
            budget = estimated_min_cost
            logger.info(
                "ScalpingEntry fixed_quantity=%d 모드 적용 card=%s stock=%s price=%d cost=%.0f "
                "(broker cash guard deferred; portfolio_cash=%.0f available_for_buy=%.0f)",
                qty, card.get("card_id"), stock_code, price, estimated_min_cost,
                current_cash, available_for_buy,
            )
        elif _card_sizing_mode in ("risk_based", "risk_unit"):
            # [2026-08-19 P1/D3] R 기반 사이징: 갭 리스크를 손절폭이 아닌 수량으로 관리.
            _equity = float(
                card.get("allocated_amount")
                or card.get("initial_capital")
                or current_cash
                or 0
            )
            _risk_pct = float(card.get("risk_per_trade_pct") or RISK_UNIT_DEFAULT_RISK_PCT)
            _gap_pct = float(card.get("assumed_gap_loss_pct") or RISK_UNIT_DEFAULT_GAP_LOSS_PCT)
            _cash_cap = available_for_buy if available_for_buy > 0 else current_cash
            if fixed_budget > 0:
                _cash_cap = min(_cash_cap, fixed_budget)
            budget = _cash_cap
            qty = calc_risk_based_qty(
                equity=_equity,
                price=price,
                risk_per_trade_pct=_risk_pct,
                assumed_gap_loss_pct=_gap_pct,
                cash_cap=_cash_cap,
            )
            if qty <= 0:
                self._audit_decision(
                    card=card, stock_code=stock_code, stage="capital_guard",
                    decision="reject", reason_code="risk_unit_budget_insufficient",
                    reason_text=(
                        f"R기반 사이징 수량 0 (equity={_equity:.0f}, risk={_risk_pct}%, "
                        f"gap={_gap_pct}%, price={price}, cash_cap={_cash_cap:.0f})"
                    ),
                    metrics={
                        "equity": _equity, "risk_per_trade_pct": _risk_pct,
                        "assumed_gap_loss_pct": _gap_pct, "price": price,
                        "cash_cap": _cash_cap,
                    },
                    throttle_seconds=60,
                )
                return False
            logger.info(
                "ScalpingEntry risk_based 사이징 적용 card=%s stock=%s price=%d qty=%d "
                "(equity=%.0f risk=%.2f%% gap=%.1f%%)",
                card.get("card_id"), stock_code, price, qty, _equity, _risk_pct, _gap_pct,
            )
        else:
            fallback_budget = current_cash * alloc_pct
            budget = fixed_budget if fixed_budget > 0 else fallback_budget
            if available_for_buy > 0:
                budget = min(budget, available_for_buy)
            budget = min(budget, current_cash)
            qty = int(budget / price) if price > 0 else 0
        _canary_max_qty = int((_json_dict(card.get("strategy_params")) or {}).get("canary_max_qty") or 0)
        if _canary_max_qty > 0 and not bool(card.get("account_is_mock")):
            if qty > _canary_max_qty:
                logger.warning(
                    "CANARY: card %s real-account buy qty clamped %s->%d stock=%s price=%s budget=%.0f",
                    card.get("card_id"), qty, _canary_max_qty, stock_code, price, budget,
                )
            qty = min(qty, _canary_max_qty)

        if qty <= 0:
            logger.debug(
                "ScalpingEntry: qty=0 for %s (cash=%.0f, available=%.0f, fixed=%.0f, price=%d)",
                stock_code, current_cash, available_for_buy, fixed_budget, price,
            )
            self._audit_decision(
                card=card,
                stock_code=stock_code,
                stage="capital_guard",
                decision="reject",
                reason_code="portfolio_budget_insufficient",
                reason_text="카드 포트폴리오 예산 또는 종목당 한도로 1주 매수 수량 산정 불가",
                metrics={
                    "price": price,
                    "portfolio_cash": current_cash,
                    "available_for_buy": available_for_buy,
                    "per_position_amount": fixed_budget,
                    "competition_sequence": card.get("competition_sequence"),
                    "live_priority": card.get("live_priority"),
                    "competition_policy": card.get("competition_policy"),
                },
                throttle_seconds=300,
            )
            return False

        buy_exchange = "KRX"
        buy_order_type = "market"
        buy_order_price = 0
        nxt_session = _current_nxt_session()
        if nxt_session:
            if not _card_allows_nxt_session(card):
                _nxt_policy = _nxt_policy_metadata(card)
                self._audit_decision(
                    card=card,
                    stock_code=stock_code,
                    stage="buy_guard",
                    decision="reject",
                    reason_code="nxt_card_not_enabled",
                    reason_text=(
                        f"{nxt_session} 세션 신규진입 정책상 명시적 허용이 없어 주문 차단 "
                        f"({_nxt_policy['nxt_policy']})"
                    ),
                    metrics={"nxt_session": nxt_session, **_nxt_policy},
                    throttle_seconds=60,
                )
                return False
            if not bool(self._universe_meta.get(stock_code, {}).get("is_nxt")):
                self._audit_decision(
                    card=card,
                    stock_code=stock_code,
                    stage="buy_guard",
                    decision="reject",
                    reason_code="nxt_not_eligible",
                    reason_text="NXT 거래 가능 종목이 아니므로 오전장 주문 차단",
                    metrics={"nxt_session": nxt_session},
                    throttle_seconds=60,
                )
                return False
            buy_exchange = "NXT"
            buy_order_type = "limit"
            buy_order_price = price

            # NXT is limit-order based.  Do not submit a signal whose source
            # tick is already stale; this prevents a delayed/replayed quote
            # from becoming a broker-side price-band rejection.  Direct unit
            # callers without entry metrics retain the existing safety gates;
            # the live candidate path always supplies _tick_metrics().
            if entry_metrics is not None:
                _tick_age = entry_metrics.get("tick_age_seconds")
                try:
                    _tick_age = float(_tick_age)
                except (TypeError, ValueError):
                    _tick_age = None
                if _tick_age is None or _tick_age > _NXT_ORDER_MAX_TICK_AGE_SEC:
                    self._audit_decision(
                        card=card,
                        stock_code=stock_code,
                        stage="buy_guard",
                        decision="reject",
                        reason_code="nxt_tick_stale",
                        reason_text=(
                            "NXT 지정가 주문 원천 틱이 없거나 오래되어 주문 미제출 "
                            f"(age={_tick_age!r}s, limit={_NXT_ORDER_MAX_TICK_AGE_SEC:.0f}s)"
                        ),
                        metrics={
                            "nxt_session": nxt_session,
                            "tick_age_seconds": _tick_age,
                            "max_tick_age_seconds": _NXT_ORDER_MAX_TICK_AGE_SEC,
                            "price": price,
                        },
                        throttle_seconds=60,
                    )
                    return False

        if (
            card.get("broker_type") == "KIWOOM"
            and int(card.get("card_id") or 0) == 303
            and int(buy_order_price or 0) <= 0
        ):
            # Kiwoom returned 308003 when #303 sent a market buy with ord_unpr=0.
            # Keep #303 live test deterministic by submitting current-price limit orders.
            buy_order_type = "limit"
            buy_order_price = int(price)

        entry_metrics = entry_metrics or {}
        is_wave_entry = any(
            key in entry_metrics
            for key in ("wave_status", "ma_wave_status", "wave_number", "cycle_number")
        )
        if is_wave_entry:
            portfolio_allowed, portfolio_qty = self._apply_wave_portfolio_gate(
                stock_code,
                price,
                qty,
                card,
                entry_metrics,
            )
            if not portfolio_allowed:
                self._audit_decision(
                    card=card,
                    stock_code=stock_code,
                    stage="portfolio_risk_gate",
                    decision="reject",
                    reason_code="wave_portfolio_limit_blocked",
                    reason_text=str(
                        entry_metrics.get("wave_portfolio_risk", {}).get("reason")
                        or "포트폴리오 파동 리스크 한도 위반"
                    ),
                    metrics=entry_metrics,
                    throttle_seconds=60,
                )
                return False
            qty = portfolio_qty

        _order_started_at = time_module.monotonic()
        try:
            sys.path.insert(0, "/root/kis-autotrade-v4")

            if card.get("broker_type") == "KIWOOM":
                # ── KIWOOM 주문 경로: KiwoomBrokerClient 전용, V4OrderExecutor 사용 금지 ──
                # Root cause of GO100-303 bug: V4OrderExecutor calls KIS API endpoints
                # (openapivts.koreainvestment.com) even for KIWOOM accounts, causing
                # [90070000] 모의투자 처리계좌의 ID와 사용자정보 상이 error.
                from backend.app.services.data.kiwoom_credentials import _load_from_db as _kw_load_from_db
                from backend.app.core.broker_kiwoom_client import KiwoomBrokerClient
                from backend.app.core.broker_base import OrderRequest as BrokerOrderRequest

                _kw_creds = _kw_load_from_db(account_id)
                if not _kw_creds:
                    logger.error(
                        "KIWOOM ORDER ROUTING: credentials not found account_id=%s card_id=%s — buy blocked",
                        account_id, card.get("card_id"),
                    )
                    self._audit_decision(
                        card=card, stock_code=stock_code, stage="buy_execute",
                        decision="reject", reason_code="kiwoom_credentials_missing",
                        reason_text=f"KIWOOM 자격증명 없음 account_id={account_id}",
                        throttle_seconds=60,
                    )
                    return False

                _kw_client = KiwoomBrokerClient(
                    app_key=_kw_creds["app_key"],
                    secret_key=_kw_creds["secret_key"],
                    is_production=_kw_creds["is_production"],
                )
                await _kw_client.authenticate()

                # 잔고 확인: 실시간 증권사 잔고 조회 (미수매수 방지)
                _db_available = float(card.get("available_for_buy") or card.get("current_cash") or 0)
                available = _db_available
                try:
                    _kw_bal = await _kw_client.get_balance(_kw_creds["account_number"])
                    _broker_deposit = float(_kw_bal.deposit or 0)
                    if _broker_deposit > 0:
                        if _defer_fixed_quantity_cash_gate(card):
                            available = _broker_deposit
                            _cash_source = "broker_deposit_fixed_quantity_canary"
                        else:
                            available = min(_db_available, _broker_deposit) if _db_available > 0 else _broker_deposit
                            _cash_source = "min_db_available_and_broker_deposit"
                        logger.info(
                            "MARGIN_GUARD: KIWOOM %s db_avail=%.0f broker_deposit=%.0f effective=%.0f source=%s",
                            stock_code, _db_available, _broker_deposit, available, _cash_source,
                        )
                    else:
                        logger.warning(
                            "MARGIN_GUARD: KIWOOM broker deposit=0, using db_available=%.0f",
                            _db_available,
                        )
                except Exception as _mg_exc:
                    logger.warning("MARGIN_GUARD: KIWOOM balance check failed, using db_available=%.0f: %s", _db_available, _mg_exc)

                estimated_cost = qty * price * 1.00015
                if estimated_cost > available:
                    logger.warning(
                        "ScalpingEntry: 잔고 부족 %s cost=%.0f > available=%.0f",
                        stock_code, estimated_cost, available,
                    )
                    self._audit_decision(
                        card=card,
                        stock_code=stock_code,
                        stage="capital_guard",
                        decision="reject",
                        reason_code="broker_available_cash_insufficient",
                        reason_text="증권사 실계좌 가용 현금 부족으로 매수 주문 미제출",
                        metrics={
                            "price": price,
                            "qty": qty,
                            "estimated_cost": estimated_cost,
                            "broker_available_cash": available,
                            "portfolio_cash": current_cash,
                            "available_for_buy": available_for_buy,
                            "competition_sequence": card.get("competition_sequence"),
                            "live_priority": card.get("live_priority"),
                            "competition_policy": card.get("competition_policy"),
                        },
                        throttle_seconds=300,
                    )
                    return False

                _kw_req = BrokerOrderRequest(
                    account_number=_kw_creds["account_number"],
                    stock_code=stock_code,
                    order_qty=qty,
                    order_price=buy_order_price,
                    order_type=buy_order_type,
                    exchange=buy_exchange,
                )
                _kw_resp = await _kw_client.buy(_kw_req)
                result = {
                    "success": _kw_resp.success,
                    "order_no": _kw_resp.order_no,
                    "message": _kw_resp.message,
                }
                logger.info(
                    "KIWOOM BUY order_router=KIWOOM account_id=%s stock=%s qty=%d price=%d "
                    "exchange=%s order_type=%s success=%s order_no=%s",
                    account_id, stock_code, qty, price, buy_exchange, buy_order_type,
                    result["success"], result.get("order_no"),
                )
            else:
                # ── KIS 주문 경로: V4OrderExecutor (기존 동작 유지) ──
                # [GO100-303] 계좌 인증 오류 회로 차단기: 이미 차단된 (card, account)면 즉시 skip
                _ca_key = (card.get("card_id"), account_id)
                if _ca_key in self._account_auth_blocked:
                    return False

                from backend.app.services.trading.v4_order_executor import V4OrderExecutor

                if config_id in self._executor_cache:
                    executor = self._executor_cache[config_id]
                else:
                    executor = V4OrderExecutor(config_id=config_id, dry_run=False)
                    self._executor_cache[config_id] = executor

                available = float(await executor.get_available_cash(stock_code=stock_code))
                estimated_cost = qty * price * 1.00015
                if estimated_cost > available:
                    logger.warning(
                        "ScalpingEntry: 잔고 부족 %s cost=%.0f > available=%.0f",
                        stock_code, estimated_cost, available,
                    )
                    self._audit_decision(
                        card=card,
                        stock_code=stock_code,
                        stage="capital_guard",
                        decision="reject",
                        reason_code="broker_available_cash_insufficient",
                        reason_text="증권사 실계좌 가용 현금 부족으로 매수 주문 미제출",
                        metrics={
                            "price": price,
                            "qty": qty,
                            "estimated_cost": estimated_cost,
                            "broker_available_cash": available,
                            "portfolio_cash": current_cash,
                            "available_for_buy": available_for_buy,
                            "competition_sequence": card.get("competition_sequence"),
                            "live_priority": card.get("live_priority"),
                            "competition_policy": card.get("competition_policy"),
                        },
                        throttle_seconds=300,
                    )
                    return False

                result = await executor.place_buy_order(
                    stock_code=stock_code,
                    qty=qty,
                    price=buy_order_price if buy_exchange == "NXT" else price,
                    order_type="00" if buy_exchange == "NXT" else "01",
                    account_id=account_id,
                    card_id=card.get("card_id"),
                    user_id=card.get("user_id"),
                    exchange=buy_exchange,
                )

            if not result.get("success"):
                err_msg = result.get("message", "")
                logger.warning("ScalpingEntry: BUY 실패 %s: %s", stock_code, err_msg)

                # [GO100-303] 90070000 계좌 인증 오류: 연속 N회 발생 시 당일 차단
                if "90070000" in err_msg and card.get("broker_type", "").upper() != "KIWOOM":
                    _ca_key = (card.get("card_id"), account_id)
                    cnt = self._account_auth_error_count.get(_ca_key, 0) + 1
                    self._account_auth_error_count[_ca_key] = cnt
                    if cnt >= _ACCOUNT_AUTH_ERR_THRESHOLD and _ca_key not in self._account_auth_blocked:
                        self._account_auth_blocked.add(_ca_key)
                        logger.warning(
                            "ScalpingEntry [90070000] card=%s account=%s 계좌인증 오류 %d회 연속 "
                            "— 당일 종료까지 주문 차단 (프로세스 재시작 시 초기화)",
                            card.get("card_id"), account_id, cnt,
                        )

                self._record_failed_buy_cooldown(stock_code, price)
                self._audit_decision(
                    card=card,
                    stock_code=stock_code,
                    stage="buy_execute",
                    decision="reject",
                    reason_code="buy_failed_cooldown_set",
                    reason_text=f"매수 실패 → {self._FAIL_COOLDOWN_SEC:.0f}초 쿨다운 설정 (누적 {self._failed_count.get(stock_code, 0)}회)",
                    metrics={"message": result.get("message", "")},
                    throttle_seconds=300,
                )
                return False

            # [GO100-303 2026-08-24] 주문 직후 브로커별 체결조회로 실제 체결가 확인.
            # KIWOOM 주문은 KIWOOM 원장, KIS 주문은 KIS 원장으로 확인한다.
            # 확인 실패 시 포지션 감시는 유지하되 주문/동기화 상태는 PENDING_CONFIRM로 남긴다.
            _order_no = result.get("order_no")
            _confirmed_price = float(price)
            _requested_qty = qty
            _confirmed_qty = 0
            _fill_status = "FILLED"
            _fill_source = "broker_confirmed"
            _confirm = {"confirmed": False, "fill_price": 0, "fill_qty": 0, "message": "not_checked"}
            _confirm_error = False
            try:
                _broker_type = str(card.get("broker_type") or "").upper()
                if _broker_type == "KIWOOM":
                    _confirm = {"confirmed": False, "fill_price": 0, "fill_qty": 0, "message": "kiwoom no match"}
                    if _order_no:
                        for _attempt in range(3):
                            if _attempt > 0:
                                await asyncio.sleep(0.3)
                            _history = await _kw_client.get_order_history(
                                _kw_creds["account_number"],
                                exchange=buy_exchange,
                                stock_code=stock_code,
                                sell_tp="2",
                            )
                            if not isinstance(_history, list):
                                _history = []
                            for _row in _history:
                                _hist_order_no = str(_row.get("order_no") or "").strip()
                                _hist_code = (
                                    str(_row.get("stock_code") or "")
                                    .replace("_NX", "")
                                    .replace("_AL", "")
                                    .lstrip("A")
                                    .strip()
                                )
                                _filled_qty = int(_row.get("filled_qty") or _row.get("ccld_qty") or 0)
                                _fill_price = int(_row.get("fill_price") or _row.get("avg_prvs") or 0)
                                if _hist_order_no == str(_order_no) and _hist_code == stock_code and _filled_qty > 0 and _fill_price > 0:
                                    _confirm = {
                                        "confirmed": True,
                                        "fill_price": _fill_price,
                                        "fill_qty": _filled_qty,
                                        "message": f"kiwoom confirmed attempt={_attempt + 1}",
                                    }
                                    break
                            if _confirm["confirmed"]:
                                break
                else:
                    from backend.app.services.go100.kis_order_gateway import confirm_order_fill
                    _confirm = await confirm_order_fill(
                        user_id=card.get("user_id") or 1,
                        kis_order_no=_order_no or "",
                    )
                if _confirm["confirmed"] and _confirm["fill_price"] > 0:
                    _confirmed_price = float(_confirm["fill_price"])
                    _confirmed_qty = min(qty, int(_confirm.get("fill_qty") or 0))
                    if _confirmed_qty < qty:
                        _fill_status = "PENDING_CONFIRM"
                        _fill_source = "broker_confirmed_partial"
                    logger.info(
                        "ScalpingEntry: BUY fill confirmed %s broker=%s order_no=%s qty=%d/%d price=%d->%d",
                        stock_code, card.get("broker_type"), _order_no, _confirmed_qty, qty,
                        int(price), int(_confirmed_price),
                    )
                else:
                    _fill_status = "PENDING_CONFIRM"
                    _fill_source = "estimated_pending_broker_confirm"
                    logger.info(
                        "ScalpingEntry: BUY fill pending %s broker=%s order_no=%s est=%d msg=%s",
                        stock_code, card.get("broker_type"), _order_no, int(price), _confirm.get("message", ""),
                    )
            except Exception as _ce:
                _confirm_error = True
                _fill_status = "PENDING_CONFIRM"
                _fill_source = "estimated_confirm_error"
                logger.warning("ScalpingEntry: fill confirm error %s broker=%s: %s", stock_code, card.get("broker_type"), _ce)

            # W1 P0-3: 명시적으로 미체결/부분체결이 확인된 파동 주문만 취소 후 재주문한다.
            # 판정 또는 브로커 취소/재주문 중 예외가 나면 기존 PENDING_CONFIRM 처리로 복귀한다.
            _confirmation_skipped = str(_confirm.get("message") or "") == "skip"
            if is_wave_entry and _confirmed_qty < qty and not _confirmation_skipped and not _confirm_error:
                try:
                    latest_price = float(price)
                    tick_history = getattr(self, "_tick_history", {}).get(stock_code)
                    if tick_history:
                        latest_price = float(tick_history[-1][2] or price)
                    reorder_decision = self._decide_wave_reorder(
                        stock_code=stock_code,
                        entry_price=float(price),
                        current_price=latest_price,
                        elapsed_seconds=time_module.monotonic() - _order_started_at,
                        metrics=entry_metrics,
                    )
                    remaining_qty = max(0, qty - _confirmed_qty)
                    entry_metrics["wave_reorder_remaining_qty"] = remaining_qty
                    if reorder_decision is not None and remaining_qty > 0 and _order_no:
                        if _broker_type == "KIWOOM":
                            cancel_request = BrokerOrderRequest(
                                account_number=_kw_creds["account_number"],
                                stock_code=stock_code,
                                order_qty=remaining_qty,
                                order_price=0,
                                order_type="cancel",
                                original_order_no=str(_order_no),
                                exchange=buy_exchange,
                            )
                            cancel_response = await _kw_client.cancel_order(cancel_request)
                            cancel_succeeded = bool(cancel_response.success)
                            cancel_message = cancel_response.message
                        else:
                            cancel_response = await executor.cancel_order(
                                order_no=str(_order_no),
                                stock_code=stock_code,
                                qty=remaining_qty,
                                price=int(buy_order_price or price),
                                order_type="00" if buy_order_type == "limit" else "01",
                                exchange=buy_exchange,
                            )
                            cancel_succeeded = bool(cancel_response.get("success"))
                            cancel_message = str(cancel_response.get("message") or "")
                        entry_metrics.update({
                            "wave_reorder_cancel_succeeded": cancel_succeeded,
                            "wave_reorder_cancel_message": cancel_message,
                        })

                        if cancel_succeeded and reorder_decision.should_reorder:
                            reorder_price = int(latest_price)
                            reorder_order_type = "limit"
                            if reorder_decision.order_type == "limit_chase":
                                reorder_price += (
                                    _krx_tick_size(reorder_price)
                                    * int(reorder_decision.price_adjustment or 0)
                                )
                            elif reorder_decision.order_type == "market":
                                reorder_price = 0
                                reorder_order_type = "market"

                            if _broker_type == "KIWOOM":
                                reorder_request = BrokerOrderRequest(
                                    account_number=_kw_creds["account_number"],
                                    stock_code=stock_code,
                                    order_qty=remaining_qty,
                                    order_price=reorder_price,
                                    order_type=reorder_order_type,
                                    exchange=buy_exchange,
                                )
                                reorder_response = await _kw_client.buy(reorder_request)
                                reorder_result = {
                                    "success": reorder_response.success,
                                    "order_no": reorder_response.order_no,
                                    "message": reorder_response.message,
                                }
                            else:
                                reorder_result = await executor.place_buy_order(
                                    stock_code=stock_code,
                                    qty=remaining_qty,
                                    price=reorder_price,
                                    order_type="01" if reorder_order_type == "market" else "00",
                                    account_id=account_id,
                                    card_id=card.get("card_id"),
                                    user_id=card.get("user_id"),
                                    exchange=buy_exchange,
                                )
                            entry_metrics["wave_reorder_result"] = {
                                "success": bool(reorder_result.get("success")),
                                "order_no": reorder_result.get("order_no"),
                                "message": str(reorder_result.get("message") or ""),
                                "qty": remaining_qty,
                                "price": reorder_price,
                            }
                            if reorder_result.get("success"):
                                if _confirmed_qty == 0:
                                    _order_no = reorder_result.get("order_no")
                                    result["order_no"] = _order_no
                                _fill_status = "PENDING_CONFIRM"
                                _fill_source = "wave_reorder_pending_confirm"
                            elif _confirmed_qty <= 0:
                                self._record_failed_buy_cooldown(stock_code, price)
                                return False
                            else:
                                qty = _confirmed_qty
                                _fill_status = "FILLED"
                                _fill_source = "partial_fill_reorder_failed"
                        elif cancel_succeeded and not reorder_decision.should_reorder:
                            if _confirmed_qty <= 0:
                                self._record_failed_buy_cooldown(stock_code, price)
                                return False
                            qty = _confirmed_qty
                            _fill_status = "FILLED"
                            _fill_source = "partial_fill_remainder_cancelled"
                except Exception as reorder_exc:
                    entry_metrics["wave_reorder_error"] = str(reorder_exc)[:160]
                    logger.warning(
                        "GO100 wave reorder execution error for %s; preserving existing fill handling: %s",
                        stock_code,
                        reorder_exc,
                    )

            entry_metrics.update({
                "broker_fill_status": _fill_status,
                "broker_fill_source": _fill_source,
                "broker_confirmed_qty": _confirmed_qty,
                "broker_requested_qty": _requested_qty,
                "broker_position_qty": qty,
            })

            buy_order_id = self._db_record_buy_order(
                user_id=card.get("user_id"),
                account_id=account_id,
                card_id=card.get("card_id"),
                stock_code=stock_code,
                qty=qty,
                price=_confirmed_price,
                order_no=_order_no,
                status=_fill_status,
            )

            fill_price = _confirmed_price
            tp_pct = card.get("tp_pct", SCALPING_DEFAULT_TP_PCT)
            sl_pct = card.get("sl_pct", SCALPING_DEFAULT_SL_PCT)
            trailing_pct = card.get("trailing_pct")
            tp_price = fill_price * (1 + tp_pct)
            sl_price = fill_price * (1 - sl_pct)
            entry_metrics = entry_metrics or {}
            fixed_wave_peak = float(entry_metrics.get("fixed_wave_peak") or fill_price)
            pullback_low = float(entry_metrics.get("pullback_low") or 0)
            if int(card.get("card_id") or 0) == 303:
                entry_metrics["primary_stop_policy"] = "pullback_low_support"
                entry_metrics["primary_take_profit_policy"] = "wave2_high_or_exhaustion"
                entry_metrics["fallback_stop_policy"] = "fixed_stop_loss_pct_emergency_only"
                entry_metrics["fallback_take_profit_policy"] = "fixed_take_profit_pct_emergency_only"
                if pullback_low > 0:
                    sl_price = pullback_low
                    entry_metrics["stop_loss_price"] = round(sl_price, 1)
                    entry_metrics["stop_loss_source"] = "pullback_low"
                else:
                    entry_metrics["stop_loss_source"] = "fixed_pct_fallback"
                if fixed_wave_peak > fill_price:
                    tp_price = fixed_wave_peak
                    entry_metrics["take_profit_price"] = round(tp_price, 1)
                    entry_metrics["take_profit_source"] = "wave2_target_high"
                else:
                    entry_metrics["take_profit_source"] = "fixed_pct_fallback"

            # DB: go100_positions INSERT. 실패 시 청산 감시가 불가능하므로 즉시 실패 처리한다.
            position_id = self._db_open_position(
                portfolio_id=portfolio_id,
                user_id=card.get("user_id"),
                account_id=account_id,
                card_id=card.get("card_id"),
                stock_code=stock_code,
                qty=qty,
                entry_price=fill_price,
                stop_loss_price=sl_price,
                take_profit_price=tp_price,
                trailing_pct=trailing_pct,
                peak_price=fixed_wave_peak,
            )
            if position_id <= 0:
                logger.error("ScalpingEntry: position insert failed after BUY %s order_no=%s", stock_code, result.get("order_no"))
                return False
            self._db_link_order_position(result.get("order_no"), stock_code, position_id, account_id, card.get("card_id"))

            # DB: 현금 차감
            self._db_update_cash(portfolio_id, qty * fill_price * 1.00015)

            # DB: go100_trades BUY 기록
            self._db_insert_buy_trade(
                portfolio_id=portfolio_id,
                user_id=card.get("user_id"),
                account_id=account_id,
                card_id=card.get("card_id"),
                position_id=position_id,
                stock_code=stock_code,
                qty=qty,
                price=fill_price,
                order_id=buy_order_id,
            )
            wave_number = None
            cycle_number = None
            try:
                wave_bars = self._get_minute_ohlc_series(stock_code)
                min_wave_bars = int(getattr(_WAVE_COUNTER, "MIN_BARS", 3) or 3)
                if _WAVE_COUNTER is not None and len(wave_bars) >= min_wave_bars:
                    wc = _WAVE_COUNTER.count(wave_bars)
                    wave_number = wc.wave_number
                    cycle_number = wc.cycle_number
                    entry_metrics["wave_number"] = wave_number
                    entry_metrics["cycle_number"] = cycle_number
            except Exception as wave_count_exc:
                logger.warning(
                    "GO100 entry-fill wave count error for %s; using evaluated metrics: %s",
                    stock_code,
                    wave_count_exc,
                )
            if wave_number is None:
                wave_number = entry_metrics.get("wave_number")
                if wave_number is None:
                    wave_number = entry_metrics.get("ma_wave_number")
            if cycle_number is None:
                cycle_number = entry_metrics.get("cycle_number")
                if cycle_number is None:
                    cycle_number = entry_metrics.get("ma_wave_cycle_number")
            self._db_record_wave_decision(
                event="entry_fill",
                stock_code=stock_code,
                card_id=card.get("card_id"),
                user_id=card.get("user_id"),
                account_id=account_id,
                portfolio_id=portfolio_id,
                position_id=position_id,
                live_order_id=buy_order_id,
                order_no=result.get("order_no"),
                price=fill_price,
                qty=qty,
                metrics=entry_metrics,
                action="buy",
                wave_number=wave_number,
                cycle_number=cycle_number,
            )

            # v4_trade_executions 동기화
            self._db_sync_v4_trade(
                user_id=card.get("user_id"), account_id=account_id,
                card_id=card.get("card_id"), stock_code=stock_code,
                side='BUY', qty=qty, price=fill_price,
                order_no=result.get("order_no"), status=_fill_status,
            )

            # Redis: ScalpingMonitor에 새 포지션 알림. 실패청산에는 card_id/prev_close가 필수다.
            prev_close = float(self._universe_meta.get(stock_code, {}).get("close_price") or 0)
            self._push_to_scalping_monitor(
                stock_code=stock_code,
                position_id=position_id,
                quantity=qty,
                entry_price=fill_price,
                tp_pct=tp_pct,
                sl_pct=sl_pct,
                portfolio_id=portfolio_id,
                user_id=card.get("user_id"),
                account_id=account_id,
                config_id=config_id,
                broker_type=card.get("broker_type"),
                card_id=card.get("card_id"),
                entry_date=datetime.now().date().isoformat(),
                prev_close=prev_close,
                exit_rules=card.get("exit_rules"),
                fixed_wave_peak=fixed_wave_peak,
                previous_low=pullback_low,
                wave_context={
                    "timeframe": "1m",
                    "wave_status": entry_metrics.get("wave_status"),
                    "wave1_start": entry_metrics.get("wave1_start"),
                    "fixed_wave_peak": entry_metrics.get("fixed_wave_peak"),
                    "pullback_low": entry_metrics.get("pullback_low"),
                    "wave_gain_pct": entry_metrics.get("wave_gain_pct"),
                    "pullback_depth_pct": entry_metrics.get("pullback_depth_pct"),
                    "rebound_from_pullback_pct": entry_metrics.get("rebound_from_pullback_pct"),
                    "recent_high": entry_metrics.get("recent_high"),
                    "wave_segments": entry_metrics.get("wave_segments"),
                    "wave_current_phase": entry_metrics.get("wave_current_phase"),
                    "volume_contraction_ratio": entry_metrics.get("volume_contraction_ratio"),
                    "volume_contraction_status": entry_metrics.get("volume_contraction_status"),
                    "ma_support_status": entry_metrics.get("ma_support_status"),
                    "rebound_candle_confirmed": entry_metrics.get("rebound_candle_confirmed"),
                    "trigger_tactics": entry_metrics.get("trigger_tactics"),
                    "mtf_confirmation": entry_metrics.get("mtf_confirmation"),
                    "mtf_consensus": entry_metrics.get("mtf_consensus"),
                    "selected_timeframes": entry_metrics.get("selected_timeframes"),
                    "mtf_alignment_score": entry_metrics.get("mtf_alignment_score"),
                    "wave2_target_high": entry_metrics.get("wave2_target_high"),
                    "stop_loss_source": entry_metrics.get("stop_loss_source"),
                    "take_profit_source": entry_metrics.get("take_profit_source"),
                    "exit_policy": {
                        "primary": ["pullback_low_stop", "wave2_high_or_exhaustion"],
                        "fallback": ["fixed_take_profit_pct", "fixed_stop_loss_pct"],
                    },
                },
            )

            self._daily_buy_count += 1
            self._bought_today.add(stock_code)
            self._bought_today_ts[stock_code] = time_module.time()
            spent = qty * fill_price * 1.00015
            card["current_cash"] -= spent
            if float(card.get("available_for_buy") or 0) > 0:
                card["available_for_buy"] = max(0.0, float(card.get("available_for_buy") or 0) - spent)

            logger.info(
                "ScalpingEntry: BUY OK %s qty=%d price=%d exchange=%s order_type=%s reason=%s pos_id=%d",
                stock_code, qty, price, buy_exchange, buy_order_type, reason, position_id,
            )
            return True

        except Exception as e:
            logger.error("ScalpingEntry: _execute_buy 예외 %s: %s", stock_code, e, exc_info=True)
            self._audit_decision(
                card=card,
                stock_code=stock_code,
                stage="buy_execute",
                decision="reject",
                reason_code="buy_execute_exception",
                reason_text=f"_execute_buy 예외: {type(e).__name__}: {str(e)[:200]}",
                metrics={"price": price, "exception": str(e)[:500]},
                throttle_seconds=60,
            )
            return False

    def _db_count_open_positions(self, portfolio_id: int) -> int:
        """DB에서 포트폴리오의 실시간 OPEN 포지션 수를 조회 (동시성 안전 가드)."""
        try:
            conn = psycopg2.connect(**_get_db_params())
            cur = conn.cursor()
            cur.execute(
                "SELECT COUNT(*) FROM go100_positions WHERE portfolio_id = %s AND status = 'OPEN'",
                (portfolio_id,),
            )
            count = cur.fetchone()[0]
            cur.close()
            conn.close()
            return int(count)
        except Exception as e:
            logger.error("_db_count_open_positions error: %s", e)
            return self._card_positions.get(portfolio_id, 0)

    def _db_open_position(
        self, portfolio_id: int, user_id: int | None, account_id: int | None, card_id: int | None,
        stock_code: str, qty: int, entry_price: float,
        stop_loss_price: float, take_profit_price: float, trailing_pct: float | None,
        peak_price: float | None = None,
    ) -> int:
        """go100_positions INSERT, 반환: position_id."""
        try:
            conn = psycopg2.connect(**_get_db_params())
            cur = conn.cursor()
            cur.execute("""
                INSERT INTO go100_positions
                    (portfolio_id, user_id, account_id, go100_card_id,
                     stock_code, quantity, remaining_qty, entry_price, current_price,
                     stop_loss_price, take_profit_price, trailing_pct, peak_price,
                     entry_date, status, source, created_at, updated_at)
                VALUES (%s, %s, %s, %s,
                        %s, %s, %s, %s, %s,
                        %s, %s, %s, %s,
                        CURRENT_DATE, 'OPEN', 'SYSTEM', now(), now())
                RETURNING id
            """, (
                portfolio_id, user_id, account_id, card_id,
                stock_code, qty, qty, entry_price, entry_price,
                stop_loss_price, take_profit_price, trailing_pct, peak_price or entry_price,
            ))
            pos_id = cur.fetchone()[0]
            conn.commit()
            cur.close()
            conn.close()
            return int(pos_id)
        except Exception as e:
            logger.error("_db_open_position error: %s", e)
            return 0

    def _db_link_order_position(
        self, order_no: str | None, stock_code: str, position_id: int, account_id: int | None, card_id: int | None
    ) -> None:
        if not order_no or position_id <= 0:
            return
        try:
            conn = psycopg2.connect(**_get_db_params())
            cur = conn.cursor()
            cur.execute("""
                UPDATE v4_order_requests
                SET position_id = %s,
                    account_id = COALESCE(account_id, %s),
                    go100_card_id = COALESCE(go100_card_id, %s),
                    updated_at = now()
                WHERE order_no = %s
                  AND side = 'BUY'
                  AND ticker = %s
                  AND status = 'SUBMITTED'
            """, (position_id, account_id, card_id, order_no, stock_code))
            conn.commit()
            cur.close()
            conn.close()
        except Exception as e:
            logger.error("_db_link_order_position error: %s", e)

    def _db_record_buy_order(
        self, user_id, account_id, card_id, stock_code, qty, price, order_no,
        status: str = "FILLED",
    ) -> int:
        try:
            order_status = status if status in ("FILLED", "PENDING_CONFIRM") else "PENDING_CONFIRM"
            filled_at_expr = "now()" if order_status == "FILLED" else "NULL"
            conn = psycopg2.connect(**_get_db_params())
            cur = conn.cursor()
            cur.execute(f"""
                INSERT INTO go100_live_orders
                    (user_id, account_id, card_id, stock_code, stock_name,
                     order_type, side, quantity, order_price, filled_price,
                     filled_quantity, status, kis_order_id,
                     safety_check_passed, created_at, filled_at)
                SELECT %s, %s, %s, %s,
                       COALESCE(su.stock_name, %s),
                       'BUY', 'BUY', %s, %s, %s,
                       %s, %s, %s,
                       true, now(), {filled_at_expr}
                FROM (SELECT 1) dummy
                LEFT JOIN stock_universe su ON su.stock_code = %s
                RETURNING order_id
            """, (
                user_id, account_id, card_id, stock_code,
                stock_code,
                qty, int(price), int(price),
                qty, order_status, order_no or '',
                stock_code,
            ))
            if not order_no:
                logger.warning('ScalpingEntry: kis_order_id empty for BUY %s — broker did not return ODNO', stock_code)
            row = cur.fetchone()
            conn.commit()
            cur.close()
            conn.close()
            return int(row[0]) if row else 0
        except Exception as e:
            logger.error("_db_record_buy_order error: %s", e)
            return 0


    def _db_insert_buy_trade(
        self, portfolio_id: int, user_id, account_id, card_id,
        position_id: int, stock_code: str, qty: int, price: float,
        order_id: int,
    ) -> None:
        """go100_trades에 BUY 기록 INSERT."""
        try:
            conn = psycopg2.connect(**_get_db_params())
            cur = conn.cursor()
            amount = float(price) * int(qty)
            cur.execute("""
                INSERT INTO go100_trades
                (order_id, portfolio_id, user_id, account_id, go100_card_id,
                 position_id, stock_code, stock_name, side, price,
                 quantity, amount, pnl_amount, pnl_pct,
                 is_paper, trade_date, traded_at)
                SELECT %s, %s, %s, %s, %s,
                       %s, %s,
                       COALESCE(su.stock_name, %s),
                       'BUY', %s, %s, %s, NULL, NULL,
                       false, CURRENT_DATE, now()
                FROM (SELECT 1) dummy
                LEFT JOIN stock_universe su ON su.stock_code = %s
            """, (
                # go100_trades.order_id FK는 go100_orders(paper/risk 전용)를 참조한다.
                # 라이브 경로는 go100_live_orders에 기록하므로 그 id를 넣으면 FK 위반으로
                # 체결 이력 INSERT가 100% 실패한다. 추적은 position_id + go100_live_orders.kis_order_id로 한다.
                None, portfolio_id, user_id, account_id, card_id,
                position_id, stock_code,
                stock_code,
                float(price), int(qty), amount,
                stock_code,
            ))
            conn.commit()
            cur.close()
            conn.close()
        except Exception as e:
            logger.error("_db_insert_buy_trade error: %s", e)

    def _db_record_wave_decision(
        self,
        event: str,
        stock_code: str,
        card_id,
        user_id,
        account_id,
        portfolio_id: int,
        position_id: int,
        live_order_id: int | None,
        order_no: str | None,
        price: float,
        qty: int,
        metrics: dict | None,
        action: str,
        wave_number: int | None = None,
        cycle_number: int | None = None,
    ) -> None:
        """Persist live #303 wave context for post-trade review and ML labels."""
        metrics = metrics or {}
        try:
            def _optional_int(value) -> int | None:
                if value is None or value == "":
                    return None
                try:
                    return int(value)
                except (TypeError, ValueError):
                    return None

            metric_wave_number = metrics.get("wave_number")
            if metric_wave_number is None:
                metric_wave_number = metrics.get("ma_wave_number")
            metric_cycle_number = metrics.get("cycle_number")
            if metric_cycle_number is None:
                metric_cycle_number = metrics.get("ma_wave_cycle_number")
            resolved_wave_number = _optional_int(
                wave_number if wave_number is not None else metric_wave_number
            )
            resolved_cycle_number = _optional_int(
                cycle_number if cycle_number is not None else metric_cycle_number
            )
            features = {
                "source": "scalping_entry_engine",
                "event": event,
                "user_id": user_id,
                "account_id": account_id,
                "portfolio_id": portfolio_id,
                "position_id": position_id,
                "live_order_id": live_order_id,
                "order_no": order_no,
                "quantity": int(qty or 0),
                "entry_price": float(price or 0),
                "wave_number": resolved_wave_number,
                "cycle_number": resolved_cycle_number,
                "wave_context": {
                    "timeframe": "1m",
                    "wave_status": metrics.get("wave_status"),
                    "wave1_start": metrics.get("wave1_start"),
                    "fixed_wave_peak": metrics.get("fixed_wave_peak"),
                    "pullback_low": metrics.get("pullback_low"),
                    "wave_gain_pct": metrics.get("wave_gain_pct"),
                    "pullback_depth_pct": metrics.get("pullback_depth_pct"),
                    "rebound_from_pullback_pct": metrics.get("rebound_from_pullback_pct"),
                    "price_to_fixed_wave_peak_pct": metrics.get("price_to_fixed_wave_peak_pct"),
                    "recent_high": metrics.get("recent_high"),
                    "wave_segments": metrics.get("wave_segments"),
                    "wave_current_phase": metrics.get("wave_current_phase"),
                    "volume_contraction_ratio": metrics.get("volume_contraction_ratio"),
                    "volume_contraction_status": metrics.get("volume_contraction_status"),
                    "ma_support_status": metrics.get("ma_support_status"),
                    "rebound_candle_confirmed": metrics.get("rebound_candle_confirmed"),
                    "trigger_tactics": metrics.get("trigger_tactics"),
                    "mtf_confirmation": metrics.get("mtf_confirmation"),
                    "mtf_consensus": metrics.get("mtf_consensus"),
                },
                "raw_metrics": metrics,
            }
            conn = psycopg2.connect(**_get_db_params())
            cur = conn.cursor()
            wave_label = "wave2_pullback" if metrics.get("pullback_low") else str(metrics.get("wave_status") or event or "entry_fill")[:30]
            pullback_grade = "A" if str(metrics.get("wave_status") or "") == "wave_pullback_ok" else None
            cur.execute("""
                INSERT INTO go100_wave_decisions
                    (stock_code, decision_time, card_id, cycle_number, wave_number,
                     sub_wave, wave_label, ma_arrangement,
                     pullback_probability, pullback_grade,
                     peak_probability, peak_grade,
                     features, action, position_pct, price_at_decision)
                VALUES
                    (%s, now(), %s, %s, %s,
                     %s, %s, NULL,
                     NULL, %s,
                     NULL, NULL,
                     %s::jsonb, %s, NULL, %s)
            """, (
                stock_code,
                int(card_id or 0),
                resolved_cycle_number,
                resolved_wave_number,
                "2" if metrics.get("pullback_low") else "1",
                wave_label,
                pullback_grade,
                json.dumps(features, ensure_ascii=False),
                action,
                float(price or 0),
            ))
            conn.commit()
            cur.close()
            conn.close()
        except Exception as e:
            logger.warning("_db_record_wave_decision skipped %s: %s", stock_code, e)

    def _db_update_cash(self, portfolio_id: int, cost: float) -> None:
        """포트폴리오 현금 차감."""
        try:
            conn = psycopg2.connect(**_get_db_params())
            cur = conn.cursor()
            cur.execute("""
                UPDATE go100_portfolios
                SET current_cash = GREATEST(current_cash - %s, 0),
                    available_for_buy = GREATEST(COALESCE(available_for_buy, current_cash) - %s, 0),
                    total_invested = COALESCE(total_invested, 0) + %s,
                    updated_at = now()
                WHERE portfolio_id = %s
            """, (cost, cost, cost, portfolio_id))
            conn.commit()
            cur.close()
            conn.close()
        except Exception as e:
            logger.error("_db_update_cash error: %s", e)


    def _db_sync_v4_trade(self, user_id, account_id, card_id, stock_code, side, qty, price, order_no, status='FILLED'):
        """v4_trade_executions에 동기화 (대시보드 조회용)."""
        try:
            trade_status = status if status in ('FILLED', 'PENDING_CONFIRM') else 'PENDING_CONFIRM'
            executed_at_expr = 'now()' if trade_status == 'FILLED' else 'NULL'
            conn = psycopg2.connect(**_get_db_params())
            cur = conn.cursor()
            cur.execute(f"""
                INSERT INTO v4_trade_executions
                    (user_id, account_id, strategy_id, stock_code, stock_name,
                     order_type, order_method, quantity, price, executed_price,
                     executed_quantity, status, broker_type, broker_order_id,
                     created_at, executed_at, updated_at)
                SELECT %s, %s, %s, %s,
                       COALESCE(su.stock_name, %s),
                       %s, 'market', %s, %s, %s,
                       %s, %s, 'KIS', %s,
                       now(), {executed_at_expr}, now()
                FROM (SELECT 1) dummy
                LEFT JOIN stock_universe su ON su.stock_code = %s
                WHERE NOT EXISTS (
                    SELECT 1 FROM v4_trade_executions
                    WHERE broker_order_id = %s AND stock_code = %s AND order_type = %s
                )
            """, (
                user_id, account_id, card_id, stock_code, stock_code,
                side, qty, int(price), int(price), qty, trade_status, order_no or '',
                stock_code, order_no or '', stock_code, side,
            ))
            conn.commit()
            cur.close()
            conn.close()
        except Exception as e:
            logger.error("_db_sync_v4_trade error: %s", e)

    def _push_to_scalping_monitor(self, **pos_info) -> None:
        """Redis를 통해 ScalpingMonitor에 새 포지션 전달."""
        try:
            r = sync_redis.from_url(
                os.environ.get("REDIS_URL", "redis://localhost:6379/0"),
                decode_responses=True, socket_timeout=1.0,
            )
            r.rpush("go100:scalping:new_positions", json.dumps(pos_info))
        except Exception as e:
            logger.error("_push_to_scalping_monitor Redis error: %s", e)

    # ── Redis 조건 변경 수신 ──────────────────────────────────────────────

    def _check_config_changed(self) -> bool:
        """Redis pub/sub 대신 polling: config_changed 플래그 확인."""
        try:
            r = sync_redis.from_url(
                os.environ.get("REDIS_URL", "redis://localhost:6379/0"),
                decode_responses=True, socket_timeout=0.3,
            )
            flag = r.get("go100:realtime:config_reload_flag")
            if flag:
                r.delete("go100:realtime:config_reload_flag")
                return True
            # 수동 종목 반영
            manual = r.smembers("go100:realtime:manual_stocks")
            if manual:
                new_codes = manual - self._universe
                if new_codes:
                    self._universe.update(new_codes)
                    logger.info("ScalpingEntryEngine: +%d manual stocks added → total %d",
                                len(new_codes), len(self._universe))
                    return False
        except Exception:
            pass
        return False

    # ── 중앙 경쟁 엔진 ──────────────────────────────────────────────────

    def _competition_score_candidate(self, candidate: dict) -> float:
        """Score one card candidate for account-level capital/resource competition."""
        card = candidate["card"]
        metrics = candidate.get("metrics") or {}
        portfolio_id = int(card.get("portfolio_id") or 0)
        max_stocks = max(1, int(candidate.get("max_stocks") or card.get("max_stocks") or 1))
        open_count = int(self._card_positions.get(portfolio_id, candidate.get("open_count") or 0))
        slot_ratio = min(1.0, max(0.0, open_count / max_stocks))
        available_for_buy = float(card.get("available_for_buy") or card.get("current_cash") or 0)
        price = float(candidate.get("price") or 0)
        budget_ratio = min(1.0, available_for_buy / price) if price > 0 else 0.0

        lock_score = float(metrics.get("lock_score") or metrics.get("ma_wave_score") or 50.0)
        data_quality_bonus = 10.0 if str(metrics.get("data_quality_status") or "PASS") == "PASS" else 0.0
        scalp_score = float(self._universe_meta.get(candidate["stock_code"], {}).get("scalp_score") or 0)
        live_priority = int(card.get("live_priority") or 1000)
        priority_bonus = max(0.0, 20.0 - min(live_priority, 1000) / 50.0)
        capacity_bonus = (1.0 - slot_ratio) * 20.0
        budget_bonus = budget_ratio * 10.0

        score = (
            lock_score * 0.55
            + min(max(scalp_score, 0.0), 100.0) * 0.15
            + capacity_bonus
            + budget_bonus
            + data_quality_bonus
            + priority_bonus
        )
        if card.get("account_is_mock"):
            score += 2.0
        if str(card.get("card_status") or "").upper() == "LIVE":
            score += 3.0
        return round(max(0.0, score), 4)

    def _select_competition_candidate(self, candidates: list[dict]) -> tuple[dict | None, list[dict]]:
        """Select the candidate to execute and return all scored candidates.

        shadow mode preserves the legacy first-pass candidate as executor target, but
        records which candidate the canonical competition engine would have selected.
        """
        if not candidates:
            return None, []
        executable_candidates = [
            c for c in candidates
            if not _is_real_buy_hard_blocked(c.get("card") or {})
        ]
        candidate_pool = executable_candidates or candidates
        scored = []
        for seq, candidate in enumerate(candidate_pool, start=1):
            candidate["competition_candidate_seq"] = seq
            candidate["competition_score"] = self._competition_score_candidate(candidate)
            scored.append(candidate)
        best = max(
            scored,
            key=lambda c: (
                c.get("competition_score", 0.0),
                -int(c["card"].get("live_priority") or 1000),
                -int(c["card"].get("competition_sequence") or 999999),
            ),
        )
        canonical_card = best["card"]
        for candidate in scored:
            candidate["competition_canonical_selected_card_id"] = canonical_card.get("card_id")
            candidate["competition_canonical_selected_portfolio_id"] = canonical_card.get("portfolio_id")
            candidate["competition_canonical_score"] = best.get("competition_score")
            candidate["competition_is_canonical_winner"] = candidate is best
        if _COMPETITION_MODE == "enforce":
            if float(best.get("competition_score") or 0.0) < _COMPETITION_MIN_SCORE:
                return None, scored
            best["competition_execution_policy"] = "competition_enforced"
            return best, scored
        legacy_first = scored[0]
        legacy_first["competition_execution_policy"] = "legacy_first_signal_shadow"
        return legacy_first, scored

    async def _process_competition_candidate(
        self,
        *,
        candidate: dict,
        stock_code: str,
        price: int,
        scored_candidates: list[dict],
    ) -> bool:
        """Run final buy guards and execute the selected card candidate."""
        card = candidate["card"]
        reason = candidate["reason"]
        metrics = {
            **(candidate.get("metrics") or {}),
            "entry_reason": reason,
            "competition_mode": _COMPETITION_MODE,
            "competition_score": candidate.get("competition_score"),
            "competition_candidate_count": len(scored_candidates),
            "competition_execution_card_id": card.get("card_id"),
            "competition_execution_portfolio_id": card.get("portfolio_id"),
            "competition_canonical_selected_card_id": candidate.get("competition_canonical_selected_card_id"),
            "competition_canonical_selected_portfolio_id": candidate.get("competition_canonical_selected_portfolio_id"),
            "competition_canonical_score": candidate.get("competition_canonical_score"),
            "competition_is_canonical_winner": candidate.get("competition_is_canonical_winner"),
            "competition_execution_policy": candidate.get("competition_execution_policy"),
            "competition_sequence": card.get("competition_sequence"),
            "live_priority": card.get("live_priority"),
            "competition_policy": "GO100_ACCOUNT_RESOURCE_COMPETITION_V1",
        }
        portfolio_id = candidate["portfolio_id"]
        max_stocks = candidate["max_stocks"]
        open_count = candidate["open_count"]

        if _COMPETITION_LOG_LOSERS:
            ranked = sorted(scored_candidates, key=lambda c: c.get("competition_score", 0.0), reverse=True)
            for rank, other in enumerate(ranked, start=1):
                other_card = other["card"]
                won = other is candidate
                self._audit_decision(
                    card=other_card,
                    stock_code=stock_code,
                    stage="competition_engine",
                    decision="pass" if won else "skip",
                    reason_code="competition_selected" if won else "competition_lost",
                    reason_text=(
                        "실제 매수 실행 후보"
                        if won
                        else "같은 틱의 실행 후보에 계좌 자원 배정"
                    ),
                    metrics={
                        **(other.get("metrics") or {}),
                        "entry_reason": other.get("reason"),
                        "competition_mode": _COMPETITION_MODE,
                        "competition_score": other.get("competition_score"),
                        "competition_rank": rank,
                        "competition_candidate_count": len(scored_candidates),
                        "competition_execution_card_id": card.get("card_id"),
                        "competition_execution_portfolio_id": card.get("portfolio_id"),
                        "competition_canonical_selected_card_id": other.get("competition_canonical_selected_card_id"),
                        "competition_canonical_selected_portfolio_id": other.get("competition_canonical_selected_portfolio_id"),
                        "competition_canonical_score": other.get("competition_canonical_score"),
                        "competition_is_canonical_winner": other.get("competition_is_canonical_winner"),
                        "competition_execution_policy": candidate.get("competition_execution_policy"),
                        "competition_policy": "GO100_ACCOUNT_RESOURCE_COMPETITION_V1",
                    },
                    throttle_seconds=0,
                )

        lock = self._entry_locks.setdefault(stock_code, asyncio.Lock())
        if lock.locked():
            self._audit_decision(
                card=card,
                stock_code=stock_code,
                stage="buy_guard",
                decision="skip",
                reason_code="entry_lock_active",
                reason_text="동일 종목 매수 Lock 활성화",
                metrics=metrics,
            )
            return False

        async with lock:
            if stock_code in self._bought_today:
                buy_ts = self._bought_today_ts.get(stock_code, 0.0)
                elapsed = time_module.time() - buy_ts if buy_ts > 0 else float("inf")
                if elapsed < _REENTRY_COOLDOWN_SEC:
                    self._audit_decision(
                        card=card,
                        stock_code=stock_code,
                        stage="buy_guard",
                        decision="skip",
                        reason_code="reentry_cooldown",
                        reason_text=f"재진입 쿨다운 중 ({elapsed:.0f}s/{_REENTRY_COOLDOWN_SEC}s)",
                        metrics=metrics,
                    )
                    return False
                self._bought_today.discard(stock_code)
                self._bought_today_ts.pop(stock_code, None)

            cd = self._failed_cooldown.get(stock_code)
            if cd:
                elapsed = time_module.monotonic() - cd[0]
                price_chg = abs(price - cd[1]) / cd[1] if cd[1] > 0 else 0
                if elapsed < self._FAIL_COOLDOWN_SEC and price_chg < self._FAIL_PRICE_CHANGE_PCT:
                    self._audit_decision(
                        card=card,
                        stock_code=stock_code,
                        stage="buy_guard",
                        decision="skip",
                        reason_code="fail_cooldown",
                        reason_text=f"매수 실패 쿨다운 중 ({elapsed:.0f}s/{self._FAIL_COOLDOWN_SEC:.0f}s)",
                        metrics=metrics,
                    )
                    return False
                del self._failed_cooldown[stock_code]

            if self._failed_count.get(stock_code, 0) >= self._FAIL_MAX_DAILY:
                self._audit_decision(
                    card=card,
                    stock_code=stock_code,
                    stage="buy_guard",
                    decision="skip",
                    reason_code="fail_max_daily",
                    reason_text=f"동일종목 일일 실패 {self._FAIL_MAX_DAILY}회 초과 차단",
                    metrics=metrics,
                )
                return False

            latest_open_count = self._card_positions.get(portfolio_id, open_count)
            if latest_open_count >= max_stocks:
                self._audit_decision(
                    card=card,
                    stock_code=stock_code,
                    stage="buy_guard",
                    decision="skip",
                    reason_code="max_positions_rechecked",
                    reason_text="매수 직전 재확인에서 카드별 최대 보유 종목 수 도달",
                    metrics={**metrics, "open_count": latest_open_count, "max_stocks": max_stocks},
                    throttle_seconds=0,
                )
                return False

            db_open_count = self._db_count_open_positions(portfolio_id)
            if db_open_count >= max_stocks:
                self._card_positions[portfolio_id] = db_open_count
                self._audit_decision(
                    card=card,
                    stock_code=stock_code,
                    stage="buy_guard",
                    decision="skip",
                    reason_code="max_positions_db_check",
                    reason_text=f"DB 실시간 확인: OPEN 포지션 {db_open_count}개 >= max_stocks {max_stocks}",
                    metrics={**metrics, "db_open_count": db_open_count, "max_stocks": max_stocks},
                    throttle_seconds=0,
                )
                return False

            success = await self._execute_buy(stock_code, price, card, reason, metrics)
            if not success and stock_code not in self._failed_cooldown:
                # _execute_buy() records broker rejections itself.  This
                # covers every earlier False path (stale quote, credentials,
                # cash, or safety gate) so a failed candidate cannot loop at
                # tick speed.
                self._record_failed_buy_cooldown(stock_code, price)
            self._audit_decision(
                card=card,
                stock_code=stock_code,
                stage="buy_execute",
                decision="buy" if success else "reject",
                reason_code="buy_order_submitted" if success else "buy_order_failed",
                reason_text="매수 주문 제출 및 포지션 등록 완료" if success else "매수 조건 통과 후 주문/포지션 등록 실패",
                metrics=metrics,
                throttle_seconds=0,
            )
            if success:
                self._card_positions[portfolio_id] = self._card_positions.get(portfolio_id, open_count) + 1
                self._card_held_stocks.setdefault(portfolio_id, set()).add(stock_code)
            return success

    # ── 메인 루프 ──────────────────────────────────────────────────────

    async def run(self) -> None:
        """메인 루프: 틱 소비 → 카드별 조건 평가 → 즉시 매수."""
        # 최종 후보의 max_positions_db_check 차단과 성공 후
        # self._card_positions[portfolio_id] = current + 1 갱신은
        # _process_competition_candidate() 안에서 원자적으로 수행한다.
        self._running = True
        logger.info("ScalpingEntryEngine started")

        self.load_scalping_cards()
        self._load_overheated_stocks()
        self._load_universe()
        self._audit_limit_up_snapshot_candidates()
        self._load_open_positions_count()
        self._load_loss_cooldown_stocks()
        self._load_bought_today_from_db()

        while self._running:
            try:
                self._reset_daily_if_needed()

                if not self._cards:
                    await asyncio.sleep(10)
                    self.load_scalping_cards()
                    continue

                now_ts = asyncio.get_event_loop().time()

                # Redis 조건 변경 알림 확인 → 즉시 리로드
                if self._check_config_changed():
                    logger.info("ScalpingEntryEngine: config changed → reloading cards")
                    self.load_scalping_cards()
                    self._last_card_reload = now_ts

                # PAUSED/is_live changes must be picked up even when Redis reload events are missed.
                if now_ts - self._last_card_reload > _CARD_RELOAD_SEC:
                    self.load_scalping_cards()
                    self._last_card_reload = now_ts

                # 주기적 유니버스/포지션 갱신
                if now_ts - self._last_universe_load > _UNIVERSE_RELOAD_SEC:
                    self._load_universe()
                    self._last_universe_load = now_ts
                if now_ts - self._last_position_load > _POSITION_RELOAD_SEC:
                    self._load_open_positions_count()
                    self._last_position_load = now_ts
                if now_ts - self._last_loss_cooldown_load > _UNIVERSE_RELOAD_SEC:
                    self._load_loss_cooldown_stocks()
                    self._last_loss_cooldown_load = now_ts
                if now_ts - self._last_exclusion_load > _UNIVERSE_RELOAD_SEC:
                    self._load_manual_exclusions()
                    self._load_overheated_stocks()
                    self._audit_limit_up_snapshot_candidates()
                    self._last_exclusion_load = now_ts

                # [P2] 우선진입: 우선순위 버퍼에서 먼저 소비, 없으면 큐에서 제한 배치 드레인+정렬
                if self._priority_buffer:
                    tick = self._priority_buffer.pop(0)
                    self._priority_processed_since_yield += 1
                    if self._priority_processed_since_yield >= _PRIORITY_YIELD_EVERY_TICKS:
                        self._priority_processed_since_yield = 0
                        await asyncio.sleep(0)
                else:
                    self._priority_processed_since_yield = 0
                    try:
                        tick = await asyncio.wait_for(
                            self._queue.get(), timeout=_CONSUME_TIMEOUT_SEC
                        )
                    except asyncio.TimeoutError:
                        continue
                    for _ in range(max(0, _PRIORITY_DRAIN_LIMIT)):
                        if self._queue.empty():
                            break
                        try:
                            self._priority_buffer.append(self._queue.get_nowait())
                        except asyncio.QueueEmpty:
                            break
                    if self._priority_buffer:
                        _all_ticks = [tick] + self._priority_buffer
                        _all_ticks.sort(
                            key=lambda t: -self._profitability_priority_sort_score(t)
                        )
                        tick = _all_ticks[0]
                        self._priority_buffer = _all_ticks[1:]

                stock_code: str = tick[0]
                price: int = int(abs(float(tick[2] or 0)))
                if price <= 0:
                    logger.debug("ScalpingEntryEngine: invalid tick price skipped %s raw=%s", stock_code, tick[2])
                    continue
                normalized_tick = (tick[0], tick[1], price, *tick[3:])
                tick = normalized_tick

                # 유니버스에 없으면 스킵
                if stock_code not in self._universe:
                    continue

                # 틱 히스토리 축적. 세션 고가는 진입 평가 후 갱신해야 돌파 조건이 막히지 않는다.
                self._tick_history[stock_code].append(tick)
                self._update_minute_bars(stock_code, tick)

                # VWAP 누적 (당일 실시간 산출)
                _tp = float(tick[2] or 0)
                _tv = float(tick[3] or 0)
                if _tp > 0 and _tv > 0:
                    _vd = self._vwap_data.get(stock_code)
                    if _vd is None:
                        _vd = {"cum_pv": 0.0, "cum_vol": 0.0}
                        self._vwap_data[stock_code] = _vd
                    _vd["cum_pv"] += _tp * _tv
                    _vd["cum_vol"] += _tv

                # [P0-FLAT-COOLDOWN] 보합 반복 방지: 같은 종목 동일가 반복 틱은 카드 평가/감사 폭주를
                # 유발하므로 _FLAT_COOLDOWN_SEC 이내에 |Δprice|/price < _FLAT_COOLDOWN_BPS bps 이면 드롭.
                # tick_history/VWAP은 이미 위에서 누적되었으므로 정보 손실 없음.
                _eval_now = time_module.monotonic()
                _prev_eval = self._last_eval_tick.get(stock_code)
                if _prev_eval is not None:
                    _prev_price, _prev_ts = _prev_eval
                    if _prev_price > 0 and (_eval_now - _prev_ts) < _FLAT_COOLDOWN_SEC:
                        _bps = abs(_tp - _prev_price) / _prev_price * 10000.0
                        if _bps < _FLAT_COOLDOWN_BPS:
                            self._flat_skip_count += 1
                            if self._flat_skip_count % 1000 == 0:
                                logger.info(
                                    "ScalpingEntry: flat-tick dedup skipped %d ticks "
                                    "(cooldown=%.1fs bps=%.0f)",
                                    self._flat_skip_count,
                                    _FLAT_COOLDOWN_SEC,
                                    _FLAT_COOLDOWN_BPS,
                                )
                            continue
                self._last_eval_tick[stock_code] = (_tp, _eval_now)

                # 진입 불가 조건 (장외, 한도, 이미 매수)
                if not self._is_entry_allowed():
                    _nxt_hours_now = _is_in_nxt_hours_now()
                    self._audit_limit_up_pre_card_skip(
                        stock_code=stock_code,
                        tick=tick,
                        reason_code="nxt_disabled" if _nxt_hours_now else "entry_globally_blocked",
                        reason_text=(
                            "NXT 세션이지만 GO100_SCALPING_NXT_ENTRY_ENABLED=false"
                            if _nxt_hours_now
                            else "장외 시간 전역 진입 차단"
                        ),
                        extra_metrics={"daily_buy_count": self._daily_buy_count},
                    )
                    continue
                if stock_code in self._bought_today:
                    _buy_ts = self._bought_today_ts.get(stock_code, 0.0)
                    _elapsed = time_module.time() - _buy_ts if _buy_ts > 0 else float("inf")
                    if _elapsed < _REENTRY_COOLDOWN_SEC:
                        self._audit_limit_up_pre_card_skip(
                            stock_code=stock_code,
                            tick=tick,
                            reason_code="reentry_cooldown_pre_card",
                            reason_text=f"재진입 쿨다운 중 ({_elapsed:.0f}s/{_REENTRY_COOLDOWN_SEC}s)",
                        )
                        continue
                    self._bought_today.discard(stock_code)
                    self._bought_today_ts.pop(stock_code, None)
                if stock_code in self._manual_excluded_stocks:
                    self._audit_limit_up_pre_card_skip(
                        stock_code=stock_code,
                        tick=tick,
                        reason_code="manual_global_excluded",
                        reason_text="스크리너 전역 선택 제외종목",
                    )
                    continue
                if stock_code in self._loss_cooldown_stocks:
                    self._audit_limit_up_pre_card_skip(
                        stock_code=stock_code,
                        tick=tick,
                        reason_code="loss_cooldown_pre_card",
                        reason_text="최근 손실 종목 재진입 쿨다운으로 카드 평가 전 차단",
                    )
                    continue
                if stock_code in self._overheated_stocks:
                    self._audit_limit_up_pre_card_skip(
                        stock_code=stock_code,
                        tick=tick,
                        reason_code="overheated_limit_up_3days",
                        reason_text="최근 3거래일 연속 상한가 과열 종목으로 카드 평가 전 차단",
                    )
                    continue

                # [P0 L0] 25% 도달 시점 추적 — 종목이 처음 25%+ 도달하면 KST 시각 기록
                if stock_code not in self._time_25pct:
                    _prev_close_l0 = float(self._universe_meta.get(stock_code, {}).get("close_price") or 0)
                    if _prev_close_l0 > 0:
                        _intraday_pct_l0 = (price / _prev_close_l0 - 1) * 100
                        if _intraday_pct_l0 >= 25.0:
                            try:
                                from zoneinfo import ZoneInfo
                                self._time_25pct[stock_code] = datetime.now(ZoneInfo("Asia/Seoul"))
                            except Exception:
                                self._time_25pct[stock_code] = datetime.now()

                # 카드별 진입 평가 + 후보 발굴 감사 로그
                _loop_start_ms = time_module.monotonic() * 1000
                _evaluated_count = 0
                _entry_candidates: list[dict] = []
                for card in self._cards:
                    if card.get("card_status") not in ("LIVE", "PAPER_LIVE"):
                        continue
                    _evaluated_count += 1
                    portfolio_id = card["portfolio_id"]
                    max_stocks = card["max_stocks"]
                    open_count = self._card_positions.get(portfolio_id, 0)

                    _card_excluded = self._card_excluded_stocks.get(int(card["card_id"]), set())
                    if stock_code in _card_excluded:
                        self._audit_decision(
                            card=card,
                            stock_code=stock_code,
                            stage="pre_entry",
                            decision="skip",
                            reason_code="manual_card_excluded",
                            reason_text="전략카드별 선택 제외종목",
                            metrics={"card_id": card["card_id"]},
                        )
                        continue

                    _nxt_session_now = _current_nxt_session()
                    if _nxt_session_now:
                        if not _card_allows_nxt_session(card):
                            self._audit_decision(
                                card=card,
                                stock_code=stock_code,
                                stage="pre_entry",
                                decision="skip",
                                reason_code="nxt_card_not_enabled",
                                reason_text=f"{_nxt_session_now} 세션이지만 카드별 NXT 신규진입 설정이 없어 평가 차단",
                                metrics={"nxt_session": _nxt_session_now},
                                throttle_seconds=300,
                            )
                            continue
                        if not bool(self._universe_meta.get(stock_code, {}).get("is_nxt")):
                            self._audit_decision(
                                card=card,
                                stock_code=stock_code,
                                stage="pre_entry",
                                decision="skip",
                                reason_code="nxt_not_eligible",
                                reason_text="NXT 거래 가능 종목이 아니므로 오전장 평가 차단",
                                metrics={"nxt_session": _nxt_session_now},
                                throttle_seconds=300,
                            )
                            continue

                    if stock_code in self._card_held_stocks.get(portfolio_id, set()):
                        self._audit_decision(
                            card=card,
                            stock_code=stock_code,
                            stage="pre_entry",
                            decision="skip",
                            reason_code="duplicate_stock_held",
                            reason_text="동일 종목 이미 보유 중 — 중복 진입 차단",
                            metrics={"portfolio_id": portfolio_id},
                            throttle_seconds=300,
                        )
                        continue

                    if open_count >= max_stocks:
                        self._audit_decision(
                            card=card,
                            stock_code=stock_code,
                            stage="pre_entry",
                            decision="skip",
                            reason_code="max_positions_reached",
                            reason_text="카드별 최대 보유 종목 수 도달",
                            metrics={"open_count": open_count, "max_stocks": max_stocks},
                        )
                        continue

                    _card303_discovery_ok, _card303_reason, _card303_metrics = (
                        self._check_card303_discovery(card, stock_code)
                    )
                    if not _card303_discovery_ok:
                        self._audit_decision(
                            card=card,
                            stock_code=stock_code,
                            stage="pre_entry",
                            decision="skip",
                            reason_code=_card303_reason,
                            reason_text=(
                                "#303 당일 fresh snapshot 거래대금 Top50 발굴 결과가 없어 진입 차단"
                                if _card303_reason == "card303_discovery_unavailable"
                                else "#303 당일 거래대금 Top50 발굴군 밖의 종목은 진입 차단"
                            ),
                            metrics=_card303_metrics,
                            throttle_seconds=60,
                        )
                        continue

                    # [P0-A] 금액형 카드만 예산 선검사를 수행한다.
                    # fixed_quantity 카드는 per_position_amount를 자본 차단값으로 사용하지
                    # 않으며, 브로커의 최신 가용현금과 실제 1주 예상비용을 _execute_buy에서
                    # 최종 확인한다. DB portfolio cash는 stale할 수 있어 여기서 선차단하지 않는다.
                    _tick_price = float(tick[2] or 0)
                    _pre_sizing_mode = str(card.get("position_sizing_mode") or "").strip().lower()
                    _pre_fixed_qty = int(card.get("fixed_quantity") or 0)
                    _is_fixed_quantity = _pre_sizing_mode == "fixed_quantity" and _pre_fixed_qty > 0
                    _defer_fixed_cash_gate = _defer_fixed_quantity_cash_gate(card)
                    if _is_fixed_quantity and not _defer_fixed_cash_gate:
                        _card_budget = min(
                            float(card.get("available_for_buy") or card.get("current_cash", 0)),
                            float(card.get("current_cash", 0)),
                        )
                    elif not _is_fixed_quantity:
                        _card_budget = min(
                            float(card.get("per_position_amount") or card.get("current_cash", 0)),
                            float(card.get("available_for_buy") or card.get("current_cash", 0)),
                            float(card.get("current_cash", 0)),
                        )
                    if (
                        not _defer_fixed_cash_gate
                        and _tick_price > 0
                        and _card_budget < _tick_price
                    ):
                        self._audit_decision(
                            card=card, stock_code=stock_code,
                            stage="pre_entry", decision="skip",
                            reason_code="budget_exhausted",
                            reason_text=f"카드 예산 소진(잔여 {_card_budget:.0f} < 주가 {_tick_price:.0f})",
                        )
                        continue

                    if card.get("no_trade_windows"):
                        try:
                            from zoneinfo import ZoneInfo
                            _ntw_now = datetime.now(ZoneInfo("Asia/Seoul")).time()
                        except Exception:
                            _ntw_now = datetime.now().time()
                        if any(s <= _ntw_now <= e for s, e in card["no_trade_windows"]):
                            self._audit_decision(
                                card=card, stock_code=stock_code,
                                stage="pre_entry", decision="skip",
                                reason_code="no_trade_window",
                                reason_text="risk_params 진입금지 시간대",
                            )
                            continue

                    _sp = card.get("strategy_params") or {}
                    _is_overnight = isinstance(_sp, dict) and str(_sp.get("engine_type", "")).lower() == "overnight_closing"
                    _overnight_gap = 120.0
                    data_quality = evaluate_realtime_data_quality(
                        stock_code, tick[2],
                        tick_max_gap_override=_overnight_gap if _is_overnight else None,
                        snapshot_max_gap_override=_overnight_gap if _is_overnight else None,
                    )
                    data_quality_status = str(data_quality.get("status") or "UNKNOWN")
                    is_paper_live = str(card.get("card_status") or "").upper() == "PAPER_LIVE"
                    data_quality_reasons = data_quality.get("reasons") or []
                    _card_id_for_quality = int(card.get("card_id") or card.get("go100_card_id") or 0)
                    is_card303_real_live = (
                        _card_id_for_quality == 303
                        and not bool(card.get("account_is_mock"))
                    )
                    canary_quality_warn_override = (
                        data_quality_status == "WARN"
                        and is_card303_real_live
                        and not _is_overnight
                    )
                    # #303 실계좌 LIVE는 WARN을 감시 로그로 남기되 주문 후보 평가를 계속한다.
                    # CRITICAL(틱 없음/stale)은 여전히 주문 차단한다.
                    quality_blocks_order = data_quality_status == "CRITICAL" or (
                        data_quality_status == "WARN"
                        and not is_paper_live
                        and not _is_overnight
                        and not canary_quality_warn_override
                    )
                    if data_quality_status != "PASS":
                        quality_metrics = {
                            "data_quality_status": data_quality_status,
                            "data_quality": data_quality,
                            "card_status": card.get("card_status"),
                            "account_is_mock": card.get("account_is_mock"),
                        }
                        if canary_quality_warn_override:
                            quality_metrics.update({
                                "canary_quality_warn_override": True,
                                "override_scope": "card303_real_live_warn_only",
                                "original_data_quality_status": "WARN",
                            })
                        self._audit_decision(
                            card=card,
                            stock_code=stock_code,
                            stage="data_quality_gate",
                            decision="reject" if quality_blocks_order else "warn",
                            reason_code="data_quality_block" if quality_blocks_order else "data_quality_warn",
                            reason_text=data_quality.get("reason_text") or "실시간 데이터 품질 미달",
                            metrics=quality_metrics,
                            throttle_seconds=60,
                        )
                        if quality_blocks_order:
                            continue

                    uf = card.get("universe_filter")
                    if isinstance(uf, dict):
                        _uf_meta = self._universe_meta.get(stock_code, {})
                        _uf_mcap = float(_uf_meta.get("market_cap") or 0)
                        _uf_skip = False
                        _uf_reason = ""
                        _tick_price = float(tick[2] or 0)
                        _tick_cum_vol = float(tick[4] or 0) if len(tick) > 4 else 0.0
                        for _uf_cond in (uf.get("conditions") or []):
                            if not isinstance(_uf_cond, dict):
                                continue
                            _ct = _uf_cond.get("type", "")
                            _cp = _uf_cond.get("params") or {}
                            if _ct == "market_cap":
                                _cap_range = _cp.get("value", {})
                                if _uf_mcap > 0:
                                    if float(_cap_range.get("min", 0)) > 0 and _uf_mcap < float(_cap_range["min"]):
                                        _uf_skip, _uf_reason = True, "시총 하한 미달"
                                    if float(_cap_range.get("max", 0)) > 0 and _uf_mcap > float(_cap_range["max"]):
                                        _uf_skip, _uf_reason = True, "시총 상한 초과"
                            elif _ct == "price" and _tick_price > 0:
                                _pr = _cp.get("current_price", {})
                                if _pr.get("min") and _tick_price < float(_pr["min"]):
                                    _uf_skip, _uf_reason = True, "가격 하한 미달"
                                if _pr.get("max") and _tick_price > float(_pr["max"]):
                                    _uf_skip, _uf_reason = True, "가격 상한 초과"
                                _cr = _cp.get("change_pct", {})
                                if _cr:
                                    _prev = float(_uf_meta.get("close_price") or 0)
                                    if _prev > 0:
                                        _chg = (_tick_price / _prev - 1) * 100
                                        if _cr.get("min") is not None and _chg < float(_cr["min"]):
                                            _uf_skip, _uf_reason = True, "등락률 하한 미달"
                                        if _cr.get("max") is not None and _chg > float(_cr["max"]):
                                            _uf_skip, _uf_reason = True, "등락률 상한 초과"
                            elif _ct == "volume":
                                _vr = _cp.get("volume_today", {})
                                if _vr.get("min") and _tick_cum_vol > 0 and _tick_cum_vol < float(_vr["min"]):
                                    _uf_skip, _uf_reason = True, "거래량 하한 미달"
                            elif _ct in ("mahaseven_top30", "mahaseven_top50"):
                                if hasattr(self, '_mahaseven_top50_codes') and self._mahaseven_top50_codes:
                                    if stock_code not in self._mahaseven_top50_codes:
                                        _uf_skip, _uf_reason = True, "마하세븐 누적 거래대금 상위 50위 외"
                        if _uf_skip:
                            self._audit_decision(
                                card=card, stock_code=stock_code,
                                stage="pre_entry", decision="skip",
                                reason_code="universe_filter_reject",
                                reason_text=f"universe_filter: {_uf_reason}",
                                metrics={"market_cap": _uf_mcap, "price": _tick_price, "cum_vol": _tick_cum_vol},
                            )
                            continue

                    # [P0 L0] 25% 도달 30분 이내 게이트 — 상한가 진입규칙 카드(#119 계열)에 적용.
                    # 주의: #119는 holding_period=DAILY 라서 _is_scalping_strategy()가 False다.
                    # 상따 카드를 정확히 잡으려면 entry_rules 기반 판별을 써야 한다.
                    if _has_limit_up_entry_rules(card.get("entry_rules")) and stock_code in self._time_25pct:
                        _reach_dt = self._time_25pct[stock_code]
                        _session_open = _reach_dt.replace(hour=9, minute=0, second=0, microsecond=0)
                        _min_from_open = (_reach_dt - _session_open).total_seconds() / 60
                        # 장중 재시작 보호: 엔진이 09:01 이후에 떴고, 관측 시각이 기동 직후
                        # 120초 이내라면 실제 25% 도달시각을 알 수 없으므로 차단하지 않는다.
                        _eng_start = self._engine_started_at
                        _started_late = (
                            _eng_start.hour > 9 or (_eng_start.hour == 9 and _eng_start.minute >= 1)
                        )
                        try:
                            _since_start = abs((_reach_dt - _eng_start).total_seconds())
                        except Exception:
                            _since_start = 9999.0
                        _reach_time_unknown = _started_late and _since_start <= 120
                        if _reach_time_unknown:
                            metrics_l0_unknown = {
                                "reach_time": str(_reach_dt.time()),
                                "engine_started_at": str(_eng_start.time()),
                            }
                            self._audit_decision(
                                card=card, stock_code=stock_code,
                                stage="l0_entry_filter", decision="warn",
                                reason_code="reach25_time_unknown_after_restart",
                                reason_text="장중 재시작으로 25% 도달시각 불명 — L0 필터 우회(fail-open)",
                                metrics=metrics_l0_unknown,
                                throttle_seconds=600,
                            )
                        elif _min_from_open > 30:
                            self._audit_decision(
                                card=card, stock_code=stock_code,
                                stage="l0_entry_filter", decision="skip",
                                reason_code="slow_25pct_reach",
                                reason_text=f"25% 도달이 장시작 {_min_from_open:.0f}분 후 (>30분, L0 필터)",
                                metrics={"reach_time": str(_reach_dt.time()), "minutes_from_open": round(_min_from_open, 1)},
                                throttle_seconds=300,
                            )
                            continue

                    # [P0] 카드별 리스크 게이트: 연속손실·일일손실한도
                    _card_id_int = int(card.get("card_id") or 0)
                    if _card_id_int > 0:
                        self._maybe_load_daily_risk_state()
                        _consec = self._consecutive_loss_count.get(_card_id_int, 0)
                        _rp = _json_dict(card.get("risk_params"))
                        _consec_limit = int(_rp.get("consecutive_loss_stop") or 3)
                        if _consec >= _consec_limit:
                            self._audit_decision(
                                card=card, stock_code=stock_code,
                                stage="risk_gate", decision="skip",
                                reason_code="consecutive_loss_breaker",
                                reason_text=f"연속 {_consec}패 >= {_consec_limit} — 카드 매수 차단",
                                metrics={"consecutive_losses": _consec, "limit": _consec_limit},
                                throttle_seconds=300,
                            )
                            continue
                        _daily_pnl = self._daily_pnl_by_card.get(_card_id_int, 0.0)
                        _init_cap = self._card_initial_capital.get(_card_id_int, 0.0)
                        _init_cap = (
                            _init_cap
                            or float(card.get("allocated_amount") or 0)
                            or float(card.get("initial_capital") or 0)
                        )
                        _loss_limits = resolve_card126_daily_loss_limits(card, _rp)
                        _loss_eval = evaluate_daily_loss_limit(
                            daily_pnl=_daily_pnl,
                            initial_capital=_init_cap,
                            limit_pct=_loss_limits["limit_pct"],
                            limit_amount=_loss_limits["limit_amount"],
                        )
                        if _loss_eval["breached"]:
                            _breach_reason = _loss_eval["breach_reason"]
                            _reason_code = (
                                "daily_loss_limit_amount"
                                if _breach_reason == "amount"
                                else "daily_loss_limit"
                            )
                            _current_pct_text = (
                                f"{_loss_eval['current_loss_pct']:.2f}%"
                                if _loss_eval["current_loss_pct"] is not None
                                else "미산출"
                            )
                            self._audit_decision(
                                card=card, stock_code=stock_code,
                                stage="risk_gate", decision="skip",
                                reason_code=_reason_code,
                                reason_text=(
                                    f"일일 손실 현재 {_daily_pnl:,.0f}원/{_current_pct_text}; "
                                    f"한도 amount={_loss_eval['limit_amount']!r}원, "
                                    f"pct={_loss_eval['limit_pct']:.2f}%"
                                ),
                                metrics={
                                    **_loss_eval,
                                    "loss_limit_semantics": _loss_limits["semantics"],
                                    "amount_configured": _loss_limits["amount_configured"],
                                },
                                throttle_seconds=300,
                            )
                            continue

                    # [P0] 손절 종목 당일 재진입 금지
                    if stock_code in self._stopped_out_today:
                        self._audit_decision(
                            card=card, stock_code=stock_code,
                            stage="risk_gate", decision="skip",
                            reason_code="stoploss_reentry_ban",
                            reason_text="손절 종목 당일 재진입 금지",
                            throttle_seconds=300,
                        )
                        continue

                    reason, reason_code, reason_text, metrics = self._evaluate_entry_with_audit(stock_code, tick, card)
                    metrics["data_quality_status"] = data_quality_status
                    metrics["data_quality"] = data_quality
                    if not reason:
                        # 모든 틱을 쓰면 과다 적재되므로 카드/종목/사유별 5분 단위 감사 로그만 남긴다.
                        self._audit_decision(
                            card=card,
                            stock_code=stock_code,
                            stage="entry_filter",
                            decision="skip",
                            reason_code=reason_code,
                            reason_text=reason_text,
                            metrics=metrics,
                        )
                        continue

                    # [P2] lock_score 품질 게이트는 순수 스캘핑 카드에만 적용한다.
                    # 종가매매/오버나이트 카드는 카드 entry_rules가 이미 신호 기준이므로
                    # 엔진 자체 점수로 이중 차단하지 않는다.
                    if _is_scalping_strategy(card):
                        _lock_score = self._compute_lock_score(stock_code, metrics, card)
                        metrics["lock_score"] = _lock_score
                        # 슬롯이 찰수록 하한 상향 → 남은 슬롯을 고품질 종목에 우선 배정
                        _slot_ratio = (open_count / max_stocks) if max_stocks else 0.0
                        _dyn_min = _MIN_LOCK_SCORE + _slot_ratio * _SLOT_PRIORITY_BONUS
                        metrics["lock_score_min"] = round(_dyn_min, 1)
                        if _lock_score < _dyn_min:
                            self._audit_decision(
                                card=card,
                                stock_code=stock_code,
                                stage="lock_score_gate",
                                decision="skip",
                                reason_code="lock_score_below_min",
                                reason_text=(
                                    f"lock_score {_lock_score:.1f} < 동적하한 {_dyn_min:.0f} "
                                    f"(슬롯 {open_count}/{max_stocks})"
                                ),
                                metrics=metrics,
                            )
                            continue

                    candidate_metrics = {
                        **metrics,
                        "entry_reason": reason,
                        "competition_sequence": card.get("competition_sequence"),
                        "live_priority": card.get("live_priority"),
                        "competition_policy": "GO100_ACCOUNT_RESOURCE_COMPETITION_V1",
                    }
                    self._audit_decision(
                        card=card,
                        stock_code=stock_code,
                        stage="entry_filter",
                        decision="pass",
                        reason_code=reason_code,
                        reason_text=reason_text,
                        metrics=candidate_metrics,
                        throttle_seconds=0,
                    )
                    _entry_candidates.append({
                        "card": card,
                        "stock_code": stock_code,
                        "price": price,
                        "reason": reason,
                        "reason_code": reason_code,
                        "reason_text": reason_text,
                        "metrics": candidate_metrics,
                        "portfolio_id": portfolio_id,
                        "max_stocks": max_stocks,
                        "open_count": open_count,
                    })

                if _entry_candidates:
                    selected, scored = self._select_competition_candidate(_entry_candidates)
                    if selected is None:
                        for candidate in scored:
                            self._audit_decision(
                                card=candidate["card"],
                                stock_code=stock_code,
                                stage="competition_engine",
                                decision="skip",
                                reason_code="competition_score_below_min",
                                reason_text=(
                                    f"중앙 경쟁 점수 {candidate.get('competition_score', 0):.1f} "
                                    f"< 최소 {_COMPETITION_MIN_SCORE:.1f}"
                                ),
                                metrics={
                                    **(candidate.get("metrics") or {}),
                                    "competition_mode": _COMPETITION_MODE,
                                    "competition_score": candidate.get("competition_score"),
                                    "competition_candidate_count": len(scored),
                                    "competition_canonical_selected_card_id": candidate.get("competition_canonical_selected_card_id"),
                                    "competition_canonical_selected_portfolio_id": candidate.get("competition_canonical_selected_portfolio_id"),
                                    "competition_canonical_score": candidate.get("competition_canonical_score"),
                                    "competition_is_canonical_winner": candidate.get("competition_is_canonical_winner"),
                                    "competition_policy": "GO100_ACCOUNT_RESOURCE_COMPETITION_V1",
                                },
                                throttle_seconds=0,
                            )
                    else:
                        await self._process_competition_candidate(
                            candidate=selected,
                            stock_code=stock_code,
                            price=price,
                            scored_candidates=scored,
                        )

                if price > self._session_high.get(stock_code, 0):
                    self._session_high[stock_code] = price

                _loop_ms = time_module.monotonic() * 1000 - _loop_start_ms
                if _loop_ms > 200 or _evaluated_count > 0:
                    logger.debug(
                        "ScalpingEntryEngine: loop_ms=%.1f evaluated_count=%d stock=%s universe=%d",
                        _loop_ms, _evaluated_count, stock_code, len(self._universe),
                    )

            except Exception as e:
                logger.error("ScalpingEntryEngine run loop error: %s", e)
                await asyncio.sleep(0.1)

    def stop(self) -> None:
        self._running = False
        logger.info("ScalpingEntryEngine stopped")


async def run_scalping_entry(
    tick_queue: asyncio.Queue,
    *,
    kiwoom_ws=None,
    minute_bar_queue: asyncio.Queue | None = None,
) -> None:
    """ScalpingEntryEngine 실행 엔트리포인트."""
    engine = ScalpingEntryEngine(tick_queue=tick_queue)
    if kiwoom_ws is not None:
        engine.set_kiwoom_ws(kiwoom_ws)
    if minute_bar_queue is None:
        await engine.run()
        return
    await asyncio.gather(
        engine.run(),
        engine.consume_external_minute_bars(minute_bar_queue),
    )
