"""자동 재작업이 부모의 활성 goal_task_links 목표·마일스톤을 상속한다."""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager

import pytest

from app.services import pipeline_auto_rework as ar

G1, M1 = "9cb633db-0000-4000-8000-000000000001", "e9d35a3f-0000-4000-8000-000000000001"
G2, M2 = "552d350d-0000-4000-8000-000000000002", "7cd67f6e-0000-4000-8000-000000000002"
TENANT = "00000000-0000-0000-0000-000000000001"
INSTRUCTION = "TASK_ID: X\nTITLE: t\nPRIORITY: P1\nSIZE: S\nTARGET: /root/aads/aads-server\n\n본문\n"


class InFailedSQLTransactionError(Exception):
    pass


class PgLikeConn:
    """aborted 트랜잭션을 흉내 낸다: 조회 오류가 SAVEPOINT 밖에서 나면 이후 모든 SQL 이 실패한다."""

    def __init__(self, parent_row, link_rows=(), link_error=None):
        self.parent_row = parent_row
        self.link_rows = list(link_rows)
        self.link_error = link_error
        self.executed: list[tuple] = []
        self.link_params: tuple | None = None
        self._depth = 0
        self._aborted_depth: int | None = None

    def _check(self):
        if self._aborted_depth is not None:
            raise InFailedSQLTransactionError(
                "current transaction is aborted, commands ignored until end of transaction block"
            )

    async def fetchrow(self, sql, *args):
        self._check()
        if "FROM code_reviews" in sql:
            return None
        if "FROM goal_task_links" in sql:
            self.link_params = args
            if self.link_error:
                self._aborted_depth = self._depth
                raise self.link_error
            return self.link_rows[0] if self.link_rows else None
        return self.parent_row

    async def fetchval(self, sql, *args):
        self._check()
        return None

    async def execute(self, sql, *args):
        self._check()
        self.executed.append((sql, args))
        return "OK"

    def transaction(self):
        return self._tx()

    @asynccontextmanager
    async def _tx(self):
        self._depth += 1
        level = self._depth
        try:
            yield
        except BaseException:
            if self._aborted_depth is not None and self._aborted_depth >= level:
                self._aborted_depth = None  # ROLLBACK [TO SAVEPOINT]
            raise
        finally:
            self._depth -= 1


def _parent(**kw):
    base = dict(
        job_id="runner-aaaaaaaa", project="AADS", instruction=INSTRUCTION,
        chat_session_id=None, status="error", phase="review_failed",
        error_detail="review_failed: verdict=REQUEST_CHANGES score=0.68",
        max_cycles=3, model="m", size="S", worker_model=None, model_override_reason=None,
        parallel_group=None, tenant_id=TENANT, commit_hash="abc", review_score=0.68,
        goal_id=G1, milestone_id=M1,
    )
    base.update(kw)
    return base


def _insert_args(conn):
    inserts = [a for s, a in conn.executed if "INSERT INTO pipeline_jobs" in s]
    assert len(inserts) == 1
    return inserts[0]


def _submit(conn):
    return asyncio.run(ar.maybe_submit_auto_rework(conn, "runner-aaaaaaaa"))


def test_active_link_overrides_stale_parent_column():
    conn = PgLikeConn(_parent(), link_rows=[{"goal_id": G2, "milestone_id": M2}])
    res = _submit(conn)
    args = _insert_args(conn)
    assert (args[-4], args[-3]) == (G2, M2)
    assert (res["goal_id"], res["milestone_id"]) == (G2, M2)
    assert res["goal_source"] == ar.GOAL_SOURCE_PARENT_ACTIVE_LINK
    assert args[-1] == ar.GOAL_SOURCE_PARENT_ACTIVE_LINK


def test_no_active_link_keeps_column_values():
    conn = PgLikeConn(_parent())
    res = _submit(conn)
    args = _insert_args(conn)
    assert (args[-4], args[-3]) == (G1, M1)
    assert (res["goal_id"], res["milestone_id"]) == (G1, M1)
    assert res["goal_source"] == ar.GOAL_SOURCE_PARENT_COLUMN


def test_lookup_only_selects_active_unsuperseded_links_of_same_tenant():
    conn = PgLikeConn(_parent())
    _submit(conn)
    assert conn.link_params == ("runner-aaaaaaaa", TENANT)


@pytest.mark.parametrize("fragment", [
    "l.task_type = 'pipeline_job'", "l.link_state = 'active'", "l.superseded_by IS NULL",
    "l.tenant_id = $2::uuid", "ORDER BY l.updated_at DESC",
])
def test_lookup_sql_filters(fragment):
    assert fragment in ar._ACTIVE_LINK_SQL


def test_superseded_or_detached_links_only_fall_back_to_column():
    # 필터가 SQL 에서 적용되므로 superseded/detached 만 있으면 조회 결과는 비어 있다.
    conn = PgLikeConn(_parent(), link_rows=[])
    res = _submit(conn)
    assert (res["goal_id"], res["milestone_id"]) == (G1, M1)
    assert res["goal_source"] == ar.GOAL_SOURCE_PARENT_COLUMN


def test_missing_tenant_skips_lookup():
    conn = PgLikeConn(_parent(tenant_id=None), link_rows=[{"goal_id": G2, "milestone_id": M2}])
    binding = asyncio.run(ar.resolve_parent_goal_binding(conn, _parent(tenant_id=None)))
    assert conn.link_params is None
    assert (binding.goal_id, binding.milestone_id) == (G1, M1)


def test_link_lookup_error_does_not_abort_the_outer_transaction():
    """조회가 PG 오류로 실패해도 같은 트랜잭션의 INSERT 가 살아 있어야 한다 (SAVEPOINT 격리)."""
    conn = PgLikeConn(_parent(), link_error=RuntimeError("canceling statement due to statement timeout"))
    res = _submit(conn)
    assert res and res["job_id"].startswith("runner-")
    args = _insert_args(conn)
    assert (args[-4], args[-3]) == (G1, M1)
    assert res["goal_source"] == ar.GOAL_SOURCE_PARENT_COLUMN
    assert any("pg_notify" in s for s, _ in conn.executed)


def test_fake_conn_really_aborts_without_savepoint():
    """위 테스트가 의미 있으려면 이 가짜 연결이 SAVEPOINT 없는 실패를 실제로 막아야 한다."""
    conn = PgLikeConn(_parent(), link_error=RuntimeError("boom"))

    async def run():
        async with conn.transaction():
            with pytest.raises(RuntimeError):
                await conn.fetchrow("SELECT 1 FROM goal_task_links")
            with pytest.raises(InFailedSQLTransactionError):
                await conn.execute("INSERT INTO pipeline_jobs VALUES (1)")

    asyncio.run(run())


def test_bound_by_records_parent_for_rework_link():
    src = open("app/api/pipeline_runner.py", encoding="utf-8").read()
    assert 'bound_by=f"auto_rework_of:{rework[\'parent\']}"' in src
