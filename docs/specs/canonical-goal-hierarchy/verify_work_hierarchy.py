#!/usr/bin/env python3
"""업무계층 등록 후 읽기 전용 SELECT 검증. aads-server 컨테이너에서 stdin 으로 실행한다.

환경변수 CREATED_IDS_JSON(결과 JSON 의 created_ids 객체)을 주면 DB 90건 ID 와 독립 대조한다.
쓰기 SQL 은 없다(SELECT 만).
"""
import asyncio
import hashlib
import json
import os

from app.core.db_pool import get_pool, init_pool

GOAL = "ac431290-66be-4d00-8bbb-9bf162917240"
RATIFICATION_ENTRY_KEY = "decision:canonical-work-hierarchy-90-ratification-20261014"
APPROVAL_MESSAGE_ID = "61410ce2-1830-40d1-a8c3-eb1b8244acd1"


async def main() -> None:
    await init_pool()
    out: dict = {}
    async with get_pool().acquire() as c:
        g = await c.fetchrow("SELECT tenant_id::text, project, status, progress FROM goals WHERE id=$1::uuid", GOAL)
        out["goal"] = {k: str(v) for k, v in dict(g).items()}
        out["by_type_status"] = [dict(r) for r in await c.fetch(
            "SELECT type, status, count(*)::int n FROM work_items WHERE goal_id=$1::uuid GROUP BY 1,2 ORDER BY 1", GOAL)]
        out["total"] = await c.fetchval("SELECT count(*) FROM work_items WHERE goal_id=$1::uuid", GOAL)
        out["wrong_parent_type"] = await c.fetchval(
            """SELECT count(*) FROM work_items w JOIN work_items p ON p.id=w.parent_id
                WHERE w.goal_id=$1::uuid AND NOT ((w.type='story' AND p.type='epic') OR (w.type='task' AND p.type='story'))""", GOAL)
        out["orphan"] = await c.fetchval(
            """SELECT count(*) FROM work_items w WHERE w.goal_id=$1::uuid
                AND ((w.type='epic' AND w.parent_id IS NOT NULL)
                  OR (w.type<>'epic' AND (w.parent_id IS NULL OR NOT EXISTS (SELECT 1 FROM work_items p WHERE p.id=w.parent_id))))""", GOAL)
        out["dup_idempotency_key"] = await c.fetchval(
            "SELECT count(*) FROM (SELECT idempotency_key FROM work_items WHERE goal_id=$1::uuid GROUP BY 1 HAVING count(*)>1) x", GOAL)
        out["cross_scope"] = await c.fetchval(
            """SELECT count(*) FROM work_items w JOIN goals g ON g.id=w.goal_id
                LEFT JOIN work_items p ON p.id=w.parent_id
                LEFT JOIN milestones m ON m.id=w.milestone_id
               WHERE w.goal_id=$1::uuid
                 AND (w.tenant_id<>g.tenant_id OR w.project<>g.project OR m.goal_id IS DISTINCT FROM w.goal_id
                      OR (p.id IS NOT NULL AND (p.tenant_id<>w.tenant_id OR p.project<>w.project
                          OR p.goal_id<>w.goal_id OR p.milestone_id IS DISTINCT FROM w.milestone_id)))""", GOAL)
        out["assignment_id_set"] = await c.fetchval(
            "SELECT count(*) FROM work_items WHERE goal_id=$1::uuid AND assignment_id IS NOT NULL", GOAL)
        out["created_by"] = [dict(r) for r in await c.fetch(
            "SELECT created_by, count(*)::int n FROM work_items WHERE goal_id=$1::uuid GROUP BY 1", GOAL)]
        out["per_milestone"] = [dict(r) for r in await c.fetch(
            """SELECT m.sequence_order AS m, m.status AS milestone_status,
                      count(*) FILTER (WHERE w.type='epic')::int e, count(*) FILTER (WHERE w.type='story')::int s,
                      count(*) FILTER (WHERE w.type='task')::int t
                 FROM milestones m LEFT JOIN work_items w ON w.milestone_id=m.id
                WHERE m.goal_id=$1::uuid GROUP BY 1,2 ORDER BY 1""", GOAL)]
        ids = sorted(str(r["id"]) for r in await c.fetch("SELECT id FROM work_items WHERE goal_id=$1::uuid", GOAL))
        out["ids_count"] = len(ids)
        out["ids_sorted_md5"] = hashlib.md5(",".join(ids).encode()).hexdigest()
        out["unique_key_count"] = await c.fetchval(
            "SELECT count(DISTINCT idempotency_key) FROM work_items WHERE goal_id=$1::uuid", GOAL)
        out["created_at_range"] = [str(v) for v in await c.fetchrow(
            "SELECT min(created_at), max(created_at) FROM work_items WHERE goal_id=$1::uuid", GOAL)]
        dec = await c.fetchrow(
            "SELECT id::text, tenant_id::text, project_key, entry_type, status, revision, source_session_id, metadata,"
            " created_at FROM project_handover_entries WHERE entry_key=$1", RATIFICATION_ENTRY_KEY)
        if dec is not None:
            meta = dec["metadata"]
            meta = json.loads(meta) if isinstance(meta, str) else dict(meta or {})
            dec_ids = sorted(meta.get("ratified_existing_work_item_ids", []))
            out["decision"] = {
                "id": dec["id"], "tenant_id": dec["tenant_id"], "project_key": dec["project_key"],
                "entry_type": dec["entry_type"], "status": dec["status"], "revision": dec["revision"],
                "source_session_id": dec["source_session_id"], "created_at": str(dec["created_at"]),
                "meta_goal_id": meta.get("goal_id"), "meta_tenant_id": meta.get("tenant_id"),
                "meta_count": meta.get("count"), "meta_ids_md5": meta.get("ids_md5"),
                "approval_message_id": meta.get("approval_message_id"),
                "actual_executor": meta.get("actual_executor"), "originating_runner": meta.get("originating_runner"),
                "decision_ids_count": len(dec_ids),
                "decision_ids_equal_db_ids": dec_ids == ids,
                "decision_ids_md5_equal_db_ids_md5": meta.get("ids_md5") == out["ids_sorted_md5"],
                "tenant_matches_goal": dec["tenant_id"] == out["goal"]["tenant_id"],
                "project_matches_goal": dec["project_key"] == out["goal"]["project"],
                "goal_matches": meta.get("goal_id") == GOAL,
            }
        else:
            out["decision"] = None
        msg = await c.fetchrow(
            "SELECT id::text, session_id::text, role, created_at FROM chat_messages WHERE id=$1::uuid", APPROVAL_MESSAGE_ID)
        out["approval_message"] = None if msg is None else {
            "id": msg["id"], "session_id": msg["session_id"], "role": msg["role"], "created_at": str(msg["created_at"])}
        raw = os.environ.get("CREATED_IDS_JSON", "").strip()
        if raw:
            result_ids = sorted(json.loads(raw).values())
            out["result_json_ids_count"] = len(result_ids)
            out["result_json_ids_md5"] = hashlib.md5(",".join(result_ids).encode()).hexdigest()
            out["result_json_ids_equal_db_ids"] = result_ids == ids
    print(json.dumps(out, ensure_ascii=False, default=str, indent=1))


asyncio.run(main())
