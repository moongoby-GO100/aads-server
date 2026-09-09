#!/usr/bin/env python3
"""goal_task_links 정합화 CLI (Goal Control P0 — C).

기본은 **dry-run** 이라 아무것도 쓰지 않는다. 실제 수리는 `--apply` 를 붙여야 한다.

사용 예
    # 감사만 (읽기 전용)
    python3 scripts/reconcile_goal_links.py --project AADS

    # 계획 확인 후 수리 (stale + 재시도 supersession)
    python3 scripts/reconcile_goal_links.py --project AADS --apply

    # 프로젝트 불일치 링크의 마일스톤 분리까지 (행은 지우지 않는다)
    python3 scripts/reconcile_goal_links.py --project AADS --apply --detach-misbound
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


async def _main(args: argparse.Namespace) -> int:
    from app.core.db_pool import init_pool, close_pool
    from app.services import goal_link_reconciler

    await init_pool()
    try:
        if args.audit_only:
            result = await goal_link_reconciler.scan(args.project)
        else:
            result = await goal_link_reconciler.reconcile(
                args.project,
                dry_run=not args.apply,
                limit=args.limit,
                detach_misbound=args.detach_misbound,
                mark_orphans=args.mark_orphans,
            )
    finally:
        await close_pool()

    print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="goal_task_links ↔ pipeline_jobs 정합화")
    parser.add_argument("--project", default=None, help="대상 프로젝트 (생략 시 전체)")
    parser.add_argument("--apply", action="store_true", help="실제로 수리한다 (기본은 dry-run)")
    parser.add_argument("--audit-only", action="store_true", help="분류 감사만 수행")
    parser.add_argument("--limit", type=int, default=200, help="한 번에 수리할 최대 링크 수")
    parser.add_argument("--detach-misbound", action="store_true",
                        help="프로젝트 불일치 링크의 milestone_id 를 분리 (행 보존)")
    parser.add_argument("--mark-orphans", action="store_true",
                        help="pipeline_jobs 행이 없는 링크를 orphaned 로 표시 (행 보존)")
    args = parser.parse_args()
    return asyncio.run(_main(args))


if __name__ == "__main__":
    raise SystemExit(main())
