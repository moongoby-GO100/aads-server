"""
Promote completed runner jobs into strategic memory_facts.

Default canary:
    docker exec aads-server python3 /app/scripts/promote_project_changes.py --project AADS --limit 10 --dry-run
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from typing import Any

sys.path.insert(0, "/app")

DEFAULT_DSN = "postgresql://aads:aads@aads-postgres:5432/aads"


def _positive_int(value: str) -> int:
    parsed = int(value)
    if parsed < 1:
        raise argparse.ArgumentTypeError("must be >= 1")
    return parsed


async def run(args: argparse.Namespace) -> dict[str, Any]:
    import asyncpg
    from app.services.project_change_promoter import promote_completed_project_changes

    dsn = os.getenv("DATABASE_URL", DEFAULT_DSN)
    pool = await asyncpg.create_pool(dsn, min_size=1, max_size=2)
    try:
        return await promote_completed_project_changes(
            pool,
            project=args.project,
            days=args.days,
            limit=args.limit,
            dry_run=args.dry_run,
            embed=not args.no_embed,
        )
    finally:
        await pool.close()


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Promote important project changes into memory_facts")
    parser.add_argument("--project", default=None, help="Optional project filter, e.g. AADS/KIS/GO100/SF/NTV2")
    parser.add_argument("--days", type=_positive_int, default=14)
    parser.add_argument("--limit", type=_positive_int, default=20)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--no-embed", action="store_true", help="Skip embedding updates for faster canary runs")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv if argv is not None else sys.argv[1:])
    result = asyncio.run(run(args))
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    return 1 if result.get("status") == "partial_error" else 0


if __name__ == "__main__":
    raise SystemExit(main())
