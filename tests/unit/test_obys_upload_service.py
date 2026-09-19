from __future__ import annotations

import io
import zipfile
from pathlib import Path

import pytest
from fastapi import HTTPException
from openpyxl import Workbook

from app.services import obys_upload_service as service


def _xlsx(rows: list[list[object]]) -> bytes:
    book = Workbook()
    sheet = book.active
    for row in rows:
        sheet.append(row)
    output = io.BytesIO()
    book.save(output)
    return output.getvalue()


@pytest.mark.parametrize(
    ("filename", "mime", "data"),
    [
        ("proof.pdf", "application/pdf", b"not-pdf"),
        ("proof.png", "image/png", b"not-png"),
        ("proof.jpg", "image/jpeg", b"not-jpeg"),
        ("ledger.csv", "image/png", b"date,amount\n2026-09-19,1"),
    ],
)
def test_signature_and_mime_mismatch_rejected(filename, mime, data):
    with pytest.raises(HTTPException) as exc:
        service._validate_file("sales", filename, mime, data)
    assert exc.value.status_code == 415


@pytest.mark.parametrize("filename", ["../ledger.csv", "folder/ledger.csv", "folder\\ledger.csv", "\x00.csv"])
def test_traversal_rejected(filename):
    with pytest.raises(HTTPException) as exc:
        service._validate_file("sales", filename, "text/csv", b"date,amount\n2026-09-19,1")
    assert exc.value.status_code == 400


def test_ten_megabyte_limit_rejected():
    with pytest.raises(HTTPException) as exc:
        service._validate_file("sales", "ledger.csv", "text/csv", b"x" * (service.MAX_BYTES + 1))
    assert exc.value.status_code == 413


def test_empty_and_malformed_csv_rejected():
    with pytest.raises(HTTPException) as empty:
        service._validate_file("sales", "ledger.csv", "text/csv", b"")
    assert empty.value.status_code == 422
    with pytest.raises(HTTPException) as malformed:
        service._raw_rows(".csv", b"\xff\xff\xff")
    assert malformed.value.status_code == 422


def test_malformed_xlsx_rejected():
    with pytest.raises(HTTPException) as exc:
        service._validate_file("sales", "ledger.xlsx", "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", b"PK\x03\x04broken")
    assert exc.value.status_code == 422


def test_xlsx_zip_bomb_rejected(monkeypatch):
    payload = io.BytesIO()
    with zipfile.ZipFile(payload, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("large.xml", b"0" * (1024 * 1024))
    monkeypatch.setattr(service, "MAX_UNPACKED_BYTES", 1024)
    with pytest.raises(HTTPException) as exc:
        service._validate_file("sales", "ledger.xlsx", "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", payload.getvalue())
    assert exc.value.status_code == 413


def test_csv_and_xlsx_canonical_rows():
    csv_rows = service._raw_rows(".csv", "일자,금액,거래처\n2026-09-19,12000,상점".encode())
    xlsx_rows = service._raw_rows(".xlsx", _xlsx([["일자", "금액", "거래처"], ["2026-09-19", 12000, "상점"]]))
    assert service._canonical("sales", csv_rows)[0][0]["amount"] == 12000
    assert service._canonical("purchase", xlsx_rows)[0][0]["counterparty"] == "상점"


def test_empty_ledger_rows_rejected():
    with pytest.raises(HTTPException) as exc:
        service._raw_rows(".csv", b"date,amount\n")
    assert exc.value.status_code == 422


def test_migration_has_explicit_seed_and_reupload_policy():
    source = Path("migrations/20260919_obys_tenant_uploads.sql").read_text(encoding="utf-8")
    for business_id in ("biz-eonni-naengmyeon", "biz-junghwa", "biz-mia", "biz-sungshin"):
        assert business_id in source
    assert source.count("15055cac-71b0-45ec-b714-7093dde189ff") == 4
    assert "WHERE deleted_at IS NULL" in source
    assert "2d701a8c-9596-4757-8588-faa4f7837112" not in source


def test_api_and_ui_expose_three_durable_upload_categories():
    api = Path("app/api/obys_finance.py").read_text(encoding="utf-8")
    ui = Path("app/static/apps/obys/index.html").read_text(encoding="utf-8")
    assert '@router.get("/uploaded-ledger")' in api
    assert '@router.post("/tenant-registry/businesses"' in api
    for category in ("sales", "purchase", "transaction"):
        assert f'data-ledger-upload="{category}"' in ui
