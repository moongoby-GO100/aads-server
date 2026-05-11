from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from uuid import UUID

import pytest

os.environ.setdefault("JWT_SECRET_KEY", "test-secret")

from app.services import design_context_builder as builder


SCREEN_ID = "11111111-1111-1111-1111-111111111111"
REQUEST_ID = "22222222-2222-2222-2222-222222222222"
SNAPSHOT_ID = "33333333-3333-3333-3333-333333333333"
TOKEN_SET_ID = "44444444-4444-4444-4444-444444444444"
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


class _BuilderConn:
    def __init__(self, repo_path: str, *, missing: bool = False):
        self.repo_path = repo_path
        self.missing = missing
        self.ts = datetime(2026, 5, 11, 9, 0, tzinfo=timezone.utc)
        self.inserted_context = None
        self.inserted_sources = None
        self.inserted_missing_context = None
        self.inserted_prompt_chars = None

    async def fetchrow(self, query: str, *args):
        if "FROM design_modification_requests r" in query:
            return self._request_row()
        if "FROM design_token_sets" in query:
            if self.missing:
                return None
            return {
                "id": TOKEN_SET_ID,
                "version": "2026.05",
                "mode": "project",
                "tokens": {"color": {"surface": "var(--surface)"}, "spacing": {"card": "8px"}},
                "created_by": "system",
                "created_at": self.ts,
            }
        if "INSERT INTO design_context_packs" in query:
            self.inserted_context = json.loads(args[1])
            self.inserted_sources = json.loads(args[2])
            self.inserted_missing_context = json.loads(args[3])
            self.inserted_prompt_chars = args[4]
            return {
                "id": CONTEXT_PACK_ID,
                "request_id": args[0],
                "context": self.inserted_context,
                "sources": self.inserted_sources,
                "missing_context": self.inserted_missing_context,
                "prompt_chars": self.inserted_prompt_chars,
                "created_at": self.ts,
            }
        return None

    async def fetch(self, query: str, *args):
        if "FROM design_visual_snapshots" not in query or self.missing:
            return []
        return [
            {
                "id": SNAPSHOT_ID,
                "viewport": "1440x900",
                "image_url": "design_screenshots/request-1/before.png",
                "dom_summary": {"headline": "Pending 12"},
                "captured_at": self.ts,
            }
        ]

    def _request_row(self):
        row = {
            "id": REQUEST_ID,
            "project_key": "AADS",
            "screen_id": SCREEN_ID,
            "user_prompt": "상단 summary 카드 높이를 줄이고 목록을 더 많이 보여주세요.",
            "normalized_card": {"target": "/admin/tasks", "goal": "first viewport density"},
            "request_type": "spacing_density",
            "allowed_scope": {"files": ["src/app/admin/tasks/page.tsx"]},
            "forbidden_scope": {"components": ["Sidebar"]},
            "acceptance_criteria": ["1440px에서 row 8개 이상"],
            "status": "draft",
            "created_at": self.ts,
            "updated_at": self.ts,
            "project_project_key": "AADS",
            "project_display_name": "AADS",
            "project_frontend_stack": "Next.js",
            "project_adapter_key": "legacy-css",
            "project_repo_path": self.repo_path,
            "project_status": "active",
            "project_metadata": {"auth_token": "secret-token", "tone": "operational"},
            "project_created_at": self.ts,
            "project_updated_at": self.ts,
            "screen_route": "/admin/tasks",
            "screen_name": "Task Monitor",
            "screen_purpose": "Pipeline Runner 작업 상태 확인",
            "screen_primary_actions": ["filter", "approve"],
            "screen_component_paths": ["src/app/admin/tasks/page.tsx"],
            "screen_metadata": {"viewport_matrix": ["390x844", "1440x900"]},
        }
        if not self.missing:
            return row
        row.update(
            {
                "screen_id": None,
                "allowed_scope": {},
                "forbidden_scope": {},
                "acceptance_criteria": [],
                "project_repo_path": "",
                "screen_route": None,
                "screen_name": None,
                "screen_purpose": None,
                "screen_primary_actions": None,
                "screen_component_paths": [],
                "screen_metadata": {},
            }
        )
        return row


@pytest.mark.asyncio
async def test_build_context_pack_collects_required_context_and_redacts_secrets(monkeypatch, tmp_path):
    design_md = tmp_path / "DESIGN.md"
    design_md.write_text(
        "Density: compact\nSECRET_VALUE=super-secret\nExample token sk-supersecretvalue123456\n",
        encoding="utf-8",
    )
    conn = _BuilderConn(str(tmp_path))
    monkeypatch.setattr(builder, "get_pool", lambda: _Pool(conn))

    result = await builder.build_context_pack(UUID(REQUEST_ID))

    context = result["context_pack"]["context"]
    assert context["project"]["key"] == "AADS"
    assert context["project"]["metadata"]["auth_token"] == "[redacted]"
    assert context["screen"]["route"] == "/admin/tasks"
    assert context["current_context"]["component_path_candidates"] == ["src/app/admin/tasks/page.tsx"]
    assert context["current_context"]["baseline_screenshot_url"] == "design_screenshots/request-1/before.png"
    assert context["current_context"]["viewport_matrix"] == ["390x844", "1440x900"]
    assert context["allowed_scope"]["files"] == ["src/app/admin/tasks/page.tsx"]
    assert context["forbidden_scope"]["components"] == ["Sidebar"]
    assert context["acceptance_criteria"] == ["1440px에서 row 8개 이상"]
    assert context["design_contract"]["design_tokens"]["version"] == "2026.05"

    design_content = context["design_contract"]["design_md"]["content"]
    assert "super-secret" not in design_content
    assert "sk-supersecretvalue123456" not in design_content
    assert "SECRET_VALUE=[redacted]" in design_content
    assert result["missing_context_count"] == 0
    assert conn.inserted_context == context
    assert conn.inserted_prompt_chars == result["context_pack"]["prompt_chars"]


@pytest.mark.asyncio
async def test_build_context_pack_persists_missing_context(monkeypatch, tmp_path):
    conn = _BuilderConn(str(tmp_path), missing=True)
    monkeypatch.setattr(builder, "get_pool", lambda: _Pool(conn))

    result = await builder.build_context_pack(UUID(REQUEST_ID))

    missing_keys = {item["key"] for item in result["context_pack"]["missing_context"]}
    assert {
        "screen",
        "component_paths",
        "baseline_screenshot_url",
        "design_md",
        "design_tokens",
        "allowed_scope",
        "forbidden_scope",
        "acceptance_criteria",
    }.issubset(missing_keys)
    assert result["context_pack"]["context"]["screen"] is None
    assert result["context_pack"]["context"]["current_context"]["viewport_matrix"] == builder.DEFAULT_VIEWPORT_MATRIX
    assert conn.inserted_missing_context == result["context_pack"]["missing_context"]
