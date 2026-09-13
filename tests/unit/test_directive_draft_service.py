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


def _selected_response_source() -> DraftSource:
    user_id = uuid.uuid4()
    assistant_id = uuid.uuid4()
    return DraftSource(
        session_id=uuid.uuid4(),
        workspace_id=uuid.uuid4(),
        session_title="파동엔진 통일",
        workspace_name="CEO 통합지시",
        project_key="AADS",
        messages=[
            {"id": user_id, "role": "user", "content": "왜 서로 다른 파동엔진을 쓰는지 보고해줘"},
            {
                "id": assistant_id,
                "role": "assistant",
                "content": (
                    "현재 정본과 소비자가 분리돼 있습니다.\n\n"
                    "→ 다음 단계:\n"
                    "1. AI 응답 버블에 지시서 버튼 추가\n"
                    "2. 선택 응답과 직전 질문만 초안 생성 API에 전달\n"
                    "3. 폴백도 선택 응답을 실행 항목으로 변환"
                ),
            },
        ],
        selected_assistant_message_id=assistant_id,
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


def test_selected_response_fallback_uses_assistant_actions_not_user_question() -> None:
    content = build_fallback_directive(_selected_response_source(), "medium")

    assert "TITLE: AI 응답 버블에 지시서 버튼 추가" in content
    assert "선택한 AI 응답의 제안·조치 항목" in content
    assert "선택한 AI 응답의 후속 항목" in content
    assert "해당 응답의 원 사용자 요청" in content
    assert validate_directive(content, expected_project="AADS")[0]


def test_selected_response_prompt_marks_and_prioritizes_assistant_answer() -> None:
    prompt = service._build_generation_prompt(_selected_response_source(), "medium")

    assert "ASSISTANT | SELECTED_RESPONSE" in prompt
    assert "사용자 질문을 그대로 다시 지시하지 않는다" in prompt


def test_generation_prompt_includes_unsent_composer_as_latest_intent() -> None:
    source = _source()
    source = DraftSource(
        **{**source.__dict__, "composer_draft": "입력창에 작성 중인 추가 요구사항도 포함해줘"}
    )

    prompt = service._build_generation_prompt(source, "medium")

    assert "[COMPOSER_DRAFT | UNSENT]" in prompt
    assert "입력창에 작성 중인 추가 요구사항도 포함해줘" in prompt
    assert "가장 최신 요구사항" in prompt


def test_fallback_prefers_unsent_composer_text() -> None:
    source = _source()
    source = DraftSource(
        **{**source.__dict__, "composer_draft": "편집창을 화면 높이에 맞춰 자동으로 확장해줘"}
    )

    content = build_fallback_directive(source, "medium")

    assert "편집창을 화면 높이에 맞춰 자동으로 확장해줘" in content
    assert validate_directive(content, expected_project="AADS")[0]


def test_fallback_supports_composer_only_source() -> None:
    source = _source()
    source = DraftSource(
        **{
            **source.__dict__,
            "messages": [],
            "composer_draft": "첫 메시지를 보내기 전에 이 내용으로 지시서를 만들어줘",
        }
    )

    content = build_fallback_directive(source, "medium")

    assert "첫 메시지를 보내기 전에 이 내용으로 지시서를 만들어줘" in content
    assert validate_directive(content, expected_project="AADS")[0]


def test_selected_response_risk_includes_selected_follow_up_actions() -> None:
    source = _selected_response_source()
    selected_id = source.selected_assistant_message_id
    assert selected_id is not None
    source.messages[-1]["content"] += "\n4. 운영에 deploy"

    assert classify_risk(service._risk_source_text(source)) == "high"


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

    monkeypatch.setattr(service, "_call_configured_model", wrong_generation)
    monkeypatch.setattr(
        service,
        "_get_directive_model_config",
        AsyncMock(return_value={"models": ["claude-sonnet-5"], "timeout_seconds": 1, "max_tokens": 2000}),
    )
    content, mode, model_used = await service.generate_directive_content(_source("AADS"), "medium")

    assert mode == "fallback"
    assert model_used is None
    assert "TASK_ID: AADS-DRAFT" in content


def test_directive_draft_api_routes_are_registered() -> None:
    paths = {route.path for route in router.routes}
    assert "/chat/sessions/{session_id}/directive-drafts" in paths
    assert "/chat/directive-drafts/{draft_id}" in paths
    assert "/chat/directive-drafts/{draft_id}/events" in paths


def test_directive_model_setting_routes_are_not_duplicated() -> None:
    from app.api.directives import router as directives_router

    paths = [route.path for route in directives_router.routes]
    assert paths.count("/settings/directive-models") == 2  # one GET and one PUT


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

    monkeypatch.setattr(service, "_call_configured_model", fail_generation)
    monkeypatch.setattr(
        service,
        "_get_directive_model_config",
        AsyncMock(return_value={"models": ["claude-sonnet-5"], "timeout_seconds": 1, "max_tokens": 2000}),
    )
    content, mode, model_used = await service.generate_directive_content(_source(), "medium")

    assert mode == "fallback"
    assert model_used is None
    assert "TASK_ID: AADS-DRAFT" in content
    assert validate_directive(content)[0]


@pytest.mark.asyncio
async def test_generation_continues_after_invalid_model_output(monkeypatch) -> None:
    valid = build_fallback_directive(_source(), "medium")
    calls: list[str] = []

    async def generate(*, model_candidate: str, **_kwargs):
        calls.append(model_candidate)
        return "not a directive" if len(calls) == 1 else valid

    monkeypatch.setattr(service, "_call_configured_model", generate)
    monkeypatch.setattr(
        service,
        "_get_directive_model_config",
        AsyncMock(return_value={
            "models": ["claude-sonnet-5", "codex:gpt-5.6-terra"],
            "timeout_seconds": 1,
            "max_tokens": 2000,
        }),
    )

    content, mode, model_used = await service.generate_directive_content(_source(), "medium")

    assert calls == ["claude-sonnet-5", "codex:gpt-5.6-terra"]
    assert mode == "generated"
    assert model_used == "codex:gpt-5.6-terra"
    assert content == valid


@pytest.mark.asyncio
async def test_configured_codex_model_uses_cli_relay(monkeypatch) -> None:
    seen: dict[str, object] = {}

    async def relay(model, system, messages, **kwargs):
        seen.update({"model": model, "system": system, "messages": messages, **kwargs})
        yield {"type": "delta", "content": "relay output"}
        yield {"type": "done"}

    monkeypatch.setattr("app.services.model_selector._stream_codex_relay", relay)

    content = await service._call_configured_model(
        model_candidate="codex:gpt-5.6-tela",
        prompt="make a directive",
        max_tokens=2000,
        system="system",
        tenant_id=None,
        user_id=None,
    )

    assert content == "relay output"
    assert seen["model"] == "gpt-5.6-terra"
    assert seen["tools"] is None


def test_directive_model_alias_is_canonicalized_and_deduplicated() -> None:
    assert service.normalize_directive_models(
        ["codex:gpt-5.6-tela", "codex:gpt-5.6-terra", ""]
    ) == ["codex:gpt-5.6-terra"]


@pytest.mark.asyncio
async def test_configured_claude_model_uses_cli_relay(monkeypatch) -> None:
    seen: dict[str, object] = {}

    async def relay(model, system, messages, **kwargs):
        seen.update({"model": model, "system": system, "messages": messages, **kwargs})
        yield {"type": "delta", "content": "claude output"}
        yield {"type": "done"}

    monkeypatch.setattr("app.services.model_selector._stream_cli_relay", relay)

    content = await service._call_configured_model(
        model_candidate="claude-sonnet-5",
        prompt="make a directive",
        max_tokens=2000,
        system="system",
        tenant_id=None,
        user_id=None,
    )

    assert content == "claude output"
    assert seen["model"] == "claude-sonnet-5"
    assert seen["tools"] is None


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
