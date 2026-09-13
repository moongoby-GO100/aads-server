"""교육자료 포털의 공개 인덱스 계약 검증.

배경: `app/static/reports/index.html` 은 `/api/v1/project-docs/scan` 을 호출했지만
이 경로는 JWT 미들웨어가 막는다(비로그인 401). 그래서 공개 열람자에게는 새 교육자료가
전혀 보이지 않고, 하드코딩된 목록만 보였다.

여기서는 두 가지를 고정한다.
1) 새로 뚫은 공개 경로는 **정확히 그 경로만** 인증 면제다 — 형제 경로는 여전히 401.
2) 포털 HTML 이 그 좁은 경로를 호출하고, 실패 시 검증된 정적 목록으로 버틴다.
"""
from __future__ import annotations

import os
import re
from pathlib import Path

import pytest

# app.main 은 import 시점에 JWT 시크릿을 요구한다(운영에서 누락되면 기동 실패해야 하므로).
# 단위 테스트는 외부 자격증명 없이 돌아야 해서 더미 값을 넣는다 — 이미 설정돼 있으면 유지.
os.environ.setdefault("JWT_SECRET_KEY", "unit-test-jwt-secret")

PUBLIC_PATH = "/api/v1/project-docs/public-education-index"
PORTAL_HTML = Path(__file__).resolve().parents[2] / "app" / "static" / "reports" / "index.html"


# ── 1) 인증 면제 범위 ──


def _exempt(main_module, path: str) -> bool:
    """미들웨어 1단계(면제 경로) 판정을 그대로 재현한다."""
    return (
        any(path.startswith(p) for p in main_module._AUTH_EXEMPT_PREFIXES)
        or path in main_module._SERVICE_AUTH_EXACT_PATHS
        or path in main_module._PUBLIC_EXACT_PATHS
    )


@pytest.fixture(scope="module")
def main_module():
    from app import main

    return main


def test_public_education_index_is_exempt(main_module):
    assert _exempt(main_module, PUBLIC_PATH) is True


@pytest.mark.parametrize(
    "path",
    [
        "/api/v1/project-docs",
        "/api/v1/project-docs/",
        "/api/v1/project-docs/scan",
        "/api/v1/project-docs/content",
        "/api/v1/project-docs/public-education-index/",
        "/api/v1/project-docs/public-education-index/../scan",
        "/api/v1/project-docs/public-education-index-extra",
        "/api/v1/project-docs/public-education-indexscan",
    ],
)
def test_sibling_project_doc_paths_stay_authenticated(main_module, path):
    """prefix 로 면제했다면 여기서 전부 새어나간다."""
    assert _exempt(main_module, path) is False


def test_no_exempt_prefix_opens_project_docs(main_module):
    """면제 prefix 목록 자체가 project-docs 를 열지 않는지 직접 확인한다."""
    for prefix in main_module._AUTH_EXEMPT_PREFIXES:
        assert not "/api/v1/project-docs".startswith(prefix)
        assert not prefix.startswith("/api/v1/project-docs")


def test_unauthenticated_request_reaches_the_public_route(main_module):
    """실제 미들웨어를 통과시켜 본다 — 인증 헤더 없이 401 이 아니어야 한다."""
    from fastapi.testclient import TestClient

    client = TestClient(main_module.app)
    response = client.get(PUBLIC_PATH)

    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["status"] == "ok"
    assert isinstance(payload["files"], list)
    for item in payload["files"]:
        assert set(item) == {"file", "title", "date", "size"}


def test_unauthenticated_sibling_endpoints_are_401(main_module):
    from fastapi.testclient import TestClient

    client = TestClient(main_module.app)
    for path in ("/api/v1/project-docs/scan", "/api/v1/project-docs/content"):
        assert client.get(path).status_code == 401, path


# ── 2) 포털 HTML 계약 ──


@pytest.fixture(scope="module")
def portal_html() -> str:
    return PORTAL_HTML.read_text(encoding="utf-8")


def test_portal_calls_the_narrow_public_endpoint(portal_html):
    assert PUBLIC_PATH in portal_html
    # 공개 열람자에게 401 을 돌려주던 옛 호출은 남아 있으면 안 된다.
    assert "/api/v1/project-docs/scan" not in portal_html


def test_portal_keeps_the_verified_static_fallback(portal_html):
    """API 실패 시 기존 하드코딩 목록으로 렌더링되어야 한다."""
    assert "const docs = [" in portal_html
    # 검수 완료된 교육자료가 정적 목록에 그대로 남아 있는지 표본 확인.
    for sample in (
        "20260912_mcp_tool_calling_education.html",
        "20260910_ai_learning_theory_education.html",
        "20260908_wiki_knowledge_management_education.html",
    ):
        assert sample in portal_html
    assert "catch (_)" in portal_html


def test_portal_dedupes_against_the_static_list(portal_html):
    assert "known.has(doc.file)" in portal_html
    assert "known.add(doc.file)" in portal_html


def test_portal_filters_filenames_client_side(portal_html):
    assert "EDU_FILENAME_RE" in portal_html
    assert r"^[A-Za-z0-9._-]+_education\.html$" in portal_html


def test_portal_refreshes_the_view_after_merge(portal_html):
    """병합 후 재렌더링하지 않으면 새 문서가 화면에 안 나온다."""
    merge_call = portal_html.index("mergeAutoEducationDocs().then(")
    tail = portal_html[merge_call:]
    assert "updateStats();" in tail
    assert "filterDocs();" in tail


def test_portal_anchor_and_id_targets_exist(portal_html):
    """렌더링 코드가 참조하는 DOM id 가 실제로 문서에 있어야 한다."""
    referenced = set(re.findall(r"getElementById\('([^']+)'\)", portal_html))
    assert {"content", "search", "totalCount", "eduCount"} <= referenced
    for element_id in referenced:
        assert f'id="{element_id}"' in portal_html, element_id


def test_portal_cards_link_to_the_document_file(portal_html):
    """카드 앵커가 파일명을 그대로 href 로 쓴다(정적 /static/reports 경유 열람)."""
    assert '<a class="card" href="${d.file}"' in portal_html
