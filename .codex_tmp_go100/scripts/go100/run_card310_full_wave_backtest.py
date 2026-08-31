#!/usr/bin/env python3
"""Run #310 full-wave-cycle one-day, one-stock backtest."""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import psycopg2
from psycopg2.extras import RealDictCursor

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "backend"))

from backend.app.services.go100.analysis.wave_cycle_trader import WaveCycleTrader

KST = timezone(timedelta(hours=9))
DB = os.environ.get(
    "DATABASE_URL",
    f"dbname={os.environ.get('DB_NAME','kisautotrade')} user={os.environ.get('DB_USER','kis_admin')} host={os.environ.get('DB_HOST','localhost')} password={os.environ.get('DB_PASSWORD','')}",
)


def connect():
    return psycopg2.connect(DB)


def latest_trade_date(conn) -> date:
    with conn.cursor() as cur:
        cur.execute("SELECT MAX(trade_date)::date FROM v4_ohlcv_minute WHERE trade_time <= '15:30'")
        row = cur.fetchone()
    if not row or not row[0]:
        raise RuntimeError("v4_ohlcv_minute has no trade_date")
    return row[0]


def select_one_stock(conn, trade_date: date) -> dict[str, Any]:
    sql = """
        WITH day_bars AS (
            SELECT
                m.stock_code,
                COUNT(*) AS bar_count,
                SUM(COALESCE(m.trade_amount, (m.close_price::bigint * m.volume))) AS total_trade_amount,
                SUM(m.volume) AS total_volume,
                MIN(m.low_price) AS day_low,
                MAX(m.high_price) AS day_high,
                (ARRAY_AGG(m.open_price ORDER BY m.trade_time))[1] AS open_price,
                (ARRAY_AGG(m.close_price ORDER BY m.trade_time DESC))[1] AS close_price,
                MAX(CASE WHEN m.trade_time <= '09:05' THEN m.high_price ELSE NULL END) AS high_0905
            FROM v4_ohlcv_minute m
            WHERE m.trade_date = %s
              AND m.trade_time BETWEEN '09:00' AND '15:20'
              AND m.open_price > 0 AND m.high_price > 0 AND m.low_price > 0 AND m.close_price > 0
            GROUP BY m.stock_code
            HAVING COUNT(*) >= 120
        )
        SELECT
            d.stock_code,
            COALESCE(NULLIF(TRIM(su.stock_name), ''), d.stock_code) AS stock_name,
            d.bar_count,
            d.total_trade_amount,
            d.total_volume,
            ROUND(((d.day_high - d.day_low)::numeric / NULLIF(d.day_low, 0) * 100), 4) AS intraday_range_pct,
            ROUND(((d.close_price - d.open_price)::numeric / NULLIF(d.open_price, 0) * 100), 4) AS day_return_pct,
            ROUND(((COALESCE(d.high_0905, d.open_price) - d.open_price)::numeric / NULLIF(d.open_price, 0) * 100), 4) AS opening_5m_high_pct
        FROM day_bars d
        LEFT JOIN stock_universe su ON su.stock_code = d.stock_code
        ORDER BY
            CASE WHEN ((COALESCE(d.high_0905, d.open_price) - d.open_price)::numeric / NULLIF(d.open_price, 0) * 100) >= 3 THEN 0 ELSE 1 END,
            d.total_trade_amount DESC NULLS LAST,
            intraday_range_pct DESC NULLS LAST
        LIMIT 1
    """
    with conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute(sql, (trade_date,))
        row = cur.fetchone()
    if not row:
        raise RuntimeError(f"no screener candidate for {trade_date}")
    return dict(row)


def load_bars(conn, trade_date: date, stock_code: str) -> list[dict[str, Any]]:
    with conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute(
            """
            SELECT trade_time, open_price, high_price, low_price, close_price, volume
            FROM v4_ohlcv_minute
            WHERE trade_date = %s AND stock_code = %s AND trade_time BETWEEN '09:00' AND '15:20'
            ORDER BY trade_time
            """,
            (trade_date, stock_code),
        )
        rows = cur.fetchall()
    return [
        {
            "t": r["trade_time"].strftime("%H:%M:%S"),
            "o": float(r["open_price"]),
            "h": float(r["high_price"]),
            "l": float(r["low_price"]),
            "c": float(r["close_price"]),
            "v": float(r["volume"] or 0),
        }
        for r in rows
    ]


def run_backtest(trade_date: date, selected: dict[str, Any], bars: list[dict[str, Any]]) -> dict[str, Any]:
    trader = WaveCycleTrader(
        {
            "min_bars": 25,
            "max_daily_trades": 20,
            "stop_loss_pct": -2.0,
            "min_pullback_pct": 0.35,
            "max_pullback_pct": 8.0,
            "min_mtf_alignment": 0.0,
            "trailing_activation_pct": 0.8,
            "force_exit_time": "15:20",
        }
    )
    position = None
    trades: list[dict[str, Any]] = []
    signals: list[dict[str, Any]] = []

    for i in range(len(bars)):
        sig = trader.evaluate(selected["stock_code"], bars[: i + 1]).to_dict()
        if sig["action"] in {"BUY", "SELL"}:
            signals.append(sig)

        if sig["action"] == "BUY" and position is None:
            position = {
                "entry_time": sig["timestamp"],
                "entry_price": sig["price"],
                "entry_wave": sig["wave_number_1m"],
                "entry_reason": sig["reason"],
                "qty_ratio": sig["qty_ratio"],
            }
        elif sig["action"] == "SELL" and position is not None:
            pnl_pct = (sig["price"] - position["entry_price"]) / position["entry_price"] * 100.0
            trades.append(
                {
                    **position,
                    "exit_time": sig["timestamp"],
                    "exit_price": sig["price"],
                    "exit_wave": sig["wave_number_1m"],
                    "exit_reason": sig["reason"],
                    "pnl_pct": round(pnl_pct, 4),
                }
            )
            position = None

    if position is not None and bars:
        last = bars[-1]
        exit_price = float(last["c"])
        pnl_pct = (exit_price - position["entry_price"]) / position["entry_price"] * 100.0
        trades.append(
            {
                **position,
                "exit_time": last["t"],
                "exit_price": round(exit_price, 4),
                "exit_wave": 0,
                "exit_reason": "EOD_FALLBACK",
                "pnl_pct": round(pnl_pct, 4),
            }
        )

    wins = [t for t in trades if t["pnl_pct"] > 0]
    losses = [t for t in trades if t["pnl_pct"] <= 0]
    gross_profit = sum(t["pnl_pct"] for t in wins)
    gross_loss = abs(sum(t["pnl_pct"] for t in losses))
    return {
        "generated_at_kst": datetime.now(KST).strftime("%Y-%m-%d %H:%M:%S KST"),
        "card_id": 310,
        "card_name": "전파동 사이클 1종목 스캘핑",
        "trade_date": str(trade_date),
        "selected_stock": selected,
        "bar_count": len(bars),
        "summary": {
            "total_trades": len(trades),
            "wins": len(wins),
            "losses": len(losses),
            "win_rate_pct": round(len(wins) / len(trades) * 100.0, 2) if trades else 0.0,
            "sum_pnl_pct": round(sum(t["pnl_pct"] for t in trades), 4),
            "avg_pnl_pct": round(sum(t["pnl_pct"] for t in trades) / len(trades), 4) if trades else 0.0,
            "profit_factor": round(gross_profit / gross_loss, 4) if gross_loss > 0 else (999.0 if gross_profit > 0 else 0.0),
        },
        "trades": trades,
        "signals": signals[:200],
    }


def write_outputs(result: dict[str, Any]) -> tuple[Path, Path]:
    out_dir = ROOT / "reports/go100"
    out_dir.mkdir(parents=True, exist_ok=True)
    suffix = result["trade_date"].replace("-", "")
    json_path = out_dir / f"card310_full_wave_backtest_{suffix}.json"
    md_path = out_dir / f"card310_full_wave_backtest_{suffix}.md"
    json_path.write_text(json.dumps(result, ensure_ascii=False, indent=2, default=str), encoding="utf-8")

    s = result["summary"]
    st = result["selected_stock"]
    lines = [
        f"# GO100 #310 전파동 사이클 1일 1종목 백테스트 - {result['trade_date']}",
        "",
        f"- 생성시각: {result['generated_at_kst']}",
        f"- 자동 스크리너 선정: {st.get('stock_code')} {st.get('stock_name')}",
        f"- 선정 근거: 거래대금 {int(st.get('total_trade_amount') or 0):,}, 장중 변동폭 {st.get('intraday_range_pct')}%, 장초 5분 고점 {st.get('opening_5m_high_pct')}%",
        f"- 1분봉 수: {result['bar_count']}",
        "",
        "## 결과",
        "",
        "| 항목 | 값 |",
        "|---|---:|",
        f"| 거래 수 | {s['total_trades']} |",
        f"| 승/패 | {s['wins']} / {s['losses']} |",
        f"| 승률 | {s['win_rate_pct']}% |",
        f"| 누적 손익률 | {s['sum_pnl_pct']}% |",
        f"| 평균 손익률 | {s['avg_pnl_pct']}% |",
        f"| PF | {s['profit_factor']} |",
        "",
        "## 거래 내역",
        "",
        "| # | 매수시각 | 매수가 | 매수파동 | 매도시각 | 매도가 | 매도사유 | 손익률 |",
        "|---:|---|---:|---:|---|---:|---|---:|",
    ]
    for idx, t in enumerate(result["trades"], 1):
        lines.append(
            f"| {idx} | {t['entry_time']} | {t['entry_price']} | W{t['entry_wave']} | {t['exit_time']} | {t['exit_price']} | {t['exit_reason']} | {t['pnl_pct']}% |"
        )
    if not result["trades"]:
        lines.append("| - | - | - | - | - | - | 거래 없음 | 0.0% |")
    lines.extend(
        [
            "",
            "## 판정",
            "",
            "- 이 결과는 1일 1종목 리플레이 검증이며, 통계적 성능 검증이 아닙니다.",
            "- 자동 스크리너는 거래대금 우선, 장초 강도와 장중 변동폭을 보조 점수로 사용했습니다.",
            "- 실매매 승격 전 최소 5~20거래일 리플레이와 페이퍼 검증이 필요합니다.",
        ]
    )
    md_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return json_path, md_path


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--date", help="YYYY-MM-DD. Default latest v4_ohlcv_minute date")
    parser.add_argument("--stock-code", help="Optional explicit stock; otherwise auto screener selects one")
    args = parser.parse_args()

    with connect() as conn:
        target_date = datetime.strptime(args.date, "%Y-%m-%d").date() if args.date else latest_trade_date(conn)
        selected = {"stock_code": args.stock_code, "stock_name": args.stock_code, "manual": True} if args.stock_code else select_one_stock(conn, target_date)
        bars = load_bars(conn, target_date, selected["stock_code"])
        result = run_backtest(target_date, selected, bars)
    json_path, md_path = write_outputs(result)
    print(json.dumps({"json": str(json_path), "markdown": str(md_path), "summary": result["summary"], "selected_stock": result["selected_stock"]}, ensure_ascii=False, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
