import asyncio
from pathlib import Path

import pytest
from fastapi import HTTPException

from app.routers.goals import (
    GoalDocRequest,
    _goal_document_identity,
)


class _DocumentUpsertDB:
    """Records the conflict key used by the document registration route."""

    def __init__(self):
        self.documents = {}

    def acquire(self):
        return self

    def transaction(self):
        return self

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_exc):
        return False

    async def fetchval(self, query, *_args):
        if "FROM goals" in query:
            return True
        return None

    async def execute(self, *_args):
        return "UPDATE 0"

    async def fetchrow(self, query, *args):
        assert "ON CONFLICT (goal_id, doc_path)" in query
        goal_id, kind, doc_path, title, note = args[:5]
        document_key, version, status, is_latest, change_summary, supersedes_id = args[6:]
        row = self.documents.setdefault(
            (goal_id, doc_path),
            {"id": f"document-{len(self.documents) + 1}"},
        )
        row.update(
            kind=kind,
            doc_path=doc_path,
            title=title,
            note=note,
            document_key=document_key,
            version=version,
            status=status,
            is_latest=is_latest,
            change_summary=change_summary,
            supersedes_id=supersedes_id,
        )
        return row


def test_version_folder_links_plan_revisions_to_one_document_key():
    old = GoalDocRequest(kind="plan", doc_path="docs/goals/x/v1.0.0/PLAN.md")
    new = GoalDocRequest(kind="plan", doc_path="docs/goals/x/v1.1.0/PLAN.md")

    old_key, old_version = _goal_document_identity(old, old.doc_path)
    new_key, new_version = _goal_document_identity(new, new.doc_path)

    assert old_key == new_key
    assert old_version == "1.0.0"
    assert new_version == "1.1.0"


def test_versioned_documents_with_matching_filenames_keep_distinct_keys():
    first = GoalDocRequest(kind="plan", doc_path="docs/goals/one/v1.0.0/PLAN.md")
    second = GoalDocRequest(kind="plan", doc_path="docs/goals/two/v1.0.0/PLAN.md")

    first_key, _ = _goal_document_identity(first, first.doc_path)
    second_key, _ = _goal_document_identity(second, second.doc_path)

    assert first_key != second_key


def test_document_registration_normalizes_absolute_path_before_upsert(monkeypatch, tmp_path):
    from app.core import db_pool
    from app.routers import goals

    repo_root = tmp_path / "aads-server"
    relative_path = "docs/goals/prd.md"
    db = _DocumentUpsertDB()
    context = {"tenant": {"id": "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"}}
    goal_id = "11111111-1111-4111-8111-111111111111"
    monkeypatch.setattr(goals, "_GOAL_DOCUMENT_REPO_ROOT", repo_root)
    monkeypatch.setattr(db_pool, "get_pool", lambda: db)

    absolute_result = asyncio.run(goals.add_goal_document(
        goal_id,
        GoalDocRequest(doc_path=str(repo_root / relative_path)),
        context,
    ))
    relative_result = asyncio.run(goals.add_goal_document(
        goal_id,
        GoalDocRequest(doc_path=relative_path),
        context,
    ))

    assert absolute_result["doc_path"] == relative_path
    assert relative_result["doc_path"] == relative_path
    assert list(db.documents) == [(goal_id, relative_path)]


def test_unversioned_documents_do_not_collapse_into_one_kind():
    first = GoalDocRequest(kind="reference", doc_path="docs/a.md")
    second = GoalDocRequest(kind="reference", doc_path="docs/b.md")

    first_key, first_version = _goal_document_identity(first, first.doc_path)
    second_key, second_version = _goal_document_identity(second, second.doc_path)

    assert first_key != second_key
    assert first_version == second_version == "1.0.0"


def test_explicit_document_key_links_nonstandard_paths():
    request = GoalDocRequest(
        kind="prd",
        doc_path="docs/PRD-current.md",
        document_key="prd:core",
        version="v2.3.4",
    )

    assert _goal_document_identity(request, request.doc_path) == ("prd:core", "2.3.4")


@pytest.mark.parametrize("version", ["2", "2.1", "latest", "1.2.3.4"])
def test_invalid_semver_fails_closed(version):
    request = GoalDocRequest(kind="prd", doc_path="docs/prd.md", version=version)

    with pytest.raises(HTTPException) as exc:
        _goal_document_identity(request, request.doc_path)

    assert exc.value.status_code == 400


def test_migration_enforces_single_latest_pointer():
    sql = Path("migrations/20260919_goal_document_versions.sql").read_text()

    assert "uq_goal_documents_latest" in sql
    assert "WHERE is_latest" in sql
    assert "supersedes_id" in sql


class _DocumentListDB:
    """Serves a fixed document list to the goal-documents view."""

    def __init__(self, rows):
        self.rows = rows

    def acquire(self):
        return self

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_exc):
        return False

    async def fetchval(self, query, *_args):
        if "FROM goals" in query:
            return 1
        return None

    async def fetch(self, *_args):
        return self.rows


def test_spec_kit_kinds_register_without_rejection(monkeypatch):
    """docs/specs 슬라이스는 spec/plan/tasks 세 벌이 정본이다.

    kind 목록에 spec·tasks 가 없던 동안 docs/specs 99건은 대장에 넣을 kind 가
    없어 0건이었다(DG1.1).
    """
    from app.core import db_pool
    from app.routers import goals

    db = _DocumentUpsertDB()
    context = {"tenant": {"id": "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"}}
    goal_id = "11111111-1111-4111-8111-111111111111"
    monkeypatch.setattr(db_pool, "get_pool", lambda: db)

    stored = [
        asyncio.run(goals.add_goal_document(
            goal_id,
            GoalDocRequest(kind=kind, doc_path=f"docs/specs/obys-v4/match/{name}"),
            context,
        ))["kind"]
        for kind, name in (
            ("spec", "spec.md"), ("plan", "plan.md"), ("tasks", "tasks.md")
        )
    ]

    assert stored == ["spec", "plan", "tasks"]


def test_unknown_kind_still_fails_closed(monkeypatch):
    from app.core import db_pool
    from app.routers import goals

    monkeypatch.setattr(db_pool, "get_pool", lambda: _DocumentUpsertDB())

    with pytest.raises(HTTPException) as exc:
        asyncio.run(goals.add_goal_document(
            "11111111-1111-4111-8111-111111111111",
            GoalDocRequest(kind="blueprint", doc_path="docs/x.md"),
            {"tenant": {"id": "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"}},
        ))

    assert exc.value.status_code == 400


def test_spec_fills_requirement_slot_so_slices_keep_prd_signal(monkeypatch):
    from app.core import db_pool
    from app.routers import goals

    def _row(doc_id, kind, name, key):
        return {
            "id": doc_id, "kind": kind,
            "doc_path": f"docs/specs/obys-v4/match/{name}",
            "title": None, "note": None, "created_at": None,
            "document_key": key, "version": "1.0.0", "status": "active",
            "is_latest": True, "change_summary": None, "supersedes_id": None,
            "updated_at": None, "version_count": 1,
        }

    rows = [_row(1, "spec", "spec.md", "spec:aaa"), _row(2, "plan", "plan.md", "plan:bbb")]
    monkeypatch.setattr(db_pool, "get_pool", lambda: _DocumentListDB(rows))

    result = asyncio.run(goals.goal_documents(
        "11111111-1111-4111-8111-111111111111",
        {"tenant": {"id": "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"}},
    ))

    assert result["missing"] == []
    assert result["has_design"] is True
