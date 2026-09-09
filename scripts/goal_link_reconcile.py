#!/usr/bin/env python3
"""goal_task_links 재조정 CLI (기본 dry-run).

사용 예:
    # 계획만 확인 (쓰기 없음)
    python3 scripts/goal_link_reconcile.py --project AADS

    # 결정적 근거가 있는 항목만 제한적으로 적용
    python3 scripts/goal_link_reconcile.py --project AADS --apply --limit 100

    # 근거 미상(과거 암묵 자동연결) 링크까지 회수 — 운영자 명시 승인 필요
    python3 scripts/goal_link_reconcile.py --project AADS --apply --include-unverified

--apply 를 주지 않으면 어떤 행도 쓰지 않는다. 링크 행은 어떤 경우에도 삭제하지 않고
link_state 표시로만 회수하므로, 되돌릴 때는 link_state='active' 로 복구하면 된다.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


async def _main() -> int:
    parser = argparse.ArgumentParser(description="Reconcile goal_task_links against pipeline_jobs")
    parser.add_argument("--project", default=None, help="프로젝트 코드 (미지정 시 전체)")
    parser.add_argument("--apply", action="store_true", help="실제 적용 (미지정 시 dry-run)")
    parser.add_argument("--limit", type=int, default=500, help="적용 상한 (기본 500)")
    parser.add_argument("--scan-limit", type=int, default=2000, help="스캔 상한 (기본 2000)")
    parser.add_argument(
        "--include-unverified",
        action="store_true",
        help="근거 미상 과거 암묵 연결까지 회수 (운영자 명시 승인)",
    )
    args = parser.parse_args()

    from app.core.db_pool import init_pool, close_pool
    from app.services.goal_link_reconciler import reconcile_goal_links

    await init_pool()
    try:
        report = await reconcile_goal_links(
            args.project,
            dry_run=not args.apply,
            limit=args.limit,
            scan_limit=args.scan_limit,
            include_unverified=args.include_unverified,
        )
    finally:
        await close_pool()

    print(json.dumps(report, ensure_ascii=False, indent=2, default=str))
    return 0 if report.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(_main()))
