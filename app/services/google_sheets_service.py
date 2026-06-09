"""Google Sheets connector.

The connector uses an encrypted service-account credential stored in the
existing e2e_credentials vault. Google API calls are synchronous, so public
methods run them through asyncio.to_thread to keep FastAPI responsive.
"""
from __future__ import annotations

import asyncio
import json
import re
from typing import Any, Iterable

from app.core.credential_vault import create_credential, get_credential, mark_used

DEFAULT_SCOPES = (
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive.file",
)
GOOGLE_SHEETS_SERVICE = "google-sheets"
_SPREADSHEET_ID_RE = re.compile(r"/spreadsheets/d/([a-zA-Z0-9-_]+)")


class GoogleSheetsError(RuntimeError):
    """Raised for Google Sheets connector failures."""


def parse_spreadsheet_id(value: str) -> str:
    """Accept a raw spreadsheet id or a Google Sheets URL."""
    text = str(value or "").strip()
    if not text:
        raise GoogleSheetsError("spreadsheet_id is required")
    match = _SPREADSHEET_ID_RE.search(text)
    if match:
        return match.group(1)
    if "/" in text or " " in text:
        raise GoogleSheetsError("invalid spreadsheet_id or Google Sheets URL")
    return text


def parse_service_account_json(value: str | dict[str, Any]) -> dict[str, Any]:
    """Validate and normalize a Google service-account JSON payload."""
    if isinstance(value, str):
        try:
            data = json.loads(value)
        except json.JSONDecodeError as exc:
            raise GoogleSheetsError(f"invalid service_account_json: {exc}") from exc
    elif isinstance(value, dict):
        data = dict(value)
    else:
        raise GoogleSheetsError("service_account_json must be an object or JSON string")

    missing = [key for key in ("client_email", "private_key", "token_uri") if not data.get(key)]
    if missing:
        raise GoogleSheetsError(f"service_account_json missing required fields: {', '.join(missing)}")
    data.setdefault("type", "service_account")
    return data


def normalize_values(values: Iterable[Any]) -> list[list[Any]]:
    """Normalize Google Sheets values into list[list[Any]]."""
    if values is None:
        raise GoogleSheetsError("values is required")
    normalized: list[list[Any]] = []
    for row in values:
        if isinstance(row, dict):
            raise GoogleSheetsError("dict rows require write_records() so headers are deterministic")
        if isinstance(row, (list, tuple)):
            normalized.append(list(row))
        else:
            normalized.append([row])
    if not normalized:
        raise GoogleSheetsError("values must contain at least one row")
    return normalized


def records_to_values(
    records: list[dict[str, Any]],
    *,
    headers: list[str] | None = None,
    include_header: bool = True,
) -> list[list[Any]]:
    """Convert records into a Sheets value matrix."""
    if not records:
        raise GoogleSheetsError("records must contain at least one row")
    if headers:
        columns = [str(column) for column in headers]
    else:
        seen: list[str] = []
        for record in records:
            for key in record.keys():
                if key not in seen:
                    seen.append(str(key))
        columns = seen
    rows = [[record.get(column, "") for column in columns] for record in records]
    return [columns, *rows] if include_header else rows


class GoogleSheetsService:
    """Read/write Google Sheets using a vault-backed service account."""

    async def register_service_account(
        self,
        *,
        service_account_json: str | dict[str, Any],
        tenant_id: str,
        project: str | None = "AADS",
        label: str = "default",
        scopes: list[str] | None = None,
    ) -> dict[str, Any]:
        data = parse_service_account_json(service_account_json)
        selected_scopes = scopes or list(DEFAULT_SCOPES)
        result = await create_credential(
            service=GOOGLE_SHEETS_SERVICE,
            username=str(data["client_email"]),
            password=str(data["private_key"]),
            project=project,
            label=label or "default",
            login_url="https://sheets.google.com",
            extra_fields={
                "service_account_json": json.dumps(data, ensure_ascii=False, separators=(",", ":")),
                "scopes": json.dumps(selected_scopes, ensure_ascii=False),
                "client_email": str(data["client_email"]),
                "project_id": str(data.get("project_id") or ""),
            },
            tenant_id=tenant_id,
        )
        return {
            "credential_id": result["id"],
            "service": GOOGLE_SHEETS_SERVICE,
            "project": result.get("project"),
            "label": result.get("label"),
            "client_email": data["client_email"],
            "scopes": selected_scopes,
        }

    async def get_values(
        self,
        *,
        credential_id: str,
        spreadsheet_id: str,
        range_name: str,
        tenant_id: str,
        major_dimension: str = "ROWS",
    ) -> dict[str, Any]:
        sheets = await self._build_sheets_client(credential_id, tenant_id)
        sid = parse_spreadsheet_id(spreadsheet_id)

        def _call() -> dict[str, Any]:
            return (
                sheets.spreadsheets()
                .values()
                .get(spreadsheetId=sid, range=range_name, majorDimension=major_dimension)
                .execute()
            )

        result = await self._run_google_call(_call)
        await mark_used(credential_id, tenant_id=tenant_id)
        return {
            "spreadsheet_id": sid,
            "range": result.get("range", range_name),
            "major_dimension": result.get("majorDimension", major_dimension),
            "values": result.get("values", []),
        }

    async def update_values(
        self,
        *,
        credential_id: str,
        spreadsheet_id: str,
        range_name: str,
        values: list[list[Any]],
        tenant_id: str,
        value_input_option: str = "USER_ENTERED",
    ) -> dict[str, Any]:
        sheets = await self._build_sheets_client(credential_id, tenant_id)
        sid = parse_spreadsheet_id(spreadsheet_id)
        normalized = normalize_values(values)

        def _call() -> dict[str, Any]:
            return (
                sheets.spreadsheets()
                .values()
                .update(
                    spreadsheetId=sid,
                    range=range_name,
                    valueInputOption=value_input_option,
                    body={"values": normalized},
                )
                .execute()
            )

        result = await self._run_google_call(_call)
        await mark_used(credential_id, tenant_id=tenant_id)
        return {
            "spreadsheet_id": sid,
            "range": result.get("updatedRange", range_name),
            "updated_rows": result.get("updatedRows", 0),
            "updated_columns": result.get("updatedColumns", 0),
            "updated_cells": result.get("updatedCells", 0),
        }

    async def append_values(
        self,
        *,
        credential_id: str,
        spreadsheet_id: str,
        range_name: str,
        values: list[list[Any]],
        tenant_id: str,
        value_input_option: str = "USER_ENTERED",
        insert_data_option: str = "INSERT_ROWS",
    ) -> dict[str, Any]:
        sheets = await self._build_sheets_client(credential_id, tenant_id)
        sid = parse_spreadsheet_id(spreadsheet_id)
        normalized = normalize_values(values)

        def _call() -> dict[str, Any]:
            return (
                sheets.spreadsheets()
                .values()
                .append(
                    spreadsheetId=sid,
                    range=range_name,
                    valueInputOption=value_input_option,
                    insertDataOption=insert_data_option,
                    body={"values": normalized},
                )
                .execute()
            )

        result = await self._run_google_call(_call)
        updates = result.get("updates", {})
        await mark_used(credential_id, tenant_id=tenant_id)
        return {
            "spreadsheet_id": sid,
            "table_range": result.get("tableRange", ""),
            "range": updates.get("updatedRange", range_name),
            "updated_rows": updates.get("updatedRows", 0),
            "updated_columns": updates.get("updatedColumns", 0),
            "updated_cells": updates.get("updatedCells", 0),
        }

    async def clear_values(
        self,
        *,
        credential_id: str,
        spreadsheet_id: str,
        range_name: str,
        tenant_id: str,
    ) -> dict[str, Any]:
        sheets = await self._build_sheets_client(credential_id, tenant_id)
        sid = parse_spreadsheet_id(spreadsheet_id)

        def _call() -> dict[str, Any]:
            return (
                sheets.spreadsheets()
                .values()
                .clear(spreadsheetId=sid, range=range_name, body={})
                .execute()
            )

        result = await self._run_google_call(_call)
        await mark_used(credential_id, tenant_id=tenant_id)
        return {
            "spreadsheet_id": sid,
            "cleared_range": result.get("clearedRange", range_name),
        }

    async def create_spreadsheet(
        self,
        *,
        credential_id: str,
        title: str,
        tenant_id: str,
        sheet_titles: list[str] | None = None,
    ) -> dict[str, Any]:
        sheets = await self._build_sheets_client(credential_id, tenant_id)
        sheets_payload = [{"properties": {"title": name}} for name in (sheet_titles or ["Sheet1"])]

        def _call() -> dict[str, Any]:
            return (
                sheets.spreadsheets()
                .create(body={"properties": {"title": title}, "sheets": sheets_payload})
                .execute()
            )

        result = await self._run_google_call(_call)
        await mark_used(credential_id, tenant_id=tenant_id)
        return {
            "spreadsheet_id": result.get("spreadsheetId"),
            "spreadsheet_url": result.get("spreadsheetUrl"),
            "title": result.get("properties", {}).get("title", title),
            "sheets": [
                sheet.get("properties", {}).get("title", "")
                for sheet in result.get("sheets", [])
            ],
        }

    async def write_records(
        self,
        *,
        credential_id: str,
        spreadsheet_id: str,
        range_name: str,
        records: list[dict[str, Any]],
        tenant_id: str,
        headers: list[str] | None = None,
        include_header: bool = True,
        mode: str = "update",
    ) -> dict[str, Any]:
        values = records_to_values(records, headers=headers, include_header=include_header)
        if mode == "append":
            return await self.append_values(
                credential_id=credential_id,
                spreadsheet_id=spreadsheet_id,
                range_name=range_name,
                values=values,
                tenant_id=tenant_id,
            )
        return await self.update_values(
            credential_id=credential_id,
            spreadsheet_id=spreadsheet_id,
            range_name=range_name,
            values=values,
            tenant_id=tenant_id,
        )

    async def _build_sheets_client(self, credential_id: str, tenant_id: str) -> Any:
        credential = await get_credential(credential_id, include_secrets=True, tenant_id=tenant_id)
        if not credential:
            raise GoogleSheetsError("google sheets credential not found")
        if credential.get("service") != GOOGLE_SHEETS_SERVICE:
            raise GoogleSheetsError(f"credential service must be {GOOGLE_SHEETS_SERVICE}")

        extra = credential.get("extra_fields") or {}
        raw_service_account = extra.get("service_account_json")
        if not raw_service_account:
            raise GoogleSheetsError("credential is missing service_account_json")
        data = parse_service_account_json(raw_service_account)
        scopes = extra.get("scopes")
        if isinstance(scopes, str):
            try:
                scopes = json.loads(scopes)
            except json.JSONDecodeError:
                scopes = list(DEFAULT_SCOPES)
        if not isinstance(scopes, list) or not scopes:
            scopes = list(DEFAULT_SCOPES)

        try:
            from google.oauth2 import service_account
            from googleapiclient.discovery import build
        except ImportError as exc:
            raise GoogleSheetsError(
                "Google Sheets dependencies are not installed: google-api-python-client, google-auth"
            ) from exc

        credentials = service_account.Credentials.from_service_account_info(data, scopes=scopes)
        return build("sheets", "v4", credentials=credentials, cache_discovery=False)

    async def _run_google_call(self, func: Any) -> dict[str, Any]:
        try:
            return await asyncio.to_thread(func)
        except Exception as exc:
            raise GoogleSheetsError(str(exc)) from exc


google_sheets_service = GoogleSheetsService()
