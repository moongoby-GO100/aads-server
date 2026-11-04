"""OBYS-CONTRACT-PDF-MOBILE-DOWNLOAD — 서명본 PDF 1회용 링크, UA 요약, 교부 기록."""
import base64
import hashlib
import json
import os
from datetime import timedelta
from io import BytesIO
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

os.environ.setdefault("JWT_SECRET_KEY", "test-only-secret-key-that-is-at-least-32-bytes-long")

from app.api import obys_finance as api
from app.core import obys_tenant
from app.services import aligo_client, yeoljeong_ops_service
from app.services.obys_client_summary import summarize_client

svc = api.svc
ROOT = Path(__file__).resolve().parents[2]
TENANT = "15055cac-71b0-45ec-b714-7093dde189ff"
OTHER_TENANT = "2a1b3c4d-0000-4000-8000-000000000001"
MEMBERSHIP = {"tenant_id": TENANT, "status": "active"}
ADMIN = {"email": "owner@example.com", "is_admin": True, "tenant_id": TENANT, "current_membership": MEMBERSHIP}
EMPLOYEE = {"email": "member@example.com", "is_admin": False, "tenant_id": TENANT, "current_membership": MEMBERSHIP}
EMPLOYEE_TWO = {"email": "second@example.com", "is_admin": False, "tenant_id": TENANT, "current_membership": MEMBERSHIP}
THIRD_PARTY = {"email": "other@example.com", "is_admin": False, "tenant_id": TENANT, "current_membership": MEMBERSHIP}
OTHER_TENANT_ADMIN = {
    "email": "boss@other.example.com",
    "is_admin": True,
    "tenant_id": OTHER_TENANT,
    "current_membership": {"tenant_id": OTHER_TENANT, "status": "active"},
}
KAKAO_ANDROID_UA = (
    "Mozilla/5.0 (Linux; Android 14; SM-S918N Build/UP1A.231005.007; wv) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Version/4.0 Chrome/126.0.0.0 Mobile Safari/537.36 KAKAOTALK 2511560"
)


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
        return {"id": "ntf-1", **kwargs}

    monkeypatch.setattr(yeoljeong_ops_service, "create_notification", fake_create_notification)
    monkeypatch.setattr(aligo_client, "is_available", lambda: False)
    svc._write("employee_join_requests", [
        {
            "id": "join-mia", "name": "가입 직원", "email": EMPLOYEE["email"], "address": "서울시 직원 주소",
            "phone": "010-1234-5678", "birth_date": "1990-01-01", "tenant_id": TENANT, "business_id": "biz-mia",
            "branch": "열정국밥_미아점", "status": "approved",
        },
        {
            "id": "join-two", "name": "두번째 직원", "email": EMPLOYEE_TWO["email"], "address": "서울시 두번째 주소",
            "phone": "010-2222-3333", "birth_date": "1991-02-02", "tenant_id": TENANT, "business_id": "biz-mia",
            "branch": "열정국밥_미아점", "status": "approved",
        },
    ])
    pytest.importorskip("reportlab")
    return {"tmp": tmp_path}


def _contract_payload(request_id="join-mia", wage=2800000):
    return {
        "employee_request_id": request_id, "business_id": "biz-mia", "branch": "열정국밥_미아점",
        "contract_type": "regular", "employment_tax_type": "four_insurance",
        "start_date": "2026-07-22", "contract_date": "2026-07-22", "wage_type": "monthly",
        "wage": wage, "workplace": "열정국밥 미아점", "job_description": "매장 운영",
        "work_time": "09:00-18:00", "rest_time": "12:00-13:00", "weekly_hours": "주 40시간",
        "work_days": "주 5일", "holidays": "매주 일요일", "pay_date": "매월 10일",
        "pay_method": "계좌이체", "wage_composition": "기본급 및 법정수당",
        "overtime_terms": "사전 승인 및 법정 가산수당", "leave_terms": "법정 연차유급휴가",
        "insurance_terms": "4대보험 법정 기준 적용",
    }


def _signature_png() -> bytes:
    from PIL import Image, ImageDraw

    image = Image.new("RGB", (320, 120), "white")
    ImageDraw.Draw(image).line((12, 96, 300, 24), fill="black", width=5)
    buffer = BytesIO()
    image.save(buffer, "PNG")
    return buffer.getvalue()


def _signed_contract(request_id="join-mia", signer=EMPLOYEE, name="가입 직원", wage=2800000):
    saved = svc.save_contract(_contract_payload(request_id, wage), ADMIN)
    requested = svc.request_contract_signature_with_notice(saved["id"], ADMIN)["contract"]
    payload = {
        "token": requested["sign_token"], "signer_name": name, "consent": True,
        "consent_version": "yeoljeong-contract-sign-v1",
        "signature_data_uri": "data:image/png;base64," + base64.b64encode(_signature_png()).decode("ascii"),
        "audit_ip": "203.0.113.10", "audit_user_agent": "pytest",
    }
    return svc.sign_contract_and_deliver(payload, signer)["contract"]


def _app(user=None):
    app = FastAPI()
    app.include_router(api.router)
    app.include_router(api.signed_pdf_link_router)
    if user is not None:
        app.dependency_overrides[api.get_current_user] = lambda: user
    return TestClient(app)


def _issue(user, contract_id, ua="pytest-agent"):
    return _app(user).post(f"/yeoljeong-finance/contracts/{contract_id}/signed-pdf/link", headers={"user-agent": ua})


def _delivered_rows():
    return [row for row in svc._read_file_rows(svc.CONTRACT_NOTIFICATION_LOG) if row["event"] == "delivered"]


def _audit(action):
    return [row for row in svc._read_file_rows(svc.CONTRACT_AUDIT_LOG) if row["action"] == action]


# --- 발급 권한 ---------------------------------------------------------------


def test_link_issued_only_to_authorized_users():
    contract = _signed_contract()
    assert _issue(THIRD_PARTY, contract["id"]).status_code == 403
    assert _issue(OTHER_TENANT_ADMIN, contract["id"]).status_code == 403
    assert _issue(OTHER_TENANT_ADMIN, "no-such-contract").status_code == 403
    assert _issue(ADMIN, contract["id"]).status_code == 200
    assert _issue(EMPLOYEE, contract["id"]).status_code == 200


def test_unsigned_contract_gets_no_link():
    saved = svc.save_contract(_contract_payload(), ADMIN)
    assert _issue(ADMIN, saved["id"]).status_code == 409


def test_issue_response_shape_and_ttl():
    contract = _signed_contract()
    response = _issue(EMPLOYEE, contract["id"])
    body = response.json()
    assert response.headers["cache-control"] == "no-store"
    assert body["url"].startswith("/api/v1/yeoljeong-finance/contracts/signed-pdf/t/")
    token = body["url"].rsplit("/", 1)[1]
    assert len(token) >= 43
    assert 0 < body["expires_in"] <= 300


def test_server_stores_only_token_hash_and_no_raw_secrets():
    contract = _signed_contract()
    token = _issue(EMPLOYEE, contract["id"], ua=KAKAO_ANDROID_UA).json()["url"].rsplit("/", 1)[1]
    _app().get(f"/yeoljeong-finance/contracts/signed-pdf/t/{token}", headers={"user-agent": KAKAO_ANDROID_UA})

    rows = svc._read_file_rows(svc.SIGNED_PDF_LINK_LOG)
    assert len(rows) == 1 and rows[0]["token_hash"] == hashlib.sha256(token.encode()).hexdigest()
    persisted = "".join(
        path.read_text(encoding="utf-8") for path in (svc.DATA_DIR).glob("*.json")
    )
    assert token not in persisted
    assert KAKAO_ANDROID_UA not in persisted


# --- 열기: 1회·만료·위조 --------------------------------------------------------


def test_token_opens_pdf_inline_once_then_410():
    contract = _signed_contract()
    url = _issue(EMPLOYEE, contract["id"]).json()["url"]
    client = _app()  # 로그인 의존성 override 없음 — 쿠키·헤더 없이 열려야 한다.

    first = client.get(url.replace("/api/v1", ""))
    assert first.status_code == 200
    assert first.headers["content-type"] == "application/pdf"
    disposition = first.headers["content-disposition"]
    assert disposition.startswith("inline;") and "filename*=UTF-8''" in disposition
    assert hashlib.sha256(first.content).hexdigest() == contract["signed_pdf_sha256"]
    assert first.headers["cache-control"] == "no-store"

    second = client.get(url.replace("/api/v1", ""))
    assert second.status_code == 410


def test_expired_token_is_410(monkeypatch):
    contract = _signed_contract()
    url = _issue(EMPLOYEE, contract["id"]).json()["url"].replace("/api/v1", "")
    real_now = svc._now_dt
    monkeypatch.setattr(svc, "_now_dt", lambda: real_now() + timedelta(seconds=svc.SIGNED_PDF_LINK_TTL_SECONDS + 1))
    assert _app().get(url).status_code == 410


def test_forged_and_malformed_tokens_are_404():
    contract = _signed_contract()
    _issue(EMPLOYEE, contract["id"])
    client = _app()
    assert client.get("/yeoljeong-finance/contracts/signed-pdf/t/" + "A" * 43).status_code == 404
    assert client.get("/yeoljeong-finance/contracts/signed-pdf/t/short").status_code == 404
    assert client.get("/yeoljeong-finance/contracts/signed-pdf/t/" + "!" * 40).status_code == 404


def test_token_is_bound_to_its_own_contract():
    first = _signed_contract()
    second = _signed_contract("join-two", EMPLOYEE_TWO, "두번째 직원", wage=3100000)
    assert first["signed_pdf_sha256"] != second["signed_pdf_sha256"]

    url_first = _issue(EMPLOYEE, first["id"]).json()["url"].replace("/api/v1", "")
    url_second = _issue(EMPLOYEE_TWO, second["id"]).json()["url"].replace("/api/v1", "")
    client = _app()
    assert hashlib.sha256(client.get(url_first).content).hexdigest() == first["signed_pdf_sha256"]
    assert hashlib.sha256(client.get(url_second).content).hexdigest() == second["signed_pdf_sha256"]
    # 직원 2 는 직원 1 의 계약서 링크를 받을 수 없다.
    assert _issue(EMPLOYEE_TWO, first["id"]).status_code == 403


def test_token_route_rejected_after_contract_tampering():
    contract = _signed_contract()
    url = _issue(EMPLOYEE, contract["id"]).json()["url"].replace("/api/v1", "")
    rows = svc._read_file_rows("contracts")
    svc._find(rows, contract["id"])["wage"] = 1
    svc._write_file_rows("contracts", rows)
    assert _app().get(url).status_code == 409
    # 검증 실패는 토큰을 소모하지 않는다 — 되돌리면 같은 링크가 열린다.
    svc._find(rows, contract["id"])["wage"] = contract["wage"]
    svc._write_file_rows("contracts", rows)
    assert _app().get(url).status_code == 200
    assert _app().get(url).status_code == 410


def test_missing_pdf_file_does_not_consume_token(monkeypatch):
    contract = _signed_contract()
    url = _issue(EMPLOYEE, contract["id"]).json()["url"].replace("/api/v1", "")
    real = svc._verified_signed_pdf_path
    monkeypatch.setattr(svc, "_verified_signed_pdf_path", lambda _contract: None)
    assert _app().get(url).status_code == 503
    assert _delivered_rows() == []
    monkeypatch.setattr(svc, "_verified_signed_pdf_path", real)
    assert _app().get(url).status_code == 200


def test_range_continuations_after_first_use_are_served_but_plain_reuse_is_410(monkeypatch):
    contract = _signed_contract()
    url = _issue(EMPLOYEE, contract["id"]).json()["url"].replace("/api/v1", "")
    client = _app()
    pdf = None

    first = client.get(url, headers={"Range": "bytes=0-1023"})
    assert first.status_code == 206
    assert first.headers["content-type"] == "application/pdf"
    assert first.headers["content-disposition"].startswith("inline;")
    pdf = first.content

    # PDF 뷰어가 나머지를 이어받는다.
    rest = client.get(url, headers={"Range": "bytes=1024-"})
    assert rest.status_code == 206
    assert hashlib.sha256(pdf + rest.content).hexdigest() == contract["signed_pdf_sha256"]

    # Range 없는 재요청은 유예 안이어도 재사용이다.
    assert client.get(url).status_code == 410
    assert len(_delivered_rows()) == 1
    assert len(_audit("contract.signed_pdf_access")) == 1
    # 이어받기 허용은 매번 감사 기록이 남는다.
    assert [row["details"]["range_hit"] for row in _audit("contract.signed_pdf_range_access")] == [1]

    # 유예가 지나면 Range 도 닫힌다.
    real_now = svc._now_dt
    monkeypatch.setattr(svc, "_now_dt", lambda: real_now() + timedelta(seconds=svc.SIGNED_PDF_LINK_RANGE_GRACE_SECONDS + 1))
    assert client.get(url, headers={"Range": "bytes=0-9"}).status_code == 410


def test_range_from_another_client_is_410_and_leaves_no_access_trail():
    contract = _signed_contract()
    url = _issue(EMPLOYEE, contract["id"]).json()["url"].replace("/api/v1", "")
    client = _app()
    assert client.get(url, headers={"Range": "bytes=0-9", "X-Forwarded-For": "198.51.100.7"}).status_code == 206

    stranger = client.get(url, headers={"Range": "bytes=0-", "X-Forwarded-For": "203.0.113.99"})
    assert stranger.status_code == 410
    assert b"%PDF" not in stranger.content
    # 클라이언트가 보낸 앞쪽 값을 흉내내도 Apache 가 붙인 마지막 항목이 요청자다.
    spoofed = client.get(url, headers={"Range": "bytes=0-", "X-Forwarded-For": "198.51.100.7, 203.0.113.99"})
    assert spoofed.status_code == 410
    assert len(_audit("contract.signed_pdf_range_access")) == 0
    # 같은 요청자는 계속 받는다.
    assert client.get(url, headers={"Range": "bytes=10-", "X-Forwarded-For": "198.51.100.7"}).status_code == 206
    assert len(_audit("contract.signed_pdf_range_access")) == 1


def test_range_grace_is_capped_per_link():
    contract = _signed_contract()
    url = _issue(EMPLOYEE, contract["id"]).json()["url"].replace("/api/v1", "")
    client = _app()
    assert client.get(url, headers={"Range": "bytes=0-9"}).status_code == 206
    for _ in range(svc.SIGNED_PDF_LINK_RANGE_MAX_HITS):
        assert client.get(url, headers={"Range": "bytes=0-9"}).status_code == 206
    assert client.get(url, headers={"Range": "bytes=0-9"}).status_code == 410
    assert len(_audit("contract.signed_pdf_range_access")) == svc.SIGNED_PDF_LINK_RANGE_MAX_HITS


def test_range_grace_without_client_address_fails_closed(monkeypatch):
    contract = _signed_contract()
    url = _issue(EMPLOYEE, contract["id"]).json()["url"].replace("/api/v1", "")
    token = url.rsplit("/", 1)[-1]
    assert svc.redeem_signed_pdf_link(token, {}, True, "")[1]
    with pytest.raises(svc.HTTPException) as exc:
        svc.redeem_signed_pdf_link(token, {}, True, "")
    assert exc.value.status_code == 410


def test_range_request_losing_expiry_race_is_410_not_served(monkeypatch):
    contract = _signed_contract()
    url = _issue(EMPLOYEE, contract["id"]).json()["url"].replace("/api/v1", "")
    token = url.rsplit("/", 1)[-1]
    real_now = svc._now_dt
    real_peek = svc._peek_signed_pdf_link

    def stale_peek(token_hash):
        status, record = real_peek(token_hash)
        monkeypatch.setattr(svc, "_now_dt", lambda: real_now() + timedelta(seconds=svc.SIGNED_PDF_LINK_TTL_SECONDS + 1))
        return status, record

    monkeypatch.setattr(svc, "_peek_signed_pdf_link", stale_peek)
    with pytest.raises(svc.HTTPException) as exc:
        svc.redeem_signed_pdf_link(token, {}, True, "198.51.100.7")
    assert exc.value.status_code == 410
    assert _audit("contract.signed_pdf_access") == []
    assert _audit("contract.signed_pdf_range_access") == []


def test_range_request_on_unused_token_never_serves_after_expiry(monkeypatch):
    contract = _signed_contract()
    url = _issue(EMPLOYEE, contract["id"]).json()["url"].replace("/api/v1", "")
    real_now = svc._now_dt
    monkeypatch.setattr(svc, "_now_dt", lambda: real_now() + timedelta(seconds=svc.SIGNED_PDF_LINK_TTL_SECONDS + 1))
    assert _app().get(url, headers={"Range": "bytes=0-9"}).status_code == 410


def test_access_log_redacts_signed_pdf_token():
    import logging

    from app.core import access_log_redact

    token = "A" * 43
    record = logging.LogRecord(
        "uvicorn.access", logging.INFO, __file__, 1,
        '%s - "%s %s HTTP/%s" %d',
        ("203.0.113.9:1234", "GET", access_log_redact.SIGNED_PDF_LINK_PREFIX + token + "?x=1", "1.1", 200), None,
    )
    assert access_log_redact.SignedPdfLinkRedactFilter().filter(record) is True
    rendered = record.getMessage()
    assert token not in rendered and "[redacted]" in rendered
    assert "x=1" in rendered

    other = logging.LogRecord("uvicorn.access", logging.INFO, __file__, 1, "%s", ("/api/v1/other/" + token,), None)
    access_log_redact.SignedPdfLinkRedactFilter().filter(other)
    assert token in other.getMessage()


def test_structlog_event_with_token_path_is_redacted_in_log_output(tmp_path, monkeypatch):
    """main.global_exception_handler 와 같은 형태: structlog 필드 path=request.url.path 로 토큰이 실려 간다."""
    import logging

    import structlog

    from app import logging_config
    from app.core import access_log_redact

    token = "B" * 43
    log_file = tmp_path / "app.log"
    monkeypatch.setattr(logging_config, "_APP_LOG_FILE", str(log_file))
    root = logging.getLogger()
    saved_handlers, saved_level = list(root.handlers), root.level
    try:
        logging_config.configure_logging(log_level="INFO", json_format=True)
        structlog.get_logger("main").error(
            "Unhandled exception",
            path=access_log_redact.SIGNED_PDF_LINK_PREFIX + token,
            method="GET",
            error="boom " + access_log_redact.SIGNED_PDF_LINK_PREFIX + token + "?x=1",
            error_type="RuntimeError",
        )
        logging.getLogger("app.some.child").warning("stdlib %s", access_log_redact.SIGNED_PDF_LINK_PREFIX + token)
        for handler in root.handlers:
            handler.flush()
    finally:
        for handler in list(root.handlers):
            if handler not in saved_handlers:
                handler.close()
        root.handlers[:] = saved_handlers
        root.setLevel(saved_level)
        structlog.reset_defaults()
    written = log_file.read_text(encoding="utf-8")
    assert "Unhandled exception" in written and "stdlib" in written
    assert token not in written
    assert written.count("signed-pdf/t/[redacted]") >= 3


def test_redact_log_value_covers_nested_containers():
    from app.core import access_log_redact

    token = "C" * 43
    prefix = access_log_redact.SIGNED_PDF_LINK_PREFIX
    value = {"event": f"GET {prefix}{token}", "extra": [f"{prefix}{token}?a=1", ("ok", 3)], "n": 5}
    cleaned = access_log_redact.redact_log_value(value)
    assert token not in repr(cleaned)
    assert cleaned["n"] == 5 and cleaned["extra"][1] == ("ok", 3)
    assert access_log_redact.redact_log_value("no token here") == "no token here"


def test_access_log_filter_installed_on_uvicorn_access_logger():
    import logging

    from app.core import access_log_redact

    access_log_redact.install_access_log_redaction()
    access_log_redact.install_access_log_redaction()
    filters = [f for f in logging.getLogger("uvicorn.access").filters if isinstance(f, access_log_redact.SignedPdfLinkRedactFilter)]
    assert len(filters) == 1


def test_apache_template_does_not_log_token_path():
    conf = (ROOT / "config" / "apache" / "fb-cafe24.conf").read_text(encoding="utf-8")
    assert conf.count("env=!signed_pdf_link") == 2
    assert conf.count("signed-pdf/t/[redacted]") == 2
    for line in conf.splitlines():
        if "[redacted]" in line:
            assert "%r" not in line and "%U" not in line and "%q" not in line


# --- 교부 기록·기기 기록 --------------------------------------------------------


def test_delivery_recorded_once_per_party_download_not_at_issue():
    contract = _signed_contract()
    url = _issue(EMPLOYEE, contract["id"]).json()["url"].replace("/api/v1", "")
    assert _delivered_rows() == []

    client = _app()
    assert client.get(url).status_code == 200
    assert client.get(url).status_code == 410
    rows = _delivered_rows()
    assert len(rows) == 1 and rows[0]["channel"] == "download" and rows[0]["status"] == "sent"
    stored = svc._find(svc._read_hr("contracts", ADMIN), contract["id"])
    assert stored["delivered_at"] and stored["delivery_channel"] == "download"


def test_admin_link_does_not_record_delivery():
    contract = _signed_contract()
    url = _issue(ADMIN, contract["id"]).json()["url"].replace("/api/v1", "")
    assert _app().get(url).status_code == 200
    assert _delivered_rows() == []


def test_device_summary_logged_for_direct_issue_and_link_without_ip_or_raw_ua():
    contract = _signed_contract()
    direct = _app(EMPLOYEE).get(
        f"/yeoljeong-finance/contracts/{contract['id']}/signed-pdf", headers={"user-agent": KAKAO_ANDROID_UA}
    )
    assert direct.status_code == 200
    url = _issue(EMPLOYEE, contract["id"], ua=KAKAO_ANDROID_UA).json()["url"].replace("/api/v1", "")
    assert _app().get(url, headers={"user-agent": KAKAO_ANDROID_UA}).status_code == 200

    access = _audit("contract.signed_pdf_access")
    assert {row["details"]["channel"] for row in access} == {"direct", "link"}
    for row in access:
        client = row["details"]["client"]
        assert client["in_app"] is True and client["in_app_name"] == "kakaotalk" and client["os"] == "android"
    issued = _audit("contract.signed_pdf_link_issued")
    assert len(issued) == 1 and issued[0]["details"]["client"]["browser"] == "chrome"
    logged = json.dumps(access + issued, ensure_ascii=False)
    assert "KAKAOTALK 2511560" not in logged
    assert "testclient" not in logged and "203.0.113" not in logged


# --- 경로 규칙·라우팅 ----------------------------------------------------------


def test_tenant_gate_and_auth_middleware_paths():
    base = "/api/v1/yeoljeong-finance/contracts"
    assert obys_tenant._is_tenant_scoped_path(f"{base}/abcdefgh12/signed-pdf/link", "POST")
    assert not obys_tenant._is_tenant_scoped_path(f"{base}/abcdefgh12/signed-pdf/link", "GET")
    prefix = "/api/v1/yeoljeong-finance/contracts/signed-pdf/t/"
    for name in ("main.py", "yeoljeong_main.py"):
        source = (ROOT / "app" / name).read_text(encoding="utf-8")
        block = source.split("_AUTH_EXEMPT_PREFIXES = (", 1)[1].split("\n)\n", 1)[0]
        assert f'"{prefix}"' in block
        assert "obys_finance.signed_pdf_link_router" in source


def test_real_yeoljeong_app_passes_token_route_but_keeps_issue_route_authenticated():
    from app import yeoljeong_main

    client = TestClient(yeoljeong_main.app, raise_server_exceptions=False)
    forged = client.get("/api/v1/yeoljeong-finance/contracts/signed-pdf/t/" + "A" * 43)
    assert forged.status_code == 404  # 미들웨어 401 이 아니라 라우트의 위조 판정
    assert client.post("/api/v1/yeoljeong-finance/contracts/abcdefgh12/signed-pdf/link").status_code == 401
    assert client.get("/api/v1/yeoljeong-finance/contracts/abcdefgh12/signed-pdf").status_code == 401


def test_token_router_has_no_login_dependency_and_other_routes_keep_it():
    paths = {route.path: route for route in api.signed_pdf_link_router.routes}
    route = paths["/yeoljeong-finance/contracts/signed-pdf/t/{token}"]
    assert not route.dependencies and not api.signed_pdf_link_router.dependencies
    assert api.router.dependencies


def test_migration_is_new_table_only_and_held_from_auto_apply():
    sql = (ROOT / "migrations" / "20261104_obys_contract_signed_pdf_links.sql").read_text(encoding="utf-8")
    upper = sql.upper()
    assert "CREATE TABLE IF NOT EXISTS YEOLJEONG_CONTRACT_PDF_LINKS" in upper
    assert "TOKEN_HASH TEXT PRIMARY KEY" in upper
    assert "USED_BY TEXT" in upper and "RANGE_HITS INTEGER" in upper
    for forbidden in ("UPDATE ", "DELETE ", "DROP ", "TRUNCATE", "ALTER "):
        assert forbidden not in upper
    baseline = (ROOT / "scripts" / "migrations_auto_apply_baseline.txt").read_text(encoding="utf-8").splitlines()
    assert "migrations/20261104_obys_contract_signed_pdf_links.sql" in baseline
    assert (ROOT / "migrations" / "rollback" / "20261104_obys_contract_signed_pdf_links.down.sql").is_file()


# --- UA 요약 -------------------------------------------------------------------


@pytest.mark.parametrize(
    ("user_agent", "expected"),
    [
        (KAKAO_ANDROID_UA, {"in_app": True, "in_app_name": "kakaotalk", "os": "android", "browser": "chrome", "mobile": True}),
        (
            "Mozilla/5.0 (iPhone; CPU iPhone OS 17_5 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) "
            "Mobile/15E148 NAVER(inapp; search; 2000; 12.5.0; 15PRO)",
            {"in_app": True, "in_app_name": "naver", "os": "ios", "browser": "other", "mobile": True},
        ),
        (
            "Mozilla/5.0 (iPhone; CPU iPhone OS 17_5 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) "
            "Version/17.5 Mobile/15E148 Safari/604.1",
            {"in_app": False, "in_app_name": "", "os": "ios", "browser": "safari", "mobile": True},
        ),
        (
            "Mozilla/5.0 (Linux; Android 14; SM-S918N) AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/126.0.0.0 Mobile Safari/537.36",
            {"in_app": False, "in_app_name": "", "os": "android", "browser": "chrome", "mobile": True},
        ),
        (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/126.0.0.0 Safari/537.36",
            {"in_app": False, "in_app_name": "", "os": "windows", "browser": "chrome", "mobile": False},
        ),
        (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/126.0.0.0 Safari/537.36 Edg/126.0.0.0",
            {"in_app": False, "in_app_name": "", "os": "windows", "browser": "edge", "mobile": False},
        ),
        (None, {"in_app": False, "in_app_name": "", "os": "other", "browser": "unknown", "mobile": False}),
    ],
)
def test_summarize_client(user_agent, expected):
    assert summarize_client(user_agent) == expected


def test_summary_never_contains_raw_user_agent():
    summary = summarize_client(KAKAO_ANDROID_UA)
    assert set(summary) == {"in_app", "in_app_name", "os", "browser", "mobile"}
    assert "Mozilla" not in json.dumps(summary)
