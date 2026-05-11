from __future__ import annotations

import os
import json
from datetime import datetime, timezone
from pathlib import Path
from uuid import UUID

import pytest

os.environ.setdefault("JWT_SECRET_KEY", "test-secret")

from app.api import design_modifications
from app.services.design_context_pack_service import build_design_context_pack

SCREEN_ID = "11111111-1111-1111-1111-111111111111"
REQUEST_ID = "22222222-2222-2222-2222-222222222222"
SNAPSHOT_ID = "33333333-3333-3333-3333-333333333333"
DECISION_ID = "44444444-4444-4444-4444-444444444444"
CONTEXT_PACK_ID = "55555555-5555-5555-5555-555555555555"


class _AcquireCtx:
    def __init__(self, conn):
        self._conn = conn

    async def __aenter__(self):
        return self._conn

    async def __aexit__(self, exc_type, exc, tb):
        return False


class _Pool:
    def __init__(self, conn):
        self._conn = conn

    def acquire(self):
        return _AcquireCtx(self._conn)


class _DesignConn:
    def __init__(self):
        self.ts = datetime(2026, 5, 11, 9, 0, tzinfo=timezone.utc)
        self.inserted_context_pack = None

    async def fetch(self, query: str, *args):
        if "FROM design_screens s" in query:
            return [
                {
                    "id": SCREEN_ID,
                    "project_key": "AADS",
                    "route": "/admin/tasks",
                    "name": "Task Monitor",
                    "purpose": "Pipeline Runner 작업 상태 확인",
                    "primary_actions": ["filter", "approve"],
                    "component_paths": ["src/app/admin/tasks/page.tsx"],
                    "metadata": {"viewport": ["390x844", "1440x900"]},
                    "created_at": self.ts,
                    "updated_at": self.ts,
                }
            ]
        if "FROM design_modification_requests r" in query and "LEFT JOIN design_screens s" in query:
            return [
                {
                    "id": REQUEST_ID,
                    "project_key": "AADS",
                    "screen_id": SCREEN_ID,
                    "user_prompt": "상단 summary 카드 높이를 줄이고 목록을 더 많이 보여주세요.",
                    "acceptance_criteria": [
                        "1440px에서 row 8개 이상",
                        "390px에서 버튼 겹침 없음",
                    ],
                    "request_type": "spacing_density",
                    "status": "ready",
                    "created_at": self.ts,
                    "updated_at": self.ts,
                    "screen_route": "/admin/tasks",
                    "screen_name": "Task Monitor",
                    "context_pack_count": 2,
                    "latest_context_pack_created_at": self.ts,
                }
            ]
        if "FROM design_visual_snapshots" in query:
            return [
                {
                    "id": SNAPSHOT_ID,
                    "request_id": REQUEST_ID,
                    "phase": "before",
                    "viewport": "1440x900",
                    "image_url": "design_screenshots/request-1/before.png",
                    "dom_summary": {"headline": "Pending 12"},
                    "captured_at": self.ts,
                }
            ]
        if "FROM design_decisions" in query:
            return [
                {
                    "id": DECISION_ID,
                    "project_key": "AADS",
                    "screen_id": SCREEN_ID,
                    "subject": "운영 대시보드 밀도",
                    "decision": "상단 요약 카드는 compact 유지",
                    "rationale": "첫 화면에서 row 수를 확보하기 위해",
                    "applies_to": "screen",
                    "confidence": 0.92,
                    "supersedes_id": None,
                    "metadata": {"source": "ceo_review"},
                    "created_at": self.ts,
                    "updated_at": self.ts,
                }
            ]
        if "FROM design_context_packs cp" in query:
            return [
                {
                    "id": CONTEXT_PACK_ID,
                    "request_id": REQUEST_ID,
                    "sources": ["screenshot", "DESIGN.md", "DOM"],
                    "missing_context": ["fixture_data"],
                    "prompt_chars": 1480,
                    "created_at": self.ts,
                }
            ]
        return []

    async def fetchrow(self, query: str, *args):
        if "INSERT INTO design_context_packs" in query:
            self.inserted_context_pack = {
                "request_id": str(args[0]),
                "context": json.loads(args[1]),
                "sources": json.loads(args[2]),
                "missing_context": json.loads(args[3]),
                "prompt_chars": args[4],
            }
            return {
                "id": CONTEXT_PACK_ID,
                "created_at": self.ts,
            }
        if "FROM design_modification_requests r" in query:
            return {
                "id": REQUEST_ID,
                "project_key": "AADS",
                "screen_id": SCREEN_ID,
                "user_prompt": "상단 summary 카드 높이를 줄이고 목록을 더 많이 보여주세요.",
                "normalized_card": {
                    "target": "/admin/tasks",
                    "goal": "첫 화면에서 목록을 더 많이 보기",
                },
                "request_type": "spacing_density",
                "allowed_scope": {"components": ["SummaryCards", "TaskTableHeader"]},
                "forbidden_scope": {"components": ["Sidebar"], "apis": ["task approve action"]},
                "acceptance_criteria": [
                    "1440px에서 row 8개 이상",
                    "390px에서 버튼 겹침 없음",
                ],
                "status": "ready",
                "created_at": self.ts,
                "updated_at": self.ts,
                "screen_route": "/admin/tasks",
                "screen_name": "Task Monitor",
                "screen_purpose": "Pipeline Runner 작업 상태 확인",
                "screen_primary_actions": ["filter", "approve"],
                "screen_component_paths": ["src/app/admin/tasks/page.tsx"],
                "screen_metadata": {
                    "viewport": ["390x844", "1440x900"],
                    "design_tokens": {"spacing": "compact", "radius": "8px"},
                    "audit_summary": {"raw_color_count": 0},
                },
                "context_pack_count": 2,
            }
        if "FROM design_context_packs cp" in query and "JOIN design_modification_requests r" in query:
            return {
                "id": CONTEXT_PACK_ID,
                "request_id": REQUEST_ID,
                "context": {
                    "project": {"key": "AADS"},
                    "screen": {"route": "/admin/tasks"},
                    "constraints": {"forbidden_scope": ["Sidebar"]},
                },
                "sources": ["screenshot", "DESIGN.md", "DOM"],
                "missing_context": ["fixture_data"],
                "prompt_chars": 1480,
                "created_at": self.ts,
                "project_key": "AADS",
                "screen_id": SCREEN_ID,
                "request_type": "spacing_density",
                "status": "ready",
                "user_prompt": "상단 summary 카드 높이를 줄이고 목록을 더 많이 보여주세요.",
                "screen_route": "/admin/tasks",
                "screen_name": "Task Monitor",
            }
        return None

    async def fetchval(self, query: str, *args):
        if "FROM design_screens" in query:
            return 1
        if "FROM design_modification_requests" in query:
            return 1
        if "FROM design_context_packs" in query:
            return 1
        return 0


def _current_user():
    return {"user_id": "ceo-1", "email": "ceo@aads.dev", "is_admin": True}


@pytest.mark.asyncio
async def test_list_design_screens(monkeypatch):
    monkeypatch.setattr(design_modifications, "get_pool", lambda: _Pool(_DesignConn()))
    response = await design_modifications.list_design_screens(
        "AADS",
        limit=50,
        offset=0,
        current_user=_current_user(),
    )

    assert response.project_key == "AADS"
    assert response.total == 1
    assert response.screens[0].route == "/admin/tasks"
    assert response.screens[0].component_paths == ["src/app/admin/tasks/page.tsx"]


@pytest.mark.asyncio
async def test_get_design_modification_request_detail(monkeypatch):
    monkeypatch.setattr(design_modifications, "get_pool", lambda: _Pool(_DesignConn()))
    response = await design_modifications.get_design_modification_request(
        UUID(REQUEST_ID),
        current_user=_current_user(),
    )

    assert response.request.screen.route == "/admin/tasks"
    assert response.request.context_pack_count == 2
    assert response.snapshots[0].phase == "before"
    assert response.decisions[0].subject == "운영 대시보드 밀도"


@pytest.mark.asyncio
async def test_list_design_modification_requests(monkeypatch):
    monkeypatch.setattr(design_modifications, "get_pool", lambda: _Pool(_DesignConn()))
    response = await design_modifications.list_design_modification_requests(
        "AADS",
        status="ready",
        screen_id=None,
        limit=50,
        offset=0,
        current_user=_current_user(),
    )

    assert response.project_key == "AADS"
    assert response.status == "ready"
    assert response.requests[0].screen_route == "/admin/tasks"
    assert response.requests[0].context_pack_count == 2


@pytest.mark.asyncio
async def test_context_pack_list_and_preview(monkeypatch):
    monkeypatch.setattr(design_modifications, "get_pool", lambda: _Pool(_DesignConn()))
    list_response = await design_modifications.list_design_context_packs(
        UUID(REQUEST_ID),
        limit=20,
        offset=0,
        current_user=_current_user(),
    )
    preview_response = await design_modifications.preview_design_context_pack(
        UUID(CONTEXT_PACK_ID),
        current_user=_current_user(),
    )

    assert list_response.total == 1
    assert list_response.context_packs[0].source_count == 3
    assert list_response.context_packs[0].missing_context_count == 1

    assert preview_response.context_pack.project_key == "AADS"
    assert preview_response.context_pack.context["screen"]["route"] == "/admin/tasks"
    assert preview_response.context_pack.source_count == 3


@pytest.mark.asyncio
async def test_generate_context_pack_preview_and_persist(monkeypatch):
    conn = _DesignConn()
    monkeypatch.setattr(design_modifications, "get_pool", lambda: _Pool(conn))

    preview_response = await design_modifications.preview_generated_design_context_pack(
        UUID(REQUEST_ID),
        options=design_modifications.DesignContextPackBuildRequest(),
        current_user=_current_user(),
    )
    persist_response = await design_modifications.generate_design_context_pack(
        UUID(REQUEST_ID),
        options=design_modifications.DesignContextPackBuildRequest(),
        current_user=_current_user(),
    )

    preview_pack = preview_response.context_pack
    assert preview_pack.persisted is False
    assert preview_pack.context["route"] == "/admin/tasks"
    assert preview_pack.context["component_paths"] == ["src/app/admin/tasks/page.tsx"]
    assert preview_pack.context["target_state"]["request_type"] == "spacing_density"
    assert preview_pack.context["locked_constraints"]["forbidden_scope"]["components"] == ["Sidebar"]
    assert preview_pack.context["token_style_evidence"]["tokens"][0]["value"]["spacing"] == "compact"
    assert preview_pack.context["acceptance_checks"][0]["source"] == "request.acceptance_criteria"

    persisted_pack = persist_response.context_pack
    assert persisted_pack.persisted is True
    assert persisted_pack.id == CONTEXT_PACK_ID
    assert conn.inserted_context_pack["context"]["pack_version"] == "design-context-pack/v1"
    assert conn.inserted_context_pack["prompt_chars"] == persisted_pack.prompt_chars


def test_build_design_context_pack_filters_secrets_and_sensitive_paths():
    row = {
        "id": REQUEST_ID,
        "project_key": "AADS",
        "screen_id": SCREEN_ID,
        "user_prompt": "카드 간격을 줄여주세요. api_key=verysecretvalue",
        "normalized_card": {
            "target": "/admin/tasks",
            "snippets": [
                {"path": ".env.local", "content": "password=hidden"},
                {"path": "src/app/admin/tasks/page.tsx", "content": "className='gap-2'"},
            ],
        },
        "request_type": "spacing_density",
        "allowed_scope": {"components": ["SummaryCards"]},
        "forbidden_scope": {"credential": "do-not-include"},
        "acceptance_criteria": ["No overlap"],
        "status": "ready",
        "screen_route": "/admin/tasks",
        "screen_name": "Task Monitor",
        "screen_purpose": "Pipeline Runner 작업 상태 확인",
        "screen_primary_actions": ["filter"],
        "screen_component_paths": ["src/app/admin/tasks/page.tsx"],
        "screen_metadata": {
            "design_tokens": {"spacing": "compact"},
            "credentials": {"token": "secret"},
        },
    }

    result = build_design_context_pack(row, [], [])
    dumped = json.dumps(result.context, ensure_ascii=False)

    assert "verysecretvalue" not in dumped
    assert ".env" not in dumped
    assert "hidden" not in dumped
    assert "do-not-include" not in dumped
    assert result.context["target_state"]["normalized_card"]["snippets"][0] == "[omitted:sensitive_source]"
    assert result.context["target_state"]["user_prompt"] == "[redacted:sensitive]"
    assert result.context["safety_filters"]["redacted_count"] >= 2


def test_build_design_context_pack_handles_missing_snapshot_gracefully():
    row = {
        "id": REQUEST_ID,
        "project_key": "AADS",
        "screen_id": SCREEN_ID,
        "user_prompt": "목록을 더 많이 보여주세요.",
        "normalized_card": {"target": "/admin/tasks"},
        "request_type": "spacing_density",
        "allowed_scope": {},
        "forbidden_scope": {},
        "acceptance_criteria": ["1440px에서 row 8개 이상"],
        "status": "ready",
        "screen_route": "/admin/tasks",
        "screen_name": "Task Monitor",
        "screen_purpose": "Pipeline Runner 작업 상태 확인",
        "screen_primary_actions": ["filter"],
        "screen_component_paths": ["src/app/admin/tasks/page.tsx"],
        "screen_metadata": {},
    }

    result = build_design_context_pack(row, [], [])

    assert result.context["current_state"]["snapshots"] == []
    assert any(item["kind"] == "visual_snapshots" for item in result.missing_context)
    assert any("No visual snapshot metadata" in note for note in result.context["risk_notes"])


def test_design_modification_migration_contains_required_schema():
    content = Path("migrations/084_design_modification_studio.sql").read_text(encoding="utf-8")

    assert "CREATE TABLE IF NOT EXISTS design_screens" in content
    assert "CREATE TABLE IF NOT EXISTS design_modification_requests" in content
    assert "CREATE TABLE IF NOT EXISTS design_context_packs" in content
    assert "CREATE TABLE IF NOT EXISTS design_visual_snapshots" in content
    assert "CREATE TABLE IF NOT EXISTS design_decisions" in content
    assert "design_modification_requests_status_check" in content
    assert "idx_design_context_packs_request_created" in content
