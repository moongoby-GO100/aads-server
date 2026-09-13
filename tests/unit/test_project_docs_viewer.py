import io
import os
import zipfile
from datetime import datetime, timezone
from pathlib import Path

import pytest
from fastapi import HTTPException
from openpyxl import Workbook

from app.api import project_docs


def _write_xlsx(path):
    wb = Workbook()
    ws = wb.active
    ws.title = "Orders"
    ws.append(["name", "amount"])
    ws.append(["OHVIS", 5600])
    wb.save(path)


def test_public_education_index_filters_metadata_and_sorts_newest_first(tmp_path):
    reports_dir = tmp_path / "reports"
    reports_dir.mkdir()

    allowed_old = reports_dir / "20260912_alpha_education.html"
    allowed_new = reports_dir / "20260913_beta-2_education.html"
    allowed_no_date = reports_dir / "topic.v1_education.html"
    for report in (allowed_old, allowed_new, allowed_no_date):
        report.write_text("<title>secret content</title>", encoding="utf-8")

    fallback_timestamp = datetime(2026, 9, 11, 12, tzinfo=timezone.utc).timestamp()
    os.utime(allowed_no_date, (fallback_timestamp, fallback_timestamp))

    for blocked_name in (
        ".hidden_education.html",
        "not-education.html",
        "nested_education.htm",
        "unsafe name_education.html",
    ):
        (reports_dir / blocked_name).write_text("blocked", encoding="utf-8")
    nested = reports_dir / "nested_education.html"
    nested.mkdir()
    (nested / "child_education.html").write_text("blocked", encoding="utf-8")
    (reports_dir / "linked_education.html").symlink_to(allowed_new)

    documents = project_docs._list_public_education_reports(reports_dir)

    assert [item["basename"] for item in documents] == [
        "20260913_beta-2_education.html",
        "20260912_alpha_education.html",
        "topic.v1_education.html",
    ]
    assert documents[0] == {
        "basename": "20260913_beta-2_education.html",
        "title": "beta 2",
        "date": "2026-09-13",
        "size": allowed_new.stat().st_size,
    }
    assert documents[-1]["date"] == "2026-09-11"
    assert all(set(item) == {"basename", "title", "date", "size"} for item in documents)
    assert "secret content" not in repr(documents)
    assert str(reports_dir) not in repr(documents)


@pytest.mark.asyncio
async def test_public_education_index_uses_fixed_server_directory(monkeypatch, tmp_path):
    reports_dir = tmp_path / "reports"
    reports_dir.mkdir()
    report = reports_dir / "20260913_new_topic_education.html"
    report.write_text("new", encoding="utf-8")
    monkeypatch.setattr(project_docs, "PUBLIC_EDUCATION_REPORTS_DIR", reports_dir)

    response = await project_docs.public_education_index()

    assert response == {"documents": [{
        "basename": report.name,
        "title": "new topic",
        "date": "2026-09-13",
        "size": 3,
    }]}


@pytest.mark.asyncio
async def test_project_docs_content_resolves_app_alias_and_converts_xlsx(tmp_path, monkeypatch):
    docs_dir = tmp_path / "docs"
    report_dir = docs_dir / "reports"
    report_dir.mkdir(parents=True)
    xlsx_path = report_dir / "sample.xlsx"
    _write_xlsx(xlsx_path)

    monkeypatch.setattr(
        project_docs,
        "SERVER_CONFIG",
        {
            "AADS": {
                "host": None,
                "paths": [{"base": "/app/docs", "label": "서버 문서"}],
            }
        },
    )
    monkeypatch.setattr(project_docs, "LOCAL_BASE_ALIASES", {"/app/docs": [str(docs_dir)]})

    response = await project_docs.get_doc_content(
        project="AADS",
        base_path="/app/docs",
        file_path="reports/sample.xlsx",
    )

    assert response["encoding"] == "text"
    assert response["mime_type"] == "text/csv"
    assert response["format"] == "excel-csv"
    assert response["converted_from"] == "xlsx"
    assert "## Sheet: Orders" in response["content"]
    assert "OHVIS,5600" in response["content"]


@pytest.mark.asyncio
async def test_project_docs_content_returns_downloadable_binary_for_archive(tmp_path, monkeypatch):
    reports_dir = tmp_path / "reports"
    reports_dir.mkdir()
    zip_path = reports_dir / "bundle.zip"
    with zipfile.ZipFile(zip_path, "w") as zf:
        zf.writestr("readme.txt", "hello")

    monkeypatch.setattr(
        project_docs,
        "SERVER_CONFIG",
        {
            "AADS": {
                "host": None,
                "paths": [{"base": "/app/reports", "label": "서버 리포트"}],
            }
        },
    )
    monkeypatch.setattr(project_docs, "LOCAL_BASE_ALIASES", {"/app/reports": [str(reports_dir)]})

    response = await project_docs.get_doc_content(
        project="AADS",
        base_path="/app/reports",
        file_path="bundle.zip",
    )

    assert response["encoding"] == "base64"
    assert response["is_binary"] is True
    assert response["format"] == "binary"
    assert len(response["content"]) > 0


@pytest.mark.asyncio
async def test_project_docs_content_blocks_sensitive_relative_paths(tmp_path, monkeypatch):
    docs_dir = tmp_path / "docs"
    docs_dir.mkdir()
    (docs_dir / ".env").write_text("SECRET=1", encoding="utf-8")

    monkeypatch.setattr(
        project_docs,
        "SERVER_CONFIG",
        {
            "AADS": {
                "host": None,
                "paths": [{"base": "/app/docs", "label": "서버 문서"}],
            }
        },
    )
    monkeypatch.setattr(project_docs, "LOCAL_BASE_ALIASES", {"/app/docs": [str(docs_dir)]})

    with pytest.raises(HTTPException) as excinfo:
        await project_docs.get_doc_content(
            project="AADS",
            base_path="/app/docs",
            file_path=".env",
        )

    assert excinfo.value.status_code == 400


@pytest.mark.asyncio
async def test_project_docs_content_repairs_legacy_aads_go100_route(monkeypatch):
    async def fake_run_cmd(cmd, timeout=10):
        remote_cmd = cmd[-1]
        if "test -f" in remote_cmd and "/root/kis-autotrade-v4/docs/reports/GO100-303.md" in remote_cmd:
            return "exists"
        if remote_cmd == "cat /root/kis-autotrade-v4/docs/reports/GO100-303.md":
            return "# GO100 report"
        return ""

    monkeypatch.setattr(project_docs, "_run_cmd", fake_run_cmd)

    response = await project_docs.get_doc_content(
        project="AADS",
        base_path="/app/docs",
        file_path="reports/GO100-303.md",
    )

    assert response["project"] == "GO100"
    assert response["file_path"] == "reports/GO100-303.md"
    assert response["full_path"] == "/root/kis-autotrade-v4/docs/reports/GO100-303.md"
    assert response["content"] == "# GO100 report"


@pytest.mark.asyncio
async def test_project_docs_content_repairs_generic_legacy_aads_go100_route(monkeypatch):
    filename = "PRD-WAVE-ENGINE-MODULARIZATION.md"

    async def fake_run_cmd(cmd, timeout=10):
        remote_cmd = cmd[-1]
        expected_path = f"/root/kis-autotrade-v4/docs/{filename}"
        if "test -f" in remote_cmd and expected_path in remote_cmd:
            return "exists"
        if remote_cmd == f"cat {expected_path}":
            return "# GO100 wave engine PRD"
        return ""

    monkeypatch.setattr(project_docs, "_run_cmd", fake_run_cmd)

    response = await project_docs.get_doc_content(
        project="AADS",
        base_path="/app/docs",
        file_path=filename,
    )

    assert response["project"] == "GO100"
    assert response["file_path"] == filename
    assert response["full_path"] == f"/root/kis-autotrade-v4/docs/{filename}"
    assert response["content"] == "# GO100 wave engine PRD"


@pytest.mark.asyncio
async def test_project_docs_content_falls_back_from_go100_reports_to_docs_reports(monkeypatch):
    async def fake_run_cmd(cmd, timeout=10):
        remote_cmd = cmd[-1]
        if "test -f" in remote_cmd and "/root/kis-autotrade-v4/docs/reports/GO100-303.md" in remote_cmd:
            return "exists"
        if remote_cmd == "cat /root/kis-autotrade-v4/docs/reports/GO100-303.md":
            return "# GO100 docs report"
        return ""

    monkeypatch.setattr(project_docs, "_run_cmd", fake_run_cmd)

    response = await project_docs.get_doc_content(
        project="GO100",
        base_path="/root/kis-autotrade-v4/reports",
        file_path="GO100-303.md",
    )

    assert response["project"] == "GO100"
    assert response["file_path"] == "GO100-303.md"
    assert response["full_path"] == "/root/kis-autotrade-v4/docs/reports/GO100-303.md"
    assert response["content"] == "# GO100 docs report"


def test_excel_bytes_to_csv_text_uses_all_sheets():
    wb = Workbook()
    ws = wb.active
    ws.title = "First"
    ws.append(["a", "b"])
    second = wb.create_sheet("Second")
    second.append(["x", "y"])
    stream = io.BytesIO()
    wb.save(stream)

    content = project_docs._excel_bytes_to_csv_text(stream.getvalue(), "multi.xlsx")

    assert "# multi.xlsx CSV preview" in content
    assert "## Sheet: First" in content
    assert "a,b" in content
    assert "## Sheet: Second" in content
    assert "x,y" in content


@pytest.mark.asyncio
async def test_project_docs_scan_include_filter_matches_relative_path(tmp_path, monkeypatch):
    docs_dir = tmp_path / "docs"
    nested = docs_dir / "go100" / "user-guide"
    nested.mkdir(parents=True)
    (nested / "onboarding.md").write_text("# onboarding", encoding="utf-8")
    (docs_dir / "README.md").write_text("# generic", encoding="utf-8")

    results = await project_docs._scan_local(str(docs_dir), include=["go100/"])

    assert [item["path"] for item in results] == ["go100/user-guide/onboarding.md"]


@pytest.mark.asyncio
async def test_go100_document_status_scans_api_and_artifacts_paths(monkeypatch):
    captured_bases = []

    async def fake_scan_remote(host, base, exclude=None, include=None):
        captured_bases.append((host, base, tuple(include or ()), tuple(exclude or ())))
        return []

    monkeypatch.setattr(project_docs, "_scan_remote", fake_scan_remote)

    await project_docs._scan_project("GO100", project_docs.SERVER_CONFIG["GO100"])

    bases = {base for _, base, _, _ in captured_bases}
    assert "/root/kis-autotrade-v4/docs/api" in bases
    assert "/root/kis-autotrade-v4/docs/plans" in bases
    assert "/root/kis-autotrade-v4/docs/handover" in bases
    assert "/root/kis-autotrade-v4/artifacts/go100" in bases

    catch_all = next(
        item for item in captured_bases
        if item[1] == "/root/kis-autotrade-v4/docs"
    )
    assert "api/" in catch_all[3]
    assert "kis-api-portal/" in catch_all[3]


# ── 병렬 세션 병합 가드 ──
# 같은 결함을 서로 다른 브랜치에서 고치는 중이고, 각 브랜치가 공개 면제 집합을
# 다른 이름(_PUBLIC_EXACT_PATHS / _AUTH_EXEMPT_EXACT_PATHS)으로 들고 있다.
# 나중에 둘 다 병합되면 면제 집합이 두 벌 생기고 같은 경로가 두 번 등록되는데,
# FastAPI 는 먼저 등록된 핸들러만 쓰므로 조용히 어긋난다. 이름과 등록 횟수를
# 여기서 고정해서, 병합 사고가 리뷰가 아니라 테스트에서 걸리게 한다.
REPO_ROOT = Path(__file__).resolve().parents[2]


def test_public_readonly_exempt_set_is_single_and_canonical():
    source = (REPO_ROOT / "app" / "main.py").read_text(encoding="utf-8")

    assert source.count("_PUBLIC_READONLY_EXACT_PATHS = {") == 1
    assert "path in _PUBLIC_READONLY_EXACT_PATHS" in source
    for rejected_alias in ("_PUBLIC_EXACT_PATHS", "_AUTH_EXEMPT_EXACT_PATHS"):
        assert rejected_alias not in source

    prefixes_block = source.split("_AUTH_EXEMPT_PREFIXES = (", 1)[1].split(")", 1)[0]
    assert "project-docs" not in prefixes_block


def test_public_education_index_route_is_registered_once():
    source = (REPO_ROOT / "app" / "api" / "project_docs.py").read_text(encoding="utf-8")

    assert source.count('@router.get("/project-docs/public-education-index")') == 1
    assert source.count("async def public_education_index(") == 1
    assert source.count("async def scan_all_docs(") == 1


def test_public_education_index_survives_unstatable_and_invalid_date_entries(tmp_path, monkeypatch):
    """날짜처럼 생겼지만 실제 날짜가 아닌 접두사와, 스캔 도중 사라진 항목을 견딘다."""
    reports_dir = tmp_path / "reports"
    reports_dir.mkdir()
    invalid_date = reports_dir / "20261345_invalid-date_education.html"
    vanishing = reports_dir / "20260913_vanished_education.html"
    for report in (invalid_date, vanishing):
        report.write_text("x", encoding="utf-8")

    fallback_timestamp = datetime(2026, 9, 10, 12, tzinfo=timezone.utc).timestamp()
    os.utime(invalid_date, (fallback_timestamp, fallback_timestamp))

    real_stat = Path.stat

    def stat_that_loses_one_entry(self, *args, **kwargs):
        if self.name == vanishing.name:
            raise OSError("entry removed mid-scan")
        return real_stat(self, *args, **kwargs)

    monkeypatch.setattr(Path, "stat", stat_that_loses_one_entry)

    documents = project_docs._list_public_education_reports(reports_dir)

    assert [item["basename"] for item in documents] == [invalid_date.name]
    assert documents[0]["date"] == "2026-09-10"


def test_public_education_index_returns_empty_when_directory_is_unreadable(tmp_path):
    assert project_docs._list_public_education_reports(tmp_path / "absent") == []


# ── 인증 미들웨어 실제 동작 검증 ──
# 위의 가드들은 app/main.py 소스 텍스트만 본다. 소스가 그대로여도 미들웨어의
# 분기 순서가 바뀌면 면제가 조용히 깨지므로, 실제 app 에 요청을 태워서
# "이 경로만 공개, 형제 경로는 401"을 행위로 못박는다.
@pytest.fixture(scope="module")
def middleware_client():
    from fastapi.testclient import TestClient

    import app.main as main_module

    # with 블록을 쓰지 않아 lifespan(DB 풀 등)은 시작되지 않는다.
    # 인증 미들웨어는 라우팅보다 앞서 돌기 때문에 이것만으로 충분하다.
    return TestClient(main_module.app, raise_server_exceptions=False)


def test_public_education_index_is_reachable_without_auth(middleware_client):
    response = middleware_client.get("/api/v1/project-docs/public-education-index")

    assert response.status_code == 200
    documents = response.json()["documents"]
    assert isinstance(documents, list)
    for item in documents:
        assert set(item) == {"basename", "title", "date", "size"}
        assert "/" not in item["basename"]
        assert not str(item["basename"]).startswith(".")


@pytest.mark.parametrize("path", [
    "/api/v1/project-docs/scan",
    "/api/v1/project-docs/content",
    "/api/v1/project-docs",
    # 면제는 정확히 일치할 때만이다. 슬래시가 붙은 변형은 공개되면 안 된다.
    "/api/v1/project-docs/public-education-index/",
])
def test_sibling_project_doc_routes_still_require_auth(middleware_client, path):
    response = middleware_client.get(path, follow_redirects=False)

    assert response.status_code == 401
