"""실제 PostgreSQL 에서 두 독립 연결의 결정적 인터리빙으로 완료 판정 원자성을 검증한다.

임시 DB 전용이다. `AADS_TEST_PG_DSN` 이 없거나 호스트가 로컬이 아니면 건너뛴다(운영 DB 쓰기 금지).
"""
import asyncio
import json
import os
import uuid
from urllib.parse import urlparse

import pytest

from app.services import milestone_checklist as mc
from app.services.goal_manager import GoalStateMachine

DSN = os.environ.get("AADS_TEST_PG_DSN", "")
_HOST = urlparse(DSN).hostname if DSN else None
pytestmark = pytest.mark.skipif(
    not DSN or _HOST not in ("localhost", "127.0.0.1", "::1"),
    reason="AADS_TEST_PG_DSN(로컬 임시 PostgreSQL) 없음",
)

asyncpg = pytest.importorskip("asyncpg")

NOW = "2026-10-20T10:00:00+00:00"
_DDL = """
CREATE TABLE milestones (
    id uuid PRIMARY KEY, goal_id uuid NOT NULL, tenant_id uuid NOT NULL,
    status text NOT NULL, version bigint NOT NULL DEFAULT 1,
    completed_at timestamptz, review_note text, updated_at timestamptz DEFAULT NOW(),
    completion_checklist jsonb
)
"""
_CHECKLIST = [{"step": "4.1", "owner": "PM", "status": "done",
               "evidence": "docs/reports/판정표.md 308행 대조 완료"}]


async def _seed(schema):
    admin = await asyncpg.connect(DSN)
    await admin.execute(f'CREATE SCHEMA "{schema}"')
    await admin.close()
    ids = {"ms": str(uuid.uuid4()), "goal": str(uuid.uuid4()), "tenant": str(uuid.uuid4())}
    conn = await asyncpg.connect(DSN, server_settings={"search_path": schema})
    await conn.execute(_DDL)
    await conn.execute(
        "INSERT INTO milestones (id, goal_id, tenant_id, status, version, completion_checklist) "
        "VALUES ($1::uuid, $2::uuid, $3::uuid, 'in_progress', 16, $4::jsonb)",
        ids["ms"], ids["goal"], ids["tenant"], json.dumps(_CHECKLIST, ensure_ascii=False),
    )
    await conn.close()
    return ids


async def _connect(schema):
    return await asyncpg.connect(DSN, server_settings={"search_path": schema})


async def _drop(schema):
    admin = await asyncpg.connect(DSN)
    await admin.execute(f'DROP SCHEMA "{schema}" CASCADE')
    await admin.close()


def _revert(conn, ids, **overrides):
    kwargs = dict(
        goal_id=ids["goal"], milestone_id=ids["ms"], tenant_id=ids["tenant"], step="4.1",
        status="pending", evidence=None, expected_version=16, request_id="concurrent-review-1",
        actor="user-pm", actor_is_approver=False, now_iso=NOW,
    )
    kwargs.update(overrides)
    return mc.update_checklist_item(conn, **kwargs)


def _run(scenario):
    schema = f"t_{uuid.uuid4().hex[:12]}"

    async def main():
        ids = await _seed(schema)
        try:
            await scenario(schema, ids)
        finally:
            await _drop(schema)

    asyncio.run(asyncio.wait_for(main(), timeout=60))


def test_revert_committed_before_completion_lock_blocks_completion():
    """되돌림이 행잠금을 쥐고 있으면 완료 판정은 대기하다 되돌려진 체크리스트를 보고 미충족이 된다."""
    async def scenario(schema, ids):
        reverter, completer = await _connect(schema), await _connect(schema)
        release = asyncio.Event()
        locked = asyncio.Event()

        class Gated:
            """되돌림 트랜잭션이 FOR UPDATE 후 UPDATE 직전에 멈추게 한다."""
            def __getattr__(self, name):
                return getattr(reverter, name)

            async def fetchrow(self, *a, **k):
                row = await reverter.fetchrow(*a, **k)
                locked.set()
                await release.wait()
                return row

        revert_task = asyncio.create_task(_revert(Gated(), ids))
        await asyncio.wait_for(locked.wait(), 10)
        complete_task = asyncio.create_task(
            GoalStateMachine._complete_milestone_if_checklist_met(completer, ids["ms"])
        )
        await asyncio.sleep(0.5)
        assert not complete_task.done(), "완료 판정이 행잠금을 기다리지 않고 진행했다"
        release.set()
        reverted = await revert_task
        outcome = await asyncio.wait_for(complete_task, 10)
        assert reverted["changed"] and reverted["version"] == 17
        assert outcome["result"] == "unmet"
        row = await completer.fetchrow("SELECT status, version FROM milestones")
        assert row["status"] == "in_progress" and row["version"] == 17
        await reverter.close()
        await completer.close()

    _run(scenario)


def test_completion_holding_lock_makes_concurrent_revert_wait_then_fail_cas():
    async def scenario(schema, ids):
        reverter, completer = await _connect(schema), await _connect(schema)
        locked = asyncio.Event()
        release = asyncio.Event()

        class Gated:
            """완료 판정이 FOR UPDATE 를 잡은 직후 UPDATE 전에 멈추게 한다."""
            def __getattr__(self, name):
                return getattr(completer, name)

            async def fetchrow(self, *a, **k):
                row = await completer.fetchrow(*a, **k)
                locked.set()
                await release.wait()
                return row

        complete_task = asyncio.create_task(
            GoalStateMachine._complete_milestone_if_checklist_met(Gated(), ids["ms"])
        )
        await asyncio.wait_for(locked.wait(), 10)
        revert_task = asyncio.create_task(_revert(reverter, ids))
        await asyncio.sleep(0.5)
        assert not revert_task.done(), "되돌림이 완료 판정의 행잠금을 기다리지 않았다"
        release.set()
        outcome = await asyncio.wait_for(complete_task, 10)
        assert outcome["result"] == "completed"
        with pytest.raises(mc.ChecklistError) as exc:
            await asyncio.wait_for(revert_task, 10)
        assert exc.value.code == "version_conflict" and exc.value.extra["current_version"] == 17
        row = await reverter.fetchrow("SELECT status, version, completion_checklist FROM milestones")
        assert row["status"] == "completed" and row["version"] == 17
        assert json.loads(row["completion_checklist"])[0]["status"] == "done"
        await reverter.close()
        await completer.close()

    _run(scenario)


def test_two_concurrent_completions_complete_exactly_once():
    async def scenario(schema, ids):
        a, b = await _connect(schema), await _connect(schema)
        outcomes = await asyncio.gather(
            GoalStateMachine._complete_milestone_if_checklist_met(a, ids["ms"]),
            GoalStateMachine._complete_milestone_if_checklist_met(b, ids["ms"]),
        )
        assert sorted(o["result"] for o in outcomes) == ["already_completed", "completed"]
        row = await a.fetchrow("SELECT status, version FROM milestones")
        assert row["status"] == "completed" and row["version"] == 17
        again = await GoalStateMachine._complete_milestone_if_checklist_met(a, ids["ms"])
        assert again["result"] == "already_completed"
        await a.close()
        await b.close()

    _run(scenario)


def test_approval_revocation_by_member_is_rejected_and_row_unchanged():
    async def scenario(schema, ids):
        conn = await _connect(schema)
        approved = [{"step": "4.3", "owner": "CEO", "status": "done",
                     "evidence": "existing approval evidence", "approved_by": "original-ceo",
                     "approved_at": "2026-10-19T01:00:00Z"}]
        await conn.execute("UPDATE milestones SET completion_checklist = $1::jsonb",
                           json.dumps(approved))
        with pytest.raises(mc.ChecklistError) as exc:
            await _revert(conn, ids, step="4.3")
        assert exc.value.status_code == 403
        row = await conn.fetchrow("SELECT version, completion_checklist FROM milestones")
        assert row["version"] == 16 and json.loads(row["completion_checklist"]) == approved
        done = await _revert(conn, ids, step="4.3", actor="ceo-1", actor_is_approver=True,
                             evidence="승인 철회: 증빙 재검토 필요", request_id="revoke-0001")
        assert done["changed"] and done["version"] == 17
        row = await conn.fetchrow("SELECT completion_checklist FROM milestones")
        entry = json.loads(row["completion_checklist"])[0]["history"][-1]
        assert entry["prev_approved_by"] == "original-ceo"
        assert entry["prev_approved_at"] == "2026-10-19T01:00:00Z"
        await conn.close()

    _run(scenario)
