"""오비서 정식 디렉터리 URL(/static/apps/obys/) 이 index.html 을 서빙해야 한다.

StaticFiles(html=False) 는 디렉터리 URL 을 404 로 돌려 직원초대/서명 링크가 깨졌다.
"""
from __future__ import annotations

from fastapi.testclient import TestClient

from app import yeoljeong_main

client = TestClient(yeoljeong_main.app, follow_redirects=False)
_INDEX = (yeoljeong_main._static_dir / "apps" / "obys" / "index.html").read_bytes()


def test_directory_url_serves_index():
    r = client.get("/static/apps/obys/")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/html")
    assert r.content == _INDEX


def test_directory_url_preserves_query():
    r = client.get("/static/apps/obys/?invite=tok123&sign=abc")
    assert r.status_code == 200
    assert r.content == _INDEX


def test_directory_url_head():
    assert client.head("/static/apps/obys/").status_code == 200


def test_no_trailing_slash_redirects_with_query():
    r = client.get("/static/apps/obys?invite=tok123")
    assert r.status_code == 307
    assert r.headers["location"] == "/static/apps/obys/?invite=tok123"


def test_explicit_index_still_served():
    r = client.get("/static/apps/obys/index.html")
    assert r.status_code == 200
    assert r.content == _INDEX


def test_static_module_still_served():
    r = client.get("/static/apps/obys/manifest.webmanifest")
    assert r.status_code == 200


def test_missing_file_stays_404():
    assert client.get("/static/apps/obys/nope.html").status_code == 404


def test_other_static_directories_not_exposed():
    assert client.get("/static/apps/").status_code == 404
    assert client.get("/static/").status_code == 404
    assert client.get("/static/apps/obys/assets/").status_code == 404


def test_api_still_requires_auth():
    assert client.get("/api/v1/obys/workspaces").status_code in (401, 404)
