"""Run against a disposable PostgreSQL database named with a ``_test`` suffix."""
import asyncio
import os
from pathlib import Path
from urllib.parse import urlparse

import pytest

asyncpg = pytest.importorskip("asyncpg")

DATABASE_URL = os.getenv("MILESTONE_CHECKLIST_TEST_DATABASE_URL", "")
pytestmark = pytest.mark.skipif(
    not DATABASE_URL, reason="MILESTONE_CHECKLIST_TEST_DATABASE_URL is not configured"
)

ROOT = Path(__file__).parents[2]
UP = (ROOT / "migrations/20260923_milestone_completion_checklist.sql").read_text()
DOWN = (ROOT / "migrations/rollback/20260923_milestone_completion_checklist.down.sql").read_text()
VERIFY = (ROOT / "migrations/20260923_milestone_completion_checklist.verify.sql").read_text()
BASE = (ROOT / "migrations/153_goal_management.sql").read_text()
ALIGN = (ROOT / "migrations/156_goal_control_loop_schema_alignment.sql").read_text()


async def _exercise(database_url):
    conn = await asyncpg.connect(database_url)
    try:
        for table in ("goals", "milestones", "goal_task_links"):
            assert await conn.fetchval("SELECT to_regclass($1)", f"public.{table}") is None
        # Keep the disposable database empty even if an assertion fails.
        async with conn.transaction():
            await conn.execute(BASE)
            await conn.execute(ALIGN)
            goal_id = await conn.fetchval(
                "INSERT INTO public.goals(title) VALUES ('migration test') RETURNING id"
            )
            milestone_id = await conn.fetchval(
                """INSERT INTO public.milestones(goal_id, title, completion_criteria)
                   VALUES ($1, 'legacy milestone', 'legacy') RETURNING id""",
                goal_id,
            )
            await conn.execute(UP)
            await conn.execute(UP)
            await conn.execute(VERIFY)
            row = await conn.fetchrow(
                """SELECT goal_id, title, completion_criteria, completion_checklist
                   FROM public.milestones WHERE id = $1""", milestone_id,
            )
            assert row["goal_id"] == goal_id
            assert row["title"] == "legacy milestone"
            assert row["completion_criteria"] == "legacy"
            assert row["completion_checklist"] is None
            await conn.execute(
                "UPDATE public.milestones SET completion_checklist = '[\"check\"]'::jsonb WHERE id = $1",
                milestone_id,
            )
            assert await conn.fetchval(
                "SELECT completion_checklist FROM public.milestones WHERE id = $1", milestone_id,
            ) == '["check"]'
            await conn.execute(DOWN)
            assert await conn.fetchval(
                "SELECT count(*) FROM information_schema.columns WHERE table_schema='public' "
                "AND table_name='milestones' AND column_name='completion_checklist'"
            ) == 0
            assert await conn.fetchval(
                "SELECT completion_criteria FROM public.milestones WHERE id = $1", milestone_id,
            ) == "legacy"
            await conn.execute("DROP TABLE public.goal_task_links, public.milestones, public.goals")
    finally:
        await conn.close()


def test_migration_twice_verify_and_rollback_on_disposable_postgres():
    assert urlparse(DATABASE_URL).path.removeprefix("/").endswith("_test")
    asyncio.run(_exercise(DATABASE_URL))
