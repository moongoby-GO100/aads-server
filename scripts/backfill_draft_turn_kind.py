#!/usr/bin/env python3
"""기존 directive_drafts 에 turn_kind / turn_kind_evidence / decisions 를 채운다 (기본은 dry-run).

신규 초안은 create_draft 가 classification 에 turn_kind 를 넣는다. 이 스크립트는
turn_kind 가 아직 없는 행만 대상으로, create_draft 와 같은 규칙(classify_turn)을
**원문 CEO 발언**에 적용한다.

발언 선택 (create_draft 의 _turn_utterance 와 같은 순서):
  1. classification.composer_draft_content (전송 전 입력창 텍스트)
  2. source_message_ids 중 같은 테넌트·세션의 가장 최근 user 메시지
  3. 둘 다 없으면 turn_kind=open, evidence=["no_user_utterance"] — 근거 없는 확정은 만들지 않는다.

classification 의 turn_kind* / decisions 키만 추가한다. goal_id / milestone_id / source_mode
등 기존 키, content, revision, events 는 건드리지 않는다. 목표 연결 정합성은 **보고만** 한다
(자동 수정하지 않는다).

사용 (운영 컨테이너 안):
    python3 scripts/backfill_draft_turn_kind.py            # dry-run (기본, 쓰기 없음)
    python3 scripts/backfill_draft_turn_kind.py --apply    # 실제 반영 — CEO 승인 후에만
"""
from __future__ import annotations

import argparse
import asyncio
import collections
import json
import os
import sys
import uuid
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.services.directive_draft_service import classify_turn  # noqa: E402

CANDIDATES_SQL = """
SELECT id, tenant_id, session_id, source_message_ids, classification
  FROM directive_drafts
 WHERE classification->>'turn_kind' IS NULL
 ORDER BY created_at, id
"""

LAST_USER_MESSAGE_SQL = """
SELECT id, content FROM chat_messages
 WHERE id = ANY($1::uuid[]) AND tenant_id = $2 AND session_id = $3
   AND role = 'user' AND COALESCE(content, '') <> ''
 ORDER BY created_at DESC LIMIT 1
"""

GOAL_SQL = "SELECT tenant_id FROM goals WHERE id = $1"
MILESTONE_SQL = "SELECT goal_id, tenant_id FROM milestones WHERE id = $1"

APPLY_SQL = """
UPDATE directive_drafts SET classification = classification || $3::jsonb
 WHERE id = $1 AND tenant_id = $2 AND classification->>'turn_kind' IS NULL
"""


def _as_dict(value: Any) -> dict[str, Any]:
    if isinstance(value, str):
        value = json.loads(value or "{}")
    return dict(value or {})


async def _goal_check(conn, row: Any, classification: dict[str, Any]) -> str:
    goal_id = classification.get("goal_id")
    if not goal_id:
        return "no_goal"
    owner = await conn.fetchval(GOAL_SQL, uuid.UUID(goal_id))
    if owner is None:
        return "goal_missing"
    if owner != row["tenant_id"]:
        return "goal_other_tenant"
    milestone_id = classification.get("milestone_id")
    if milestone_id:
        milestone = await conn.fetchrow(MILESTONE_SQL, uuid.UUID(milestone_id))
        if not milestone or milestone["goal_id"] != uuid.UUID(goal_id):
            return "milestone_not_in_goal"
    return "goal_ok"


async def plan_backfill(conn) -> list[dict[str, Any]]:
    plans = []
    for row in await conn.fetch(CANDIDATES_SQL):
        classification = _as_dict(row["classification"])
        text, message_id, origin = (classification.get("composer_draft_content") or "").strip(), None, "composer"
        if not text:
            message = await conn.fetchrow(
                LAST_USER_MESSAGE_SQL,
                list(row["source_message_ids"] or []), row["tenant_id"], row["session_id"],
            )
            text, message_id, origin = (
                (message["content"], message["id"], "source_message") if message else ("", None, "none")
            )
        if text:
            result = classify_turn(text, source_message_id=message_id)
        else:
            result = {"turn_kind": "open", "turn_kind_evidence": ["no_user_utterance"], "decisions": []}
        plans.append({
            "draft_id": str(row["id"]),
            "source_mode": classification.get("source_mode"),
            "utterance_origin": origin,
            "goal_check": await _goal_check(conn, row, classification),
            **result,
            "_id": row["id"], "_tenant_id": row["tenant_id"],
        })
    return plans


async def apply_backfill(conn, plans: list[dict[str, Any]]) -> int:
    updated = 0
    async with conn.transaction():
        for plan in plans:
            patch = {k: plan[k] for k in ("turn_kind", "turn_kind_evidence", "decisions")}
            status = await conn.execute(
                APPLY_SQL, plan["_id"], plan["_tenant_id"], json.dumps(patch, ensure_ascii=False)
            )
            updated += int(status.split()[-1])
    return updated


def distribution(plans: list[dict[str, Any]]) -> dict[str, Any]:
    def count(*keys: str) -> dict[str, int]:
        counter = collections.Counter(" / ".join(str(p.get(k)) for k in keys) for p in plans)
        return dict(sorted(counter.items()))

    return {
        "total": len(plans),
        "turn_kind": count("turn_kind"),
        "turn_kind_by_source_mode": count("source_mode", "turn_kind"),
        "utterance_origin": count("utterance_origin"),
        "goal_check": count("goal_check"),
    }


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--apply", action="store_true", help="실제 UPDATE (기본은 dry-run)")
    args = parser.parse_args()

    import asyncpg

    conn = await asyncpg.connect(
        os.environ["DATABASE_URL"],
        server_settings={} if args.apply else {"default_transaction_read_only": "on"},
    )
    try:
        plans = await plan_backfill(conn)
        print(json.dumps(distribution(plans), ensure_ascii=False, indent=2))
        print(f"대상 {len(plans)}행 ({'apply' if args.apply else 'dry-run: 쓰기 없음'})")
        if args.apply:
            print(f"UPDATE 반영 {await apply_backfill(conn, plans)}행")
        return 0
    finally:
        await conn.close()


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
