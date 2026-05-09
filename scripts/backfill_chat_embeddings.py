"""
Backfill chat_messages.embedding for semantic recall.

Default canary:
    docker exec aads-server python3 /app/scripts/backfill_chat_embeddings.py --limit 100
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from datetime import datetime, timezone
from typing import Any

sys.path.insert(0, "/app")


DEFAULT_DSN = "postgresql://aads:aads@aads-postgres:5432/aads"
VALID_ROLES = {"assistant", "user", "system", "all"}


def _positive_int(value: str) -> int:
    parsed = int(value)
    if parsed < 1:
        raise argparse.ArgumentTypeError("must be >= 1")
    return parsed


def _role_filter(role: str, *, param_index: int) -> tuple[str, list[Any]]:
    if role == "all":
        return "", []
    return f"AND role = ${param_index}", [role]


def _count_where(role: str, min_length: int) -> tuple[str, list[Any]]:
    role_sql, role_params = _role_filter(role, param_index=2)
    return (
        f"""
        embedding IS NULL
        AND content IS NOT NULL
        AND length(content) >= $1
        {role_sql}
        """,
        [min_length, *role_params],
    )


async def count_missing(conn: Any, *, role: str, min_length: int) -> int:
    where_sql, params = _count_where(role, min_length)
    return int(await conn.fetchval(f"SELECT COUNT(*) FROM chat_messages WHERE {where_sql}", *params))


async def fetch_batch(
    conn: Any,
    *,
    role: str,
    min_length: int,
    limit: int,
    order: str,
) -> list[Any]:
    role_sql, role_params = _role_filter(role, param_index=3)
    direction = "ASC" if order == "oldest" else "DESC"
    params: list[Any] = [min_length, limit, *role_params]
    return list(
        await conn.fetch(
            f"""
            SELECT id, role, left(content, 2000) AS content
            FROM chat_messages
            WHERE embedding IS NULL
              AND content IS NOT NULL
              AND length(content) >= $1
              {role_sql}
            ORDER BY created_at {direction}, id {direction}
            LIMIT $2
            """,
            *params,
        )
    )


async def update_batch(pool: Any, rows: list[Any]) -> int:
    from app.services.chat_embedding_service import embed_texts

    if not rows:
        return 0

    embeddings = await embed_texts([row["content"] for row in rows])
    updated = 0
    async with pool.acquire() as conn:
        for row, embedding in zip(rows, embeddings):
            if not embedding:
                continue
            status = await conn.execute(
                """
                UPDATE chat_messages
                SET embedding = $1::vector
                WHERE id = $2 AND embedding IS NULL
                """,
                str(embedding),
                row["id"],
            )
            if status.endswith(" 1"):
                updated += 1
    return updated


async def run_backfill(args: argparse.Namespace) -> dict[str, Any]:
    import asyncpg

    dsn = os.getenv("DATABASE_URL", DEFAULT_DSN)
    pool = await asyncpg.create_pool(dsn, min_size=1, max_size=2)
    started_at = datetime.now(timezone.utc)
    processed = 0
    updated = 0
    batches = 0
    errors: list[str] = []

    try:
        async with pool.acquire() as conn:
            before_missing = await count_missing(conn, role=args.role, min_length=args.min_length)

        if args.dry_run:
            return {
                "status": "dry_run",
                "role": args.role,
                "limit": args.limit,
                "batch_size": args.batch_size,
                "missing_before": before_missing,
                "processed": 0,
                "updated": 0,
                "missing_after": before_missing,
                "errors": [],
            }

        remaining_limit = args.limit
        while remaining_limit > 0:
            batch_limit = min(args.batch_size, remaining_limit)
            async with pool.acquire() as conn:
                rows = await fetch_batch(
                    conn,
                    role=args.role,
                    min_length=args.min_length,
                    limit=batch_limit,
                    order=args.order,
                )
            if not rows:
                break

            try:
                updated += await update_batch(pool, rows)
                processed += len(rows)
                batches += 1
            except Exception as exc:  # keep later batches available for manual retry
                errors.append(str(exc)[:300])
                break

            remaining_limit -= len(rows)
            if args.sleep > 0 and remaining_limit > 0:
                await asyncio.sleep(args.sleep)

        async with pool.acquire() as conn:
            after_missing = await count_missing(conn, role=args.role, min_length=args.min_length)

        return {
            "status": "ok" if not errors else "partial_error",
            "role": args.role,
            "order": args.order,
            "limit": args.limit,
            "batch_size": args.batch_size,
            "missing_before": before_missing,
            "processed": processed,
            "updated": updated,
            "missing_after": after_missing,
            "batches": batches,
            "duration_seconds": round((datetime.now(timezone.utc) - started_at).total_seconds(), 2),
            "errors": errors,
        }
    finally:
        await pool.close()


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Backfill chat_messages.embedding")
    parser.add_argument("--limit", type=_positive_int, default=100)
    parser.add_argument("--batch-size", type=_positive_int, default=20)
    parser.add_argument("--role", choices=sorted(VALID_ROLES), default="assistant")
    parser.add_argument("--min-length", type=_positive_int, default=10)
    parser.add_argument("--order", choices=["newest", "oldest"], default="newest")
    parser.add_argument("--sleep", type=float, default=0.2)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)
    if args.batch_size > args.limit:
        args.batch_size = args.limit
    return args


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv if argv is not None else sys.argv[1:])
    result = asyncio.run(run_backfill(args))
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    return 1 if result.get("status") == "partial_error" else 0


if __name__ == "__main__":
    raise SystemExit(main())
