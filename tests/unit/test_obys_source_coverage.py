from unittest.mock import AsyncMock

import pytest
from fastapi import FastAPI, HTTPException
from httpx import ASGITransport, AsyncClient

from app.api import acct_source_ledger as source
from app.api import obys_workspaces as api


@pytest.mark.parametrize("code,label", [
    ("1", "미확인"), ("2", "확정"), ("3", "제외(중복/취소)"),
    ("5", "보류"), ("4", "검토 필요"), ("0", "검토 필요"),
    ("", "검토 필요"), (None, "검토 필요"),
])
def test_source_status_does_not_confirm_pending_or_cancelled_rows(code, label):
    row = source._row({"status_code": code}, "purchase")
    assert row["status"] == label
    assert row["status_code"] == (code or "")


@pytest.mark.asyncio
async def test_inventory_separates_cells_from_transactions(monkeypatch):
    monkeypatch.setattr(source, "_authorized_acct_scope", AsyncMock(return_value=(11, 11)))
    fetch = AsyncMock(return_value=[
        {"domain": "sales", "registered_files": 3, "sheets": 2, "files_with_cells": 2, "cells": 25},
        {"domain": "card", "registered_files": 1},
        {"domain": "wehago", "registered_files": 2, "files_with_records": 2, "snapshot_records": 12},
    ])
    monkeypatch.setattr(source, "_fetch_acct_journals", fetch)
    result = await source.source_coverage({"tenant_id": "owned"}, "business")
    assert result["complete"] is False
    assert [row["storage_status"] for row in result["domains"]] == ["cells_only", "registered_only", "records_only"]
    assert result["domains"][0]["spreadsheet_transaction_mapping"] == "not_implemented"
    assert result["domains"][0]["snapshot_records"] == 0
    sql, tenant = fetch.call_args.args
    assert tenant == 11 and "company_id = 11" in sql
    assert "is_current IS TRUE" in sql
    assert "abs_path" not in sql and "raw_text" not in sql


@pytest.mark.asyncio
async def test_inventory_checks_owner_before_remote_read(monkeypatch):
    monkeypatch.setattr(source, "_authorized_acct_scope", AsyncMock(side_effect=HTTPException(404)))
    fetch = AsyncMock()
    monkeypatch.setattr(source, "_fetch_acct_journals", fetch)
    with pytest.raises(HTTPException) as exc:
        await source.source_coverage({}, "foreign")
    assert exc.value.status_code == 404
    fetch.assert_not_awaited()


@pytest.mark.asyncio
async def test_inventory_failure_is_not_empty_success(monkeypatch):
    monkeypatch.setattr(source, "_authorized_acct_scope", AsyncMock(return_value=(11, 11)))
    monkeypatch.setattr(source, "_fetch_acct_journals", AsyncMock(side_effect=HTTPException(502)))
    with pytest.raises(HTTPException) as exc:
        await source.source_coverage({}, "business")
    assert exc.value.status_code == 502


@pytest.mark.asyncio
async def test_coverage_endpoint_reports_actual_route_coverage(monkeypatch):
    monkeypatch.setattr(api, "_business", AsyncMock(return_value={"id": "owned", "name": "회사"}))
    monkeypatch.setattr(source, "source_coverage", AsyncMock(return_value={"domains": [], "complete": False}))
    app = FastAPI()
    app.include_router(api.router)
    app.dependency_overrides[api.get_current_user] = lambda: {"tenant_id": "test"}
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/workspaces/owned/source-coverage")
        assert response.status_code == 200
        result = response.json()
        assert set(result["routes"]) == api.ROUTES
        assert result["routes"]["sales"]["acct_connected"] is True
        assert result["routes"]["journals"]["acct_connected"] is True
        for route in ("cards", "accounts", "tax-evidence", "home"):
            assert result["routes"][route]["acct_connected"] is False
        monkeypatch.setattr(api, "_business", AsyncMock(side_effect=HTTPException(404)))
        source.source_coverage.reset_mock()
        response = await client.get("/workspaces/foreign/source-coverage")
        assert response.status_code == 404
        source.source_coverage.assert_not_awaited()
