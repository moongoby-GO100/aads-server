from pathlib import Path

import pytest
from fastapi import HTTPException

from app.routers.goals import GoalDocRequest, _goal_document_identity


def test_version_folder_links_plan_revisions_to_one_document_key():
    old = GoalDocRequest(kind="plan", doc_path="docs/goals/x/v1.0.0/PLAN.md")
    new = GoalDocRequest(kind="plan", doc_path="docs/goals/x/v1.1.0/PLAN.md")

    assert _goal_document_identity(old, old.doc_path) == ("plan:plan.md", "1.0.0")
    assert _goal_document_identity(new, new.doc_path) == ("plan:plan.md", "1.1.0")


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
