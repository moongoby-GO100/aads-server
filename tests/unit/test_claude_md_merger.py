from __future__ import annotations

import logging
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import app.core.claude_md_merger as claude_md_merger
from app.api import ops


def _write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


@pytest.fixture(autouse=True)
def _clear_claude_md_cache():
    claude_md_merger.invalidate_claude_md_cache()
    yield
    claude_md_merger.invalidate_claude_md_cache()


@pytest.mark.asyncio
async def test_build_merged_claude_md_skips_missing_sources_with_warning(tmp_path, monkeypatch, caplog):
    monkeypatch.setattr(claude_md_merger, "_REPO_ROOT", tmp_path)
    _write(tmp_path / "CLAUDE.md", "# Root Rules\nroot")
    _write(tmp_path / ".claude" / "rules" / "alpha.md", "rule alpha")
    _write(tmp_path / "docs" / "shared-lessons" / "INDEX.md", "shared lessons")

    caplog.set_level(logging.WARNING)
    content = await claude_md_merger.build_merged_claude_md(project="AADS")

    assert "# === CLAUDE.md ===" in content
    assert "# === .claude/rules/alpha.md ===" in content
    assert "# === docs/shared-lessons/INDEX.md ===" in content
    assert "# === docs/knowledge/AADS-KNOWLEDGE.md ===" not in content
    assert content.index("# === CLAUDE.md ===") < content.index("# === .claude/rules/alpha.md ===")
    assert content.index("# === .claude/rules/alpha.md ===") < content.index("# === docs/shared-lessons/INDEX.md ===")
    assert "claude_md_source_missing: docs/knowledge/AADS-KNOWLEDGE.md" in caplog.text


@pytest.mark.asyncio
async def test_build_merged_claude_md_uses_ttl_cache(tmp_path, monkeypatch):
    now = {"value": 1000.0}
    monkeypatch.setattr(claude_md_merger, "_REPO_ROOT", tmp_path)
    monkeypatch.setattr(claude_md_merger.time, "monotonic", lambda: now["value"])

    source = tmp_path / "CLAUDE.md"
    _write(source, "version one")

    first = await claude_md_merger.build_merged_claude_md(project="AADS")
    _write(source, "version two")

    second = await claude_md_merger.build_merged_claude_md(project="AADS")
    now["value"] += claude_md_merger._CACHE_TTL_SECONDS + 1
    third = await claude_md_merger.build_merged_claude_md(project="AADS")

    assert "version one" in first
    assert second == first
    assert "version two" in third
    assert claude_md_merger.get_merged_claude_md_sha256("AADS") is not None


@pytest.mark.asyncio
async def test_build_merged_claude_md_truncates_low_priority_sections_first(tmp_path, monkeypatch):
    monkeypatch.setattr(claude_md_merger, "_REPO_ROOT", tmp_path)
    monkeypatch.setattr(claude_md_merger, "_MAX_MERGED_BYTES", 1024)

    _write(tmp_path / "CLAUDE.md", "root\n" * 10)
    _write(tmp_path / ".claude" / "rules" / "alpha.md", "rule\n" * 10)
    _write(tmp_path / "docs" / "knowledge" / "AADS-KNOWLEDGE.md", "knowledge\n" * 45)
    _write(tmp_path / "docs" / "shared-lessons" / "INDEX.md", "shared\n" * 80)

    content = await claude_md_merger.build_merged_claude_md(project="AADS")

    assert content.startswith("[truncated:")
    assert "# === CLAUDE.md ===" in content
    assert "# === .claude/rules/alpha.md ===" in content
    assert "# === docs/knowledge/AADS-KNOWLEDGE.md ===" in content
    assert "# === docs/shared-lessons/INDEX.md ===" not in content
    assert len(content.encode("utf-8")) <= claude_md_merger._MAX_MERGED_BYTES


def test_ops_claude_md_endpoint_returns_markdown_and_etag(monkeypatch):
    app = FastAPI()
    app.include_router(ops.router, prefix="/api/v1")

    async def _fake_build(project: str = "AADS") -> str:
        assert project == "AADS"
        return "# merged"

    monkeypatch.setattr(ops, "build_merged_claude_md", _fake_build)
    monkeypatch.setattr(ops, "get_merged_claude_md_sha256", lambda project="AADS": "1234567890abcdef9999")

    with TestClient(app) as client:
        response = client.get("/api/v1/ops/claude-md?project=AADS")
        assert response.status_code == 200
        assert response.text == "# merged"
        assert response.headers["etag"] == '"1234567890abcdef"'
        assert response.headers["content-type"].startswith("text/markdown")

        not_modified = client.get(
            "/api/v1/ops/claude-md?project=AADS",
            headers={"If-None-Match": '"1234567890abcdef"'},
        )
        assert not_modified.status_code == 304
        assert not_modified.headers["etag"] == '"1234567890abcdef"'
