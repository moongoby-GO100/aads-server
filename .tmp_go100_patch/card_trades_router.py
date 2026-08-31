"""전략카드별 실매매/모의매매 이력 및 통계 API."""
import json
import logging
import os
import time
from copy import deepcopy
from datetime import date as _date
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any, Optional

from fastapi import APIRouter, Body, Depends, HTTPException, Query
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.core.database import get_db
from backend.app.core.security_middleware import get_current_user
from backend.app.services.market.krx_calendar import is_weekend
from backend.app.services.go100.strategies.card303_discovery import (
    CARD303_DISCOVERY_LIMIT,
    CARD303_DISCOVERY_MAX_CHANGE_PCT,
    CARD303_DISCOVERY_MIN_CHANGE_PCT,
    CARD303_DISCOVERY_MIN_TRADING_VALUE_KRW,
    CARD303_DISCOVERY_SNAPSHOT_FRESH_MINUTES,
    get_card303_discovery_contract,
)
from backend.app.services.go100.user_utils import get_go100_domain_uid

KST = timezone(datetime.now().astimezone().utcoffset() or __import__("datetime").timedelta(hours=9))
try:
    from zoneinfo import ZoneInfo
    KST = ZoneInfo("Asia/Seoul")
except Exception:
    pass

logger = logging.getLogger(__name__)
router = APIRouter(
    prefix="/api/go100/strategy-cards",
    tags=["GO100 Card Trades"],
)

_WORKBENCH_CACHE_TTL_SEC = 45.0
_WORKBENCH_CACHE_MAX = 128
_WORKBENCH_DB_TIMEOUT_MS = 4500
_WORKBENCH_CACHE: dict[tuple[Any, ...], dict[str, Any]] = {}
_TRADE_VALUE_WINDOWS_CACHE_TTL_SEC = 60.0
_TRADE_VALUE_WINDOWS_CACHE_MAX = 64
_TRADE_VALUE_WINDOWS_CACHE: dict[tuple[Any, ...], dict[str, Any]] = {}
_CARD119_ENTRY_MIN_CHANGE_PCT = 27.0
_CARD119_DISCOVERY_MIN_CHANGE_PCT = 20.0
_CARD119_DISCOVERY_MIN_TRADE_VALUE_KRW = float(
    os.environ.get(
        "GO100_119_RELAXED_MIN_TRADE_VALUE",
        os.environ.get("GO100_CARD119_FAST_LIMIT_MIN_TRADE_VALUE", "100000000"),
    )
)


def _workbench_cache_get(cache_key: tuple[Any, ...]) -> dict[str, Any] | None:
    item = _WORKBENCH_CACHE.get(cache_key)
    if not item:
        return None
    age_sec = time.monotonic() - float(item.get("stored_at", 0.0))
    if age_sec > _WORKBENCH_CACHE_TTL_SEC:
        _WORKBENCH_CACHE.pop(cache_key, None)
        return None
    response = deepcopy(item["response"])
    perf = response.setdefault("performance", {})
    perf.update({"cache_hit": True, "cache_age_sec": round(age_sec, 1), "stale": False})
    return response


def _workbench_cache_set(cache_key: tuple[Any, ...], response: dict[str, Any]) -> None:
    if len(_WORKBENCH_CACHE) >= _WORKBENCH_CACHE_MAX:
        oldest_key = min(_WORKBENCH_CACHE, key=lambda k: _WORKBENCH_CACHE[k].get("stored_at", 0.0))
        _WORKBENCH_CACHE.pop(oldest_key, None)
    _WORKBENCH_CACHE[cache_key] = {"stored_at": time.monotonic(), "response": deepcopy(response)}


def _trade_value_windows_cache_get(cache_key: tuple[Any, ...]) -> dict[str, Any] | None:
    item = _TRADE_VALUE_WINDOWS_CACHE.get(cache_key)
    if not item:
        return None
    age_sec = time.monotonic() - float(item.get("stored_at", 0.0))
    if age_sec > _TRADE_VALUE_WINDOWS_CACHE_TTL_SEC:
        _TRADE_VALUE_WINDOWS_CACHE.pop(cache_key, None)
        return None
    response = deepcopy(item["response"])
    summary = response.setdefault("summary", {})
    summary.update({"cache_hit": True, "cache_age_sec": round(age_sec, 1)})
    return response


def _trade_value_windows_cache_set(cache_key: tuple[Any, ...], response: dict[str, Any]) -> None:
    if len(_TRADE_VALUE_WINDOWS_CACHE) >= _TRADE_VALUE_WINDOWS_CACHE_MAX:
        oldest_key = min(_TRADE_VALUE_WINDOWS_CACHE, key=lambda k: _TRADE_VALUE_WINDOWS_CACHE[k].get("stored_at", 0.0))
        _TRADE_VALUE_WINDOWS_CACHE.pop(oldest_key, None)
    _TRADE_VALUE_WINDOWS_CACHE[cache_key] = {"stored_at": time.monotonic(), "response": deepcopy(response)}


async def _effective_uid(current_user: dict, db: AsyncSession) -> int:
    return await get_go100_domain_uid(db, current_user["user_id"])


@router.get("/{card_id}/trades")
async def get_card_trades(
    card_id: int,
    page: int = Query(1, ge=1),
    size: int = Query(50, ge=1, le=200),
    is_paper: Optional[bool] = Query(None),
    current_user: dict = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """전략카드별 실매매/모의매매 체결 이력 조회."""
    filters = ["t.go100_card_id = :card_id"]
    params: dict[str, Any] = {
        "card_id": card_id,
        "offset": (page - 1) * size,
        "limit": size,
    }
    if is_paper is not None:
        filters.append("t.is_paper = :is_paper")
        params["is_paper"] = is_paper
    where_clause = " AND ".join(filters)

    count_result = await db.execute(
        text(f"SELECT COUNT(*) FROM go100_trades_effective t WHERE {where_clause}"),
        params,
    )
    total = count_result.scalar() or 0

    rows_result = await db.execute(
        text(f"""
            SELECT t.id, t.go100_card_id, t.stock_code,
                   COALESCE(t.stock_name, su.stock_name, t.stock_code) AS stock_name,
                   t.side, t.price, t.quantity, t.amount,
                   t.pnl_amount, t.pnl_pct, t.is_paper, t.trade_date, t.traded_at
            FROM go100_trades_effective t
            LEFT JOIN stock_universe su ON su.stock_code = t.stock_code
            WHERE {where_clause}
            ORDER BY t.traded_at DESC
            OFFSET :offset LIMIT :limit
        """),
        params,
    )
    rows = rows_result.fetchall()

    return {
        "total": total,
        "page": page,
        "size": size,
        "items": [
            {
                "id": r.id,
                "go100_card_id": r.go100_card_id,
                "stock_code": r.stock_code,
                "stock_name": r.stock_name or r.stock_code,
                "side": r.side,
                "price": float(r.price) if r.price else 0,
                "quantity": r.quantity or 0,
                "amount": float(r.amount) if r.amount else 0,
                "pnl_amount": float(r.pnl_amount) if r.pnl_amount is not None else None,
                "pnl_pct": float(r.pnl_pct) if r.pnl_pct is not None else None,
                "is_paper": r.is_paper,
                "trade_date": str(r.trade_date) if r.trade_date else None,
                "traded_at": r.traded_at.isoformat() if r.traded_at else None,
            }
            for r in rows
        ],
    }


@router.get("/{card_id}/trade-stats")
async def get_card_trade_stats(
    card_id: int,
    current_user: dict = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """전략카드별 매매통계 (승률, 손익, 포지션 등). Paper/Real 분리 통계 포함."""
    result = await db.execute(
        text("""
            SELECT
                COUNT(*) AS total_trades,
                COUNT(CASE WHEN side='BUY' THEN 1 END) AS buy_count,
                COUNT(CASE WHEN side='SELL' THEN 1 END) AS sell_count,
                COUNT(CASE WHEN is_paper=false THEN 1 END) AS live_count,
                COUNT(CASE WHEN is_paper=true THEN 1 END) AS paper_count,
                COUNT(CASE WHEN side='SELL' AND pnl_amount>0 THEN 1 END) AS win_count,
                COUNT(CASE WHEN side='SELL' AND pnl_amount<=0 THEN 1 END) AS loss_count,
                SUM(pnl_amount) FILTER (WHERE side='SELL') AS total_pnl,
                AVG(pnl_pct) FILTER (WHERE side='SELL' AND pnl_pct IS NOT NULL) AS avg_pnl_pct,
                MAX(pnl_pct) FILTER (WHERE side='SELL') AS max_pnl_pct,
                MIN(pnl_pct) FILTER (WHERE side='SELL') AS min_pnl_pct,
                MIN(trade_date) AS first_trade_date,
                MAX(trade_date) AS last_trade_date,
                COUNT(DISTINCT stock_code) AS unique_stocks
            FROM go100_trades_effective
            WHERE go100_card_id = :card_id
        """),
        {"card_id": card_id},
    )
    row = result.fetchone()
    if not row or row.total_trades == 0:
        return {"card_id": card_id, "has_data": False}

    sell_total = (row.win_count or 0) + (row.loss_count or 0)
    win_rate = round((row.win_count or 0) / sell_total * 100, 1) if sell_total > 0 else None

    pos_result = await db.execute(
        text("""
            SELECT
                COUNT(CASE WHEN status='OPEN' THEN 1 END) AS open_positions,
                COUNT(CASE WHEN status='CLOSED' THEN 1 END) AS closed_positions,
                SUM(pnl_amount) FILTER (WHERE status='CLOSED') AS realized_pnl
            FROM go100_positions
            WHERE go100_card_id = :card_id
        """),
        {"card_id": card_id},
    )
    pos = pos_result.fetchone()

    def _sub_stats(is_paper_val: bool):
        """Paper/Real 분리 통계 쿼리 헬퍼."""
        return {
            "win_count": 0, "loss_count": 0, "win_rate": None,
            "total_pnl": 0, "avg_pnl_pct": None,
        }

    live_r = await db.execute(
        text("""
            SELECT
                COUNT(CASE WHEN side='SELL' AND pnl_amount>0 THEN 1 END) AS win_count,
                COUNT(CASE WHEN side='SELL' AND pnl_amount<=0 THEN 1 END) AS loss_count,
                SUM(pnl_amount) FILTER (WHERE side='SELL') AS total_pnl,
                AVG(pnl_pct) FILTER (WHERE side='SELL' AND pnl_pct IS NOT NULL) AS avg_pnl_pct
            FROM go100_trades_effective
            WHERE go100_card_id = :card_id AND is_paper = false
        """),
        {"card_id": card_id},
    )
    lr = live_r.fetchone()
    live_sell = (lr.win_count or 0) + (lr.loss_count or 0) if lr else 0

    paper_r = await db.execute(
        text("""
            SELECT
                COUNT(CASE WHEN side='SELL' AND pnl_amount>0 THEN 1 END) AS win_count,
                COUNT(CASE WHEN side='SELL' AND pnl_amount<=0 THEN 1 END) AS loss_count,
                SUM(pnl_amount) FILTER (WHERE side='SELL') AS total_pnl,
                AVG(pnl_pct) FILTER (WHERE side='SELL' AND pnl_pct IS NOT NULL) AS avg_pnl_pct
            FROM go100_trades_effective
            WHERE go100_card_id = :card_id AND is_paper = true
        """),
        {"card_id": card_id},
    )
    pr = paper_r.fetchone()
    paper_sell = (pr.win_count or 0) + (pr.loss_count or 0) if pr else 0

    return {
        "card_id": card_id,
        "has_data": True,
        "total_trades": row.total_trades,
        "buy_count": row.buy_count or 0,
        "sell_count": row.sell_count or 0,
        "live_count": row.live_count or 0,
        "paper_count": row.paper_count or 0,
        "win_count": row.win_count or 0,
        "loss_count": row.loss_count or 0,
        "win_rate": win_rate,
        "total_pnl": float(row.total_pnl) if row.total_pnl else 0,
        "avg_pnl_pct": round(float(row.avg_pnl_pct), 2) if row.avg_pnl_pct is not None else None,
        "max_pnl_pct": round(float(row.max_pnl_pct), 2) if row.max_pnl_pct is not None else None,
        "min_pnl_pct": round(float(row.min_pnl_pct), 2) if row.min_pnl_pct is not None else None,
        "first_trade_date": str(row.first_trade_date) if row.first_trade_date else None,
        "last_trade_date": str(row.last_trade_date) if row.last_trade_date else None,
        "unique_stocks": row.unique_stocks or 0,
        "open_positions": pos.open_positions if pos else 0,
        "closed_positions": pos.closed_positions if pos else 0,
        "realized_pnl": float(pos.realized_pnl) if pos and pos.realized_pnl else 0,
        "live_stats": {
            "win_count": lr.win_count or 0 if lr else 0,
            "loss_count": lr.loss_count or 0 if lr else 0,
            "win_rate": round((lr.win_count or 0) / live_sell * 100, 1) if live_sell > 0 else None,
            "total_pnl": float(lr.total_pnl) if lr and lr.total_pnl else 0,
            "avg_pnl_pct": round(float(lr.avg_pnl_pct), 2) if lr and lr.avg_pnl_pct is not None else None,
        },
        "paper_stats": {
            "win_count": pr.win_count or 0 if pr else 0,
            "loss_count": pr.loss_count or 0 if pr else 0,
            "win_rate": round((pr.win_count or 0) / paper_sell * 100, 1) if paper_sell > 0 else None,
            "total_pnl": float(pr.total_pnl) if pr and pr.total_pnl else 0,
            "avg_pnl_pct": round(float(pr.avg_pnl_pct), 2) if pr and pr.avg_pnl_pct is not None else None,
        },
    }


# ── 6-Stage Trading Workbench ─────────────────────────────────────────────────

def _ts(v: Any) -> Optional[str]:
    """datetime/date → ISO string; None passthrough."""
    if v is None:
        return None
    if hasattr(v, "isoformat"):
        return v.isoformat()
    return str(v)


def _num(v: Any) -> Any:
    """Decimal → float; None passthrough."""
    if v is None:
        return None
    if isinstance(v, Decimal):
        return float(v)
    return v


def _row_dict(r: Any) -> dict[str, Any]:
    """SQLAlchemy Row/RowMapping -> plain dict with serialisable values."""
    mapping = getattr(r, "_mapping", r)
    return {k: _ts(v) if hasattr(v, "isoformat") else _num(v) for k, v in dict(mapping).items()}


def _normalize_stock_code(v: Any) -> str:
    raw = "" if v is None else str(v).strip()
    digits = "".join(ch for ch in raw if ch.isdigit())
    if len(digits) >= 6:
        return digits[-6:]
    return raw


def _name_payload(stock_code: Any, stock_name: Any) -> dict[str, Any]:
    code = _normalize_stock_code(stock_code)
    name = "" if stock_name is None else str(stock_name).strip()
    missing = not name or name == code or name == str(stock_code or "").strip()
    display = name if not missing else code
    return {
        "stock_code": code or str(stock_code or ""),
        "stock_name": display,
        "display_name": display,
        "stock_name_missing": missing,
    }


def _safe_float(v: Any) -> Optional[float]:
    if v is None:
        return None
    try:
        return float(v)
    except Exception:
        return None


def _exit_result(pnl_amount: Any, pnl_pct: Any = None) -> str:
    pnl = _safe_float(pnl_amount)
    if pnl is None:
        pnl = _safe_float(pnl_pct)
    if pnl is None:
        return "미분류"
    if pnl > 0:
        return "익절"
    if pnl < 0:
        return "손절"
    return "보합"


def _extract_first_pct(text_value: Any) -> Optional[float]:
    text_value = "" if text_value is None else str(text_value)
    token = ""
    for ch in text_value:
        if ch.isdigit() or ch in ".+-":
            token += ch
        elif token:
            if "%" in text_value[text_value.find(token):text_value.find(token) + len(token) + 2]:
                return _safe_float(token)
            token = ""
    return None


def _normalize_trading_value_krw(
    raw_value: Any,
    *,
    price: Any = None,
    volume: Any = None,
) -> Optional[float]:
    """Return current-day cumulative trade value in KRW.

    Some realtime snapshots store trade_amount in compressed market units while
    volume is cumulative. Infer the KRW scale against price * volume so the UI
    never displays a same-day cumulative value as 0.0억.
    """
    value = _safe_float(raw_value)
    price_value = _safe_float(price)
    volume_value = _safe_float(volume)
    expected = None
    if price_value and volume_value and price_value > 0 and volume_value > 0:
        expected = price_value * volume_value

    if value is None or value <= 0:
        return expected
    if expected is None or expected <= 0:
        return value

    candidates = (value, value * 1_000.0, value * 1_000_000.0)
    best = min(candidates, key=lambda candidate: abs(candidate - expected))
    # Keep native KRW when it is already close. Otherwise use the inferred market unit.
    if abs(value - expected) <= expected * 0.20:
        return value
    if abs(best - expected) <= expected * 0.20 or value < expected * 0.01:
        return best
    return value


def _sum_trade_values(*values: Any) -> Optional[float]:
    total = 0.0
    has_value = False
    for value in values:
        numeric = _safe_float(value)
        if numeric is None or numeric <= 0:
            continue
        total += numeric
        has_value = True
    return total if has_value else None


async def _enrich_stocks_with_live_data(
    db: AsyncSession,
    stock_codes: list[str],
    stale_threshold_sec: float = 420.0,
) -> "dict[str, dict[str, Any]]":
    """Fetch live market data from stock_price_snapshot -> v4_ohlcv_minute fallback.
    Returns dict keyed by stock_code with fields:
    current_price, change_rate_pct, volume, trading_value_krw, quote_time,
    quote_age_sec, freshness_status, upper_limit_price, distance_to_limit_price,
    distance_to_limit_pct, data_source, missing_reason, prev_close, stock_name.
    """
    if not stock_codes:
        return {}
    codes = list({str(c) for c in stock_codes if c})
    result: dict[str, dict[str, Any]] = {}

    nxt_eligible: dict[str, bool] = {}
    try:
        nxt_r = await db.execute(
            text("""
                SELECT stock_code, COALESCE(is_nxt, false) AS is_nxt
                FROM stock_universe
                WHERE stock_code = ANY(:codes)
            """),
            {"codes": codes},
        )
        nxt_eligible = {str(row.stock_code): bool(row.is_nxt) for row in nxt_r.fetchall()}
    except Exception:
        nxt_eligible = {}

    # 1. Query stock_price_snapshot (latest per stock)
    try:
        snap_r = await db.execute(
            text("""
                SELECT DISTINCT ON (s.stock_code)
                    s.stock_code,
                    COALESCE(su.stock_name, s.stock_code) AS stock_name,
                    s.price              AS current_price,
                    s.change_pct         AS change_rate_pct,
                    s.volume             AS volume,
                    s.trade_amount       AS trading_value_krw,
                    s.snapshot_time      AS quote_time,
                    EXTRACT(EPOCH FROM (NOW() - s.snapshot_time)) AS quote_age_sec
                FROM stock_price_snapshot s
                LEFT JOIN stock_universe su ON su.stock_code = s.stock_code
                WHERE s.stock_code = ANY(:codes)
                ORDER BY s.stock_code, s.snapshot_time DESC
            """),
            {"codes": codes},
        )
        snap_rows: dict[str, dict] = {}
        for row in snap_r.fetchall():
            current_price = _safe_float(row.current_price)
            volume = int(row.volume or 0) if row.volume is not None else None
            market_trade_value = _normalize_trading_value_krw(
                row.trading_value_krw, price=current_price, volume=volume,
            )
            snap_rows[row.stock_code] = {
                "stock_name": row.stock_name,
                "current_price": current_price,
                "change_rate_pct": _safe_float(row.change_rate_pct),
                "volume": volume,
                "trading_value_krw": market_trade_value,
                "market_trading_value_krw": market_trade_value,
                "market_trading_value_source": "snapshot_cumulative",
                "trading_value_source": "snapshot_cumulative",
                "quote_time": row.quote_time.isoformat() if row.quote_time else None,
                "quote_age_sec": _safe_float(row.quote_age_sec),
            }
    except Exception:
        snap_rows = {}

    # Determine which need OHLCV fallback
    need_fallback = [c for c in codes if c not in snap_rows or (snap_rows[c].get("quote_age_sec") or 99999) > stale_threshold_sec]

    # 2. Query v4_ohlcv_minute + ohlcv_daily for fallback
    minute_rows: dict[str, dict] = {}
    if need_fallback:
        try:
            today_kst = datetime.now(KST).date()
            min_r = await db.execute(
                text("""
                    WITH latest AS (
                        SELECT DISTINCT ON (m.stock_code)
                            m.stock_code,
                            COALESCE(su.stock_name, m.stock_code) AS stock_name,
                            m.close_price AS current_price,
                            m.trade_time  AS quote_time
                        FROM v4_ohlcv_minute m
                        LEFT JOIN stock_universe su ON su.stock_code = m.stock_code
                        WHERE m.stock_code = ANY(:codes) AND m.trade_date = :today
                        ORDER BY m.stock_code, m.trade_time DESC
                    ),
                    intraday AS (
                        SELECT m.stock_code,
                               SUM(COALESCE(m.volume, 0))::bigint AS cumulative_volume,
                               SUM(
                                   COALESCE(
                                       NULLIF(m.trade_amount, 0)::numeric,
                                       COALESCE(m.close_price, 0)::numeric * COALESCE(m.volume, 0)::numeric
                                   )
                               ) AS trading_value_krw
                        FROM v4_ohlcv_minute m
                        WHERE m.stock_code = ANY(:codes) AND m.trade_date = :today
                        GROUP BY m.stock_code
                    ),
                    prev_date AS (
                        SELECT MAX(date) AS date
                        FROM ohlcv_daily
                        WHERE date < to_char(CAST(:today_date AS date), 'YYYYMMDD')
                    ),
                    prev AS (
                        SELECT stock_code, close AS prev_close
                        FROM ohlcv_daily
                        WHERE date = (SELECT date FROM prev_date)
                          AND stock_code = ANY(:codes)
                    )
                    SELECT l.stock_code, l.stock_name, l.current_price,
                           i.cumulative_volume AS volume, l.quote_time, p.prev_close,
                           CASE WHEN COALESCE(p.prev_close, 0) > 0
                                THEN ROUND((((l.current_price / p.prev_close) - 1) * 100)::numeric, 2)
                                ELSE NULL END AS change_rate_pct,
                           i.trading_value_krw AS trading_value_krw
                    FROM latest l
                    LEFT JOIN intraday i USING (stock_code)
                    LEFT JOIN prev p USING (stock_code)
                """),
                {"codes": need_fallback, "today": today_kst, "today_date": today_kst},
            )
            for row in min_r.fetchall():
                minute_rows[row.stock_code] = {
                    "stock_name": row.stock_name,
                    "current_price": _safe_float(row.current_price),
                    "change_rate_pct": _safe_float(row.change_rate_pct),
                    "volume": int(row.volume or 0) if row.volume is not None else None,
                    "trading_value_krw": _safe_float(row.trading_value_krw),
                    "market_trading_value_krw": _safe_float(row.trading_value_krw),
                    "market_trading_value_source": "ohlcv_minute_cumulative",
                    "trading_value_source": "ohlcv_minute_cumulative",
                    "quote_time": row.quote_time.isoformat() if row.quote_time else None,
                    "quote_age_sec": None,
                    "prev_close": _safe_float(row.prev_close),
                }
        except Exception:
            pass

    # 3. Query source-split tick cumulative values when available. Today there may
    # be no NXT source rows; keep that as explicit "not collected" instead of 0.
    source_trade_rows: dict[str, dict[str, Any]] = {}
    try:
        source_r = await db.execute(
            text("""
                WITH tick_source AS (
                    SELECT stock_code, tick_time, price, cum_volume, source
                    FROM go100_tick_data
                    WHERE stock_code = ANY(:codes)
                      AND (tick_time AT TIME ZONE 'Asia/Seoul')::date = (NOW() AT TIME ZONE 'Asia/Seoul')::date
                      AND COALESCE(source, '') <> ''
                ),
                ranked AS (
                    SELECT stock_code,
                           UPPER(COALESCE(source, '')) AS source_name,
                           price,
                           cum_volume,
                           tick_time,
                           ROW_NUMBER() OVER (
                               PARTITION BY stock_code, UPPER(COALESCE(source, ''))
                               ORDER BY tick_time DESC
                           ) AS rn
                    FROM tick_source
                )
                SELECT stock_code, source_name,
                       (ABS(COALESCE(price, 0)::numeric) * COALESCE(cum_volume, 0)::numeric) AS trading_value_krw,
                       tick_time
                FROM ranked
                WHERE rn = 1
            """),
            {"codes": codes},
        )
        for row in source_r.fetchall():
            code = str(row.stock_code)
            source_name = str(row.source_name or "").upper()
            source_trade_rows.setdefault(code, {})
            source_payload = {
                "value": _safe_float(row.trading_value_krw),
                "source": source_name.lower(),
                "quote_time": row.tick_time.isoformat() if row.tick_time else None,
            }
            if source_name in {"NXT", "NEXTTRADE"}:
                source_trade_rows[code]["nxt"] = source_payload
            elif source_name in {"KIS", "KIWOOM", "KRX", "MKT", "MXT"}:
                source_trade_rows[code]["market"] = source_payload
    except Exception:
        source_trade_rows = {}

    # 4. Merge and compute derived fields
    for code in codes:
        snap = snap_rows.get(code)
        minute = minute_rows.get(code)
        split_trade = source_trade_rows.get(code, {})

        if snap and (snap.get("quote_age_sec") or 99999) <= stale_threshold_sec:
            data: dict[str, Any] = {**snap, "data_source": "snapshot", "missing_reason": None}
        elif minute:
            data = {**minute, "data_source": "ohlcv_minute"}
            if snap:
                data["missing_reason"] = f"스냅샷 {int(snap.get('quote_age_sec') or 0)}초 지연"
            else:
                data["missing_reason"] = "스냅샷 없음"
        elif snap:
            data = {**snap, "data_source": "stale_snapshot",
                    "missing_reason": f"스냅샷 {int(snap.get('quote_age_sec') or 0)}초 전 (지연)"}
        else:
            data = {
                "stock_code": code, "stock_name": None,
                "current_price": None, "change_rate_pct": None,
                "volume": None, "trading_value_krw": None,
                "market_trading_value_krw": None,
                "market_trading_value_source": None,
                "quote_time": None, "quote_age_sec": None,
                "data_source": "missing", "missing_reason": "시세 데이터 없음",
            }

        is_nxt = bool(nxt_eligible.get(code))
        market_split = split_trade.get("market") or {}
        nxt_split = split_trade.get("nxt") or {}
        market_trade_value = _safe_float(data.get("market_trading_value_krw"))
        if market_trade_value is None:
            market_trade_value = _safe_float(market_split.get("value"))
        if market_trade_value is None:
            market_trade_value = _safe_float(data.get("trading_value_krw"))
        nxt_trade_value = _safe_float(nxt_split.get("value"))
        total_trade_value = _sum_trade_values(market_trade_value, nxt_trade_value)

        data["is_nxt"] = is_nxt
        data["market_trading_value_krw"] = market_trade_value
        data["market_trading_value_source"] = (
            data.get("market_trading_value_source")
            or market_split.get("source")
            or data.get("trading_value_source")
        )
        data["market_trading_value_quote_time"] = market_split.get("quote_time") or data.get("quote_time")
        data["nxt_trading_value_krw"] = nxt_trade_value
        data["nxt_trading_value_source"] = (
            nxt_split.get("source")
            if nxt_trade_value is not None
            else ("nxt_not_collected" if is_nxt else None)
        )
        data["nxt_trading_value_quote_time"] = nxt_split.get("quote_time")
        data["total_trading_value_krw"] = total_trade_value
        data["trading_value_krw"] = total_trade_value
        data["trading_value_source"] = (
            "market_nxt_sum"
            if nxt_trade_value is not None
            else data.get("market_trading_value_source")
            or data.get("trading_value_source")
        )

        # Derived: upper_limit_price
        price = _safe_float(data.get("current_price"))
        pct = _safe_float(data.get("change_rate_pct"))
        if price and pct is not None and pct > 0:
            prev_close = price / (1.0 + pct / 100.0)
            upper = round(prev_close * 1.299)
            data["upper_limit_price"] = upper
            data["distance_to_limit_price"] = round(upper - price)
            data["distance_to_limit_pct"] = round((upper - price) / price * 100, 2)
        else:
            data["upper_limit_price"] = None
            data["distance_to_limit_price"] = None
            data["distance_to_limit_pct"] = None

        # Freshness status
        age = _safe_float(data.get("quote_age_sec"))
        if age is None:
            if data["data_source"] == "ohlcv_minute":
                data["freshness_status"] = "ok"
            else:
                data["freshness_status"] = "missing"
        elif age <= 30:
            data["freshness_status"] = "fresh"
        elif age <= stale_threshold_sec:
            data["freshness_status"] = "ok"
        else:
            data["freshness_status"] = "stale"

        result[code] = data

    return result


def _stage2_score_rows(
    rows: list[Any],
    live_data: "dict[str, dict[str, Any]] | None" = None,
    strategy_type: str = "limitup_chase",
) -> "list[dict[str, Any]]":
    """Score and enrich Stage-2 buy-watch candidate rows.

    live_data keyed by stock_code with pre-fetched market metrics.
    score_breakdown returned as dict (keys: momentum, trade_value, freshness, session_evidence)
    so the frontend keyLabel map renders Korean labels correctly.
    """
    scored: list[dict[str, Any]] = []
    _ld = live_data or {}
    for r in rows:
        row = _row_dict(r)
        row.update(_name_payload(row.get("stock_code"), row.get("stock_name")))
        code = str(row.get("stock_code") or "")
        live = _ld.get(code, {})

        # Merge live market fields (prefer live over event row)
        for field in (
            "current_price", "change_rate_pct", "volume", "trading_value_krw",
            "quote_time", "quote_age_sec", "freshness_status",
            "upper_limit_price", "distance_to_limit_price", "distance_to_limit_pct",
            "data_source", "missing_reason",
        ):
            if live.get(field) is not None and row.get(field) is None:
                row[field] = live[field]
        if live.get("stock_name") and row.get("stock_name_missing"):
            row["stock_name"] = live["stock_name"]
            row["display_name"] = live["stock_name"]
            row["stock_name_missing"] = False

        reason = str(row.get("reason_text") or "")
        decision = str(row.get("decision") or "").lower()
        metrics = _parse_json_field(row.get("metrics_json"))
        if not isinstance(metrics, dict):
            metrics = {}
        pass_reasons: list[str] = []
        fail_reasons: list[str] = []
        missing_data: list[str] = []
        score_breakdown: dict[str, Any] = {}
        total = 0.0

        # Hard gate
        hard_gate = 35.0 if decision == "pass" else -50.0
        total += hard_gate
        (pass_reasons if decision == "pass" else fail_reasons).append(
            "하드게이트 통과" if decision == "pass" else f"하드게이트 미통과({decision or 'unknown'})"
        )
        score_breakdown["hard_gate"] = {"score": hard_gate, "max": 35, "label": "하드게이트"}

        # Momentum (change_rate_pct)
        momentum_pct = _safe_float(
            live.get("change_rate_pct")
            or row.get("momentum_pct") or row.get("change_pct") or row.get("fluctuation_rate")
        )
        if momentum_pct is None:
            momentum_pct = _extract_first_pct(reason)
        if momentum_pct is None:
            missing_data.append("momentum_pct")
            momentum_score = -8.0
        else:
            if strategy_type == 'limitup_chase':
                momentum_score = max(0.0, min(30.0, (momentum_pct - 20.0) * 3.0))
                if momentum_pct >= 25:
                    pass_reasons.append(f"모멘텀 {momentum_pct:.1f}% (상한가 근접)")
                elif momentum_pct >= 20:
                    pass_reasons.append(f"모멘텀 {momentum_pct:.1f}%")
                else:
                    fail_reasons.append(f"모멘텀 부족 {momentum_pct:.1f}% (<20%)")
            else:
                momentum_score = min(30.0, max(0.0, momentum_pct * 3.0))
                if momentum_pct >= 5:
                    pass_reasons.append(f"모멘텀 {momentum_pct:.1f}%")
                elif momentum_pct >= 0:
                    pass_reasons.append(f"모멘텀 {momentum_pct:.1f}%")
                else:
                    fail_reasons.append(f"모멘텀 부족 {momentum_pct:.1f}%")
        total += momentum_score
        _momentum_label = "등락률/상한가 근접" if strategy_type == 'limitup_chase' else "등락률/모멘텀"
        score_breakdown["momentum"] = {
            "score": round(momentum_score, 1), "max": 30,
            "label": _momentum_label, "value": momentum_pct,
        }

        # Trading value
        trade_value = _safe_float(live.get("trading_value_krw") or row.get("trading_value_krw"))
        if trade_value is not None:
            tv_score = min(15.0, max(0.0, (trade_value / 1_000_000_000.0) * 1.5))
            if trade_value >= 1_000_000_000:
                pass_reasons.append(f"거래대금 {trade_value/1e8:.1f}억원")
            else:
                fail_reasons.append(f"거래대금 부족 {trade_value/1e8:.1f}억원")
        else:
            tv_score = 0.0
            missing_data.append("trading_value_krw")
        total += tv_score
        score_breakdown["trade_value"] = {
            "score": round(tv_score, 1), "max": 15,
            "label": "거래대금", "value": trade_value,
        }

        # Freshness
        age_sec = _safe_float(live.get("quote_age_sec"))
        if age_sec is not None:
            if age_sec <= 30:
                freshness_score = 10.0
                pass_reasons.append(f"데이터 신선 {age_sec:.0f}초")
            elif age_sec <= 120:
                freshness_score = 5.0
                pass_reasons.append(f"데이터 지연 {age_sec:.0f}초")
            else:
                freshness_score = -10.0
                fail_reasons.append(f"데이터 오래됨 {age_sec:.0f}초")
        else:
            # Fallback: try source_ts/received_at from event row
            try:
                src_ts = row.get("source_ts")
                rcv_at = row.get("received_at")
                if src_ts and rcv_at:
                    src = datetime.fromisoformat(str(src_ts).replace("Z", "+00:00"))
                    recv = datetime.fromisoformat(str(rcv_at).replace("Z", "+00:00"))
                    lag = max(0.0, (recv - src).total_seconds())
                    age_sec = lag
                    freshness_score = 10.0 if lag <= 5 else 5.0 if lag <= 30 else -10.0
                    if lag > 30:
                        fail_reasons.append(f"이벤트 지연 {lag:.0f}초")
                    else:
                        pass_reasons.append(f"이벤트 지연 {lag:.1f}초")
                else:
                    freshness_score = -5.0
                    missing_data.append("freshness")
            except Exception:
                freshness_score = -5.0
                missing_data.append("freshness")
        total += freshness_score
        score_breakdown["freshness"] = {
            "score": round(freshness_score, 1), "max": 10,
            "label": "데이터 신선도", "value": age_sec,
        }

        # NXT/preopen/opening evidence
        lower_reason = reason.lower()
        evidence_score = 0.0
        if "nxt" in lower_reason or "preopen" in lower_reason or "opening" in lower_reason or "09:00" in reason:
            evidence_score = 10.0
            pass_reasons.append("NXT/프리오픈/오프닝 증거")
        else:
            missing_data.append("nxt_preopen_opening_evidence")
        total += evidence_score
        score_breakdown["session_evidence"] = {
            "score": round(evidence_score, 1), "max": 10,
            "label": "NXT/프리오픈/오프닝 증거",
        }

        # Buy trigger / distance
        current_price = _safe_float(live.get("current_price") or row.get("current_price"))
        upper_limit_price = _safe_float(live.get("upper_limit_price") or row.get("upper_limit_price"))
        buy_trigger_price = upper_limit_price if strategy_type == 'limitup_chase' else None
        distance_to_trigger_price: Optional[float] = None
        distance_to_trigger_pct: Optional[float] = None
        if buy_trigger_price is not None and current_price and current_price > 0:
            distance_to_trigger_price = round(buy_trigger_price - current_price)
            distance_to_trigger_pct = round((buy_trigger_price - current_price) / current_price * 100, 2)

        # Order readiness
        order_blockers: list[str] = []
        if decision != "pass":
            order_blockers.append("stage_decision_not_pass")
        data_src = str(live.get("data_source") or row.get("data_source") or "")
        if data_src in ("missing", "stale_snapshot"):
            order_blockers.append("stale_or_missing_quote")
        if strategy_type == 'limitup_chase' and momentum_pct is not None and momentum_pct < 20.0:
            order_blockers.append("below_20pct_change")
        if strategy_type != "scalping_pullback" and trade_value is not None and trade_value < 1_000_000_000:
            order_blockers.append("below_min_trade_value")

        _ready_msg = "신호 발생 대기 중 (상한가 근접 조건 충족)" if strategy_type == 'limitup_chase' else "매수 조건 충족 대기 중"
        if strategy_type == "scalping_pullback" and not order_blockers:
            order_readiness = "waiting"
            next_required_action = "진입후보 통과 · 주문 직전 쿨다운/중복/보유한도/일일리스크/계좌현금 게이트 평가 대기"
        elif not order_blockers and total >= 40:
            order_readiness = "ready"
            next_required_action = _ready_msg
        elif not order_blockers:
            order_readiness = "waiting"
            next_required_action = "추가 점수 누적 필요 (현재 통과 기준 40점)"
        else:
            order_readiness = "blocked"
            next_required_action = "차단 사유: " + " | ".join(order_blockers[:3])

        pass_fail_status = (
            "pass" if decision == "pass" and total >= 20
            else "soft_gate_fail" if total >= 0
            else "fail"
        )

        row["total_score"] = round(total, 1)
        row["score_breakdown"] = score_breakdown
        row["pass_fail_status"] = pass_fail_status
        row["pass_reasons"] = pass_reasons
        row["fail_reasons"] = fail_reasons
        row["missing_data"] = missing_data
        row["reason_text_detailed"] = reason or "; ".join(pass_reasons + fail_reasons)
        row["buy_trigger_price"] = buy_trigger_price
        row["distance_to_trigger_price"] = distance_to_trigger_price
        row["distance_to_trigger_pct"] = distance_to_trigger_pct
        row["order_readiness"] = order_readiness
        row["order_blockers"] = order_blockers
        row["next_required_action"] = next_required_action
        row["selection_reason_code"] = row.get("reason_code") or decision or "unknown"
        row["selection_reason"] = reason or "; ".join(pass_reasons + fail_reasons)
        row["gate_fields"] = [
            {
                "key": "today_market_data",
                "label": "당일 데이터 보유",
                "status": "pass" if momentum_pct is not None and trade_value is not None else "blocked",
            },
            {
                "key": "live_quote_valid",
                "label": "실시간 tick/quote 유효",
                "status": "blocked" if "stale_or_missing_quote" in order_blockers else "pass",
                "value": live.get("freshness_status") or row.get("freshness_status"),
            },
            {
                "key": "readiness_gate",
                "label": "이벤트 readiness gate",
                "status": "pass" if decision == "pass" else "blocked",
                "value": row.get("reason_code"),
            },
            {
                "key": "one_minute_pullback",
                "label": "1분봉 눌림 후보",
                "status": "pass" if metrics.get("wave_pattern_detected") is True else "pending",
                "value": metrics.get("wave_current_phase") or metrics.get("wave_phase"),
            },
            {
                "key": "final_order_gates",
                "label": "주문 가능/쿨다운/리스크 제한",
                "status": "blocked" if order_blockers else "pending",
                "value": "live_order_preflight",
            },
        ]
        row["entry_diagnostics"] = {
            key: metrics.get(key)
            for key in (
                "wave_segments", "wave_current_phase", "pullback_low",
                "fixed_wave_peak", "recent_high", "pullback_depth_pct",
                "volume_contraction_ratio", "volume_contraction_status",
                "ma_support_status", "rebound_candle_confirmed",
                "trigger_tactics", "mtf_confirmation", "mtf_consensus",
            )
            if key in metrics
        }
        row["final_order_gates_pending"] = [
            "order_available", "cooldown", "duplicate_order", "position_limit",
            "daily_risk_limit", "account_cash",
        ]
        scored.append(row)

    scored.sort(
        key=lambda x: (x.get("pass_fail_status") == "pass", x.get("total_score") or -999),
        reverse=True,
    )
    for idx, row in enumerate(scored, 1):
        row["priority_rank"] = idx
    return scored


def _max_ts(rows: list, field: str = "created_at") -> Optional[str]:
    times = [r[field] for r in rows if r.get(field)]
    if not times:
        return None
    m = max(times)
    return m.isoformat() if hasattr(m, "isoformat") else str(m)


@router.get("/{card_id}/operations")
async def get_card_operations(
    card_id: int,
    mode: str = Query("all", pattern="^(live|paper|all)$"),
    date_from: Optional[str] = Query(None, description="YYYY-MM-DD (KST)"),
    date_to: Optional[str] = Query(None, description="YYYY-MM-DD (KST)"),
    current_user: dict = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """전략카드 6단계 운영 워크벤치 데이터 (인증 필수, 카드 소유자 전용)."""
    uid = await _effective_uid(current_user, db)

    card_r = await db.execute(
        text("""
            SELECT go100_card_id, strategy_name, max_stocks,
                   risk_params::text AS risk_params,
                   entry_rules::text AS entry_rules,
                   exit_rules::text  AS exit_rules
            FROM go100_strategy_cards
            WHERE go100_card_id = :card_id
              AND user_id = :uid
              AND card_status != 'RETIRED'
        """),
        {"card_id": card_id, "uid": uid},
    )
    card = card_r.mappings().first()
    if not card:
        raise HTTPException(status_code=404, detail="카드를 찾을 수 없거나 접근 권한이 없습니다.")

    today_kst = datetime.now(KST).date()
    try:
        d_from: _date = _date.fromisoformat(date_from) if date_from else today_kst
        d_to: _date = _date.fromisoformat(date_to) if date_to else today_kst
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=f"날짜 형식 오류 (YYYY-MM-DD): {exc}") from exc
    if d_from > d_to:
        raise HTTPException(status_code=422, detail="date_from이 date_to보다 클 수 없습니다.")

    is_paper: Optional[bool] = None
    if mode == "live":
        is_paper = False
    elif mode == "paper":
        is_paper = True

    diagnostics: list[dict[str, Any]] = []

    async def sq(label: str, sql: str, params: dict[str, Any]) -> list:
        try:
            r = await db.execute(text(sql), params)
            return list(r.mappings().all())
        except Exception as exc:
            await db.rollback()
            diagnostics.append({"section": label, "status": "unavailable", "reason": str(exc)[:240]})
            return []

    base: dict[str, Any] = {"card_id": card_id, "d_from": d_from, "d_to": d_to}

    # Backward-compat alias map: legacy stage name → canonical enum
    # canonical: candidate_generation, entry, position_management, exit, review
    _STAGE_ALIASES = {
        "data_quality_gate": "candidate_generation",
        "entry_filter": "entry",
    }

    # S1: Data-quality gate / candidate_generation events (universe coverage)
    s1_rows = await sq("s1_data_quality", """
        SELECT DISTINCT ON (e.stock_code, e.trade_date)
               e.stock_code, COALESCE(su.stock_name, e.stock_code) AS stock_name,
               e.trade_date, e.decision, e.reason_code, e.reason_text, e.created_at
        FROM go100_strategy_run_events e
        LEFT JOIN stock_universe su ON su.stock_code = e.stock_code
        WHERE e.go100_card_id = :card_id
          AND (e.stage IN ('data_quality_gate', 'candidate_generation')
               OR e.event_phase IN ('data_quality_gate', 'candidate_generation'))
          AND e.trade_date BETWEEN :d_from AND :d_to
        ORDER BY e.stock_code, e.trade_date, e.created_at DESC
        LIMIT 200
    """, base)

    # S2: Entry-filter / entry events (buy-watch candidates)
    s2_rows = await sq("s2_entry_filter", """
        SELECT DISTINCT ON (e.stock_code, e.trade_date)
               e.stock_code, COALESCE(su.stock_name, e.stock_code) AS stock_name,
               e.trade_date, e.decision, e.reason_code, e.reason_text, e.created_at
        FROM go100_strategy_run_events e
        LEFT JOIN stock_universe su ON su.stock_code = e.stock_code
        WHERE e.go100_card_id = :card_id
          AND (e.stage IN ('entry_filter', 'entry')
               OR e.event_phase IN ('entry_filter', 'entry'))
          AND e.trade_date BETWEEN :d_from AND :d_to
        ORDER BY e.stock_code, e.trade_date, e.created_at DESC
        LIMIT 200
    """, base)

    # S3: Buy-execute events + live BUY orders
    s3_events = await sq("s3_buy_execute_events", """
        SELECT stock_code, trade_date, decision, reason_code, reason_text, created_at
        FROM go100_strategy_run_events
        WHERE go100_card_id = :card_id
          AND stage = 'buy_execute'
          AND trade_date BETWEEN :d_from AND :d_to
        ORDER BY created_at DESC
        LIMIT 100
    """, base)

    s3_orders = await sq("s3_live_orders_buy", """
        SELECT order_id, stock_code,
               COALESCE(stock_name, stock_code) AS stock_name,
               order_type, quantity, order_price, filled_price,
               filled_quantity, status, side, exit_reason,
               created_at, filled_at
        FROM go100_live_orders
        WHERE card_id = :card_id AND side = 'BUY'
          AND (created_at AT TIME ZONE 'Asia/Seoul')::date BETWEEN :d_from AND :d_to
        ORDER BY created_at DESC
        LIMIT 100
    """, base)

    # S4: Open positions (live)
    s4_live = await sq("s4_positions_open", """
        SELECT p.id, p.stock_code,
               COALESCE(p.stock_name, su.stock_name, p.stock_code) AS stock_name,
               p.quantity, p.remaining_qty, p.entry_price, p.current_price, p.status,
               p.stop_loss_price, p.take_profit_price, p.trailing_pct,
               p.peak_price, p.pnl_amount, p.pnl_pct, p.entry_date, p.updated_at,
               false AS is_paper
        FROM go100_positions p
        LEFT JOIN stock_universe su ON su.stock_code = p.stock_code
        WHERE p.go100_card_id = :card_id AND p.status = 'OPEN'
        ORDER BY p.entry_date DESC, p.id DESC
        LIMIT 50
    """, {"card_id": card_id})

    s4_paper_rows: list = []
    if is_paper is not False:
        s4_paper_rows = await sq("s4_paper_positions_open", """
            SELECT p.position_id AS id, p.stock_code,
                   COALESCE(p.stock_name, p.stock_code) AS stock_name,
                   p.quantity, p.quantity AS remaining_qty,
                   p.avg_price AS entry_price, p.current_price, p.status,
                   NULL AS stop_loss_price, NULL AS take_profit_price,
                   NULL AS trailing_pct, NULL AS peak_price,
                   p.unrealized_pnl AS pnl_amount,
                   p.unrealized_pnl_pct AS pnl_pct,
                   p.entry_date, p.updated_at,
                   true AS is_paper
            FROM go100_paper_positions p
            WHERE p.card_id = :card_id AND p.status = 'OPEN'
            ORDER BY p.entry_date DESC, p.position_id DESC
            LIMIT 50
        """, {"card_id": card_id})

    if is_paper is True:
        s4_combined: list = s4_paper_rows
    elif is_paper is False:
        s4_combined = s4_live
    else:
        s4_combined = list(s4_live) + list(s4_paper_rows)

    # S5: SELL orders + SELL trades
    s5_orders = await sq("s5_live_orders_sell", """
        SELECT order_id, stock_code,
               COALESCE(stock_name, stock_code) AS stock_name,
               quantity, order_price, filled_price, filled_quantity,
               status, exit_reason, created_at, filled_at
        FROM go100_live_orders
        WHERE card_id = :card_id AND side = 'SELL'
          AND (created_at AT TIME ZONE 'Asia/Seoul')::date BETWEEN :d_from AND :d_to
        ORDER BY created_at DESC
        LIMIT 100
    """, base)

    trade_params: dict[str, Any] = {**base}
    trade_where = (
        "t.go100_card_id = :card_id AND t.side = 'SELL'"
        " AND t.trade_date BETWEEN :d_from AND :d_to"
    )
    if is_paper is not None:
        trade_where += " AND t.is_paper = :is_paper"
        trade_params["is_paper"] = is_paper

    s5_trades = await sq("s5_sell_trades", f"""
        SELECT t.id, t.stock_code,
               COALESCE(t.stock_name, su.stock_name, t.stock_code) AS stock_name,
               t.price, t.quantity, t.amount,
               t.pnl_amount, t.pnl_pct, t.is_paper,
               t.trade_date, t.traded_at, t.position_id
        FROM go100_trades_effective t
        LEFT JOIN stock_universe su ON su.stock_code = t.stock_code
        WHERE {trade_where}
        ORDER BY t.traded_at DESC
        LIMIT 100
    """, trade_params)

    # S6: Derived daily review from SELL trades
    s6_stats = await sq("s6_review_derived", f"""
        SELECT
            COUNT(*) FILTER (WHERE t.pnl_amount > 0)  AS win_count,
            COUNT(*) FILTER (WHERE t.pnl_amount <= 0) AS loss_count,
            SUM(t.pnl_amount)                          AS total_pnl,
            AVG(t.pnl_pct)                             AS avg_pnl_pct,
            MAX(t.pnl_pct)                             AS best_pnl_pct,
            MIN(t.pnl_pct)                             AS worst_pnl_pct,
            COUNT(DISTINCT t.stock_code)               AS unique_stocks,
            MAX(t.traded_at)                           AS last_trade_at
        FROM go100_trades_effective t
        WHERE {trade_where}
    """, trade_params)

    # Parse risk / exit thresholds
    def _parse_json(raw: Any) -> Any:
        if not raw or not isinstance(raw, str):
            return {} if not isinstance(raw, (list, dict)) else raw
        try:
            return json.loads(raw)
        except Exception:
            return {}

    risk = _parse_json(card.get("risk_params"))
    if not isinstance(risk, dict):
        risk = {}
    exit_rules_raw = _parse_json(card.get("exit_rules"))
    exit_list = exit_rules_raw if isinstance(exit_rules_raw, list) else []

    def _f(v: Any) -> Optional[float]:
        return float(v) if v is not None else None

    thresholds: dict[str, Any] = {
        "stop_loss_pct": _f(risk.get("stop_loss_pct")),
        "take_profit_pct": _f(risk.get("take_profit_pct")),
        "trailing_stop_pct": _f(risk.get("trailing_stop_pct")),
        "trailing_activate_pct": _f(risk.get("trailing_activate_pct")),
        "max_hold_days": risk.get("max_hold_days"),
        "max_stocks": risk.get("max_stocks") or card.get("max_stocks"),
        "position_size_pct": _f(risk.get("position_size_pct")),
        "per_position_amount": risk.get("per_position_amount"),
        "daily_loss_limit_pct": _f(risk.get("daily_loss_limit_pct")),
    }
    for rule in exit_list:
        if not isinstance(rule, dict):
            continue
        rt = rule.get("type", "")
        if rt == "stop_loss" and thresholds["stop_loss_pct"] is None:
            thresholds["stop_loss_pct"] = _f(rule.get("pct"))
        elif rt in ("profit_target", "take_profit") and thresholds["take_profit_pct"] is None:
            thresholds["take_profit_pct"] = _f(rule.get("pct"))
        elif rt == "trailing_stop" and thresholds["trailing_stop_pct"] is None:
            thresholds["trailing_stop_pct"] = _f(rule.get("pct"))
        elif rt == "holding_days" and thresholds["max_hold_days"] is None:
            thresholds["max_hold_days"] = rule.get("max")

    checked_at = datetime.now(KST).isoformat()

    s1_pass = [r for r in s1_rows if r.get("decision") != "reject"]
    s1_reject = [r for r in s1_rows if r.get("decision") == "reject"]
    s2_pass = [r for r in s2_rows if r.get("decision") == "pass"]
    s2_skip = [r for r in s2_rows if r.get("decision") == "skip"]
    s3_filled = [o for o in s3_orders if str(o.get("status") or "").upper() == "FILLED"]
    s5_filled = [o for o in s5_orders if str(o.get("status") or "").upper() == "FILLED"]

    s6 = dict(s6_stats[0]) if s6_stats else {}
    win_c = int(s6.get("win_count") or 0)
    loss_c = int(s6.get("loss_count") or 0)
    sell_total = win_c + loss_c

    return {
        "card_id": card_id,
        "card_snapshot": {
            "strategy_name": card.get("strategy_name"),
            "max_stocks": thresholds["max_stocks"],
            "thresholds": thresholds,
        },
        "query": {
            "mode": mode,
            "date_from": str(d_from),
            "date_to": str(d_to),
            "checked_at": checked_at,
        },
        "stages": {
            "s1_target": {
                "count": len(s1_rows),
                "pass_count": len(s1_pass),
                "reject_count": len(s1_reject),
                "updated_at": _max_ts(s1_rows),
                "source": "go100_strategy_run_events:data_quality_gate",
                "rows": [_row_dict(r) for r in s1_pass[:30]],
            },
            "s2_watch": {
                "count": len(s2_rows),
                "pass_count": len(s2_pass),
                "skip_count": len(s2_skip),
                "updated_at": _max_ts(s2_rows),
                "source": "go100_strategy_run_events:entry_filter",
                "rows": [_row_dict(r) for r in s2_rows[:50]],
            },
            "s3_buy": {
                "event_count": len(s3_events),
                "order_count": len(s3_orders),
                "filled_count": len(s3_filled),
                "updated_at": _max_ts(s3_orders) or _max_ts(s3_events),
                "source": "go100_live_orders(BUY)+go100_strategy_run_events:buy_execute",
                "orders": [_row_dict(o) for o in s3_orders[:50]],
            },
            "s4_position": {
                "open_count": len(s4_combined),
                "updated_at": _max_ts(s4_combined, "updated_at"),
                "source": "go100_positions+go100_paper_positions",
                "rows": [_row_dict(p) for p in s4_combined],
            },
            "s5_exit": {
                "order_count": len(s5_orders),
                "filled_count": len(s5_filled),
                "trade_count": len(s5_trades),
                "updated_at": _max_ts(s5_trades, "traded_at") or _max_ts(s5_orders),
                "source": "go100_live_orders(SELL)+go100_trades(SELL)",
                "orders": [_row_dict(o) for o in s5_orders[:50]],
                "trades": [_row_dict(t) for t in s5_trades[:50]],
            },
            "s6_review": {
                "derived": True,
                "source": "go100_trades:SELL (도출됨 — 매수 포지션과 자동 연결 없음)",
                "updated_at": checked_at,
                "mode_filter": mode,
                "summary": {
                    "win_count": win_c,
                    "loss_count": loss_c,
                    "win_rate": round(win_c / sell_total * 100, 1) if sell_total > 0 else None,
                    "total_pnl": float(s6.get("total_pnl") or 0),
                    "avg_pnl_pct": round(float(s6["avg_pnl_pct"]), 2) if s6.get("avg_pnl_pct") is not None else None,
                    "best_pnl_pct": round(float(s6["best_pnl_pct"]), 2) if s6.get("best_pnl_pct") is not None else None,
                    "worst_pnl_pct": round(float(s6["worst_pnl_pct"]), 2) if s6.get("worst_pnl_pct") is not None else None,
                    "unique_stocks": int(s6.get("unique_stocks") or 0),
                    "last_trade_at": _ts(s6.get("last_trade_at")),
                },
            },
        },
        "diagnostics": diagnostics,
    }


# ---------------------------------------------------------------------------
# ── 전략 타입 감지 + 동적 컬럼 정의 ────────────────────────────────────


def _detect_strategy_type(card_row) -> str:
    """카드의 전략 유형을 판별."""
    raw_strategy_type = getattr(card_row, 'strategy_type', '')
    raw_name = getattr(card_row, 'strategy_name', None) or getattr(card_row, 'card_name', '')
    st = raw_strategy_type if isinstance(raw_strategy_type, str) else ''
    name = raw_name if isinstance(raw_name, str) else ''
    if (
        '상따' in name
        or '상한가' in name
        or 'limitup' in st.lower()
        or 'limit_up' in st.lower()
        or 'limit-up' in st.lower()
        or 'limitup' in name.lower()
        or 'limit_up' in name.lower()
    ):
        return 'limitup_chase'
    if (
        '눌림' in name
        or '스캘핑' in name
        or 'scalping' in st.lower()
        or 'pullback' in st.lower()
        or 'scalping' in name.lower()
        or 'pullback' in name.lower()
    ):
        return 'scalping_pullback'
    return 'default'


_STAGE1_COLUMNS: dict[str, list[dict]] = {
    'limitup_chase': [
        {"key": "upper_limit_price", "label": "상한가", "type": "price"},
        {"key": "distance_to_limit_pct", "label": "잔여거리", "type": "pct_with_price",
         "price_key": "distance_to_limit_price"},
        {"key": "total_trading_value_krw", "label": "누적합계", "type": "trade_value"},
    ],
    'scalping_pullback': [
        {"key": "change_rate_pct", "label": "등락률", "type": "pct"},
        {"key": "volume", "label": "거래량", "type": "number"},
        {"key": "total_trading_value_krw", "label": "누적합계", "type": "trade_value"},
        {"key": "market_trading_value_krw", "label": "KRX거래대금", "type": "trade_value"},
        {"key": "nxt_trading_value_krw", "label": "NXT거래대금", "type": "trade_value"},
    ],
    'default': [
        {"key": "change_rate_pct", "label": "등락률", "type": "pct"},
        {"key": "total_trading_value_krw", "label": "누적합계", "type": "trade_value"},
    ],
}

_STAGE2_COLUMNS: dict[str, list[dict]] = {
    'limitup_chase': [
        {"key": "buy_trigger_price", "label": "매수트리거(상한가)", "type": "price"},
    ],
    'scalping_pullback': [
        {"key": "buy_trigger_price", "label": "매수트리거", "type": "price"},
    ],
    'default': [
        {"key": "buy_trigger_price", "label": "매수트리거", "type": "price"},
    ],
}


# Workbench endpoint — 6-stage trading operations view
# ---------------------------------------------------------------------------

def _parse_json_field(raw: Any) -> Any:
    """DB에서 온 jsonb 컬럼을 파싱. dict/list는 그대로, str이면 파싱."""
    if raw is None:
        return {}
    if isinstance(raw, (dict, list)):
        return raw
    try:
        return json.loads(raw)
    except Exception:
        return {}


def _build_card303_strategy_definition(
    *,
    card_id: int,
    strategy_type: str,
    thresholds: dict[str, Any],
    trigger_tactic: Any,
) -> dict[str, Any] | None:
    """API-visible contract that mirrors the current #303 live engine."""
    if int(card_id) != 303 or strategy_type != "scalping_pullback":
        return None
    return {
        "contract_version": "card303-full-sync-v5-20260827",
        "discovery": {
            **get_card303_discovery_contract(),
            "status": "implemented",
            "filter": "configured change range AND cumulative trading-value rank",
            "data_source": "stock_price_snapshot + collector shard DB",
        },
        "selection": {
            "status": "implemented",
            "reason_fields": ["selection_reason_code", "selection_reason"],
            "gate_fields": [
                "today_market_data", "live_quote_valid", "readiness_gate",
                "one_minute_pullback", "order_available", "cooldown",
                "position_limit", "daily_risk_limit", "account_cash",
            ],
            "note": "주문 직전 게이트는 Stage 2에서 pending/blocked/pass로 분리 표시",
        },
        "entry": {
            "status": "implemented",
            "primary_timeframe": "1m",
            "auxiliary_timeframes": ["3m", "5m"],
            "diagnostics": [
                "wave_1_2_3", "pullback_low", "fixed_wave_peak", "recent_high",
                "pullback_depth_pct", "volume_contraction", "ma_support",
                "rebound_candle", "trigger_tactics", "mtf_consensus",
            ],
            "trigger_tactics": _parse_json_field(trigger_tactic),
        },
        "exit": {
            "status": "implemented_with_fallback",
            "primary": [
                "pullback_low_stop", "fixed_wave_peak_target",
                "recent_peak_reversal_with_volume_dryup",
            ],
            "fallback": {
                "take_profit_pct": thresholds.get("take_profit_pct"),
                "stop_loss_pct": thresholds.get("stop_loss_pct"),
                "trailing_stop_pct": thresholds.get("trailing_stop_pct"),
            },
            "note": "파동 기준과 고정 손익 기준을 별도 경로로 기록",
        },
    }


def _build_card119_strategy_definition(
    *,
    card_id: int,
    strategy_type: str,
    thresholds: dict[str, Any],
) -> dict[str, Any] | None:
    """API-visible contract that mirrors the current #119 live engine."""
    if int(card_id) != 119 or strategy_type != "limitup_chase":
        return None
    return {
        "contract_version": "card119-independent-discovery-v14-20260826",
        "discovery": {
            "status": "implemented",
            "filter": "preopen expected_change_rate >= 27.0 OR intraday change_pct >= 27.0 AND trading_value_krw >= 100,000,000",
            "sort": "lock_score priority, trade_value_krw DESC, change_pct DESC, stock_code ASC",
            "data_source": "kiwoom 0H redis + stock_price_snapshot + v4_ohlcv_minute",
            "excludes_common_universe": True,
            "note": "공통 v4_scalping_universe 50종목을 쓰지 않고 #119 실매매 하드게이트 후보에서만 매매선정합니다.",
        },
        "selection": {
            "status": "implemented",
            "rule": "독립 +20%·거래대금 1억원 이상 watch 후보 안에서 +27% BUY, 상한가권, 재료/학습 게이트를 평가",
            "hard_exclusion": "15% 구간 soft bypass는 #119에서 금지",
            "candidate_source": "card119_independent_discovery",
        },
        "entry": {
            "status": "implemented",
            "primary_gate": "change_pct >= 27.0 and limit-up zone/lock score confirmation",
            "opening_lane": "09:00~09:04 fast limit lane, min trade value 100,000,000 KRW",
            "regular_lane": "09:05~14:20 normal lane",
            "prelock_model": "shadow_only until bid_stack_retention/limit_bid_volume coverage is verified",
        },
        "exit": {
            "status": "implemented",
            "contract": "close_locked_next_open",
            "next_day": "gap-up 50% partial exit, remaining 2% trailing, -5% stop, force close 09:20",
            "same_day_failure": "limit-up failure / not-limit-zone force exit",
            "thresholds": {
                "stop_loss_pct": thresholds.get("stop_loss_pct"),
                "trailing_stop_pct": thresholds.get("trailing_stop_pct"),
                "force_close_time": thresholds.get("time_exit"),
            },
        },
    }
def _num_from_context(ctx: dict[str, Any], *keys: str) -> Optional[float]:
    for key in keys:
        value = ctx.get(key)
        if value is None:
            continue
        try:
            return float(value)
        except (TypeError, ValueError):
            continue
    return None


def _build_wave_trade_review(entry_context_raw: Any, exit_context_raw: Any) -> dict[str, Any]:
    """Compact #303 review for live snapshots and historical replay payloads."""
    entry_context = _parse_json_field(entry_context_raw)
    exit_context = _parse_json_field(exit_context_raw)
    if not isinstance(entry_context, dict):
        entry_context = {}
    if not isinstance(exit_context, dict):
        exit_context = {}

    fixed_peak = _num_from_context(exit_context, "fixed_wave_peak") or _num_from_context(entry_context, "fixed_wave_peak")
    pullback_low = _num_from_context(exit_context, "pullback_low", "pullback_low_price") or _num_from_context(entry_context, "pullback_low", "pullback_low_price")
    entry_pullback_depth = _num_from_context(entry_context, "pullback_depth_pct", "entry_pullback_depth_pct")
    entry_rebound = _num_from_context(entry_context, "rebound_from_pullback_pct", "entry_rebound_from_pullback_pct", "entry_from_pullback_pct")
    exit_to_peak = _num_from_context(exit_context, "exit_to_fixed_wave_peak_pct", "exit_from_peak_pct")
    if exit_to_peak is None:
        exit_to_peak = _num_from_context(entry_context, "exit_to_fixed_wave_peak_pct", "exit_from_peak_pct")
    exit_from_low = _num_from_context(exit_context, "exit_from_pullback_low_pct", "exit_from_pullback_pct")
    if exit_from_low is None:
        exit_from_low = _num_from_context(entry_context, "exit_from_pullback_low_pct", "exit_from_pullback_pct")
    entry_zone_pct = _num_from_context(entry_context, "entry_zone_pct")
    exit_zone_pct = _num_from_context(exit_context, "exit_zone_pct")
    if exit_zone_pct is None:
        exit_zone_pct = _num_from_context(entry_context, "exit_zone_pct")
    sample_source = (
        entry_context.get("sample_source")
        or exit_context.get("sample_source")
        or entry_context.get("source")
        or exit_context.get("source")
    )
    data_quality = exit_context.get("data_quality") or entry_context.get("data_quality")
    if not isinstance(data_quality, dict):
        data_quality = None

    if not entry_context and not exit_context:
        verdict = "NO_WAVE_SNAPSHOT"
    elif sample_source == "historical_trade_replay_v1":
        verdict = "HISTORICAL_REPLAY_MATCHED"
    elif exit_to_peak is not None and exit_to_peak >= 0:
        verdict = "EXIT_AT_OR_ABOVE_FIXED_WAVE_PEAK"
    elif exit_to_peak is not None and exit_to_peak >= -1.0:
        verdict = "EXIT_NEAR_FIXED_WAVE_PEAK"
    elif exit_from_low is not None and exit_from_low > 0:
        verdict = "EXIT_AFTER_PULLBACK_RECOVERY"
    else:
        verdict = "WAVE_CONTEXT_RECORDED"

    return {
        "available": bool(entry_context or exit_context),
        "entry_phase": entry_context.get("wave_status") or entry_context.get("wave_phase_at_entry") or entry_context.get("timeframe"),
        "fixed_wave_peak": fixed_peak,
        "pullback_low": pullback_low,
        "entry_pullback_depth_pct": entry_pullback_depth,
        "entry_rebound_from_pullback_pct": entry_rebound,
        "exit_to_fixed_wave_peak_pct": exit_to_peak,
        "exit_from_pullback_low_pct": exit_from_low,
        "entry_zone_pct": entry_zone_pct,
        "exit_zone_pct": exit_zone_pct,
        "entry_from_pullback_pct": _num_from_context(entry_context, "entry_from_pullback_pct"),
        "exit_from_peak_pct": _num_from_context(exit_context, "exit_from_peak_pct") or _num_from_context(entry_context, "exit_from_peak_pct"),
        "wave1_start_price": _num_from_context(entry_context, "wave1_start_price"),
        "wave1_start_time": entry_context.get("wave1_start_time") or entry_context.get("wave1_start"),
        "wave1_high_price": _num_from_context(entry_context, "wave1_high_price"),
        "wave1_high_time": entry_context.get("wave1_high_time"),
        "source": sample_source,
        "sample_source": sample_source,
        "good_entry_zone": entry_context.get("good_entry_zone"),
        "premature_exit_candidate": exit_context.get("premature_exit_candidate") or entry_context.get("premature_exit_candidate"),
        "late_exit_candidate": exit_context.get("late_exit_candidate") or entry_context.get("late_exit_candidate"),
        "data_quality": data_quality,
        "learning_included": entry_context.get("learning_included") if sample_source == "historical_trade_replay_v1" else None,
        "verdict": verdict,
    }


def _extract_thresholds(entry_rules: Any, exit_rules: Any, risk_params: Any) -> dict:
    """entry_rules, exit_rules, risk_params JSON에서 임계값 추출.

    risk_params(dict)를 우선 탐색, 없으면 exit_rules(list) 규칙 탐색.
    값이 없으면 None 반환 (하드코딩 금지).
    """
    risk_raw = _parse_json_field(risk_params)
    risk: dict = risk_raw if isinstance(risk_raw, dict) else {}

    exit_raw = _parse_json_field(exit_rules)
    exit_list: list = exit_raw if isinstance(exit_raw, list) else []

    def _f(v: Any) -> Optional[float]:
        return float(v) if v is not None else None

    thresholds: dict[str, Any] = {
        "stop_loss_pct": _f(risk.get("stop_loss_pct")),
        "take_profit_pct": _f(risk.get("take_profit_pct")),
        "trailing_stop_pct": _f(risk.get("trailing_stop_pct")),
        "trailing_activate_pct": _f(risk.get("trailing_activate_pct")),
        "max_stocks": risk.get("max_stocks"),
        "max_position_count": risk.get("max_position_count") or risk.get("max_stocks"),
        "max_loss_pct": _f(risk.get("daily_loss_limit_pct") or risk.get("max_loss_pct")),
        "holding_days": risk.get("max_hold_days"),
        "time_exit": risk.get("force_close_time"),
        "position_size_pct": _f(risk.get("position_size_pct")),
        "per_position_amount": risk.get("per_position_amount"),
    }
    # Supplement from exit_rules list if risk_params lacks the value
    for rule in exit_list:
        if not isinstance(rule, dict):
            continue
        rt = rule.get("type", "")
        if rt == "stop_loss" and thresholds["stop_loss_pct"] is None:
            thresholds["stop_loss_pct"] = _f(rule.get("pct"))
        elif rt in ("profit_target", "take_profit") and thresholds["take_profit_pct"] is None:
            thresholds["take_profit_pct"] = _f(rule.get("pct"))
        elif rt == "trailing_stop" and thresholds["trailing_stop_pct"] is None:
            thresholds["trailing_stop_pct"] = _f(rule.get("pct"))
        elif rt == "time_stop" and thresholds["time_exit"] is None:
            thresholds["time_exit"] = rule.get("time")
        elif rt == "holding_days" and thresholds["holding_days"] is None:
            thresholds["holding_days"] = rule.get("max")
    return thresholds


def _date_filter_clause(
    mode: str,
    date_from: Optional[_date],
    date_to: Optional[_date],
    col: str = "created_at",
) -> tuple[str, dict]:
    """mode에 따라 SQL WHERE 절 조각과 DATE 타입 params를 반환."""
    if mode == "realtime":
        clause = f"({col} AT TIME ZONE 'Asia/Seoul')::date = (NOW() AT TIME ZONE 'Asia/Seoul')::date"
        return clause, {}
    if mode == "date_range" or (mode in ("cumulative", "lifecycle") and date_from is not None):
        if not date_from:
            date_from = datetime.now(KST).date() - timedelta(days=7)
        if not date_to:
            date_to = datetime.now(KST).date()
        clause = f"({col} AT TIME ZONE 'Asia/Seoul')::date BETWEEN :date_from AND :date_to"
        return clause, {"date_from": date_from, "date_to": date_to}
    # cumulative or lifecycle without explicit dates: no filter
    return "", {}


def _regime_filter_clause(col: str, market_regime: Optional[str]) -> tuple[str, dict]:
    """원천 일자를 일별 시장 레짐과 대사하는 공통 필터."""
    if not market_regime:
        return "", {}
    return (
        "EXISTS (SELECT 1 FROM v4_market_regime_daily mr "
        f"WHERE mr.date = ({col} AT TIME ZONE 'Asia/Seoul')::date "
        "AND UPPER(mr.regime) = :market_regime)",
        {"market_regime": market_regime.upper()},
    )


_STAGE1_LIVE_FIELDS = (
    "current_price", "change_rate_pct", "volume", "trading_value_krw",
    "market_trading_value_krw", "market_trading_value_source",
    "market_trading_value_quote_time",
    "nxt_trading_value_krw", "nxt_trading_value_source",
    "nxt_trading_value_quote_time",
    "total_trading_value_krw", "trading_value_source",
    "is_nxt", "quote_time", "quote_age_sec", "freshness_status",
    "upper_limit_price", "distance_to_limit_price", "distance_to_limit_pct",
    "data_source", "missing_reason",
)


def _stage1_row_value(row: Any, key: str, default: Any = None) -> Any:
    """SQLAlchemy Row와 테스트용 dict 양쪽에서 컬럼을 읽는다."""
    if isinstance(row, dict):
        return row.get(key, default)
    return getattr(row, key, default)


def _stage1_live_for_code(
    live_data: dict[str, dict[str, Any]],
    stock_code: Any,
) -> dict[str, Any]:
    raw_code = str(stock_code or "")
    normalized_code = _normalize_stock_code(stock_code)
    return live_data.get(raw_code) or live_data.get(normalized_code) or {}


def _stage1_thresholds(
    strategy_type: str,
    strategy_params_raw: Any,
    entry_rules_raw: Any,
    *,
    event_fallback: bool = False,
) -> dict[str, Optional[float]]:
    """Stage 1 후보 판정에 사용하는 카드별 임계값을 추출한다.

    실시간 후보 유니버스에는 카드 JSON에 선언된 값만 적용한다. 값이 없는
    scalping 카드는 유니버스 자체를 우선 원장으로 보고 상승(0% 이상)만
    기본 게이트로 사용하며, stale/missing은 행을 숨기지 않고 상태로 노출한다.
    """
    params = _parse_json_field(strategy_params_raw)
    params = params if isinstance(params, dict) else {}
    nested_params = [
        value for key in ("scalping_params", "limitup_params", "entry_params")
        if isinstance((value := params.get(key)), dict)
    ]
    sources: list[dict[str, Any]] = [params, *nested_params]

    rules = _parse_json_field(entry_rules_raw)
    if isinstance(rules, list):
        for rule in rules:
            if not isinstance(rule, dict):
                continue
            sources.append(rule)
            rule_params = rule.get("params")
            if isinstance(rule_params, dict):
                sources.append(rule_params)

    def _first_float(keys: tuple[str, ...]) -> Optional[float]:
        for source in sources:
            for key in keys:
                value = source.get(key)
                if value is None or value == "":
                    continue
                try:
                    return float(value)
                except (TypeError, ValueError):
                    continue
        return None

    min_change = _first_float((
        "min_change_rate_pct", "min_momentum_pct", "daily_change_pct_min",
        "min_intraday_pct", "entry_min_intraday_pct", "change_rate_min",
    ))
    max_change = _first_float((
        "max_change_rate_pct", "daily_change_pct_max", "max_intraday_pct",
        "max_entry_pct", "change_rate_max",
    ))
    min_trade_value = _first_float((
        "min_trading_value", "min_trade_value_krw", "min_trade_value",
        "min_amount_krw", "min_amount",
    ))

    if min_change is None:
        if strategy_type == "limitup_chase":
            # scalping_entry_engine의 상한가 진입 하한과 동일하게 맞춘다.
            min_change = 25.0
        else:
            min_change = 0.0
    # scalping 후보 화면에서는 음수 등락률을 기본적으로 제외한다.
    min_change = max(0.0, min_change)
    if strategy_type == "limitup_chase" and max_change is None:
        max_change = 30.0
    if event_fallback and min_trade_value is None:
        # 기존 이벤트 화면의 threshold_values 호환값을 유지한다.
        min_trade_value = 1_000_000_000.0

    return {
        "min_change_rate_pct": min_change,
        "max_change_rate_pct": max_change,
        "min_trading_value_krw": min_trade_value,
    }


def _stage1_candidate_status(
    live: dict[str, Any],
    thresholds: dict[str, Optional[float]],
) -> tuple[Optional[str], list[str], dict[str, Optional[bool]]]:
    """시세 상태와 카드 threshold를 조합해 후보 상태를 계산한다."""
    data_source = str(live.get("data_source") or "")
    freshness = str(live.get("freshness_status") or "")
    change_rate = _safe_float(live.get("change_rate_pct"))
    current_price = _safe_float(live.get("current_price"))
    min_change = thresholds.get("min_change_rate_pct")
    max_change = thresholds.get("max_change_rate_pct")
    min_trade_value = thresholds.get("min_trading_value_krw")
    trading_value = _safe_float(live.get("trading_value_krw"))

    if data_source == "missing" or current_price is None or change_rate is None:
        status = "data_missing"
    elif data_source == "stale_snapshot" or freshness == "stale":
        status = "stale"
    else:
        status = "qualified"

    reasons: list[str] = []
    if status == "data_missing":
        reasons.append(str(live.get("missing_reason") or "시세 데이터 없음"))
    elif status == "stale":
        reasons.append(str(live.get("missing_reason") or "시세 지연"))

    min_change_pass = (
        None if change_rate is None or min_change is None
        else change_rate >= min_change
    )
    max_change_pass = (
        None if change_rate is None or max_change is None
        else change_rate <= max_change
    )
    min_trade_pass = (
        None if trading_value is None or min_trade_value is None
        else trading_value >= min_trade_value
    )

    if status not in ("data_missing", "stale"):
        if min_change_pass is False:
            reasons.append("min_change_rate_pct 미충족")
        if max_change_pass is False:
            reasons.append("max_change_rate_pct 초과")
        if min_trade_pass is False:
            reasons.append("min_trading_value_krw 미충족")
        if reasons:
            status = "rejected"

    return status, reasons, {
        "min_change_rate_pct": min_change_pass,
        "max_change_rate_pct": max_change_pass,
        "min_trading_value_krw": min_trade_pass,
    }


def _stage1_universe_row(
    row: Any,
    live: dict[str, Any],
    thresholds: dict[str, Optional[float]],
    candidate_status: str,
    reasons: list[str],
    threshold_passes: dict[str, Optional[bool]],
    card_version: int,
) -> dict[str, Any]:
    code = _stage1_row_value(row, "stock_code")
    status = candidate_status
    return {
        **_name_payload(code, _stage1_row_value(row, "stock_name")),
        "stage": "candidate_universe",
        "event_phase": "candidate_universe",
        "decision": "candidate",
        "reason_text": "; ".join(reasons) if reasons else str(_stage1_row_value(row, "reason_text", "v4_scalping_universe 후보")),
        "source_ts": None,
        "received_at": None,
        "trade_group_id": None,
        "card_version": card_version,
        "source_table": _stage1_row_value(row, "source_table", "v4_scalping_universe"),
        "created_at": _ts(_stage1_row_value(row, "created_date")),
        "market": _stage1_row_value(row, "market") or live.get("market"),
        **{key: live.get(key) for key in _STAGE1_LIVE_FIELDS},
        "intraday_change_rank": _num(_stage1_row_value(row, "intraday_change_rank")),
        "bullish_trade_value_rank": _num(_stage1_row_value(row, "bullish_trade_value_rank")),
        "candidate_scope": _stage1_row_value(row, "candidate_scope"),
        "intraday_candidate_bucket": _stage1_row_value(row, "intraday_candidate_bucket"),
        "intraday_bullish": _stage1_row_value(row, "intraday_bullish"),
        "avg_trade_value_20d": _num(_stage1_row_value(row, "avg_trade_value_20d")),
        "avg_atr_pct_20d": _num(_stage1_row_value(row, "avg_atr_pct_20d")),
        "avg_volume_20d": _num(_stage1_row_value(row, "avg_volume_20d")),
        "close_price": _num(_stage1_row_value(row, "close_price")),
        "market_cap": _num(_stage1_row_value(row, "market_cap")),
        "scalp_score": _num(_stage1_row_value(row, "scalp_score")),
        "threshold_values": {
            "min_change_rate_pct": thresholds.get("min_change_rate_pct"),
            "max_change_rate_pct": thresholds.get("max_change_rate_pct"),
            "min_trading_value_krw": thresholds.get("min_trading_value_krw"),
        },
        "threshold_passes": {
            **threshold_passes,
            "entry_window": True,
            "opening_lane": False,
            "nxt_preopen": False,
        },
        "candidate_status": status,
        "candidate_rejection_reasons": reasons,
        "detailed_reason": "; ".join(reasons) if reasons else str(_stage1_row_value(row, "reason_text", "v4_scalping_universe 후보")),
    }


async def _stage1_intraday_top50_candidates(db: AsyncSession) -> dict[str, dict[str, Any]]:
    """Build the canonical current-day #303 discovery pool."""
    today_kst = datetime.now(KST).date()
    today_start_kst = datetime.combine(today_kst, datetime.min.time(), tzinfo=KST)
    tomorrow_start_kst = today_start_kst + timedelta(days=1)
    fresh_after_kst = datetime.now(KST) - timedelta(
        minutes=CARD303_DISCOVERY_SNAPSHOT_FRESH_MINUTES
    )
    try:
        result = await db.execute(
            text("""
                WITH base AS (
                    SELECT DISTINCT ON (sps.stock_code)
                           sps.stock_code,
                           COALESCE(su.stock_name, sps.stock_code) AS stock_name,
                           COALESCE(su.market, 'UNKNOWN') AS market,
                           COALESCE(sps.price, 0) AS current_price,
                           sps.snapshot_time AS latest_time,
                           COALESCE(su.is_nxt, false) AS is_nxt,
                           COALESCE(sps.volume, 0)::bigint AS cumulative_volume,
                           CASE
                               WHEN NULLIF(sps.trade_amount, 0) IS NULL THEN
                                   COALESCE(sps.price, 0)::numeric * COALESCE(sps.volume, 0)::numeric
                               WHEN COALESCE(sps.price, 0)::numeric * COALESCE(sps.volume, 0)::numeric > 0
                                    AND NULLIF(sps.trade_amount, 0)::numeric <
                                        (COALESCE(sps.price, 0)::numeric * COALESCE(sps.volume, 0)::numeric * 0.01)
                               THEN COALESCE(sps.price, 0)::numeric * COALESCE(sps.volume, 0)::numeric
                               ELSE NULLIF(sps.trade_amount, 0)::numeric
                           END AS trading_value_krw,
                           COALESCE(sps.change_pct, 0)::numeric AS change_rate_pct,
                           COALESCE(sps.change_pct, 0) >= 0 AS intraday_bullish
                    FROM stock_price_snapshot sps
                    LEFT JOIN stock_universe su ON su.stock_code = sps.stock_code
                    WHERE sps.snapshot_time >= :today_start_kst
                      AND sps.snapshot_time < :tomorrow_start_kst
                      AND sps.snapshot_time >= :fresh_after_kst
                      AND sps.stock_code ~ '^[0-9]{6}$'
                      AND COALESCE(su.is_active, true) = true
                      AND NOT (
                        UPPER(REPLACE(COALESCE(su.stock_name, sps.stock_code, ''), ' ', '')) LIKE 'KODEX%'
                        OR UPPER(REPLACE(COALESCE(su.stock_name, sps.stock_code, ''), ' ', '')) LIKE 'TIGER%'
                        OR UPPER(REPLACE(COALESCE(su.stock_name, sps.stock_code, ''), ' ', '')) LIKE 'ACE%'
                        OR UPPER(REPLACE(COALESCE(su.stock_name, sps.stock_code, ''), ' ', '')) LIKE 'SOL%'
                        OR UPPER(REPLACE(COALESCE(su.stock_name, sps.stock_code, ''), ' ', '')) LIKE 'KBSTAR%'
                        OR UPPER(REPLACE(COALESCE(su.stock_name, sps.stock_code, ''), ' ', '')) LIKE 'HANARO%'
                        OR UPPER(REPLACE(COALESCE(su.stock_name, sps.stock_code, ''), ' ', '')) LIKE 'ARIRANG%'
                        OR UPPER(REPLACE(COALESCE(su.stock_name, sps.stock_code, ''), ' ', '')) LIKE 'KOSEF%'
                        OR UPPER(REPLACE(COALESCE(su.stock_name, sps.stock_code, ''), ' ', '')) LIKE 'TIMEFOLIO%'
                        OR UPPER(REPLACE(COALESCE(su.stock_name, sps.stock_code, ''), ' ', '')) LIKE 'RISE%'
                        OR UPPER(REPLACE(COALESCE(su.stock_name, sps.stock_code, ''), ' ', '')) LIKE 'PLUS%'
                        OR UPPER(REPLACE(COALESCE(su.stock_name, sps.stock_code, ''), ' ', '')) LIKE 'WON%'
                        OR UPPER(REPLACE(COALESCE(su.stock_name, sps.stock_code, ''), ' ', '')) LIKE 'TREX%'
                        OR REPLACE(COALESCE(su.stock_name, sps.stock_code, ''), ' ', '') LIKE '마이티%'
                        OR UPPER(COALESCE(su.stock_name, sps.stock_code, '')) LIKE '%ETF%'
                        OR UPPER(COALESCE(su.stock_name, sps.stock_code, '')) LIKE '%ETN%'
                        OR COALESCE(su.stock_name, sps.stock_code, '') LIKE '%레버리지%'
                        OR COALESCE(su.stock_name, sps.stock_code, '') LIKE '%인버스%'
                        OR COALESCE(su.stock_name, sps.stock_code, '') LIKE '%선물%'
                        OR COALESCE(su.stock_name, sps.stock_code, '') LIKE '%채권%'
                        OR COALESCE(su.stock_name, sps.stock_code, '') LIKE '%국채%'
                        OR COALESCE(su.stock_name, sps.stock_code, '') LIKE '%통안채%'
                        OR COALESCE(su.stock_name, sps.stock_code, '') LIKE '%스팩%'
                        OR UPPER(COALESCE(su.stock_name, sps.stock_code, '')) LIKE '%SPAC%'
                        OR COALESCE(su.stock_name, sps.stock_code, '') LIKE '%리츠%'
                        OR UPPER(COALESCE(su.stock_name, sps.stock_code, '')) LIKE '%REIT%'
                        OR COALESCE(su.stock_name, sps.stock_code, '') LIKE '%관리종목%'
                        OR COALESCE(su.stock_name, sps.stock_code, '') LIKE '%정리매매%'
                        OR COALESCE(su.stock_name, sps.stock_code, '') LIKE '%우선주%'
                        OR COALESCE(su.stock_name, sps.stock_code, '') LIKE '%우'
                        OR COALESCE(su.stock_name, sps.stock_code, '') LIKE '%우B'
                        OR COALESCE(su.stock_name, sps.stock_code, '') LIKE '%우C'
                      )
                    ORDER BY sps.stock_code, sps.snapshot_time DESC
                ),
                candidate_top AS (
                    SELECT stock_code,
                           ROW_NUMBER() OVER (ORDER BY trading_value_krw DESC NULLS LAST, change_rate_pct DESC NULLS LAST, stock_code ASC) AS discovery_rank,
                           ROW_NUMBER() OVER (ORDER BY trading_value_krw DESC NULLS LAST, change_rate_pct DESC NULLS LAST, stock_code ASC) AS bullish_trade_value_rank
                    FROM base
                    WHERE COALESCE(change_rate_pct, -999) >= :min_change_pct
                      AND (
                          CAST(:max_change_pct AS numeric) IS NULL
                          OR change_rate_pct <= CAST(:max_change_pct AS numeric)
                      )
                      AND (
                          CAST(:min_trading_value_krw AS numeric) IS NULL
                          OR trading_value_krw >= CAST(:min_trading_value_krw AS numeric)
                      )
                    ORDER BY trading_value_krw DESC NULLS LAST, change_rate_pct DESC NULLS LAST, stock_code ASC
                    LIMIT :candidate_limit
                )
                SELECT b.*,
                       ct.discovery_rank AS intraday_change_rank,
                       ct.bullish_trade_value_rank
                FROM base b
                JOIN candidate_top ct USING (stock_code)
                ORDER BY ct.discovery_rank ASC
            """),
            {
                "today": today_kst,
                "today_date": today_kst,
                "today_start_kst": today_start_kst,
                "tomorrow_start_kst": tomorrow_start_kst,
                "fresh_after_kst": fresh_after_kst,
                "min_change_pct": CARD303_DISCOVERY_MIN_CHANGE_PCT,
                "max_change_pct": CARD303_DISCOVERY_MAX_CHANGE_PCT,
                "min_trading_value_krw": CARD303_DISCOVERY_MIN_TRADING_VALUE_KRW,
                "candidate_limit": CARD303_DISCOVERY_LIMIT,
            },
        )
    except Exception as exc:
        logger.warning("stage1 intraday top50 candidates unavailable: %s", exc)
        return {}

    candidates: dict[str, dict[str, Any]] = {}
    for row in result.fetchall():
        code = str(row.stock_code or "").strip()
        stock_name = getattr(row, "stock_name", code) or code
        if len(code) != 6 or not code.isdigit() or _is_excluded_name_119(stock_name, stock_code=code):
            continue
        buckets: list[str] = []
        intraday_change_rank = getattr(row, "intraday_change_rank", None)
        bullish_trade_value_rank = getattr(row, "bullish_trade_value_rank", None)
        if intraday_change_rank is not None:
            buckets.append("change_rate_3pct_plus")
        if bullish_trade_value_rank is not None:
            buckets.append("trade_value_top50")
        current_price = _num(getattr(row, "current_price", None))
        raw_volume = getattr(row, "cumulative_volume", getattr(row, "volume", None))
        volume = int(raw_volume or 0) if raw_volume is not None else None
        trading_value_krw = _normalize_trading_value_krw(
            getattr(row, "trading_value_krw", getattr(row, "trade_amount", None)),
            price=current_price,
            volume=volume,
        )
        candidates[code] = {
            "stock_code": code,
            "stock_name": stock_name,
            "market": getattr(row, "market", "UNKNOWN"),
            "avg_trade_value_20d": None,
            "avg_atr_pct_20d": None,
            "avg_volume_20d": None,
            "close_price": current_price,
            "market_cap": None,
            "scalp_score": None,
            "created_date": today_kst,
            "updated_at": getattr(row, "latest_time", getattr(row, "snapshot_time", None)),
            "is_nxt": bool(getattr(row, "is_nxt", False)),
            "current_price": current_price,
            "change_rate_pct": _num(getattr(row, "change_rate_pct", getattr(row, "change_pct", None))),
            "volume": volume,
            "trading_value_krw": trading_value_krw,
            "total_trading_value_krw": trading_value_krw,
            "quote_time": (
                getattr(row, "latest_time", getattr(row, "snapshot_time", None)).isoformat()
                if getattr(row, "latest_time", getattr(row, "snapshot_time", None)) else None
            ),
            "freshness_status": "fresh",
            "data_source": "stock_price_snapshot",
            "trading_value_source": "snapshot_cumulative",
            "source_table": "stock_price_snapshot_intraday_top50",
            "reason_text": "당일 " + "+".join(buckets) + " 후보",
            "candidate_scope": "intraday_change3_trade_value_top50",
            "intraday_candidate_bucket": buckets,
            "intraday_change_rank": int(intraday_change_rank) if intraday_change_rank is not None else None,
            "bullish_trade_value_rank": int(bullish_trade_value_rank) if bullish_trade_value_rank is not None else None,
            "intraday_bullish": bool(getattr(row, "intraday_bullish", True)),
        }
    return candidates


_TRADE_VALUE_WINDOW_SPECS: tuple[tuple[str, str, str, int], ...] = (
    ("open_5m", "장시작 5분", "open", 5),
    ("open_10m", "장시작 10분", "open", 10),
    ("open_30m", "장시작 30분", "open", 30),
    ("open_60m", "장시작 1시간", "open", 60),
    ("recent_5m", "최근 5분", "recent", 5),
    ("recent_10m", "최근 10분", "recent", 10),
    ("recent_30m", "최근 30분", "recent", 30),
    ("recent_60m", "최근 1시간", "recent", 60),
)


async def _stage1_trade_value_windows(
    db: AsyncSession,
    stock_codes: list[str],
) -> tuple[dict[str, dict[str, Any]], dict[str, Any]]:
    """Aggregate #303 intraday trade value windows from 1-minute OHLCV."""
    clean_codes = sorted({str(code).strip() for code in stock_codes if str(code).strip()})
    if not clean_codes:
        return {}, {"status": "empty", "windows": []}

    today_kst = datetime.now(KST).date()
    latest_result = await db.execute(
        text("""
            SELECT MAX(trade_date::timestamp + trade_time::interval) AS latest_trade_time
            FROM v4_ohlcv_minute
            WHERE trade_date = :trade_date
              AND stock_code = ANY(:stock_codes)
        """),
        {"trade_date": today_kst, "stock_codes": clean_codes},
    )
    latest_trade_time = latest_result.scalar()
    if not latest_trade_time:
        return {}, {"status": "missing_ohlcv", "trade_date": today_kst.isoformat(), "windows": []}

    session_start = datetime.combine(today_kst, datetime.min.time(), tzinfo=KST).replace(hour=9)
    if latest_trade_time.tzinfo is None:
        latest_trade_time = latest_trade_time.replace(tzinfo=KST)

    windows: list[dict[str, Any]] = []
    for key, label, mode, minutes in _TRADE_VALUE_WINDOW_SPECS:
        if mode == "open":
            start_ts = session_start
            end_ts = session_start + timedelta(minutes=minutes)
        else:
            end_ts = latest_trade_time
            start_ts = latest_trade_time - timedelta(minutes=minutes)
        windows.append({
            "key": key,
            "label": label,
            "start_ts": start_ts,
            "end_ts": end_ts,
            "minutes": minutes,
        })

    values_sql = ",\n".join(
        f"(:key_{i}, :label_{i}, CAST(:start_{i} AS timestamp), CAST(:end_{i} AS timestamp), CAST(:minutes_{i} AS integer))"
        for i, _ in enumerate(windows)
    )
    params: dict[str, Any] = {"trade_date": today_kst, "stock_codes": clean_codes}
    for i, window in enumerate(windows):
        params[f"key_{i}"] = window["key"]
        params[f"label_{i}"] = window["label"]
        params[f"start_{i}"] = window["start_ts"].replace(tzinfo=None)
        params[f"end_{i}"] = window["end_ts"].replace(tzinfo=None)
        params[f"minutes_{i}"] = window["minutes"]

    result = await db.execute(
        text(f"""
            WITH windows(window_key, window_label, start_ts, end_ts, minutes) AS (
                VALUES {values_sql}
            ),
            aggregated AS (
                SELECT
                    w.window_key,
                    w.window_label,
                    w.start_ts,
                    w.end_ts,
                    w.minutes,
                    m.stock_code,
                    COUNT(*)::integer AS sample_count,
                    SUM(
                        COALESCE(
                            NULLIF(m.trade_amount, 0)::numeric,
                            COALESCE(m.close_price, 0)::numeric * COALESCE(m.volume, 0)::numeric
                        )
                    ) AS trade_value_krw
                FROM windows w
                JOIN v4_ohlcv_minute m
                  ON m.trade_date = :trade_date
                 AND m.stock_code = ANY(:stock_codes)
                 AND (m.trade_date::timestamp + m.trade_time::interval) >= w.start_ts
                 AND (m.trade_date::timestamp + m.trade_time::interval) < w.end_ts
                GROUP BY w.window_key, w.window_label, w.start_ts, w.end_ts, w.minutes, m.stock_code
            ),
            ranked AS (
                SELECT
                    a.*,
                    ROW_NUMBER() OVER (
                        PARTITION BY a.window_key
                        ORDER BY a.trade_value_krw DESC NULLS LAST, a.stock_code
                    ) AS trade_value_rank
                FROM aggregated a
            )
            SELECT window_key, window_label, start_ts, end_ts, minutes,
                   stock_code, sample_count, trade_value_krw, trade_value_rank
            FROM ranked
            ORDER BY window_key, trade_value_rank, stock_code
        """),
        params,
    )

    by_code: dict[str, dict[str, Any]] = {code: {} for code in clean_codes}
    for row in result.fetchall():
        by_code.setdefault(str(row.stock_code), {})[str(row.window_key)] = {
            "label": row.window_label,
            "minutes": int(row.minutes or 0),
            "start_at": _ts(row.start_ts),
            "end_at": _ts(row.end_ts),
            "trade_value_krw": _safe_float(row.trade_value_krw),
            "rank": int(row.trade_value_rank) if row.trade_value_rank is not None else None,
            "data_source": "v4_ohlcv_minute",
            "as_of_kst": _ts(latest_trade_time),
            "window_start": _ts(row.start_ts),
            "window_end": _ts(row.end_ts),
            "sample_count": int(row.sample_count or 0),
            "rank_basis": "candidate_set_trade_value_krw_desc_stock_code_asc",
        }

    for code in clean_codes:
        code_windows = by_code.setdefault(code, {})
        for window in windows:
            code_windows.setdefault(window["key"], {
                "label": window["label"],
                "minutes": window["minutes"],
                "start_at": _ts(window["start_ts"]),
                "end_at": _ts(window["end_ts"]),
                "trade_value_krw": None,
                "rank": None,
                "data_source": "v4_ohlcv_minute",
                "as_of_kst": _ts(latest_trade_time),
                "window_start": _ts(window["start_ts"]),
                "window_end": _ts(window["end_ts"]),
                "sample_count": 0,
                "rank_basis": "candidate_set_trade_value_krw_desc_stock_code_asc",
            })

    return by_code, {
        "status": "available",
        "trade_date": today_kst.isoformat(),
        "latest_trade_time": _ts(latest_trade_time),
        "source": "v4_ohlcv_minute",
        "data_source": "v4_ohlcv_minute",
        "as_of_kst": _ts(latest_trade_time),
        "candidate_count": len(clean_codes),
        "rank_basis": "requested_candidate_set_trade_value_krw_desc_stock_code_asc",
        "windows": [
            {
                "key": window["key"],
                "label": window["label"],
                "start_at": _ts(window["start_ts"]),
                "end_at": _ts(window["end_ts"]),
                "minutes": window["minutes"],
            }
            for window in windows
        ],
    }


async def _enqueue_stage1_snapshot_backfill(
    db: AsyncSession,
    *,
    limit: int = 200,
) -> dict[str, Any]:
    """Queue on-demand snapshot backfill when #303 intraday candidates are empty."""
    try:
        result = await db.execute(
            text("""
                WITH target AS (
                    SELECT u.stock_code, u.stock_name
                    FROM stock_universe u
                    WHERE u.stock_code ~ '^[0-9]{6}$'
                      AND COALESCE(u.is_active, true) = true
                      AND NOT EXISTS (
                          SELECT 1
                          FROM stock_price_snapshot s
                          WHERE s.stock_code = u.stock_code
                            AND (s.snapshot_time AT TIME ZONE 'Asia/Seoul')::date = (NOW() AT TIME ZONE 'Asia/Seoul')::date
                          LIMIT 1
                      )
                      AND NOT EXISTS (
                          SELECT 1
                          FROM go100_data_backfill_queue q
                          WHERE q.stock_code = u.stock_code
                            AND q.missing_type = 'snapshot_today'
                            AND q.status IN ('pending', 'running', 'source_unavailable')
                          LIMIT 1
                      )
                    ORDER BY COALESCE(u.market_cap, 0) DESC NULLS LAST, u.stock_code
                    LIMIT :limit
                )
                INSERT INTO go100_data_backfill_queue
                    (stock_code, stock_name, missing_type, source_table, reason, priority, status, metadata, created_at, updated_at)
                SELECT stock_code, stock_name, 'snapshot_today', 'stock_price_snapshot',
                       'workbench_stage1_empty_snapshot_backfill', 100, 'pending',
                       jsonb_build_object(
                           'requested_by', 'strategy_303_operations_page',
                           'scope', 'stage1_empty_intraday_candidates',
                           'candidate_design', 'change_pct >= 3.0 AND KRX+NXT total_trading_value top50'
                       ),
                       NOW(), NOW()
                FROM target
                RETURNING stock_code
            """),
            {"limit": limit},
        )
        rows = result.fetchall()
        await db.commit()
        return {
            "status": "queued" if rows else "already_covered_or_pending",
            "missing_type": "snapshot_today",
            "enqueued_count": len(rows),
            "queue_reason": "workbench_stage1_empty_snapshot_backfill",
        }
    except Exception as exc:
        await db.rollback()
        logger.warning("stage1 snapshot backfill enqueue failed: %s", exc)
        return {
            "status": "failed",
            "missing_type": "snapshot_today",
            "enqueued_count": 0,
            "error": str(exc)[:180],
        }


async def _enqueue_stage1_minute_backfill(
    db: AsyncSession,
    stock_codes: list[str],
) -> dict[str, Any]:
    """Queue only missing #303 candidate minute bars; never block the API."""
    clean_codes = sorted({str(code).strip() for code in stock_codes if str(code).strip()})[:100]
    if not clean_codes:
        return {"status": "empty", "missing_type": "minute_ohlcv_365d", "enqueued_count": 0}
    try:
        result = await db.execute(
            text("""
                WITH target AS (
                    SELECT u.stock_code, u.stock_name
                    FROM stock_universe u
                    WHERE u.stock_code = ANY(:stock_codes)
                      AND NOT EXISTS (
                          SELECT 1 FROM v4_ohlcv_minute m
                          WHERE m.stock_code = u.stock_code
                            AND m.trade_date = (NOW() AT TIME ZONE 'Asia/Seoul')::date
                      )
                      AND NOT EXISTS (
                          SELECT 1 FROM go100_data_backfill_queue q
                          WHERE q.stock_code = u.stock_code
                            AND q.missing_type = 'minute_ohlcv_365d'
                            AND q.status IN ('pending', 'running', 'source_unavailable')
                      )
                )
                INSERT INTO go100_data_backfill_queue
                    (stock_code, stock_name, missing_type, source_table, reason,
                     priority, status, metadata, created_at, updated_at)
                SELECT stock_code, stock_name, 'minute_ohlcv_365d', 'v4_ohlcv_minute',
                       'card303_intraday_window_missing', 90, 'pending',
                       jsonb_build_object('requested_by', 'card303_trade_value_windows',
                                          'scope', 'candidate_set_only'),
                       NOW(), NOW()
                FROM target
                RETURNING stock_code
            """),
            {"stock_codes": clean_codes},
        )
        rows = result.fetchall()
        await db.commit()
        return {
            "status": "queued" if rows else "available_or_pending",
            "missing_type": "minute_ohlcv_365d",
            "enqueued_count": len(rows),
        }
    except Exception as exc:
        await db.rollback()
        logger.warning("stage1 minute backfill enqueue failed: %s", exc)
        return {
            "status": "failed",
            "missing_type": "minute_ohlcv_365d",
            "enqueued_count": 0,
            "error": str(exc)[:180],
        }


async def _build_stage1_card303_top50_stage(
    db: AsyncSession,
    *,
    thresholds: dict[str, Optional[float]],
    card_version: int,
) -> dict[str, Any]:
    """#303 Stage 1: configured change filter + cumulative trading-value rank.

    등락률 하한 미충족 행도 삭제하지 않고 candidate_status/candidate_rejection_reasons/
    detailed_reason으로 표시한다. 무거운 tick source split/OHLCV fallback은 메인 렌더
    경로에서 제외하고 시간대 거래대금은 lazy endpoint에서 별도 조회한다.
    """
    intraday_candidates = await _stage1_intraday_top50_candidates(db)
    backfill_result: dict[str, Any] | None = None
    if not intraday_candidates:
        backfill_result = await _enqueue_stage1_snapshot_backfill(db)

    top50_count = len(intraday_candidates)
    live_data: dict[str, dict[str, Any]] = {}
    for row in intraday_candidates.values():
        code = str(_stage1_row_value(row, "stock_code") or "")
        if not code:
            continue
        live_data[code] = {
            "stock_name": _stage1_row_value(row, "stock_name"),
            "current_price": _num(_stage1_row_value(row, "current_price")),
            "change_rate_pct": _num(_stage1_row_value(row, "change_rate_pct")),
            "volume": _stage1_row_value(row, "volume"),
            "trading_value_krw": _num(_stage1_row_value(row, "trading_value_krw")),
            "total_trading_value_krw": _num(_stage1_row_value(row, "total_trading_value_krw")),
            "market_trading_value_krw": _num(_stage1_row_value(row, "trading_value_krw")),
            "market_trading_value_source": "snapshot_cumulative",
            "trading_value_source": _stage1_row_value(row, "trading_value_source"),
            "quote_time": _stage1_row_value(row, "quote_time"),
            "freshness_status": _stage1_row_value(row, "freshness_status"),
            "data_source": _stage1_row_value(row, "data_source"),
            "is_nxt": bool(_stage1_row_value(row, "is_nxt")),
            "market": _stage1_row_value(row, "market"),
        }

    ranked_codes = sorted(
        live_data.keys(),
        key=lambda c: (
            -(_safe_float(live_data[c].get("total_trading_value_krw")) or 0.0),
            -(_safe_float(live_data[c].get("change_rate_pct")) or 0.0),
            c,
        ),
    )
    for rank, ranked_code in enumerate(ranked_codes, start=1):
        if ranked_code in intraday_candidates:
            intraday_candidates[ranked_code]["bullish_trade_value_rank"] = rank
            intraday_candidates[ranked_code]["intraday_change_rank"] = rank

    visible_rows: list[dict[str, Any]] = []
    status_counts: dict[str, int] = {}
    for universe_row in intraday_candidates.values():
        code = _stage1_row_value(universe_row, "stock_code")
        live = _stage1_live_for_code(live_data, str(code) if code else "")
        live = {
            **live,
            "is_nxt": bool(live.get("is_nxt")) or bool(_stage1_row_value(universe_row, "is_nxt")),
            "trading_value_source": live.get("trading_value_source") or (
                "intraday_cumulative"
                if _safe_float(live.get("trading_value_krw")) is not None
                else None
            ),
        }
        candidate_status, reasons, threshold_passes = _stage1_candidate_status(live, thresholds)
        rendered = _stage1_universe_row(
            universe_row, live, thresholds, candidate_status, reasons,
            threshold_passes, card_version,
        )
        rendered["trade_value_windows"] = {}
        rendered["source_table"] = "stock_price_snapshot_intraday_top50"
        visible_rows.append(rendered)
        status_counts[candidate_status] = status_counts.get(candidate_status, 0) + 1

    visible_rows.sort(
        key=lambda row: (
            -(_safe_float(row.get("total_trading_value_krw")) or 0.0),
            -(_safe_float(row.get("change_rate_pct")) or 0.0),
            str(row.get("stock_code") or ""),
        ),
    )
    return {
        "stage_id": 1,
        "stage_key": "target_selection",
        "label": f"종목선정 후보 (Top{CARD303_DISCOVERY_LIMIT})",
        "count": len(visible_rows),
        "total_evaluations": len(visible_rows),
        "unique_stocks": len(visible_rows),
        "status": "available" if visible_rows else "empty",
        "updated_at": None,
        "source": "stock_price_snapshot_intraday_rank",
        "fallback_reason": None,
        "is_paper_filter_applied": False,
        "stage_columns": _STAGE1_COLUMNS.get("scalping_pullback", _STAGE1_COLUMNS["default"]),
        "rows": visible_rows,
        "summary": {
            "by_phase": [],
            "candidate_universe_count": top50_count,
            "static_universe_count": 0,
            "intraday_top50_unique_count": top50_count,
            "top50_count": top50_count,
            "row_count": len(visible_rows),
            "qualified_count": status_counts.get("qualified", 0),
            "rejected_count": status_counts.get("rejected", 0),
            "data_missing_count": status_counts.get("data_missing", 0),
            "stale_count": status_counts.get("stale", 0),
            "by_status": status_counts,
            "dynamic_intraday_source": "stock_price_snapshot",
            "candidate_design": get_card303_discovery_contract(),
            "sort_order": "total_trading_value_krw DESC, change_rate_pct DESC, stock_code ASC",
            "backfill": backfill_result,
            "trade_value_windows": {
                "status": "lazy",
                "source": "v4_ohlcv_minute",
                "endpoint": "/api/go100/strategy-cards/{card_id}/trade-value-windows",
                "candidate_count": top50_count,
            },
            "visible_count": len(visible_rows),
            "excluded_below_min_change_count": status_counts.get("rejected", 0),
            "excluded_negative_change_count": status_counts.get("rejected", 0),
        },
    }


async def _build_stage1_universe_stage(
    db: AsyncSession,
    *,
    strategy_type: str,
    thresholds: dict[str, Optional[float]],
    card_version: int,
) -> dict[str, Any]:
    """스캘핑/상한가 카드의 실제 실행 유니버스 기반 Stage 1을 구성한다."""
    universe_result = await db.execute(
        text("""
            SELECT vu.stock_code,
                   COALESCE(vu.stock_name, su.stock_name, vu.stock_code) AS stock_name,
                   vu.market, vu.avg_trade_value_20d, vu.avg_atr_pct_20d,
                   vu.avg_volume_20d, vu.close_price, vu.market_cap,
                   vu.scalp_score, vu.created_date, vu.updated_at,
                   COALESCE(su.is_nxt, false) AS is_nxt
            FROM v4_scalping_universe vu
            LEFT JOIN stock_universe su ON su.stock_code = vu.stock_code
            WHERE vu.created_date = (SELECT MAX(created_date) FROM v4_scalping_universe)
              AND COALESCE(vu.is_active, true) = true
              AND COALESCE(su.is_active, true) = true
              AND NOT (
                  UPPER(REPLACE(COALESCE(vu.stock_name, su.stock_name, ''), ' ', '')) LIKE 'KODEX%'
                  OR UPPER(REPLACE(COALESCE(vu.stock_name, su.stock_name, ''), ' ', '')) LIKE 'TIGER%'
                  OR UPPER(REPLACE(COALESCE(vu.stock_name, su.stock_name, ''), ' ', '')) LIKE 'ACE%'
                  OR UPPER(REPLACE(COALESCE(vu.stock_name, su.stock_name, ''), ' ', '')) LIKE 'SOL%'
                  OR UPPER(REPLACE(COALESCE(vu.stock_name, su.stock_name, ''), ' ', '')) LIKE 'KBSTAR%'
                  OR UPPER(REPLACE(COALESCE(vu.stock_name, su.stock_name, ''), ' ', '')) LIKE 'HANARO%'
                  OR UPPER(REPLACE(COALESCE(vu.stock_name, su.stock_name, ''), ' ', '')) LIKE 'ARIRANG%'
                  OR UPPER(REPLACE(COALESCE(vu.stock_name, su.stock_name, ''), ' ', '')) LIKE 'KOSEF%'
                  OR UPPER(REPLACE(COALESCE(vu.stock_name, su.stock_name, ''), ' ', '')) LIKE 'TIMEFOLIO%'
                  OR UPPER(REPLACE(COALESCE(vu.stock_name, su.stock_name, ''), ' ', '')) LIKE 'RISE%'
                  OR UPPER(REPLACE(COALESCE(vu.stock_name, su.stock_name, ''), ' ', '')) LIKE 'PLUS%'
                  OR UPPER(REPLACE(COALESCE(vu.stock_name, su.stock_name, ''), ' ', '')) LIKE 'WON%'
                  OR UPPER(COALESCE(vu.stock_name, su.stock_name, '')) LIKE '%ETF%'
                  OR UPPER(COALESCE(vu.stock_name, su.stock_name, '')) LIKE '%ETN%'
                  OR COALESCE(vu.stock_name, su.stock_name, '') LIKE '%스팩%'
                  OR COALESCE(vu.stock_name, su.stock_name, '') LIKE '%리츠%'
                  OR COALESCE(vu.stock_name, su.stock_name, '') LIKE '%관리%'
                  OR COALESCE(vu.stock_name, su.stock_name, '') LIKE '%정리%'
              )
            ORDER BY COALESCE(vu.scalp_score, 0) DESC,
                     COALESCE(vu.avg_trade_value_20d, 0) DESC,
                     vu.stock_code
        """),
    )
    universe_rows = list(universe_result.fetchall())
    intraday_candidates = await _stage1_intraday_top50_candidates(db)
    universe_codes = {str(_stage1_row_value(row, "stock_code")) for row in universe_rows if _stage1_row_value(row, "stock_code")}
    min_visible_change = _safe_float(thresholds.get("min_change_rate_pct")) or 0.0
    use_intraday_only = strategy_type == "scalping_pullback" and min_visible_change >= 3.0
    backfill_result: dict[str, Any] | None = None
    if use_intraday_only and not intraday_candidates:
        backfill_result = await _enqueue_stage1_snapshot_backfill(db)
    if use_intraday_only:
        universe_rows = list(intraday_candidates.values())
        universe_codes = set()
    else:
        for code, candidate in intraday_candidates.items():
            if code not in universe_codes:
                universe_rows.append(candidate)
    raw_count = len(universe_rows)
    stock_codes = [str(_stage1_row_value(row, "stock_code")) for row in universe_rows if _stage1_row_value(row, "stock_code")]
    if use_intraday_only:
        live_data = {
            str(_stage1_row_value(row, "stock_code")): {
                "stock_name": _stage1_row_value(row, "stock_name"),
                "current_price": _num(_stage1_row_value(row, "current_price")),
                "change_rate_pct": _num(_stage1_row_value(row, "change_rate_pct")),
                "volume": _stage1_row_value(row, "volume"),
                "trading_value_krw": _num(_stage1_row_value(row, "trading_value_krw")),
                "total_trading_value_krw": _num(_stage1_row_value(row, "total_trading_value_krw")),
                "market_trading_value_krw": _num(_stage1_row_value(row, "trading_value_krw")),
                "market_trading_value_source": "snapshot_cumulative",
                "trading_value_source": _stage1_row_value(row, "trading_value_source"),
                "quote_time": _stage1_row_value(row, "quote_time"),
                "freshness_status": _stage1_row_value(row, "freshness_status"),
                "data_source": _stage1_row_value(row, "data_source"),
                "is_nxt": bool(_stage1_row_value(row, "is_nxt")),
                "market": _stage1_row_value(row, "market"),
            }
            for row in universe_rows
            if _stage1_row_value(row, "stock_code")
        }
        # #303 Stage 1 already carries latest snapshot price/change/trade amount.
        # Heavy live enrichment (tick source split/fallback OHLCV) is kept out of
        # the main list path; detailed time-window supply is loaded lazily.
        enriched_live = {}
        for code, enriched in enriched_live.items():
            if code not in live_data:
                continue
            base_live = live_data[code]
            for key in (
                "market_trading_value_krw", "market_trading_value_source",
                "market_trading_value_quote_time", "nxt_trading_value_krw",
                "nxt_trading_value_source", "nxt_trading_value_quote_time",
                "total_trading_value_krw", "trading_value_krw",
                "trading_value_source", "quote_age_sec", "freshness_status",
                "upper_limit_price", "distance_to_limit_price", "distance_to_limit_pct",
                "missing_reason",
            ):
                if enriched.get(key) is not None:
                    base_live[key] = enriched[key]
            base_live["intraday_ranked_market_trading_value_krw"] = _num(
                _stage1_row_value(intraday_candidates.get(code, {}), "trading_value_krw")
            )
        ranked_codes = sorted(
            stock_codes,
            key=lambda item_code: (
                -(_safe_float(live_data.get(item_code, {}).get("total_trading_value_krw")) or 0.0),
                -(_safe_float(live_data.get(item_code, {}).get("change_rate_pct")) or 0.0),
                item_code,
            ),
        )
        for rank, ranked_code in enumerate(ranked_codes, start=1):
            if ranked_code in intraday_candidates:
                intraday_candidates[ranked_code]["bullish_trade_value_rank"] = rank
                intraday_candidates[ranked_code]["intraday_change_rank"] = rank
    else:
        live_data = await _enrich_stocks_with_live_data(db, stock_codes) if stock_codes else {}
    # 시간대 거래대금은 별도 endpoint에서 후보군만 지연 조회한다. 메인 workbench
    # 응답 경로는 후보 50개/등락률/누적 거래대금 표시를 빠르게 반환한다.
    trade_value_windows: dict[str, dict[str, Any]] = {}
    trade_value_window_summary: dict[str, Any] = {
        "status": "lazy",
        "source": "v4_ohlcv_minute",
        "endpoint": "/api/go100/strategy-cards/{card_id}/trade-value-windows",
        "candidate_count": len(stock_codes),
    }

    visible_rows: list[dict[str, Any]] = []
    excluded_below_min_change_count = 0
    status_counts: dict[str, int] = {}
    for universe_row in universe_rows:
        code = _stage1_row_value(universe_row, "stock_code")
        live = _stage1_live_for_code(live_data, code)
        live_trade_value = _safe_float(live.get("trading_value_krw"))
        live = {
            **live,
            "is_nxt": bool(live.get("is_nxt")) or bool(_stage1_row_value(universe_row, "is_nxt")),
            "trading_value_source": live.get("trading_value_source") or (
                "intraday_cumulative" if live_trade_value is not None else None
            ),
        }
        change_rate = _safe_float(live.get("change_rate_pct"))
        if change_rate is not None and change_rate < min_visible_change and not use_intraday_only:
            excluded_below_min_change_count += 1
            continue
        candidate_status, reasons, threshold_passes = _stage1_candidate_status(live, thresholds)
        if change_rate is not None and change_rate < min_visible_change and use_intraday_only:
            excluded_below_min_change_count += 1
        intraday_ranked = bool(_stage1_row_value(universe_row, "intraday_change_rank")) or bool(_stage1_row_value(universe_row, "bullish_trade_value_rank"))
        if intraday_ranked and not reasons:
            reasons = [str(_stage1_row_value(universe_row, "reason_text", "당일 top100 후보"))]
        rendered = _stage1_universe_row(
            universe_row, live, thresholds, candidate_status, reasons,
            threshold_passes, card_version,
        )
        rendered["trade_value_windows"] = trade_value_windows.get(str(code), {})
        visible_rows.append(rendered)
        status_counts[candidate_status] = status_counts.get(candidate_status, 0) + 1

    if use_intraday_only:
        visible_rows.sort(
            key=lambda row: (
                -(_safe_float(row.get("total_trading_value_krw")) or 0.0),
                -(_safe_float(row.get("change_rate_pct")) or 0.0),
                str(row.get("stock_code") or ""),
            )
        )

    first_updated = None
    if universe_rows:
        first_updated = _stage1_row_value(universe_rows[0], "updated_at") or _stage1_row_value(universe_rows[0], "created_date")
    return {
        "stage_id": 1,
        "stage_key": "target_selection",
        "label": "종목선정 후보",
        "count": len(visible_rows),
        "total_evaluations": len(visible_rows),
        "unique_stocks": len(visible_rows),
        "status": "available" if visible_rows else "empty",
        "updated_at": _ts(first_updated),
        "source": "v4_scalping_universe",
        "fallback_reason": None,
        "is_paper_filter_applied": False,
        "stage_columns": _STAGE1_COLUMNS.get(strategy_type, _STAGE1_COLUMNS["default"]),
        "rows": visible_rows,
        "summary": {
            "by_phase": [],
            "candidate_universe_count": raw_count,
            "static_universe_count": len(universe_codes),
            "intraday_top50_unique_count": len(intraday_candidates),
            "intraday_change3_count": sum(1 for row in intraday_candidates.values() if row.get("intraday_change_rank") is not None),
            "bullish_trade_value_top50_count": sum(1 for row in intraday_candidates.values() if row.get("bullish_trade_value_rank") is not None),
            "dynamic_intraday_source": "stock_price_snapshot",
            "candidate_design": "change_pct >= 3.0 AND KRX+NXT total_trading_value top50",
            "sort_order": "total_trading_value_krw DESC, change_rate_pct DESC, stock_code ASC",
            "strategy_flow": [
                {
                    "step": "발굴",
                    "rule": "당일 change_pct >= 3.0 종목 중 거래대금 상위 50",
                    "source": "stock_price_snapshot",
                },
                {
                    "step": "매매선정",
                    "rule": "실시간 가격/거래대금/신선도/리스크/쿨다운 gate 통과",
                    "source": "stage1 threshold_passes + candidate_status",
                },
                {
                    "step": "진입",
                    "rule": "1분봉 눌림 반등을 중심으로 3분/5분 MTF 파동 위치를 보조 확인",
                    "source": "scalping_entry_engine wave metrics",
                },
                {
                    "step": "청산",
                    "rule": "고정 TP/SL fallback과 트레일링, first-wave/MA-wave 청산 trigger 병행",
                    "source": "scalping_monitor exit rules",
                },
            ],
            "backfill": backfill_result,
            "trade_value_windows": trade_value_window_summary,
            "visible_count": len(visible_rows),
            "row_count": len(visible_rows),
            "top50_count": len(visible_rows) if use_intraday_only else len(intraday_candidates),
            "rejected_count": status_counts.get("rejected", 0),
            "data_missing_count": status_counts.get("data_missing", 0),
            "stale_count": status_counts.get("stale", 0),
            "qualified_count": status_counts.get("qualified", 0),
            "excluded_below_min_change_count": excluded_below_min_change_count,
            "excluded_negative_change_count": excluded_below_min_change_count,
            "by_status": status_counts,
        },
    }


async def _stage1_card119_preopen_expected_rows(
    db: AsyncSession,
    *,
    limit: int,
    min_change_pct: float,
) -> list[dict[str, Any]]:
    """Return #119 pre-open candidates from Kiwoom 0H expected-change Redis keys."""
    if datetime.now(KST).time() >= datetime.strptime("09:00", "%H:%M").time():
        return []
    try:
        import redis as sync_redis

        redis_client = sync_redis.from_url(
            os.environ.get("REDIS_URL", "redis://localhost:6379/0"),
            decode_responses=True,
            socket_timeout=0.5,
        )
        ranked: list[tuple[str, float, dict[str, Any]]] = []
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
            expected_change = payload.get("expected_change_rate")
            if expected_change is None:
                expected_change = payload.get("change_rate")
            expected_change_f = _safe_float(expected_change)
            if expected_change_f is None or expected_change_f < min_change_pct:
                continue
            ranked.append((code, expected_change_f, payload))
    except Exception as exc:
        logger.debug("workbench #119 preopen expected-change load skipped: %s", exc)
        return []

    ranked.sort(key=lambda item: (-item[1], item[0]))
    codes = [code for code, _change, _payload in ranked[:limit]]
    if not codes:
        return []

    name_map: dict[str, str] = {}
    try:
        name_r = await db.execute(
            text("""
                SELECT stock_code, stock_name
                FROM stock_universe
                WHERE stock_code = ANY(:codes)
                  AND COALESCE(is_active, true) = true
            """),
            {"codes": codes},
        )
        name_map = {str(row.stock_code): row.stock_name for row in name_r.fetchall()}
    except Exception:
        name_map = {}

    rows: list[dict[str, Any]] = []
    for code, expected_change, payload in ranked[:limit]:
        if code not in name_map and name_map:
            continue
        rows.append({
            "stock_code": code,
            "stock_name": name_map.get(code) or payload.get("stock_name") or code,
            "current_price": _safe_float(payload.get("expected_price") or payload.get("current_price")),
            "change_rate_pct": expected_change,
            "volume": _safe_float(payload.get("expected_volume") or payload.get("volume")),
            "trading_value_krw": None,
            "total_trading_value_krw": None,
            "source_table": "redis:go100:kiwoom:0H",
            "created_date": datetime.now(KST),
            "updated_at": datetime.now(KST),
            "reason_text": f"장시작 전 예정당일등락률 +{expected_change:.2f}% 후보",
            "candidate_scope": "preopen_expected_watch",
            "intraday_candidate_bucket": ["watch_discovery"],
            "threshold_stage": "watch_discovery",
            "discovery_source": "preopen_expected_change",
            "is_nxt": False,
        })
    return rows


def _is_excluded_name_119(name: str, stock_code: str | None = None) -> bool:
    """Return True if the stock name matches ETF/ETN/SPAC/REIT/management exclusion patterns."""
    n_upper = (name or "").upper().replace(" ", "")
    code = str(stock_code or "").strip()
    is_foreign_listing = code.startswith("900") and len(code) == 6 and code.isdigit()
    excluded_prefixes = (
        "KODEX", "TIGER", "ACE", "SOL", "KBSTAR", "HANARO", "ARIRANG",
        "KOSEF", "TIMEFOLIO", "RISE", "PLUS", "WON", "TREX", "마이티",
    )
    if is_foreign_listing and not n_upper:
        return False
    return any([
        any(n_upper.startswith(prefix.upper()) for prefix in excluded_prefixes),
        "ETF" in n_upper, "ETN" in n_upper,
        "레버리지" in name, "인버스" in name, "선물" in name,
        "채권" in name, "국채" in name, "통안채" in name,
        "스팩" in name, "SPAC" in n_upper, "리츠" in name, "REIT" in n_upper,
        "관리종목" in name, "정리매매" in name,
        "우선주" in name,
        name.endswith("우") or name.endswith("우B") or name.endswith("우C"),
    ])


async def _stage1_card119_cumulative_candidates(
    db: AsyncSession,
    *,
    trade_date: Any,
    min_change_pct: float,
    min_trade_value_krw: float,
    limit: int,
) -> dict[str, dict[str, Any]]:
    """Return today's cumulative #119 candidates from BOTH decision logs and strategy run events.

    Unions the live candidate snapshot ledger with go100_trade_decision_logs
    (stage=candidate_generation) and go100_strategy_run_events
    (stage/event_phase=candidate_generation or data_quality_gate) for card 119.
    Aggregates per stock across sources and records which tables contributed.
    """
    try:
        cumul_r = await db.execute(
            text(r"""
                WITH from_candidate_snapshots AS (
                    SELECT
                        symbol AS stock_code,
                        MAX(change_rate::numeric) AS max_seen_change_pct,
                        MAX(captured_at) AS last_seen,
                        MIN(captured_at) AS first_seen,
                        (array_agg(stock_name ORDER BY captured_at DESC NULLS LAST))[1] AS stock_name,
                        (array_agg(
                            CASE
                                WHEN COALESCE(raw_payload->>'price', raw_payload->>'current_price') ~ '^-?[0-9]+(\.[0-9]+)?$'
                                THEN COALESCE(raw_payload->>'price', raw_payload->>'current_price')::numeric
                                ELSE NULL
                            END
                            ORDER BY captured_at DESC NULLS LAST
                        ))[1] AS last_price,
                        MAX(
                            CASE
                                WHEN COALESCE(
                                    raw_payload->>'effective_trade_amount_krw',
                                    raw_payload->>'trade_amount',
                                    raw_payload->>'trading_value',
                                    raw_payload->>'total_trading_value_krw'
                                ) ~ '^-?[0-9]+(\.[0-9]+)?$'
                                THEN COALESCE(
                                    raw_payload->>'effective_trade_amount_krw',
                                    raw_payload->>'trade_amount',
                                    raw_payload->>'trading_value',
                                    raw_payload->>'total_trading_value_krw'
                                )::numeric
                                ELSE NULL
                            END
                        ) AS max_seen_trade_value_krw,
                        'go100_card119_candidate_snapshots'::text AS source_table
                    FROM go100_card119_candidate_snapshots
                    WHERE card_id = 119
                      AND trading_date = :trade_date
                      AND change_rate IS NOT NULL
                      AND change_rate::numeric >= :min_change
                    GROUP BY symbol
                ),
                from_decision_logs AS (
                    SELECT
                        stock_code,
                        MAX((metrics_json->>'change_pct')::numeric) AS max_seen_change_pct,
                        MAX(created_at) AS last_seen,
                        MIN(created_at) AS first_seen,
                        (array_agg(metrics_json->>'stock_name'
                            ORDER BY created_at DESC NULLS LAST))[1] AS stock_name,
                        (array_agg((metrics_json->>'price')::numeric
                            ORDER BY created_at DESC NULLS LAST))[1] AS last_price,
                        MAX(COALESCE(
                            NULLIF(metrics_json->>'effective_trade_amount_krw', '')::numeric,
                            NULLIF(metrics_json->>'trade_amount', '')::numeric,
                            NULLIF(metrics_json->>'trading_value', '')::numeric,
                            0
                        )) AS max_seen_trade_value_krw,
                        'go100_trade_decision_logs'::text AS source_table
                    FROM go100_trade_decision_logs
                    WHERE go100_card_id = 119
                      AND trade_date = :trade_date
                      AND stage = 'candidate_generation'
                      AND (metrics_json->>'change_pct') IS NOT NULL
                      AND (metrics_json->>'change_pct')::numeric >= :min_change
                      AND COALESCE(
                          NULLIF(metrics_json->>'effective_trade_amount_krw', '')::numeric,
                          NULLIF(metrics_json->>'trade_amount', '')::numeric,
                          NULLIF(metrics_json->>'trading_value', '')::numeric,
                          0
                      ) >= :min_trade_value
                    GROUP BY stock_code
                ),
                from_run_events AS (
                    SELECT
                        stock_code,
                        MAX((metrics_json->>'change_pct')::numeric) AS max_seen_change_pct,
                        MAX(created_at) AS last_seen,
                        MIN(created_at) AS first_seen,
                        (array_agg(metrics_json->>'stock_name'
                            ORDER BY created_at DESC NULLS LAST))[1] AS stock_name,
                        (array_agg((metrics_json->>'price')::numeric
                            ORDER BY created_at DESC NULLS LAST))[1] AS last_price,
                        MAX(COALESCE(
                            NULLIF(metrics_json->>'effective_trade_amount_krw', '')::numeric,
                            NULLIF(metrics_json->>'trade_amount', '')::numeric,
                            NULLIF(metrics_json->>'trading_value', '')::numeric,
                            0
                        )) AS max_seen_trade_value_krw,
                        'go100_strategy_run_events'::text AS source_table
                    FROM go100_strategy_run_events
                    WHERE go100_card_id = 119
                      AND trade_date = :trade_date
                      AND (stage IN ('candidate_generation', 'data_quality_gate')
                           OR event_phase IN ('candidate_generation', 'data_quality_gate'))
                      AND (metrics_json->>'change_pct') IS NOT NULL
                      AND (metrics_json->>'change_pct')::numeric >= :min_change
                      AND COALESCE(
                          NULLIF(metrics_json->>'effective_trade_amount_krw', '')::numeric,
                          NULLIF(metrics_json->>'trade_amount', '')::numeric,
                          NULLIF(metrics_json->>'trading_value', '')::numeric,
                          0
                      ) >= :min_trade_value
                    GROUP BY stock_code
                ),
                combined AS (
                    SELECT * FROM from_candidate_snapshots
                    UNION ALL
                    SELECT * FROM from_decision_logs
                    UNION ALL
                    SELECT * FROM from_run_events
                )
                SELECT
                    stock_code,
                    MAX(max_seen_change_pct) AS max_seen_change_pct,
                    MAX(last_seen) AS last_seen,
                    MIN(first_seen) AS first_seen,
                    (array_agg(stock_name ORDER BY last_seen DESC NULLS LAST))[1] AS stock_name,
                    (array_agg(last_price ORDER BY last_seen DESC NULLS LAST))[1] AS last_price,
                    MAX(max_seen_trade_value_krw) AS max_seen_trade_value_krw,
                    CASE
                        WHEN COUNT(DISTINCT source_table) > 1 THEN 'both'
                        ELSE MAX(source_table)
                    END AS data_source,
                    array_agg(DISTINCT source_table ORDER BY source_table) AS source_tables
                FROM combined
                GROUP BY stock_code
                ORDER BY MAX(max_seen_change_pct) DESC
                LIMIT :lim
            """),
            {
                "trade_date": trade_date,
                "min_change": min_change_pct,
                "min_trade_value": min_trade_value_krw,
                "lim": limit,
            },
        )
        result: dict[str, dict[str, Any]] = {}
        for row in cumul_r.fetchall():
            code = str(row.stock_code)
            data_source = row.data_source or "go100_trade_decision_logs"
            source_tables = list(row.source_tables) if row.source_tables else [data_source]
            result[code] = {
                "stock_code": code,
                "stock_name": row.stock_name or code,
                "max_seen_change_pct": _safe_float(row.max_seen_change_pct),
                "last_seen": row.last_seen,
                "first_seen": row.first_seen,
                "last_price": _safe_float(row.last_price),
                "max_seen_trade_value_krw": _safe_float(row.max_seen_trade_value_krw),
                "data_source": data_source,
                "source_tables": source_tables,
            }
        return result
    except Exception as exc:
        logger.warning("workbench #119 cumulative candidates query failed: %s", exc)
        return {}


async def _build_stage1_card119_independent_stage(
    db: AsyncSession,
    *,
    thresholds: dict[str, Optional[float]],
    card_version: int,
) -> dict[str, Any]:
    """#119 Stage 1: independent +20% watch/discovery matching live observation.

    Rows are split into two candidate_scope buckets:
    - current_snapshot_watch: currently at >=20% and >=1억원 in stock_price_snapshot
    - today_cumulative_watch: seen at >=20% and >=1억원 earlier today (from go100_trade_decision_logs
      AND/OR go100_strategy_run_events) but no longer visible in the current snapshot

    discovery_source records the underlying data system (stock_price_snapshot,
    go100_trade_decision_logs, go100_strategy_run_events, or 'both') whereas
    candidate_scope records the bucket classification.
    """
    min_change = _CARD119_DISCOVERY_MIN_CHANGE_PCT
    min_trade_value = _CARD119_DISCOVERY_MIN_TRADE_VALUE_KRW
    limit = int(os.environ.get("GO100_CARD119_DISCOVERY_LIMIT", "200"))
    today_kst = datetime.now(KST).date()
    today_start_kst = datetime.combine(today_kst, datetime.min.time(), tzinfo=KST)
    tomorrow_start_kst = today_start_kst + timedelta(days=1)

    rows_by_code: dict[str, dict[str, Any]] = {}
    for row in await _stage1_card119_preopen_expected_rows(db, limit=limit, min_change_pct=min_change):
        entry = dict(row)
        entry["candidate_scope"] = "preopen_expected_watch"
        entry["threshold_stage"] = "watch_discovery"
        entry["discovery_source"] = "redis:go100:kiwoom:0H"
        rows_by_code[str(row["stock_code"])] = entry

    snapshot_r = await db.execute(
        text("""
            WITH latest_snapshot AS (
                SELECT DISTINCT ON (sps.stock_code)
                       sps.stock_code,
                       COALESCE(su.stock_name, sps.stock_code) AS stock_name,
                       COALESCE(su.market, 'UNKNOWN') AS market,
                       COALESCE(su.is_nxt, false) AS is_nxt,
                       sps.price AS current_price,
                       COALESCE(sps.change_pct, 0)::numeric AS change_rate_pct,
                       COALESCE(sps.volume, 0)::bigint AS volume,
                       COALESCE(sps.trade_amount, 0)::numeric AS trading_value_krw,
                       sps.snapshot_time AS updated_at
                FROM stock_price_snapshot sps
                LEFT JOIN stock_universe su ON su.stock_code = sps.stock_code
                WHERE sps.snapshot_time >= :today_start_kst
                  AND sps.snapshot_time < :tomorrow_start_kst
                  AND COALESCE(su.is_active, true) = true
                  AND COALESCE(sps.change_pct, 0) >= :min_change
                  AND CASE WHEN COALESCE(sps.trade_amount, 0) > 0 AND COALESCE(sps.trade_amount, 0) < 10000000
                           THEN COALESCE(sps.trade_amount, 0) * 1000000
                           ELSE COALESCE(sps.trade_amount, 0)
                      END >= :min_trade_value
                  AND NOT (
                      UPPER(REPLACE(COALESCE(su.stock_name, sps.stock_code, ''), ' ', '')) LIKE 'KODEX%'
                      OR UPPER(REPLACE(COALESCE(su.stock_name, sps.stock_code, ''), ' ', '')) LIKE 'TIGER%'
                      OR UPPER(REPLACE(COALESCE(su.stock_name, sps.stock_code, ''), ' ', '')) LIKE 'ACE%'
                      OR UPPER(REPLACE(COALESCE(su.stock_name, sps.stock_code, ''), ' ', '')) LIKE 'SOL%'
                      OR UPPER(REPLACE(COALESCE(su.stock_name, sps.stock_code, ''), ' ', '')) LIKE 'KBSTAR%'
                      OR UPPER(REPLACE(COALESCE(su.stock_name, sps.stock_code, ''), ' ', '')) LIKE 'HANARO%'
                      OR UPPER(REPLACE(COALESCE(su.stock_name, sps.stock_code, ''), ' ', '')) LIKE 'ARIRANG%'
                      OR UPPER(REPLACE(COALESCE(su.stock_name, sps.stock_code, ''), ' ', '')) LIKE 'KOSEF%'
                      OR UPPER(REPLACE(COALESCE(su.stock_name, sps.stock_code, ''), ' ', '')) LIKE 'TIMEFOLIO%'
                      OR UPPER(REPLACE(COALESCE(su.stock_name, sps.stock_code, ''), ' ', '')) LIKE 'RISE%'
                      OR UPPER(REPLACE(COALESCE(su.stock_name, sps.stock_code, ''), ' ', '')) LIKE 'PLUS%'
                      OR UPPER(REPLACE(COALESCE(su.stock_name, sps.stock_code, ''), ' ', '')) LIKE 'WON%'
                      OR UPPER(REPLACE(COALESCE(su.stock_name, sps.stock_code, ''), ' ', '')) LIKE 'TREX%'
                      OR REPLACE(COALESCE(su.stock_name, sps.stock_code, ''), ' ', '') LIKE '마이티%'
                      OR UPPER(COALESCE(su.stock_name, sps.stock_code, '')) LIKE '%ETF%'
                      OR UPPER(COALESCE(su.stock_name, sps.stock_code, '')) LIKE '%ETN%'
                      OR COALESCE(su.stock_name, sps.stock_code, '') LIKE '%레버리지%'
                      OR COALESCE(su.stock_name, sps.stock_code, '') LIKE '%인버스%'
                      OR COALESCE(su.stock_name, sps.stock_code, '') LIKE '%선물%'
                      OR COALESCE(su.stock_name, sps.stock_code, '') LIKE '%채권%'
                      OR COALESCE(su.stock_name, sps.stock_code, '') LIKE '%국채%'
                      OR COALESCE(su.stock_name, sps.stock_code, '') LIKE '%통안채%'
                      OR COALESCE(su.stock_name, sps.stock_code, '') LIKE '%스팩%'
                      OR UPPER(COALESCE(su.stock_name, sps.stock_code, '')) LIKE '%SPAC%'
                      OR COALESCE(su.stock_name, sps.stock_code, '') LIKE '%리츠%'
                      OR UPPER(COALESCE(su.stock_name, sps.stock_code, '')) LIKE '%REIT%'
                      OR COALESCE(su.stock_name, sps.stock_code, '') LIKE '%관리%'
                      OR COALESCE(su.stock_name, sps.stock_code, '') LIKE '%정리%'
                      OR COALESCE(su.stock_name, sps.stock_code, '') LIKE '%우선주%'
                      OR COALESCE(su.stock_name, sps.stock_code, '') LIKE '%우'
                      OR COALESCE(su.stock_name, sps.stock_code, '') LIKE '%우B'
                      OR COALESCE(su.stock_name, sps.stock_code, '') LIKE '%우C'
                  )
                ORDER BY sps.stock_code, sps.snapshot_time DESC
            )
            SELECT *
            FROM latest_snapshot
            ORDER BY change_rate_pct DESC, trading_value_krw DESC, stock_code ASC
            LIMIT :lim
        """),
        {
            "today_start_kst": today_start_kst,
            "tomorrow_start_kst": tomorrow_start_kst,
            "min_change": min_change,
            "min_trade_value": min_trade_value,
            "lim": limit,
        },
    )
    for row in snapshot_r.fetchall():
        code = str(row.stock_code)
        current_price = _safe_float(row.current_price)
        volume = int(row.volume or 0) if row.volume is not None else None
        trading_value = _normalize_trading_value_krw(row.trading_value_krw, price=current_price, volume=volume)
        rows_by_code[code] = {
            "stock_code": code,
            "stock_name": row.stock_name,
            "market": row.market,
            "current_price": current_price,
            "change_rate_pct": _safe_float(row.change_rate_pct),
            "volume": volume,
            "trading_value_krw": trading_value,
            "total_trading_value_krw": trading_value,
            "source_table": "stock_price_snapshot",
            "created_date": row.updated_at,
            "updated_at": row.updated_at,
            "reason_text": f"당일 등락률 +{float(row.change_rate_pct or 0):.2f}%·거래대금 1억원 이상 독립 발굴 후보",
            "candidate_scope": "current_snapshot_watch",
            "intraday_candidate_bucket": ["watch_discovery"],
            "threshold_stage": "watch_discovery",
            "discovery_source": "stock_price_snapshot",
            "is_nxt": bool(row.is_nxt),
        }

    # Track which codes are currently visible at the #119 entry-gate discovery floor.
    current_snapshot_codes = set(rows_by_code.keys())

    # Merge today's cumulative candidates from BOTH decision logs and strategy run events
    cumul_by_code = await _stage1_card119_cumulative_candidates(
        db,
        trade_date=today_kst,
        min_change_pct=min_change,
        min_trade_value_krw=min_trade_value,
        limit=limit,
    )
    for code, cumul_data in cumul_by_code.items():
        cumul_ds = cumul_data.get("data_source", "go100_trade_decision_logs")
        if code in current_snapshot_codes:
            # Already visible at the entry-gate discovery floor: annotate with max-seen info and cumulative source
            rows_by_code[code]["max_seen_change_pct"] = cumul_data["max_seen_change_pct"]
            rows_by_code[code]["max_seen_trade_value_krw"] = cumul_data.get("max_seen_trade_value_krw")
            rows_by_code[code]["last_seen"] = cumul_data["last_seen"]
            rows_by_code[code]["cumulative_data_source"] = cumul_ds
        else:
            # Was seen at the entry-gate discovery floor earlier today but has since dropped below threshold
            stock_name = cumul_data["stock_name"] or code
            if _is_excluded_name_119(stock_name):
                continue
            rows_by_code[code] = {
                "stock_code": code,
                "stock_name": stock_name,
                "market": None,
                "current_price": cumul_data["last_price"],
                "change_rate_pct": None,
                "volume": None,
                "trading_value_krw": None,
                "total_trading_value_krw": cumul_data.get("max_seen_trade_value_krw"),
                "source_table": cumul_ds,
                "created_date": cumul_data["last_seen"],
                "updated_at": cumul_data["last_seen"],
                "reason_text": f"오늘 최고 등락률 +{cumul_data['max_seen_change_pct']:.2f}%·거래대금 1억원 이상 watch 기록 (현재 watch 기준 미만)",
                "candidate_scope": "today_cumulative_watch",
                "intraday_candidate_bucket": ["today_cumulative_watch"],
                "threshold_stage": "watch_discovery",
                "discovery_source": cumul_ds,
                "max_seen_change_pct": cumul_data["max_seen_change_pct"],
                "max_seen_trade_value_krw": cumul_data.get("max_seen_trade_value_krw"),
                "last_seen": cumul_data["last_seen"],
                "is_nxt": False,
            }

    # Compute counts before building visible_rows
    cumulative_only_codes = set(cumul_by_code.keys()) - current_snapshot_codes
    cumulative_watch_count = len(cumul_by_code)
    current_watch_count = len(current_snapshot_codes)
    cumulative_only_count = len(cumulative_only_codes)

    # Count cumulative candidates by source for summary
    decision_logs_count = sum(
        1 for v in cumul_by_code.values()
        if v.get("data_source") in ("go100_trade_decision_logs", "both")
    )
    candidate_snapshots_count = sum(
        1 for v in cumul_by_code.values()
        if "go100_card119_candidate_snapshots" in (v.get("source_tables") or [])
    )
    strategy_run_events_count = sum(
        1 for v in cumul_by_code.values()
        if v.get("data_source") in ("go100_strategy_run_events", "both")
    )
    both_sources_count = sum(
        1 for v in cumul_by_code.values() if v.get("data_source") == "both"
    )

    candidate_rows = list(rows_by_code.values())
    stock_codes = [str(row["stock_code"]) for row in candidate_rows if row.get("stock_code")]
    live_data = await _enrich_stocks_with_live_data(db, stock_codes) if stock_codes else {}
    visible_rows: list[dict[str, Any]] = []
    status_counts: dict[str, int] = {}
    for raw_row in candidate_rows:
        code = raw_row.get("stock_code")
        live = {**raw_row, **_stage1_live_for_code(live_data, code)}
        live.setdefault("trading_value_krw", raw_row.get("trading_value_krw"))
        live.setdefault("total_trading_value_krw", raw_row.get("total_trading_value_krw"))
        scope = raw_row.get("candidate_scope", "current_snapshot_watch")
        eval_thresholds = {
            **thresholds,
            "min_change_rate_pct": min_change if scope != "today_cumulative_watch" else 0.0,
        }
        candidate_status, reasons, threshold_passes = _stage1_candidate_status(live, eval_thresholds)
        if not reasons:
            reasons = [str(raw_row.get("reason_text") or "당일 +20%·거래대금 1억원 이상 watch/discovery 후보")]
        rendered = _stage1_universe_row(raw_row, live, eval_thresholds, candidate_status, reasons, threshold_passes, card_version)
        rendered["candidate_scope"] = scope
        rendered["discovery_source"] = raw_row.get("discovery_source") or scope
        rendered["detailed_reason"] = "; ".join(reasons)
        rendered["max_seen_change_pct"] = raw_row.get("max_seen_change_pct")
        rendered["last_seen"] = str(raw_row["last_seen"]) if raw_row.get("last_seen") else None
        rendered["cumulative_data_source"] = raw_row.get("cumulative_data_source")
        visible_rows.append(rendered)
        status_counts[candidate_status] = status_counts.get(candidate_status, 0) + 1

    def _row_sort_key(row: dict[str, Any]) -> tuple:
        is_cumul = 1 if row.get("candidate_scope") == "today_cumulative_watch" else 0
        if is_cumul:
            max_seen = _safe_float(row.get("max_seen_change_pct")) or 0.0
            last_seen_raw = row.get("last_seen")
            last_ts = 0.0
            if last_seen_raw:
                try:
                    last_ts = float(last_seen_raw) if isinstance(last_seen_raw, (int, float)) else 0.0
                except Exception:
                    last_ts = 0.0
            return (1, -max_seen, -last_ts, str(row.get("stock_code") or ""))
        change = _safe_float(row.get("change_rate_pct")) or 0.0
        tv = _safe_float(row.get("total_trading_value_krw")) or 0.0
        return (0, -change, -tv, str(row.get("stock_code") or ""))

    visible_rows.sort(key=_row_sort_key)
    first_updated = visible_rows[0].get("created_at") if visible_rows else None
    return {
        "stage_id": 1,
        "stage_key": "target_selection",
        "label": "#119 독립 발굴종목",
        "count": len(visible_rows),
        "total_evaluations": len(visible_rows),
        "unique_stocks": len(visible_rows),
        "status": "available" if visible_rows else "empty",
        "updated_at": first_updated,
        "source": "card119_independent_discovery",
        "fallback_reason": None,
        "is_paper_filter_applied": False,
        "stage_columns": _STAGE1_COLUMNS.get("limitup_chase", _STAGE1_COLUMNS["default"]),
        "rows": visible_rows,
        "summary": {
            "candidate_universe_count": len(candidate_rows),
            "visible_count": len(visible_rows),
            "common_universe_used": False,
            "discovery_min_change_pct": min_change,
            "discovery_min_trade_value_krw": min_trade_value,
            "entry_min_change_pct": _CARD119_ENTRY_MIN_CHANGE_PCT,
            "current_watch_count": current_watch_count,
            "cumulative_watch_count": cumulative_watch_count,
            "cumulative_only_count": cumulative_only_count,
            "candidate_snapshots_count": candidate_snapshots_count,
            "decision_logs_count": decision_logs_count,
            "strategy_run_events_count": strategy_run_events_count,
            "both_sources_count": both_sources_count,
            "candidate_design": (
                f"preopen expected_change_rate >= {min_change:.0f} OR "
                f"intraday change_pct >= {min_change:.0f} and trading_value >= {min_trade_value:,.0f} (current) "
                f"+ today candidate_snapshots/decision_logs/strategy_run_events >= {min_change:.0f} (cumulative)"
            ),
            "cumulative_sources": [
                "go100_card119_candidate_snapshots",
                "go100_trade_decision_logs",
                "go100_strategy_run_events",
            ],
            "selection_scope": "#119 매매선정은 +20% watch/discovery 후보 안에서만 수행",
            "entry_gate": "+27% BUY 하드플로어와 고가권/잠김점수/재료/리스크/틱 모멘텀 게이트 통과 시 매수",
            "by_status": status_counts,
        },
    }


async def _build_stage1_limitup_stage(
    db: AsyncSession,
    *,
    strategy_type: str,
    thresholds: dict[str, Optional[float]],
    card_version: int,
) -> dict[str, Any]:
    """상한가 추격 카드 Stage 1: go100_limitup_events 기반 당일/최근 상한가 대상 종목."""
    limitup_result = await db.execute(
        text("""
            SELECT le.stock_code,
                   COALESCE(le.stock_name, su.stock_name, le.stock_code) AS stock_name,
                   le.event_type, le.change_pct, le.close_price,
                   le.estimated_limit_price AS upper_limit_price,
                   le.is_first_limitup,
                   le.consecutive_days, le.theme_strength,
                   le.gap_band, le.trade_date,
                   le.lock_status, le.trade_amount, le.volume,
                   le.created_at, le.updated_at,
                   COALESCE(su.is_nxt, false) AS is_nxt
            FROM go100_limitup_events le
            LEFT JOIN stock_universe su ON su.stock_code = le.stock_code
            WHERE le.trade_date = (
                SELECT MAX(trade_date) FROM go100_limitup_events
                WHERE event_type IN ('limitup', 'near_limitup')
            )
              AND le.event_type IN ('limitup', 'near_limitup')
            ORDER BY le.change_pct DESC, le.close_price DESC
        """),
    )
    limitup_rows = limitup_result.fetchall()
    raw_count = len(limitup_rows)
    stock_codes = [str(_stage1_row_value(row, "stock_code")) for row in limitup_rows if _stage1_row_value(row, "stock_code")]
    live_data = await _enrich_stocks_with_live_data(db, stock_codes) if stock_codes else {}

    visible_rows: list[dict[str, Any]] = []
    status_counts: dict[str, int] = {}
    trade_date_str = None
    for row in limitup_rows:
        code = _stage1_row_value(row, "stock_code")
        live = _stage1_live_for_code(live_data, code)
        live = {
            **live,
            "is_nxt": bool(live.get("is_nxt")) or bool(_stage1_row_value(row, "is_nxt")),
        }
        if not trade_date_str:
            td = _stage1_row_value(row, "trade_date")
            trade_date_str = str(td) if td else None

        candidate_status, reasons, threshold_passes = _stage1_candidate_status(live, thresholds)
        event_type = _stage1_row_value(row, "event_type") or ""
        is_first = _stage1_row_value(row, "is_first_limitup")
        consec_days = _stage1_row_value(row, "consecutive_days")
        theme = _stage1_row_value(row, "theme_strength")

        type_label = "상한가" if event_type == "limitup" else "근접"
        first_label = "첫상한가" if is_first else f"연속{consec_days}일"
        detail_parts = [type_label, first_label]
        if theme:
            detail_parts.append(str(theme))

        rendered = {
            **_name_payload(code, _stage1_row_value(row, "stock_name")),
            "stage": "candidate_universe",
            "event_phase": "limitup_candidate",
            "decision": "candidate",
            "reason_text": " / ".join(detail_parts),
            "source_ts": None,
            "received_at": None,
            "trade_group_id": None,
            "card_version": card_version,
            "source_table": "go100_limitup_events",
            "created_at": _ts(_stage1_row_value(row, "created_at")),
            **{key: live.get(key) for key in _STAGE1_LIVE_FIELDS},
            "upper_limit_price": _num(_stage1_row_value(row, "upper_limit_price")) or live.get("upper_limit_price"),
            "change_pct": _num(_stage1_row_value(row, "change_pct")),
            "close_price": _num(_stage1_row_value(row, "close_price")),
            "event_type": event_type,
            "is_first_limitup": is_first,
            "consecutive_days": consec_days,
            "theme_strength": theme,
            "gap_band": _stage1_row_value(row, "gap_band"),
            "lock_status": _stage1_row_value(row, "lock_status"),
            "trade_amount": _num(_stage1_row_value(row, "trade_amount")),
            "threshold_values": {
                "min_change_rate_pct": thresholds.get("min_change_rate_pct"),
                "max_change_rate_pct": thresholds.get("max_change_rate_pct"),
                "min_trading_value_krw": thresholds.get("min_trading_value_krw"),
            },
            "threshold_passes": {
                **threshold_passes,
                "entry_window": True,
                "opening_lane": False,
                "nxt_preopen": False,
            },
            "candidate_status": candidate_status,
            "candidate_rejection_reasons": reasons,
            "detailed_reason": " / ".join(detail_parts),
        }
        visible_rows.append(rendered)
        status_counts[candidate_status] = status_counts.get(candidate_status, 0) + 1

    first_updated = None
    if limitup_rows:
        first_updated = _stage1_row_value(limitup_rows[0], "updated_at") or _stage1_row_value(limitup_rows[0], "trade_date")
    return {
        "stage_id": 1,
        "stage_key": "target_selection",
        "label": "상한가 대상종목",
        "count": len(visible_rows),
        "total_evaluations": len(visible_rows),
        "unique_stocks": len(visible_rows),
        "status": "available" if visible_rows else "empty",
        "updated_at": _ts(first_updated),
        "source": "go100_limitup_events",
        "fallback_reason": None,
        "is_paper_filter_applied": False,
        "stage_columns": _STAGE1_COLUMNS.get(strategy_type, _STAGE1_COLUMNS["default"]),
        "rows": visible_rows,
        "summary": {
            "by_phase": [],
            "candidate_universe_count": raw_count,
            "visible_count": len(visible_rows),
            "trade_date": trade_date_str,
            "by_status": status_counts,
        },
    }


async def _build_stage1_event_stage(
    db: AsyncSession,
    *,
    card_id: int,
    user_id: int,
    active_card_version: int,
    is_paper: Optional[bool],
    date_clause: str,
    date_params: dict[str, Any],
    normalized_regime: Optional[str],
    strategy_type: str,
    thresholds: dict[str, Optional[float]],
    fallback_reason: Optional[str],
    source: str = "go100_strategy_run_events",
) -> dict[str, Any]:
    """일반 카드 또는 유니버스 장애 시 기존 이벤트 원장을 fallback으로 구성한다."""
    s1_params: dict[str, Any] = {
        "card_id": card_id, "user_id": user_id,
        "card_version": active_card_version,
    }
    s1_where = (
        "go100_card_id = :card_id AND user_id = :user_id "
        "AND (card_version = :card_version OR card_version IS NULL) "
        "AND (stage IN ('data_quality_gate', 'candidate_generation') "
        "  OR event_phase IN ('data_quality_gate', 'candidate_generation'))"
    )
    if is_paper is not None:
        s1_where += " AND (is_paper = :is_paper OR is_paper IS NULL)"
        s1_params["is_paper"] = is_paper
    if date_clause:
        s1_where += f" AND {date_clause}"
        s1_params.update(date_params)
    s1_regime_clause, s1_regime_params = _regime_filter_clause("created_at", normalized_regime)
    if s1_regime_clause:
        s1_where += f" AND {s1_regime_clause}"
        s1_params.update(s1_regime_params)

    stats_result = await db.execute(
        text(f"""
            SELECT COUNT(*) AS total_evaluations,
                   COUNT(DISTINCT stock_code) AS unique_stocks,
                   MAX(created_at) AS last_at,
                   COUNT(DISTINCT (stock_code, (created_at AT TIME ZONE 'Asia/Seoul')::date)) AS pair_count
            FROM go100_strategy_run_events
            WHERE {s1_where}
        """),
        s1_params,
    )
    stats = stats_result.fetchone()
    total_evaluations = int(stats.total_evaluations or 0) if stats else 0
    unique_stocks = int(stats.unique_stocks or 0) if stats else 0
    last_at = stats.last_at if stats else None
    count = int(stats.pair_count or 0) if stats else 0

    rows_result = await db.execute(
        text(f"""
            SELECT e.stock_code,
                   COALESCE(su.stock_name, su_norm.stock_name, e.stock_code) AS stock_name,
                   e.stage, e.event_phase, e.decision, e.reason_text,
                   e.source_ts, e.received_at, e.trade_group_id, e.card_version,
                   COALESCE(e.source_table, e.source) AS source_table, e.created_at
            FROM (
                SELECT *
                FROM go100_strategy_run_events
                WHERE {s1_where}
                ORDER BY created_at DESC
                LIMIT 10
            ) e
            LEFT JOIN stock_universe su ON su.stock_code = e.stock_code
            LEFT JOIN stock_universe su_norm
              ON regexp_replace(su_norm.stock_code, '[^0-9]', '', 'g') = right(regexp_replace(e.stock_code, '[^0-9]', '', 'g'), 6)
            ORDER BY e.created_at DESC
        """),
        s1_params,
    )
    event_rows = rows_result.fetchall()
    summary_result = await db.execute(
        text(f"""
            SELECT event_phase, stage, COUNT(*) AS cnt
            FROM go100_strategy_run_events
            WHERE {s1_where}
            GROUP BY event_phase, stage
            ORDER BY cnt DESC
        """),
        s1_params,
    )
    summary_rows = summary_result.fetchall()

    stock_codes = [str(_stage1_row_value(row, "stock_code")) for row in event_rows if _stage1_row_value(row, "stock_code")]
    live_data = await _enrich_stocks_with_live_data(db, stock_codes) if stock_codes else {}
    rendered_rows: list[dict[str, Any]] = []
    for row in event_rows:
        code = _stage1_row_value(row, "stock_code")
        live = _stage1_live_for_code(live_data, code)
        candidate_status, reasons, threshold_passes = _stage1_candidate_status(live, thresholds)
        rendered_rows.append({
            **_name_payload(code, _stage1_row_value(row, "stock_name")),
            "stage": _stage1_row_value(row, "stage"),
            "event_phase": _stage1_row_value(row, "event_phase"),
            "decision": _stage1_row_value(row, "decision"),
            "reason_text": _stage1_row_value(row, "reason_text"),
            "source_ts": _ts(_stage1_row_value(row, "source_ts")),
            "received_at": _ts(_stage1_row_value(row, "received_at")),
            "trade_group_id": _stage1_row_value(row, "trade_group_id"),
            "card_version": _stage1_row_value(row, "card_version"),
            "source_table": _stage1_row_value(row, "source_table"),
            "created_at": _ts(_stage1_row_value(row, "created_at")),
            **{key: live.get(key) for key in _STAGE1_LIVE_FIELDS},
            "threshold_values": {
                "min_change_rate_pct": thresholds.get("min_change_rate_pct"),
                "max_change_rate_pct": thresholds.get("max_change_rate_pct"),
                "min_trading_value_krw": thresholds.get("min_trading_value_krw"),
            },
            "threshold_passes": {
                **threshold_passes,
                "entry_window": str(_stage1_row_value(row, "decision") or "").lower() != "reject",
                "opening_lane": "opening" in str(_stage1_row_value(row, "event_phase") or "").lower()
                or "opening" in str(_stage1_row_value(row, "reason_text") or "").lower(),
                "nxt_preopen": "nxt" in str(_stage1_row_value(row, "reason_text") or "").lower()
                or "preopen" in str(_stage1_row_value(row, "reason_text") or "").lower(),
            },
            "candidate_status": candidate_status,
            "candidate_rejection_reasons": reasons,
            "detailed_reason": str(_stage1_row_value(row, "reason_text") or ""),
        })

    return {
        "stage_id": 1,
        "stage_key": "target_selection",
        "label": "종목선정 후보",
        "count": count,
        "total_evaluations": total_evaluations,
        "unique_stocks": unique_stocks,
        "status": "available" if total_evaluations > 0 else "empty",
        "updated_at": _ts(last_at),
        "source": source,
        "fallback_reason": fallback_reason,
        "is_paper_filter_applied": is_paper is not None,
        "stage_columns": _STAGE1_COLUMNS.get(strategy_type, _STAGE1_COLUMNS["default"]),
        "rows": rendered_rows,
        "summary": {
            "by_phase": [
                {
                    "event_phase": _stage1_row_value(row, "event_phase"),
                    "stage": _stage1_row_value(row, "stage"),
                    "count": _stage1_row_value(row, "cnt"),
                }
                for row in summary_rows
            ],
        },
    }


# ── Realtime data reliability summary ─────────────────────────────────────────

# gap-guard check_type → canonical source table shown in the UI banner
_DATA_QUALITY_SOURCES: list[tuple[str, str, str]] = [
    # (source_table, gap_guard_check_type, korean_label)
    ("stock_price_snapshot", "price_snapshot_freshness", "스냅샷"),
    ("v4_ohlcv_minute", "minute_ohlcv_freshness", "분봉"),
    ("go100_tick_data", "tick_freshness", "틱"),
    ("v4_orderbook_realtime", "orderbook_freshness", "호가"),
]

# source_table → go100_source_health.source name (for optional latency lookup)
_SOURCE_HEALTH_NAMES: dict[str, str] = {
    "stock_price_snapshot": "stock_price_snapshot",
    "go100_tick_data": "go100_tick_data",
    "v4_orderbook_realtime": "realtime_orderbook",
}


def _detect_trading_session(now: datetime) -> str:
    """NXT_PRE / KRX_REGULAR / NXT_AFTER / CLOSED (weekdays only, KST).

    Mirrors backend/scripts/go100_realtime_data_gap_guard.py session windows.
    """
    if is_weekend(now.date()):
        return "CLOSED"
    hhmm = now.hour * 100 + now.minute
    if 800 <= hhmm <= 850:
        return "NXT_PRE"
    if 900 <= hhmm <= 1535:
        return "KRX_REGULAR"
    if 1540 <= hhmm <= 2000:
        return "NXT_AFTER"
    return "CLOSED"


def _map_source_status(is_pass: Any, severity: Any) -> str:
    """go100_data_integrity_log (is_pass, severity) → PASS/WARN/CRITICAL."""
    if is_pass:
        return "PASS"
    sev = str(severity or "").lower()
    if sev in ("critical", "error", "fatal"):
        return "CRITICAL"
    if sev in ("warning", "warn"):
        return "WARN"
    return "WARN"


async def _build_data_quality_summary(db: AsyncSession) -> dict[str, Any]:
    """Summarise the latest realtime-source health from go100_data_integrity_log.

    SELECT-only and non-blocking: any failure returns overall_status UNKNOWN with an
    error message instead of raising, so the workbench response is never broken by it.
    This is display/metadata only — it does NOT gate order placement or entry.
    """
    now_kst = datetime.now(KST)
    session = _detect_trading_session(now_kst)
    try:
        check_types = [ct for _, ct, _ in _DATA_QUALITY_SOURCES]
        rows_r = await db.execute(
            text("""
                SELECT DISTINCT ON (check_type)
                       check_type, target_table, is_pass, severity,
                       actual_value, expected_value, message, check_time
                FROM go100_data_integrity_log
                WHERE check_type = ANY(:check_types)
                ORDER BY check_type, check_time DESC
            """),
            {"check_types": check_types},
        )
        by_check: dict[str, Any] = {r.check_type: r for r in rows_r.fetchall()}

        # Optional latency_ms enrichment from go100_source_health
        health_latency: dict[str, Any] = {}
        try:
            health_r = await db.execute(
                text("""
                    SELECT source, latency_ms, status, checked_at
                    FROM go100_source_health
                    WHERE source = ANY(:sources)
                """),
                {"sources": list(_SOURCE_HEALTH_NAMES.values())},
            )
            for hr in health_r.fetchall():
                health_latency[hr.source] = {
                    "latency_ms": _safe_float(hr.latency_ms),
                    "health_status": hr.status,
                    "checked_at": hr.checked_at.isoformat() if hr.checked_at else None,
                }
        except Exception:
            await db.rollback()

        sources: list[dict[str, Any]] = []
        latest_check: Optional[datetime] = None
        heal_recent = False
        heal_result: Optional[str] = None
        statuses: list[str] = []

        for source_table, check_type, label in _DATA_QUALITY_SOURCES:
            row = by_check.get(check_type)
            if row is None:
                sources.append({
                    "source": source_table,
                    "label": label,
                    "status": "UNKNOWN",
                    "pass": None,
                    "actual": None,
                    "severity": None,
                    "message": "점검 이력 없음",
                    "checked_at_kst": None,
                    "latency_ms": None,
                })
                statuses.append("UNKNOWN")
                continue

            status = _map_source_status(row.is_pass, row.severity)
            statuses.append(status)
            ct = row.check_time
            if ct is not None and (latest_check is None or ct > latest_check):
                latest_check = ct
            msg = str(row.message or "")
            if "heal=" in msg:
                heal_recent = True
                heal_result = msg.split("heal=", 1)[1][:160]

            hn = _SOURCE_HEALTH_NAMES.get(source_table)
            latency = health_latency.get(hn, {}).get("latency_ms") if hn else None

            sources.append({
                "source": source_table,
                "label": label,
                "status": status,
                "pass": bool(row.is_pass),
                "actual": row.actual_value,
                "expected": row.expected_value,
                "severity": row.severity,
                "message": msg,
                "checked_at_kst": ct.astimezone(KST).isoformat() if ct else None,
                "latency_ms": latency,
            })

        # overall_status: CRITICAL > WARN > PASS; UNKNOWN only if nothing known
        known = [s for s in statuses if s != "UNKNOWN"]
        if not known:
            overall = "UNKNOWN"
        elif "CRITICAL" in statuses:
            overall = "CRITICAL"
        elif "WARN" in statuses:
            overall = "WARN"
        else:
            overall = "PASS"

        return {
            "overall_status": overall,
            "checked_at_kst": (latest_check.astimezone(KST).isoformat() if latest_check
                               else now_kst.isoformat()),
            "session": session,
            "sources": sources,
            "heal_recent": heal_recent,
            "heal_result": heal_result,
            "source": "go100_data_integrity_log+go100_source_health",
            "note": "표시/메타데이터 전용 — 신규진입 차단에는 사용하지 않음",
        }
    except Exception as exc:
        try:
            await db.rollback()
        except Exception:
            pass
        return {
            "overall_status": "UNKNOWN",
            "checked_at_kst": now_kst.isoformat(),
            "session": session,
            "sources": [],
            "heal_recent": False,
            "heal_result": None,
            "error": str(exc)[:240],
            "note": "표시/메타데이터 전용 — 신규진입 차단에는 사용하지 않음",
        }


async def _stage1_rise_context(
    db: AsyncSession,
    stock_codes: list[str],
) -> dict[str, dict[str, Any]]:
    """Return candidate-scoped theme/news evidence for the CEO operations view."""
    clean_codes = sorted({str(code).strip() for code in stock_codes if str(code).strip()})[:100]
    contexts: dict[str, dict[str, Any]] = {
        code: {
            "status": "missing",
            "themes": [],
            "latest_news": None,
            "backfill_status": "request_available",
            "request_path": "/api/go100/news-analysis/analyze-batch",
        }
        for code in clean_codes
    }
    if not clean_codes:
        return contexts
    try:
        theme_rows = (await db.execute(
            text("""
                SELECT ts.stock_code,
                       ARRAY_AGG(DISTINCT tm.theme_name ORDER BY tm.theme_name) AS theme_names
                FROM v4_theme_stock ts
                JOIN v4_theme_master tm ON tm.theme_code = ts.theme_code
                WHERE ts.stock_code = ANY(:stock_codes)
                  AND COALESCE(tm.is_active, true) = true
                GROUP BY ts.stock_code
            """),
            {"stock_codes": clean_codes},
        )).fetchall()
        for row in theme_rows:
            code = str(row.stock_code)
            contexts[code]["themes"] = list(row.theme_names or [])
            contexts[code]["status"] = "available"
    except Exception as exc:
        await db.rollback()
        logger.warning("stage1 theme context unavailable: %s", exc)

    try:
        news_rows = (await db.execute(
            text("""
                SELECT DISTINCT ON (candidate.stock_code)
                       candidate.stock_code, news.id, news.title, news.material_type,
                       news.material_strength, news.material_confidence,
                       news.provider_name, news.data_date, news.data_time,
                       news.source_url, news.created_at
                FROM UNNEST(CAST(:stock_codes AS text[])) AS candidate(stock_code)
                JOIN go100_news_items news
                  ON candidate.stock_code IN (news.stock_code1, news.stock_code2, news.stock_code3)
                 AND news.created_at >= NOW() - INTERVAL '72 hours'
                ORDER BY candidate.stock_code,
                         ABS(COALESCE(news.material_strength, 0)) DESC,
                         news.created_at DESC
            """),
            {"stock_codes": clean_codes},
        )).fetchall()
        for row in news_rows:
            code = str(row.stock_code)
            contexts[code]["latest_news"] = {
                "id": row.id,
                "title": row.title,
                "material_type": row.material_type,
                "material_strength": _safe_float(row.material_strength),
                "material_confidence": row.material_confidence,
                "provider": row.provider_name,
                "data_time": f"{row.data_date or ''} {row.data_time or ''}".strip() or _ts(row.created_at),
                "source_url": row.source_url,
            }
            contexts[code]["status"] = "available"
            contexts[code]["backfill_status"] = "not_required"
    except Exception as exc:
        await db.rollback()
        logger.warning("stage1 news context unavailable: %s", exc)
    return contexts


@router.get("/{card_id}/trade-value-windows")
async def get_card_trade_value_windows(
    card_id: int,
    stock_codes: Optional[str] = Query(None, description="Comma-separated stock codes. Defaults to the current #303 Stage 1 pool."),
    current_user: dict = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Return 1-minute OHLCV trade-value windows and ranks for the operations page."""
    user_id = await _effective_uid(current_user, db)
    owner_result = await db.execute(
        text("""
            SELECT 1 FROM go100_strategy_cards
            WHERE go100_card_id = :card_id AND user_id = :user_id
              AND card_status != 'RETIRED'
        """),
        {"card_id": card_id, "user_id": user_id},
    )
    if not owner_result.fetchone():
        raise HTTPException(status_code=404, detail="전략카드를 찾을 수 없습니다.")
    codes: list[str]
    if stock_codes:
        codes = [code.strip() for code in stock_codes.split(",") if code.strip()]
    elif card_id == 303:
        intraday = await _stage1_intraday_top50_candidates(db)
        codes = list(intraday.keys())
    else:
        codes = []
    if not codes:
        return {
            "card_id": card_id,
            "source": "v4_ohlcv_minute",
            "summary": {"status": "empty", "windows": []},
            "items": [],
        }
    scoped_codes = codes[:100]
    cache_key = (user_id, card_id, tuple(scoped_codes))
    cached_response = _trade_value_windows_cache_get(cache_key)
    if cached_response is not None:
        return cached_response

    window_data, summary = await _stage1_trade_value_windows(db, scoped_codes)
    missing_codes = [
        code for code in scoped_codes
        if not any(
            window.get("sample_count", 0) > 0
            for window in window_data.get(code, {}).values()
        )
    ]
    backfill_status = (
        await _enqueue_stage1_minute_backfill(db, missing_codes)
        if missing_codes
        else {"status": "not_required", "missing_type": "minute_ohlcv_365d", "enqueued_count": 0}
    )
    summary["missing_stock_codes"] = missing_codes
    summary["backfill_status"] = backfill_status
    summary["cache_hit"] = False
    summary["cache_ttl_sec"] = _TRADE_VALUE_WINDOWS_CACHE_TTL_SEC
    response = {
        "card_id": card_id,
        "source": "v4_ohlcv_minute",
        "summary": summary,
        "items": [
            {
                "stock_code": code,
                "windows": window_data.get(code, {}),
            }
            for code in scoped_codes
        ],
    }
    _trade_value_windows_cache_set(cache_key, response)
    return response


@router.get("/{card_id}/workbench")
async def get_card_workbench(
    card_id: int,
    mode: str = Query("realtime", pattern="^(realtime|cumulative|date_range|lifecycle)$"),
    is_paper: Optional[bool] = Query(None),
    date_from: Optional[str] = Query(None),
    date_to: Optional[str] = Query(None),
    card_version: Optional[int] = Query(None, ge=1),
    market_regime: Optional[str] = Query(None, min_length=1, max_length=30, pattern="^[A-Za-z0-9_-]+$"),
    current_user: dict = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """전략카드 6단계 매매운영 워크벤치 데이터.

    mode: realtime (오늘 KST) | cumulative (전체) | date_range (날짜범위) | lifecycle (포지션별 연결)
    is_paper: true=모의, false=실매매, None=전체
    date_from, date_to: YYYY-MM-DD (date_range 모드에서만 사용)
    """
    parsed_from = None
    parsed_to = None
    try:
        parsed_from = _date.fromisoformat(date_from) if date_from else None
        parsed_to = _date.fromisoformat(date_to) if date_to else None
    except ValueError as exc:
        raise HTTPException(status_code=422, detail="날짜는 YYYY-MM-DD 형식이어야 합니다.") from exc
    if parsed_from and parsed_to and parsed_from > parsed_to:
        raise HTTPException(status_code=422, detail="시작일은 종료일보다 늦을 수 없습니다.")

    request_started = time.perf_counter()
    user_id = await _effective_uid(current_user, db)
    normalized_regime = market_regime.upper() if market_regime else None

    try:
        await db.execute(
            text("SELECT set_config('statement_timeout', :timeout_ms, true)"),
            {"timeout_ms": f"{_WORKBENCH_DB_TIMEOUT_MS}ms"},
        )
    except Exception as exc:
        await db.rollback()
        logger.warning("workbench statement_timeout setup failed card=%s: %s", card_id, exc)

    # ── 카드 소유권 확인 ──────────────────────────────────────────────────────
    card_result = await db.execute(
        text("""
            SELECT go100_card_id, strategy_name, strategy_type, card_status, is_active, is_live,
                   allocated_amount, max_stocks,
                   COALESCE(
                       (SELECT MAX(cv.card_version)
                        FROM go100_strategy_card_versions cv
                        WHERE cv.go100_card_id = go100_strategy_cards.go100_card_id),
                       1
                   ) AS current_card_version,
                   entry_rules::text AS entry_rules_raw,
                   exit_rules::text  AS exit_rules_raw,
                   risk_params::text AS risk_params_raw,
                   strategy_params::text AS strategy_params_raw,
                   trigger_tactic::text AS trigger_tactic_raw,
                   updated_at
            FROM go100_strategy_cards
            WHERE go100_card_id = :card_id
              AND user_id = :user_id
              AND card_status != 'RETIRED'
        """),
        {"card_id": card_id, "user_id": user_id},
    )
    card_row = card_result.fetchone()
    if not card_row:
        raise HTTPException(status_code=404, detail="전략카드를 찾을 수 없습니다.")

    current_card_version = int(card_row.current_card_version or 1)
    active_card_version = card_version or current_card_version
    cache_key = (
        current_user.get("user_id"), user_id, card_id, mode, is_paper,
        parsed_from.isoformat() if parsed_from else None,
        parsed_to.isoformat() if parsed_to else None,
        active_card_version, normalized_regime,
    )
    cached_response = _workbench_cache_get(cache_key)
    if cached_response is not None:
        return cached_response

    historical_snapshot: dict[str, Any] | None = None
    if active_card_version != current_card_version:
        snapshot_result = await db.execute(
            text("""
                SELECT snapshot_json
                FROM go100_strategy_card_versions
                WHERE go100_card_id = :card_id AND card_version = :card_version
            """),
            {"card_id": card_id, "card_version": active_card_version},
        )
        snapshot_row = snapshot_result.fetchone()
        if not snapshot_row:
            raise HTTPException(status_code=422, detail="대사 가능한 카드 버전이 아닙니다.")
        historical_snapshot = snapshot_row.snapshot_json or {}
    if historical_snapshot:
        thresholds = _extract_thresholds(
            json.dumps(historical_snapshot.get("entry_rules") or []),
            json.dumps(historical_snapshot.get("exit_rules") or []),
            json.dumps(historical_snapshot.get("risk_params") or {}),
        )
    else:
        thresholds = _extract_thresholds(
            card_row.entry_rules_raw,
            card_row.exit_rules_raw,
            card_row.risk_params_raw,
        )

    versions_result = await db.execute(
        text("""
            SELECT card_version FROM go100_strategy_card_versions
            WHERE go100_card_id = :card_id ORDER BY card_version DESC
        """),
        {"card_id": card_id},
    )
    available_card_versions = sorted(
        {current_card_version, *(int(row.card_version) for row in versions_result.fetchall())},
        reverse=True,
    )

    strategy_type = _detect_strategy_type(card_row)
    effective_trigger_tactic = (
        historical_snapshot.get("trigger_tactic")
        if historical_snapshot and "trigger_tactic" in historical_snapshot
        else getattr(card_row, "trigger_tactic_raw", None)
    )
    card_info = {
        "id": card_row.go100_card_id,
        "name": historical_snapshot.get("strategy_name", card_row.strategy_name) if historical_snapshot else card_row.strategy_name,
        "status": historical_snapshot.get("card_status", card_row.card_status) if historical_snapshot else card_row.card_status,
        "is_active": historical_snapshot.get("is_active", card_row.is_active) if historical_snapshot else card_row.is_active,
        "is_live": historical_snapshot.get("is_live", card_row.is_live) if historical_snapshot else card_row.is_live,
        "allocated_amount": (
            float(historical_snapshot.get("allocated_amount"))
            if historical_snapshot and historical_snapshot.get("allocated_amount") is not None
            else float(card_row.allocated_amount) if card_row.allocated_amount is not None else None
        ),
        "max_stocks": historical_snapshot.get("max_stocks", card_row.max_stocks) if historical_snapshot else card_row.max_stocks,
        "version": active_card_version,
        "thresholds": thresholds,
        "strategy_definition": _build_card119_strategy_definition(
            card_id=card_id,
            strategy_type=strategy_type,
            thresholds=thresholds,
        ) or _build_card303_strategy_definition(
            card_id=card_id,
            strategy_type=strategy_type,
            thresholds=thresholds,
            trigger_tactic=effective_trigger_tactic,
        ),
        "updated_at": card_row.updated_at.isoformat() if card_row.updated_at else None,
    }

    # ── 날짜 필터 준비 ────────────────────────────────────────────────────────
    limited_range = False
    if mode == "cumulative" and parsed_from is None and parsed_to is None:
        parsed_from = datetime.now(KST).date() - timedelta(days=42)
        parsed_to = datetime.now(KST).date()
        limited_range = True

    date_clause, date_params = _date_filter_clause(mode, parsed_from, parsed_to, col="created_at")

    diagnostics: list[dict] = []
    stages: list[dict] = []

    # ── Stage 1: 종목선정 후보 (실행 유니버스 우선, 이벤트 원장 fallback) ──────
    stage1_fallback_reason: Optional[str] = None
    stage1_source = (
        "card119_independent_discovery"
        if int(card_id) == 119
        else "v4_scalping_universe" if strategy_type in ("limitup_chase", "scalping_pullback")
        else "go100_strategy_run_events"
    )
    try:
        effective_strategy_params = (
            historical_snapshot.get("strategy_params")
            if historical_snapshot and "strategy_params" in historical_snapshot
            else card_row.strategy_params_raw
        )
        effective_entry_rules = (
            historical_snapshot.get("entry_rules")
            if historical_snapshot and "entry_rules" in historical_snapshot
            else card_row.entry_rules_raw
        )
        stage1_thresholds = _stage1_thresholds(
            strategy_type,
            effective_strategy_params,
            effective_entry_rules,
            event_fallback=strategy_type not in ("limitup_chase", "scalping_pullback"),
        )
        if int(card_id) == 303 and strategy_type == "scalping_pullback":
            # Discovery thresholds are separate from entry/risk values.  The
            # live engine and operations Stage 1 expose this single contract.
            stage1_thresholds["min_change_rate_pct"] = CARD303_DISCOVERY_MIN_CHANGE_PCT
            stage1_thresholds["max_change_rate_pct"] = CARD303_DISCOVERY_MAX_CHANGE_PCT
            stage1_thresholds["min_trading_value_krw"] = CARD303_DISCOVERY_MIN_TRADING_VALUE_KRW
        if strategy_type in ("limitup_chase", "scalping_pullback"):
            try:
                if int(card_id) == 119 and strategy_type == "limitup_chase":
                    stage1_thresholds["min_change_rate_pct"] = 20.0
                    stage1 = await _build_stage1_card119_independent_stage(
                        db,
                        thresholds=stage1_thresholds,
                        card_version=active_card_version,
                    )
                elif strategy_type == "limitup_chase":
                    stage1 = await _build_stage1_limitup_stage(
                        db,
                        strategy_type=strategy_type,
                        thresholds=stage1_thresholds,
                        card_version=active_card_version,
                    )
                elif int(card_id) == 303 and strategy_type == "scalping_pullback":
                    stage1 = await _build_stage1_card303_top50_stage(
                        db,
                        thresholds=stage1_thresholds,
                        card_version=active_card_version,
                    )
                else:
                    stage1 = await _build_stage1_universe_stage(
                        db,
                        strategy_type=strategy_type,
                        thresholds=stage1_thresholds,
                        card_version=active_card_version,
                    )
            except Exception as universe_exc:
                _src = (
                    "card119_independent_discovery"
                    if int(card_id) == 119
                    else "go100_limitup_events" if strategy_type == "limitup_chase"
                    else "v4_scalping_universe"
                )
                stage1_fallback_reason = f"{_src} 조회 실패: {str(universe_exc)[:180]}"
                stage1_source = "go100_strategy_run_events:fallback"
                logger.warning("workbench stage1 universe fallback card=%s: %s", card_id, universe_exc)
                try:
                    await db.rollback()
                except Exception:
                    pass
                stage1 = await _build_stage1_event_stage(
                    db,
                    card_id=card_id,
                    user_id=user_id,
                    active_card_version=active_card_version,
                    is_paper=is_paper,
                    date_clause=date_clause,
                    date_params=date_params,
                    normalized_regime=normalized_regime,
                    strategy_type=strategy_type,
                    thresholds=_stage1_thresholds(
                        strategy_type, effective_strategy_params, effective_entry_rules,
                        event_fallback=True,
                    ),
                    fallback_reason=stage1_fallback_reason,
                    source="go100_strategy_run_events:fallback",
                )
        else:
            stage1_fallback_reason = (
                f"strategy_type={strategy_type}에 선언된 후보 유니버스가 없어 "
                "go100_strategy_run_events를 사용"
            )
            stage1 = await _build_stage1_event_stage(
                db,
                card_id=card_id,
                user_id=user_id,
                active_card_version=active_card_version,
                is_paper=is_paper,
                date_clause=date_clause,
                date_params=date_params,
                normalized_regime=normalized_regime,
                strategy_type=strategy_type,
                thresholds=stage1_thresholds,
                fallback_reason=stage1_fallback_reason,
            )
        stages.append(stage1)
    except Exception as e:
        logger.warning("workbench stage1 error card=%s: %s", card_id, e)
        await db.rollback()
        diagnostics.append({"stage": 1, "key": "target_selection", "error": str(e)})
        stages.append({
            "stage_id": 1, "stage_key": "target_selection", "label": "종목선정 후보",
            "count": 0, "total_evaluations": 0, "unique_stocks": 0,
            "status": "unavailable", "updated_at": None,
            "source": stage1_source, "fallback_reason": stage1_fallback_reason,
            "is_paper_filter_applied": is_paper is not None,
            "rows": [], "summary": {},
        })

    # ── Stage 2: 매수감시 후보 (entry_filter/pass 이벤트) ────────────────
    # Stage 1 fallback probes can leave the asyncpg transaction aborted even when
    # the response degrades gracefully. Isolate Stage 2 so one diagnostic failure
    # does not cascade into InFailedSQLTransactionError.
    try:
        await db.rollback()
    except Exception:
        pass
    try:
        s2_params: dict[str, Any] = {
            "card_id": card_id, "user_id": user_id,
            "card_version": active_card_version,
        }
        # Accept both pass and rejected/skip evaluations so the workbench reflects
        # today's actual watch flow even when no candidate reaches order preflight.
        s2_where = (
            "go100_card_id = :card_id AND user_id = :user_id "
            "AND (card_version = :card_version OR card_version IS NULL) "
            "AND (stage IN ('entry_filter', 'entry') "
            "  OR event_phase IN ('entry_filter', 'entry', 'entry_filter/pass')) "
            "AND decision IN ('pass', 'skip', 'reject')"
        )
        if is_paper is not None:
            s2_where += " AND (is_paper = :is_paper OR is_paper IS NULL)"
            s2_params["is_paper"] = is_paper
        if date_clause:
            s2_where += f" AND {date_clause}"
            s2_params.update(date_params)
        s2_regime_clause, s2_regime_params = _regime_filter_clause("created_at", normalized_regime)
        if s2_regime_clause:
            s2_where += f" AND {s2_regime_clause}"
            s2_params.update(s2_regime_params)

        s2_stats_r = await db.execute(
            text(f"""
                SELECT COUNT(*) AS s2_total,
                       COUNT(DISTINCT stock_code) AS unique_stocks,
                       MAX(created_at) AS last_at
                FROM go100_strategy_run_events
                WHERE {s2_where}
            """),
            s2_params,
        )
        s2_stats = s2_stats_r.fetchone()
        s2_count = int(s2_stats.s2_total or 0) if s2_stats else 0
        s2_unique_stocks = int(s2_stats.unique_stocks or 0) if s2_stats else 0
        s2_last_at = s2_stats.last_at if s2_stats else None

        s2_rows_r = await db.execute(
            text(f"""
                SELECT e.stock_code,
                       COALESCE(su.stock_name, su_norm.stock_name, e.stock_code) AS stock_name,
                       e.stage, e.event_phase, e.decision, e.reason_code, e.reason_text,
                       e.metrics_json,
                       e.source_ts, e.received_at, e.trade_group_id, e.card_version,
                       COALESCE(e.source_table, e.source) AS source_table, e.created_at
                FROM (
                    SELECT *
                    FROM go100_strategy_run_events
                    WHERE {s2_where}
                    ORDER BY created_at DESC
                    LIMIT 50
                ) e
                LEFT JOIN stock_universe su ON su.stock_code = e.stock_code
                LEFT JOIN stock_universe su_norm
                  ON regexp_replace(su_norm.stock_code, '[^0-9]', '', 'g') = right(regexp_replace(e.stock_code, '[^0-9]', '', 'g'), 6)
                ORDER BY e.created_at DESC
            """),
            s2_params,
        )
        s2_rows = s2_rows_r.fetchall()

        s2_stock_codes = [str(r.stock_code) for r in s2_rows if r.stock_code]
        s2_live_data: dict[str, dict[str, Any]] = {}
        if s2_stock_codes:
            try:
                s2_live_data = await _enrich_stocks_with_live_data(db, s2_stock_codes)
            except Exception:
                pass

        stages.append({
            "stage_id": 2,
            "stage_key": "buy_watch_candidates",
            "label": "매수감시 후보",
            "count": s2_count,
            "total_evaluations": s2_count,
            "unique_stocks": s2_unique_stocks,
            "status": "available" if s2_count > 0 else "empty",
            "updated_at": s2_last_at.isoformat() if s2_last_at else None,
            "source": "go100_strategy_run_events",
            "is_paper_filter_applied": is_paper is not None,
            "stage_columns": _STAGE2_COLUMNS.get(strategy_type, _STAGE2_COLUMNS['default']),
            "rows": _stage2_score_rows(s2_rows, live_data=s2_live_data, strategy_type=strategy_type),
            "summary": {
                "priority_model": "event hard gate + live quote + 1m pullback diagnostics + final order preflight",
                "sort_order": "pass_fail_status desc, total_score desc",
                "fallback": "stock_universe exact -> normalized code -> stock_code",
                "final_order_gate_policy": "주문 가능/쿨다운/중복/보유한도/일일리스크/계좌현금은 주문 직전 평가",
            },
        })
    except Exception as e:
        logger.warning("workbench stage2 error card=%s: %s", card_id, e)
        await db.rollback()
        diagnostics.append({"stage": 2, "key": "buy_watch_candidates", "error": str(e)})
        stages.append({
            "stage_id": 2, "stage_key": "buy_watch_candidates", "label": "매수감시 후보",
            "count": 0, "total_evaluations": 0, "unique_stocks": 0,
            "status": "unavailable", "updated_at": None,
            "source": "go100_strategy_run_events", "is_paper_filter_applied": is_paper is not None,
            "rows": [], "summary": {},
        })

    # ── Stage 3: 매수신호/주문/체결 (LIVE + PAPER BUY orders) ───────────────
    try:
        s3_params: dict[str, Any] = {
            "card_id": card_id, "user_id": user_id,
            "card_version": active_card_version,
        }
        s3_relation = """
            (
                SELECT order_id::text AS order_id, stock_code, stock_name,
                       status, order_price, filled_price, filled_quantity,
                       created_at, filled_at, false AS is_paper, card_version
                FROM go100_live_orders
                WHERE card_id = :card_id AND user_id = :user_id AND side = 'BUY'
                UNION ALL
                SELECT ('P-' || order_id::text) AS order_id, stock_code, stock_name,
                       status, target_price::numeric AS order_price,
                       filled_price::numeric AS filled_price,
                       quantity AS filled_quantity, created_at, filled_at,
                       true AS is_paper, card_version
                FROM go100_paper_orders
                WHERE card_id = :card_id AND UPPER(order_type) = 'BUY'
                UNION ALL
                SELECT ('T-' || t.id::text) AS order_id, t.stock_code, t.stock_name,
                       'FILLED' AS status, t.price AS order_price,
                       t.price AS filled_price, t.quantity AS filled_quantity,
                       t.traded_at AS created_at, t.traded_at AS filled_at,
                       t.is_paper, t.card_version
                FROM go100_trades_effective t
                WHERE t.go100_card_id = :card_id AND t.user_id = :user_id AND t.side = 'BUY'
                  AND NOT EXISTS (
                      SELECT 1 FROM go100_live_orders lo
                      WHERE lo.card_id = :card_id
                        AND lo.user_id = :user_id
                        AND lo.stock_code = t.stock_code
                        AND lo.side = 'BUY'
                        AND (lo.created_at AT TIME ZONE 'Asia/Seoul')::date = t.trade_date
                  )
            ) AS buy_orders
        """
        s3_where = "card_version = :card_version"
        if is_paper is True:
            s3_where += " AND is_paper = true"
        elif is_paper is False:
            s3_where += " AND is_paper = false"
        if date_clause:
            s3_where += f" AND {date_clause}"
            s3_params.update(date_params)
        s3_regime_clause, s3_regime_params = _regime_filter_clause("created_at", normalized_regime)
        if s3_regime_clause:
            s3_where += f" AND {s3_regime_clause}"
            s3_params.update(s3_regime_params)

        s3_stats_r = await db.execute(
            text(f"""
                SELECT COUNT(*) AS s3_total,
                       COUNT(*) FILTER (WHERE status = 'FILLED') AS s3_filled,
                       MAX(created_at) AS s3_last_at
                FROM {s3_relation}
                WHERE {s3_where}
            """),
            s3_params,
        )
        s3_stats = s3_stats_r.fetchone()
        s3_count = int(s3_stats.s3_total or 0) if s3_stats else 0
        s3_filled = int(s3_stats.s3_filled or 0) if s3_stats else 0
        s3_last_at = s3_stats.s3_last_at if s3_stats else None

        s3_rows_r = await db.execute(
            text(f"""
                SELECT order_id, stock_code, stock_name, status,
                       order_price, filled_price, filled_quantity,
                       created_at, filled_at, is_paper
                FROM {s3_relation}
                WHERE {s3_where}
                ORDER BY created_at DESC
                LIMIT 10
            """),
            s3_params,
        )
        s3_rows = s3_rows_r.fetchall()

        stages.append({
            "stage_id": 3,
            "stage_key": "buy_signal_order_fill",
            "label": "매수신호/주문/체결",
            "count": s3_count,
            "status": "available" if s3_count > 0 else "empty",
            "updated_at": s3_last_at.isoformat() if s3_last_at else None,
            "source": "go100_live_orders+go100_paper_orders+go100_trades_effective(BUY fallback)",
            "is_paper_filter_applied": True,
            "rows": [
                {
                    "order_id": r.order_id,
                    "stock_code": r.stock_code,
                    "stock_name": r.stock_name,
                    "status": r.status,
                    "order_price": float(r.order_price) if r.order_price is not None else None,
                    "filled_price": float(r.filled_price) if r.filled_price is not None else None,
                    "filled_quantity": r.filled_quantity,
                    "created_at": r.created_at.isoformat() if r.created_at else None,
                    "filled_at": r.filled_at.isoformat() if r.filled_at else None,
                    "is_paper": bool(r.is_paper),
                }
                for r in s3_rows
            ],
            "summary": {
                "total_orders": s3_count,
                "filled_orders": s3_filled,
                "by_status": [],
            },
        })
    except Exception as e:
        logger.warning("workbench stage3 error card=%s: %s", card_id, e)
        await db.rollback()
        diagnostics.append({"stage": 3, "key": "buy_signal_order_fill", "error": str(e)})
        stages.append({
            "stage_id": 3, "stage_key": "buy_signal_order_fill", "label": "매수신호/주문/체결",
            "count": 0, "status": "unavailable", "updated_at": None,
            "source": "go100_live_orders+go100_paper_orders+go100_trades_effective(BUY fallback)", "is_paper_filter_applied": True,
            "rows": [], "summary": {},
        })

    # ── Stage 4: 보유 포지션 관리 (LIVE + PAPER OPEN) ──────────────────────
    try:
        s4_params: dict[str, Any] = {
            "card_id": card_id,
            "user_id": user_id,
            "card_version": active_card_version,
        }
        live_scope = " AND false" if is_paper is True else ""
        paper_scope = " AND false" if is_paper is False else ""
        live_status_scope = "AND p.status = 'OPEN'" if mode == "realtime" else ""
        paper_status_scope = "AND pp.status = 'OPEN'" if mode == "realtime" else ""
        if mode == "date_range":
            range_from = parsed_from or (datetime.now(KST).date() - timedelta(days=7))
            range_to = parsed_to or datetime.now(KST).date()
            s4_params.update({"date_from": range_from, "date_to": range_to})
            live_scope += " AND (p.created_at AT TIME ZONE 'Asia/Seoul')::date BETWEEN :date_from AND :date_to"
            paper_scope += " AND (pp.entry_date AT TIME ZONE 'Asia/Seoul')::date BETWEEN :date_from AND :date_to"
        if normalized_regime:
            s4_params["market_regime"] = normalized_regime
            live_scope += (
                " AND EXISTS (SELECT 1 FROM v4_market_regime_daily mr "
                "WHERE mr.date = (p.created_at AT TIME ZONE 'Asia/Seoul')::date "
                "AND UPPER(mr.regime) = :market_regime)"
            )
            paper_scope += (
                " AND EXISTS (SELECT 1 FROM v4_market_regime_daily mr "
                "WHERE mr.date = (pp.entry_date AT TIME ZONE 'Asia/Seoul')::date "
                "AND UPPER(mr.regime) = :market_regime)"
            )
        s4_union = f"""
            SELECT p.id, p.stock_code, COALESCE(NULLIF(p.stock_name, ''), su.stock_name, p.stock_code) AS stock_name, p.entry_price,
                   p.current_price, p.pnl_pct, p.pnl_amount,
                   p.remaining_qty, p.stop_loss_price, p.take_profit_price,
                   p.trailing_pct, p.entry_date, p.updated_at,
                   false AS is_paper
            FROM go100_positions p
            LEFT JOIN stock_universe su ON su.stock_code = p.stock_code
            WHERE p.go100_card_id = :card_id
              AND p.user_id = :user_id
              AND p.card_version = :card_version
              {live_status_scope}
              {live_scope}
            UNION ALL
            SELECT pp.position_id AS id, pp.stock_code, COALESCE(NULLIF(pp.stock_name, ''), su.stock_name, pp.stock_code) AS stock_name,
                   pp.avg_price AS entry_price, pp.current_price,
                   pp.unrealized_pnl_pct AS pnl_pct,
                   pp.unrealized_pnl AS pnl_amount,
                   pp.quantity AS remaining_qty,
                   NULL AS stop_loss_price, NULL AS take_profit_price,
                   NULL AS trailing_pct, pp.entry_date, pp.updated_at,
                   true AS is_paper
            FROM go100_paper_positions pp
            LEFT JOIN stock_universe su ON su.stock_code = pp.stock_code
            WHERE pp.card_id = :card_id
              AND pp.card_version = :card_version
              {paper_status_scope}
              {paper_scope}
        """

        s4_summary_r = await db.execute(
            text(f"""
                SELECT COUNT(*) AS position_count,
                       COALESCE(SUM(pnl_amount), 0) AS total_pnl,
                       MAX(updated_at) AS last_at
                FROM ({s4_union}) combined_positions
            """),
            s4_params,
        )
        s4_summary = s4_summary_r.fetchone()
        s4_count = int(s4_summary.position_count or 0) if s4_summary else 0
        s4_pnl = s4_summary.total_pnl or 0 if s4_summary else 0
        s4_last_at = s4_summary.last_at if s4_summary else None

        s4_rows_r = await db.execute(
            text(f"""
                SELECT * FROM ({s4_union}) combined_positions
                ORDER BY updated_at DESC
                LIMIT 10
            """),
            s4_params,
        )
        s4_rows = s4_rows_r.fetchall()

        s4_stock_codes = list({str(r.stock_code) for r in s4_rows if r.stock_code})
        s4_live_data: dict[str, dict[str, Any]] = {}
        if s4_stock_codes:
            try:
                s4_live_data = await _enrich_stocks_with_live_data(db, s4_stock_codes)
            except Exception:
                pass

        def _s4_enrich_row(r):
            code = str(r.stock_code or "")
            live = s4_live_data.get(code, {})
            entry = _safe_float(r.entry_price)
            stored_cur = _safe_float(r.current_price)
            live_cur = live.get("current_price")
            cur = live_cur if live_cur else stored_cur
            pnl = (
                round(((cur - entry) / entry) * 100, 2)
                if cur and entry and entry > 0
                else _safe_float(r.pnl_pct)
            )
            name = r.stock_name
            if live.get("stock_name") and (not name or name == code):
                name = live["stock_name"]
            name_payload = _name_payload(code, name)
            return {
                "id": r.id,
                "stock_code": code,
                "stock_name": name_payload["stock_name"],
                "display_name": name_payload["display_name"],
                "stock_name_missing": name_payload["stock_name_missing"],
                "buy_price": entry,
                "entry_price": entry,
                "current_price": cur,
                "unrealized_pnl_pct": pnl,
                "pnl_pct": pnl,
                "quantity": r.remaining_qty,
                "remaining_qty": r.remaining_qty,
                "stop_loss_price": _safe_float(r.stop_loss_price),
                "take_profit_price": _safe_float(r.take_profit_price),
                "trailing_pct": _safe_float(r.trailing_pct),
                "bought_at": _ts(r.entry_date),
                "entry_date": str(r.entry_date) if r.entry_date else None,
                "is_paper": bool(r.is_paper),
                "data_source": live.get("data_source", "stored"),
            }

        s4_enriched = [_s4_enrich_row(r) for r in s4_rows]
        s4_total_pnl = sum(
            ((row["current_price"] or 0) - (row["entry_price"] or 0)) * (row["quantity"] or 0)
            for row in s4_enriched
        )

        if is_paper is True:
            s4_source = "go100_paper_positions"
        elif is_paper is False:
            s4_source = "go100_positions"
        else:
            s4_source = "go100_positions+go100_paper_positions"

        stages.append({
            "stage_id": 4,
            "stage_key": "open_position_management",
            "label": "보유 포지션 관리",
            "count": s4_count,
            "status": "available" if s4_count > 0 else "empty",
            "updated_at": s4_last_at.isoformat() if s4_last_at else None,
            "source": s4_source,
            "is_paper_filter_applied": is_paper is not None,
            "rows": s4_enriched,
            "summary": {
                "open_count": s4_count,
                "total_unrealized_pnl": s4_total_pnl,
            },
        })
    except Exception as e:
        logger.warning("workbench stage4 error card=%s: %s", card_id, e)
        await db.rollback()
        diagnostics.append({"stage": 4, "key": "open_position_management", "error": str(e)})
        stages.append({
            "stage_id": 4, "stage_key": "open_position_management", "label": "보유 포지션 관리",
            "count": 0, "status": "unavailable", "updated_at": None,
            "source": "go100_positions+go100_paper_positions", "is_paper_filter_applied": is_paper is not None,
            "rows": [], "summary": {},
        })

    # ── Stage 5: 매도/손절/익절 (LIVE + PAPER SELL orders) ─────────────────
    try:
        s5_params: dict[str, Any] = {
            "card_id": card_id, "user_id": user_id,
            "card_version": active_card_version,
            "card_versions": available_card_versions,
        }
        s5_relation = """
            (
                SELECT lo.order_id::text AS order_id, lo.stock_code,
                       COALESCE(NULLIF(lo.stock_name, ''), su.stock_name, lo.stock_code) AS stock_name,
                       lo.status, lo.exit_reason, lo.filled_price, lo.filled_quantity,
                       tr.pnl_amount, tr.pnl_pct,
                       quantity AS order_quantity,
                       lo.created_at, COALESCE(lo.filled_at, tr.traded_at) AS filled_at, false AS is_paper, lo.card_version,
                       'live_order' AS record_source
                FROM go100_live_orders lo
                LEFT JOIN stock_universe su ON su.stock_code = lo.stock_code
                LEFT JOIN LATERAL (
                    SELECT t.pnl_amount, t.pnl_pct, t.traded_at
                    FROM go100_trades_effective t
                    WHERE t.go100_card_id = :card_id
                      AND t.user_id = :user_id
                      AND t.card_version = lo.card_version
                      AND t.side = 'SELL'
                      AND t.stock_code = lo.stock_code
                      AND (
                          t.order_id = lo.order_id
                          OR (lo.filled_at IS NOT NULL AND ABS(EXTRACT(EPOCH FROM (t.traded_at - lo.filled_at))) <= 300)
                          OR (t.trade_date = (lo.created_at AT TIME ZONE 'Asia/Seoul')::date)
                      )
                    ORDER BY
                        CASE WHEN t.order_id = lo.order_id THEN 0 ELSE 1 END,
                        ABS(EXTRACT(EPOCH FROM (t.traded_at - COALESCE(lo.filled_at, lo.created_at)))) ASC
                    LIMIT 1
                ) tr ON true
                WHERE lo.card_id = :card_id AND lo.user_id = :user_id AND lo.side = 'SELL'
                UNION ALL
                SELECT ('P-' || po.order_id::text) AS order_id, po.stock_code,
                       COALESCE(NULLIF(po.stock_name, ''), su.stock_name, po.stock_code) AS stock_name,
                       po.status, po.order_reason AS exit_reason,
                       filled_price::numeric AS filled_price,
                       po.quantity AS filled_quantity,
                       ptr.pnl_amount, ptr.pnl_pct,
                       po.quantity AS order_quantity,
                       po.created_at, COALESCE(po.filled_at, ptr.traded_at) AS filled_at,
                       true AS is_paper, po.card_version,
                      'paper_order' AS record_source
                FROM go100_paper_orders po
                LEFT JOIN stock_universe su ON su.stock_code = po.stock_code
                LEFT JOIN LATERAL (
                    SELECT t.pnl_amount, t.pnl_pct, t.traded_at
                    FROM go100_trades_effective t
                    WHERE t.go100_card_id = :card_id
                      AND t.user_id = :user_id
                      AND t.card_version = po.card_version
                      AND t.side = 'SELL'
                      AND t.stock_code = po.stock_code
                      AND t.is_paper = true
                      AND t.trade_date = (po.created_at AT TIME ZONE 'Asia/Seoul')::date
                    ORDER BY ABS(EXTRACT(EPOCH FROM (t.traded_at - COALESCE(po.filled_at, po.created_at)))) ASC
                    LIMIT 1
                ) ptr ON true
                WHERE po.card_id = :card_id AND UPPER(po.order_type) = 'SELL'
                UNION ALL
                SELECT ('T-' || t.id::text) AS order_id, t.stock_code,
                       COALESCE(NULLIF(t.stock_name, ''), su.stock_name, t.stock_code) AS stock_name,
                       'FILLED' AS status,
                       COALESCE(NULLIF(lo.exit_reason, ''), NULLIF(po.order_reason, ''), '체결원장') AS exit_reason,
                       t.price AS filled_price, t.quantity AS filled_quantity,
                       t.pnl_amount, t.pnl_pct,
                       t.quantity AS order_quantity,
                       t.traded_at AS created_at, t.traded_at AS filled_at,
                       t.is_paper, t.card_version,
                       'trade_fallback' AS record_source
                FROM go100_trades_effective t
                LEFT JOIN stock_universe su ON su.stock_code = t.stock_code
                LEFT JOIN go100_live_orders lo
                  ON lo.order_id = t.order_id
                 AND lo.card_id = :card_id
                 AND lo.user_id = :user_id
                 AND lo.side = 'SELL'
                LEFT JOIN go100_paper_orders po
                  ON po.card_id = :card_id
                 AND po.stock_code = t.stock_code
                 AND UPPER(po.order_type) = 'SELL'
                 AND (po.created_at AT TIME ZONE 'Asia/Seoul')::date = t.trade_date
                WHERE t.go100_card_id = :card_id AND t.user_id = :user_id AND t.side = 'SELL'
                  AND NOT EXISTS (
                      SELECT 1 FROM go100_live_orders lo
                      WHERE lo.card_id = :card_id
                        AND lo.user_id = :user_id
                        AND lo.stock_code = t.stock_code
                        AND lo.side = 'SELL'
                        AND (
                            lo.order_id = t.order_id
                            OR (
                                t.order_id IS NULL
                                AND lo.filled_at IS NOT NULL
                                AND ABS(EXTRACT(EPOCH FROM (t.traded_at - lo.filled_at))) <= 300
                            )
                        )
                  )
            ) AS sell_orders
        """
        if mode == "realtime" and card_version is None:
            s5_where = "card_version = ANY(:card_versions)"
        else:
            s5_where = "card_version = :card_version"
        if is_paper is True:
            s5_where += " AND is_paper = true"
        elif is_paper is False:
            s5_where += " AND is_paper = false"
        if date_clause:
            s5_where += f" AND {date_clause}"
            s5_params.update(date_params)
        s5_regime_clause, s5_regime_params = _regime_filter_clause("created_at", normalized_regime)
        if s5_regime_clause:
            s5_where += f" AND {s5_regime_clause}"
            s5_params.update(s5_regime_params)

        s5_stats_r = await db.execute(
            text(f"""
                SELECT COUNT(*) AS s5_total,
                       MAX(created_at) AS s5_last_at,
                       COUNT(*) FILTER (WHERE
                           UPPER(COALESCE(status, '')) IN ('FAILED', 'REJECTED', 'CANCELLED', 'ERROR')
                           OR GREATEST(COALESCE(order_quantity, 0) - COALESCE(filled_quantity, 0), 0) > 0
                       ) AS s5_unresolved
                FROM {s5_relation}
                WHERE {s5_where}
            """),
            s5_params,
        )
        s5_stats = s5_stats_r.fetchone()
        s5_count = int(s5_stats.s5_total or 0) if s5_stats else 0
        s5_last_at = s5_stats.s5_last_at if s5_stats else None
        s5_unresolved = int(s5_stats.s5_unresolved or 0) if s5_stats else 0

        s5_rows_r = await db.execute(
            text(f"""
                SELECT order_id, stock_code, stock_name, status, exit_reason,
                       filled_price, filled_quantity, order_quantity,
                       pnl_amount, pnl_pct,
                       GREATEST(COALESCE(order_quantity, 0) - COALESCE(filled_quantity, 0), 0) AS remaining_quantity,
                       filled_at, is_paper, card_version, record_source
                FROM {s5_relation}
                WHERE {s5_where}
                ORDER BY created_at DESC
                LIMIT 10
            """),
            s5_params,
        )
        s5_rows = s5_rows_r.fetchall()

        stages.append({
            "stage_id": 5,
            "stage_key": "sell_exit_orders",
            "label": "매도/손절/익절",
            "count": s5_count,
            "status": "available" if s5_count > 0 else "empty",
            "updated_at": s5_last_at.isoformat() if s5_last_at else None,
            "source": "go100_live_orders+go100_paper_orders+go100_trades_effective(SELL fallback)",
            "is_paper_filter_applied": True,
            "rows": [
                (lambda name_payload: {
                    "order_id": r.order_id,
                    "stock_code": name_payload["stock_code"],
                    "stock_name": name_payload["stock_name"],
                    "display_name": name_payload["display_name"],
                    "stock_name_missing": name_payload["stock_name_missing"],
                    "status": r.status,
                    "exit_reason": r.exit_reason or "미기록",
                    "exit_result": _exit_result(r.pnl_amount, r.pnl_pct),
                    "filled_price": float(r.filled_price) if r.filled_price is not None else None,
                    "filled_quantity": r.filled_quantity,
                    "order_quantity": r.order_quantity,
                    "remaining_quantity": r.remaining_quantity,
                    "pnl_amount": float(r.pnl_amount) if r.pnl_amount is not None else None,
                    "pnl_pct": float(r.pnl_pct) if r.pnl_pct is not None else None,
                    "realized_pnl_pct": float(r.pnl_pct) if r.pnl_pct is not None else None,
                    "filled_at": r.filled_at.isoformat() if r.filled_at else None,
                    "is_paper": bool(r.is_paper),
                    "card_version": r.card_version,
                    "record_source": r.record_source,
                })(_name_payload(r.stock_code, r.stock_name))
                for r in s5_rows
            ],
            "summary": {
                "total_sells": s5_count,
                "unresolved_failures": s5_unresolved,
                "by_exit_reason": [],
                "merge_policy": "live/paper orders plus unmatched sell trades by order_id or close fill time",
                "included_card_versions": available_card_versions if mode == "realtime" and card_version is None else [active_card_version],
            },
        })
    except Exception as e:
        logger.warning("workbench stage5 error card=%s: %s", card_id, e)
        await db.rollback()
        diagnostics.append({"stage": 5, "key": "sell_exit_orders", "error": str(e)})
        stages.append({
            "stage_id": 5, "stage_key": "sell_exit_orders", "label": "매도/손절/익절",
            "count": 0, "status": "unavailable", "updated_at": None,
            "source": "go100_live_orders+go100_paper_orders+go100_trades_effective(SELL fallback)", "is_paper_filter_applied": True,
            "rows": [], "summary": {},
        })

    # ── Stage 6: 일일 리뷰 (go100_trades SELL, DERIVED) ────────────────────
    try:
        # go100_trades는 trade_date 컬럼 사용
        t6_date_clause, t6_date_params = _date_filter_clause(mode, parsed_from, parsed_to, col="traded_at")

        s6_params: dict[str, Any] = {
            "card_id": card_id, "user_id": user_id,
            "card_version": active_card_version,
        }
        s6_where = (
            "go100_card_id = :card_id AND user_id = :user_id "
            "AND card_version = :card_version AND side = 'SELL'"
        )
        if is_paper is not None:
            s6_where += " AND is_paper = :is_paper"
            s6_params["is_paper"] = is_paper
        if t6_date_clause:
            s6_where += f" AND {t6_date_clause}"
            s6_params.update(t6_date_params)
        s6_regime_clause, s6_regime_params = _regime_filter_clause("traded_at", normalized_regime)
        if s6_regime_clause:
            s6_where += f" AND {s6_regime_clause}"
            s6_params.update(s6_regime_params)

        s6_stats_r = await db.execute(
            text(f"""
                SELECT
                    COUNT(*) AS total_sells,
                    COUNT(CASE WHEN pnl_amount > 0 THEN 1 END) AS win_count,
                    COUNT(CASE WHEN pnl_amount <= 0 THEN 1 END) AS loss_count,
                    COALESCE(SUM(pnl_amount), 0) AS total_pnl,
                    AVG(pnl_pct) FILTER (WHERE pnl_pct IS NOT NULL) AS avg_pnl_pct,
                    MAX(traded_at) AS last_at
                FROM go100_trades_effective
                WHERE {s6_where}
            """),
            s6_params,
        )
        s6_stats = s6_stats_r.fetchone()

        s6_total = s6_stats.total_sells or 0
        s6_win = s6_stats.win_count or 0
        s6_loss = s6_stats.loss_count or 0
        s6_win_rate = round(s6_win / s6_total * 100, 1) if s6_total > 0 else None
        s6_avg_pnl_pct = (
            round(float(s6_stats.avg_pnl_pct), 2)
            if s6_stats.avg_pnl_pct is not None
            else None
        )
        s6_last_at = s6_stats.last_at

        improvement_items: list[dict[str, str]] = []
        if s6_total == 0:
            improvement_items.append({
                "priority": "INFO",
                "title": "청산 데이터 대기",
                "evidence": "선택 기간에 완료된 매도 거래가 0건입니다.",
                "action": "매도 체결 후 승률·평균손익 기반 자동 복기를 생성합니다.",
            })
        else:
            if s6_win_rate is not None and s6_win_rate < 50:
                improvement_items.append({
                    "priority": "P1",
                    "title": "진입 조건 재검토",
                    "evidence": f"승률 {s6_win_rate:.1f}% ({s6_win}승/{s6_loss}패)",
                    "action": "손실 거래의 진입 사유와 entry_filter 통과 조건을 우선 대조하십시오.",
                })
            if s6_avg_pnl_pct is not None and s6_avg_pnl_pct < 0:
                improvement_items.append({
                    "priority": "P1",
                    "title": "청산 규칙 재검토",
                    "evidence": f"평균 손익률 {s6_avg_pnl_pct:.2f}%",
                    "action": "손절·시간청산·트레일링 조건별 손익 기여도를 비교하십시오.",
                })
            if s6_loss > 0:
                improvement_items.append({
                    "priority": "P2",
                    "title": "손실 거래 건별 복기",
                    "evidence": f"손실 거래 {s6_loss}건",
                    "action": "아래 건별 이력에서 진입 근거와 손절 준수 여부를 확인하십시오.",
                })
            if not improvement_items:
                improvement_items.append({
                    "priority": "MONITOR",
                    "title": "현재 조건 유지 관찰",
                    "evidence": f"승률 {s6_win_rate:.1f}%, 평균 손익률 {s6_avg_pnl_pct:.2f}%",
                    "action": "표본을 추가 축적하며 동일 지표의 기간별 변화를 관찰하십시오.",
                })

        # Enrich with multi-day trend from persisted daily snapshots (last 14 days)
        daily_trend_rows: list[dict] = []
        try:
            snap_mode = (
                "live" if is_paper is False
                else "paper" if is_paper is True
                else "all"
            )
            snap_r = await db.execute(
                text("""
                    SELECT trade_date, sell_count, win_count, loss_count,
                           win_rate, realized_pnl, avg_pnl_pct, market_regime, confidence
                    FROM go100_strategy_card_daily_results
                    WHERE go100_card_id = :card_id AND user_id = :user_id
                      AND card_version = :card_version AND mode = :mode
                    ORDER BY trade_date DESC
                    LIMIT 14
                """),
                {
                    "card_id": card_id,
                    "user_id": user_id,
                    "card_version": active_card_version,
                    "mode": snap_mode,
                },
            )
            snap_rows = snap_r.fetchall()
            daily_trend_rows = [
                {
                    "trade_date": str(r.trade_date),
                    "sell_count": r.sell_count or 0,
                    "win_count": r.win_count or 0,
                    "loss_count": r.loss_count or 0,
                    "win_rate": float(r.win_rate) if r.win_rate is not None else None,
                    "realized_pnl": float(r.realized_pnl) if r.realized_pnl is not None else 0.0,
                    "avg_pnl_pct": float(r.avg_pnl_pct) if r.avg_pnl_pct is not None else None,
                    "market_regime": r.market_regime or "UNKNOWN",
                    "confidence": r.confidence or "LOW",
                }
                for r in snap_rows
            ]
            # Detect declining win rate trend over last 7 persisted days with sells
            days_with_sells = [r for r in daily_trend_rows if r["sell_count"] > 0 and r["win_rate"] is not None]
            if len(days_with_sells) >= 4:
                recent_rates = [r["win_rate"] for r in days_with_sells[:2]]
                older_rates = [r["win_rate"] for r in days_with_sells[2:]]
                recent_avg = sum(recent_rates) / len(recent_rates)
                older_avg = sum(older_rates) / len(older_rates)
                if older_avg > 0 and recent_avg < older_avg * 0.80:
                    improvement_items.append({
                        "priority": "P2",
                        "title": "최근 승률 하락 추세 (일별 스냅샷 기반)",
                        "evidence": (
                            f"최근 2일 평균 승률 {recent_avg:.1f}% vs "
                            f"이전 {len(older_rates)}일 평균 {older_avg:.1f}%"
                        ),
                        "action": "시장 레짐 변화 또는 진입 조건 드리프트를 점검하십시오.",
                    })
            # Detect consecutive loss days
            consecutive_loss = 0
            for r in daily_trend_rows:
                if r["sell_count"] > 0 and r.get("win_rate") is not None and r["win_rate"] < 40:
                    consecutive_loss += 1
                else:
                    break
            if consecutive_loss >= 3:
                improvement_items.append({
                    "priority": "P1",
                    "title": f"연속 {consecutive_loss}일 저승률 (일별 스냅샷 기반)",
                    "evidence": f"최근 {consecutive_loss}일 연속 승률 40% 미만",
                    "action": "진입·청산 조건을 즉시 재검토하고 모의매매 전환을 고려하십시오.",
                })
        except Exception as _trend_err:
            logger.debug("s6 daily trend enrich skip card=%s: %s", card_id, _trend_err)

        s6_rows_r = await db.execute(
            text(f"""
                SELECT id, position_id, stock_code, stock_name, price, quantity,
                       pnl_amount, pnl_pct, is_paper, trade_date, traded_at,
                       (
                           SELECT o.exit_reason
                           FROM go100_live_orders o
                           WHERE o.card_id = :card_id
                             AND o.user_id = :user_id
                             AND o.side = 'SELL'
                             AND o.stock_code = go100_trades_effective.stock_code
                             AND (
                                 o.order_id = go100_trades_effective.order_id
                                 OR (o.created_at AT TIME ZONE 'Asia/Seoul')::date = go100_trades_effective.trade_date
                             )
                           ORDER BY
                             CASE WHEN o.order_id = go100_trades_effective.order_id THEN 0 ELSE 1 END,
                             ABS(EXTRACT(EPOCH FROM (go100_trades_effective.traded_at - COALESCE(o.filled_at, o.created_at)))) ASC
                           LIMIT 1
                       ) AS exit_reason,
                       (
                           SELECT wd.features->'wave_context'
                           FROM go100_wave_decisions wd
                           WHERE (wd.features->>'card_id') ~ '^[0-9]+$'
                             AND (wd.features->>'card_id')::int = :card_id
                             AND wd.stock_code = go100_trades_effective.stock_code
                             AND wd.action IN ('buy', 'historical_replay')
                             AND (wd.features->>'position_id') ~ '^[0-9]+$'
                             AND (wd.features->>'position_id')::bigint = go100_trades_effective.position_id
                           ORDER BY CASE WHEN wd.action = 'buy' THEN 0 ELSE 1 END, wd.decision_time DESC
                           LIMIT 1
                       ) AS entry_wave_context,
                       (
                           SELECT wd.features->'wave_context'
                           FROM go100_wave_decisions wd
                           WHERE (wd.features->>'card_id') ~ '^[0-9]+$'
                             AND (wd.features->>'card_id')::int = :card_id
                             AND wd.stock_code = go100_trades_effective.stock_code
                             AND wd.action IN ('sell', 'historical_replay')
                             AND (wd.features->>'position_id') ~ '^[0-9]+$'
                             AND (wd.features->>'position_id')::bigint = go100_trades_effective.position_id
                           ORDER BY CASE WHEN wd.action = 'sell' THEN 0 ELSE 1 END, wd.decision_time DESC
                           LIMIT 1
                       ) AS exit_wave_context
                FROM go100_trades_effective
                WHERE {s6_where}
                ORDER BY traded_at DESC
                LIMIT 10
            """),
            s6_params,
        )
        s6_rows = s6_rows_r.fetchall()

        stages.append({
            "stage_id": 6,
            "stage_key": "daily_review",
            "label": "일일 리뷰",
            "count": s6_total,
            "status": "available" if s6_total > 0 else "empty",
            "updated_at": s6_last_at.isoformat() if s6_last_at else None,
            "source": "go100_trades",
            "is_paper_filter_applied": is_paper is not None,
            "rows": [
                {
                    "id": r.id,
                    "position_id": r.position_id,
                    "stock_code": _name_payload(r.stock_code, r.stock_name)["stock_code"],
                    "stock_name": _name_payload(r.stock_code, r.stock_name)["stock_name"],
                    "display_name": _name_payload(r.stock_code, r.stock_name)["display_name"],
                    "stock_name_missing": _name_payload(r.stock_code, r.stock_name)["stock_name_missing"],
                    "price": float(r.price) if r.price is not None else None,
                    "quantity": r.quantity,
                    "pnl_amount": float(r.pnl_amount) if r.pnl_amount is not None else None,
                    "pnl_pct": float(r.pnl_pct) if r.pnl_pct is not None else None,
                    "is_paper": r.is_paper,
                    "trade_date": str(r.trade_date) if r.trade_date else None,
                    "traded_at": r.traded_at.isoformat() if r.traded_at else None,
                    "exit_reason": r.exit_reason or "미기록",
                    "exit_result": _exit_result(r.pnl_amount, r.pnl_pct),
                    "entry_wave_context": _parse_json_field(r.entry_wave_context),
                    "exit_wave_context": _parse_json_field(r.exit_wave_context),
                    "wave_review": _build_wave_trade_review(r.entry_wave_context, r.exit_wave_context),
                    "review_result": _exit_result(r.pnl_amount, r.pnl_pct),
                    "review_note": (
                        "수익 거래: 진입 근거와 청산 규칙의 재현성을 확인하십시오."
                        if r.pnl_amount is not None and r.pnl_amount > 0
                        else "손실 거래: 진입 필터와 손절 준수 여부를 우선 점검하십시오."
                        if r.pnl_amount is not None and r.pnl_amount < 0
                        else "보합 거래: 거래비용 포함 실효 손익을 확인하십시오."
                    ),
                }
                for r in s6_rows
            ],
            "summary": {
                "total_sells": s6_total,
                "win_count": s6_win,
                "loss_count": s6_loss,
                "win_rate": s6_win_rate,
                "total_pnl": float(s6_stats.total_pnl) if s6_stats.total_pnl is not None else 0,
                "avg_pnl_pct": s6_avg_pnl_pct,
                "review_basis": "RULE_BASED_ACTUAL_TRADES",
                "improvement_items": improvement_items,
                "daily_trend": daily_trend_rows,
            },
        })
    except Exception as e:
        logger.warning("workbench stage6 error card=%s: %s", card_id, e)
        await db.rollback()
        diagnostics.append({"stage": 6, "key": "daily_review", "error": str(e)})
        stages.append({
            "stage_id": 6, "stage_key": "daily_review", "label": "일일 리뷰",
            "count": 0, "status": "unavailable", "updated_at": None,
            "source": "go100_trades", "is_paper_filter_applied": is_paper is not None,
            "rows": [], "summary": {},
        })

    # ── Lifecycle mode: 포지션별 매수→포지션→매도→체결 연결 ────────────────
    lifecycle_items: list[dict] = []
    if mode == "lifecycle":
        try:
            if is_paper is True:
                lifecycle_scope = " AND t.is_paper = true"
            elif is_paper is False:
                lifecycle_scope = " AND t.is_paper = false"
            else:
                lifecycle_scope = ""

            # 과거 live_orders.position_id는 비어 있으므로 SELL 체결 원장을 기준으로
            # position_id가 연결된 LIVE/PAPER 포지션을 보조 결합한다.
            lc_r = await db.execute(
                text(f"""
                    SELECT
                        COALESCE(
                            (SELECT e.trade_group_id
                             FROM go100_strategy_run_events e
                             WHERE e.go100_card_id = t.go100_card_id
                               AND e.user_id = t.user_id
                               AND e.card_version = t.card_version
                               AND e.stock_code = t.stock_code
                               AND e.trade_date = t.trade_date
                               AND e.trade_group_id IS NOT NULL
                             ORDER BY e.created_at DESC LIMIT 1),
                            CONCAT(t.go100_card_id, ':', t.stock_code, ':', t.trade_date, ':', COALESCE(t.position_id, t.id))
                        ) AS trade_group_id,
                        (SELECT MIN(e.created_at)
                         FROM go100_strategy_run_events e
                         WHERE e.go100_card_id = t.go100_card_id
                           AND e.user_id = t.user_id
                           AND e.card_version = t.card_version
                           AND e.stock_code = t.stock_code
                           AND e.trade_date = t.trade_date) AS selected_at,
                        NULL::bigint AS buy_order_id,
                        t.stock_code,
                        COALESCE(t.stock_name, p.stock_name, pp.stock_name, su.stock_name, t.stock_code) AS stock_name,
                        COALESCE(p.entry_price, pp.avg_price::numeric) AS buy_price,
                        COALESCE(p.quantity, pp.quantity) AS buy_qty,
                        COALESCE(p.created_at, pp.entry_date AT TIME ZONE 'Asia/Seoul') AS bought_at,
                        t.position_id,
                        COALESCE(p.status, pp.status) AS position_status,
                        COALESCE(p.pnl_pct, pp.unrealized_pnl_pct::numeric) AS unrealized_pnl_pct,
                        p.stop_loss_price,
                        p.take_profit_price,
                        p.trailing_pct,
                        t.order_id AS sell_order_id,
                        sell_order.exit_reason,
                        t.traded_at AS sold_at,
                        t.pnl_amount,
                        t.pnl_pct AS realized_pnl_pct,
                        (
                            SELECT wd.features->'wave_context'
                            FROM go100_wave_decisions wd
                            WHERE (wd.features->>'card_id') ~ '^[0-9]+$'
                              AND (wd.features->>'card_id')::int = :card_id
                              AND wd.stock_code = t.stock_code
                              AND wd.action IN ('buy', 'historical_replay')
                              AND (wd.features->>'position_id') ~ '^[0-9]+$'
                              AND (wd.features->>'position_id')::bigint = t.position_id
                            ORDER BY CASE WHEN wd.action = 'buy' THEN 0 ELSE 1 END, wd.decision_time DESC
                            LIMIT 1
                        ) AS entry_wave_context,
                        (
                            SELECT wd.features->'wave_context'
                            FROM go100_wave_decisions wd
                            WHERE (wd.features->>'card_id') ~ '^[0-9]+$'
                              AND (wd.features->>'card_id')::int = :card_id
                              AND wd.stock_code = t.stock_code
                              AND wd.action IN ('sell', 'historical_replay')
                              AND (wd.features->>'position_id') ~ '^[0-9]+$'
                              AND (wd.features->>'position_id')::bigint = t.position_id
                            ORDER BY CASE WHEN wd.action = 'sell' THEN 0 ELSE 1 END, wd.decision_time DESC
                            LIMIT 1
                        ) AS exit_wave_context,
                        ARRAY_REMOVE(ARRAY[
                            CASE WHEN t.id IS NOT NULL THEN 'go100_trades' END,
                            CASE WHEN p.id IS NOT NULL THEN 'go100_positions' END,
                            CASE WHEN pp.position_id IS NOT NULL THEN 'go100_paper_positions' END,
                            CASE WHEN sell_order.order_id IS NOT NULL THEN 'go100_live_orders' END
                        ], NULL) AS source_tables
                    FROM go100_trades_effective t
                    LEFT JOIN go100_positions p
                      ON p.id = t.position_id
                     AND p.go100_card_id = :card_id
                     AND p.user_id = :user_id
                     AND p.card_version = :card_version
                    LEFT JOIN go100_paper_positions pp
                     ON pp.position_id = t.position_id
                     AND pp.card_id = :card_id
                     AND pp.card_version = :card_version
                    LEFT JOIN go100_live_orders sell_order
                     ON sell_order.order_id = t.order_id
                     AND sell_order.side = 'SELL'
                     AND sell_order.card_version = :card_version
                    LEFT JOIN stock_universe su ON su.stock_code = t.stock_code
                    WHERE t.go100_card_id = :card_id
                      AND t.user_id = :user_id
                      AND t.card_version = :card_version
                      AND t.side = 'SELL'
                      {lifecycle_scope}
                    ORDER BY t.traded_at DESC
                    LIMIT 50
                """),
                {
                    "card_id": card_id, "user_id": user_id,
                    "card_version": active_card_version,
                },
            )
            lc_rows = lc_r.fetchall()
            lifecycle_items = [
                {
                    "trade_group_id": r.trade_group_id,
                    "selected_at": r.selected_at.isoformat() if r.selected_at else None,
                    "trace_gaps": [
                        key for key, missing in (
                            ("selection", r.selected_at is None),
                            ("buy", r.bought_at is None),
                            ("position", r.position_id is None),
                            ("sell", r.sold_at is None),
                        ) if missing
                    ],
                    "source_tables": list(r.source_tables or []),
                    "buy_order_id": r.buy_order_id,
                    "stock_code": r.stock_code,
                    "stock_name": r.stock_name,
                    "buy_price": float(r.buy_price) if r.buy_price is not None else None,
                    "buy_qty": r.buy_qty,
                    "bought_at": r.bought_at.isoformat() if r.bought_at else None,
                    "position_id": r.position_id,
                    "position_status": r.position_status,
                    "unrealized_pnl_pct": float(r.unrealized_pnl_pct) if r.unrealized_pnl_pct is not None else None,
                    "stop_loss_price": float(r.stop_loss_price) if r.stop_loss_price is not None else None,
                    "take_profit_price": float(r.take_profit_price) if r.take_profit_price is not None else None,
                    "trailing_pct": float(r.trailing_pct) if r.trailing_pct is not None else None,
                    "sell_order_id": r.sell_order_id,
                    "exit_reason": r.exit_reason,
                    "sold_at": r.sold_at.isoformat() if r.sold_at else None,
                    "pnl_amount": float(r.pnl_amount) if r.pnl_amount is not None else None,
                    "realized_pnl_pct": float(r.realized_pnl_pct) if r.realized_pnl_pct is not None else None,
                    "entry_wave_context": _parse_json_field(r.entry_wave_context),
                    "exit_wave_context": _parse_json_field(r.exit_wave_context),
                    "wave_review": _build_wave_trade_review(r.entry_wave_context, r.exit_wave_context),
                }
                for r in lc_rows
            ]
        except Exception as e:
            logger.warning("workbench lifecycle error card=%s: %s", card_id, e)
            diagnostics.append({"stage": "lifecycle", "key": "lifecycle_items", "error": str(e)})

    # ── 기간 분석: 일별 추이·손익 분포·청산 유형별 성과 ─────────────────────
    period_analysis: Optional[dict[str, Any]] = None
    available_regimes: list[str] = []
    if mode == "date_range":
        try:
            range_from = parsed_from or (datetime.now(KST).date() - timedelta(days=7))
            range_to = parsed_to or datetime.now(KST).date()
            pa_params: dict[str, Any] = {
                "card_id": card_id,
                "user_id": user_id,
                "date_from": range_from,
                "date_to": range_to,
                "card_version": active_card_version,
            }
            pa_where = (
                "t.go100_card_id = :card_id AND t.user_id = :user_id "
                "AND t.card_version = :card_version AND t.side = 'SELL' "
                "AND t.trade_date BETWEEN :date_from AND :date_to"
            )
            if is_paper is not None:
                pa_where += " AND t.is_paper = :is_paper"
                pa_params["is_paper"] = is_paper
            if normalized_regime:
                pa_where += (
                    " AND EXISTS (SELECT 1 FROM v4_market_regime_daily mr "
                    "WHERE mr.date = t.trade_date AND UPPER(mr.regime) = :market_regime)"
                )
                pa_params["market_regime"] = normalized_regime

            daily_r = await db.execute(
                text(f"""
                    SELECT t.trade_date,
                           COUNT(*) AS sample_count,
                           COUNT(*) FILTER (WHERE t.pnl_amount > 0) AS win_count,
                           COUNT(*) FILTER (WHERE t.pnl_amount <= 0) AS loss_count,
                           COALESCE(SUM(t.pnl_amount), 0) AS total_pnl,
                           AVG(t.pnl_pct) FILTER (WHERE t.pnl_pct IS NOT NULL) AS avg_pnl_pct,
                           COALESCE((
                               SELECT mr.regime FROM v4_market_regime_daily mr
                               WHERE mr.date = t.trade_date
                               ORDER BY CASE WHEN mr.market_type = 'KOSPI' THEN 0 ELSE 1 END, mr.id DESC
                               LIMIT 1
                           ), 'UNKNOWN') AS market_regime
                    FROM go100_trades_effective t
                    WHERE {pa_where}
                    GROUP BY t.trade_date
                    ORDER BY t.trade_date
                """),
                pa_params,
            )
            daily_rows = daily_r.fetchall()

            error_regime_sql = ""
            error_params: dict[str, Any] = {
                "card_id": card_id,
                "user_id": user_id,
                "date_from": range_from,
                "date_to": range_to,
                "card_version": active_card_version,
            }
            if normalized_regime:
                error_regime_sql = (
                    " AND EXISTS (SELECT 1 FROM v4_market_regime_daily mr "
                    "WHERE mr.date = (created_at AT TIME ZONE 'Asia/Seoul')::date "
                    "AND UPPER(mr.regime) = :market_regime)"
                )
                error_params["market_regime"] = normalized_regime
            error_mode_sql = ""
            if is_paper is not None:
                error_mode_sql = " AND is_paper = :is_paper"
                error_params["is_paper"] = is_paper
            errors_r = await db.execute(
                text(f"""
                    SELECT (created_at AT TIME ZONE 'Asia/Seoul')::date AS event_date,
                           COUNT(*) AS error_count
                    FROM go100_strategy_run_events
                    WHERE go100_card_id = :card_id AND user_id = :user_id
                      AND card_version = :card_version
                      {error_mode_sql}
                      AND (created_at AT TIME ZONE 'Asia/Seoul')::date BETWEEN :date_from AND :date_to
                      AND (decision IN ('fail', 'reject', 'blocked', 'error') OR reason_code IS NOT NULL)
                      {error_regime_sql}
                    GROUP BY event_date
                """),
                error_params,
            )
            error_by_date = {str(r.event_date): int(r.error_count or 0) for r in errors_r.fetchall()}

            distribution_r = await db.execute(
                text(f"""
                    SELECT CASE
                               WHEN t.pnl_pct < -5 THEN '< -5%'
                               WHEN t.pnl_pct < -2 THEN '-5~-2%'
                               WHEN t.pnl_pct < 0 THEN '-2~0%'
                               WHEN t.pnl_pct < 2 THEN '0~2%'
                               WHEN t.pnl_pct < 5 THEN '2~5%'
                               ELSE '>= 5%'
                           END AS bucket,
                           COUNT(*) AS cnt
                    FROM go100_trades_effective t
                    WHERE {pa_where} AND t.pnl_pct IS NOT NULL
                    GROUP BY bucket
                    ORDER BY MIN(t.pnl_pct)
                """),
                pa_params,
            )
            distribution_rows = distribution_r.fetchall()

            exit_r = await db.execute(
                text(f"""
                    SELECT COALESCE(NULLIF(exit_order.exit_reason, ''), '미분류') AS exit_reason,
                           COUNT(*) AS trade_count,
                           COUNT(*) FILTER (WHERE t.pnl_amount > 0) AS win_count,
                           COALESCE(SUM(t.pnl_amount), 0) AS total_pnl,
                           AVG(t.pnl_pct) FILTER (WHERE t.pnl_pct IS NOT NULL) AS avg_pnl_pct
                    FROM go100_trades_effective t
                    LEFT JOIN (
                        SELECT order_id::bigint AS order_id, exit_reason, false AS is_paper, card_version
                        FROM go100_live_orders WHERE side = 'SELL'
                        UNION ALL
                        SELECT order_id::bigint AS order_id, order_reason AS exit_reason,
                               true AS is_paper, card_version
                        FROM go100_paper_orders WHERE UPPER(order_type) = 'SELL'
                    ) exit_order
                      ON exit_order.order_id = t.order_id
                     AND exit_order.is_paper = t.is_paper
                     AND exit_order.card_version = t.card_version
                    WHERE {pa_where}
                    GROUP BY COALESCE(NULLIF(exit_order.exit_reason, ''), '미분류')
                    ORDER BY trade_count DESC, exit_reason
                """),
                pa_params,
            )
            exit_rows = exit_r.fetchall()

            regimes_r = await db.execute(
                text("""
                    SELECT DISTINCT UPPER(regime) AS regime
                    FROM v4_market_regime_daily
                    WHERE date BETWEEN :date_from AND :date_to AND regime IS NOT NULL
                    ORDER BY regime
                """),
                {"date_from": range_from, "date_to": range_to},
            )
            available_regimes = [str(r.regime) for r in regimes_r.fetchall()]

            sample_size = sum(int(r.sample_count or 0) for r in daily_rows)
            confidence = "HIGH" if sample_size >= 100 else "MEDIUM" if sample_size >= 30 else "LOW"
            period_analysis = {
                "date_from": str(range_from),
                "date_to": str(range_to),
                "sample_size": sample_size,
                "confidence": confidence,
                "daily_trend": [
                    {
                        "trade_date": str(r.trade_date),
                        "sample_count": int(r.sample_count or 0),
                        "win_count": int(r.win_count or 0),
                        "loss_count": int(r.loss_count or 0),
                        "win_rate": round(int(r.win_count or 0) / int(r.sample_count or 1) * 100, 1),
                        "total_pnl": float(r.total_pnl or 0),
                        "avg_pnl_pct": round(float(r.avg_pnl_pct), 3) if r.avg_pnl_pct is not None else None,
                        "market_regime": r.market_regime,
                        "error_count": error_by_date.get(str(r.trade_date), 0),
                    }
                    for r in daily_rows
                ],
                "pnl_distribution": [
                    {"bucket": r.bucket, "count": int(r.cnt or 0)} for r in distribution_rows
                ],
                "exit_performance": [
                    {
                        "exit_reason": r.exit_reason,
                        "trade_count": int(r.trade_count or 0),
                        "win_rate": round(int(r.win_count or 0) / int(r.trade_count or 1) * 100, 1),
                        "total_pnl": float(r.total_pnl or 0),
                        "avg_pnl_pct": round(float(r.avg_pnl_pct), 3) if r.avg_pnl_pct is not None else None,
                    }
                    for r in exit_rows
                ],
            }
        except Exception as e:
            logger.warning("workbench period analysis error card=%s: %s", card_id, e)
            diagnostics.append({"stage": "period", "key": "period_analysis", "error": str(e)})

    # ── 데이터 신뢰도 요약 (비차단, SELECT 전용) ──────────────────────────────
    data_quality = await _build_data_quality_summary(db)

    # ── 응답 조합 ─────────────────────────────────────────────────────────────
    response: dict[str, Any] = {
        "checked_at": datetime.now(KST).isoformat(),
        "mode": mode,
        "is_paper_filter": is_paper,
        "data_quality": data_quality,
        "filters": {
            "card_version": active_card_version,
            "current_card_version": current_card_version,
            "market_regime": normalized_regime,
            "available_card_versions": available_card_versions,
            "available_market_regimes": available_regimes,
        },
        "card": card_info,
        "strategy_type": strategy_type,
        "stages": stages,
        "diagnostics": diagnostics,
    }
    if mode == "lifecycle":
        response["lifecycle_items"] = lifecycle_items
    if mode == "date_range":
        response["period_analysis"] = period_analysis

    elapsed_ms = round((time.perf_counter() - request_started) * 1000, 1)
    response["performance"] = {
        "elapsed_ms": elapsed_ms,
        "cache_hit": False,
        "cache_ttl_sec": _WORKBENCH_CACHE_TTL_SEC,
        "statement_timeout_ms": _WORKBENCH_DB_TIMEOUT_MS,
        "partial": bool(diagnostics),
        "stale": False,
        "limited_range": (
            {
                "mode": mode,
                "date_from": str(parsed_from) if parsed_from else None,
                "date_to": str(parsed_to) if parsed_to else None,
            }
            if limited_range else None
        ),
    }
    _workbench_cache_set(cache_key, response)
    logger.info(
        "workbench response card=%s mode=%s elapsed_ms=%s cache_hit=false diagnostics=%s",
        card_id, mode, elapsed_ms, len(diagnostics),
    )
    return response


# ---------------------------------------------------------------------------
# Improvement Proposals — daily review approval workflow
# ---------------------------------------------------------------------------

@router.get("/{card_id}/improvement-proposals")
async def list_improvement_proposals(
    card_id: int,
    trade_date: Optional[str] = Query(None, description="YYYY-MM-DD; defaults to today KST"),
    status: Optional[str] = Query(None, pattern="^(PENDING|APPROVED|REJECTED|APPLIED)$"),
    current_user: dict = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """전략카드별 개선안 목록 조회. trade_date 기본값: 오늘 KST."""
    user_id = await _effective_uid(current_user, db)

    # Verify card ownership
    card_check = await db.execute(
        text("""
            SELECT go100_card_id FROM go100_strategy_cards
            WHERE go100_card_id = :card_id AND user_id = :user_id AND card_status != 'RETIRED'
        """),
        {"card_id": card_id, "user_id": user_id},
    )
    if not card_check.fetchone():
        raise HTTPException(status_code=404, detail="전략카드를 찾을 수 없습니다.")

    target_date: _date
    try:
        target_date = _date.fromisoformat(trade_date) if trade_date else datetime.now(KST).date()
    except ValueError as exc:
        raise HTTPException(status_code=422, detail="날짜는 YYYY-MM-DD 형식이어야 합니다.") from exc

    where = ["go100_card_id = :card_id", "user_id = :user_id", "trade_date = :trade_date"]
    params: dict[str, Any] = {"card_id": card_id, "user_id": user_id, "trade_date": target_date}
    if status:
        where.append("status = :status")
        params["status"] = status

    result = await db.execute(
        text(f"""
            SELECT proposal_id, go100_card_id, user_id, trade_date, stock_code,
                   trade_id, issue_type, priority, root_cause, proposed_action,
                   expected_impact, backtest_note, status, approver_id, approved_at,
                   rejection_reason, applied_at, auto_generated, source_stage, is_paper,
                   validation_status, backtest_result_json, proposed_changes_json,
                   rollback_card_version,
                   applied_card_version,
                   created_at, updated_at
            FROM go100_improvement_proposals
            WHERE {" AND ".join(where)}
            ORDER BY
                CASE status WHEN 'PENDING' THEN 0 WHEN 'APPROVED' THEN 1 WHEN 'APPLIED' THEN 2 ELSE 3 END,
                priority ASC,
                created_at DESC
        """),
        params,
    )
    rows = result.fetchall()
    return {
        "card_id": card_id,
        "trade_date": str(target_date),
        "total": len(rows),
        "items": [
            {
                "proposal_id": r.proposal_id,
                "issue_type": r.issue_type,
                "priority": r.priority,
                "root_cause": r.root_cause,
                "proposed_action": r.proposed_action,
                "expected_impact": r.expected_impact,
                "backtest_note": r.backtest_note,
                "validation_status": r.validation_status,
                "backtest_result": r.backtest_result_json or {},
                "proposed_changes": r.proposed_changes_json or {},
                "rollback_card_version": r.rollback_card_version,
                "applied_card_version": r.applied_card_version,
                "status": r.status,
                "stock_code": r.stock_code,
                "trade_id": r.trade_id,
                "auto_generated": r.auto_generated,
                "source_stage": r.source_stage,
                "is_paper": r.is_paper,
                "approver_id": r.approver_id,
                "approved_at": r.approved_at.isoformat() if r.approved_at else None,
                "rejection_reason": r.rejection_reason,
                "applied_at": r.applied_at.isoformat() if r.applied_at else None,
                "created_at": r.created_at.isoformat() if r.created_at else None,
                "updated_at": r.updated_at.isoformat() if r.updated_at else None,
            }
            for r in rows
        ],
    }


@router.post("/{card_id}/improvement-proposals")
async def create_improvement_proposal(
    card_id: int,
    body: dict = Body(...),
    current_user: dict = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """개선안 저장 (수동 생성 or 자동 생성 위임). 전략 자동 적용 없음 — 승인 후 권고사항으로만 기록됩니다."""
    user_id = await _effective_uid(current_user, db)

    # Verify card ownership
    card_check = await db.execute(
        text("""
            SELECT go100_card_id FROM go100_strategy_cards
            WHERE go100_card_id = :card_id AND user_id = :user_id AND card_status != 'RETIRED'
        """),
        {"card_id": card_id, "user_id": user_id},
    )
    if not card_check.fetchone():
        raise HTTPException(status_code=404, detail="전략카드를 찾을 수 없습니다.")

    issue_type = body.get("issue_type", "").strip()
    proposed_action = body.get("proposed_action", "").strip()
    if not issue_type or not proposed_action:
        raise HTTPException(status_code=422, detail="issue_type과 proposed_action은 필수입니다.")

    priority = body.get("priority", "P2")
    if priority not in ("INFO", "P1", "P2", "MONITOR"):
        priority = "P2"

    trade_date_raw = body.get("trade_date")
    try:
        trade_date = _date.fromisoformat(trade_date_raw) if trade_date_raw else datetime.now(KST).date()
    except ValueError as exc:
        raise HTTPException(status_code=422, detail="날짜는 YYYY-MM-DD 형식이어야 합니다.") from exc

    result = await db.execute(
        text("""
            INSERT INTO go100_improvement_proposals
                (go100_card_id, user_id, trade_date, stock_code, trade_id,
                 issue_type, priority, root_cause, proposed_action, expected_impact,
                 backtest_note, status, auto_generated, source_stage, is_paper)
            VALUES
                (:card_id, :user_id, :trade_date, :stock_code, :trade_id,
                 :issue_type, :priority, :root_cause, :proposed_action, :expected_impact,
                 :backtest_note, 'PENDING', :auto_generated, :source_stage, :is_paper)
            ON CONFLICT (go100_card_id, trade_date, issue_type, COALESCE(stock_code, ''))
            WHERE auto_generated = TRUE
            DO NOTHING
            RETURNING proposal_id
        """),
        {
            "card_id": card_id,
            "user_id": user_id,
            "trade_date": trade_date,
            "stock_code": body.get("stock_code"),
            "trade_id": body.get("trade_id"),
            "issue_type": issue_type,
            "priority": priority,
            "root_cause": body.get("root_cause"),
            "proposed_action": proposed_action,
            "expected_impact": body.get("expected_impact"),
            "backtest_note": body.get("backtest_note"),
            "auto_generated": bool(body.get("auto_generated", False)),
            "source_stage": body.get("source_stage", 6),
            "is_paper": body.get("is_paper"),
        },
    )
    row = result.fetchone()
    if not row:
        # ON CONFLICT did nothing (auto-generated duplicate)
        await db.commit()
        return {"created": False, "detail": "동일 날짜/유형의 자동 생성 개선안이 이미 존재합니다."}

    await db.execute(
        text("""
            INSERT INTO go100_improvement_proposal_events
                (proposal_id, go100_card_id, actor_user_id, event_type,
                 from_status, to_status, reason, metadata_json)
            VALUES
                (:proposal_id, :card_id, :user_id, 'CREATED',
                 NULL, 'PENDING', NULL, CAST(:metadata_json AS JSONB))
        """),
        {
            "proposal_id": row.proposal_id,
            "card_id": card_id,
            "user_id": user_id,
            "metadata_json": json.dumps(
                {
                    "auto_generated": bool(body.get("auto_generated", False)),
                    "source_stage": body.get("source_stage", 6),
                    "is_paper": body.get("is_paper"),
                }
            ),
        },
    )
    await db.commit()
    return {"created": True, "proposal_id": row.proposal_id}


@router.patch("/{card_id}/improvement-proposals/{proposal_id}")
async def update_improvement_proposal(
    card_id: int,
    proposal_id: int,
    body: dict = Body(...),
    current_user: dict = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """개선안 승인·거절·적용 처리. 적용은 검증·롤백 버전을 강제하고 새 카드 버전을 기록합니다.

    body.action: 'approve' | 'reject' | 'apply'
    body.rejection_reason: (거절 시 선택적)
    body.backtest_note: (승인 시 필수)
    body.rollback_card_version: (승인 시 필수)
    """
    user_id = await _effective_uid(current_user, db)

    # Verify ownership via card
    existing = await db.execute(
        text("""
            SELECT p.proposal_id, p.status, p.backtest_note, p.validation_status,
                   p.rollback_card_version, p.backtest_result_json,
                   p.proposed_changes_json,
                   COALESCE(c.card_version, c.version, 1) AS current_card_version,
                   c.entry_rules AS current_entry_rules,
                   c.exit_rules AS current_exit_rules,
                   c.risk_params AS current_risk_params,
                   c.strategy_params AS current_strategy_params,
                   c.max_stocks AS current_max_stocks
            FROM go100_improvement_proposals p
            JOIN go100_strategy_cards c ON c.go100_card_id = p.go100_card_id
            WHERE p.proposal_id = :proposal_id
              AND p.go100_card_id = :card_id
              AND c.user_id = :user_id
        """),
        {"proposal_id": proposal_id, "card_id": card_id, "user_id": user_id},
    )
    proposal = existing.fetchone()
    if not proposal:
        raise HTTPException(status_code=404, detail="개선안을 찾을 수 없습니다.")

    action = body.get("action", "").strip()
    if action not in ("approve", "reject", "apply"):
        raise HTTPException(status_code=422, detail="action은 approve | reject | apply 중 하나여야 합니다.")

    current_status = proposal.status

    # State machine validation
    valid_transitions: dict[str, list[str]] = {
        "approve": ["PENDING"],
        "reject": ["PENDING"],
        "apply": ["APPROVED"],
    }
    if current_status not in valid_transitions[action]:
        raise HTTPException(
            status_code=409,
            detail=f"현재 상태 '{current_status}'에서 '{action}' 전환은 허용되지 않습니다.",
        )

    backtest_note = str(body.get("backtest_note") or "").strip()
    rollback_version_raw = body.get("rollback_card_version")
    if action == "approve":
        proposed_changes = body.get("proposed_changes")
        allowed_change_keys = {"entry_rules", "exit_rules", "risk_params", "strategy_params", "max_stocks"}
        if not isinstance(proposed_changes, dict) or not proposed_changes:
            raise HTTPException(status_code=422, detail="승인 전 proposed_changes JSON 객체가 필요합니다.")
        unknown_keys = set(proposed_changes) - allowed_change_keys
        if unknown_keys:
            raise HTTPException(status_code=422, detail=f"허용되지 않은 설정 키: {', '.join(sorted(unknown_keys))}")
        for key in ("risk_params", "strategy_params"):
            if key in proposed_changes and not isinstance(proposed_changes[key], dict):
                raise HTTPException(status_code=422, detail=f"{key}는 JSON 객체여야 합니다.")
        for key in ("entry_rules", "exit_rules"):
            if key in proposed_changes and not isinstance(proposed_changes[key], (dict, list)):
                raise HTTPException(status_code=422, detail=f"{key}는 JSON 객체 또는 배열이어야 합니다.")
        if "max_stocks" in proposed_changes:
            value = proposed_changes["max_stocks"]
            if not isinstance(value, int) or isinstance(value, bool) or value < 1:
                raise HTTPException(status_code=422, detail="max_stocks는 1 이상의 정수여야 합니다.")
        if not backtest_note or rollback_version_raw is None:
            raise HTTPException(
                status_code=422,
                detail="승인 전 backtest_note와 rollback_card_version이 필요합니다.",
            )
        try:
            rollback_version = int(rollback_version_raw)
        except (TypeError, ValueError) as exc:
            raise HTTPException(status_code=422, detail="rollback_card_version은 정수여야 합니다.") from exc
        if rollback_version != int(proposal.current_card_version or 1):
            raise HTTPException(status_code=409, detail="카드 버전이 변경되었습니다. 새 버전으로 다시 검증해 주세요.")
    else:
        rollback_version = int(getattr(proposal, "rollback_card_version", 0) or 0)
        proposed_changes = getattr(proposal, "proposed_changes_json", None) or {}

    if action == "apply" and (
        getattr(proposal, "validation_status", None) != "BACKTESTED"
        or not getattr(proposal, "backtest_note", None)
        or not proposed_changes
        or rollback_version <= 0
    ):
        raise HTTPException(
            status_code=409,
            detail="백테스트 결과와 롤백 카드 버전 확인 후에만 적용할 수 있습니다.",
        )

    now = datetime.now(timezone.utc)
    expected_status = current_status
    new_status = {
        "approve": "APPROVED",
        "reject": "REJECTED",
        "apply": "APPLIED",
    }[action]
    if action == "approve":
        update_result = await db.execute(
            text("""
                UPDATE go100_improvement_proposals
                SET status = 'APPROVED', approver_id = :approver_id,
                    approved_at = :now,
                    backtest_note = :backtest_note,
                    validation_status = 'BACKTESTED',
                    backtest_result_json = CAST(:backtest_result AS JSONB),
                    proposed_changes_json = CAST(:proposed_changes AS JSONB),
                    rollback_card_version = :rollback_card_version
                WHERE proposal_id = :proposal_id AND status = :expected_status
                RETURNING proposal_id
            """),
            {
                "approver_id": user_id,
                "now": now,
                "backtest_note": body.get("backtest_note"),
                "backtest_result": json.dumps(body.get("backtest_result") or {}),
                "proposed_changes": json.dumps(proposed_changes),
                "rollback_card_version": rollback_version,
                "proposal_id": proposal_id,
                "expected_status": expected_status,
            },
        )
    elif action == "reject":
        update_result = await db.execute(
            text("""
                UPDATE go100_improvement_proposals
                SET status = 'REJECTED',
                    approver_id = :approver_id,
                    approved_at = :now,
                    rejection_reason = :reason
                WHERE proposal_id = :proposal_id AND status = :expected_status
                RETURNING proposal_id
            """),
            {
                "approver_id": user_id,
                "now": now,
                "reason": body.get("rejection_reason", ""),
                "proposal_id": proposal_id,
                "expected_status": expected_status,
            },
        )
    else:  # apply
        version_result = await db.execute(
            text("""
                UPDATE go100_strategy_cards
                SET entry_rules = CASE WHEN :entry_rules IS NULL THEN entry_rules ELSE CAST(:entry_rules AS JSONB) END,
                    exit_rules = CASE WHEN :exit_rules IS NULL THEN exit_rules ELSE CAST(:exit_rules AS JSONB) END,
                    risk_params = CASE WHEN :risk_params IS NULL THEN risk_params ELSE COALESCE(risk_params, '{}'::jsonb) || CAST(:risk_params AS JSONB) END,
                    strategy_params = CASE WHEN :strategy_params IS NULL THEN strategy_params ELSE COALESCE(strategy_params, '{}'::jsonb) || CAST(:strategy_params AS JSONB) END,
                    max_stocks = COALESCE(:max_stocks, max_stocks),
                    card_version = COALESCE(card_version, version, 1) + 1,
                    updated_at = :now
                WHERE go100_card_id = :card_id AND user_id = :user_id
                  AND COALESCE(card_version, version, 1) = :rollback_card_version
                RETURNING card_version
            """),
            {
                "now": now,
                "card_id": card_id,
                "user_id": user_id,
                "rollback_card_version": rollback_version,
                "entry_rules": json.dumps(proposed_changes["entry_rules"]) if "entry_rules" in proposed_changes else None,
                "exit_rules": json.dumps(proposed_changes["exit_rules"]) if "exit_rules" in proposed_changes else None,
                "risk_params": json.dumps(proposed_changes["risk_params"]) if "risk_params" in proposed_changes else None,
                "strategy_params": json.dumps(proposed_changes["strategy_params"]) if "strategy_params" in proposed_changes else None,
                "max_stocks": proposed_changes.get("max_stocks"),
            },
        )
        version_row = version_result.fetchone()
        if not version_row:
            await db.rollback()
            raise HTTPException(status_code=409, detail="롤백 버전과 현재 카드 버전이 일치하지 않습니다.")
        applied_card_version = int(version_row.card_version)
        await db.execute(
            text("""
                INSERT INTO go100_strategy_card_versions
                    (go100_card_id, card_version, snapshot_json, source_proposal_id)
                SELECT c.go100_card_id, c.card_version,
                       jsonb_build_object(
                           'strategy_name', c.strategy_name,
                           'card_status', c.card_status,
                           'is_active', c.is_active,
                           'is_live', c.is_live,
                           'allocated_amount', c.allocated_amount,
                           'max_stocks', c.max_stocks,
                           'entry_rules', c.entry_rules,
                           'exit_rules', c.exit_rules,
                           'risk_params', c.risk_params,
                           'strategy_params', c.strategy_params,
                           'updated_at', c.updated_at
                       ), :proposal_id
                FROM go100_strategy_cards c
                WHERE c.go100_card_id = :card_id AND c.user_id = :user_id
                ON CONFLICT (go100_card_id, card_version) DO NOTHING
            """),
            {"proposal_id": proposal_id, "card_id": card_id, "user_id": user_id},
        )
        update_result = await db.execute(
            text("""
                UPDATE go100_improvement_proposals
                SET status = 'APPLIED', applied_at = :now,
                    applied_card_version = :applied_card_version
                WHERE proposal_id = :proposal_id AND status = :expected_status
                RETURNING proposal_id
            """),
            {
                "now": now,
                "proposal_id": proposal_id,
                "expected_status": expected_status,
                "applied_card_version": applied_card_version,
            },
        )
        await db.execute(
            text("""
                INSERT INTO go100_strategy_edit_history
                    (strategy_card_id, user_id, edit_instruction,
                     before_rules, after_rules, field_changed, approved,
                     change_type, before_params, after_params, backtest_result,
                     approved_by, created_at)
                SELECT c.go100_card_id, :user_id, p.proposed_action,
                       jsonb_build_object('entry_rules', CAST(:before_entry_rules AS JSONB),
                                          'exit_rules', CAST(:before_exit_rules AS JSONB)),
                       jsonb_build_object('entry_rules', c.entry_rules, 'exit_rules', c.exit_rules),
                       'approved_improvement_proposal', TRUE,
                       'IMPROVEMENT_PROPOSAL_APPLIED',
                       jsonb_build_object('risk_params', CAST(:before_risk_params AS JSONB),
                                          'strategy_params', CAST(:before_strategy_params AS JSONB),
                                          'max_stocks', :before_max_stocks,
                                          'rollback_card_version', :rollback_card_version),
                       jsonb_build_object('risk_params', c.risk_params, 'strategy_params', c.strategy_params,
                                          'max_stocks', c.max_stocks,
                                          'applied_card_version', :applied_card_version),
                       p.backtest_result_json, :user_id, :now
                FROM go100_strategy_cards c
                JOIN go100_improvement_proposals p ON p.proposal_id = :proposal_id
                WHERE c.go100_card_id = :card_id AND c.user_id = :user_id
            """),
            {
                "user_id": user_id,
                "rollback_card_version": rollback_version,
                "applied_card_version": applied_card_version,
                "now": now,
                "proposal_id": proposal_id,
                "card_id": card_id,
                "before_entry_rules": json.dumps(proposal.current_entry_rules or []),
                "before_exit_rules": json.dumps(proposal.current_exit_rules or []),
                "before_risk_params": json.dumps(proposal.current_risk_params or {}),
                "before_strategy_params": json.dumps(proposal.current_strategy_params or {}),
                "before_max_stocks": proposal.current_max_stocks,
            },
        )

    if not update_result.fetchone():
        await db.rollback()
        raise HTTPException(
            status_code=409,
            detail="개선안 상태가 동시에 변경되었습니다. 새로고침 후 다시 시도해 주세요.",
        )

    await db.execute(
        text("""
            INSERT INTO go100_improvement_proposal_events
                (proposal_id, go100_card_id, actor_user_id, event_type,
                 from_status, to_status, reason, metadata_json)
            VALUES
                (:proposal_id, :card_id, :user_id, :event_type,
                 :from_status, :to_status, :reason, CAST(:metadata_json AS JSONB))
        """),
        {
            "proposal_id": proposal_id,
            "card_id": card_id,
            "user_id": user_id,
            "event_type": new_status,
            "from_status": current_status,
            "to_status": new_status,
            "reason": body.get("rejection_reason") if action == "reject" else None,
            "metadata_json": json.dumps(
                {
                    "backtest_note_updated": bool(body.get("backtest_note")),
                    "rollback_card_version": rollback_version or None,
                    "applied_card_version": locals().get("applied_card_version"),
                }
            ),
        },
    )
    await db.commit()
    return {
        "updated": True,
        "proposal_id": proposal_id,
        "new_status": new_status,
        "applied_card_version": locals().get("applied_card_version"),
    }


# ═══════════════════════════════════════════════════════════════════════════════
# Daily Results Snapshot API
# ═══════════════════════════════════════════════════════════════════════════════

@router.get("/{card_id}/daily-results")
async def get_daily_results(
    card_id: int,
    date_from: Optional[str] = Query(None, description="YYYY-MM-DD"),
    date_to: Optional[str] = Query(None, description="YYYY-MM-DD"),
    card_version: Optional[int] = Query(None),
    mode: Optional[str] = Query(None, pattern="^(all|paper|live)$"),
    current_user: dict = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """전략카드별 일자별 결과 스냅샷 조회 (persisted 테이블)."""
    user_id = await _effective_uid(current_user, db)

    # Ownership check
    owner_r = await db.execute(
        text("SELECT user_id FROM go100_strategy_cards WHERE go100_card_id = :card_id"),
        {"card_id": card_id},
    )
    owner_row = owner_r.fetchone()
    if not owner_row or int(owner_row.user_id) != user_id:
        raise HTTPException(status_code=404, detail="전략카드를 찾을 수 없습니다.")

    now_kst = datetime.now(KST).date()
    try:
        parsed_from = _date.fromisoformat(date_from) if date_from else (now_kst - timedelta(days=30))
        parsed_to = _date.fromisoformat(date_to) if date_to else now_kst
    except ValueError as e:
        raise HTTPException(status_code=422, detail=f"날짜 형식 오류: {e}") from e

    # Resolve card_version if not specified
    if card_version is None:
        cv_r = await db.execute(
            text("SELECT card_version FROM go100_strategy_cards WHERE go100_card_id = :card_id"),
            {"card_id": card_id},
        )
        cv_row = cv_r.fetchone()
        card_version = int(cv_row.card_version) if cv_row and cv_row.card_version else 1

    params: dict[str, Any] = {
        "card_id": card_id,
        "card_version": card_version,
        "date_from": parsed_from,
        "date_to": parsed_to,
    }
    extra = ""
    if mode:
        extra += " AND mode = :mode"
        params["mode"] = mode

    rows_r = await db.execute(
        text(f"""
            SELECT id, go100_card_id, user_id, trade_date, card_version, mode,
                   event_count, candidate_count, pass_count, error_count,
                   buy_count, sell_count, win_count, loss_count, win_rate,
                   realized_pnl, avg_pnl_pct, max_pnl_pct, min_pnl_pct,
                   unique_stocks, market_regime, confidence, source_range,
                   raw_metrics_json, computed_at
            FROM go100_strategy_card_daily_results
            WHERE go100_card_id = :card_id
              AND card_version = :card_version
              AND trade_date BETWEEN :date_from AND :date_to
              {extra}
            ORDER BY trade_date DESC
        """),
        params,
    )
    rows = rows_r.fetchall()

    return {
        "total": len(rows),
        "date_from": str(parsed_from),
        "date_to": str(parsed_to),
        "card_version": card_version,
        "source": "persisted",
        "items": [
            {
                "id": r.id,
                "go100_card_id": r.go100_card_id,
                "trade_date": str(r.trade_date),
                "card_version": r.card_version,
                "mode": r.mode,
                "event_count": r.event_count,
                "candidate_count": r.candidate_count,
                "pass_count": r.pass_count,
                "error_count": r.error_count,
                "buy_count": r.buy_count,
                "sell_count": r.sell_count,
                "win_count": r.win_count,
                "loss_count": r.loss_count,
                "win_rate": float(r.win_rate) if r.win_rate is not None else None,
                "realized_pnl": float(r.realized_pnl) if r.realized_pnl is not None else 0.0,
                "avg_pnl_pct": float(r.avg_pnl_pct) if r.avg_pnl_pct is not None else None,
                "max_pnl_pct": float(r.max_pnl_pct) if r.max_pnl_pct is not None else None,
                "min_pnl_pct": float(r.min_pnl_pct) if r.min_pnl_pct is not None else None,
                "unique_stocks": r.unique_stocks,
                "market_regime": r.market_regime or "UNKNOWN",
                "confidence": r.confidence or "LOW",
                "source_range": r.source_range or "",
                "computed_at": r.computed_at.isoformat() if r.computed_at else None,
            }
            for r in rows
        ],
    }


@router.post("/{card_id}/daily-results/recompute")
async def recompute_daily_results(
    card_id: int,
    body: dict = Body(default={}),
    current_user: dict = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """일자별 결과 스냅샷 재집계 (소유자 전용).

    Body params (optional):
        date_from: YYYY-MM-DD  (default: 30일 전)
        date_to:   YYYY-MM-DD  (default: 오늘)
        mode:      'all' | 'paper' | 'live'  (default: 'all')
    """
    from backend.app.services.go100.daily_results_service import (
        recompute_daily_results_for_card,
    )

    user_id = await _effective_uid(current_user, db)

    # Ownership check + card_version
    card_r = await db.execute(
        text("""
            SELECT user_id, card_version
            FROM go100_strategy_cards
            WHERE go100_card_id = :card_id
        """),
        {"card_id": card_id},
    )
    card_row = card_r.fetchone()
    if not card_row or int(card_row.user_id) != user_id:
        raise HTTPException(status_code=404, detail="전략카드를 찾을 수 없습니다.")

    card_version = int(card_row.card_version or 1)

    now_kst = datetime.now(KST).date()
    try:
        date_from = _date.fromisoformat(body.get("date_from") or str(now_kst - timedelta(days=30)))
        date_to = _date.fromisoformat(body.get("date_to") or str(now_kst))
    except (ValueError, TypeError) as e:
        raise HTTPException(status_code=422, detail=f"날짜 형식 오류: {e}") from e

    mode = body.get("mode", "all")
    if mode not in ("all", "paper", "live"):
        raise HTTPException(status_code=422, detail="mode는 'all', 'paper', 'live' 중 하나여야 합니다.")

    results = await recompute_daily_results_for_card(
        db, card_id, user_id, card_version, date_from, date_to, mode
    )
    await db.commit()

    ok_count = sum(1 for r in results if r.get("status") == "ok")
    err_count = len(results) - ok_count
    return {
        "card_id": card_id,
        "card_version": card_version,
        "date_from": str(date_from),
        "date_to": str(date_to),
        "mode": mode,
        "computed_dates": len(results),
        "ok": ok_count,
        "errors": err_count,
        "details": results,
    }


# ── Trade Journal (종목별 매매일지) ───────────────────────────────────────────

@router.get("/{card_id}/trade-journal/{stock_code}")
async def get_trade_journal(
    card_id: int,
    stock_code: str,
    trade_date: Optional[str] = Query(None),
    current_user: dict = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """종목별 매매일지 — 상세 레포트 + B/S 차트 데이터."""
    user_id = await _effective_uid(current_user, db)
    target_date = _date.fromisoformat(trade_date) if trade_date else datetime.now(KST).date()

    # 1. 체결 내역
    trades_r = await db.execute(
        text("""
            SELECT id, side, price, quantity, amount, pnl_amount, pnl_pct,
                   is_paper, traded_at, order_id, position_id
            FROM go100_trades_effective
            WHERE go100_card_id = :card_id AND stock_code = :stock_code
              AND trade_date = :td AND user_id = :uid
            ORDER BY traded_at ASC
        """),
        {"card_id": card_id, "stock_code": stock_code, "td": target_date, "uid": user_id},
    )
    trades = trades_r.fetchall()

    buys = [t for t in trades if t.side == "BUY"]
    sells = [t for t in trades if t.side == "SELL"]

    buy_price = float(buys[0].price) if buys else None
    sell_price = float(sells[-1].price) if sells else None
    total_buy_qty = sum(t.quantity or 0 for t in buys)
    total_sell_qty = sum(t.quantity or 0 for t in sells)
    total_pnl = sum(float(t.pnl_amount or 0) for t in sells)
    avg_pnl_pct = (
        sum(float(t.pnl_pct or 0) for t in sells) / len(sells)
        if sells else None
    )

    holding_minutes = None
    if buys and sells:
        first_buy = buys[0].traded_at
        last_sell = sells[-1].traded_at
        if first_buy and last_sell:
            holding_minutes = int((last_sell - first_buy).total_seconds() / 60)

    # 2. 주문 상세 (exit_reason 등)
    orders_r = await db.execute(
        text("""
            SELECT order_id, side, status, order_price, filled_price,
                   quantity, filled_quantity, exit_reason, created_at, filled_at
            FROM go100_live_orders
            WHERE card_id = :card_id AND stock_code = :stock_code
              AND user_id = :uid
              AND (created_at AT TIME ZONE 'Asia/Seoul')::date = :td
            ORDER BY created_at ASC
        """),
        {"card_id": card_id, "stock_code": stock_code, "uid": user_id, "td": target_date},
    )
    orders = orders_r.fetchall()
    exit_reason = None
    for o in reversed(orders):
        if o.exit_reason:
            exit_reason = o.exit_reason
            break

    # 3. 진입 분석 (decision_logs)
    entry_r = await db.execute(
        text("""
            SELECT stage, decision, reason_code, reason_text, metrics_json, created_at
            FROM go100_trade_decision_logs
            WHERE go100_card_id = :card_id AND stock_code = :stock_code
              AND trade_date = :td
              AND stage IN ('entry', 'candidate_generation')
              AND decision IN ('buy', 'pass', 'fail')
            ORDER BY created_at ASC
            LIMIT 10
        """),
        {"card_id": card_id, "stock_code": stock_code, "td": target_date},
    )
    entry_logs = entry_r.fetchall()

    entry_analysis = None
    for el in entry_logs:
        if el.decision == "buy":
            metrics = None
            if el.metrics_json:
                try:
                    metrics = json.loads(el.metrics_json) if isinstance(el.metrics_json, str) else el.metrics_json
                except Exception:
                    pass
            entry_analysis = {
                "stage": el.stage,
                "decision": el.decision,
                "reason_code": el.reason_code,
                "reason_text": el.reason_text,
                "metrics": metrics,
                "decided_at": el.created_at.isoformat() if el.created_at else None,
            }
            break

    # 4. 타임라인
    timeline: list[dict] = []
    for el in entry_logs:
        if el.decision in ("pass", "buy"):
            timeline.append({
                "time": el.created_at.isoformat() if el.created_at else None,
                "event": "entry_signal" if el.decision == "pass" else "entry_decision",
                "detail": el.reason_text or el.reason_code or el.decision,
            })
    for t in trades:
        evt = "buy_filled" if t.side == "BUY" else "sell_filled"
        timeline.append({
            "time": t.traded_at.isoformat() if t.traded_at else None,
            "event": evt,
            "price": float(t.price) if t.price else None,
            "quantity": t.quantity,
            "pnl_pct": float(t.pnl_pct) if t.pnl_pct is not None else None,
            "exit_reason": exit_reason if t.side == "SELL" else None,
        })
    timeline.sort(key=lambda x: x.get("time") or "")

    # 5. 분봉 차트 데이터 (lightweight-charts format)
    candles: list[dict] = []
    try:
        ohlcv_r = await db.execute(
            text("""
                SELECT trade_time, open_price, high_price, low_price, close_price, volume
                FROM v4_ohlcv_minute
                WHERE stock_code = :stock_code AND trade_date = :td
                ORDER BY trade_time ASC
            """),
            {"stock_code": stock_code, "td": target_date},
        )
        for row in ohlcv_r.fetchall():
            ts = int(row.trade_time.timestamp()) if row.trade_time else 0
            candles.append({
                "time": ts,
                "open": float(row.open_price or 0),
                "high": float(row.high_price or 0),
                "low": float(row.low_price or 0),
                "close": float(row.close_price or 0),
                "volume": int(row.volume or 0),
            })
    except Exception:
        pass

    # 6. 매수/매도 마커
    markers: list[dict] = []
    for t in trades:
        if t.traded_at:
            ts = int(t.traded_at.timestamp())
            if t.side == "BUY":
                markers.append({
                    "time": ts,
                    "position": "belowBar",
                    "color": "#ef4444",
                    "shape": "arrowUp",
                    "text": f"B {int(t.price):,}",
                })
            else:
                markers.append({
                    "time": ts,
                    "position": "aboveBar",
                    "color": "#3b82f6",
                    "shape": "arrowDown",
                    "text": f"S {int(t.price):,}",
                })

    # 7. 손절/목표 수평선
    lines: list[dict] = []
    try:
        pos_r = await db.execute(
            text("""
                SELECT stop_loss_price, take_profit_price
                FROM go100_positions
                WHERE go100_card_id = :card_id AND stock_code = :stock_code
                  AND user_id = :uid
                ORDER BY created_at DESC LIMIT 1
            """),
            {"card_id": card_id, "stock_code": stock_code, "uid": user_id},
        )
        pos = pos_r.fetchone()
        if pos:
            if pos.stop_loss_price:
                lines.append({"price": float(pos.stop_loss_price), "color": "#3b82f6", "label": "손절가"})
            if pos.take_profit_price:
                lines.append({"price": float(pos.take_profit_price), "color": "#ef4444", "label": "목표가"})
    except Exception:
        pass

    # 8. 종목명
    name_r = await db.execute(
        text("SELECT stock_name FROM stock_universe WHERE stock_code = :code LIMIT 1"),
        {"code": stock_code},
    )
    name_row = name_r.fetchone()
    stock_name = name_row.stock_name if name_row else stock_code

    return {
        "card_id": card_id,
        "stock_code": stock_code,
        "stock_name": stock_name,
        "trade_date": str(target_date),
        "summary": {
            "buy_price": buy_price,
            "sell_price": sell_price,
            "buy_quantity": total_buy_qty,
            "sell_quantity": total_sell_qty,
            "pnl_amount": round(total_pnl, 2) if total_pnl else None,
            "pnl_pct": round(avg_pnl_pct, 2) if avg_pnl_pct is not None else None,
            "holding_minutes": holding_minutes,
            "exit_reason": exit_reason,
            "is_round_trip": bool(buys and sells),
        },
        "timeline": timeline,
        "entry_analysis": entry_analysis,
        "chart_data": {
            "candles": candles,
            "markers": markers,
            "lines": lines,
        },
    }
