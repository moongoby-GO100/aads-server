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

    async def fetch(self, query, *_args):
        if "project_document_goal_links" in query:
            return getattr(self, "canonical_rows", [])
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


GOAL = "11111111-1111-4111-8111-111111111111"
TENANT_A = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
TENANT_B = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"


def _legacy(doc_id, kind, key, latest=True):
    return {
        "id": doc_id, "kind": kind, "doc_path": f"docs/{key}.md", "title": None,
        "note": None, "created_at": None, "document_key": key, "version": "1.0.0",
        "status": "active", "is_latest": latest, "change_summary": None,
        "supersedes_id": None, "updated_at": None, "version_count": 1,
    }


def _canon(kind, key, approved=True, project_key="AADS", title="정본 문서"):
    return {
        "head_id": f"head-{key}", "kind": kind, "document_key": key,
        "head_title": "head title", "approved_revision_id": "rev" if approved else None,
        "updated_at": "2026-10-22", "project_key": project_key, "created_at": None,
        "title": title, "version": "2.0.0", "version_count": 2,
    }


class _TenantCanonicalDB(_DocumentListDB):
    """Canonical links are visible only to the tenant that owns them."""

    def __init__(self, rows, canonical_by_tenant):
        super().__init__(rows)
        self.canonical_by_tenant = canonical_by_tenant
        self.canonical_queries = 0

    async def fetchval(self, query, *args):
        return 1 if args[1] == TENANT_A else None

    async def fetch(self, query, *args):
        if "project_document_goal_links" in query:
            self.canonical_queries += 1
            assert "LIMIT 200" in query
            assert "l.tenant_id = $1::uuid AND l.goal_id = $2::uuid" in query
            return self.canonical_by_tenant.get(args[0], [])
        return self.rows


def _call(monkeypatch, db, tenant=TENANT_A, **kwargs):
    from app.core import db_pool
    from app.routers import goals

    monkeypatch.setattr(db_pool, "get_pool", lambda: db)
    return asyncio.run(goals.goal_documents(GOAL, {"tenant": {"id": tenant}}, **kwargs))


def test_canonical_only_goal_gets_documents_and_design(monkeypatch):
    db = _TenantCanonicalDB([], {TENANT_A: [_canon("plan", "plan:a"), _canon("prd", "prd:a", approved=False)]})
    result = _call(monkeypatch, db)

    assert db.canonical_queries == 1
    assert {d["document_key"] for d in result["documents"]} == {"plan:a", "prd:a"}
    plan = next(d for d in result["documents"] if d["kind"] == "plan")
    assert plan["source"] == "canonical" and plan["approved"] is True
    assert plan["doc_path"] is None and plan["is_latest"] is True
    assert plan["version"] == "2.0.0" and plan["project_key"] == "AADS"
    prd = next(d for d in result["documents"] if d["kind"] == "prd")
    assert prd["approved"] is False
    assert result["has_design"] is True and result["missing"] == []
    assert result["has_history"] is True


def test_canonical_spec_counts_as_requirement(monkeypatch):
    db = _TenantCanonicalDB([], {TENANT_A: [_canon("plan", "plan:a"), _canon("spec", "spec:a")]})
    result = _call(monkeypatch, db)

    assert result["has_design"] is True and result["missing"] == []


def test_goal_with_both_keeps_legacy_and_canonical(monkeypatch):
    db = _TenantCanonicalDB([_legacy(1, "plan", "plan:old")], {TENANT_A: [_canon("prd", "prd:new")]})
    result = _call(monkeypatch, db)

    sources = {d["document_key"]: d["source"] for d in result["documents"]}
    assert sources == {"plan:old": "legacy", "prd:new": "canonical"}
    assert result["has_design"] is True


def test_duplicate_document_key_prefers_canonical(monkeypatch):
    db = _TenantCanonicalDB(
        [_legacy(1, "plan", "plan:dup"), _legacy(2, "plan", "plan:dup", latest=False)],
        {TENANT_A: [_canon("plan", "plan:dup")]},
    )
    result = _call(monkeypatch, db)

    assert [(d["source"], d["document_key"]) for d in result["documents"]] == [("canonical", "plan:dup")]
    history = result["document_history"]
    assert len(history) == 3
    assert [d["source"] for d in history].count("legacy") == 2
    assert all(not d["is_latest"] for d in history if d["source"] == "legacy")
    assert "prd" in result["missing"]


def test_include_history_lists_shadowed_legacy(monkeypatch):
    db = _TenantCanonicalDB([_legacy(1, "plan", "plan:dup")], {TENANT_A: [_canon("plan", "plan:dup")]})
    result = _call(monkeypatch, db, include_history=True)

    assert len(result["documents"]) == 2


def test_other_tenant_cannot_see_canonical_documents(monkeypatch):
    db = _TenantCanonicalDB([], {TENANT_A: [_canon("plan", "plan:a")]})

    with pytest.raises(HTTPException) as exc:
        _call(monkeypatch, db, tenant=TENANT_B)

    assert exc.value.status_code == 404
    assert db.canonical_queries == 0
