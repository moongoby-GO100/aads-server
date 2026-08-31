#!/usr/bin/env python3
"""One-off read-only #119 current-condition direct simulator report.

Calls Go100MinuteSimulator directly with the current strategy card rules. It does
not create go100_backtest_runs and does not update the card/whitepaper.
"""
from __future__ import annotations

import asyncio
import json
import sys
from collections import Counter, defaultdict
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

from sqlalchemy import text

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))
if str(REPO_ROOT / "backend") not in sys.path:
    sys.path.insert(0, str(REPO_ROOT / "backend"))

from backend.app.core.database import AsyncSessionLocal
from backend.app.services.go100.backtest.minute_simulator import Go100MinuteSimulator
from backend.app.services.go100.execution_profile import build_go100_execution_profile, normalize_rules_for_profile

CARD_ID = 119
USER_ID = 15
OUT = Path("/tmp/go100_card119_current_3day_direct_report.json")


def _jsonable(value: Any) -> Any:
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, dict):
        return {str(k): _jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(v) for v in value]
    return value


def _load_json(value: Any, default: Any) -> Any:
    if value is None:
        return default
    if isinstance(value, str):
        try:
            return json.loads(value) if value else default
        except Exception:
            return default
    return value


async def fetch_rows(db, sql: str, params: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    result = await db.execute(text(sql), params or {})
    return [dict(row) for row in result.mappings().all()]


def summarize_rules(entry_rules: list[dict[str, Any]], exit_rules: list[dict[str, Any]]) -> dict[str, Any]:
    entries = []
    for rule in entry_rules if isinstance(entry_rules, list) else []:
        if not isinstance(rule, dict):
            continue
        params = rule.get("params") if isinstance(rule.get("params"), dict) else rule
        entries.append({"name": rule.get("name") or rule.get("type"), "params": params})
    exits = []
    for rule in exit_rules if isinstance(exit_rules, list) else []:
        if not isinstance(rule, dict):
            continue
        params = rule.get("params") if isinstance(rule.get("params"), dict) else rule
        exits.append({"type": rule.get("type") or rule.get("name"), "params": params})
    return {"entry_rules": entries, "exit_rules": exits}


async def main() -> None:
    report: dict[str, Any] = {"card_id": CARD_ID, "user_id": USER_ID, "mode": "direct_minute_simulator_no_run_insert"}
    async with AsyncSessionLocal() as db:
        kst = (await db.execute(text("SELECT NOW() AT TIME ZONE 'Asia/Seoul' AS kst_time"))).mappings().first()
        report["measured_kst"] = str(kst["kst_time"]) if kst else ""

        latest_days = await fetch_rows(db, """
            SELECT trade_date, COUNT(*) AS minute_rows, COUNT(DISTINCT stock_code) AS minute_stocks
            FROM v4_ohlcv_minute
            GROUP BY trade_date
            HAVING COUNT(*) > 1000 AND COUNT(DISTINCT stock_code) > 10
            ORDER BY trade_date DESC
            LIMIT 3
        """)
        latest_days = list(reversed(latest_days))
        if len(latest_days) < 3:
            raise RuntimeError(f"not enough minute-data trading days: {latest_days}")
        start_date_obj = latest_days[0]["trade_date"]
        end_date_obj = latest_days[-1]["trade_date"]
        start_date = str(start_date_obj)
        end_date = str(end_date_obj)
        report["trade_days"] = latest_days
        report["period"] = {"start_date": start_date, "end_date": end_date}

        card = (await db.execute(text("""
            SELECT go100_card_id, strategy_name, card_status, is_active, is_live,
                   allocated_amount, max_stocks, universe_filter, entry_rules, exit_rules,
                   risk_params, strategy_params, metadata, parent_card_id, desk_id, bar_timeframe
            FROM go100_strategy_cards
            WHERE go100_card_id = :cid AND user_id = :uid
        """), {"cid": CARD_ID, "uid": USER_ID})).mappings().first()
        if not card:
            raise RuntimeError("card #119 not found")
        card_dict = dict(card)
        universe_filter = _load_json(card_dict.get("universe_filter"), {})
        entry_rules = _load_json(card_dict.get("entry_rules"), [])
        exit_rules = _load_json(card_dict.get("exit_rules"), [])
        risk_params = dict(_load_json(card_dict.get("risk_params"), {}) or {})
        strategy_params = _load_json(card_dict.get("strategy_params"), {})
        metadata = _load_json(card_dict.get("metadata"), {})
        source_card_id = int((metadata or {}).get("source_card_id") or card_dict.get("parent_card_id") or 0)
        trade_engine = str((metadata or {}).get("trade_engine") or strategy_params.get("trade_engine") or "").strip().lower()
        if int(card_dict.get("go100_card_id") or CARD_ID) == 119 or source_card_id == 119 or trade_engine == "limitup_next_open":
            risk_params.setdefault("limit_up_exit_mode", "close_locked_next_open")
        risk_params.setdefault("max_stocks", card_dict.get("max_stocks") or 5)
        execution_profile = build_go100_execution_profile(
            desk_id=int(card_dict.get("desk_id") or 0),
            bar_timeframe=str(card_dict.get("bar_timeframe") or "").strip().lower(),
            entry_rules=entry_rules,
            exit_rules=exit_rules,
            risk_params=risk_params,
            strategy_params=strategy_params,
        )
        entry_rules, exit_rules, approximated_rules = normalize_rules_for_profile(entry_rules, exit_rules, execution_profile)
        risk_params.setdefault("execution_profile", execution_profile.to_dict())
        if approximated_rules:
            risk_params.setdefault("backtest_rule_approximations", approximated_rules)
        initial_capital = float(card_dict.get("allocated_amount") or 10_000_000)
        report["card_snapshot"] = {
            "strategy_name": card_dict.get("strategy_name"),
            "card_status": card_dict.get("card_status"),
            "is_active": card_dict.get("is_active"),
            "is_live": card_dict.get("is_live"),
            "allocated_amount": initial_capital,
            "max_stocks": card_dict.get("max_stocks"),
            "entry_rule_names": [str((r or {}).get("name") or (r or {}).get("type") or "") for r in entry_rules if isinstance(r, dict)],
            "exit_rule_types": [str((r or {}).get("type") or (r or {}).get("name") or "") for r in exit_rules if isinstance(r, dict)],
            "effective_limit_up_exit_mode": risk_params.get("limit_up_exit_mode"),
            "execution_profile": execution_profile.to_dict(),
            "rule_approximations": approximated_rules,
            "rule_params": summarize_rules(entry_rules, exit_rules),
        }

        event_rows = await fetch_rows(db, """
            SELECT e.trade_date, e.stock_code,
                   COALESCE(NULLIF(su.stock_name, ''), NULLIF(sm.stock_name, ''), e.stock_code) AS stock_name,
                   e.event_type, e.first_25pct_at, e.first_touch_at, e.lock_at,
                   e.high_return_pct, e.closed_locked
            FROM go100_limitup_events e
            LEFT JOIN stock_universe su ON su.stock_code = e.stock_code
            LEFT JOIN v4_stock_master sm ON sm.stock_code = e.stock_code
            WHERE e.trade_date BETWEEN :start_date AND :end_date
              AND COALESCE(e.event_type, '') <> 'invalid_data'
              AND (
                    e.first_25pct_at IS NOT NULL
                 OR e.first_touch_at IS NOT NULL
                 OR e.lock_at IS NOT NULL
                 OR COALESCE(e.high_return_pct, 0) >= 27
              )
            ORDER BY e.trade_date,
                     CASE WHEN e.closed_locked IS TRUE THEN 0 ELSE 1 END,
                     COALESCE(e.lock_at, e.first_touch_at, e.first_25pct_at) NULLS LAST,
                     COALESCE(e.high_return_pct, 0) DESC, e.stock_code
        """, {"start_date": start_date_obj, "end_date": end_date_obj})

        simulator = Go100MinuteSimulator()
        metrics = await simulator.run(
            go100_card_id=CARD_ID,
            universe_filter=universe_filter,
            entry_rules=entry_rules,
            exit_rules=exit_rules,
            risk_params=risk_params,
            start_date=start_date,
            end_date=end_date,
            initial_capital=initial_capital,
            db=db,
            desk_level=execution_profile.desk_level or "DESK2",
        )

    trades = metrics.get("trade_log") or []
    codes = sorted({str(t.get("stock_code") or "") for t in trades if t.get("stock_code")} | {str(e.get("stock_code") or "") for e in event_rows if e.get("stock_code")})
    stock_names: dict[str, str] = {}
    if codes:
        async with AsyncSessionLocal() as db:
            rows = await fetch_rows(db, """
                SELECT code, COALESCE(MAX(NULLIF(stock_name, '')), code) AS stock_name
                FROM (
                    SELECT stock_code AS code, stock_name FROM stock_universe WHERE stock_code = ANY(:codes)
                    UNION ALL
                    SELECT stock_code AS code, stock_name FROM v4_stock_master WHERE stock_code = ANY(:codes)
                ) s
                GROUP BY code
            """, {"codes": codes})
            stock_names = {str(r["code"]): str(r.get("stock_name") or r["code"]) for r in rows}

    audit_sample = metrics.get("decision_audit_sample") or []
    audit_by_code_day: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for rec in audit_sample:
        if not isinstance(rec, dict):
            continue
        code = str(rec.get("stock_code") or "")
        day = str(rec.get("trade_date") or "")[:10]
        if code and day:
            audit_by_code_day[(code, day)].append(rec)

    event_by_code_day = {(str(r.get("stock_code")), str(r.get("trade_date"))[:10]): r for r in event_rows}
    stock_reports = []
    traded_keys = set()
    for idx, tr in enumerate(trades, 1):
        code = str(tr.get("stock_code") or "")
        entry_day = str(tr.get("entry_date") or "")[:10]
        traded_keys.add((code, entry_day))
        recs = audit_by_code_day.get((code, entry_day), [])
        buy = next((r for r in recs if r.get("stage") == "entry" and r.get("decision") == "buy"), {})
        sell = next((r for r in recs if r.get("stage") == "exit" and r.get("decision") == "sell"), {})
        event = event_by_code_day.get((code, entry_day), {})
        name = stock_names.get(code) or str(event.get("stock_name") or "종목명미확인")
        stock_reports.append({
            "seq": idx,
            "stock_label": f"{name}({code})",
            "discovery": {
                "trade_date": entry_day,
                "event_type": event.get("event_type"),
                "first_25pct_at": event.get("first_25pct_at"),
                "first_touch_at": event.get("first_touch_at"),
                "lock_at": event.get("lock_at"),
                "high_return_pct": event.get("high_return_pct"),
                "closed_locked": event.get("closed_locked"),
            },
            "selection": {
                "source": "go100_limitup_events",
                "decision_basis": "point_in_time_intraday_only",
                "buy_audit_reason": buy.get("reason_text"),
                "intraday_metrics": (buy.get("metrics") or {}).get("intraday") or {},
            },
            "entry": {
                "entry_date": tr.get("entry_date"),
                "entry_time": tr.get("entry_time") or (buy.get("metrics") or {}).get("entry_time"),
                "entry_price": tr.get("entry_price"),
                "quantity": tr.get("quantity"),
                "position_size": (buy.get("metrics") or {}).get("position_size"),
            },
            "exit": {
                "exit_date": tr.get("exit_date"),
                "exit_time": tr.get("exit_time") or (sell.get("metrics") or {}).get("exit_time"),
                "exit_price": tr.get("exit_price"),
                "return_pct": tr.get("return_pct"),
                "exit_reason": tr.get("exit_reason"),
                "sell_pct": tr.get("sell_pct"),
                "closed_locked": tr.get("closed_locked"),
                "exit_metrics": tr.get("exit_metrics") or (sell.get("metrics") or {}).get("card119_limitup_guard"),
            },
        })

    nontraded = []
    for ev in event_rows:
        code = str(ev.get("stock_code") or "")
        day = str(ev.get("trade_date") or "")[:10]
        if (code, day) in traded_keys:
            continue
        recs = audit_by_code_day.get((code, day), [])
        skip = next((r for r in reversed(recs) if r.get("stage") == "entry" and r.get("decision") == "skip"), {})
        name = stock_names.get(code) or str(ev.get("stock_name") or "종목명미확인")
        nontraded.append({
            "stock_label": f"{name}({code})",
            "trade_date": day,
            "high_return_pct": ev.get("high_return_pct"),
            "closed_locked": ev.get("closed_locked"),
            "lock_at": ev.get("lock_at"),
            "entry_result": "not_entered",
            "main_blocker": skip.get("reason_text") or "audit sample limit or no per-stock skip captured",
            "metrics": skip.get("metrics") or {},
        })

    stage_counts = Counter()
    reason_counts = Counter()
    for rec in audit_sample:
        if isinstance(rec, dict):
            stage_counts[f"{rec.get('stage')}:{rec.get('decision')}"] += 1
            reason_counts[str(rec.get("reason_code") or "unknown")] += 1

    report["discovery_events"] = event_rows
    report["summary"] = {k: metrics.get(k) for k in [
        "total_return", "annual_return", "max_drawdown", "sharpe_ratio", "win_rate",
        "total_trades", "profit_trades", "loss_trades", "avg_holding_days",
        "gross_return", "timeframe", "desk_level",
    ]}
    report["cost_breakdown"] = metrics.get("cost_breakdown") or {}
    report["card119_exit_attribution"] = metrics.get("card119_exit_attribution")
    report["decision_audit_summary"] = metrics.get("decision_audit_summary")
    report["sample_stage_counts"] = dict(stage_counts)
    report["sample_reason_counts_top"] = dict(reason_counts.most_common(20))
    report["trades"] = trades
    report["stock_reports"] = stock_reports
    report["nontraded_candidates"] = nontraded

    OUT.write_text(json.dumps(_jsonable(report), ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({
        "ok": True,
        "output": str(OUT),
        "period": report["period"],
        "trade_days": report["trade_days"],
        "summary": _jsonable(report["summary"]),
        "candidate_count": len(event_rows),
        "trade_count": len(trades),
        "nontraded_count": len(nontraded),
    }, ensure_ascii=False))


if __name__ == "__main__":
    asyncio.run(main())
