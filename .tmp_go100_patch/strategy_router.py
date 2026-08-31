# Modified by: CUR-GO100-PHASE3-CARD-SERVICE, 2026-02-21
"""
GO100 전략 카드 API Router.
prefix: /api/go100/strategy-cards, /api/go100/store
"""
import json
import logging
from asyncio import TimeoutError as AsyncTimeoutError, wait_for
from datetime import datetime
from typing import Any, Optional
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.core.database import get_db
from backend.app.core.security_middleware import get_current_user
from backend.app.services.data.condition_search_collector import (
    get_recent_condition_stock_codes,
)
from backend.app.services.go100.universe.data_cache import DataCache
from backend.app.services.go100.universe.engine import UniverseEngine
from backend.app.services.go100.strategy import (
    go100_strategy_card_service,
    Go100StrategyCardCreate,
    Go100StrategyCardUpdate,
    Go100StrategyCardResponse,
    Go100StrategyCardListResponse,
    Go100StatusTransitionRequest,
    Go100StoreSubscribeRequest,
)
from backend.app.services.go100.strategy.card_service import (
    NotFoundException,
    BusinessLogicException,
    OwnershipException,
)
from backend.app.services.go100.backtest.data_gate import check_readiness as data_gate_check_readiness
from backend.app.services.market.krx_calendar import is_krx_trading_day_async
from backend.app.services.go100.strategy_autonomous_loop import StrategyAutonomousLoopService
from backend.app.services.go100.strategy_evolution import create_card_from_hypothesis
from backend.app.services.go100.strategy_promotion_approval import approve_strategy_promotion
from backend.app.services.go100.user_utils import get_effective_uid, get_go100_domain_uid

router = APIRouter(prefix="/api/go100/strategy-cards", tags=["GO100 Strategy Cards"])
approval_router = APIRouter(prefix="/api/go100/strategies", tags=["GO100 Strategy Promotions"])
KST = ZoneInfo("Asia/Seoul")
logger = logging.getLogger(__name__)


class StrategyApprovalRequest(BaseModel):
    reason: Optional[str] = None


class StrategyAutonomousImproveRequest(BaseModel):
    card_id: Optional[int] = None
    limit: int = 1
    min_proposals: int = 3
    queue_backtests: bool = True
    windows: Optional[list[str]] = None


async def _effective_user_id(current_user: dict, db: AsyncSession) -> int:
    return await get_go100_domain_uid(db, current_user["user_id"])


async def _screen_card119_watch_candidates(db: AsyncSession) -> tuple[list[dict[str, Any]], str | None]:
    """Return #119 watch candidates from point-in-time live sources only."""
    now_kst = datetime.now(KST)
    today = now_kst.date()
    result = await db.execute(
        text(r"""
            WITH current_snapshot AS (
                SELECT DISTINCT ON (sps.stock_code)
                       sps.stock_code AS code,
                       COALESCE(su.stock_name, sps.stock_code) AS name,
                       su.market_cap,
                       sps.price AS current_price,
                       sps.change_pct AS change_rate,
                       sps.volume,
                       sps.trade_amount,
                       sps.snapshot_time AS observed_at,
                       'current_snapshot_watch'::text AS candidate_scope,
                       NULL::text AS entry_gate_state,
                       NULL::text AS watch_reason
                FROM stock_price_snapshot sps
                LEFT JOIN stock_universe su ON su.stock_code = sps.stock_code
                WHERE (sps.snapshot_time AT TIME ZONE 'Asia/Seoul')::date = :today
                  AND COALESCE(sps.change_pct, 0) >= 20
                  AND sps.price > 0
                ORDER BY sps.stock_code, sps.snapshot_time DESC
            ),
            candidate_snapshots AS (
                SELECT DISTINCT ON (snap.symbol)
                       snap.symbol AS code,
                       COALESCE(snap.stock_name, su.stock_name, snap.symbol) AS name,
                       su.market_cap,
                       CASE
                           WHEN COALESCE(snap.raw_payload->>'price', snap.raw_payload->>'current_price') ~ '^-?[0-9]+(\.[0-9]+)?$'
                           THEN COALESCE(snap.raw_payload->>'price', snap.raw_payload->>'current_price')::numeric
                           ELSE NULL
                       END AS current_price,
                       snap.change_rate AS change_rate,
                       CASE
                           WHEN snap.raw_payload->>'volume' ~ '^[0-9]+$'
                           THEN (snap.raw_payload->>'volume')::bigint
                           ELSE NULL
                       END AS volume,
                       CASE
                           WHEN COALESCE(
                               snap.raw_payload->>'effective_trade_amount_krw',
                               snap.raw_payload->>'trade_amount',
                               snap.raw_payload->>'trading_value',
                               snap.raw_payload->>'total_trading_value_krw'
                           ) ~ '^-?[0-9]+(\.[0-9]+)?$'
                           THEN COALESCE(
                               snap.raw_payload->>'effective_trade_amount_krw',
                               snap.raw_payload->>'trade_amount',
                               snap.raw_payload->>'trading_value',
                               snap.raw_payload->>'total_trading_value_krw'
                           )::numeric
                           ELSE NULL
                       END AS trade_amount,
                       snap.captured_at AS observed_at,
                       'today_cumulative_watch'::text AS candidate_scope,
                       snap.entry_gate_state,
                       COALESCE(snap.watch_reason, snap.selected_reason) AS watch_reason
                FROM go100_card119_candidate_snapshots snap
                LEFT JOIN stock_universe su ON su.stock_code = snap.symbol
                WHERE snap.card_id = 119
                  AND snap.trading_date = :today
                  AND snap.change_rate >= 20
                ORDER BY snap.symbol, snap.captured_at DESC
            ),
            combined AS (
                SELECT * FROM current_snapshot
                UNION ALL
                SELECT * FROM candidate_snapshots
            ),
            ranked AS (
                SELECT *,
                       ROW_NUMBER() OVER (
                           PARTITION BY code
                           ORDER BY
                               CASE WHEN candidate_scope = 'current_snapshot_watch' THEN 0 ELSE 1 END,
                               observed_at DESC NULLS LAST
                       ) AS rn,
                       MAX(change_rate) OVER (PARTITION BY code) AS max_seen_change_rate,
                       MAX(observed_at) OVER (PARTITION BY code) AS last_seen_at
                FROM combined
            )
            SELECT code, name, market_cap, current_price, change_rate, volume, trade_amount,
                   candidate_scope, entry_gate_state, watch_reason,
                   max_seen_change_rate, last_seen_at
            FROM ranked
            WHERE rn = 1
            ORDER BY change_rate DESC NULLS LAST, max_seen_change_rate DESC NULLS LAST, code ASC
            LIMIT 200
        """),
        {"today": today},
    )
    rows = result.mappings().all()
    stocks = [
        {
            "code": row["code"],
            "name": row["name"] or row["code"],
            "market_cap": int(row["market_cap"]) if row["market_cap"] is not None else None,
            "current_price": int(row["current_price"]) if row["current_price"] is not None else None,
            "change_rate": round(float(row["change_rate"]), 2) if row["change_rate"] is not None else None,
            "volume": int(row["volume"]) if row["volume"] is not None else None,
            "trade_amount": float(row["trade_amount"]) if row["trade_amount"] is not None else None,
            "candidate_scope": row["candidate_scope"],
            "entry_gate_state": row["entry_gate_state"],
            "watch_reason": row["watch_reason"],
            "max_seen_change_rate": round(float(row["max_seen_change_rate"]), 2)
            if row["max_seen_change_rate"] is not None else None,
            "last_seen_at": row["last_seen_at"].isoformat() if row["last_seen_at"] else None,
            "signal_hit": bool(row["max_seen_change_rate"] is not None and float(row["max_seen_change_rate"]) >= 27.0),
        }
        for row in rows
    ]
    last_seen = max((item.get("last_seen_at") for item in stocks if item.get("last_seen_at")), default=None)
    return stocks, last_seen


def _as_rule_list(entry_rules: Any) -> list[dict[str, Any]]:
    if isinstance(entry_rules, dict):
        conditions = entry_rules.get("conditions")
        if isinstance(conditions, list):
            return [item for item in conditions if isinstance(item, dict)]
        return [entry_rules]
    if isinstance(entry_rules, list):
        return [item for item in entry_rules if isinstance(item, dict)]
    return []


def _extract_nested_rules(rule: dict[str, Any]) -> list[dict[str, Any]]:
    nested: list[dict[str, Any]] = [rule]
    for key in ("conditions", "rules", "filters"):
        value = rule.get(key)
        if isinstance(value, list):
            for item in value:
                if isinstance(item, dict):
                    nested.extend(_extract_nested_rules(item))
    for key in ("condition",):
        value = rule.get(key)
        if isinstance(value, dict):
            nested.extend(_extract_nested_rules(value))
    return nested


def _coerce_float(value: Any) -> float | None:
    try:
        if value is None or value == "":
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def _calc_rsi(closes: list[float], period: int = 14) -> float | None:
    if len(closes) <= period:
        return None
    gains: list[float] = []
    losses: list[float] = []
    for idx in range(1, len(closes)):
        delta = closes[idx] - closes[idx - 1]
        gains.append(max(delta, 0.0))
        losses.append(abs(min(delta, 0.0)))

    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period
    for idx in range(period, len(gains)):
        avg_gain = ((avg_gain * (period - 1)) + gains[idx]) / period
        avg_loss = ((avg_loss * (period - 1)) + losses[idx]) / period

    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return 100.0 - (100.0 / (1.0 + rs))


async def _evaluate_signal_hit(
    db: AsyncSession,
    entry_rules: Any,
    stock_code: str,
    ohlcv_rows: list[Any],
) -> bool:
    rules = _as_rule_list(entry_rules)
    if not rules:
        return False

    flattened: list[dict[str, Any]] = []
    for rule in rules:
        flattened.extend(_extract_nested_rules(rule))

    closes = [float(row.close) for row in ohlcv_rows if getattr(row, "close", None) is not None]
    volumes = [float(row.volume) for row in ohlcv_rows if getattr(row, "volume", None) is not None]
    latest_volume = volumes[-1] if volumes else None
    avg_volume_20 = (
        sum(volumes[-21:-1]) / 20
        if len(volumes) >= 21 and latest_volume is not None
        else None
    )
    volume_spike_ratio = (
        latest_volume / avg_volume_20
        if latest_volume is not None and avg_volume_20 not in (None, 0)
        else None
    )
    rsi_14 = _calc_rsi(closes, 14)

    trade_date = datetime.now(KST).date()
    minute_result = await db.execute(
        text(
            """
            SELECT trade_time, high_price, close_price
            FROM v4_ohlcv_minute
            WHERE stock_code = :stock_code
              AND trade_date = :trade_date
            ORDER BY trade_time ASC
            LIMIT 390
            """
        ),
        {"stock_code": stock_code, "trade_date": trade_date},
    )
    minute_rows = minute_result.fetchall()
    breakout_5m = False
    if len(minute_rows) >= 6:
        prev_high = max(float(row.high_price or 0) for row in minute_rows[-6:-1])
        latest_minute_close = float(minute_rows[-1].close_price or 0)
        breakout_5m = latest_minute_close > prev_high if prev_high > 0 else False

    matched_any = False
    for rule in flattened:
        kind = str(rule.get("type") or rule.get("indicator") or rule.get("field") or "").lower()
        params = rule.get("params") if isinstance(rule.get("params"), dict) else {}

        if kind in {"volume_spike_ratio", "volume_surge", "volume_explosion"}:
            threshold = (
                _coerce_float(rule.get("value"))
                or _coerce_float(rule.get("ratio"))
                or _coerce_float(params.get("threshold"))
                or _coerce_float(params.get("ratio"))
                or 1.0
            )
            if volume_spike_ratio is None:
                return False
            matched_any = True
            if volume_spike_ratio < threshold:
                return False
            continue

        if kind in {"rsi", "rsi_threshold", "rsi_oversold", "rsi_overbought"}:
            operator = str(rule.get("operator") or params.get("operator") or "<=").strip()
            threshold = (
                _coerce_float(rule.get("value"))
                or _coerce_float(rule.get("threshold"))
                or _coerce_float(params.get("value"))
                or _coerce_float(params.get("threshold"))
                or (30.0 if "oversold" in kind else 70.0)
            )
            if rsi_14 is None or threshold is None:
                return False
            matched_any = True
            if operator in {"<", "<="}:
                if not rsi_14 <= threshold:
                    return False
            elif operator in {">", ">="}:
                if not rsi_14 >= threshold:
                    return False
            else:
                return False
            continue

        if kind in {"breakout_5m", "price_breakout"}:
            timeframe = str(rule.get("timeframe") or params.get("timeframe") or "").lower()
            lookback = int(
                _coerce_float(rule.get("lookback"))
                or _coerce_float(rule.get("lookback_days"))
                or _coerce_float(params.get("lookback"))
                or 5
            )
            if kind == "breakout_5m" or timeframe in {"5m", "5min", "5minute", "minute"} or lookback == 5:
                matched_any = True
                if not breakout_5m:
                    return False
            continue

    return matched_any


async def _apply_signal_hits(
    db: AsyncSession,
    stocks: list[dict[str, Any]],
    entry_rules: Any,
    cache: DataCache,
) -> list[dict[str, Any]]:
    ranked = sorted(
        stocks,
        key=lambda item: (item["market_cap"] is None, -(item["market_cap"] or 0), item["code"]),
    )
    targets = ranked[:15]
    signal_map: dict[str, bool | None] = {stock["code"]: None for stock in stocks}
    for stock in targets:
        signal_map[stock["code"]] = await _evaluate_signal_hit(
            db,
            entry_rules,
            stock["code"],
            cache.get_ohlcv(stock["code"]),
        )

    enriched: list[dict[str, Any]] = []
    for stock in stocks:
        item = dict(stock)
        item["signal_hit"] = signal_map.get(stock["code"])
        enriched.append(item)
    return enriched


@router.post("", response_model=Go100StrategyCardResponse, status_code=201)
async def create_card(
    data: Go100StrategyCardCreate,
    current_user: dict = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """새 전략 카드 생성."""
    try:
        user_id = await _effective_user_id(current_user, db)
        return await go100_strategy_card_service.create_card(
            user_id, data, db
        )
    except Exception as e:
        if isinstance(e, (NotFoundException, BusinessLogicException, OwnershipException)):
            raise HTTPException(status_code=400, detail=str(e))
        raise HTTPException(status_code=500, detail="Internal server error")


@router.get("", response_model=Go100StrategyCardListResponse)
async def list_cards(
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    status: Optional[str] = Query(None),
    source_type: Optional[str] = Query(None),
    category: Optional[str] = Query(None),
    include_inactive: bool = Query(False),
    current_user: dict = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """내 전략 카드 목록 조회."""
    user_id = await _effective_user_id(current_user, db)
    return await go100_strategy_card_service.list_cards(
        user_id, page, page_size, status, source_type, db, include_inactive=include_inactive, category=category
    )


@router.get("/readiness/report")
async def readiness_report(
    target_mode: str = Query("LIVE", pattern="^(CREATION|PAPER_LIVE|LIVE)$"),
    scope: str = Query("user", pattern="^(user|all)$"),
    limit: int = Query(200, ge=1, le=1000),
    current_user: dict = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """전략카드 readiness 리포트. all scope는 관리자만 전체 카드로 확장."""
    is_admin = bool(current_user.get("is_admin")) or str(current_user.get("role") or "").lower() in {"admin", "ceo"}
    include_all = scope == "all" and is_admin
    user_id = await _effective_user_id(current_user, db)
    return await go100_strategy_card_service.list_readiness_reports(
        user_id,
        target_mode,
        db,
        include_all_users=include_all,
        limit=limit,
    )


@router.get("/readiness/account-report")
async def account_readiness_report(
    target_mode: str = Query("LIVE", pattern="^(CREATION|PAPER_LIVE|LIVE)$"),
    ensure_live_config_draft: bool = Query(False),
    current_user: dict = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """현재 effective user 기준 전략카드/계좌/실매매 안전설정 리포트."""
    user_id = await _effective_user_id(current_user, db)
    return await go100_strategy_card_service.get_effective_user_readiness_report(
        user_id,
        db,
        target_mode=target_mode,
        ensure_live_config_draft=ensure_live_config_draft,
    )


@router.get("/readiness/moongoby-report")
async def moongoby_readiness_report(
    target_mode: str = Query("LIVE", pattern="^(CREATION|PAPER_LIVE|LIVE)$"),
    ensure_live_config_draft: bool = Query(False),
    current_user: dict = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """CEO 계정(moongoby@naver.com)을 v4 effective user 기준으로 검증."""
    is_admin = bool(current_user.get("is_admin")) or str(current_user.get("role") or "").lower() in {"admin", "ceo"}
    if not is_admin:
        raise HTTPException(status_code=403, detail="관리자 권한이 필요합니다.")
    email = "moongoby@naver.com"
    result = await db.execute(
        text("SELECT id FROM users WHERE email = :email"),
        {"email": email},
    )
    effective_uid = result.scalar_one_or_none()
    if effective_uid is None:
        raise HTTPException(status_code=404, detail="moongoby 계정을 찾을 수 없습니다.")
    return await go100_strategy_card_service.get_effective_user_readiness_report(
        int(effective_uid),
        db,
        target_mode=target_mode,
        ensure_live_config_draft=ensure_live_config_draft,
        email=email,
    )


@router.get("/active")
async def list_active_cards(db: AsyncSession = Depends(get_db)):
    """활성 상태 전략 카드 공개 목록 (Command Center용, 인증 불필요)."""
    try:
        result = await db.execute(text("""
            SELECT go100_card_id AS id,
                   strategy_name AS name,
                   description,
                   strategy_type,
                   source_type,
                   category,
                   CASE
                       WHEN card_status = 'DRAFT'
                        AND (last_backtest_id IS NOT NULL OR last_backtest_at IS NOT NULL)
                       THEN 'BACKTESTED'
                       ELSE card_status
                   END AS card_status,
                   max_stocks,
                   condition_code,
                   bar_timeframe,
                   CASE
                       WHEN jsonb_typeof(entry_rules) = 'array' THEN jsonb_array_length(entry_rules)
                       WHEN jsonb_typeof(entry_rules) = 'object' THEN 1
                       ELSE 0
                   END AS entry_rule_count,
                   CASE
                       WHEN jsonb_typeof(exit_rules) = 'array' THEN jsonb_array_length(exit_rules)
                       WHEN jsonb_typeof(exit_rules) = 'object' THEN 1
                       ELSE 0
                   END AS exit_rule_count,
                   last_backtest_return AS backtest_pnl_pct,
                   last_backtest_mdd AS backtest_mdd,
                   last_backtest_sharpe,
                   updated_at
            FROM go100_strategy_cards
            WHERE card_status IN ('IDEA', 'DRAFT', 'BACKTESTED', 'PAPER_LIVE', 'LIVE')
              AND is_active = true
            ORDER BY updated_at DESC
            LIMIT 20
        """))
        rows = result.fetchall()
        strategies = []
        for r in rows:
            strategies.append({
                "id": r.id, "name": r.name,
                "description": r.description,
                "category": r.category,
                "strategy_type": r.strategy_type,
                "source_type": r.source_type,
                "status": r.card_status,
                "max_stocks": r.max_stocks,
                "condition_code": r.condition_code,
                "bar_timeframe": r.bar_timeframe,
                "entry_rule_count": int(r.entry_rule_count or 0),
                "exit_rule_count": int(r.exit_rule_count or 0),
                "win_rate": float(r.last_backtest_sharpe) if r.last_backtest_sharpe is not None else None,
                "pnl_pct": float(r.backtest_pnl_pct) if r.backtest_pnl_pct is not None else None,
                "mdd": float(r.backtest_mdd) if r.backtest_mdd is not None else None,
                "updated_at": r.updated_at.isoformat() if r.updated_at else None,
            })
        return {"strategies": strategies, "count": len(strategies)}
    except Exception as e:
        import logging
        logging.getLogger(__name__).error("list_active_cards error: %s", e)
        return {"strategies": [], "count": 0}


@router.get("/signals")
async def list_recent_signals(
    limit: int = Query(10, ge=1, le=50),
    db: AsyncSession = Depends(get_db),
):
    """최근 매매 시그널 (mock_trades 기반, 인증 불필요)."""
    try:
        result = await db.execute(text("""
            SELECT id, trade_date, ticker, strategy_id, direction,
                   entry_price, exit_price, pnl_pct
            FROM v4_mock_trades
            ORDER BY created_at DESC
            LIMIT :limit
        """), {"limit": limit})
        rows = result.fetchall()
        signals = []
        for r in rows:
            signals.append({
                "id": r.id, "date": str(r.trade_date),
                "ticker": r.ticker, "strategy": r.strategy_id,
                "direction": r.direction,
                "entry": float(r.entry_price) if r.entry_price else None,
                "exit": float(r.exit_price) if r.exit_price else None,
                "pnl_pct": float(r.pnl_pct) if r.pnl_pct else None,
            })
        return {"signals": signals, "count": len(signals)}
    except Exception as e:
        import logging
        logging.getLogger(__name__).error("list_recent_signals error: %s", e)
        return {"signals": [], "count": 0}


@router.post("/autonomous-improve")
async def autonomous_improve_strategy_cards(
    body: StrategyAutonomousImproveRequest = StrategyAutonomousImproveRequest(),
    current_user: dict = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Inspect strategy cards and create pending improvement approvals without applying edits."""
    user_id = await _effective_user_id(current_user, db)
    service = StrategyAutonomousLoopService()
    try:
        if body.card_id:
            return await service.run_for_card(
                db,
                user_id=user_id,
                card_id=int(body.card_id),
                min_proposals=body.min_proposals,
                queue_backtests=body.queue_backtests,
                windows=body.windows,
            )
        return await service.run_for_user_cards(
            db,
            user_id=user_id,
            limit=body.limit,
            min_proposals=body.min_proposals,
            queue_backtests=body.queue_backtests,
            windows=body.windows,
        )
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    except Exception as exc:
        logger.exception("autonomous_improve_strategy_cards failed: %s", exc)
        raise HTTPException(status_code=500, detail="전략카드 자율 개선 루프 처리 실패")


@router.post("/{card_id}/autonomous-improve")
async def autonomous_improve_strategy_card(
    card_id: int,
    body: StrategyAutonomousImproveRequest = StrategyAutonomousImproveRequest(),
    current_user: dict = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Inspect one strategy card and create pending improvement approvals without applying edits."""
    user_id = await _effective_user_id(current_user, db)
    service = StrategyAutonomousLoopService()
    try:
        return await service.run_for_card(
            db,
            user_id=user_id,
            card_id=card_id,
            min_proposals=body.min_proposals,
            queue_backtests=body.queue_backtests,
            windows=body.windows,
        )
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    except Exception as exc:
        logger.exception("autonomous_improve_strategy_card failed: %s", exc)
        raise HTTPException(status_code=500, detail="전략카드 자율 개선 루프 처리 실패")


@router.get("/{card_id}/readiness")
async def get_card_readiness(
    card_id: int,
    target_mode: str = Query("LIVE", pattern="^(CREATION|PAPER_LIVE|LIVE)$"),
    current_user: dict = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """전략카드 단건 readiness 리포트와 보완 후보."""
    try:
        user_id = await _effective_user_id(current_user, db)
        return await go100_strategy_card_service.get_readiness_report(
            user_id, card_id, target_mode, db
        )
    except NotFoundException as e:
        raise HTTPException(status_code=404, detail=str(e))


@router.post("/{card_id}/readiness/repair")
async def repair_card_readiness(
    card_id: int,
    target_mode: str = Query("CREATION", pattern="^(CREATION|PAPER_LIVE|LIVE)$"),
    current_user: dict = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """전략카드의 비어 있는 readiness 필드를 결정론적 기본값으로 보완."""
    try:
        user_id = await _effective_user_id(current_user, db)
        return await go100_strategy_card_service.repair_card_readiness(
            user_id, card_id, target_mode, db
        )
    except NotFoundException as e:
        raise HTTPException(status_code=404, detail=str(e))
    except (BusinessLogicException, OwnershipException) as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.get("/{card_id}", response_model=Go100StrategyCardResponse)
async def get_card(
    card_id: int,
    current_user: dict = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """전략 카드 상세 조회."""
    try:
        user_id = await _effective_user_id(current_user, db)
        return await go100_strategy_card_service.get_card(
            user_id, card_id, db
        )
    except NotFoundException as e:
        raise HTTPException(status_code=404, detail=str(e))
    except OwnershipException as e:
        raise HTTPException(status_code=403, detail=str(e))


@router.get("/{card_id}/analysis")
async def get_card_analysis(
    card_id: int,
    current_user: dict = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """전략카드별 분석 워크벤치 데이터."""
    user_id = await _effective_user_id(current_user, db)
    diagnostics: list[dict[str, Any]] = []

    async def safe_query(label: str, sql: str, params: dict[str, Any]) -> list[Any]:
        try:
            result = await db.execute(text(sql), params)
            return result.mappings().all()
        except Exception as exc:
            await db.rollback()
            diagnostics.append({"section": label, "status": "unavailable", "reason": str(exc)[:240]})
            return []

    card_rows = await safe_query(
        "card",
        """
        SELECT go100_card_id, strategy_name, description, strategy_type, card_status,
               is_active, is_live, allocated_amount, max_stocks, condition_code,
               entry_rules::text AS entry_rules, exit_rules::text AS exit_rules,
               risk_params::text AS risk_params, last_backtest_return,
               last_backtest_mdd, last_backtest_sharpe, last_backtest_at, updated_at
        FROM go100_strategy_cards
        WHERE go100_card_id = :card_id
          AND user_id = :user_id
          AND card_status != 'RETIRED'
        """,
        {"card_id": card_id, "user_id": user_id},
    )
    card_row = card_rows[0] if card_rows else None
    if not card_row:
        raise HTTPException(status_code=404, detail="카드를 찾을 수 없거나 접근 권한이 없습니다.")

    checked_at = datetime.now(KST).isoformat()
    today_filter = "(created_at AT TIME ZONE 'Asia/Seoul')::date = (NOW() AT TIME ZONE 'Asia/Seoul')::date"

    order_rows = await safe_query(
        "live_orders_today",
        f"""
        SELECT COUNT(*) AS total_orders,
               COUNT(*) FILTER (WHERE UPPER(COALESCE(status, '')) IN ('FILLED', 'PARTIAL_FILLED', 'EXECUTED', 'DONE')) AS filled_orders,
               MAX(created_at) AS last_order_at
        FROM go100_live_orders
        WHERE card_id = :card_id AND {today_filter}
        """,
        {"card_id": card_id},
    )
    order_summary = order_rows[0] if order_rows else {}

    position_rows = await safe_query(
        "open_positions",
        """
        SELECT COUNT(*) AS open_positions
        FROM go100_positions
        WHERE card_id = :card_id AND UPPER(COALESCE(status, '')) = 'OPEN'
        """,
        {"card_id": card_id},
    )
    position_summary = position_rows[0] if position_rows else {}

    decision_rows = await safe_query(
        "recent_decisions",
        """
        SELECT decision_type, stock_code, reason, confidence, created_at
        FROM go100_autonomous_decisions
        WHERE card_id = :card_id
        ORDER BY created_at DESC
        LIMIT 30
        """,
        {"card_id": card_id},
    )

    backtest_rows = await safe_query(
        "backtest_runs",
        """
        SELECT id, start_date, end_date, total_return, max_drawdown, sharpe_ratio,
               win_rate, total_trades, status, completed_at
        FROM go100_backtest_runs
        WHERE go100_card_id = :card_id
        ORDER BY completed_at DESC NULLS LAST, id DESC
        LIMIT 1
        """,
        {"card_id": card_id},
    )
    latest_backtest = backtest_rows[0] if backtest_rows else None

    event_rows = await safe_query(
        "strategy_events_today",
        """
        SELECT stage, COUNT(*) AS count, MAX(created_at) AS last_at
        FROM go100_strategy_run_events
        WHERE card_id = :card_id
          AND (created_at AT TIME ZONE 'Asia/Seoul')::date = (NOW() AT TIME ZONE 'Asia/Seoul')::date
        GROUP BY stage
        ORDER BY count DESC
        """,
        {"card_id": card_id},
    )

    funnel_counts = {str(row.get("stage") or "unknown"): int(row.get("count") or 0) for row in event_rows}
    total_orders = int(order_summary.get("total_orders") or 0)
    filled_orders = int(order_summary.get("filled_orders") or 0)
    open_positions = int(position_summary.get("open_positions") or 0)
    funnel = [
        {"stage": "candidate_generation", "label": "후보생성", "count": funnel_counts.get("candidate_generation", len(decision_rows)), "source": "events_or_decisions"},
        {"stage": "data_validation", "label": "데이터검증", "count": funnel_counts.get("data_validation", 0), "source": "events"},
        {"stage": "entry_rule", "label": "진입룰", "count": funnel_counts.get("entry_rule", 0), "source": "events"},
        {"stage": "order_attempt", "label": "주문시도", "count": total_orders, "source": "go100_live_orders"},
        {"stage": "filled", "label": "체결", "count": filled_orders, "source": "go100_live_orders"},
        {"stage": "open_position", "label": "보유", "count": open_positions, "source": "go100_positions"},
    ]

    candidates = [
        {
            "code": row.get("stock_code"),
            "name": row.get("stock_code"),
            "stage": row.get("decision_type") or "decision",
            "reason": row.get("reason"),
            "confidence": float(row.get("confidence")) if row.get("confidence") is not None else None,
            "created_at": row.get("created_at").isoformat() if row.get("created_at") else None,
        }
        for row in decision_rows
    ]

    last_event_at = None
    event_times = [row.get("last_at") for row in event_rows if row.get("last_at")]
    if event_times:
        last_event_at = max(event_times).isoformat()
    elif order_summary.get("last_order_at"):
        last_event_at = order_summary["last_order_at"].isoformat()
    elif candidates:
        last_event_at = candidates[0].get("created_at")

    performance = {
        "source": "go100_backtest_runs" if latest_backtest else "card_snapshot",
        "latest_backtest": dict(latest_backtest) if latest_backtest else None,
        "card_snapshot": {
            "total_return": float(card_row["last_backtest_return"]) if card_row["last_backtest_return"] is not None else None,
            "max_drawdown": float(card_row["last_backtest_mdd"]) if card_row["last_backtest_mdd"] is not None else None,
            "sharpe_ratio": float(card_row["last_backtest_sharpe"]) if card_row["last_backtest_sharpe"] is not None else None,
            "last_backtest_at": card_row["last_backtest_at"].isoformat() if card_row["last_backtest_at"] else None,
        },
    }

    return {
        "checked_at": checked_at,
        "card": {
            "id": card_row["go100_card_id"],
            "name": card_row["strategy_name"],
            "description": card_row["description"],
            "trade_engine": card_row["strategy_type"] or "unknown",
            "status": card_row["card_status"],
            "is_active": bool(card_row["is_active"]),
            "is_live": bool(card_row["is_live"]),
            "allocated_amount": float(card_row["allocated_amount"] or 0),
            "max_stocks": card_row["max_stocks"],
            "condition_code": card_row["condition_code"],
            "rules": {
                "entry": card_row["entry_rules"],
                "exit": card_row["exit_rules"],
                "risk": card_row["risk_params"],
            },
        },
        "live_status": {
            "today_orders": total_orders,
            "today_fills": filled_orders,
            "open_positions": open_positions,
            "last_event_at": last_event_at,
            "recent_reasons": [item for item in candidates[:10] if item.get("reason")],
        },
        "performance": performance,
        "funnel": funnel,
        "candidates": candidates,
        "diagnostics": diagnostics,
    }


@router.get("/{card_id}/screen")
async def screen_stocks(
    card_id: int,
    with_signals: bool = Query(False),
    current_user: dict = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """전략카드의 universe_filter로 종목 스크리닝 실행."""
    user_id = await _effective_user_id(current_user, db)
    result = await db.execute(
        text("""
            SELECT go100_card_id, strategy_name, universe_filter, entry_rules, condition_code
            FROM go100_strategy_cards
            WHERE go100_card_id = :card_id
              AND user_id = :user_id
              AND is_active = true
        """),
        {"card_id": card_id, "user_id": user_id},
    )
    card = result.mappings().first()
    if not card:
        raise HTTPException(status_code=404, detail="카드를 찾을 수 없거나 접근 권한이 없습니다.")

    universe_filter = card["universe_filter"] or {}
    entry_rules = card["entry_rules"] or []
    condition_code = (card["condition_code"] or "").strip()
    if isinstance(universe_filter, str):
        try:
            universe_filter = json.loads(universe_filter)
        except json.JSONDecodeError:
            universe_filter = {}
    if isinstance(entry_rules, str):
        try:
            entry_rules = json.loads(entry_rules)
        except json.JSONDecodeError:
            entry_rules = []
    if int(card_id) == 119:
        stocks, live_snapshot_at = await _screen_card119_watch_candidates(db)
        return {
            "ok": 1,
            "card_id": card["go100_card_id"],
            "strategy_name": card["strategy_name"],
            "count": len(stocks),
            "stocks": stocks,
            "screened_at": datetime.now(KST).isoformat(),
            "base_date": datetime.now(KST).date().isoformat(),
            "ohlcv_base_date": datetime.now(KST).date().isoformat(),
            "is_realtime": True,
            "data_source": "go100_card119_candidate_snapshots+stock_price_snapshot",
            "live_snapshot_at": live_snapshot_at,
            "live_missing_count": 0,
            "source": "card119_watch20_live_sources",
            "watch_min_change_pct": 20.0,
            "entry_min_change_pct": 27.0,
        }
    if not isinstance(universe_filter, dict) or not universe_filter:
        return {
            "ok": 0,
            "error": "no_filter",
            "message": "종목선정 조건이 설정되지 않았습니다",
        }

    reference_date = datetime.now(KST).date()
    engine = UniverseEngine()
    candidates = await engine.select_stocks(universe_filter, reference_date, db)
    cache = DataCache()
    await cache.load(db, reference_date)
    source = "universe_only"
    if condition_code:
        condition_table, condition_codes = await get_recent_condition_stock_codes(db, condition_code)
        if condition_table is None:
            logger.info("condition search table missing for card=%s condition_code=%s", card_id, condition_code)
        elif condition_codes:
            allowed_codes = set(condition_codes)
            candidates = [candidate for candidate in candidates if candidate.code in allowed_codes]
            source = "universe+kiwoom"

    stocks = []
    for candidate in candidates:
        info = candidate.info
        ohlcv = cache.get_ohlcv(candidate.code)
        latest = ohlcv[-1] if ohlcv else None
        prev = ohlcv[-2] if len(ohlcv) > 1 else None
        current_price = int(latest.close) if latest else None
        change_rate = None
        if latest and prev and prev.close:
            change_rate = round(((latest.close - prev.close) / prev.close) * 100, 2)
        volume = int(latest.volume) if latest else (info.trade_volume if info else None)

        stocks.append(
            {
                "code": candidate.code,
                "name": info.stock_name if info else candidate.code,
                "market_cap": info.market_cap if info else None,
                "current_price": current_price,
                "change_rate": change_rate,
                "volume": volume,
            }
        )

    stocks.sort(key=lambda item: (item["market_cap"] is None, -(item["market_cap"] or 0), item["code"]))
    if with_signals:
        try:
            stocks = await wait_for(_apply_signal_hits(db, stocks, entry_rules, cache), timeout=5.0)
        except AsyncTimeoutError:
            logger.warning("screen signal evaluation timeout: card_id=%s", card_id)
            stocks = [{**stock, "signal_hit": None} for stock in stocks]
        except Exception as e:
            logger.warning("screen signal evaluation failed: card_id=%s error=%s", card_id, e)
            stocks = [{**stock, "signal_hit": None} for stock in stocks]

        stocks.sort(
            key=lambda item: (
                item.get("signal_hit") is not True,
                item["market_cap"] is None,
                -(item["market_cap"] or 0),
                item["code"],
            )
        )

    # 장중이면 stock_price_snapshot으로 실시간 가격 오버라이드
    is_realtime = False
    live_snapshot_at = None
    live_missing_count = 0
    try:
        now_kst = datetime.now(KST)
        # 스냅샷 수집 시간창(08:00~20:00 KST)과 정합: 장외/주말에는 fallback
        is_market = (
            await is_krx_trading_day_async(now_kst.date(), db)
            and 800 <= (now_kst.hour * 100 + now_kst.minute) <= 2000
        )
        if is_market and stocks:
            codes = [s["code"] for s in stocks]
            snap_result = await db.execute(
                text("""
                    SELECT DISTINCT ON (stock_code)
                           stock_code, price, change_pct, volume,
                           COALESCE(NULLIF(trade_amount, 0), ROUND((price::numeric * COALESCE(volume, 0)::numeric) / 1000000.0, 3)::real) AS trade_amount,
                           market_cap, snapshot_time
                    FROM stock_price_snapshot
                    WHERE stock_code = ANY(:codes)
                      AND (snapshot_time AT TIME ZONE 'Asia/Seoul')::date =
                          (NOW() AT TIME ZONE 'Asia/Seoul')::date
                      AND price > 0
                    ORDER BY stock_code, snapshot_time DESC
                """),
                {"codes": codes},
            )
            snap_map = {r.stock_code: r for r in snap_result.fetchall()}
            if snap_map:
                is_realtime = True
                live_missing_count = max(0, len(stocks) - len(snap_map))
                live_snapshot_at = max(
                    (getattr(snap, "snapshot_time", None) for snap in snap_map.values()),
                    default=None,
                )
                realtime_stocks = []
                for s in stocks:
                    snap = snap_map.get(s["code"])
                    if not snap:
                        continue
                    s["current_price"] = int(snap.price) if snap.price else s["current_price"]
                    s["change_rate"] = round(float(snap.change_pct), 2) if snap.change_pct is not None else s["change_rate"]
                    s["volume"] = int(snap.volume) if snap.volume is not None else s["volume"]
                    s["trade_amount"] = float(snap.trade_amount) if snap.trade_amount is not None else s.get("trade_amount")
                    if snap.market_cap is not None:
                        s["market_cap"] = int(snap.market_cap)
                    realtime_stocks.append(s)
                stocks = realtime_stocks
    except Exception as e:
        logger.warning("screen snapshot overlay failed: %s", e)

    response = {
        "ok": 1,
        "card_id": card["go100_card_id"],
        "strategy_name": card["strategy_name"],
        "count": len(stocks),
        "stocks": stocks,
        "screened_at": datetime.now(KST).isoformat(),
        "base_date": datetime.now(KST).date().isoformat() if is_realtime else reference_date.isoformat(),
        "ohlcv_base_date": reference_date.isoformat(),
        "is_realtime": is_realtime,
        "data_source": "stock_price_snapshot" if is_realtime else "ohlcv_daily",
        "live_snapshot_at": live_snapshot_at.isoformat() if live_snapshot_at else None,
        "live_missing_count": live_missing_count,
    }
    if condition_code:
        response["source"] = source
        response["condition_code"] = condition_code
    return response


@router.put("/{card_id}", response_model=Go100StrategyCardResponse)
async def update_card(
    card_id: int,
    data: Go100StrategyCardUpdate,
    current_user: dict = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """전략 카드 수정."""
    try:
        user_id = await _effective_user_id(current_user, db)
        return await go100_strategy_card_service.update_card(
            user_id, card_id, data, db
        )
    except NotFoundException as e:
        raise HTTPException(status_code=404, detail=str(e))
    except (BusinessLogicException, OwnershipException) as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.patch("/{card_id}/toggle")
async def toggle_card_active(
    card_id: int,
    current_user: dict = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """전략 카드 활성/비활성 토글. 활성화 전 데이터 준비도와 조건 미흡 사유를 반환."""
    user_id = await _effective_user_id(current_user, db)
    result = await db.execute(
        text("""
            SELECT go100_card_id, strategy_name, is_active
            FROM go100_strategy_cards
            WHERE go100_card_id = :card_id AND user_id = :user_id AND card_status != 'RETIRED'
        """),
        {"card_id": card_id, "user_id": user_id},
    )
    card = result.mappings().first()
    if not card:
        raise HTTPException(status_code=404, detail="카드를 찾을 수 없거나 접근 권한이 없습니다.")

    next_active = not bool(card["is_active"])
    if next_active:
        readiness = await data_gate_check_readiness(db, card_id, period_months=3, user_id=user_id)
        if readiness.get("gate_level") == "RED":
            raise HTTPException(
                status_code=400,
                detail={
                    "code": "ACTIVATION_READINESS_RED",
                    "message": "활성화 조건이 부족합니다. 백테스트 실행 조건을 먼저 보완하세요.",
                    "checks": readiness.get("checks", []),
                    "collect_suggestions": readiness.get("collect_suggestions", []),
                    "unsupported_rules": readiness.get("unsupported_rules", []),
                },
            )

    updated = await db.execute(
        text("""
            UPDATE go100_strategy_cards
            SET is_active = :next_active, updated_at = NOW()
            WHERE go100_card_id = :card_id AND user_id = :user_id
            RETURNING go100_card_id, strategy_name, is_active
        """),
        {"next_active": next_active, "card_id": card_id, "user_id": user_id},
    )
    row = updated.mappings().first()
    await db.commit()
    return {
        "go100_card_id": row["go100_card_id"],
        "strategy_name": row["strategy_name"],
        "is_active": row["is_active"],
    }


@router.post("/{card_id}/transition", response_model=Go100StrategyCardResponse)
async def transition_status(
    card_id: int,
    req: Go100StatusTransitionRequest,
    current_user: dict = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """상태 전이."""
    try:
        user_id = await _effective_user_id(current_user, db)
        return await go100_strategy_card_service.transition_status(
            user_id, card_id, req, db
        )
    except NotFoundException as e:
        raise HTTPException(status_code=404, detail=str(e))
    except (BusinessLogicException, OwnershipException) as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.delete("/{card_id}")
async def delete_card(
    card_id: int,
    current_user: dict = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """전략 카드 삭제 (soft delete)."""
    try:
        user_id = await _effective_user_id(current_user, db)
        return await go100_strategy_card_service.delete_card(
            user_id, card_id, db
        )
    except NotFoundException as e:
        raise HTTPException(status_code=404, detail=str(e))
    except (BusinessLogicException, OwnershipException) as e:
        raise HTTPException(status_code=400, detail=str(e))


# ─── P1-2: 전략카드 진단 리포트 표준화 ────────────────────────────────────


@router.get("/{card_id}/diagnosis")
async def get_card_diagnosis(
    card_id: int,
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """전략카드 표준 진단 리포트: 카드 상태 + 백테스트 탈락 사유 TOP N + 라이브 주문/결정 로그."""
    import asyncio
    from collections import Counter

    # 1) 카드 기본 정보
    r_card = await db.execute(text("""
        SELECT go100_card_id, strategy_name, card_status, is_active, is_live,
               max_stocks, allocated_amount, last_backtest_return, last_backtest_mdd,
               last_backtest_sharpe, last_backtest_at, created_at, updated_at,
               entry_rules::text, exit_rules::text, risk_params::text
        FROM go100_strategy_cards WHERE go100_card_id = :cid
    """), {"cid": card_id})
    card_row = r_card.mappings().first()
    if not card_row:
        raise HTTPException(status_code=404, detail=f"카드 #{card_id} 없음")

    card_info = {
        "card_id": card_row["go100_card_id"],
        "strategy_name": card_row["strategy_name"],
        "card_status": card_row["card_status"],
        "is_active": card_row["is_active"],
        "is_live": card_row["is_live"],
        "max_stocks": card_row["max_stocks"],
        "allocated_amount": float(card_row["allocated_amount"] or 0),
        "last_backtest_return": float(card_row["last_backtest_return"]) if card_row["last_backtest_return"] else None,
        "last_backtest_mdd": float(card_row["last_backtest_mdd"]) if card_row["last_backtest_mdd"] else None,
        "last_backtest_sharpe": float(card_row["last_backtest_sharpe"]) if card_row["last_backtest_sharpe"] else None,
        "last_backtest_at": card_row["last_backtest_at"].isoformat() if card_row["last_backtest_at"] else None,
    }

    # 2) 최근 백테스트 결과 + decision_audit 탈락 사유
    rejection_top_n: list[dict] = []
    backtest_overview: dict[str, Any] = {"total_runs": 0, "latest": None}
    try:
        r_bt = await db.execute(text("""
            SELECT id, start_date, end_date, initial_capital,
                   total_return, max_drawdown, sharpe_ratio, win_rate,
                   total_trades, status, result_detail::text, completed_at
            FROM go100_backtest_runs
            WHERE go100_card_id = :cid
            ORDER BY completed_at DESC NULLS LAST, id DESC
            LIMIT 5
        """), {"cid": card_id})
        bt_rows = r_bt.mappings().all()
        backtest_overview["total_runs"] = len(bt_rows)
        if bt_rows:
            latest = bt_rows[0]
            backtest_overview["latest"] = {
                "id": latest["id"],
                "period": f"{latest['start_date']}~{latest['end_date']}",
                "total_return": float(latest["total_return"]) if latest["total_return"] else None,
                "max_drawdown": float(latest["max_drawdown"]) if latest["max_drawdown"] else None,
                "sharpe_ratio": float(latest["sharpe_ratio"]) if latest["sharpe_ratio"] else None,
                "win_rate": float(latest["win_rate"]) if latest["win_rate"] else None,
                "total_trades": int(latest["total_trades"] or 0),
                "status": latest["status"],
                "completed_at": latest["completed_at"].isoformat() if latest["completed_at"] else None,
            }
            # decision_audit에서 탈락 사유 추출
            detail_text = latest["result_detail"]
            if detail_text:
                try:
                    detail = json.loads(detail_text) if isinstance(detail_text, str) else detail_text
                    audit = detail.get("decision_audit_summary", {})
                    reason_counts = audit.get("reason_counts", {})
                    if reason_counts:
                        sorted_reasons = sorted(reason_counts.items(), key=lambda x: x[1], reverse=True)
                        rejection_top_n = [
                            {"reason": k, "count": v, "rank": i + 1}
                            for i, (k, v) in enumerate(sorted_reasons[:10])
                        ]
                except (json.JSONDecodeError, TypeError):
                    pass
    except Exception as e:
        backtest_overview["error"] = str(e)

    # 3) 최근 라이브 주문/결정 로그 (최근 7일)
    live_activity: dict[str, Any] = {"orders": [], "decisions": []}
    try:
        r_orders = await db.execute(text("""
            SELECT stock_code, stock_name, side, quantity, price, status, created_at
            FROM go100_live_orders
            WHERE card_id = :cid AND created_at >= NOW() - INTERVAL '7 days'
            ORDER BY created_at DESC LIMIT 10
        """), {"cid": card_id})
        for r in r_orders.mappings().all():
            live_activity["orders"].append({
                "stock_code": r["stock_code"], "stock_name": r.get("stock_name"),
                "side": r["side"], "quantity": int(r["quantity"] or 0),
                "price": float(r["price"] or 0), "status": r["status"],
                "at": r["created_at"].isoformat() if r["created_at"] else None,
            })
    except Exception:
        pass
    try:
        r_dec = await db.execute(text("""
            SELECT decision_type, stock_code, reason, confidence, created_at
            FROM go100_autonomous_decisions
            WHERE card_id = :cid AND created_at >= NOW() - INTERVAL '7 days'
            ORDER BY created_at DESC LIMIT 10
        """), {"cid": card_id})
        for r in r_dec.mappings().all():
            live_activity["decisions"].append({
                "type": r["decision_type"], "stock_code": r.get("stock_code"),
                "reason": r["reason"],
                "confidence": float(r["confidence"]) if r["confidence"] else None,
                "at": r["created_at"].isoformat() if r["created_at"] else None,
            })
    except Exception:
        pass

    # 4) 종합 판정
    observations: list[str] = []
    bt_latest = backtest_overview.get("latest")
    if not bt_latest:
        observations.append("완료된 백테스트가 없어 성과 근거가 부족합니다.")
    elif bt_latest.get("total_trades", 0) == 0:
        observations.append("최근 백테스트 거래 0건 — 진입조건 과강도 또는 유니버스 미통과 가능성.")
    elif (bt_latest.get("total_return") or 0) < 0:
        observations.append(f"최근 백테스트 수익률 음수 ({bt_latest['total_return']:.1f}%).")
    if rejection_top_n:
        top = rejection_top_n[0]
        observations.append(f"탈락 사유 1위: {top['reason']} ({top['count']}건).")
    if not live_activity["orders"] and card_info["is_live"]:
        observations.append("LIVE 카드인데 최근 7일 주문 0건.")
    if card_info["is_active"] and not card_info["is_live"] and card_info["card_status"] not in ("READY", "LIVE"):
        observations.append(f"카드 활성인데 상태가 {card_info['card_status']}로 LIVE 전환 불가.")

    entry_rules_raw = card_row["entry_rules"]
    exit_rules_raw = card_row["exit_rules"]
    entry_count = 0
    exit_count = 0
    try:
        er = json.loads(entry_rules_raw) if isinstance(entry_rules_raw, str) else entry_rules_raw
        entry_count = len(er) if isinstance(er, list) else 0
    except Exception:
        pass
    try:
        xr = json.loads(exit_rules_raw) if isinstance(exit_rules_raw, str) else exit_rules_raw
        exit_count = len(xr) if isinstance(xr, list) else 0
    except Exception:
        pass

    if entry_count == 0:
        observations.append("진입 조건이 비어 있습니다.")
    if exit_count == 0:
        observations.append("청산 조건이 비어 있습니다.")

    try:
        await db.rollback()
        r_kst = await db.execute(text("SELECT NOW() AT TIME ZONE 'Asia/Seoul'"))
        checked_at = r_kst.scalar().strftime("%Y-%m-%d %H:%M:%S KST")
    except Exception:
        from datetime import timedelta
        checked_at = (datetime.utcnow() + timedelta(hours=9)).strftime("%Y-%m-%d %H:%M:%S KST")

    return {
        "checked_at": checked_at,
        "card": card_info,
        "rules": {"entry_count": entry_count, "exit_count": exit_count},
        "backtest": backtest_overview,
        "rejection_reasons_top10": rejection_top_n,
        "live_activity_7d": live_activity,
        "observations": observations,
    }


class BatchDeployRequest(BaseModel):
    account_ids: list[int]
    capital_per_account: Optional[int] = None


@approval_router.get("/{card_id}/deployed-accounts")
async def get_deployed_accounts(
    card_id: int,
    current_user: dict = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """이 전략이 이미 배포된 계좌 목록 (parent_card_id 기준)."""
    user_id = await _effective_user_id(current_user, db)
    result = await db.execute(
        text("""
            SELECT DISTINCT account_id
            FROM go100_strategy_cards
            WHERE (parent_card_id = :card_id OR go100_card_id = :card_id)
              AND account_id IS NOT NULL
              AND user_id = :user_id
        """),
        {"card_id": card_id, "user_id": user_id},
    )
    return {"deployed_account_ids": [r[0] for r in result.fetchall()]}


@approval_router.post("/{card_id}/deploy")
async def batch_deploy_strategy(
    card_id: int,
    req: BatchDeployRequest,
    current_user: dict = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """전략카드를 여러 계좌에 일괄 배포. 단일 트랜잭션, 중복 skip."""
    user_id = await _effective_user_id(current_user, db)

    orig = await db.execute(
        text("SELECT * FROM go100_strategy_cards WHERE go100_card_id = :card_id AND user_id = :user_id"),
        {"card_id": card_id, "user_id": user_id},
    )
    orig_row = orig.mappings().first()
    if not orig_row:
        raise HTTPException(status_code=404, detail="전략 카드를 찾을 수 없습니다.")

    valid_accts = await db.execute(
        text("SELECT account_id FROM accounts WHERE account_id = ANY(:ids) AND is_active = true"),
        {"ids": req.account_ids},
    )
    valid_set = {r[0] for r in valid_accts.fetchall()}
    invalid = [aid for aid in req.account_ids if aid not in valid_set]
    if invalid:
        raise HTTPException(status_code=400, detail=f"존재하지 않는 account_id: {invalid}")

    already = await db.execute(
        text("""
            SELECT DISTINCT account_id FROM go100_strategy_cards
            WHERE (parent_card_id = :card_id OR go100_card_id = :card_id)
              AND account_id = ANY(:ids)
              AND user_id = :user_id
        """),
        {"card_id": card_id, "ids": req.account_ids, "user_id": user_id},
    )
    already_deployed = {r[0] for r in already.fetchall()}

    # 시퀀스가 실제 max(id)보다 뒤처진 경우를 방지
    await db.execute(text("""
        SELECT setval('go100_strategy_cards_go100_card_id_seq',
            GREATEST(
                (SELECT last_value FROM go100_strategy_cards_go100_card_id_seq),
                (SELECT COALESCE(MAX(go100_card_id), 0) FROM go100_strategy_cards) + 1
            ), false)
    """))
    await db.execute(text("""
        SELECT setval('go100_portfolios_portfolio_id_seq',
            GREATEST(
                (SELECT last_value FROM go100_portfolios_portfolio_id_seq),
                (SELECT COALESCE(MAX(portfolio_id), 0) FROM go100_portfolios) + 1
            ), false)
    """))

    initial_cap = req.capital_per_account or int(orig_row["allocated_amount"] or 0)

    created = []
    skipped = []

    for acct_id in req.account_ids:
        if acct_id in already_deployed:
            skipped.append({"account_id": acct_id, "reason": "이미 배포됨"})
            continue

        new_card_res = await db.execute(
            text("""
                INSERT INTO go100_strategy_cards (
                    user_id, account_id, strategy_name, strategy_type, universe_filter,
                    entry_rules, exit_rules, risk_params, strategy_params, allocated_amount,
                    max_stocks, card_status, is_active, is_live, source_type, description,
                    parent_card_id, card_code, card_name, desk_id, situation_code, condition_code,
                    card_version, parent_card_code, relay_order, bar_timeframe, card_type,
                    stage_id, bounce_conditions, trigger_tactic, broker_config, data_requirements,
                    metadata, category, version
                )
                SELECT
                    user_id, :acct_id, strategy_name, strategy_type, universe_filter,
                    entry_rules, exit_rules, risk_params, strategy_params, :capital,
                    max_stocks, card_status, is_active, is_live, source_type, description,
                    go100_card_id, card_code, card_name, desk_id, situation_code, condition_code,
                    card_version, parent_card_code, relay_order, bar_timeframe, card_type,
                    stage_id, bounce_conditions, trigger_tactic, broker_config, data_requirements,
                    metadata, category, version
                FROM go100_strategy_cards
                WHERE go100_card_id = :card_id
                RETURNING go100_card_id
            """),
            {"card_id": card_id, "acct_id": acct_id, "capital": initial_cap},
        )
        new_card_id = new_card_res.scalar_one()

        await db.execute(
            text("""
                INSERT INTO go100_portfolios (
                    user_id, account_id, go100_card_id,
                    initial_capital, current_cash,
                    total_invested, total_eval, is_paper, status, is_live
                ) VALUES (
                    :user_id, :acct_id, :new_card_id,
                    :capital, :capital,
                    0, 0, false, 'ACTIVE', false
                )
            """),
            {
                "user_id": user_id,
                "acct_id": acct_id,
                "new_card_id": new_card_id,
                "capital": initial_cap,
            },
        )
        created.append({"account_id": acct_id, "card_id": new_card_id})

    await db.commit()
    return {
        "created": created,
        "skipped": skipped,
        "total_requested": len(req.account_ids),
    }


@approval_router.post("/{strategy_id}/approve")
async def approve_strategy(
    strategy_id: int,
    body: StrategyApprovalRequest = StrategyApprovalRequest(),
    current_user: dict = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """가설 전략 승격 승인. PENDING_APPROVAL 상태에서만 승인 가능."""
    try:
        user_id = await _effective_user_id(current_user, db)
        approval = await approve_strategy_promotion(
            db,
            hypothesis_id=strategy_id,
            user_id=user_id,
            actor_user_id=user_id,
            reason=body.reason,
        )
        card_id = await create_card_from_hypothesis(db, strategy_id)
        return {
            "ok": True,
            "hypothesis_id": strategy_id,
            "status": "CARD_CREATED" if card_id else approval.get("status"),
            "card_id": card_id,
        }
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except Exception as exc:
        logger.exception("approve_strategy failed: %s", exc)
        raise HTTPException(status_code=500, detail="전략 승격 승인 처리 실패")


# --- Store (마켓플레이스): /api/go100/store ---
store_router = APIRouter(prefix="/api/go100", tags=["GO100 Store"])


@store_router.get("/store")
async def list_store(db: AsyncSession = Depends(get_db)):
    """시스템 전략 마켓플레이스 목록."""
    return await go100_strategy_card_service.list_store(db)


@store_router.get("/store/strategies")
async def list_store_strategies(db: AsyncSession = Depends(get_db)):
    """전략 스토어 목록 반환 (strategy_id, name, description, performance 등)."""
    try:
        result = await db.execute(text("""
            SELECT go100_card_id AS strategy_id, strategy_name AS name,
                   description, strategy_type, source_type,
                   backtest_win_rate, backtest_pnl_pct, backtest_mdd,
                   backtest_sharpe, card_status AS status, created_at
            FROM go100_strategy_cards
            WHERE source_type = 'SYSTEM' AND is_active = true
            ORDER BY backtest_sharpe DESC NULLS LAST, created_at DESC
        """))
        rows = result.mappings().all()
        strategies = []
        for r in rows:
            strategies.append({
                "strategy_id": r["strategy_id"],
                "name": r["name"],
                "description": r["description"],
                "strategy_type": r["strategy_type"],
                "source_type": r["source_type"],
                "performance": {
                    "win_rate": float(r["backtest_win_rate"]) if r["backtest_win_rate"] else None,
                    "pnl_pct": float(r["backtest_pnl_pct"]) if r["backtest_pnl_pct"] else None,
                    "mdd": float(r["backtest_mdd"]) if r["backtest_mdd"] else None,
                    "sharpe": float(r["backtest_sharpe"]) if r["backtest_sharpe"] else None,
                },
                "status": r["status"],
            })
        return {"strategies": strategies, "count": len(strategies)}
    except Exception:
        return {"strategies": [], "count": 0}


@store_router.post("/store/subscribe", response_model=Go100StrategyCardResponse, status_code=201)
async def store_subscribe(
    req: Go100StoreSubscribeRequest,
    current_user: dict = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """시스템 전략 구독 (복제)."""
    try:
        user_id = await _effective_user_id(current_user, db)
        return await go100_strategy_card_service.subscribe_from_store(
            user_id, req, db
        )
    except NotFoundException as e:
        raise HTTPException(status_code=404, detail=str(e))
    except (BusinessLogicException, OwnershipException) as e:
        raise HTTPException(status_code=400, detail=str(e))
