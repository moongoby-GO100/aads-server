#!/usr/bin/env python3
"""Sync current git dirty files into chat_workspace_change_ledger.

Use this before commit/push/deploy automation so DB ownership reflects the
real worktree instead of stale chat-direct ledger rows.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.core.db_pool import close_pool, init_pool
from app.services.workspace_change_tracker import sync_git_dirty_snapshot


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Sync git dirty files to the AADS change ledger.")
    parser.add_argument("--session-id", required=True)
    parser.add_argument("--project", default="AADS")
    parser.add_argument("--repo", choices=["aads-server", "aads-dashboard"])
    parser.add_argument("--owner", default="")
    parser.add_argument("--task-id", default="")
    parser.add_argument(
        "--claim-path",
        action="append",
        default=[],
        help="Dirty path owned by this task. Repeatable. Non-claimed dirty files are marked UNKNOWN-PREEXISTING-DIRTY.",
    )
    parser.add_argument(
        "--allow-empty-task-id",
        action="store_true",
        help="Permit a manual diagnostic snapshot without task attribution.",
    )
    parser.add_argument("--no-mark-stale-clean", action="store_true")
    return parser.parse_args()


async def main() -> int:
    args = parse_args()
    if not args.task_id and not args.allow_empty_task_id:
        print(
            json.dumps(
                {
                    "ok": False,
                    "error": "task_id_required",
                    "detail": "Pass --task-id for commit/push/deploy automation, or --allow-empty-task-id for diagnostics.",
                },
                ensure_ascii=False,
                indent=2,
            ),
            file=sys.stderr,
        )
        return 2
    await init_pool()
    try:
        result = await sync_git_dirty_snapshot(
            session_id=args.session_id,
            project=args.project,
            repo=args.repo,
            owner=args.owner or None,
            task_id=args.task_id or None,
            claim_paths=args.claim_path,
            mark_stale_clean=not args.no_mark_stale_clean,
        )
        print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
        return 0 if result.get("ok") else 1
    finally:
        await close_pool()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
