"""정본 문서 등록·조회의 원격 프로젝트(GO100) 저장소 경로 지원."""
from __future__ import annotations

import asyncio
import hashlib
import os
import shlex
import subprocess
import unicodedata

import pytest
from fastapi import HTTPException

from app.api import canonical_documents as docs
from app.services import canonical_document_tools as cdt
from app.services import document_refs
from tests.unit.test_canonical_document_tools import (  # noqa: F401  (autouse 게이트 스텁 포함)
    FakeConn,
    FakePool,
    TENANT,
    _head,
    _register,
    _stub_gate,
)

KOREAN_PATH = "docs/reports/20261028_GO100_조회분석.md"
BODY = "# 조회 분석\n본문"


class NewDocConn(FakeConn):
    async def execute(self, sql, *args):
        await super().execute(sql, *args)
        if "INSERT INTO project_document_heads" in sql:
            self.head = _head(generation=0)


class FakeProc:
    def __init__(self, stdout=b"", returncode=0):
        self._stdout, self.returncode = stdout, returncode
        self.killed = False

    async def communicate(self):
        return self._stdout, b""

    def kill(self):
        self.killed = True

    async def wait(self):
        return self.returncode


def _patch_ssh(monkeypatch, proc):
    calls = []

    async def fake_exec(*args, **kwargs):
        calls.append(args)
        return proc

    monkeypatch.setattr(docs.asyncio, "create_subprocess_exec", fake_exec)
    return calls


def _inp(**over):
    base = {"project": "GO100", "document_key": "doc-a", "kind": "spec", "title": "조회 분석",
            "change_summary": "조회 분석 문서를 정본으로 등록"}
    base.update(over)
    return base


# ── 경로 검증 ────────────────────────────────────────────────────────


def test_source_syntax_accepts_korean_and_normalizes_to_nfc():
    decomposed = unicodedata.normalize("NFD", KOREAN_PATH)
    assert decomposed != KOREAN_PATH
    assert docs._source_syntax(decomposed) == KOREAN_PATH


@pytest.mark.parametrize("bad", [
    "/etc/passwd", "/root/kis-autotrade-v4/docs/a.md", "../x.md", "docs/../x.md", "reports/a/../../x.md",
    "app/x.py", "docs/.hidden.md", "docs/a\nb.md", "docs\\a.md", "http://x/docs/a.md", "",
])
def test_source_syntax_keeps_restrictions(bad):
    with pytest.raises(HTTPException) as exc:
        docs._source_syntax(bad)
    assert exc.value.status_code == 422


def test_remote_target_only_for_remote_projects_with_real_workdir():
    assert docs._remote_target("GO100")["workdir"] == "/root/kis-autotrade-v4"
    assert docs._remote_target("AADS") is None
    assert docs._remote_target("SF") is None
    assert docs._remote_target("NOPE") is None


# ── 원격 읽기 ────────────────────────────────────────────────────────


def test_remote_read_builds_quoted_ssh_command(monkeypatch):
    calls = _patch_ssh(monkeypatch, FakeProc(BODY.encode()))
    target = docs._remote_target("GO100")
    raw = asyncio.run(docs._read_remote_source(target, KOREAN_PATH))
    assert raw == BODY.encode()
    args = calls[0]
    assert args[0] == "ssh" and "root@5.104.86.14" in args
    parts = shlex.split(args[-1])
    assert parts[:3] == ["timeout", "15", "sh"]
    assert parts[-3:] == [KOREAN_PATH, "/root/kis-autotrade-v4", str(docs.MAX_DOCUMENT_BYTES + 1)]
    assert "origin/main" in docs._REMOTE_READ_SCRIPT and "realpath" not in docs._REMOTE_READ_SCRIPT


def test_remote_read_path_cannot_inject_shell(monkeypatch):
    calls = _patch_ssh(monkeypatch, FakeProc(b"x"))
    evil = "docs/a; touch other.md"
    asyncio.run(docs._read_remote_source(docs._remote_target("GO100"), evil))
    assert shlex.split(calls[0][-1])[-3] == evil


@pytest.mark.parametrize("code,status", [(3, 422), (255, 502), (124, 502)])
def test_remote_read_exit_codes(monkeypatch, code, status):
    _patch_ssh(monkeypatch, FakeProc(b"", code))
    with pytest.raises(HTTPException) as exc:
        asyncio.run(docs._read_remote_source(docs._remote_target("GO100"), KOREAN_PATH))
    assert exc.value.status_code == status


def test_remote_read_rejects_oversize(monkeypatch):
    _patch_ssh(monkeypatch, FakeProc(b"a" * (docs.MAX_DOCUMENT_BYTES + 1)))
    with pytest.raises(HTTPException) as exc:
        asyncio.run(docs._read_remote_source(docs._remote_target("GO100"), KOREAN_PATH))
    assert exc.value.status_code == 413


# ── 원격 스크립트 실행(실제 sh + 임시 git 저장소) ─────────────────────────


def _git(cwd, *args):
    env = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
           "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}
    subprocess.run(["git", "-C", str(cwd), *args], check=True, capture_output=True, env=env)


def _run_script(workdir, path, limit=100):
    proc = subprocess.run(["sh", "-c", docs._REMOTE_READ_SCRIPT, "sh", path, str(workdir), str(limit)],
                          capture_output=True)
    return proc.returncode, proc.stdout


@pytest.fixture
def repos(tmp_path):
    work, origin = tmp_path / "work", tmp_path / "origin.git"
    (work / "docs").mkdir(parents=True)
    (work / "docs" / "stale.md").write_text("doc-a")
    (work / "docs" / "real.md").write_text("doc-a")
    (work / "docs" / "link.md").symlink_to("real.md")
    _git(work, "init", "-q", "-b", "main")
    _git(work, "add", ".")
    _git(work, "commit", "-qm", "i")
    _git(tmp_path, "clone", "-q", "--bare", str(work), str(origin))
    _git(work, "remote", "add", "origin", str(origin))
    _git(work, "fetch", "-q", "origin")
    other = tmp_path / "other"
    _git(tmp_path, "clone", "-q", str(origin), str(other))
    (other / "docs" / "new.md").write_text("doc-b")
    _git(other, "add", ".")
    _git(other, "commit", "-qm", "n")
    _git(other, "push", "-q", "origin", "main")
    (work / "docs" / "stale.md").write_text("dirty")
    return work, origin


def test_script_reads_file_only_present_on_origin_main(repos):
    work, _ = repos
    assert not (work / "docs" / "new.md").exists()
    assert _run_script(work, "docs/new.md") == (0, b"doc-b")


def test_script_reads_committed_blob_not_dirty_worktree(repos):
    assert _run_script(repos[0], "docs/stale.md") == (0, b"doc-a")


def test_script_rejects_symlink_blob(repos):
    assert _run_script(repos[0], "docs/link.md")[0] == 3


@pytest.mark.parametrize("path", ["docs", "docs/none.md", "/etc/passwd", "docs/../etc/passwd", "docs/*.md"])
def test_script_rejects_tree_missing_and_escape(repos, path):
    assert _run_script(repos[0], path) == (3, b"")


def test_script_fetch_failure_still_reads_existing_blob(repos):
    work, origin = repos
    origin.rename(origin.with_name("gone.git"))
    assert _run_script(work, "docs/real.md") == (0, b"doc-a")


def test_script_truncates_at_limit(repos):
    assert _run_script(repos[0], "docs/real.md", limit=3) == (0, b"doc")


# ── 본문 확정 ────────────────────────────────────────────────────────


def _stub_origin(monkeypatch, raw=None, error=None):
    async def fake(source):
        if error:
            raise error
        return raw

    monkeypatch.setattr(docs, "_read_origin_source", fake)


def _stub_remote(monkeypatch, raw=None, error=None):
    async def fake(target, source):
        if error:
            raise error
        return raw

    monkeypatch.setattr(docs, "_read_remote_source", fake)


def test_body_for_remote_project_reads_remote_file(monkeypatch):
    _stub_remote(monkeypatch, BODY.encode())
    content, source, digest = asyncio.run(docs._body_for_project("GO100", "", KOREAN_PATH))
    assert (content, source) == (BODY, KOREAN_PATH)
    assert digest == hashlib.sha256(BODY.encode()).hexdigest()


def test_body_for_remote_project_rejects_mismatched_content(monkeypatch):
    _stub_remote(monkeypatch, BODY.encode())
    with pytest.raises(HTTPException) as exc:
        asyncio.run(docs._body_for_project("GO100", "다른 본문", KOREAN_PATH))
    assert exc.value.status_code == 409


def test_body_for_remote_project_missing_file_is_not_bypassed_by_content(monkeypatch):
    _stub_remote(monkeypatch, error=HTTPException(422, "invalid_source_path"))
    with pytest.raises(HTTPException) as exc:
        asyncio.run(docs._body_for_project("GO100", BODY, KOREAN_PATH))
    assert exc.value.detail == "invalid_source_path"


def test_body_for_remote_project_transport_failure_uses_supplied_content(monkeypatch):
    _stub_remote(monkeypatch, error=HTTPException(502, "remote_source_unavailable"))
    content, source, _ = asyncio.run(docs._body_for_project("GO100", BODY, KOREAN_PATH))
    assert (content, source) == (BODY, KOREAN_PATH)
    with pytest.raises(HTTPException) as exc:
        asyncio.run(docs._body_for_project("GO100", "", KOREAN_PATH))
    assert exc.value.status_code == 502


def test_body_for_remote_project_still_scans_credentials(monkeypatch):
    _stub_remote(monkeypatch, "password: abc".encode())
    with pytest.raises(HTTPException) as exc:
        asyncio.run(docs._body_for_project("GO100", "", KOREAN_PATH))
    assert exc.value.detail == "credential_content_rejected"


def test_body_for_local_project_uses_local_reader(monkeypatch):
    seen = []
    monkeypatch.setattr(docs, "_read_local_source", lambda path: seen.append(path) or (BODY.encode(), "host"))
    _stub_origin(monkeypatch, BODY.encode())
    content, source, _ = asyncio.run(docs._body_for_project("AADS", "", "reports/a.md"))
    assert seen == ["reports/a.md"] and (content, source) == (BODY, "reports/a.md")


# ── 도구 경로 ────────────────────────────────────────────────────────


def test_register_go100_file_path_stores_source_path(monkeypatch):
    _stub_remote(monkeypatch, BODY.encode())
    conn = NewDocConn()
    inserts = []
    original = conn.fetchrow

    async def spy(sql, *args):
        if "INSERT INTO project_document_revisions" in sql:
            inserts.append(args)
        return await original(sql, *args)

    conn.fetchrow = spy
    out = _register(conn, _inp(file_path=KOREAN_PATH))
    assert out["registered"] is True and out["approved"] is False
    assert inserts[0][8] == KOREAN_PATH and inserts[0][9] == "repository"


def test_register_rejects_escape_for_remote_project(monkeypatch):
    out = _register(NewDocConn(), _inp(file_path="docs/../../etc/passwd"))
    assert out["error"] == "invalid_source_path" and out["status"] == 422


def test_lookup_file_path_matches_normalized_candidates(monkeypatch):
    _stub_remote(monkeypatch, error=HTTPException(502, "remote_source_unavailable"))
    decomposed = unicodedata.normalize("NFD", "/root/kis-autotrade-v4/" + KOREAN_PATH)

    class Spy(FakeConn):
        args = None

        async def fetch(self, sql, *args):
            Spy.args = args
            return []

    out = asyncio.run(cdt.lookup_documents(
        tenant_id=TENANT, inp={"project": "GO100", "file_path": decomposed}, pool=FakePool(Spy())))
    assert out["registered"] is False
    assert KOREAN_PATH in Spy.args[2]


# ── rdoc 준수 판정 ────────────────────────────────────────────────────


def test_rdoc_matches_absolute_remote_path_and_unicode_forms():
    absolute = "/root/kis-autotrade-v4/" + unicodedata.normalize("NFD", KOREAN_PATH)
    assert document_refs._norm(absolute) == KOREAN_PATH
    result = document_refs.evaluate_rdoc_compliance([absolute], [KOREAN_PATH])
    assert result["status"] == "complete"
