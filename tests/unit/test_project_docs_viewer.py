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
