"""ACCT-CONTRACT-BUNDLE-SIGN-20261008 — 같은 직원의 서명요청 계약 여러 건을 서명 1회로 건별 서명 처리.

파일 모드로만 돈다(운영 DB·업로드 경로·알리고 미접촉). 계약서는 합치지 않고 건별로 유지된다.
"""
import base64
import os
from datetime import timedelta
from io import BytesIO

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

os.environ.setdefault("JWT_SECRET_KEY", "test-only-secret-key-that-is-at-least-32-bytes-long")

from app.api import obys_finance as api
from app.services import aligo_client, yeoljeong_ops_service

svc = api.svc
TENANT = "15055cac-71b0-45ec-b714-7093dde189ff"
MEMBERSHIP = {"tenant_id": TENANT, "status": "active"}
ADMIN = {"email": "owner@example.com", "is_admin": True, "tenant_id": TENANT, "current_membership": MEMBERSHIP}
EMPLOYEE = {"email": "member@example.com", "is_admin": False, "tenant_id": TENANT, "current_membership": MEMBERSHIP}
OTHER_EMPLOYEE = {"email": "other@example.com", "is_admin": False, "tenant_id": TENANT, "current_membership": MEMBERSHIP}


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

    async def fake_create_notification(**kwargs):
        return {"id": "ntf", **kwargs}

    monkeypatch.setattr(yeoljeong_ops_service, "create_notification", fake_create_notification)
    monkeypatch.setattr(aligo_client, "is_available", lambda: False)
    svc._write("employee_join_requests", [_join("join-mia", "가입 직원", EMPLOYEE["email"]), _join("join-other", "다른 직원", OTHER_EMPLOYEE["email"])])


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
        "work_days": "주 5일", "holidays": "매주 일요일", "pay_date": "매월 10일",
        "pay_method": "계좌이체", "wage_composition": "기본급 및 법정수당",
        "overtime_terms": "사전 승인 및 법정 가산수당", "leave_terms": "법정 연차유급휴가",
        "insurance_terms": "4대보험 법정 기준 적용",
    }
    payload.update(overrides)
    return payload


def _png() -> bytes:
    from PIL import Image, ImageDraw

    image = Image.new("RGB", (320, 120), "white")
    ImageDraw.Draw(image).line((12, 96, 300, 24), fill="black", width=5)
    buffer = BytesIO()
    image.save(buffer, "PNG")
    return buffer.getvalue()


def _sign_payload(token, *bundle_ids, name="가입 직원"):
    payload = {
        "token": token,
        "signer_name": name,
        "consent": True,
        "consent_version": "yeoljeong-contract-sign-v1",
        "signature_data_uri": "data:image/png;base64," + base64.b64encode(_png()).decode("ascii"),
    }
    if bundle_ids:
        payload["bundle_contract_ids"] = list(bundle_ids)
    return payload


def _client(user):
    app = FastAPI()
    app.include_router(api.router)
    app.dependency_overrides[api.get_current_user] = lambda: user
    return TestClient(app)


def _requested(**overrides):
    saved = svc.save_contract(_contract_payload(**overrides), ADMIN)
    return svc.request_contract_signature(saved["id"], ADMIN)


def _stored(contract_id):
    return next(row for row in svc._read_hr("contracts", ADMIN) if row["id"] == contract_id)


def _pair():
    """홍석빈 사례: 최초 계약(06-25~07-31)과 변경 계약(08-01~). 변경 계약을 먼저 만들어 정렬을 시험한다."""
    later = _requested(start_date="2026-08-01", contract_date="2026-08-01", wage=3000000)
    first = _requested(start_date="2026-06-25", end_date="2026-07-31", contract_date="2026-06-25")
    return first, later


# --- 1. 서명 화면 API ------------------------------------------------------------------------
def test_signing_view_lists_same_employee_requested_contracts_by_start_date():
    first, later = _pair()
    draft = svc.save_contract(_contract_payload(start_date="2026-09-01"), ADMIN)
    signed_other = _requested(start_date="2026-10-01")
    svc.sign_contract(_sign_payload(signed_other["sign_token"]), EMPLOYEE)

    body = _client(EMPLOYEE).get(f"/yeoljeong-finance/contracts/signing/{later['sign_token']}").json()
    assert body["contract"]["id"] == later["id"]
    assert [item["id"] for item in body["bundle"]] == [first["id"], later["id"]]
    assert [item["is_current"] for item in body["bundle"]] == [False, True]
    assert draft["id"] not in {item["id"] for item in body["bundle"]}
    entry = body["bundle"][0]
    assert entry["start_date"] == "2026-06-25" and entry["end_date"] == "2026-07-31" and entry["contract_date"] == "2026-06-25"
    assert entry["title"]
    assert "sign_token" not in entry["contract"] and "sign_token_hash" not in entry["contract"]
    assert first["sign_token"] not in str(body["bundle"])


def test_signing_view_never_includes_other_employee_other_business_expired_or_deleted():
    first = _requested(start_date="2026-06-25")
    other_employee = svc.save_contract(
        _contract_payload(employee_request_id="join-other", employee_email=OTHER_EMPLOYEE["email"], employee_name="다른 직원"), ADMIN
    )
    other_employee = svc.request_contract_signature(other_employee["id"], ADMIN)
    other_business = _requested(start_date="2026-07-01")
    row = _stored(other_business["id"])
    row["business_id"] = "biz-other"
    svc._write_hr_record("contracts", row, ADMIN)
    expired = _requested(start_date="2026-08-01")
    row = _stored(expired["id"])
    row["requested_at"] = (svc._pg_ts(row["requested_at"]) - timedelta(days=svc.CONTRACT_SIGN_LINK_TTL_DAYS + 1)).isoformat(timespec="seconds")
    svc._write_hr_record("contracts", row, ADMIN)
    deleted = _requested(start_date="2026-09-01")
    svc.delete_contract(deleted["id"], ADMIN)

    view = svc.get_contract_signing_view(first["sign_token"], EMPLOYEE)
    assert [item["id"] for item in view["bundle"]] == [first["id"]]
    assert {other_employee["id"], other_business["id"], expired["id"], deleted["id"]}.isdisjoint(item["id"] for item in view["bundle"])


# --- 2. 서명 1회로 건별 서명 ------------------------------------------------------------------
def test_bundle_sign_signs_each_contract_separately_in_start_date_order():
    first, later = _pair()
    result = _client(EMPLOYEE).post(
        "/yeoljeong-finance/contracts/signing", json=_sign_payload(later["sign_token"], first["id"], later["id"])
    )
    assert result.status_code == 200, result.text
    body = result.json()
    assert body["contract"]["id"] == later["id"]
    assert [item["contract_id"] for item in body["bundle"]["results"]] == [first["id"], later["id"]]
    assert all(item["signed_pdf"]["status"] == "stored" for item in body["bundle"]["results"])

    one, two = _stored(first["id"]), _stored(later["id"])
    assert one["status"] == two["status"] == "signed"
    assert svc._pg_ts(one["signed_at"]) < svc._pg_ts(two["signed_at"])
    assert one["signed_snapshot_sha256"] != two["signed_snapshot_sha256"]
    assert one["signed_pdf_path"] and two["signed_pdf_path"] and one["signed_pdf_path"] != two["signed_pdf_path"]
    assert one["signed_pdf_sha256"] != two["signed_pdf_sha256"]
    for row in (one, two):
        assert "sign_token" not in row and row["sign_token_hash"]
        assert row["signature_audit"]["bundle_id"] == body["bundle"]["bundle_id"]
        assert row["signature_audit"]["bundle_contract_ids"] == [first["id"], later["id"]]
        assert row["signature_audit"]["authenticated_email"] == EMPLOYEE["email"]
        assert row["signature_consent"]["accepted"] is True
        assert row["signed_at"] == row["signature_audit"]["signed_at"] == row["signature_consent"]["accepted_at"]
    assert (svc._contract_pdf_root() / one["signed_pdf_path"]).is_file()
    assert (svc._contract_pdf_root() / two["signed_pdf_path"]).is_file()


def test_bundle_signed_at_is_strictly_increasing_even_within_one_second(monkeypatch):
    first, later = _pair()
    third = _requested(start_date="2026-12-01")
    monkeypatch.setattr(svc, "_now", lambda: "2026-10-08T21:00:00+09:00")
    svc.sign_contract_and_deliver(_sign_payload(third["sign_token"], first["id"], later["id"]), EMPLOYEE)
    stamps = [_stored(item["id"])["signed_at"] for item in (first, later, third)]
    parsed = [svc._pg_ts(stamp) for stamp in stamps]
    assert parsed == sorted(parsed) and len(set(parsed)) == 3
    assert stamps[0] == "2026-10-08T21:00:00+09:00"


def test_bundle_may_be_a_subset_and_leaves_unselected_contract_requested():
    first, later = _pair()
    svc.sign_contract_and_deliver(_sign_payload(later["sign_token"], later["id"]), EMPLOYEE)
    assert _stored(later["id"])["status"] == "signed"
    assert "bundle_id" not in _stored(later["id"])["signature_audit"]
    assert _stored(first["id"])["status"] == "requested"


# --- 3. 전부 아니면 전무(409) -----------------------------------------------------------------
def _assert_nothing_signed(*contracts):
    for contract in contracts:
        row = _stored(contract["id"])
        assert row["status"] == "requested", contract["id"]
        assert row.get("sign_token") == contract["sign_token"]
        assert not row.get("signed_at") and not row.get("signed_snapshot")


def test_bundle_with_other_employee_contract_is_409_and_signs_nothing():
    mine = _requested(start_date="2026-08-01")
    theirs = svc.save_contract(
        _contract_payload(employee_request_id="join-other", employee_email=OTHER_EMPLOYEE["email"], employee_name="다른 직원"), ADMIN
    )
    theirs = svc.request_contract_signature(theirs["id"], ADMIN)
    with pytest.raises(HTTPException) as exc:
        svc.sign_contract_and_deliver(_sign_payload(mine["sign_token"], theirs["id"]), EMPLOYEE)
    assert exc.value.status_code == 409
    _assert_nothing_signed(mine, theirs)


def test_bundle_with_other_business_or_unknown_id_is_409_and_signs_nothing():
    mine = _requested(start_date="2026-08-01")
    other_business = _requested(start_date="2026-09-01")
    row = _stored(other_business["id"])
    row["business_id"] = "biz-other"
    svc._write_hr_record("contracts", row, ADMIN)
    for bad in (other_business["id"], "no-such-contract"):
        with pytest.raises(HTTPException) as exc:
            svc.sign_contract_and_deliver(_sign_payload(mine["sign_token"], bad), EMPLOYEE)
        assert exc.value.status_code == 409
    _assert_nothing_signed(mine)
    assert _stored(other_business["id"])["status"] == "requested"


def test_bundle_with_draft_member_is_409_and_signs_nothing():
    mine = _requested(start_date="2026-08-01")
    draft = svc.save_contract(_contract_payload(start_date="2026-06-25"), ADMIN)
    with pytest.raises(HTTPException) as exc:
        svc.sign_contract_and_deliver(_sign_payload(mine["sign_token"], draft["id"]), EMPLOYEE)
    assert exc.value.status_code == 409
    _assert_nothing_signed(mine)
    assert _stored(draft["id"])["status"] == "draft"


def test_bundle_with_already_signed_member_is_409_and_signs_nothing():
    mine = _requested(start_date="2026-08-01")
    done = _requested(start_date="2026-06-25")
    svc.sign_contract(_sign_payload(done["sign_token"]), EMPLOYEE)
    with pytest.raises(HTTPException) as exc:
        svc.sign_contract_and_deliver(_sign_payload(mine["sign_token"], done["id"]), EMPLOYEE)
    assert exc.value.status_code == 409
    _assert_nothing_signed(mine)


def test_bundle_with_expired_member_link_is_409_and_signs_nothing():
    first, later = _pair()
    row = _stored(first["id"])
    row["requested_at"] = (svc._pg_ts(row["requested_at"]) - timedelta(days=svc.CONTRACT_SIGN_LINK_TTL_DAYS + 1)).isoformat(timespec="seconds")
    svc._write_hr_record("contracts", row, ADMIN)
    with pytest.raises(HTTPException) as exc:
        svc.sign_contract_and_deliver(_sign_payload(later["sign_token"], first["id"]), EMPLOYEE)
    assert exc.value.status_code == 409
    assert _stored(later["id"])["status"] == "requested"
    assert _stored(first["id"])["status"] == "requested"


def test_bundle_member_failing_single_contract_validation_is_409():
    first, later = _pair()
    row = _stored(first["id"])
    row["employee_address"] = ""
    svc._write_hr_record("contracts", row, ADMIN)
    with pytest.raises(HTTPException) as exc:
        svc.sign_contract_and_deliver(_sign_payload(later["sign_token"], first["id"]), EMPLOYEE)
    assert exc.value.status_code == 409
    assert _stored(later["id"])["status"] == "requested"


def test_write_failure_midway_rolls_back_already_signed_contracts(monkeypatch):
    first, later = _pair()
    real_write = svc._write_hr_record
    calls = {"signed": 0}

    def flaky(name, record, user):
        if name == "contracts" and record.get("status") == "signed":
            calls["signed"] += 1
            if calls["signed"] == 2:
                raise HTTPException(status_code=404, detail="boom")
        return real_write(name, record, user)

    monkeypatch.setattr(svc, "_write_hr_record", flaky)
    with pytest.raises(HTTPException) as exc:
        svc.sign_contract_and_deliver(_sign_payload(later["sign_token"], first["id"]), EMPLOYEE)
    assert exc.value.status_code == 409
    assert calls["signed"] == 2
    monkeypatch.setattr(svc, "_write_hr_record", real_write)
    _assert_nothing_signed(first, later)
    retry = svc.sign_contract_and_deliver(_sign_payload(later["sign_token"], first["id"]), EMPLOYEE)
    assert {item["contract_id"] for item in retry["bundle"]["results"]} == {first["id"], later["id"]}


def test_bundle_still_requires_consent_name_and_signature_image():
    first, later = _pair()
    for override in ({"consent": False}, {"signer_name": "남의 이름"}, {"signature_data_uri": "data:image/png;base64,AAAA"}):
        with pytest.raises(HTTPException) as exc:
            svc.sign_contract_and_deliver({**_sign_payload(later["sign_token"], first["id"]), **override}, EMPLOYEE)
        assert exc.value.status_code == 400
    _assert_nothing_signed(first, later)


# --- 4. 단건 서명은 그대로 -------------------------------------------------------------------
def test_without_bundle_ids_single_signing_is_unchanged():
    first, later = _pair()
    for payload in (_sign_payload(later["sign_token"]), {**_sign_payload(first["sign_token"]), "bundle_contract_ids": []}):
        result = _client(EMPLOYEE).post("/yeoljeong-finance/contracts/signing", json=payload)
        assert result.status_code == 200, result.text
        assert "bundle" not in result.json()
    assert _stored(first["id"])["status"] == _stored(later["id"])["status"] == "signed"
    assert "bundle_id" not in _stored(first["id"])["signature_audit"]


def test_sign_contract_rejects_bundle_ids_instead_of_silently_signing_one():
    first, later = _pair()
    with pytest.raises(HTTPException) as exc:
        svc.sign_contract(_sign_payload(later["sign_token"], first["id"]), EMPLOYEE)
    assert exc.value.status_code == 400
    _assert_nothing_signed(first, later)


# --- 5. 관리자·비로그인 서명 금지 ------------------------------------------------------------
def test_admin_and_anonymous_cannot_bundle_sign():
    first, later = _pair()
    with pytest.raises(HTTPException) as admin:
        svc.sign_contract_and_deliver(_sign_payload(later["sign_token"], first["id"]), ADMIN)
    assert admin.value.status_code == 403
    with pytest.raises(HTTPException) as anonymous:
        svc.sign_contract_and_deliver(_sign_payload(later["sign_token"], first["id"]), None)
    assert anonymous.value.status_code in {401, 403}
    with pytest.raises(HTTPException) as viewer:
        svc.get_contract_signing_view(later["sign_token"], ADMIN)
    assert viewer.value.status_code == 403
    _assert_nothing_signed(first, later)


def test_other_employee_cannot_bundle_sign_with_my_link():
    first, later = _pair()
    with pytest.raises(HTTPException) as exc:
        svc.sign_contract_and_deliver(_sign_payload(later["sign_token"], first["id"], name="다른 직원"), OTHER_EMPLOYEE)
    assert exc.value.status_code == 403
    _assert_nothing_signed(first, later)


def test_api_payload_rejects_oversized_bundle():
    first, later = _pair()
    ids = [f"id-{n}" for n in range(21)]
    result = _client(EMPLOYEE).post("/yeoljeong-finance/contracts/signing", json=_sign_payload(later["sign_token"], *ids))
    assert result.status_code == 422
    _assert_nothing_signed(first, later)


# --- 6. 관리자 안내 ---------------------------------------------------------------------------
def test_request_signature_tells_admin_how_many_contracts_employee_signs_at_once():
    alone = svc.request_contract_signature_with_notice(svc.save_contract(_contract_payload(start_date="2026-06-25"), ADMIN)["id"], ADMIN)
    assert alone["bundle_notice"]["count"] == 1 and alone["bundle_notice"]["message"] == ""
    second = svc.request_contract_signature_with_notice(svc.save_contract(_contract_payload(start_date="2026-08-01"), ADMIN)["id"], ADMIN)
    assert second["bundle_notice"]["count"] == 2
    assert second["bundle_notice"]["message"] == "직원은 2건을 한 번에 서명합니다"
    assert second["contract"]["status"] == "requested" and second["notify"]["event"] == "signature_requested"


# --- 7. 직원 서명 화면·관리자 화면 고정 문자열 ----------------------------------------------------
def test_signing_screen_static_strings_and_sw_cache_bump():
    from pathlib import Path

    root = Path(__file__).resolve().parents[2] / "app" / "static" / "apps" / "obys"
    html = (root / "index.html").read_text(encoding="utf-8")
    assert 'id="contractSignBundle"' in html
    assert "함께 서명할 계약서 ${pendingContractBundle.length}건" in html
    assert "위 계약서 ${total}건의 내용을 모두 확인했으며 각각에 전자서명합니다" in html
    assert "body.bundle_contract_ids = bundleIds" in html
    assert 'openContractSignModal(contract, payload.bundle || [])' in html
    assert "data-bundle-preview" in html and "data-bundle-contract" in html and "checked ${current ? \"disabled\"" in html
    assert html.count('id="contractSignatureCanvas"') == 1
    assert "payload.bundle_notice?.message" in html
    assert 'CACHE_VERSION = "obys-clock-shell-20261008-r4"' in (root / "sw.js").read_text(encoding="utf-8")
