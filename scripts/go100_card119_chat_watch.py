#!/usr/bin/env python3
"""GO100 #119 live trading watcher that posts measured summaries to a chat.

This script is intentionally small and scheduler-friendly. It runs existing
GO100 diagnostic commands through the AADS remote command helper, summarizes
the measured output, and inserts one assistant message into the bound chat
session. It does not place, cancel, or modify orders.
"""
from __future__ import annotations

import argparse
import asyncio
import ast
import json
import re
import uuid
from datetime import datetime, time
from zoneinfo import ZoneInfo

import asyncpg

from app.api.ceo_chat_tools import tool_run_remote_command
from app.config import settings


KST = ZoneInfo("Asia/Seoul")


def _tail(text: str, limit: int = 4000) -> str:
    text = str(text or "")
    return text[-limit:] if len(text) > limit else text


def _extract_json_summary(text: str) -> dict:
    marker = "── JSON SUMMARY ──"
    if marker not in text:
        return {}
    raw = text.split(marker, 1)[1].strip()
    start = raw.find("{")
    end = raw.rfind("}")
    if start < 0 or end < start:
        return {}
    try:
        return json.loads(raw[start : end + 1])
    except json.JSONDecodeError:
        return {}


def _extract_signal_summary(text: str) -> dict:
    match = re.search(r"\{'candidate_count':.*\}\s*$", text.strip(), re.S)
    if not match:
        return {}
    try:
        parsed = ast.literal_eval(match.group(0))
        return parsed if isinstance(parsed, dict) else {}
    except (SyntaxError, ValueError):
        return {}


def _short_error(text: str) -> str:
    for line in reversed(str(text or "").splitlines()):
        if "RuntimeError:" in line or "Error:" in line or "Traceback" in line:
            return line.strip()[:220]
    return str(text or "").strip().splitlines()[-1][:220] if str(text or "").strip() else ""


async def _run_go100(command: str) -> str:
    try:
        return await tool_run_remote_command("GO100", command)
    except Exception as exc:  # noqa: BLE001 - scheduler report must not crash silently
        return f"[AADS watcher error] {type(exc).__name__}: {exc}"


def _compose_message(
    *,
    run_label: str,
    buyability_raw: str,
    signals_raw: str,
    ready_raw: str,
    entry_window_raw: str,
) -> str:
    now = datetime.now(KST).strftime("%Y-%m-%d %H:%M:%S KST")
    buy = _extract_json_summary(buyability_raw)
    sig = _extract_signal_summary(signals_raw)

    blockers = buy.get("blockers") or []
    warnings = buy.get("warnings") or []
    is_buyable = buy.get("is_buyable")
    candidate_count = sig.get("candidate_count", buy.get("candidate_count", "미확인"))
    signal_count = sig.get("signal_count", "미확인")
    open_positions = buy.get("open_positions", "미확인")
    available_slots = buy.get("available_slots", "미확인")
    market_status = buy.get("market_status", "미확인")
    ohlcv_tickers = buy.get("ohlcv_minute_tickers", "미확인")
    snapshot_tickers = buy.get("snapshot_tickers", "미확인")
    nxt_bars_count = buy.get("nxt_bars_count", "미확인")

    ready_ok = "[GO100 명령 실행 — exit=0]" in ready_raw
    entry_ok = "[GO100 명령 실행 — exit=0]" in entry_window_raw
    hard_status = "✅ 매수 경로 열림" if is_buyable and not blockers else "⚠️ 차단/점검 필요"
    if is_buyable is None:
        hard_status = "⚠️ 매수 가능 판정 미확인"

    ready_note = "통과" if ready_ok else f"실패: {_short_error(ready_raw)}"
    entry_note = "확인" if entry_ok else f"실패: {_short_error(entry_window_raw)}"
    warn_text = ", ".join(map(str, warnings[:4])) if warnings else "없음"
    blocker_text = ", ".join(map(str, blockers[:4])) if blockers else "없음"

    return (
        f"🔄 **#119 상따 실매매 감시 보고 — {run_label}**\n"
        f"- 기준시각: {now}\n"
        f"- 판정: {hard_status}\n\n"
        f"| 항목 | 실측 |\n"
        f"|---|---:|\n"
        f"| 시장상태 | `{market_status}` |\n"
        f"| 후보 수 | `{candidate_count}` |\n"
        f"| 최종 신호 수 | `{signal_count}` |\n"
        f"| 보유 포지션 | `{open_positions}` |\n"
        f"| 매수 가능 슬롯 | `{available_slots}` |\n"
        f"| 분봉 종목 수 | `{ohlcv_tickers}` |\n"
        f"| 스냅샷 종목 수 | `{snapshot_tickers}` |\n"
        f"| NXT 08:00 데이터 | `{nxt_bars_count}` |\n\n"
        f"- 차단 사유: {blocker_text}\n"
        f"- 경고: {warn_text}\n"
        f"- live-ready smoke: {ready_note}\n"
        f"- 진입창 설정: {entry_note}\n\n"
        f"→ 다음 확인: 후보→신호→주문→체결→손익 중 어느 단계에서 막히는지 계속 추적합니다."
    )


async def _insert_chat_message(session_id: str, content: str) -> None:
    dsn = settings.DATABASE_URL.replace("postgresql://", "postgres://")
    conn = await asyncpg.connect(dsn, timeout=10)
    try:
        await conn.execute(
            """
            INSERT INTO chat_messages (
                id, session_id, role, content, model_used, intent, cost,
                tokens_in, tokens_out, bookmarked, attachments, sources,
                tools_called, is_compacted
            )
            VALUES (
                $1, $2::uuid, 'assistant', $3, 'go100-card119-watch', 'scheduled_watch', 0,
                0, 0, false, '[]'::jsonb, '[]'::jsonb,
                '["run_remote_command:GO100"]'::jsonb, false
            )
            """,
            uuid.uuid4(),
            session_id,
            content,
        )
    finally:
        await conn.close()


async def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--session-id", required=True)
    parser.add_argument("--label", default="scheduled")
    parser.add_argument("--chat", action="store_true")
    parser.add_argument("--start", default="")
    parser.add_argument("--end", default="")
    args = parser.parse_args()

    now_dt = datetime.now(KST)
    if args.start and args.end:
        start_t = time.fromisoformat(args.start)
        end_t = time.fromisoformat(args.end)
        if not (start_t <= now_dt.time().replace(tzinfo=None) <= end_t):
            msg = (
                f"#119 watcher skipped: now={now_dt.strftime('%Y-%m-%d %H:%M:%S KST')} "
                f"window={args.start}~{args.end}"
            )
            print(msg)
            return 0

    buyability_raw, signals_raw, ready_raw, entry_window_raw = await asyncio.gather(
        _run_go100("python3 backend/scripts/go100_diagnose_card119_buyability.py"),
        _run_go100("python3 backend/scripts/go100_check_card119_signals.py"),
        _run_go100("python3 backend/scripts/go100_smoke_card119_live_ready.py"),
        _run_go100("python3 backend/scripts/go100_verify_card119_entry_window_state.py"),
    )

    content = _compose_message(
        run_label=args.label,
        buyability_raw=buyability_raw,
        signals_raw=signals_raw,
        ready_raw=ready_raw,
        entry_window_raw=entry_window_raw,
    )
    if args.chat:
        await _insert_chat_message(args.session_id, content)
    print(content)
    print("\n--- raw tail: buyability ---")
    print(_tail(buyability_raw, 1200))
    print("\n--- raw tail: signals ---")
    print(_tail(signals_raw, 800))
    print("\n--- raw tail: live_ready ---")
    print(_tail(ready_raw, 800))
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
