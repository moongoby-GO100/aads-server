from pathlib import Path

import pytest
from fastapi import HTTPException

from app.api import files


def test_verified_tmp_legacy_link_resolves_only_to_persistent_copy(monkeypatch, tmp_path):
    persistent = tmp_path / "docs" / "chat" / "aads-chat-continuity-directive-review.md"
    persistent.parent.mkdir(parents=True)
    persistent.write_text("same content", encoding="utf-8")
    monkeypatch.setitem(
        files.LEGACY_PATH_ALIASES,
        "/tmp/aads-chat-continuity-directive-review.md",
        persistent,
    )
    monkeypatch.setattr(files, "ALLOWED_ROOTS", (str(tmp_path / "docs"),))

    assert files._resolve("/tmp/aads-chat-continuity-directive-review.md") == persistent.resolve()


def test_unknown_tmp_file_is_not_resolved_by_matching_filename(monkeypatch, tmp_path):
    export_dir = tmp_path / "aads_exports"
    export_dir.mkdir()
    (export_dir / "report.md").write_text("another session", encoding="utf-8")
    monkeypatch.setattr(files, "FALLBACK_DIRS", (export_dir,))
    monkeypatch.setattr(files, "ALLOWED_ROOTS", (str(export_dir),))

    with pytest.raises(HTTPException) as exc:
        files._resolve("/tmp/unowned/report.md")

    assert exc.value.status_code == 404


def test_release_docs_precede_separate_host_runtime_mount():
    candidates = files._candidates("docs/goals/example/PRD.md")

    assert candidates.index(Path("/app/docs/goals/example/PRD.md")) < candidates.index(
        Path("/host/aads-server/docs/goals/example/PRD.md")
    )
    assert files._is_allowed(Path("/host/aads-server/docs/generated.md"))


def test_compose_does_not_shadow_release_docs_or_reports():
    compose = (Path(__file__).parents[2] / "docker-compose.prod.yml").read_text(
        encoding="utf-8"
    )

    assert "/root/aads/aads-server/docs:/app/docs" not in compose
    assert "/root/aads/aads-server/reports:/app/reports" not in compose
    assert compose.count("/root/aads/aads-server/docs:/host/aads-server/docs:ro") == 2
    assert compose.count("/root/aads/aads-server/reports:/host/aads-server/reports:ro") == 2


@pytest.mark.parametrize(
    "raw",
    [
        "/tmp/../etc/passwd",
        "sandbox:/mnt/data/missing.md",
        "/tmp/없는 파일.md",
    ],
)
def test_unverified_or_traversing_paths_are_rejected(raw):
    with pytest.raises(HTTPException) as exc:
        files._resolve(raw)

    assert exc.value.status_code in {400, 404}
