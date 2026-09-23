import json
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException

from app.api import acct_source_ledger as source, obys_workspaces as api


def account(**changes):
    row = dict(id="bank-1", bank_name="검증은행", account_number_masked="****1234",
               memo=json.dumps(dict(source="stored_wehago_screen", acct_company_id=11,
                                    source_account_code="098001", sha256="a" * 64)))
    row.update(changes)
    return row


@pytest.mark.parametrize("route", ["bank-connect", "integrations"])
@pytest.mark.asyncio
async def test_bank_list_displays_identity_and_auth_without_claiming_sync(monkeypatch, route):
    conn = AsyncMock()
    conn.fetch.return_value = [account(status="needs_auth", connection_type="manual",
                                       last_sync_status="error", last_synced_at="2026-09-01")]
    monkeypatch.setattr(api.upload_svc, "_connect", AsyncMock(return_value=conn))
    rows, _ = await api._source_rows(route=route, user={}, business_id="owned",
                                     date_from=None, date_to=None)
    assert conn.fetch.call_args.args[1] == "owned"
    record = api._record(*rows[0], "검증 사업자")
    assert record["display"]["서비스"] == "검증은행"
    assert record["display"]["계좌·카드"] == "****1234"
    assert record["display"]["상태"] == "인증 필요"
    assert record["display"]["마지막성공"] == "성공 이력 미확인"
    assert record["display"]["연동방식"] == "수동 등록"
    assert api._filter([record], "****1234", "인증 필요") == [record]
    conn.close.assert_awaited_once()


@pytest.mark.asyncio
async def test_account_summary_does_not_present_unknown_balances_as_zero(monkeypatch):
    monkeypatch.setattr(api, "_business", AsyncMock(return_value={"id": "owned", "name": "사업자"}))
    monkeypatch.setattr(api, "_source_rows", AsyncMock(return_value=(
        [("bank-account", account(status="needs_auth"))], "yeoljeong_bank_accounts")))
    result = await api.workspace_summary("owned", "bank-connect", None, None, {})
    assert result["metrics"][0] == {"label": "등록 계좌", "value": "1건"}
    assert result["metrics"][1]["value"] == "1건"
    assert not any("원" == metric["value"][-1:] for metric in result["metrics"])


@pytest.mark.parametrize("name,expected", [
    ("통장덤프_20260827/098001.json", "098001"),
    ("bank_098001_우리.json", "098001"),
    ("통장8_098001.json", "098001"),
    ("통장_098001_우측.json", "098001"),
    ("bank_1_uri.json", ""), ("통장_1.json", ""),
    ("other/098001.json", ""), ("bank_1234567.json", ""),
])
def test_only_explicit_account_codes_are_accepted(name, expected):
    assert source._bank_file_code("/srv/company/.wehago/" + name) == expected


@pytest.mark.asyncio
@pytest.mark.parametrize("accounts,expected", [
    ([account()], "matched"), ([account(), account(id="bank-2")], "conflict"),
    ([account(memo="invalid")], "unresolved"),
    ([account(memo=json.dumps(dict(source="stored_wehago_screen", acct_company_id=12,
                                  source_account_code="098001", sha256="a" * 64)))], "unresolved"),
    ([account(account_number_masked="12345678901234")], "unresolved"),
])
async def test_identity_join_is_scoped_preserves_amount_and_rejects_ambiguity(monkeypatch, accounts, expected):
    fetch = AsyncMock(return_value=[dict(id=100, abs_path="/srv/.wehago/통장덤프_20260827/098001.json"),
                                   dict(id=101, abs_path="/srv/.wehago/bank_1_uri.json")])
    monkeypatch.setattr(source, "_fetch_acct_journals", fetch)
    conn = AsyncMock()
    conn.fetch.return_value = accounts
    monkeypatch.setattr(source.obys_upload_service, "_connect", AsyncMock(return_value=conn))
    rows = [dict(id="acct:100:1", source_file_id=100, total_amount=123, status="확정"),
            dict(id="acct:101:1", source_file_id=101, total_amount=-45, status="보류")]
    await source._enrich_bank_accounts(rows, 11, 11, "owned")
    assert fetch.call_args.args[1] == 11
    assert "company_id=11" in fetch.call_args.args[0]
    assert conn.fetch.call_args.args[1] == "owned"
    assert rows[0]["account_match_status"] == expected
    assert rows[1]["account_match_status"] == "unresolved"
    assert [(r["id"], r["total_amount"], r["status"]) for r in rows] == [
        ("acct:100:1", 123, "확정"), ("acct:101:1", -45, "보류")]
    if expected == "matched":
        assert rows[0]["account_label"] == "검증은행 ****1234"
    else:
        assert "account_id" not in rows[0]


@pytest.mark.asyncio
async def test_unauthorized_scope_prevents_bank_identity_lookup(monkeypatch):
    monkeypatch.setattr(source, "_authorized_acct_scope", AsyncMock(side_effect=HTTPException(404)))
    fetch = AsyncMock()
    monkeypatch.setattr(source, "_enrich_bank_accounts", fetch)
    with pytest.raises(HTTPException):
        await source.source_transactions({}, "foreign", "bank")
    fetch.assert_not_awaited()
