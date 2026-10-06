"""R-DOC 통합 저장소 — 뷰어 경로 해석 회귀(AADS-RDOC-UNIFIED-STORAGE-20261006).

운영 컨테이너에는 /app/reports 가 없고 /app/docs 는 낡은 이미지 사본이다. 최신 문서는
읽기 전용 마운트 /host/aads-server/{docs,reports} 에만 있다. 그 구조를 tmp 로 재현한다.
"""
from __future__ import annotations

from urllib.parse import quote

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from app.api import project_docs

KOREAN_NAME = "20261006_AADS_문서 저장 통합 결과.md"


@pytest.fixture
def layout(tmp_path, monkeypatch):
    """host 마운트(최신)·image 사본(낡음)·없는 /app/reports 를 재현한다."""
    host = tmp_path / "host" / "aads-server"
    (host / "docs").mkdir(parents=True)
    (host / "reports").mkdir(parents=True)
    image_docs = tmp_path / "app" / "docs"
    image_docs.mkdir(parents=True)
    missing_reports = tmp_path / "app" / "reports"  # 만들지 않는다 — 운영과 같다.

    live_docs = [str(host / "docs"), str(image_docs)]
    live_reports = [str(host / "reports"), str(missing_reports)]
    aliases = {
        "/app": [str(tmp_path / "app"), str(host)],
        "/app/docs": list(live_docs),
        "/app/reports": list(live_reports),
        f"{project_docs.HOST_DOC_MOUNT}/docs": list(live_docs),
        f"{project_docs.HOST_DOC_MOUNT}/reports": list(live_reports),
    }
    monkeypatch.setattr(project_docs, "LOCAL_BASE_ALIASES", aliases)
    monkeypatch.setattr(project_docs, "HOST_DOC_MOUNT", str(host))
    monkeypatch.setattr(
        project_docs,
        "SERVER_CONFIG",
        {
            "AADS": {"host": None, "paths": [
                {"base": "/app/docs", "label": "서버 문서"},
                {"base": "/app/reports", "label": "서버 리포트"},
            ]},
            "KIS": {"host": None, "paths": [{"base": "/app/docs", "label": "KIS 문서"}]},
        },
    )
    monkeypatch.setattr(
        project_docs,
        "_AADS_ACCEPTED_ALIAS_BASES",
        (f"{project_docs.HOST_DOC_MOUNT}/docs", f"{project_docs.HOST_DOC_MOUNT}/reports"),
    )
    return {"tmp": tmp_path, "host": host, "image_docs": image_docs}


def test_host_only_report_resolves_under_app_reports_alias(layout):
    (layout["host"] / "reports" / KOREAN_NAME).write_text("# 한글 제목", encoding="utf-8")

    resolved = project_docs._resolve_local_file("AADS", "/app/reports", KOREAN_NAME)

    assert resolved == (layout["host"] / "reports" / KOREAN_NAME).resolve()
    assert resolved.is_file()


@pytest.mark.asyncio
async def test_content_for_report_missing_in_image_is_served_from_host(layout):
    (layout["host"] / "reports" / KOREAN_NAME).write_text("# 한글 제목\n본문", encoding="utf-8")

    res = await project_docs.get_doc_content(
        project="AADS", base_path="/app/reports", file_path=KOREAN_NAME,
    )

    assert res["content"].startswith("# 한글 제목")
    assert res["encoding"] == "text"


@pytest.mark.asyncio
async def test_host_copy_wins_over_stale_image_copy(layout):
    (layout["image_docs"] / "plan.md").write_text("낡은 사본", encoding="utf-8")
    (layout["host"] / "docs" / "plan.md").write_text("최신 사본", encoding="utf-8")

    res = await project_docs.get_doc_content(project="AADS", base_path="/app/docs", file_path="plan.md")

    assert res["content"] == "최신 사본"


@pytest.mark.asyncio
async def test_image_only_docs_still_resolve_old_links(layout):
    (layout["image_docs"] / "legacy.md").write_text("이미지에만 있는 옛 문서", encoding="utf-8")

    res = await project_docs.get_doc_content(project="AADS", base_path="/app/docs", file_path="legacy.md")

    assert res["content"] == "이미지에만 있는 옛 문서"


@pytest.mark.asyncio
async def test_host_mount_base_path_is_accepted_for_aads(layout):
    (layout["host"] / "reports" / "r.md").write_text("호스트 base 직접 지정", encoding="utf-8")

    res = await project_docs.get_doc_content(
        project="AADS", base_path=f"{project_docs.HOST_DOC_MOUNT}/reports", file_path="r.md",
    )

    assert res["content"] == "호스트 base 직접 지정"


@pytest.mark.asyncio
async def test_app_root_base_with_reports_prefix_resolves_from_host(layout):
    (layout["host"] / "reports" / "r.md").write_text("루트 base", encoding="utf-8")

    res = await project_docs.get_doc_content(project="AADS", base_path="/app", file_path="reports/r.md")

    assert res["content"] == "루트 base"


def test_host_mount_base_is_not_accepted_for_other_projects(layout):
    base = f"{project_docs.HOST_DOC_MOUNT}/docs"
    with pytest.raises(HTTPException) as exc:
        project_docs._resolve_local_file("KIS", base, "x.md")
    assert exc.value.status_code == 400


def test_unknown_base_path_is_rejected(layout):
    with pytest.raises(HTTPException) as exc:
        project_docs._resolve_local_file("AADS", "/etc", "passwd")
    assert exc.value.status_code == 400


@pytest.mark.parametrize("bad", ["../secret.md", "reports/../../secret.md", "/etc/passwd", "a/../../b.md"])
def test_path_traversal_is_rejected(layout, bad):
    with pytest.raises(HTTPException) as exc:
        project_docs._resolve_local_file("AADS", "/app/reports", bad)
    assert exc.value.status_code in (400, 403)


@pytest.mark.asyncio
async def test_symlink_escaping_host_mount_is_not_served(layout):
    outside = layout["tmp"] / "outside-secret.md"
    outside.write_text("바깥 비밀", encoding="utf-8")
    (layout["host"] / "reports" / "link.md").symlink_to(outside)

    with pytest.raises(HTTPException) as exc:
        await project_docs.get_doc_content(project="AADS", base_path="/app/reports", file_path="link.md")

    assert exc.value.status_code in (400, 404)
    with pytest.raises(HTTPException):
        project_docs._resolve_local_file("AADS", "/app/reports", "link.md")


@pytest.mark.asyncio
async def test_symlinked_directory_escape_is_not_served(layout):
    outside_dir = layout["tmp"] / "outside-dir"
    outside_dir.mkdir()
    (outside_dir / "x.md").write_text("바깥", encoding="utf-8")
    (layout["host"] / "docs" / "d").symlink_to(outside_dir, target_is_directory=True)

    with pytest.raises(HTTPException) as exc:
        await project_docs.get_doc_content(project="AADS", base_path="/app/docs", file_path="d/x.md")

    assert exc.value.status_code in (400, 404)


@pytest.mark.asyncio
async def test_sensitive_file_in_host_mount_is_still_blocked(layout):
    (layout["host"] / "docs" / "server.pem").write_text("KEY", encoding="utf-8")

    with pytest.raises(HTTPException) as exc:
        await project_docs.get_doc_content(project="AADS", base_path="/app/docs", file_path="server.pem")

    assert exc.value.status_code in (400, 403, 404)


@pytest.mark.asyncio
async def test_scan_lists_host_only_reports_without_duplicates(layout):
    (layout["host"] / "reports" / KOREAN_NAME).write_text("x", encoding="utf-8")
    (layout["host"] / "docs" / "a.md").write_text("x", encoding="utf-8")

    scan = await project_docs._scan_project("AADS", project_docs.SERVER_CONFIG["AADS"])

    files = scan["files"]
    reports = [f for f in files if f["name"] == KOREAN_NAME]
    assert len(reports) == 1 and reports[0]["base_path"] == "/app/reports"
    assert sum(1 for f in files if f["name"] == "a.md") == 1


def test_korean_space_url_roundtrip_through_http(layout):
    (layout["host"] / "reports" / KOREAN_NAME).write_text("# 한글 본문", encoding="utf-8")
    app = FastAPI()
    app.include_router(project_docs.router, prefix="/api/v1")
    client = TestClient(app)

    url = (
        "/api/v1/project-docs/content"
        f"?project=AADS&base_path={quote('/app/reports', safe='')}&file_path={quote(KOREAN_NAME, safe='')}"
    )
    res = client.get(url)

    assert res.status_code == 200, res.text
    assert res.json()["content"].startswith("# 한글 본문")
    assert "%EB" in url and "%20" in url  # 한글·공백이 실제로 퍼센트 인코딩 되었다
