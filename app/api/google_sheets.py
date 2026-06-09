"""Google Sheets operation API."""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field

from app.auth import TenantRole, require_tenant_role
from app.services.google_sheets_service import GoogleSheetsError, google_sheets_service

router = APIRouter(prefix="/google-sheets", tags=["google-sheets"])
TenantContext = dict[str, Any]
require_tenant_viewer = require_tenant_role(TenantRole.VIEWER)
require_tenant_member = require_tenant_role(TenantRole.MEMBER)


def _tenant_id(context: TenantContext) -> str:
    return str(context["tenant"]["id"])


def _handle_sheets_error(exc: GoogleSheetsError) -> HTTPException:
    text = str(exc)
    status = 400
    if "not found" in text.lower():
        status = 404
    if "not installed" in text.lower():
        status = 503
    return HTTPException(status_code=status, detail=text)


class ServiceAccountRegisterRequest(BaseModel):
    service_account_json: dict[str, Any] | str = Field(
        ...,
        description="Google service-account JSON object or JSON string",
    )
    project: str | None = Field("AADS", description="AADS project scope")
    label: str = Field("default", description="Vault label")
    scopes: list[str] | None = Field(None, description="Optional Google OAuth scopes")


class ValuesWriteRequest(BaseModel):
    range_name: str = Field(..., description="A1 range, e.g. Sheet1!A1")
    values: list[list[Any]] = Field(..., description="2D values matrix")
    value_input_option: str = Field("USER_ENTERED", description="RAW or USER_ENTERED")


class ValuesAppendRequest(ValuesWriteRequest):
    insert_data_option: str = Field("INSERT_ROWS", description="INSERT_ROWS or OVERWRITE")


class ValuesClearRequest(BaseModel):
    range_name: str = Field(..., description="A1 range to clear")


class SpreadsheetCreateRequest(BaseModel):
    credential_id: str = Field(..., description="Google Sheets vault credential id")
    title: str = Field(..., min_length=1, max_length=200)
    sheet_titles: list[str] | None = Field(None, description="Initial sheet names")


class RecordsWriteRequest(BaseModel):
    range_name: str = Field(..., description="A1 range, e.g. Sheet1!A1")
    records: list[dict[str, Any]] = Field(..., description="List of objects to write")
    headers: list[str] | None = Field(None, description="Optional fixed column order")
    include_header: bool = True
    mode: str = Field("update", description="update or append")


@router.post("/credentials/service-account")
async def api_register_service_account(
    body: ServiceAccountRegisterRequest,
    context: TenantContext = Depends(require_tenant_member),
) -> dict[str, Any]:
    try:
        credential = await google_sheets_service.register_service_account(
            service_account_json=body.service_account_json,
            project=body.project,
            label=body.label,
            scopes=body.scopes,
            tenant_id=_tenant_id(context),
        )
        return {"status": "registered", "credential": credential}
    except GoogleSheetsError as exc:
        raise _handle_sheets_error(exc) from exc


@router.get("/{credential_id}/spreadsheets/{spreadsheet_id}/values")
async def api_get_values(
    credential_id: str,
    spreadsheet_id: str,
    range_name: str = Query(..., description="A1 range, e.g. Sheet1!A1:C10"),
    major_dimension: str = Query("ROWS", description="ROWS or COLUMNS"),
    context: TenantContext = Depends(require_tenant_viewer),
) -> dict[str, Any]:
    try:
        return await google_sheets_service.get_values(
            credential_id=credential_id,
            spreadsheet_id=spreadsheet_id,
            range_name=range_name,
            major_dimension=major_dimension,
            tenant_id=_tenant_id(context),
        )
    except GoogleSheetsError as exc:
        raise _handle_sheets_error(exc) from exc


@router.put("/{credential_id}/spreadsheets/{spreadsheet_id}/values")
async def api_update_values(
    credential_id: str,
    spreadsheet_id: str,
    body: ValuesWriteRequest,
    context: TenantContext = Depends(require_tenant_member),
) -> dict[str, Any]:
    try:
        return await google_sheets_service.update_values(
            credential_id=credential_id,
            spreadsheet_id=spreadsheet_id,
            range_name=body.range_name,
            values=body.values,
            value_input_option=body.value_input_option,
            tenant_id=_tenant_id(context),
        )
    except GoogleSheetsError as exc:
        raise _handle_sheets_error(exc) from exc


@router.post("/{credential_id}/spreadsheets/{spreadsheet_id}/append")
async def api_append_values(
    credential_id: str,
    spreadsheet_id: str,
    body: ValuesAppendRequest,
    context: TenantContext = Depends(require_tenant_member),
) -> dict[str, Any]:
    try:
        return await google_sheets_service.append_values(
            credential_id=credential_id,
            spreadsheet_id=spreadsheet_id,
            range_name=body.range_name,
            values=body.values,
            value_input_option=body.value_input_option,
            insert_data_option=body.insert_data_option,
            tenant_id=_tenant_id(context),
        )
    except GoogleSheetsError as exc:
        raise _handle_sheets_error(exc) from exc


@router.post("/{credential_id}/spreadsheets/{spreadsheet_id}/clear")
async def api_clear_values(
    credential_id: str,
    spreadsheet_id: str,
    body: ValuesClearRequest,
    context: TenantContext = Depends(require_tenant_member),
) -> dict[str, Any]:
    try:
        return await google_sheets_service.clear_values(
            credential_id=credential_id,
            spreadsheet_id=spreadsheet_id,
            range_name=body.range_name,
            tenant_id=_tenant_id(context),
        )
    except GoogleSheetsError as exc:
        raise _handle_sheets_error(exc) from exc


@router.post("/spreadsheets")
async def api_create_spreadsheet(
    body: SpreadsheetCreateRequest,
    context: TenantContext = Depends(require_tenant_member),
) -> dict[str, Any]:
    try:
        return await google_sheets_service.create_spreadsheet(
            credential_id=body.credential_id,
            title=body.title,
            sheet_titles=body.sheet_titles,
            tenant_id=_tenant_id(context),
        )
    except GoogleSheetsError as exc:
        raise _handle_sheets_error(exc) from exc


@router.post("/{credential_id}/spreadsheets/{spreadsheet_id}/records")
async def api_write_records(
    credential_id: str,
    spreadsheet_id: str,
    body: RecordsWriteRequest,
    context: TenantContext = Depends(require_tenant_member),
) -> dict[str, Any]:
    try:
        return await google_sheets_service.write_records(
            credential_id=credential_id,
            spreadsheet_id=spreadsheet_id,
            range_name=body.range_name,
            records=body.records,
            headers=body.headers,
            include_header=body.include_header,
            mode=body.mode,
            tenant_id=_tenant_id(context),
        )
    except GoogleSheetsError as exc:
        raise _handle_sheets_error(exc) from exc
