#!/usr/bin/env python3
"""Thinking E2E verification: check thinking_summary storage by model."""
import asyncio
import asyncpg
import os

async def main():
    urls = [
        os.environ.get("DATABASE_URL"),
        "postgresql://aads:aads2026secure@localhost:5433/aads",
        "postgresql://aads:aads2026secure@aads-postgres:5432/aads",
    ]
    last_error = None
    conn = None
    for url in [u for u in urls if u]:
        try:
            conn = await asyncpg.connect(url)
            break
        except Exception as exc:
            last_error = exc
    if conn is None:
        raise RuntimeError(f"DB connection failed: {last_error}") from last_error
    rows = await conn.fetch(
        "SELECT model_used, COUNT(*) as cnt, "
        "SUM(CASE WHEN thinking_summary IS NOT NULL AND thinking_summary != $2 THEN 1 ELSE 0 END)::int as has_thinking, "
        "SUM(CASE WHEN thinking_summary IS NULL OR thinking_summary = $2 THEN 1 ELSE 0 END)::int as no_thinking "
        "FROM chat_messages WHERE role = $1 AND created_at >= '2026-05-06' "
        "GROUP BY model_used ORDER BY cnt DESC LIMIT 15",
        "assistant", ""
    )
    print(f"{'model_used':<35} {'total':>5} {'thinking':>8} {'none':>6}")
    print("-" * 60)
    for r in rows:
        print(f"{str(r['model_used'] or 'NULL'):<35} {r['cnt']:>5} {r['has_thinking']:>8} {r['no_thinking']:>6}")
    await conn.close()

asyncio.run(main())
