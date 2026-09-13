import io
import zipfile

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


# ── 공개 교육자료 인덱스 ──


def _seed_education_dir(root):
    """교육자료 · 비교육자료 · 숨김파일 · 하위 디렉토리를 섞어 둔다."""
    root.mkdir(parents=True, exist_ok=True)
    (root / "20260912_mcp_tool_calling_education.html").write_text("<html>a</html>")
    (root / "20260908_multi_agent_ops_education.html").write_text("<html>b</html>")
    # 교육자료가 아닌 이름 — 노출되면 안 된다.
    (root / "20260909_recolumn_cogcom_company_analysis.html").write_text("<html>c</html>")
    (root / "index.html").write_text("<html>portal</html>")
    (root / "notes_education.txt").write_text("not html")
    # 숨김 파일 — 이름은 규칙에 맞아도 노출되면 안 된다.
    (root / ".secret_education.html").write_text("<html>hidden</html>")
    # 하위 디렉토리는 재귀하지 않는다.
    nested = root / "nested"
    nested.mkdir()
    (nested / "20260901_nested_education.html").write_text("<html>nested</html>")
    # 디렉토리 이름이 규칙에 맞아도 파일이 아니면 제외한다.
    (root / "20260902_dir_education.html").mkdir()


@pytest.mark.asyncio
async def test_public_education_index_returns_only_allowlisted_education_files(tmp_path, monkeypatch):
    reports = tmp_path / "reports"
    _seed_education_dir(reports)
    monkeypatch.setattr(
        project_docs,
        "LOCAL_BASE_ALIASES",
        {project_docs.EDUCATION_PUBLIC_BASE: [str(reports)]},
    )

    response = await project_docs.public_education_index()

    names = [item["file"] for item in response["files"]]
    assert names == [
        "20260912_mcp_tool_calling_education.html",
        "20260908_multi_agent_ops_education.html",
    ]
    assert response["total"] == 2
    assert response["status"] == "ok"


@pytest.mark.asyncio
async def test_public_education_index_metadata_never_leaks_paths(tmp_path, monkeypatch):
    reports = tmp_path / "reports"
    _seed_education_dir(reports)
    monkeypatch.setattr(
        project_docs,
        "LOCAL_BASE_ALIASES",
        {project_docs.EDUCATION_PUBLIC_BASE: [str(reports)]},
    )

    response = await project_docs.public_education_index()

    assert set(response) == {"status", "category", "total", "files"}
    for item in response["files"]:
        # 파일명·제목·날짜·크기만. 절대경로/호스트/본문은 없다.
        assert set(item) == {"file", "title", "date", "size"}
        assert "/" not in item["file"]
        for value in item.values():
            assert str(tmp_path) not in str(value)
            assert not str(value).startswith("/")


@pytest.mark.asyncio
async def test_public_education_index_sorts_newest_first(tmp_path, monkeypatch):
    reports = tmp_path / "reports"
    reports.mkdir()
    for name in (
        "20260908_alpha_education.html",
        "20260912_bravo_education.html",
        "20260910_charlie_education.html",
        "20260912_alpha_education.html",
    ):
        (reports / name).write_text("<html></html>")
    monkeypatch.setattr(
        project_docs,
        "LOCAL_BASE_ALIASES",
        {project_docs.EDUCATION_PUBLIC_BASE: [str(reports)]},
    )

    response = await project_docs.public_education_index()

    assert [item["file"] for item in response["files"]] == [
        "20260912_bravo_education.html",
        "20260912_alpha_education.html",
        "20260910_charlie_education.html",
        "20260908_alpha_education.html",
    ]
    assert response["files"][0]["date"] == "2026-09-12"


@pytest.mark.asyncio
async def test_public_education_index_dedupes_container_and_host_aliases(tmp_path, monkeypatch):
    """컨테이너 경로와 호스트 경로가 같은 문서를 가리켜도 한 번만 나와야 한다."""
    first = tmp_path / "container"
    second = tmp_path / "host"
    for root in (first, second):
        root.mkdir()
        (root / "20260912_mcp_tool_calling_education.html").write_text("<html></html>")
    monkeypatch.setattr(
        project_docs,
        "LOCAL_BASE_ALIASES",
        {project_docs.EDUCATION_PUBLIC_BASE: [str(first), str(second)]},
    )

    response = await project_docs.public_education_index()

    assert [item["file"] for item in response["files"]] == [
        "20260912_mcp_tool_calling_education.html"
    ]


@pytest.mark.asyncio
async def test_public_education_index_skips_symlinks(tmp_path, monkeypatch):
    """심볼릭 링크는 디렉토리 밖 파일을 가리킬 수 있으므로 노출하지 않는다."""
    outside = tmp_path / "outside.html"
    outside.write_text("<html>secret</html>")
    reports = tmp_path / "reports"
    reports.mkdir()
    (reports / "20260912_linked_education.html").symlink_to(outside)
    (reports / "20260911_real_education.html").write_text("<html></html>")
    monkeypatch.setattr(
        project_docs,
        "LOCAL_BASE_ALIASES",
        {project_docs.EDUCATION_PUBLIC_BASE: [str(reports)]},
    )

    response = await project_docs.public_education_index()

    assert [item["file"] for item in response["files"]] == [
        "20260911_real_education.html"
    ]


@pytest.mark.asyncio
async def test_public_education_index_is_empty_when_directory_is_missing(tmp_path, monkeypatch):
    monkeypatch.setattr(
        project_docs,
        "LOCAL_BASE_ALIASES",
        {project_docs.EDUCATION_PUBLIC_BASE: [str(tmp_path / "does-not-exist")]},
    )

    response = await project_docs.public_education_index()

    assert response["files"] == []
    assert response["total"] == 0


@pytest.mark.parametrize(
    "name",
    [
        "",
        ".hidden_education.html",
        "..education.html",
        "../20260912_escape_education.html",
        "..%2F20260912_escape_education.html",
        "sub/20260912_nested_education.html",
        "sub\\20260912_nested_education.html",
        "20260912_report_analysis.html",
        "20260912_mcp_tool_calling_education.htm",
        "20260912_mcp_tool_calling_education.html.bak",
        "education.html",
        "20260912 spaced_education.html",
        "20260912_null_education.html\x00.png",
        ".env_education.html",
    ],
)
def test_public_education_basename_filter_blocks_unsafe_names(name):
    assert project_docs._is_public_education_basename(name) is False


@pytest.mark.parametrize(
    "name",
    [
        "20260912_mcp_tool_calling_education.html",
        "multi_agent_ops_education.html",
        "a-b.c_education.html",
    ],
)
def test_public_education_basename_filter_allows_expected_names(name):
    assert project_docs._is_public_education_basename(name) is True


def test_public_education_title_is_derived_from_filename_only():
    title = project_docs._education_title_from_basename(
        "20260912_mcp_tool_calling_education.html"
    )
    assert title == "MCP tool calling"
    assert "<" not in title and ">" not in title


def test_public_education_date_falls_back_to_mtime_without_prefix():
    import time as _time

    mtime = 1_757_721_600
    expected = _time.strftime("%Y-%m-%d", _time.localtime(mtime))

    # 파일명에 YYYYMMDD_ 접두사가 없으면 mtime 날짜를 쓴다.
    assert project_docs._education_date_from_basename("plain_education.html", mtime) == expected
    # 접두사가 있어도 달/일이 말이 안 되면 mtime 으로 되돌아간다.
    assert (
        project_docs._education_date_from_basename("20261399_bad_education.html", mtime)
        == expected
    )
    # 정상 접두사는 그대로 쓴다.
    assert (
        project_docs._education_date_from_basename("20260912_ok_education.html", mtime)
        == "2026-09-12"
    )
