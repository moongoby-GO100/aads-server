from __future__ import annotations

import inspect
import uuid
from unittest.mock import AsyncMock
from pathlib import Path

import pytest

from app.api.directive_drafts import router
from app.services import directive_draft_service as service
from app.services.directive_draft_service import (
    DraftSource,
    build_fallback_directive,
    classify_risk,
    normalize_project_key,
    validate_directive,
)


def _source(project_key: str = "AADS") -> DraftSource:
    return DraftSource(
        session_id=uuid.uuid4(),
        workspace_id=uuid.uuid4(),
        session_title="파동엔진 통일",
        workspace_name="CEO 통합지시",
        project_key=project_key,
        messages=[
            {"id": uuid.uuid4(), "role": "user", "content": "파동엔진 정본을 통일해서 구현해줘"},
            {"id": uuid.uuid4(), "role": "assistant", "content": "현재 경로가 둘로 나뉘어 있습니다."},
        ],
    )


def test_normalize_project_key_fails_closed_to_custom() -> None:
    assert normalize_project_key("newtalk-v2") == "NTV2"
    assert normalize_project_key("ShortFlow") == "SF"
    assert normalize_project_key("unknown-project") == "CUSTOM"


def test_classify_risk_distinguishes_read_write_and_ops() -> None:
    assert classify_risk("상태를 확인하고 보고해줘") == "low"
    assert classify_risk("서비스 코드를 수정해줘") == "medium"
    assert classify_risk("DB migration 후 docker 배포해줘") == "high"


def test_fallback_directive_satisfies_v2_contract() -> None:
    content = build_fallback_directive(_source(), "medium")
    valid, errors = validate_directive(content)
    assert valid, errors
    assert "TASK_ID: AADS-DRAFT" in content
    assert "기존 정본과 호출 경로" in content
    assert "완료 보고:" in content


def test_validate_directive_rejects_partial_response() -> None:
    valid, errors = validate_directive(">>>DIRECTIVE_START\nTITLE: 누락\n>>>DIRECTIVE_END")
    assert not valid
    assert "task_id" in errors
    assert "description" in errors


def test_validate_directive_enforces_project_model_and_description_contract() -> None:
    content = build_fallback_directive(_source(), "medium")
    invalid = content.replace("TASK_ID: AADS-DRAFT", "TASK_ID: GO100-DRAFT").replace(
        "MODEL: AUTO", "MODEL: CUSTOM"
    ).replace("  완료 보고:\n", "  결과 보고:\n")

    valid, errors = validate_directive(invalid, expected_project="AADS")

    assert not valid
    assert "task_id_value" in errors
    assert "model_value" in errors
    assert "description_완료_보고" in errors


def test_validate_directive_accepts_matching_project_contract() -> None:
    content = build_fallback_directive(_source("GO100"), "medium")

    valid, errors = validate_directive(content, expected_project="GO100")

    assert valid, errors


@pytest.mark.asyncio
async def test_generation_rejects_valid_looking_wrong_project_and_falls_back(monkeypatch) -> None:
    wrong_project = build_fallback_directive(_source("GO100"), "medium")

    async def wrong_generation(**_kwargs):
        return wrong_project

    monkeypatch.setattr(service, "call_llm_with_fallback", wrong_generation)
    content, mode = await service.generate_directive_content(_source("AADS"), "medium")

    assert mode == "fallback"
    assert "TASK_ID: AADS-DRAFT" in content


def test_directive_draft_api_routes_are_registered() -> None:
    paths = {route.path for route in router.routes}
    assert "/chat/sessions/{session_id}/directive-drafts" in paths
    assert "/chat/directive-drafts/{draft_id}" in paths
    assert "/chat/directive-drafts/{draft_id}/events" in paths


def test_migration_is_additive_and_contains_audit_tables() -> None:
    sql = (Path(__file__).parents[2] / "migrations/172_directive_draft_copilot.sql").read_text()
    assert "CREATE TABLE IF NOT EXISTS directive_drafts" in sql
    assert "CREATE TABLE IF NOT EXISTS directive_draft_revisions" in sql
    assert "CREATE TABLE IF NOT EXISTS directive_draft_events" in sql
    assert "DROP TABLE" not in sql.upper()
    assert "TRUNCATE" not in sql.upper()


def test_artifact_revision_metadata_parameter_has_explicit_postgres_type() -> None:
    source = inspect.getsource(service.update_draft)

    assert "jsonb_build_object('revision', $5::integer)" in source


def test_serialize_artifact_decodes_json_metadata() -> None:
    artifact_id = uuid.uuid4()

    result = service._serialize_artifact(
        {
            "id": artifact_id,
            "type": "report",
            "metadata": '{"subtype":"directive_draft","revision":2}',
        }
    )

    assert result["id"] == str(artifact_id)
    assert result["artifact_type"] == "report"
    assert result["metadata"] == {"subtype": "directive_draft", "revision": 2}


@pytest.mark.asyncio
async def test_generation_failure_uses_deterministic_fallback(monkeypatch) -> None:
    async def fail_generation(**_kwargs):
        raise RuntimeError("provider unavailable")

    monkeypatch.setattr(service, "call_llm_with_fallback", fail_generation)
    content, mode = await service.generate_directive_content(_source(), "medium")

    assert mode == "fallback"
    assert "TASK_ID: AADS-DRAFT" in content
    assert validate_directive(content)[0]


@pytest.mark.asyncio
async def test_artifact_edit_uses_artifact_revision_for_optimistic_lock(monkeypatch) -> None:
    artifact_id = uuid.uuid4()
    draft_id = uuid.uuid4()
    tenant_id = str(uuid.uuid4())
    update_mock = AsyncMock(return_value={})
    monkeypatch.setattr(service, "update_draft", update_mock)

    class _Connection:
        async def fetchrow(self, *_args):
            return {
                "id": artifact_id,
                "session_id": uuid.uuid4(),
                "type": "report",
                "title": "지시 초안: 수정본",
                "content": build_fallback_directive(_source(), "medium"),
                "metadata": {"draft_id": str(draft_id), "revision": 8},
            }

    class _Acquire:
        async def __aenter__(self):
            return _Connection()

        async def __aexit__(self, *_args):
            return None

    class _Pool:
        def acquire(self):
            return _Acquire()

    monkeypatch.setattr(service, "get_pool", lambda: _Pool())

    await service.update_draft_from_artifact(
        tenant_id=tenant_id,
        user_id=None,
        artifact={
            "id": str(artifact_id),
            "metadata": {"draft_id": str(draft_id), "revision": 8},
        },
        title="지시 초안: 수정본",
        content=build_fallback_directive(_source(), "medium"),
    )

    assert update_mock.await_args.kwargs["expected_revision"] == 8
    assert update_mock.await_args.kwargs["change_source"] == "artifact_edit"
