"""HTML 가져오기(목업 asset) 계약 테스트.

고정하는 계약
  1. 업로드 성공·멱등(같은 내용 = 같은 경로), 응답이 manifest assets 항목에 그대로 들어간다.
  2. 외부·절대·상대 참조가 있으면 저장하지 않고 422 + 목록 + 한국어 메시지.
  3. URL 가져오기: https·허용 호스트·사설 IP 차단·리다이렉트 거부·타임아웃·크기 상한.
  4. 25MB 초과·비 UTF-8·시크릿 거부, 권한 없는 프로젝트 403.
  5. 채팅 도구 html_source 는 같은 서비스 함수를 거치고 이 채팅 세션의 업로드만 읽는다.
  6. 열람 응답의 CSP 는 sandbox(allow-same-origin 없음) + nosniff.
"""
from __future__ import annotations

import asyncio
import socket
import uuid
from unittest.mock import AsyncMock

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from app.api import mockup_assets as assets_api
from app.api import mockup_reviews as routes
from app.auth import get_current_tenant_context
from app.models.mockup_review import RevisionCreate
from app.services import mockup_html_import as imp
from app.services import mockup_review_service as svc
from app.services import mockup_review_tools as mrt

from tests.unit.test_mockup_review_tools import EXEMPT, REVIEW, REVISION, SESSION, TENANT, FakeConn, FakePool

PUBLIC_IP = "93.184.216.34"
PAGE = ("<!doctype html><html><head><meta charset=utf-8><title> 발주 &amp; 반품\n 목업 </title>"
        "<style>body{background:url(data:image/png;base64,AAAA)}</style></head>"
        "<body><img src='data:image/gif;base64,R0lGODlh'><a href='https://example.com/x'>link</a>"
        "<script>var a=1;</script></body></html>")
USER = {"user": {"user_id": "u1", "is_internal_admin": False}, "tenant": {"id": TENANT}, "membership": {"role": "member"}}


@pytest.fixture
def root(tmp_path, monkeypatch):
    path = tmp_path / "assets"
    monkeypatch.setenv("MOCKUP_REVIEW_ASSET_ROOT", str(path))
    monkeypatch.delenv("MOCKUP_HTML_IMPORT_HOSTS", raising=False)
    return path


def _client(monkeypatch, *, granted=True):
    conn = FakeConn(granted=granted)
    monkeypatch.setattr(assets_api, "get_pool", lambda: FakePool(conn))
    app = FastAPI()
    app.include_router(assets_api.router, prefix="/api/v1")
    app.dependency_overrides[assets_api.WRITE.dependency] = lambda: USER
    return TestClient(app), conn


def _upload(client, data=PAGE.encode(), name="m.html", ctype="text/html", project="ntv2"):
    return client.post(f"/api/v1/projects/{project}/mockup-assets/html", files={"file": (name, data, ctype)})


def _detail(response):
    return response.json()["detail"]


def _files(root):
    return sorted(p for p in root.rglob("*") if p.is_file()) if root.exists() else []


# ── 1. 업로드·멱등 ─────────────────────────────────────────────────────


def test_upload_stores_under_hash_path_and_is_idempotent(root, monkeypatch):
    client, _ = _client(monkeypatch)
    body = PAGE.replace("https://example.com/x", "#x").encode()
    first = _upload(client, body)
    assert first.status_code == 201, first.text
    out = first.json()
    digest = svc.sha256_hex(body)
    assert out == {"uri": f"internal://mockup_assets/NTV2/html/{digest[:16]}/index.html", "mime": "text/html",
                   "sha256": digest, "byte_size": len(body), "title": "발주 & 반품 목업", "external_refs": []}
    stored = root / "NTV2" / "html" / digest[:16] / "index.html"
    assert stored.read_bytes() == body
    assert _files(root) == [stored]

    second = _upload(client, body)
    assert second.status_code == 200 and second.json() == out
    assert _files(root) == [stored]


def test_response_fits_the_manifest_and_passes_the_existing_hash_gate(root, monkeypatch):
    client, _ = _client(monkeypatch)
    out = _upload(client, PAGE.replace("https://example.com/x", "#x").encode()).json()
    manifest = {"screens": [{"screen_id": "s1", "title": "화면", "route": "/r", "requirement_ids": ["R1"]}],
                "assets": [{"asset_id": "html-1", "role": "primary", "uri": out["uri"], "sha256": out["sha256"],
                            "byte_size": out["byte_size"], "mime": out["mime"], "screen_id": "s1", "phase": "mockup",
                            "viewport": "desktop", "fixture_id": "f", "state": "default",
                            "captured_at": "2026-10-23T00:00:00Z", "capture_source": "html_import"}]}
    body = RevisionCreate(idempotency_key="k" * 12, expected_generation=0, manifest=manifest)
    built = asyncio.run(svc.build_manifest(body, "new"))
    assert built["assets"][0]["sha256"] == out["sha256"]


def test_upload_requires_html_file(root, monkeypatch):
    client, _ = _client(monkeypatch)
    res = _upload(client, b"x", name="a.png", ctype="image/png")
    assert res.status_code == 415 and _detail(res)["code"] == "html_file_required"
    assert _upload(client, PAGE.encode(), name="blob", ctype="text/html").status_code == 201
    assert _files(root)


def test_empty_request_and_conflicting_inputs_rejected(root, monkeypatch):
    client, _ = _client(monkeypatch)
    res = client.post("/api/v1/projects/NTV2/mockup-assets/html", json={})
    assert res.status_code == 422 and _detail(res)["code"] == "html_source_required"
    res = client.post("/api/v1/projects/NTV2/mockup-assets/html",
                      files={"file": ("a.html", b"<p>x</p>", "text/html")},
                      data={"source_url": "https://v2.newtalk.kr/a.html"})
    assert res.status_code == 422 and _detail(res)["code"] == "html_source_conflict"
    assert _files(root) == []


def test_openapi_exposes_the_new_path(monkeypatch):
    client, _ = _client(monkeypatch)
    spec = client.get("/openapi.json").json()
    op = spec["paths"]["/api/v1/projects/{project_key}/mockup-assets/html"]["post"]
    assert set(op["requestBody"]["content"]) == {"multipart/form-data", "application/json"}
    source = open(svc.ROOT / "app" / "main.py", encoding="utf-8").read()
    assert "app.include_router(mockup_assets_router" in source


# ── 2. 참조 검사 ───────────────────────────────────────────────────────


def test_external_references_are_rejected_with_list_and_nothing_is_stored(root, monkeypatch):
    client, _ = _client(monkeypatch)
    refs = [f"https://cdn.example.com/{i}.png" for i in range(25)]
    html = "<html><head><link rel=stylesheet href='//fonts.example.com/f.css'></head><body>" + \
        "".join(f"<img src='{r}'>" for r in refs) + "<img src='/abs/logo.png'></body></html>"
    res = _upload(client, html.encode())
    detail = _detail(res)
    assert res.status_code == 422 and detail["code"] == "html_external_reference"
    assert detail["count"] == 27 and len(detail["external_refs"]) == 20
    assert detail["external_refs"][0] == "//fonts.example.com/f.css"
    assert "외부 주소 27건" in detail["message"] and "data:" in detail["message"]
    assert _files(root) == []


def test_css_url_and_relative_references_rejected(root, monkeypatch):
    client, _ = _client(monkeypatch)
    res = _upload(client, b"<html><style>@import 'https://x.example/a.css';</style></html>")
    assert _detail(res)["code"] == "html_external_reference"
    res = _upload(client, b"<html><img src='img/a.png'><script src='app.js'></script></html>")
    detail = _detail(res)
    assert res.status_code == 422 and detail["code"] == "html_relative_reference"
    assert detail["relative_refs"] == ["img/a.png", "app.js"]
    assert _files(root) == []


# ── 4. 본문 검사 ───────────────────────────────────────────────────────


def _secret_html() -> bytes:
    # 저장소 텍스트에 키 패턴이 남지 않도록 런타임에 조립한다 (gitleaks generic-api-key 회피).
    name = "".join(["api", "_", "key"])
    value = "".join(["ab", "cd", "ef", "12", "34", "56"])
    return ("<html><script>var " + name + " = '" + value + "';</script></html>").encode()


def test_oversize_non_utf8_empty_and_secret_rejected(root, monkeypatch):
    client, _ = _client(monkeypatch)
    big = b"<html>" + b"a" * svc.MAX_ASSET_BYTES
    res = _upload(client, big)
    assert res.status_code == 413 and _detail(res)["code"] == "asset_too_large"
    res = _upload(client, b"<html>\xff\xfe\x00bad</html>")
    assert res.status_code == 422 and _detail(res)["code"] == "asset_not_utf8"
    res = _upload(client, b"   \n")
    assert _detail(res)["code"] == "html_empty"
    res = _upload(client, _secret_html())
    assert res.status_code == 422 and _detail(res)["code"] == "credential_content_rejected"
    assert _files(root) == []


def test_exactly_max_size_is_accepted(root):
    data = b"<html>" + b"a" * (svc.MAX_ASSET_BYTES - len(b"<html>"))
    assert len(data) == svc.MAX_ASSET_BYTES
    asset, created = imp.store_html("NTV2", data)
    assert created and asset["byte_size"] == svc.MAX_ASSET_BYTES


def test_content_length_over_limit_rejected_before_reading(root, monkeypatch):
    client, _ = _client(monkeypatch)
    res = client.post("/api/v1/projects/NTV2/mockup-assets/html", content=b"{}",
                      headers={"content-length": str(svc.MAX_ASSET_BYTES + 2 * 1024 * 1024),
                               "content-type": "application/json"})
    assert res.status_code == 413


def test_unauthorized_project_is_403_and_nothing_is_stored(root, monkeypatch):
    client, _ = _client(monkeypatch, granted=False)
    res = _upload(client)
    assert res.status_code == 403 and _detail(res) == "project_access_denied"
    assert _files(root) == []


def _unmocked_client(monkeypatch):
    conn = FakeConn(granted=True)
    monkeypatch.setattr(assets_api, "get_pool", lambda: FakePool(conn))
    app = FastAPI()
    app.include_router(assets_api.router, prefix="/api/v1")
    return app, TestClient(app, raise_server_exceptions=False)


def test_no_credentials_is_401_and_nothing_is_stored(root, monkeypatch):
    _, client = _unmocked_client(monkeypatch)
    res = _upload(client)
    assert res.status_code == 401
    assert _files(root) == []


def test_viewer_role_is_403_and_nothing_is_stored(root, monkeypatch):
    app, client = _unmocked_client(monkeypatch)
    viewer = {**USER, "membership": {"role": "viewer"}}
    app.dependency_overrides[get_current_tenant_context] = lambda: viewer
    res = _upload(client)
    assert res.status_code == 403
    assert _files(root) == []


def test_import_route_is_registered_exactly_once_in_the_real_app():
    from app.main import app as real_app

    seen: dict[tuple[str, str], int] = {}
    for route in real_app.routes:
        for method in getattr(route, "methods", None) or ():
            seen[(method, route.path)] = seen.get((method, route.path), 0) + 1
    target = ("POST", "/api/v1/projects/{project_key}/mockup-assets/html")
    assert seen.get(target) == 1
    mockup = {key: n for key, n in seen.items() if "/mockup-" in key[1]}
    assert mockup and all(n == 1 for n in mockup.values()), {k: n for k, n in mockup.items() if n != 1}
    assert not [r for r in routes.router.routes if "mockup-assets" in r.path]
    assert [r.path for r in assets_api.router.routes] == ["/projects/{project_key}/mockup-assets/html"]


def test_invalid_project_key_rejected(root, monkeypatch):
    client, _ = _client(monkeypatch)
    assert _upload(client, project="a.b").status_code == 422
    assert _files(root) == []


# ── 3. URL 가져오기 ────────────────────────────────────────────────────


def _patch_net(monkeypatch, *, ips=(PUBLIC_IP,), result=None):
    monkeypatch.setattr(imp.socket, "getaddrinfo",
                        lambda host, port, **kw: [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (ip, port)) for ip in ips])
    calls = []

    def fake_get(host, port, target, ip, deadline):
        calls.append((host, port, target, ip))
        if isinstance(result, Exception):
            raise result
        return result or (200, {"content-type": "text/html; charset=utf-8"}, b"<html><title>t</title></html>")

    monkeypatch.setattr(imp, "_http_get", fake_get)
    return calls


def _post_url(client, url):
    return client.post("/api/v1/projects/NTV2/mockup-assets/html", json={"source_url": url})


def test_url_import_from_allowed_host_pins_validated_ip(root, monkeypatch):
    client, _ = _client(monkeypatch)
    calls = _patch_net(monkeypatch)
    res = _post_url(client, "https://v2.newtalk.kr/reports/ntv2-order-return-rev5.html?x=1")
    assert res.status_code == 201, res.text
    assert calls == [("v2.newtalk.kr", 443, "/reports/ntv2-order-return-rev5.html?x=1", PUBLIC_IP)]
    assert res.json()["title"] == "t" and len(_files(root)) == 1
    assert _post_url(client, "https://aads.newtalk.kr/a.html").status_code == 200  # same bytes: idempotent


@pytest.mark.parametrize("url, code", [
    ("http://v2.newtalk.kr/a.html", "source_url_invalid"),
    ("https://user:pw@v2.newtalk.kr/a.html", "source_url_invalid"),
    ("https://v2.newtalk.kr:8443/a.html", "source_url_invalid"),
    ("https://v2.newtalk.kr.evil.example/a.html", "source_host_not_allowed"),
    ("https://evil.example/a.html", "source_host_not_allowed"),
    ("https://127.0.0.1/a.html", "source_host_not_allowed"),
    ("https://169.254.169.254/latest/meta-data", "source_host_not_allowed"),
    ("https://localhost/a.html", "source_host_not_allowed"),
    ("file:///etc/passwd", "source_url_invalid"),
    ("https://v2.newtalk.kr/a b.html", "source_url_invalid"),
    ("", "html_source_required"),
])
def test_disallowed_urls_never_reach_the_network(root, monkeypatch, url, code):
    client, _ = _client(monkeypatch)
    calls = _patch_net(monkeypatch)
    res = _post_url(client, url)
    assert res.status_code == 422 and _detail(res)["code"] == code
    assert calls == [] and _files(root) == []


@pytest.mark.parametrize("ips", [("10.0.0.5",), ("127.0.0.1",), ("169.254.169.254",), ("192.168.1.1",),
                                 ("100.64.0.1",), ("::1",), ("::ffff:127.0.0.1",), (PUBLIC_IP, "10.0.0.5")])
def test_hosts_resolving_to_non_public_addresses_are_blocked(root, monkeypatch, ips):
    client, _ = _client(monkeypatch)
    calls = _patch_net(monkeypatch, ips=ips)
    res = _post_url(client, "https://v2.newtalk.kr/a.html")
    assert res.status_code == 422 and _detail(res)["code"] == "source_resolves_private"
    assert calls == [] and _files(root) == []


@pytest.mark.parametrize("status", [301, 302, 307, 308])
def test_redirects_are_refused(root, monkeypatch, status):
    client, _ = _client(monkeypatch)
    _patch_net(monkeypatch, result=(status, {"location": "https://evil.example/"}, b""))
    res = _post_url(client, "https://v2.newtalk.kr/a.html")
    assert res.status_code == 422 and _detail(res)["code"] == "source_redirect_not_allowed"
    assert _files(root) == []


def test_other_fetch_failures(root, monkeypatch):
    client, _ = _client(monkeypatch)
    _patch_net(monkeypatch, result=(404, {}, b""))
    assert _detail(_post_url(client, "https://v2.newtalk.kr/a.html"))["code"] == "source_fetch_failed"
    _patch_net(monkeypatch, result=(200, {"content-type": "application/pdf"}, b"%PDF"))
    assert _detail(_post_url(client, "https://v2.newtalk.kr/a.html"))["code"] == "source_not_html"
    _patch_net(monkeypatch, result=TimeoutError())
    res = _post_url(client, "https://v2.newtalk.kr/a.html")
    assert res.status_code == 504 and _detail(res)["code"] == "source_fetch_timeout"
    _patch_net(monkeypatch, result=ConnectionRefusedError())
    assert _post_url(client, "https://v2.newtalk.kr/a.html").status_code == 502
    _patch_net(monkeypatch, result=(200, {"content-type": "text/html"}, b"a" * (svc.MAX_ASSET_BYTES + 1)))
    res = _post_url(client, "https://v2.newtalk.kr/a.html")
    assert res.status_code == 413 and _detail(res)["code"] == "asset_too_large"
    assert _files(root) == []


def test_fetch_timeout_is_twenty_seconds_and_download_is_capped(monkeypatch):
    assert imp.FETCH_TIMEOUT == 20.0
    seen = []
    monkeypatch.setattr(imp.socket, "getaddrinfo", lambda *a, **k: [(socket.AF_INET, 1, 6, "", (PUBLIC_IP, 443))])
    monkeypatch.setattr(imp, "_http_get", lambda h, p, t, ip, deadline: seen.append(deadline) or (
        200, {"content-type": "text/html"}, b"<p>x</p>"))
    import time
    before = time.monotonic()
    imp.fetch_html_url("https://v2.newtalk.kr/")
    assert 19 < seen[0] - before <= 20.5


def test_request_does_not_ask_for_text_html_so_cloudflare_does_not_inject_its_beacon():
    assert imp._REQUEST_HEADERS["Accept"] == "*/*" and imp._REQUEST_HEADERS["Accept-Encoding"] == "identity"


def test_extra_hosts_come_from_setting_and_only_valid_hostnames(monkeypatch):
    monkeypatch.setenv("MOCKUP_HTML_IMPORT_HOSTS", "Reports.Example.com, 10.0.0.1 ,bad host,,x_y.example.com")
    assert imp.allowed_hosts() == {"aads.newtalk.kr", "v2.newtalk.kr", "reports.example.com"}
    assert imp.validate_source_url("https://reports.example.com/a.html")[0] == "reports.example.com"


# ── 5. 채팅 도구 html_source ───────────────────────────────────────────


@pytest.fixture
def services(monkeypatch):
    mocks = {
        "create_revision": AsyncMock(return_value={
            "review_id": REVIEW, "revision_id": REVISION, "manifest_hash": "h" * 64, "status": "draft",
            "generation": 5, "pointer_applied": True, "idempotent": False}),
    }
    for name, mock in mocks.items():
        monkeypatch.setattr(svc, name, mock)
    return mocks


@pytest.fixture
def chat_dir(tmp_path, monkeypatch):
    base = tmp_path / "chat_files"
    monkeypatch.setenv("CHAT_FILES_DIR", str(base))
    session_dir = base / SESSION
    session_dir.mkdir(parents=True)
    return session_dir


SCREEN = {"screen_id": "s1", "title": "화면", "route": "/r", "requirement_ids": ["R1"]}


def _run_tool(**inp):
    inp.setdefault("project", "ntv2")
    inp.setdefault("action", "revision")
    inp.setdefault("review_id", REVIEW)
    inp.setdefault("doc_refs", [EXEMPT])
    inp.setdefault("manifest", {"screens": [SCREEN]})
    return asyncio.run(mrt.run_action(tenant_id=TENANT, session_id=SESSION, inp=inp, pool=FakePool(FakeConn())))


def _submitted_assets(services):
    return services["create_revision"].await_args.args[5].manifest.assets


def test_tool_imports_chat_upload_and_adds_primary_asset(root, chat_dir, services):
    (chat_dir / "page.html").write_text(PAGE.replace("https://example.com/x", "#"), encoding="utf-8")
    out = _run_tool(html_source={"upload_path": "page.html"})
    assert out["revision_id"] == REVISION and out["approved"] is False
    (asset,) = _submitted_assets(services)
    assert asset.role == "primary" and asset.screen_id == "s1" and asset.mime == "text/html"
    assert asset.uri.startswith("internal://mockup_assets/NTV2/html/") and asset.phase == "mockup"
    assert (root / asset.uri.split("mockup_assets/")[1]).is_file()

    again = _run_tool(html_source={"upload_path": str(chat_dir / "page.html")})
    assert again["revision_id"] == REVISION
    first_key = services["create_revision"].await_args_list[0].args[5].idempotency_key
    assert services["create_revision"].await_args_list[1].args[5].idempotency_key == first_key


def test_tool_imports_url_with_overrides_and_keeps_existing_assets(root, chat_dir, services, monkeypatch):
    _patch_net(monkeypatch)
    existing = {"asset_id": "keep", "role": "child", "uri": "internal://docs/a.png", "sha256": "a" * 64,
                "byte_size": 1, "mime": "image/png"}
    out = _run_tool(manifest={"screens": [SCREEN, {**SCREEN, "screen_id": "s2"}], "assets": [existing]},
                    html_source={"url": "https://v2.newtalk.kr/r.html", "screen_id": "s2", "viewport": "mobile",
                                 "asset_id": "rev5-mobile"})
    assert out["revision_id"] == REVISION
    kept, added = _submitted_assets(services)
    assert kept.asset_id == "keep" and added.asset_id == "rev5-mobile"
    assert added.screen_id == "s2" and added.viewport == "mobile"


def test_tool_url_rejections_surface_as_error_codes(root, chat_dir, services, monkeypatch):
    _patch_net(monkeypatch, ips=("10.1.1.1",))
    out = _run_tool(html_source={"url": "https://v2.newtalk.kr/r.html"})
    assert out["error"] == "source_resolves_private" and out["status"] == 422
    out = _run_tool(html_source={"url": "https://evil.example/r.html"})
    assert out["error"] == "source_host_not_allowed"
    (chat_dir / "ext.html").write_text("<img src='https://cdn.example/a.png'>", encoding="utf-8")
    out = _run_tool(html_source={"upload_path": "ext.html"})
    assert out["error"] == "html_external_reference" and out["detail"]["external_refs"] == ["https://cdn.example/a.png"]
    assert "외부 주소 1건" in out["detail"]["message"]
    assert not services["create_revision"].await_count


def test_tool_upload_path_is_confined_to_this_session(root, chat_dir, services, tmp_path):
    other = chat_dir.parent / str(uuid.uuid4())
    other.mkdir()
    (other / "x.html").write_text("<p>other</p>", encoding="utf-8")
    secret = tmp_path / "secret.html"
    secret.write_text("<p>host file</p>", encoding="utf-8")
    (chat_dir / "link.html").symlink_to(secret)
    (chat_dir / "note.txt").write_text("<p>t</p>", encoding="utf-8")
    for path in (str(other / "x.html"), "../" + other.name + "/x.html", str(secret), "/etc/passwd",
                 "link.html", "note.txt", "../../secret.html"):
        out = _run_tool(html_source={"upload_path": path})
        assert out["error"] in ("upload_path_not_allowed", "upload_not_found"), (path, out)
    assert _run_tool(html_source={"upload_path": "missing.html"})["error"] == "upload_not_found"
    assert not services["create_revision"].await_count


def test_tool_html_source_input_validation(root, chat_dir, services):
    bad = [{"url": "https://v2.newtalk.kr/a", "upload_path": "a.html"}, {}, "https://v2.newtalk.kr/a",
           {"url": "https://v2.newtalk.kr/a", "path": "x"}]
    for source in bad:
        out = _run_tool(html_source=source) if source else _run_tool(html_source={"url": ""})
        assert out["error"] == "invalid_input", (source, out)
    out = _run_tool(action="status", html_source={"url": "https://v2.newtalk.kr/a"})
    assert out["error"] == "invalid_input"
    out = _run_tool(manifest={"screens": [SCREEN, {**SCREEN, "screen_id": "s2"}]},
                    html_source={"upload_path": "a.html"})
    assert out["error"] == "invalid_input" and "screen_id" in out["message"]
    assert not services["create_revision"].await_count


def test_tool_without_html_source_is_unchanged(root, chat_dir, services):
    out = _run_tool(manifest={"screens": [SCREEN]})
    assert out["revision_id"] == REVISION
    assert _submitted_assets(services) == []
    assert _files(root) == []


def test_tool_denied_user_never_imports(root, chat_dir, services):
    (chat_dir / "page.html").write_text("<p>x</p>", encoding="utf-8")
    inp = {"project": "ntv2", "action": "revision", "review_id": REVIEW, "doc_refs": [EXEMPT],
           "manifest": {"screens": [SCREEN]}, "html_source": {"upload_path": "page.html"}}
    out = asyncio.run(mrt.run_action(tenant_id=TENANT, session_id=SESSION, inp=inp,
                                     pool=FakePool(FakeConn(granted=False))))
    assert out["error"] == "project_access_denied"
    assert _files(root) == [] and not services["create_revision"].await_count


def test_tool_schema_documents_html_source():
    from app.services.tool_registry import ToolRegistry

    props = ToolRegistry().get_tool("mockup_review_submit")["input_schema"]["properties"]
    assert set(props["html_source"]["properties"]) >= {"url", "upload_path", "screen_id"}


# ── 6. 열람 헤더 ───────────────────────────────────────────────────────


def _asset_response(monkeypatch, data, mime, csp=svc.LEGACY_ASSET_CSP):
    monkeypatch.setattr(routes, "_run", AsyncMock(return_value=(data, mime, csp)))
    return asyncio.run(routes.get_asset("NTV2", uuid.uuid4(), uuid.uuid4(), "a1", USER))


def test_html_asset_is_served_sandboxed_without_same_origin(monkeypatch):
    res = _asset_response(monkeypatch, b"<p>x</p>", "text/html", svc.HTML_ASSET_CSP)
    csp = res.headers["content-security-policy"]
    assert csp == ("sandbox allow-scripts; default-src 'none'; img-src data: blob:; style-src 'unsafe-inline' data:; "
                   "font-src data:; script-src 'unsafe-inline'")
    assert "allow-same-origin" not in csp and "allow-top-navigation" not in csp and "allow-forms" not in csp
    assert res.headers["x-content-type-options"] == "nosniff"
    assert res.media_type == "text/html"


def test_other_assets_keep_the_strict_sandbox(monkeypatch):
    res = _asset_response(monkeypatch, b"\x89PNG", "image/png")
    assert res.headers["content-security-policy"].startswith("sandbox; default-src 'none'")
    assert res.headers["x-content-type-options"] == "nosniff"


def test_existing_child_references_unchanged_by_the_refactor():
    asset = {"uri": "internal://docs/m/index.html", "mime": "text/html"}
    html = b"<img src='a.png'><style>.x{background:url(b.png)}</style><img src='data:image/png;base64,AA'>"
    assert svc.child_references(asset, html) == {"internal://docs/m/a.png", "internal://docs/m/b.png"}
    with pytest.raises(svc.AssetProblem) as exc:
        svc.child_references(asset, b"<img src='https://x.example/a.png'>")
    assert exc.value.code == "external_reference_not_allowed"
    with pytest.raises(HTTPException):
        svc.fail(422, "x")


@pytest.mark.parametrize("imported", [False, True])
def test_asset_headers_select_policy_from_stored_uri(monkeypatch, imported):
    data = b"<html><img src='a.png'></html>" if not imported else b"<html>single</html>"
    uri = ("internal://mockup_assets/NTV2/html/0123456789abcdef/index.html" if imported
           else "internal://docs/mockups/index.html")
    asset = {"asset_id": "a1", "uri": uri, "mime": "text/html",
             "sha256": svc.sha256_hex(data), "byte_size": len(data)}

    class Conn:
        async def fetchrow(self, *args):
            return {"manifest": {"assets": [asset]}}

    async def run(project, context, access, work):
        return await work(Conn(), TENANT, project, "actor", False)

    monkeypatch.setattr(routes, "_run", run)
    monkeypatch.setattr(svc, "read_asset", lambda uri: data)
    response = asyncio.run(routes.get_asset("NTV2", REVIEW, REVISION, "a1", USER))
    csp = response.headers["content-security-policy"]
    assert csp == (svc.HTML_ASSET_CSP if imported else svc.LEGACY_ASSET_CSP)
    assert "allow-same-origin" not in csp
    assert ("img-src 'self' data:" in csp) == (not imported)
    assert "default-src 'none'" in csp


@pytest.mark.parametrize("mode", ["close", "keep-alive", "chunked", "deadline", "header-deadline"])
def test_loopback_fetch_response_socket_and_deadline(monkeypatch, mode):
    import http.client
    import threading
    import time
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.0" if mode == "close" else "HTTP/1.1"

        def log_message(self, *args):
            pass

        def do_GET(self):
            try:
                if mode == "header-deadline":
                    time.sleep(0.3)
                self.send_response(200)
                self.send_header("Content-Type", "text/html")
                if mode == "chunked":
                    self.send_header("Transfer-Encoding", "chunked")
                else:
                    self.send_header("Content-Length", str(len(PAGE.encode())))
                if mode == "close":
                    self.send_header("Connection", "close")
                self.end_headers()
                if mode == "deadline":
                    # Continuous progress must not reset the total deadline.
                    for byte in PAGE.encode():
                        self.wfile.write(bytes([byte]))
                        self.wfile.flush()
                        time.sleep(0.02)
                elif mode == "chunked":
                    body = PAGE.encode()
                    self.wfile.write(f"{len(body):x}\r\n".encode() + body + b"\r\n0\r\n\r\n")
                else:
                    self.wfile.write(PAGE.encode())
            except OSError:
                pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    connections = []

    def connection(host, port, ip, timeout):
        conn = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=timeout)
        connections.append(conn)
        return conn

    monkeypatch.setattr(imp, "_PinnedHTTPS", connection)
    monkeypatch.setattr(imp, "resolve_public_ip", lambda *args: "127.0.0.1")
    monkeypatch.setattr(imp, "FETCH_TIMEOUT", 0.15 if "deadline" in mode else 2)
    started = time.monotonic()
    try:
        if "deadline" in mode:
            with pytest.raises(HTTPException) as exc:
                imp.fetch_html_url("https://aads.newtalk.kr/test")
            assert exc.value.status_code == 504
            assert time.monotonic() - started < 0.8
        else:
            assert imp.fetch_html_url("https://aads.newtalk.kr/test") == PAGE.encode()
        assert all(conn.sock is None for conn in connections)
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


@pytest.mark.parametrize("mode", ["close", "keep-alive", "chunked"])
def test_http_client_detached_socket_with_in_memory_transport(monkeypatch, mode):
    """Exercise real HTTPResponse parsing even when the sandbox denies sockets."""
    import http.client
    import io
    import time

    data = PAGE.encode()
    wire_body = (f"{len(data):x}\r\n".encode() + data + b"\r\n0\r\n\r\n"
                 if mode == "chunked" else data)
    framing = (b"Transfer-Encoding: chunked\r\n" if mode == "chunked"
               else f"Content-Length: {len(data)}\r\n".encode())
    version = b"HTTP/1.0" if mode == "close" else b"HTTP/1.1"
    wire = version + b" 200 OK\r\nContent-Type: text/html\r\n" + framing + b"\r\n" + wire_body
    streams, sockets, connections = [], [], []

    class Socket:
        def __init__(self):
            self.timeouts = []
            self.closed = False
            sockets.append(self)

        def sendall(self, data):
            pass

        def makefile(self, *args):
            stream = io.BytesIO(wire)
            streams.append(stream)
            return stream

        def settimeout(self, value):
            self.timeouts.append(value)

        def close(self):
            self.closed = True

    class Connection(http.client.HTTPConnection):
        def __init__(self, host, port, ip, timeout):
            super().__init__(host, port, timeout=timeout)
            connections.append(self)

        def connect(self):
            self.sock = Socket()

        def getresponse(self):
            response = super().getresponse()
            if mode == "close":
                assert self.sock is None  # exact CEO-reported condition
            return response

    monkeypatch.setattr(imp, "_PinnedHTTPS", Connection)
    status, headers, body = imp._http_get("aads.newtalk.kr", 443, "/", PUBLIC_IP, time.monotonic() + 2)
    assert status == 200 and body == data
    assert headers["content-type"] == "text/html"
    assert sockets[0].timeouts and sockets[0].closed
    assert all(stream.closed for stream in streams)
    assert all(conn.sock is None for conn in connections)
