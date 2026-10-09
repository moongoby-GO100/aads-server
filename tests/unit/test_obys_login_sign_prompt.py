"""ACCT-OBYS-LOGIN-SIGN-PROMPT-20261009 — 로그인 직후 '서명할 계약서' 안내, 계좌 마스킹, 기본 급여지급일 매월 5일.

파일 모드로만 돈다(운영 DB·업로드 경로·알리고 미접촉). 서명요청 발송·운영 계약 DB 수정은 하지 않는다.
"""
import os
from datetime import timedelta
from pathlib import Path

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

os.environ.setdefault("JWT_SECRET_KEY", "test-only-secret-key-that-is-at-least-32-bytes-long")

from app.api import obys_finance as api
from app.core import obys_tenant
from app.services import aligo_client, yeoljeong_ops_service

svc = api.svc
TENANT = "15055cac-71b0-45ec-b714-7093dde189ff"
MEMBERSHIP = {"tenant_id": TENANT, "status": "active"}
ADMIN = {"email": "owner@example.com", "is_admin": True, "tenant_id": TENANT, "current_membership": MEMBERSHIP}
EMPLOYEE = {"email": "member@example.com", "is_admin": False, "tenant_id": TENANT, "current_membership": MEMBERSHIP}
OTHER_EMPLOYEE = {"email": "other@example.com", "is_admin": False, "tenant_id": TENANT, "current_membership": MEMBERSHIP}
ROOT = Path(__file__).resolve().parents[2] / "app" / "static" / "apps" / "obys"
NOTIFIED: list[dict] = []


def _disable_db(coroutine):
    close = getattr(coroutine, "close", None)
    if close:
        close()
    return None


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    for name in ("OBYS_DATABASE_URL", "YEOLJEONG_FINANCE_DATABASE_URL", "OBYS_CONTRACT_NOTIFY_CHANNELS",
                 "OBYS_CONTRACT_PDF_FONT_PATH", "OBYS_CONTRACT_SIGN_BASE_URL"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("OBYS_UPLOAD_ROOT", str(tmp_path / "upload-root"))
    monkeypatch.setattr(svc, "DATA_DIR", tmp_path / "data")
    monkeypatch.setattr(svc, "UPLOAD_DIR", tmp_path / "data" / "uploads" / "onboarding")
    monkeypatch.setattr(svc, "_run_db", _disable_db)
    NOTIFIED.clear()

    async def fake_create_notification(**kwargs):
        NOTIFIED.append(kwargs)
        return {"id": "ntf", **kwargs}

    monkeypatch.setattr(yeoljeong_ops_service, "create_notification", fake_create_notification)
    monkeypatch.setattr(aligo_client, "is_available", lambda: False)
    svc._write(
        "employee_join_requests",
        [
            _join("join-mia", "가입 직원", EMPLOYEE["email"]),
            _join("join-other", "다른 직원", OTHER_EMPLOYEE["email"]),
            {**_join("join-admin", "점장", "manager@example.com"), "role": "admin"},
        ],
    )


def _join(join_id, name, email):
    return {
        "id": join_id, "name": name, "email": email, "address": "서울시 직원 주소", "phone": "010-1234-5678",
        "birth_date": "1990-01-01", "tenant_id": TENANT, "business_id": "biz-mia", "branch": "열정국밥_미아점",
        "status": "approved",
    }


def _contract_payload(**overrides):
    payload = {
        "employee_request_id": "join-mia", "business_id": "biz-mia", "branch": "열정국밥_미아점",
        "contract_type": "regular", "employment_tax_type": "four_insurance",
        "start_date": "2026-07-22", "contract_date": "2026-07-22", "wage_type": "monthly",
        "wage": 2800000, "workplace": "열정국밥 미아점", "job_description": "매장 운영",
        "work_time": "09:00-18:00", "rest_time": "12:00-13:00", "weekly_hours": "주 40시간",
        "work_days": "주 5일", "holidays": "매주 일요일", "pay_date": "매월 5일",
        "pay_method": "계좌이체", "wage_composition": "기본급 및 법정수당",
        "overtime_terms": "사전 승인 및 법정 가산수당", "leave_terms": "법정 연차유급휴가",
        "insurance_terms": "4대보험 법정 기준 적용",
    }
    payload.update(overrides)
    return payload


def _requested(**overrides):
    saved = svc.save_contract(_contract_payload(**overrides), ADMIN)
    return svc.request_contract_signature(saved["id"], ADMIN)


def _stored(contract_id):
    return next(row for row in svc._read_hr("contracts", ADMIN) if row["id"] == contract_id)


def _age(contract_id, days):
    row = _stored(contract_id)
    row["requested_at"] = (svc._pg_ts(row["requested_at"]) - timedelta(days=days)).isoformat(timespec="seconds")
    svc._write_hr_record("contracts", row, ADMIN)


def _client(user):
    app = FastAPI()
    app.include_router(api.router)
    app.dependency_overrides[api.get_current_user] = lambda: user
    return TestClient(app)


# --- 1. 서명 대기 목록 API ---------------------------------------------------------------------
def test_pending_list_is_own_requested_alive_only_and_never_exposes_sign_token():
    mine = _requested(start_date="2026-06-25")
    token = mine["sign_token"]
    svc.save_contract(_contract_payload(start_date="2026-09-01"), ADMIN)  # draft
    other = svc.save_contract(
        _contract_payload(employee_request_id="join-other", employee_email=OTHER_EMPLOYEE["email"], employee_name="다른 직원"), ADMIN
    )
    svc.request_contract_signature(other["id"], ADMIN)
    expired = _requested(start_date="2026-08-01")
    _age(expired["id"], svc.CONTRACT_SIGN_LINK_TTL_DAYS + 1)
    deleted = _requested(start_date="2026-09-01")
    svc.delete_contract(deleted["id"], ADMIN)

    result = svc.list_pending_signature_contracts(EMPLOYEE)
    assert [item["id"] for item in result["pending"]] == [mine["id"]]
    assert result["count"] == 1
    item = result["pending"][0]
    assert item["business_name"] and item["title"] and item["days_left"] == svc.CONTRACT_SIGN_LINK_TTL_DAYS
    dumped = str(result)
    assert token not in dumped and "sign_token" not in dumped

    body = _client(EMPLOYEE).get("/yeoljeong-finance/contracts/pending-signature").json()
    assert [row["id"] for row in body["pending"]] == [mine["id"]]
    assert token not in str(body) and "sign_token" not in str(body)

    other_result = svc.list_pending_signature_contracts(OTHER_EMPLOYEE)
    assert [row["id"] for row in other_result["pending"]] == [other["id"]]


def test_pending_list_is_empty_for_admin_and_owner_accounts():
    _requested()
    assert svc.list_pending_signature_contracts(ADMIN)["pending"] == []
    owner = {"email": "boss@example.com", "tenant_role": "owner", "tenant_id": TENANT, "current_membership": MEMBERSHIP}
    assert svc.list_pending_signature_contracts(owner) == {"pending": [], "groups": [], "count": 0}
    manager = {"email": "manager@example.com", "is_admin": False, "tenant_id": TENANT, "current_membership": MEMBERSHIP}
    assert svc.list_pending_signature_contracts(manager)["pending"] == []
    assert _client(ADMIN).get("/yeoljeong-finance/contracts/pending-signature").json()["pending"] == []


def test_pending_list_orders_businesses_by_nearest_deadline_and_groups_same_business():
    late_a = _requested(start_date="2026-08-01")
    late_b = _requested(start_date="2026-06-25")
    urgent = _requested(start_date="2026-09-01")
    row = _stored(urgent["id"])
    row["business_id"] = "biz-other"
    row["branch"] = "열정국밥_다른점"
    svc._write_hr_record("contracts", row, ADMIN)
    _age(urgent["id"], 10)

    result = svc.list_pending_signature_contracts(EMPLOYEE)
    assert [group["business_id"] for group in result["groups"]] == ["biz-other", "biz-mia"]
    assert result["groups"][0]["contract_ids"] == [urgent["id"]]
    assert result["groups"][0]["days_left"] == svc.CONTRACT_SIGN_LINK_TTL_DAYS - 10
    assert result["groups"][1]["contract_ids"] == [late_b["id"], late_a["id"]]
    assert [item["id"] for item in result["pending"]] == [urgent["id"], late_b["id"], late_a["id"]]


def test_pending_list_skips_contract_when_join_is_no_longer_approved():
    _requested()
    rows = svc._read_hr("employee_join_requests", ADMIN)
    rows[0]["status"] = "rejected"
    svc._write("employee_join_requests", rows)
    assert svc.list_pending_signature_contracts(EMPLOYEE)["pending"] == []


# --- 2. 지금 서명: id 로 서명 화면 데이터 -----------------------------------------------------------
def test_signing_view_by_id_matches_token_view_and_only_for_owner():
    first = _requested(start_date="2026-06-25")
    second = _requested(start_date="2026-08-01")
    view = svc.get_contract_signing_view_by_id(second["id"], EMPLOYEE)
    assert view == svc.get_contract_signing_view(second["sign_token"], EMPLOYEE)
    assert [item["id"] for item in view["bundle"]] == [first["id"], second["id"]]
    body = _client(EMPLOYEE).get(f"/yeoljeong-finance/contracts/{second['id']}/signing-view")
    assert body.status_code == 200 and body.json()["contract"]["id"] == second["id"]

    with pytest.raises(HTTPException) as other:
        svc.get_contract_signing_view_by_id(second["id"], OTHER_EMPLOYEE)
    assert other.value.status_code == 403
    with pytest.raises(HTTPException) as admin:
        svc.get_contract_signing_view_by_id(second["id"], ADMIN)
    assert admin.value.status_code == 403
    with pytest.raises(HTTPException) as unknown:
        svc.get_contract_signing_view_by_id("no-such-contract-id", EMPLOYEE)
    assert unknown.value.status_code == 403
    _age(second["id"], svc.CONTRACT_SIGN_LINK_TTL_DAYS + 1)
    with pytest.raises(HTTPException) as expired:
        svc.get_contract_signing_view_by_id(second["id"], EMPLOYEE)
    assert expired.value.status_code == 410


# --- 3. 다른 계정으로 링크를 열었을 때 / 기한 지난 링크 -------------------------------------------------
def test_other_account_gets_masked_email_hint_without_leaking_full_address():
    contract = _requested()
    with pytest.raises(HTTPException) as exc:
        svc.get_contract_signing_view(contract["sign_token"], OTHER_EMPLOYEE)
    assert exc.value.status_code == 403
    assert "me***" in exc.value.detail and "@example.com 계정으로 보냈습니다" in exc.value.detail
    assert EMPLOYEE["email"] not in exc.value.detail
    with pytest.raises(HTTPException) as admin:
        svc.get_contract_signing_view(contract["sign_token"], ADMIN)
    assert admin.value.status_code == 403 and EMPLOYEE["email"] not in admin.value.detail


def test_renewal_request_notifies_admins_only_and_never_resends_signature():
    contract = _requested()
    original_token = contract["sign_token"]
    with pytest.raises(HTTPException) as alive:
        svc.request_contract_signature_renewal({"token": original_token}, EMPLOYEE)
    assert alive.value.status_code == 409

    _age(contract["id"], svc.CONTRACT_SIGN_LINK_TTL_DAYS + 1)
    with pytest.raises(HTTPException) as expired:
        svc.get_contract_signing_view(original_token, EMPLOYEE)
    assert expired.value.status_code == 410
    with pytest.raises(HTTPException) as stranger:
        svc.request_contract_signature_renewal({"token": original_token}, OTHER_EMPLOYEE)
    assert stranger.value.status_code == 403
    with pytest.raises(HTTPException) as admin:
        svc.request_contract_signature_renewal({"token": original_token}, ADMIN)
    assert admin.value.status_code == 403
    assert NOTIFIED == []

    result = svc.request_contract_signature_renewal({"token": original_token}, EMPLOYEE)
    assert result["notified"] == 1 and result["status"] == "sent"
    assert [item["target_user"] for item in NOTIFIED] == ["manager@example.com"]
    assert NOTIFIED[0]["notification_type"] == "contract_signature_renewal_requested"
    assert original_token not in str(NOTIFIED)

    stored = _stored(contract["id"])
    assert stored["status"] == "requested" and stored["sign_token"] == original_token
    events = {row["event"] for row in svc._contract_notification_history(TENANT, contract["id"])}
    assert "signature_requested" not in events or events == {"signature_requested", "signature_renewal_requested"}
    assert "signature_renewal_requested" in events

    with pytest.raises(HTTPException) as again:
        svc.request_contract_signature_renewal({"token": original_token}, EMPLOYEE)
    assert again.value.status_code == 429


def test_renewal_api_route_exists_for_employee():
    contract = _requested()
    _age(contract["id"], svc.CONTRACT_SIGN_LINK_TTL_DAYS + 1)
    response = _client(EMPLOYEE).post("/yeoljeong-finance/contracts/signing-renewal", json={"token": contract["sign_token"]})
    assert response.status_code == 200 and response.json()["notified"] == 1


# --- 4. 계약서 계좌: bank_account_masked 칸에 원문이 들어오면 여전히 마스킹한다(전체 번호는 bank_account_number) ----------
@pytest.mark.parametrize(
    "raw, expected",
    [
        ("1002-123-456789", "****-***-**6789"),
        ("우리은행 100212345678", "우리은행 ********5678"),
        ("1002-***-**3886", "1002-***-**3886"),
        ("", ""),
        ("1234", "1234"),
    ],
)
def test_contract_account_is_stored_masked_only(raw, expected):
    assert svc._mask_contract_account(raw) == expected
    saved = svc.save_contract(_contract_payload(bank_name="우리은행", bank_account_masked=raw), ADMIN)
    assert saved["bank_account_masked"] == expected
    assert raw == "" or "123456789" not in str(_stored(saved["id"]))


# --- 5. 테넌트 게이트: 신규 테넌트 직원도 새 경로를 쓴다 ------------------------------------------------------
def test_tenant_gate_opens_exactly_the_three_new_employee_routes():
    base = "/api/v1/yeoljeong-finance/contracts"
    assert obys_tenant._is_contract_signing_route("GET", f"{base}/pending-signature")
    assert obys_tenant._is_contract_signing_route("POST", f"{base}/signing-renewal")
    assert obys_tenant._is_contract_signing_route("GET", f"{base}/abc12345-6789/signing-view")
    assert not obys_tenant._is_contract_signing_route("POST", f"{base}/pending-signature")
    assert not obys_tenant._is_contract_signing_route("GET", f"{base}/pending-signature/extra")
    assert not obys_tenant._is_contract_signing_route("DELETE", f"{base}/abc12345-6789/signing-view")
    assert not obys_tenant._is_contract_signing_route("GET", f"{base}/abc/signing-view")
    assert not obys_tenant._is_contract_signing_route("GET", base)


# --- 6. 정적 화면 고정 문자열 -------------------------------------------------------------------------
def test_default_pay_date_is_the_fifth_everywhere():
    html = (ROOT / "index.html").read_text(encoding="utf-8")
    core = (ROOT / "modules" / "contract-core.js").read_text(encoding="utf-8")
    assert 'payDate: "매월 5일"' in html and 'payDate: "매월 5일"' in core
    assert '<input name="payDate" placeholder="예: 매월 5일">' in html
    assert "매월 10일" not in core
    assert 'payDate: "매월 10일"' not in html and "예: 매월 10일\">" not in html.split('id="contractForm"', 1)[1][:60000]


def test_login_sign_prompt_static_strings_and_sw_cache_bump():
    html = (ROOT / "index.html").read_text(encoding="utf-8")
    assert 'id="signPromptSheet"' in html and 'id="signPendingStrip"' in html
    assert "서명할 계약서 ${count}건" in html
    assert "서명 대기 ${count}건 · 지금 서명" in html
    assert 'id="signPromptNowBtn" class="primary big">지금 서명</button>' in html
    assert 'id="signPromptLaterBtn">나중에</button>' in html
    assert '"/contracts/pending-signature"' in html and "/signing-view`" in html
    assert 'sessionStorage.setItem(SIGN_PROMPT_DISMISSED_KEY, "1")' in html
    assert "if (!hasServerAuth() || canManageOnboarding())" in html
    assert "다시 요청하기" in html and '"/contracts/signing-renewal"' in html
    assert "env(safe-area-inset-bottom" in html
    assert ".sign-prompt-sheet button { min-height: 44px; }" in html
    assert "통장사본 보기" in html and "직접 입력" in html and "bankbook_copy" in html
    assert "maskContractAccountInput(String(data.bankAccountMasked" in html
    assert 'CACHE_VERSION = "obys-clock-shell-20261009-r1"' in (ROOT / "sw.js").read_text(encoding="utf-8")
