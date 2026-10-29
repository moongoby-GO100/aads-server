"""M7 draft concurrency and binding regressions on a disposable PostgreSQL database.

Only generation is replaced; source reads, locks, revisions and artifacts use PostgreSQL.
"""
from __future__ import annotations

import asyncio
import errno
import os
import uuid
from pathlib import Path
from urllib.parse import unquote, urlparse

import asyncpg
import pytest

from app.services import directive_draft_service as service

ROOT = Path(__file__).parents[2]


async def exercise(monkeypatch, scenario):
    url = os.environ.get("AADS_DRAFT_TEST_DATABASE_URL")
    if not url:
        pytest.skip("AADS_DRAFT_TEST_DATABASE_URL is unset; real DB execution incomplete")
    if not unquote(urlparse(url).path).removeprefix("/").endswith("_test"):
        pytest.fail("AADS_DRAFT_TEST_DATABASE_URL must name a disposable *_test database")
    try:
        conn = await asyncpg.connect(url, timeout=5)
    except OSError as exc:
        if exc.errno == errno.EPERM:
            pytest.skip("EPERM: PostgreSQL access blocked; real DB execution incomplete")
        raise
    schema = f"draft_binding_test_{uuid.uuid4().hex}"
    pool = None
    try:
        await conn.execute(f"CREATE SCHEMA {schema}")
        await conn.execute(f"SET search_path TO {schema}")
        await conn.execute("""
            CREATE TABLE tenants (id uuid PRIMARY KEY);
            CREATE TABLE chat_workspaces (id uuid PRIMARY KEY, name text, project_key text);
            CREATE TABLE chat_sessions (id uuid PRIMARY KEY, tenant_id uuid, workspace_id uuid, title text);
            CREATE TABLE chat_messages (id uuid PRIMARY KEY, tenant_id uuid, session_id uuid,
                role text, content text, created_at timestamptz DEFAULT now());
            CREATE TABLE chat_artifacts (id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
                tenant_id uuid, session_id uuid, workspace_id uuid, type text, title text,
                content text, metadata jsonb DEFAULT '{}', updated_at timestamptz DEFAULT now());
            CREATE TABLE goals (id uuid PRIMARY KEY, tenant_id uuid,
                owner_role_key text NOT NULL, owner_session_id uuid NOT NULL,
                CONSTRAINT ck_goals_owner_required CHECK (
                    owner_role_key <> '' AND owner_session_id IS NOT NULL));
            CREATE TABLE milestones (id uuid PRIMARY KEY, goal_id uuid, tenant_id uuid);
            CREATE TABLE goal_task_links (goal_id uuid, task_type text, task_id text);
        """)
        for filename in ("172_directive_draft_copilot.sql", "20261022_directive_draft_turn_dedup.sql"):
            await conn.execute((ROOT / "migrations" / filename).read_text())
        # Match the model attribution column required by the current service.
        await conn.execute("ALTER TABLE directive_drafts ADD COLUMN model_used text")
        pool = await asyncpg.create_pool(url, min_size=2, max_size=4,
                                        server_settings={"search_path": schema})
        monkeypatch.setattr(service, "get_pool", lambda: pool)
        tenant, foreign, workspace, session, other_session = [uuid.uuid4() for _ in range(5)]
        goal, other_goal, foreign_goal, milestone, other_milestone = [uuid.uuid4() for _ in range(5)]
        await conn.executemany("INSERT INTO tenants VALUES($1)", [(tenant,), (foreign,)])
        await conn.execute("INSERT INTO chat_workspaces VALUES($1,'테스트','AADS')", workspace)
        await conn.executemany("INSERT INTO chat_sessions VALUES($1,$2,$3,'테스트')",
                              [(session, tenant, workspace), (other_session, tenant, workspace)])
        await conn.executemany("INSERT INTO goals VALUES($1,$2,'ceo',$3)",
                              [(goal, tenant, session), (other_goal, tenant, session),
                               (foreign_goal, foreign, other_session)])
        await conn.executemany("INSERT INTO milestones VALUES($1,$2,$3)",
                              [(milestone, goal, tenant), (other_milestone, other_goal, tenant)])
        question = {"question": "목표를 바꿔도 될까요?",
                    "assumption": "만약 테넌트를 분리한다고 가정하면 비용이 얼마야"}.get(
                        scenario, "검증을 개선해줘")
        user_message, reply = uuid.uuid4(), uuid.uuid4()
        await conn.executemany("INSERT INTO chat_messages(id,tenant_id,session_id,role,content) VALUES($1,$2,$3,$4,$5)",
                              [(user_message, tenant, session, "user", question),
                               (reply, tenant, session, "assistant", "설계 초안")])
        generated = []
        barrier = asyncio.Event()

        async def generate(source, risk_level, **kwargs):
            content = service.build_fallback_directive(source, risk_level)
            index = len(generated)
            if scenario == "different_content":
                content += f"\n수정 제안 {index}"
            generated.append(content)
            if scenario in {"concurrent", "different_content", "concurrent_binding"}:
                if len(generated) == 2:
                    barrier.set()
                await asyncio.wait_for(barrier.wait(), timeout=10)
            return content, "fallback", "test-only"

        monkeypatch.setattr(service, "generate_directive_content", generate)
        kwargs = dict(tenant_id=str(tenant), user_id=None, session_id=str(session),
                      message_ids=[str(user_message), str(reply)],
                      goal_id=str(goal), milestone_id=str(milestone))
        if scenario in {"concurrent", "different_content", "concurrent_binding"}:
            second = dict(kwargs)
            if scenario == "concurrent_binding":
                second.update(goal_id=str(other_goal), milestone_id=str(other_milestone))
            results = await asyncio.wait_for(asyncio.gather(
                service.create_draft(**kwargs), service.create_draft(**second),
                return_exceptions=True), timeout=20)
            if scenario == "concurrent_binding":
                assert sum(isinstance(r, service.GoalBindingError) for r in results) == 1
                assert sum(isinstance(r, dict) for r in results) == 1
            else:
                assert all(isinstance(r, dict) for r in results), results
                assert results[0]["id"] == results[1]["id"]
            expected = 2 if scenario == "different_content" else 1
            assert await conn.fetchval("SELECT count(*) FROM directive_drafts") == 1
            assert await conn.fetchval("SELECT count(*) FROM chat_artifacts") == 1
            assert await conn.fetchval("SELECT count(*) FROM directive_draft_revisions") == expected
            assert await conn.fetchval("SELECT count(*) FROM directive_draft_events") == expected
            return
        first = await service.create_draft(**kwargs)
        if scenario == "explicit_bundle":
            trace_before = await service.trace_draft_sources(tenant_id=str(tenant), draft_id=first["id"])
            continuation = dict(kwargs, draft_id=first["id"], base_revision=1,
                                composer_draft="근거를 보완해줘")
            second = await service.create_draft(**continuation)
            assert second["current_revision"] == 2 and second["id"] == first["id"]
            with pytest.raises(service.DraftConflictError):
                await service.create_draft(**continuation)
            continuation["base_revision"] = 2
            assert (await service.create_draft(**continuation))["turn_dedup"] == "existing"
            trace_after = await service.trace_draft_sources(tenant_id=str(tenant), draft_id=first["id"])
            assert trace_before["revisions"][0] == trace_after["revisions"][0]
            assert trace_after["revisions"][1]["review_bundle"]["base_revision"] == 1
            await service.record_event(tenant_id=str(tenant), user_id=None,
                                       draft_id=first["id"], action="approved")
            with pytest.raises(service.DraftConflictError):
                await service.create_draft(**continuation)
        elif scenario == "stale_revision":
            edit = dict(tenant_id=str(tenant), user_id=None, draft_id=first["id"],
                        title="수정", content=None, expected_revision=1)
            await service.update_draft(**edit)
            with pytest.raises(service.DraftConflictError):
                await service.update_draft(**edit)
            assert await conn.fetchval("SELECT count(*) FROM directive_draft_revisions") == 2
        elif scenario in {"question", "assumption"}:
            assert first["classification"]["turn_kind"] == "open"
            assert first["goal_change_allowed"] is False
        else:
            invalid = dict(kwargs)
            if scenario == "cross_tenant":
                invalid["tenant_id"] = str(foreign)
                error = service.DraftNotFoundError
            elif scenario == "cross_session":
                invalid["session_id"] = str(other_session)
                error = ValueError
            elif scenario == "foreign_goal":
                invalid.update(goal_id=str(foreign_goal), milestone_id=None)
                error = service.GoalBindingError
            else:
                invalid.update(goal_id=str(other_goal), milestone_id=str(other_milestone))
                error = service.GoalBindingError
            with pytest.raises(error):
                await service.create_draft(**invalid)
            assert await conn.fetchval("SELECT count(*) FROM directive_draft_revisions") == 1
        assert await conn.fetchval("SELECT count(*) FROM goals") == 3
        assert await conn.fetchval("SELECT count(*) FROM milestones") == 2
    finally:
        if pool is not None:
            await pool.close()
        try:
            await conn.execute("SET search_path TO public")
            await conn.execute(f"DROP SCHEMA IF EXISTS {schema} CASCADE")
        finally:
            await conn.close()


@pytest.mark.parametrize("scenario", [
    "concurrent", "different_content", "concurrent_binding", "stale_revision", "explicit_bundle",
    "cross_tenant", "cross_session", "foreign_goal", "cross_goal", "question", "assumption",
])
def test_real_draft_binding_and_revisions(monkeypatch, scenario):
    asyncio.run(exercise(monkeypatch, scenario))
