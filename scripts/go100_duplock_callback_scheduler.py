#!/usr/bin/env python3
"""One-shot GO100 duplicate-lock callback scheduler.

This is a fallback worker for cases where the MCP schedule_task bridge is
running in a separate process without an initialized scheduler.
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo


sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

KST = ZoneInfo("Asia/Seoul")


async def _post(session_id: str, title: str, body: str, status: str = "done") -> None:
    if not session_id:
        return
    import asyncpg

    from app.services.session_reporter import post_session_report

    conn = await asyncpg.connect(os.environ["DATABASE_URL"])
    try:
        await post_session_report(
            session_id=session_id,
            title=title,
            body=body[:4000],
            status=status,
            source="go100_duplock_callback_scheduler",
            project="GO100",
            metadata={"schedule_callback": True, "fallback_worker": True},
            intent="auto_report",
            trigger_reaction=True,
            conn=conn,
        )
    finally:
        await conn.close()


async def _run_remote(session_id: str, title: str, command: str) -> None:
    from app.api.ceo_chat_tools import tool_run_remote_command

    try:
        result = await tool_run_remote_command("GO100", command)
        await _post(session_id, title, str(result), "done")
    except Exception as exc:
        await _post(session_id, f"{title} 실패", str(exc), "error")


async def _run_db(session_id: str, title: str, query: str) -> None:
    from app.api.ceo_chat_tools_db import query_project_database

    try:
        result = await query_project_database("GO100", query, limit=50)
        await _post(
            session_id,
            title,
            json.dumps(result, ensure_ascii=False, default=str, indent=2),
            "done" if not result.get("error") else "error",
        )
    except Exception as exc:
        await _post(session_id, f"{title} 실패", str(exc), "error")


def _delay_seconds(target: datetime) -> float:
    return max(1.0, (target - datetime.now(KST)).total_seconds())


async def _sleep_then(name: str, delay: float, coro_factory) -> None:
    await asyncio.sleep(delay)
    await coro_factory()


async def main() -> int:
    session_id = (sys.argv[1] if len(sys.argv) > 1 else os.getenv("AADS_CALLBACK_SESSION_ID", "")).strip()
    now = datetime.now(KST)
    open_check = datetime(2026, 8, 24, 9, 10, tzinfo=KST)
    close_check = datetime(2026, 8, 24, 15, 35, tzinfo=KST)

    duplicate_query = """
SELECT account_id, stock_code, COUNT(*) AS open_count
FROM go100_scalping_positions
WHERE status = 'OPEN'
GROUP BY account_id, stock_code
HAVING COUNT(*) > 1
ORDER BY open_count DESC, account_id, stock_code
LIMIT 20
""".strip()

    jobs = [
        _sleep_then(
            "smoke",
            120,
            lambda: _run_remote(
                session_id,
                "GO100 전역 중복락 콜백 smoke",
                "systemctl status go100-kiwoom-scalping",
            ),
        ),
        _sleep_then(
            "market_open",
            _delay_seconds(open_check),
            lambda: _run_db(session_id, "GO100 장중 OPEN 중복 포지션 점검", duplicate_query),
        ),
        _sleep_then(
            "market_close",
            _delay_seconds(close_check),
            lambda: _run_db(session_id, "GO100 장마감 OPEN 중복 포지션 점검", duplicate_query),
        ),
    ]

    await _post(
        session_id,
        "GO100 전역 중복락 검증 예약 등록",
        "\n".join(
            [
                f"등록시각: {now.strftime('%Y-%m-%d %H:%M:%S KST')}",
                "smoke: 2분 후 go100-kiwoom-scalping 상태 확인",
                f"장중: {open_check.strftime('%Y-%m-%d %H:%M KST')} OPEN 중복 포지션 점검",
                f"장마감: {close_check.strftime('%Y-%m-%d %H:%M KST')} OPEN 중복 포지션 점검",
            ]
        ),
    )
    await asyncio.gather(*jobs)
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
