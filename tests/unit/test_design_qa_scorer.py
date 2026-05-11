from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from uuid import UUID

import pytest

from app.services import design_audit_service, design_qa_scorer

REQUEST_ID = UUID("77777777-7777-7777-7777-777777777777")
SCREEN_ID = UUID("88888888-8888-8888-8888-888888888888")


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


class _ScoringConn:
    def __init__(self):
        self.ts = datetime(2026, 5, 11, 9, 0, tzinfo=timezone.utc)
        self.execute_calls: list[tuple[str, tuple]] = []

    async def fetchrow(self, query: str, *args):
        if "FROM design_modification_requests r" in query:
            return {
                "id": REQUEST_ID,
                "project_key": "AADS",
                "screen_id": SCREEN_ID,
                "user_prompt": "Tighten spacing and preserve existing tokens.",
                "normalized_card": {"goal": "show more rows"},
                "request_type": "spacing_density",
                "allowed_scope": {"component_paths": ["src/app/admin/tasks/page.tsx"]},
                "forbidden_scope": {"components": ["Sidebar"]},
                "acceptance_criteria": [
                    "Desktop should show more rows",
                    "Mobile should avoid text overlap",
                ],
                "status": "review",
                "created_at": self.ts,
                "updated_at": self.ts,
                "screen_route": "/admin/tasks",
                "screen_name": "Task Monitor",
                "screen_purpose": "Track jobs",
                "screen_component_paths": ["src/app/admin/tasks/page.tsx"],
                "screen_metadata": {"viewport": ["390x844", "1440x900"]},
            }
        if "FROM design_context_packs" in query:
            return {
                "id": UUID("99999999-9999-9999-9999-999999999999"),
                "context": {
                    "screen": {"component_paths": ["src/app/admin/tasks/page.tsx"]},
                    "design": {"source_files": ["src/app/admin/tasks/page.tsx"]},
                },
                "sources": ["DESIGN.md", "before-screenshot", "decision"],
                "missing_context": [],
                "prompt_chars": 1800,
                "created_at": self.ts,
            }
        return None

    async def fetch(self, query: str, *args):
        if "FROM design_visual_snapshots" in query:
            return [
                {
                    "id": UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"),
                    "request_id": REQUEST_ID,
                    "phase": "before",
                    "viewport": "390x844",
                    "image_url": "before.png",
                    "dom_summary": {"headline": "Pending 12"},
                    "captured_at": self.ts,
                },
                {
                    "id": UUID("bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"),
                    "request_id": REQUEST_ID,
                    "phase": "after",
                    "viewport": "1440x900",
                    "image_url": "after.png",
                    "dom_summary": {"headline": "Pending 12", "rows": 8},
                    "captured_at": self.ts,
                },
            ]
        if "FROM design_decisions" in query:
            return [
                {
                    "id": UUID("cccccccc-cccc-cccc-cccc-cccccccccccc"),
                    "applies_to": "screen",
                    "metadata": {"source": "ceo_review"},
                }
            ]
        return []

    async def execute(self, query: str, *args):
        self.execute_calls.append((query, args))
        return "INSERT 0 1"


def test_check_token_compliance_detects_required_violations(tmp_path):
    source_file = tmp_path / "page.tsx"
    source_file.write_text(
        """
        <button className="px-4 py-2 text-[#123456]">Save</button>
        <button className="px-4 py-2 text-[#123456]">Save</button>
        <button className="px-4 py-2 text-[#123456]">🚨 Save</button>
        <div style={{ fontSize: "2vw" }}>Viewport title</div>
        """,
        encoding="utf-8",
    )

    report = design_qa_scorer.check_token_compliance([source_file.as_posix()])
    by_kind = report["summary"]["by_kind"]

    assert report["files_scanned"] == 1
    assert report["compliant"] is False
    assert by_kind["raw_hex_color"] == 3
    assert by_kind["emoji_icon"] == 1
    assert by_kind["viewport_font_scaling"] == 1
    assert by_kind["repeated_button_pattern"] == 1


@pytest.mark.asyncio
async def test_score_modification_persists_score(monkeypatch, tmp_path):
    project_root = tmp_path / "dashboard"
    source_dir = project_root / "src" / "app" / "admin" / "tasks"
    source_dir.mkdir(parents=True)
    (source_dir / "page.tsx").write_text(
        """
        <button className="px-4 py-2 text-[#123456]">Save</button>
        <button className="px-4 py-2 text-[#123456]">Save</button>
        <button className="px-4 py-2 text-[#123456]">🚨 Save</button>
        <div className="focus-visible:outline-none" aria-label="Save">
          <img alt="Task chart" src="/chart.png" />
          <div style={{ fontSize: "2vw" }}>Viewport title</div>
        </div>
        """,
        encoding="utf-8",
    )

    conn = _ScoringConn()
    monkeypatch.setitem(design_audit_service.ALLOWED_PROJECT_ROOTS, "AADS", project_root)
    monkeypatch.setattr(design_qa_scorer, "get_pool", lambda: _Pool(conn))

    result = await design_qa_scorer.score_modification(REQUEST_ID)

    assert result["request_id"] == str(REQUEST_ID)
    assert result["project_key"] == "AADS"
    assert result["scoring_version"] == "static-v1"
    assert result["rating"] in {
        "approval_candidate",
        "conditional_approval",
        "needs_revision",
        "rejected",
    }
    assert result["token_compliance"]["summary"]["by_kind"]["raw_hex_color"] == 3
    assert result["token_compliance"]["summary"]["by_kind"]["emoji_icon"] == 1
    assert result["token_compliance"]["summary"]["by_kind"]["viewport_font_scaling"] == 1
    assert result["axes"]["request_match"]["max_score"] == 25
    assert result["axes"]["responsive_stability"]["score"] < 15
    assert conn.execute_calls
    assert "INSERT INTO design_qa_scores" in conn.execute_calls[0][0]


def test_design_qa_score_service_reads_migration_schema():
    content = Path("migrations/085_design_qa_scores.sql").read_text(encoding="utf-8")

    assert "CREATE TABLE IF NOT EXISTS design_qa_scores" in content
    assert "score_details JSONB" in content
    assert "evidence JSONB" in content
