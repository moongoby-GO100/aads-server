"""Job-bound draft registration and persisted document evidence contracts."""
import asyncio
import hashlib
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException
from pydantic import ValidationError
from starlette.requests import Request

from app.api import canonical_documents as docs
from app.api import pipeline_runner as runner
from tests.unit.test_canonical_documents import StubConnection, TENANT


class Connection(StubConnection):
    def __init__(self, project="AADS", status="running", rows=()):
        super().__init__(matches=[{"id": "revision-1", "content_hash": docs._body("safe text", None)[2],
                                   "version": "1.0.0", "idempotency_key": "request-1"}])
        self.project, self.status, self.rows = project, status, rows
        self.audit = []

    async def fetchrow(self, sql, *args):
        if "SELECT project,status FROM pipeline_jobs" in sql:
            assert args == ("runner-test", TENANT)
            return {"project": self.project, "status": self.status}
        return await super().fetchrow(sql, *args)

    async def fetch(self, sql, *args):
        if "FROM pipeline_jobs j JOIN" in sql:
            assert "r.source_task_id=j.job_id" in sql
            assert "r.tenant_id=j.tenant_id" in sql
            assert "r.project_key=upper(j.project)" in sql
            assert "DOC_AUTO_RESOLVE_OF" in sql
            return self.rows
        return await super().fetch(sql, *args)

    async def execute(self, sql, *args):
        self.audit.append((sql, args))
        await super().execute(sql, *args)


def body(**over):
    revision = dict(document_key="test-doc", title="테스트 문서", kind="prd", version="1.0.0",
                    content="safe text", expected_generation=1, idempotency_key="request-1",
                    source_task_id="runner-other", change_summary="러너 정본 초안 등록을 검증합니다")
    revision.update(over)
    return runner.RunnerDraftInput(project_key="AADS", revision=revision)


def register(value):
    return asyncio.run(runner.register_runner_draft(value, {"sub": "runner-test", "tenant_id": TENANT}))


def test_source_overwritten_and_transactional_audit(monkeypatch):
    conn = Connection()
    monkeypatch.setattr("app.core.db_pool.get_pool", lambda: conn)
    create = AsyncMock(return_value={"revision_id": "revision-1"})
    monkeypatch.setattr(docs, "create_revision_in_tx", create)
    register(body())
    assert create.call_args.args[-1].source_task_id == "runner-test"
    assert create.call_args.args[-1].source_session_id is None
    assert '"revision_id": "revision-1"' in conn.audit[-1][1][-1]
    assert '"document_key": "test-doc"' in conn.audit[-1][1][-1]


@pytest.mark.parametrize("project,status,code", [("GO100", "running", 403), ("AADS", "done", 409),
                                                  ("AADS", "queued", 409)])
def test_job_scope_and_running_required(monkeypatch, project, status, code):
    monkeypatch.setattr("app.core.db_pool.get_pool", lambda: Connection(project, status))
    with pytest.raises(HTTPException) as exc:
        register(body())
    assert exc.value.status_code == code


def test_approval_and_source_reads_rejected():
    with pytest.raises(ValidationError):
        runner.RunnerDraftInput(project_key="AADS", revision={}, status="approved")
    for fields in ({"approve": True}, {"source_path": "docs/plan.md"}):
        with pytest.raises(HTTPException) as exc:
            register(body(**fields))
        assert exc.value.status_code == 422


def test_secret_and_idempotency_reuse_existing_revision_logic(monkeypatch):
    conn = Connection()
    monkeypatch.setattr("app.core.db_pool.get_pool", lambda: conn)
    with pytest.raises(HTTPException) as exc:
        register(body(content="password=supersecretvalue"))
    assert exc.value.status_code == 422
    assert register(body())["idempotent"] is True
    assert register(body())["revision_id"] == "revision-1"
    assert not any("INSERT INTO project_document_revisions" in sql for sql in conn.writes)


def test_common_monitor_key_cannot_choose_a_job():
    request = Request({"type": "http", "headers": [(b"x-monitor-key", b"internal-pipeline-call")]})
    with pytest.raises(HTTPException) as exc:
        asyncio.run(runner.authenticated_runner_job(request))
    assert exc.value.status_code == 401


def test_existing_signed_bearer_identity(monkeypatch):
    from app import auth
    monkeypatch.setattr(auth, "verify_token", lambda token: {"sub": "runner-test", "tenant_id": TENANT})
    request = Request({"type": "http", "headers": [(b"authorization", b"Bearer signed-job-token")]})
    assert asyncio.run(runner.authenticated_runner_job(request))["sub"] == "runner-test"


def test_persisted_hash_and_excerpt(monkeypatch):
    content = "등록된 본문" * 1000
    row = {"document_key": "test-doc", "version": "1.0.0", "content": content,
           "content_hash": hashlib.sha256(content.encode()).hexdigest(), "revision_id": "revision-1"}
    conn = Connection(rows=[row])
    monkeypatch.setattr(docs, "get_pool", lambda: conn)
    result = asyncio.run(docs.runner_revision_artifacts("runner-test", "AADS"))
    assert result[0]["hash_verified"]
    assert len(result[0]["content_excerpt"]) == 4000
    row["content_hash"] = "tampered"
    with pytest.raises(ValueError, match="hash_mismatch"):
        asyncio.run(docs.runner_revision_artifacts("runner-test", "AADS"))


def test_document_revision_prevents_empty_completion_and_reaches_reviewer(monkeypatch):
    from types import SimpleNamespace
    from app.services import pipeline_runner_service as service
    from app.core import anthropic_client

    artifacts = [{"document_key": "test-doc", "version": "1.0.0", "content_hash": "verified-hash",
                  "content_excerpt": "등록된 본문", "hash_verified": True}]
    monkeypatch.setattr(docs, "runner_revision_artifacts", AsyncMock(return_value=artifacts))
    llm = AsyncMock(return_value='{"verdict":"PASS","summary":"검증","feedback":""}')
    monkeypatch.setattr(anthropic_client, "call_background_llm", llm)
    instruction = "MODE: READ_ONLY\nDOC_AUTO_RESOLVE_OF: runner-parent"
    assert not service._is_read_only_done(instruction, "", "완료", artifacts)
    job = SimpleNamespace(job_id="runner-test", project="AADS", instruction=instruction,
                          git_diff="", result_output="완료")
    result = asyncio.run(service.PipelineCJob._ai_review(job))
    assert result["verdict"] == "PASS"
    prompt = llm.call_args.kwargs["prompt"]
    for evidence in ("test-doc", "1.0.0", "verified-hash", "등록된 본문"):
        assert evidence in prompt
    # Unmarked code tasks retain their prior empty-input behavior.
    from app.services.code_reviewer import _precheck_review_input
    assert _precheck_review_input("", revision_artifacts=artifacts) is None
    assert _precheck_review_input("").verdict == "SKIP"
    assert service._is_read_only_done("일반 코드 작업", "", "완료", artifacts) == service._is_read_only_done("일반 코드 작업", "", "완료")
