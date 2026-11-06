"""작업 목록의 대기 사유와 지시 세션 표시 회귀 검증."""
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.api import pipeline_runner as pr


@pytest.mark.asyncio
@pytest.mark.parametrize("session_filter", [None, "session-a"])
async def test_jobs_batch_metadata_and_existing_fields(monkeypatch, session_filter):
    now = datetime.now(timezone.utc)
    parent_statuses = ["running", "error", None, "done", "queued",
                       "awaiting_approval", "cancelled", None]
    rows = [
        dict(job_id=f"job-{i}", project="AADS", instruction="x" * 250,
             status="running" if i == 7 else "queued", phase="queued",
             cycle=2, error_detail=None, created_at=now, updated_at=now,
             started_at=now, depends_on=f"parent-{i}" if state else None,
             chat_session_id="session-a", model="model", worker_model="worker",
             actual_model="actual", size="S", runner_pid=None,
             runner_host="contabo42", auth_recovery_state="",
             auth_recovery_metadata={})
        for i, state in enumerate(parent_statuses)
    ]
    calls = []

    async def fetch(query, *args):
        calls.append((query, args))
        if "FROM chat_sessions s" in query:
            assert "ANY($2::text[])" in query
            assert args == ("tenant-test", ["session-a"])
            assert "s.tenant_id = $1::uuid" in query
            assert "w.id = s.workspace_id" in query
            return [dict(session_id="session-a", title="지시한 채팅창",
                         workspace_name="서버 작업")]
        if "job_id = ANY" in query:
            assert args[0] == "tenant-test"
            assert set(args[1]) == {f"parent-{i}" for i, s in enumerate(parent_statuses) if s}
            return [dict(job_id=f"parent-{i}", status=s)
                    for i, s in enumerate(parent_statuses) if s]
        assert "runner_host" in query
        assert "tenant_id = $1::uuid" in query
        if session_filter:
            assert "chat_session_id = $2" in query
            assert args == ("tenant-test", session_filter, len(rows))
        else:
            assert "chat_session_id =" not in query
            assert args == ("tenant-test", len(rows))
        return rows

    conn = MagicMock(fetch=AsyncMock(side_effect=fetch))
    pool = MagicMock()
    pool.acquire.return_value.__aenter__ = AsyncMock(return_value=conn)
    pool.acquire.return_value.__aexit__ = AsyncMock(return_value=False)
    monkeypatch.setattr("app.core.db_pool.get_pool", lambda: pool)
    monkeypatch.setattr(pr, "_pipeline_column_exists", AsyncMock(return_value=True))
    monkeypatch.setattr(pr, "_runner_health_probe", AsyncMock(return_value={"alive": True}))
    result = await pr.list_jobs(status=None, project=None, session_id=session_filter,
                                limit=len(rows), context={"tenant": {"id": "tenant-test"}})
    assert len(calls) == 3  # 목록 + 세션/워크스페이스 한 번 + 선행 상태 한 번
    assert [r["wait_reason"] for r in result] == [
        "선행 작업 대기: parent-0(running)",
        "선행 작업 실패로 막힘: parent-1(error)",
        "서버 빈자리 대기", "서버 빈자리 대기",
        "선행 작업 대기: parent-4(queued)",
        "선행 작업 대기: parent-5(awaiting_approval)",
        "선행 작업 실패로 막힘: parent-6(cancelled)", None,
    ]
    expected = {k: rows[0][k] for k in (
        "job_id", "project", "status", "phase", "cycle", "error_detail",
        "depends_on", "model", "worker_model", "actual_model", "size",
        "auth_recovery_state", "auth_recovery_metadata",
    )}
    expected.update(instruction="x" * 200, created_at=now.isoformat(),
                    updated_at=now.isoformat(), started_at=now.isoformat())
    expected.update(pr._runner_display_status("queued", "queued", None, ""))
    assert {k: result[0][k] for k in expected} == expected
    assert result[0]["health_probe"] == {"alive": True}
    for item in result:
        assert item["runner_host"] == "contabo42"
        assert item["chat_session_title"] == "지시한 채팅창"
        assert item["workspace_name"] == "서버 작업"


@pytest.mark.asyncio
async def test_archived_metadata_and_missing_session(monkeypatch):
    conn = MagicMock(fetch=AsyncMock(return_value=[]))
    pool = MagicMock()
    pool.acquire.return_value.__aenter__ = AsyncMock(return_value=conn)
    pool.acquire.return_value.__aexit__ = AsyncMock(return_value=False)
    monkeypatch.setattr("app.core.db_pool.get_pool", lambda: pool)
    monkeypatch.setattr(pr, "_pipeline_column_exists", AsyncMock(return_value=True))
    archived = pr._archived_job_item(
        dict(job_id="old", status="done", chat_session_id="deleted-session"), detail=False)
    archived["runner_host"] = "contabo14"
    archive_fetch = AsyncMock(return_value=[archived])
    monkeypatch.setattr(pr, "_fetch_archived_jobs", archive_fetch)
    result = await pr.list_jobs(status=None, project=None, session_id="deleted-session",
                                limit=20, context={"tenant": {"id": "tenant-test"}})
    assert result[0]["archived"] is True
    assert result[0]["runner_host"] == "contabo14"
    assert result[0]["chat_session_title"] is None
    assert result[0]["workspace_name"] is None
    assert result[0]["wait_reason"] is None
    assert archive_fetch.call_args.kwargs["session_id"] == "deleted-session"
