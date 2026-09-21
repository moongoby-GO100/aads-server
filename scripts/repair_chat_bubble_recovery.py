#!/usr/bin/env python3
"""Repair only the two reported sessions; default is a non-mutating preview.

An exclusive snapshot file is required before --apply. Running executions,
valid leases, response text, model choices and user messages are preserved.
"""
import argparse
import asyncio
import json
import os
from pathlib import Path
from uuid import UUID

import asyncpg


SESSIONS = [UUID(value) for value in (
    "7fb5f50a-9fe7-4a29-9333-9c023125aa6e",
    "15782f6e-35ca-475b-ac45-c152c26a42fa",
)]
MISSING_BUBBLE_EXECUTIONS = [UUID(value) for value in (
    "18e67cc5-d1f1-45e4-a262-fda0b379c2b6",
    "1eef6586-261f-4f6e-bfc5-0660037dd694",
    "d9eb0fad-8bf3-4102-8a2e-3b918ac74455",
    "b25ac21a-3347-47ed-8861-7519eceecaf0",
)]
# Original timeout timestamps from the exclusive before-repair snapshot. Old
# resume attempts subsequently rewrote completed_at; do not move notices into
# the middle of newer conversations when recreating a deleted repair notice.
ORIGINAL_TIMEOUT_AT = {
    "18e67cc5-d1f1-45e4-a262-fda0b379c2b6": "2026-09-21T09:47:55.396525+00:00",
    "1eef6586-261f-4f6e-bfc5-0660037dd694": "2026-09-21T09:57:17.898489+00:00",
    "b25ac21a-3347-47ed-8861-7519eceecaf0": "2026-09-21T09:59:44.263192+00:00",
    "d9eb0fad-8bf3-4102-8a2e-3b918ac74455": "2026-09-21T09:56:05.404730+00:00",
}


async def main(args):
    conn = await asyncpg.connect(os.environ["DATABASE_URL"], host=os.getenv("AADS_BUBBLE_DB_HOST"))
    try:
        async with conn.transaction():
            await conn.execute("SET LOCAL lock_timeout = '5s'")
            sessions = await conn.fetch(
                "SELECT id,current_execution_id,message_count,updated_at FROM chat_sessions WHERE id=ANY($1::uuid[]) ORDER BY id FOR UPDATE",
                SESSIONS,
            )
            executions = await conn.fetch("""
                SELECT id,session_id,status,assistant_message_id,owner_epoch,owner_instance,
                       lease_expires_at,error_message,completed_at,created_at,updated_at
                FROM chat_turn_executions
                WHERE session_id=ANY($1::uuid[])
                  AND status IN ('interrupted','cancelled')
                  AND (lease_expires_at IS NULL OR lease_expires_at<=NOW())
                  AND (id=ANY($2::uuid[]) OR error_message LIKE '%superseded%')
                ORDER BY id FOR UPDATE
            """, SESSIONS, MISSING_BUBBLE_EXECUTIONS)
            ids = [row["id"] for row in executions]
            messages = await conn.fetch("""
                SELECT id,execution_id,intent,model_used,is_hidden,edited_at
                FROM chat_messages WHERE execution_id=ANY($1::uuid[])
            """, ids)
            snapshot = {"sessions": [dict(row) for row in sessions],
                        "executions": [dict(row) for row in executions],
                        "messages": [dict(row) for row in messages]}
            preview = {"apply": args.apply, "terminal_executions": len(ids),
                       "missing_bubbles": sum(row["id"] in MISSING_BUBBLE_EXECUTIONS and not row["assistant_message_id"] for row in executions),
                       "stale_placeholders": sum(row["intent"] == "streaming_placeholder" for row in messages)}
            if not args.apply:
                print(json.dumps(preview))
                return
            if not args.snapshot:
                raise ValueError("--snapshot is required for --apply")
            # Refuse overwriting an earlier backup. No SQL writes precede this.
            with Path(args.snapshot).open("x", encoding="utf-8") as handle:
                os.chmod(args.snapshot, 0o600)
                json.dump(snapshot, handle, ensure_ascii=False, indent=2, default=str)
                handle.flush()
                os.fsync(handle.fileno())
            archived = await conn.execute("""
                UPDATE chat_messages m
                SET intent='_archived_partial',model_used='interrupted',is_hidden=TRUE,edited_at=NOW()
                FROM chat_turn_executions te
                WHERE te.id=ANY($1::uuid[]) AND m.execution_id=te.id
                  AND te.status IN ('interrupted','cancelled')
                  AND te.error_message LIKE '%superseded%'
                  AND m.intent='streaming_placeholder'
            """, ids)
            cleared = await conn.execute("""
                UPDATE chat_sessions s SET current_execution_id=NULL,updated_at=NOW()
                FROM chat_turn_executions te
                WHERE te.id=ANY($1::uuid[]) AND s.current_execution_id=te.id
                  AND te.status IN ('interrupted','cancelled')
            """, ids)
            inserted = await conn.fetch("""
                WITH inserted AS (
                    INSERT INTO chat_messages
                      (session_id,execution_id,role,content,model_used,intent,is_hidden,tools_called,quality_details,created_at)
                    SELECT te.session_id,te.id,'assistant',
                      '첫 응답 대기 시간이 초과되어 이 요청의 응답이 중단되었습니다. 오류 정리 중 사라졌던 안내를 복구했습니다. 이후 대화는 그대로 유지됩니다.',
                      'interrupted','interruption_notice',FALSE,'[]'::jsonb,
                      jsonb_build_object('recovery_repair','20260921_chat_bubble','interruption_reason',te.error_message),
                      COALESCE(($3::jsonb->>te.id::text)::timestamptz,te.completed_at,te.created_at)
                    FROM chat_turn_executions te
                    WHERE te.id=ANY($1::uuid[]) AND te.id=ANY($2::uuid[])
                      AND te.status='interrupted' AND te.assistant_message_id IS NULL
                      AND (te.error_message LIKE '%llm_first_response_timeout%'
                           OR te.error_message LIKE '%superseded%')
                      AND NOT EXISTS (SELECT 1 FROM chat_messages m WHERE m.execution_id=te.id AND m.role='assistant')
                    RETURNING id,execution_id,session_id
                ), linked AS (
                    UPDATE chat_turn_executions te SET assistant_message_id=i.id,updated_at=NOW()
                    FROM inserted i WHERE te.id=i.execution_id RETURNING te.id
                ), counted AS (
                    UPDATE chat_sessions s SET message_count=s.message_count+i.n,updated_at=NOW()
                    FROM (SELECT session_id,COUNT(*)::int n FROM inserted GROUP BY session_id) i
                    WHERE s.id=i.session_id RETURNING s.id
                ) SELECT id,execution_id,session_id FROM inserted
            """, MISSING_BUBBLE_EXECUTIONS, ids, json.dumps(ORIGINAL_TIMEOUT_AT))
            # An old API may have resumed and archived our repair notice while
            # the fixed release was waiting for drain. Restore only notices
            # created by this repair, only on the locked terminal executions.
            # Keep the content/diagnostics and terminal execution state intact.
            restored = await conn.execute("""
                UPDATE chat_messages m
                SET intent='interruption_notice',
                    quality_details=COALESCE(m.quality_details,'{}'::jsonb)
                        || jsonb_build_object('recovery_notice_restored_at',NOW())
                FROM chat_turn_executions te
                WHERE te.id=m.execution_id AND te.id=ANY($1::uuid[])
                  AND te.id=ANY($2::uuid[])
                  AND te.status IN ('interrupted','cancelled')
                  AND te.assistant_message_id=m.id
                  AND m.quality_details->>'recovery_repair'='20260921_chat_bubble'
                  AND m.intent='_archived_partial'
            """, MISSING_BUBBLE_EXECUTIONS, ids)
            # The historical BEFORE trigger hides interruption_notice on
            # INSERT/content updates. Match _mark_execution_interrupted's final
            # visibility write: changing only is_hidden does not retrigger it.
            visible = await conn.execute("""
                UPDATE chat_messages SET is_hidden=FALSE
                WHERE execution_id=ANY($1::uuid[]) AND execution_id=ANY($2::uuid[])
                  AND quality_details->>'recovery_repair'='20260921_chat_bubble'
                  AND intent='interruption_notice' AND is_hidden=TRUE
            """, MISSING_BUBBLE_EXECUTIONS, ids)
            result = {**preview, "archived": archived, "cleared_pointers": cleared,
                      "inserted": [dict(row) for row in inserted], "restored_notices": restored, "made_visible": visible,
                      "snapshot": str(args.snapshot)}
        print(json.dumps(result, default=str))
    finally:
        await conn.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--snapshot", type=Path)
    asyncio.run(main(parser.parse_args()))
