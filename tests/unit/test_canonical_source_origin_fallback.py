"""정본 등록 file_path 해석: 로컬(마운트·이미지)에 없거나 낡은 docs/ 파일은 GitHub 원격 기본 브랜치에서 읽는다."""
from __future__ import annotations

import asyncio
import base64
import hashlib

import httpx
import pytest
from fastapi import HTTPException

from app.api import canonical_documents as docs
from tests.unit.test_canonical_document_tools import FakeConn, _head, _register, _stub_gate  # noqa: F401
from tests.unit.test_canonical_remote_source import NewDocConn

PATH = "docs/prd/20261028_AADS_정본등록경로.md"
BODY = "# 정본 등록 경로\n본문"
OLD_BODY = "# 정본 등록 경로\n낡은 본문"


class FakeResponse:
    def __init__(self, status=200, payload=None):
        self.status_code, self._payload = status, payload

    def json(self):
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise httpx.HTTPStatusError("err", request=httpx.Request("GET", "https://x"), response=self)


def _file_payload(raw: bytes, **over):
    payload = {"type": "file", "size": len(raw), "encoding": "base64", "content": base64.encodebytes(raw).decode()}
    payload.update(over)
    return payload


@pytest.fixture
def github(monkeypatch):
    """httpx 를 목으로 바꾼다. state['response'] 또는 state['error'] 를 지정하고 state['calls'] 로 호출을 본다."""
    state = {"calls": [], "response": FakeResponse(404), "error": None}

    class Client:
        def __init__(self, *a, **kw):
            state["timeout"] = kw.get("timeout")

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def get(self, url, headers=None, params=None):
            state["calls"].append((url, headers, params))
            if state["error"]:
                raise state["error"]
            return state["response"]

    async def no_key(*a, **kw):
        return ""

    monkeypatch.setattr(docs.httpx, "AsyncClient", Client)
    monkeypatch.setattr("app.core.llm_key_provider.get_api_key", no_key)
    monkeypatch.delenv("GITHUB_PAT", raising=False)
    return state


@pytest.fixture
def roots(tmp_path, monkeypatch):
    host, image = tmp_path / "host", tmp_path / "image"
    for r in (host, image):
        (r / "docs" / "prd").mkdir(parents=True)
    monkeypatch.setattr(docs, "ROOT", image)
    monkeypatch.setattr(docs, "_DEFAULT_ROOT", image)
    monkeypatch.setattr(docs, "HOST_SOURCE_ROOT", host)
    return host, image


def _inp(**over):
    base = {"project": "AADS", "document_key": "doc-a", "kind": "spec", "title": "정본 등록 경로",
            "change_summary": "원격 대체 경로로 정본 등록", "file_path": PATH}
    base.update(over)
    return base


def _body(content="", path=PATH):
    return asyncio.run(docs._body_for_project_ex("AADS", content, path))


# (a) 로컬에 없고 원격에 있다 → 등록 성공 + source_resolved_from=remote
def test_register_succeeds_from_remote_when_not_local(roots, github):
    github["response"] = FakeResponse(200, _file_payload(BODY.encode()))
    inserts = []
    conn = NewDocConn()
    original = conn.fetchrow

    async def spy(sql, *args):
        if "INSERT INTO project_document_revisions" in sql:
            inserts.append(args)
        return await original(sql, *args)

    conn.fetchrow = spy
    out = _register(conn, _inp())
    assert out["registered"] is True and out["approved"] is False
    assert out["source_resolved_from"] == "remote" and out["source_stale_local"] is False
    assert inserts[0][7] == hashlib.sha256(BODY.encode()).hexdigest()
    assert inserts[0][8] == PATH and inserts[0][9] == "repository"
    url, headers, params = github["calls"][0]
    assert url.startswith("https://api.github.com/repos/moongoby-GO100/aads-server/contents/docs/prd/")
    assert params == {"ref": "main"} and github["timeout"] <= 10


def test_remote_read_reuses_registry_token_without_new_secret(roots, github, monkeypatch):
    seen = []

    async def key(name, fallback_env=""):
        seen.append((name, fallback_env))
        return "tok"

    monkeypatch.setattr("app.core.llm_key_provider.get_api_key", key)
    github["response"] = FakeResponse(200, _file_payload(BODY.encode()))
    assert asyncio.run(docs._read_origin_source(PATH)) == BODY.encode()
    assert seen == [("GITHUB_TOKEN", "GITHUB_TOKEN")]
    assert github["calls"][0][1]["Authorization"] == "token tok"


# 로컬과 원격이 같다 → 로컬 출처 표시, 낡지 않음
@pytest.mark.parametrize("where", ["host", "image"])
def test_same_local_and_remote_reports_local_origin(roots, github, where):
    (dict(zip(("host", "image"), roots))[where] / PATH).write_text(BODY, encoding="utf-8")
    github["response"] = FakeResponse(200, _file_payload(BODY.encode()))
    *_, meta = _body()
    assert meta == {"source_resolved_from": where, "source_stale_local": False, "source_remote_checked": True, "source_remote_status": "same"}


# (b) 로컬과 원격 내용이 다르다 → 원격이 정본, source_stale_local=true
def test_stale_local_prefers_remote_and_flags_it(roots, github):
    host, _ = roots
    (host / PATH).write_text(OLD_BODY, encoding="utf-8")
    github["response"] = FakeResponse(200, _file_payload(BODY.encode()))
    content, source, digest, meta = _body()
    assert content == BODY and source == PATH and digest == hashlib.sha256(BODY.encode()).hexdigest()
    assert meta["source_resolved_from"] == "remote" and meta["source_stale_local"] is True
    assert meta["source_remote_status"] == "differs" and meta["source_remote_checked"] is True
    out = _register(NewDocConn(), _inp())
    assert out["source_resolved_from"] == "remote" and out["source_stale_local"] is True


def test_content_with_file_path_is_compared_to_remote_resolution(roots, github):
    host, _ = roots
    (host / PATH).write_text(OLD_BODY, encoding="utf-8")
    github["response"] = FakeResponse(200, _file_payload(BODY.encode()))
    assert _body(content=BODY)[0] == BODY
    with pytest.raises(HTTPException) as exc:
        _body(content=OLD_BODY)
    assert exc.value.status_code == 409 and exc.value.detail == "source_content_mismatch"


def test_remote_unavailable_falls_back_to_local_copy(roots, github):
    _, image = roots
    (image / PATH).write_text(BODY, encoding="utf-8")
    github["error"] = httpx.ConnectError("down")
    *_, meta = _body()
    assert meta == {"source_resolved_from": "image", "source_stale_local": False, "source_remote_checked": False, "source_remote_status": "unavailable"}


def test_local_source_with_remote_404_reports_missing_and_uses_local(roots, github):
    host, _ = roots
    (host / PATH).write_text(BODY, encoding="utf-8")
    github["response"] = FakeResponse(404)
    content, _, _, meta = _body()
    assert content == BODY
    assert meta == {"source_resolved_from": "host", "source_stale_local": False,
                    "source_remote_checked": True, "source_remote_status": "missing"}


# (c) 원격 404 → 422 + 한글 사유
def test_remote_404_without_local_is_422_with_korean_reason(roots, github):
    github["response"] = FakeResponse(404)
    with pytest.raises(HTTPException) as exc:
        _body()
    assert exc.value.status_code == 422
    assert exc.value.detail["code"] == "invalid_source_path"
    assert "어디에서도 파일을 찾지 못했습니다" in exc.value.detail["message"]
    out = _register(NewDocConn(), _inp())
    assert out["error"] == "invalid_source_path" and out["status"] == 422
    assert "어디에서도 파일을 찾지 못했습니다" in out["message"]


@pytest.mark.parametrize("failure", [
    {"error": httpx.ConnectError("down")},
    {"error": httpx.ReadTimeout("slow")},
    {"response": FakeResponse(500)},
    {"response": FakeResponse(200, {"type": "dir"})},
])
def test_remote_failures_are_422(roots, github, failure):
    github.update(failure)
    with pytest.raises(HTTPException) as exc:
        _body()
    assert exc.value.status_code == 422 and exc.value.detail["code"] == "invalid_source_path"


def test_remote_dir_or_symlink_is_not_found(roots, github):
    github["response"] = FakeResponse(200, [{"type": "file"}])
    with pytest.raises(HTTPException) as exc:
        _body()
    assert "찾지 못했습니다" in exc.value.detail["message"]


def test_remote_read_has_hard_timeout(roots, github, monkeypatch):
    async def hang(source):
        await asyncio.sleep(5)

    monkeypatch.setattr(docs, "_github_fetch", hang)
    monkeypatch.setattr(docs, "GITHUB_READ_TIMEOUT", 0.05)
    with pytest.raises(HTTPException) as exc:
        asyncio.run(docs._read_origin_source(PATH))
    assert exc.value.status_code == 422 and "조회 실패" in exc.value.detail["message"]


def test_remote_oversize_is_413(roots, github):
    github["response"] = FakeResponse(200, _file_payload(b"x", size=docs.MAX_DOCUMENT_BYTES + 1))
    with pytest.raises(HTTPException) as exc:
        _body()
    assert exc.value.status_code == 413


def test_remote_content_is_still_credential_scanned(roots, github):
    github["response"] = FakeResponse(200, _file_payload(b"password: abcdef"))
    with pytest.raises(HTTPException) as exc:
        _body()
    assert exc.value.detail == "credential_content_rejected"


# (d) 경로 검사는 원격 대체 경로에도 똑같이 적용된다
@pytest.mark.parametrize("bad", [
    "../x.md", "docs/../x.md", "reports/a/../../x.md", "/etc/passwd", "/root/aads/aads-server/docs/a.md",
    "app/main.py", "README.md", "docs/.hidden.md", "docs\\a.md", "http://x/docs/a.md", "",
])
def test_bad_paths_rejected_before_any_remote_call(roots, github, bad):
    github["response"] = FakeResponse(200, _file_payload(BODY.encode()))
    with pytest.raises(HTTPException) as exc:
        _body(path=bad) if bad else asyncio.run(docs._body_for_project_ex("AADS", "", bad))
    assert exc.value.status_code == 422
    with pytest.raises(HTTPException) as direct:
        asyncio.run(docs._read_origin_source(bad))
    assert direct.value.status_code == 422
    assert github["calls"] == []


def test_remote_path_is_nfc_normalized_and_quoted(roots, github):
    import unicodedata

    github["response"] = FakeResponse(200, _file_payload(BODY.encode()))
    asyncio.run(docs._read_origin_source(unicodedata.normalize("NFD", PATH)))
    assert unicodedata.normalize("NFC", github["calls"][0][0]) == github["calls"][0][0]
    assert " " not in github["calls"][0][0] and "%" in github["calls"][0][0]


def test_no_source_path_makes_no_remote_call(roots, github):
    content, source, _, meta = asyncio.run(docs._body_for_project_ex("AADS", "직접 본문", None))
    assert (content, source, meta) == ("직접 본문", None, {}) and github["calls"] == []
