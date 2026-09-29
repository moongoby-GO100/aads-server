"""승계 작업 제출 시 취소됐던 하위작업 재연결 (AADS-RUNNER-SUPERSEDED-ORPHAN-RELINK).

배경. 2026-09-29 runner-2ae121bc 가 배포 게이트에서 실패하자
runner-72c2a09d 가 cancelled/blocked_dependency 로 닫혔고, 같은 산출물을
이어받은 승계 작업이 성공해도 그 하위작업을 다시 여는 경로가 없었다.
"""
import pytest

from app.api.pipeline_runner import (
    _ORPHAN_RELINK_MAX,
    _ORPHAN_RELINK_WINDOW_HOURS,
    _relink_superseded_orphans,
    parse_superseded_job_ids,
)


class _FakeConn:
    """fetch 는 미리 정한 행을 돌려주고, execute 는 호출만 기록한다."""

    def __init__(self, rows=None):
        self._rows = rows or []
        self.fetch_calls = []
        self.execute_calls = []

    async def fetch(self, sql, *args):
        self.fetch_calls.append((sql, args))
        return self._rows

    async def execute(self, sql, *args):
        self.execute_calls.append((sql, args))
        return "SELECT 1"


def test_parse_collects_supersedes_and_auto_rework_without_duplicates():
    instruction = (
        "ALLOW_DUP_JOB\n"
        "SUPERSEDES: runner-2ae121bc, runner-a0bde2b6\n"
        "AUTO_REWORK_OF: runner-2ae121bc\n"
        "TASK_ID: AADS-X\n"
        "본문에 runner-deadbeef 를 적어도 헤더가 아니면 무시하지 않는다는 뜻은 아니다.\n"
    )
    assert parse_superseded_job_ids(instruction) == [
        "runner-2ae121bc",
        "runner-a0bde2b6",
    ]


def test_parse_returns_empty_without_header():
    assert parse_superseded_job_ids("TASK_ID: AADS-X\nTITLE: 무관\n") == []
    assert parse_superseded_job_ids("") == []
    assert parse_superseded_job_ids(None) == []


@pytest.mark.asyncio
async def test_relink_noop_without_supersedes_header():
    conn = _FakeConn(rows=[{"job_id": "runner-11111111"}])
    out = await _relink_superseded_orphans(
        conn,
        job_id="runner-99999999",
        project="AADS",
        instruction="TASK_ID: AADS-X\n",
        tenant_id="00000000-0000-0000-0000-000000000000",
    )
    assert out == []
    assert conn.fetch_calls == []
    assert conn.execute_calls == []


@pytest.mark.asyncio
async def test_relink_requeues_children_and_notifies_runner():
    conn = _FakeConn(rows=[{"job_id": "runner-72c2a09d"}])
    out = await _relink_superseded_orphans(
        conn,
        job_id="runner-5df9e0d3",
        project="AADS",
        instruction="ALLOW_DUP_JOB\nSUPERSEDES: runner-2ae121bc, runner-a0bde2b6\n",
        tenant_id="11111111-1111-1111-1111-111111111111",
    )
    assert out == ["runner-72c2a09d"]

    sql, args = conn.fetch_calls[0]
    # 되살리는 조건이 좁게 유지돼야 한다.
    assert "status = 'cancelled'" in sql
    assert "phase = 'blocked_dependency'" in sql
    assert "make_interval" in sql
    assert args[0] == "runner-5df9e0d3"
    assert args[3] == "AADS"
    assert args[4] == ["runner-2ae121bc", "runner-a0bde2b6"]
    # 대체 대상 자신과 신규 작업은 되살리지 않는다.
    assert args[5] == sorted(
        {"runner-2ae121bc", "runner-a0bde2b6", "runner-5df9e0d3"}
    )
    assert args[6] == _ORPHAN_RELINK_WINDOW_HOURS
    assert args[7] == _ORPHAN_RELINK_MAX

    # 큐가 깨어나야 실제로 실행된다.
    assert len(conn.execute_calls) == 1
    notify_sql, notify_args = conn.execute_calls[0]
    assert "pg_notify" in notify_sql
    assert notify_args == ("runner-72c2a09d",)


@pytest.mark.asyncio
async def test_relink_bounds_are_conservative():
    assert _ORPHAN_RELINK_MAX <= 10
    assert _ORPHAN_RELINK_WINDOW_HOURS <= 24
