import json

import pytest

from app.services.google_sheets_service import (
    GOOGLE_SHEETS_SERVICE,
    GoogleSheetsError,
    parse_service_account_json,
    parse_spreadsheet_id,
    records_to_values,
)


def test_parse_spreadsheet_id_accepts_url_or_raw_id():
    sid = "1abcDEF_ghi-123"
    assert parse_spreadsheet_id(sid) == sid
    assert parse_spreadsheet_id(f"https://docs.google.com/spreadsheets/d/{sid}/edit") == sid


def test_parse_spreadsheet_id_rejects_invalid_url():
    with pytest.raises(GoogleSheetsError):
        parse_spreadsheet_id("https://docs.google.com/spreadsheets/no-id")


def test_parse_service_account_json_validates_required_fields():
    payload = {
        "client_email": "svc@example.iam.gserviceaccount.com",
        "private_key": "-----BEGIN PRIVATE KEY-----\\nxxx\\n-----END PRIVATE KEY-----\\n",
        "token_uri": "https://oauth2.googleapis.com/token",
    }

    parsed = parse_service_account_json(json.dumps(payload))

    assert parsed["type"] == "service_account"
    assert parsed["client_email"] == payload["client_email"]


def test_parse_service_account_json_rejects_missing_private_key():
    with pytest.raises(GoogleSheetsError, match="private_key"):
        parse_service_account_json({
            "client_email": "svc@example.iam.gserviceaccount.com",
            "token_uri": "https://oauth2.googleapis.com/token",
        })


def test_records_to_values_uses_stable_union_header_order():
    records = [{"name": "A", "qty": 1}, {"qty": 2, "price": 10}]

    assert records_to_values(records) == [
        ["name", "qty", "price"],
        ["A", 1, ""],
        ["", 2, 10],
    ]


def test_google_sheets_tools_registered():
    from app.api.ceo_chat_tools import TOOL_DEFINITIONS
    from app.services.tool_registry import _TOOLS

    expected = {
        "google_sheets_register",
        "google_sheets_read",
        "google_sheets_update",
        "google_sheets_append",
        "google_sheets_write_records",
        "google_sheets_clear",
        "google_sheets_create",
    }

    assert GOOGLE_SHEETS_SERVICE == "google-sheets"
    assert expected <= {tool["name"] for tool in TOOL_DEFINITIONS}
    assert expected <= set(_TOOLS)


@pytest.mark.asyncio
async def test_tool_executor_forwards_current_session_to_google_sheets(monkeypatch):
    from app.api import ceo_chat_tools
    from app.services.tool_executor import ToolExecutor, current_chat_session_id

    captured = {}

    async def fake_execute_tool(name, params, dsn, chat_session_id=""):
        captured["name"] = name
        captured["params"] = params
        captured["chat_session_id"] = chat_session_id
        return "{}"

    monkeypatch.setattr(ceo_chat_tools, "execute_tool", fake_execute_tool)
    token = current_chat_session_id.set("session-123")
    try:
        result = await ToolExecutor()._google_sheets_read({
            "credential_id": "cred-1",
            "spreadsheet_id": "sheet-1",
            "range_name": "Sheet1!A1",
        })
    finally:
        current_chat_session_id.reset(token)

    assert result == "{}"
    assert captured["name"] == "google_sheets_read"
    assert captured["chat_session_id"] == "session-123"
