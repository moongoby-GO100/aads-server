"""AADS-OBYS-H1-SIGN-INVARIANTS-20261022 — 서명 불변성·번들 원자성·PDF 이력.

- 파일 모드 테스트는 실제 서비스 함수를 그대로 부른다.
- DB 경로(조건부 UPDATE·트랜잭션)는 로컬 Postgres 가 없어 메모리 asyncpg 대역으로 돈다. 대역은 트랜잭션 롤백과
  행 단위 직렬화를 흉내 내며, 서비스가 보내는 조건(status='requested' AND sign_token_hash)을 SQL 의미 그대로 판정한다.
  실제 Postgres 의 SQL 문법 검증은 아니다.
"""
import base64
import copy
import hashlib
import json
import os
import threading
from datetime import datetime, timedelta
from io import BytesIO

import pytest
from fastapi import HTTPException

os.environ.setdefault("JWT_SECRET_KEY", "test-only-secret-key-that-is-at-least-32-bytes-long")

from app.api import obys_finance as api
from app.services import aligo_client, yeoljeong_contract_pdf, yeoljeong_ops_service

svc = api.svc
TENANT = "15055cac-71b0-45ec-b714-7093dde189ff"
MEMBERSHIP = {"tenant_id": TENANT, "status": "active"}
ADMIN = {"email": "owner@example.com", "is_admin": True, "tenant_id": TENANT, "current_membership": MEMBERSHIP}
EMPLOYEE = {"email": "member@example.com", "is_admin": False, "tenant_id": TENANT, "current_membership": MEMBERSHIP}


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

    clock = {"now": datetime(2026, 10, 20, 9, 0, tzinfo=svc.KST)}

    def fake_now():
        clock["now"] += timedelta(minutes=1)
        return clock["now"]

    monkeypatch.setattr(svc, "_now_dt", fake_now)

    async def fake_create_notification(**kwargs):
        return {"id": "ntf", **kwargs}

    monkeypatch.setattr(yeoljeong_ops_service, "create_notification", fake_create_notification)
    monkeypatch.setattr(aligo_client, "is_available", lambda: False)
    svc._write("employee_join_requests", [{
        "id": "join-mia", "name": "가입 직원", "email": EMPLOYEE["email"], "address": "서울시 직원 주소",
        "phone": "010-1234-5678", "birth_date": "1990-01-01", "tenant_id": TENANT, "business_id": "biz-mia",
        "branch": "열정국밥_미아점", "status": "approved",
    }])
    return clock


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


def _png(shift: int = 0) -> bytes:
    from PIL import Image, ImageDraw

    image = Image.new("RGB", (320, 120), "white")
    ImageDraw.Draw(image).line((12, 96 - shift, 300, 24 + shift), fill="black", width=5)
    buffer = BytesIO()
    image.save(buffer, "PNG")
    return buffer.getvalue()


def _sign_payload(token, *bundle_ids, shift=0):
    payload = {
        "token": token,
        "signer_name": "가입 직원",
        "consent": True,
        "consent_version": "yeoljeong-contract-sign-v1",
        "signature_data_uri": "data:image/png;base64," + base64.b64encode(_png(shift)).decode("ascii"),
    }
    if bundle_ids:
        payload["bundle_contract_ids"] = list(bundle_ids)
    return payload


def _requested(**overrides):
    saved = svc.save_contract(_contract_payload(**overrides), ADMIN)
    return svc.request_contract_signature(saved["id"], ADMIN)


def _stored(contract_id):
    return next(row for row in svc._read_file_rows("contracts") if row["id"] == contract_id)


def _put_stored(row):
    rows = svc._read_file_rows("contracts")
    for index, existing in enumerate(rows):
        if existing["id"] == row["id"]:
            rows[index] = row
    svc._write_file_rows("contracts", rows)


def _audit(action=None, contract_id=None):
    rows = svc._read_file_rows(svc.CONTRACT_AUDIT_LOG)
    return [
        row for row in rows
        if (action is None or row["action"] == action) and (contract_id is None or row["resource_id"] == contract_id)
    ]


def _signed_and_delivered():
    contract = _requested()
    result = svc.sign_contract_and_deliver(_sign_payload(contract["sign_token"]), EMPLOYEE)
    assert result["signed_pdf"]["status"] == "stored"
    return contract["id"]


# ---------------------------------------------------------------------------------------------
# 메모리 asyncpg 대역
# ---------------------------------------------------------------------------------------------
class FakePg:
    def __init__(self):
        self.rows: dict[str, dict] = {}
        self.audit: list[dict] = []
        self.lock = threading.RLock()
        self.audit_fail_on_call: int | None = None
        self.audit_calls = 0

    def load_contracts_from_file_mode(self):
        for row in svc._read_file_rows("contracts"):
            token = str(row.get("sign_token") or "")
            self.rows[row["id"]] = {
                "payload": copy.deepcopy(row),
                "status": row.get("status"),
                "sign_token_hash": hashlib.sha256(token.encode()).hexdigest() if token else row.get("sign_token_hash", ""),
                "tenant_id": row["tenant_id"],
                "deleted_at": None,
                "signed_at": row.get("signed_at"),
            }

    def contracts(self, tenant_id):
        return [copy.deepcopy(item["payload"]) for item in self.rows.values()
                if item["tenant_id"] == tenant_id and item["deleted_at"] is None]


class _Tx:
    def __init__(self, pg):
        self.pg = pg

    async def __aenter__(self):
        self.pg.lock.acquire()
        self.snapshot = (copy.deepcopy(self.pg.rows), len(self.pg.audit))
        return self

    async def __aexit__(self, exc_type, exc, tb):
        try:
            if exc_type is not None:
                self.pg.rows, audit_len = self.snapshot
                del self.pg.audit[audit_len:]
        finally:
            self.pg.lock.release()
        return False


class FakeConn:
    def __init__(self, pg):
        self.pg = pg

    async def close(self):
        return None

    def transaction(self):
        return _Tx(self.pg)

    async def fetchval(self, sql, *args):
        if "to_regclass" in sql:
            return True
        raise AssertionError(f"unexpected fetchval: {sql[:60]}")

    async def fetchrow(self, sql, *args):
        pg = self.pg
        if "UPDATE yeoljeong_contracts" in sql and "SET status = 'signed'" in sql:
            row_id, tenant, expected_hash, signed_at, _email, _name, token_hash, payload, _updated = args
            row = pg.rows.get(row_id)
            if (
                row is None or row["tenant_id"] != str(tenant) or row["status"] != "requested"
                or row["deleted_at"] is not None
                or not (row["sign_token_hash"] in ("", None) or row["sign_token_hash"] == expected_hash)
            ):
                return None
            row.update(status="signed", sign_token_hash=token_hash, payload=json.loads(payload),
                       signed_at=signed_at)
            return {"id": row_id}
        if "INSERT INTO yeoljeong_audit_logs" in sql:
            pg.audit_calls += 1
            if pg.audit_fail_on_call == pg.audit_calls:
                raise RuntimeError("audit insert failed")
            business_id, actor, action, resource_type, resource_id, details, _ip = args
            entry = {"business_id": business_id, "actor": actor, "action": action, "resource_type": resource_type,
                     "resource_id": resource_id, "details": json.loads(details)}
            pg.audit.append(entry)
            return entry
        raise AssertionError(f"unexpected fetchrow: {sql[:60]}")


@pytest.fixture
def fakepg():
    return FakePg()


def _enter_db_mode(monkeypatch, pg):
    """파일 모드로 만든 계약을 대역 DB 로 옮기고, 이후 서비스 호출을 DB 경로로 돌린다."""
    import asyncpg

    pg.load_contracts_from_file_mode()

    async def connect(*_args, **_kwargs):
        return FakeConn(pg)

    def read_hr(name, user):
        if name == "contracts":
            return pg.contracts(svc._tenant_id(user))
        tenant_id = svc._tenant_id(user)  # 계약 외 원장은 파일 모드 행을 그대로 읽는다
        return [row for row in svc._read_file_rows(name) if str(row.get("tenant_id") or "").strip() == tenant_id]

    monkeypatch.setattr(asyncpg, "connect", connect)
    monkeypatch.setattr(svc, "_db_url", lambda: "postgresql://fake/fake")
    monkeypatch.setattr(svc, "_db_available", lambda: True)
    monkeypatch.setattr(svc, "_read_hr", read_hr)
    return pg


def _sync_commit_barrier(monkeypatch, parties=2):
    """두 호출이 모두 'requested' 를 읽고 계산을 끝낸 뒤에야 저장 단계로 들어가게 한다."""
    barrier = threading.Barrier(parties, timeout=15)
    real_commit = svc._commit_signed_contracts

    def commit(items, user):
        barrier.wait()
        return real_commit(items, user)

    monkeypatch.setattr(svc, "_commit_signed_contracts", commit)


def _race(payloads):
    outcomes: list = [None] * len(payloads)

    def run(index, payload):
        try:
            outcomes[index] = ("ok", svc._sign_contract_locked(payload, EMPLOYEE))
        except HTTPException as exc:
            outcomes[index] = ("http", exc.status_code)

    threads = [threading.Thread(target=run, args=(i, p)) for i, p in enumerate(payloads)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)
    return outcomes


# --- F-2: 이중 서명은 프로세스 Lock 이 아니라 저장 조건이 막는다 ----------------------------------
def test_concurrent_double_sign_file_mode_one_succeeds_other_409_without_overwrite(monkeypatch):
    contract = _requested()
    _sync_commit_barrier(monkeypatch)
    # sign_contract 의 Lock 을 우회해 _sign_contract_locked 를 직접 동시에 부른다.
    outcomes = _race([_sign_payload(contract["sign_token"], shift=0), _sign_payload(contract["sign_token"], shift=15)])
    assert sorted(kind for kind, _ in outcomes) == ["http", "ok"], outcomes
    winner = next(value for kind, value in outcomes if kind == "ok")
    assert next(value for kind, value in outcomes if kind == "http") == 409
    stored = _stored(contract["id"])
    assert stored["status"] == "signed"
    assert stored["signature_sha256"] == winner["signature_sha256"]
    assert stored["signed_at"] == winner["signed_at"]
    assert len(_audit("contract.signed", contract["id"])) == 1


def test_concurrent_double_sign_db_mode_conditional_update_rejects_loser(monkeypatch, fakepg):
    contract = _requested()
    pg = _enter_db_mode(monkeypatch, fakepg)
    _sync_commit_barrier(monkeypatch)
    outcomes = _race([_sign_payload(contract["sign_token"], shift=0), _sign_payload(contract["sign_token"], shift=15)])
    assert sorted(kind for kind, _ in outcomes) == ["http", "ok"], outcomes
    winner = next(value for kind, value in outcomes if kind == "ok")
    assert next(value for kind, value in outcomes if kind == "http") == 409
    row = pg.rows[contract["id"]]
    assert row["status"] == "signed"
    assert row["payload"]["signature_sha256"] == winner["signature_sha256"]
    assert row["payload"]["signed_at"] == winner["signed_at"] == row["signed_at"].isoformat(timespec="seconds")
    signed_audits = [item for item in pg.audit if item["action"] == "contract.signed"]
    assert len(signed_audits) == 1
    assert signed_audits[0]["details"]["from"] == "requested" and signed_audits[0]["details"]["to"] == "signed"
    assert "signature_data_uri" not in json.dumps(signed_audits[0]["details"])


def test_stale_commit_after_signing_is_409_and_keeps_original_signature(monkeypatch, fakepg):
    """읽은 뒤 다른 요청이 먼저 서명을 끝낸 경우 — 상태 검사를 지난 호출도 저장 조건에서 막힌다."""
    contract = _requested()
    pg = _enter_db_mode(monkeypatch, fakepg)
    stale_payload = _sign_payload(contract["sign_token"], shift=15)
    real_commit = svc._commit_signed_contracts
    state = {"first": True}

    def commit(items, user):
        if state["first"]:
            state["first"] = False
            monkeypatch.setattr(svc, "_commit_signed_contracts", real_commit)
            svc._sign_contract_locked(_sign_payload(contract["sign_token"], shift=0), EMPLOYEE)
        return real_commit(items, user)

    monkeypatch.setattr(svc, "_commit_signed_contracts", commit)
    with pytest.raises(HTTPException) as exc:
        svc._sign_contract_locked(stale_payload, EMPLOYEE)
    assert exc.value.status_code == 409
    first_sha = hashlib.sha256(_png(0)).hexdigest()
    assert pg.rows[contract["id"]]["payload"]["signature_sha256"] == first_sha


def test_second_sign_attempt_after_completion_is_409_and_changes_nothing():
    contract = _requested()
    signed = svc.sign_contract(_sign_payload(contract["sign_token"], shift=0), EMPLOYEE)
    with pytest.raises(HTTPException) as exc:
        svc.sign_contract(_sign_payload(contract["sign_token"], shift=15), EMPLOYEE)
    assert exc.value.status_code == 409
    stored = _stored(contract["id"])
    assert stored["signature_sha256"] == signed["signature_sha256"]
    assert stored["signed_at"] == signed["signed_at"]
    assert len(_audit("contract.signed", contract["id"])) == 1


# --- F-2: 번들은 중간 실패에도 서명 전 상태를 유지한다 ---------------------------------------------
def test_bundle_midway_failure_in_db_transaction_keeps_every_contract_unsigned(monkeypatch, fakepg):
    later = _requested(start_date="2026-08-01", contract_date="2026-08-01")
    first = _requested(start_date="2026-06-25", end_date="2026-07-31", contract_date="2026-06-25")
    pg = _enter_db_mode(monkeypatch, fakepg)
    pg.audit_fail_on_call = 2  # 두 번째 계약의 감사 행 INSERT 에서 실패
    with pytest.raises(HTTPException) as exc:
        svc.sign_contract_bundle(_sign_payload(later["sign_token"], first["id"]), EMPLOYEE)
    assert exc.value.status_code == 409
    assert pg.audit_calls == 2
    assert {pg.rows[first["id"]]["status"], pg.rows[later["id"]]["status"]} == {"requested"}
    assert pg.rows[first["id"]]["payload"].get("signed_at") is None
    assert pg.audit == []
    pg.audit_fail_on_call = None
    done = svc.sign_contract_bundle(_sign_payload(later["sign_token"], first["id"]), EMPLOYEE)
    assert {item["id"] for item in done["contracts"]} == {first["id"], later["id"]}
    assert {pg.rows[first["id"]]["status"], pg.rows[later["id"]]["status"]} == {"signed"}
    assert len([item for item in pg.audit if item["action"] == "contract.signed"]) == 2


def test_bundle_with_one_already_signed_member_signs_nothing(monkeypatch, fakepg):
    later = _requested(start_date="2026-08-01", contract_date="2026-08-01")
    first = _requested(start_date="2026-06-25", end_date="2026-07-31", contract_date="2026-06-25")
    real_bundle_member = svc._bundle_member_for_signing
    pg = _enter_db_mode(monkeypatch, fakepg)

    def member_signed_meanwhile(member, primary, payload, user):
        real_bundle_member(member, primary, payload, user)
        pg.rows[member["id"]]["status"] = "signed"  # 검사 뒤·저장 전에 다른 요청이 서명 완료

    monkeypatch.setattr(svc, "_bundle_member_for_signing", member_signed_meanwhile)
    with pytest.raises(HTTPException) as exc:
        svc.sign_contract_bundle(_sign_payload(later["sign_token"], first["id"]), EMPLOYEE)
    assert exc.value.status_code == 409
    assert pg.rows[later["id"]]["status"] == "requested"
    assert pg.audit == []


# --- F-3: 서명 후 변조는 다운로드·재생성에서 오류 + 감사 행 -----------------------------------------
def test_tampered_payload_blocks_download_and_regeneration_with_audit_rows():
    contract_id = _signed_and_delivered()
    svc.signed_contract_pdf_for_download(contract_id, ADMIN)  # 변조 전에는 받아진다
    row = _stored(contract_id)
    row["wage"] = 99999999
    _put_stored(row)

    with pytest.raises(HTTPException) as download_exc:
        svc.signed_contract_pdf_for_download(contract_id, EMPLOYEE)
    assert download_exc.value.status_code == 409
    with pytest.raises(HTTPException) as regen_exc:
        svc.regenerate_signed_contract_pdf(contract_id, ADMIN)
    assert regen_exc.value.status_code == 409

    violations = _audit("contract.integrity_violation", contract_id)
    assert [item["details"]["attempted"] for item in violations] == ["regenerate", "download"]
    for item in violations:
        assert item["details"]["problems"] == ["payload_diverged_from_snapshot"]
        assert item["details"]["changed_fields"] == ["wage"]
        assert "99999999" not in json.dumps(item["details"])
        assert item["details"]["expected_sha256"] != item["details"]["actual_sha256"]
    # 변조 거절이 PDF 파일·이력을 건드리지 않았다
    assert "signed_pdf_history" not in _stored(contract_id)


def test_tampered_snapshot_itself_is_detected():
    contract_id = _signed_and_delivered()
    row = _stored(contract_id)
    row["signed_snapshot"]["wage"] = 1
    _put_stored(row)
    with pytest.raises(HTTPException) as exc:
        svc.signed_contract_pdf_for_download(contract_id, ADMIN)
    assert exc.value.status_code == 409
    problems = _audit("contract.integrity_violation", contract_id)[0]["details"]["problems"]
    assert "snapshot_hash_mismatch" in problems


def test_swapped_signature_image_is_detected_even_though_it_is_outside_the_snapshot_hash():
    contract_id = _signed_and_delivered()
    row = _stored(contract_id)
    row["signature_data_uri"] = "data:image/png;base64," + base64.b64encode(_png(30)).decode("ascii")
    _put_stored(row)
    with pytest.raises(HTTPException) as exc:
        svc.signed_contract_pdf_for_download(contract_id, ADMIN)
    assert exc.value.status_code == 409
    details = _audit("contract.integrity_violation", contract_id)[0]["details"]
    assert details["problems"] == ["signature_image_mismatch"]
    assert details["changed_fields"] == ["signature_data_uri"]


def test_contract_regenerated_twice_still_downloads(monkeypatch):
    """T-H: PDF 재생성 이력 필드는 해시 제외 목록에 있어 정상 재생성이 무결성 검사를 깨지 않는다."""
    contract_id = _signed_and_delivered()
    svc.regenerate_signed_contract_pdf(contract_id, ADMIN)
    svc.regenerate_signed_contract_pdf(contract_id, ADMIN)
    stored = _stored(contract_id)
    assert len(stored["signed_pdf_history"]) == 2
    assert svc._contract_integrity_problems(stored) == ([], [], stored["signed_snapshot_sha256"])
    path, filename = svc.signed_contract_pdf_for_download(contract_id, EMPLOYEE)
    assert path.read_bytes().startswith(b"%PDF") and filename.endswith(".pdf")
    assert _audit("contract.integrity_violation", contract_id) == []


# --- F-4: 이전 PDF sha·경로는 append-only 이력에 남는다 ---------------------------------------------
def test_previous_pdf_sha_and_path_survive_regeneration_in_append_only_history(monkeypatch):
    contract_id = _signed_and_delivered()
    first = _stored(contract_id)
    first_sha, first_path = first["signed_pdf_sha256"], first["signed_pdf_path"]
    original_bytes = (svc._contract_pdf_root() / first_path).read_bytes()

    renders = {"n": 0}

    def render(contract, seal=None):
        renders["n"] += 1
        return b"%PDF-1.4 regenerated build " + str(renders["n"]).encode()

    monkeypatch.setattr(yeoljeong_contract_pdf, "render_signed_contract_pdf", render)
    svc.regenerate_signed_contract_pdf(contract_id, ADMIN)
    after_first = _stored(contract_id)
    assert after_first["signed_pdf_sha256"] != first_sha
    history = after_first["signed_pdf_history"]
    assert len(history) == 1
    entry = history[0]
    assert entry["sha256"] == first_sha and entry["archived"] is True and entry["reason"] == "admin_regenerate"
    assert entry["replaced_by_sha256"] == after_first["signed_pdf_sha256"]
    archived = svc._contract_pdf_root() / entry["path"]
    assert archived.read_bytes() == original_bytes
    assert hashlib.sha256(archived.read_bytes()).hexdigest() == first_sha

    svc.regenerate_signed_contract_pdf(contract_id, ADMIN)
    after_second = _stored(contract_id)
    assert after_second["signed_pdf_history"][0] == entry  # 앞 항목은 그대로(append-only)
    assert [item["sha256"] for item in after_second["signed_pdf_history"]] == [first_sha, after_first["signed_pdf_sha256"]]

    audits = _audit("contract.pdf_regenerated", contract_id)
    assert [(item["details"]["from_sha256"], item["details"]["to_sha256"]) for item in reversed(audits)] == [
        (first_sha, after_first["signed_pdf_sha256"]),
        (after_first["signed_pdf_sha256"], after_second["signed_pdf_sha256"]),
    ]


def test_first_pdf_creation_adds_no_history_and_no_regeneration_audit():
    contract_id = _signed_and_delivered()
    stored = _stored(contract_id)
    assert "signed_pdf_history" not in stored
    assert _audit("contract.pdf_regenerated", contract_id) == []


def test_pdf_meta_persist_cannot_change_signed_originals():
    """저장 경로는 가변 메타만 반영한다 — 서명 원본 필드를 바꾼 사본을 넘겨도 저장본은 그대로다."""
    contract_id = _signed_and_delivered()
    stored = _stored(contract_id)
    forged = copy.deepcopy(stored)
    forged.update(wage=1, signature_sha256="f" * 64, signed_at="2000-01-01T00:00:00+09:00", signature_data_uri="data:x")
    forged["signed_pdf_error"] = "meta may change"
    svc._persist_contract_delivery(forged, EMPLOYEE)
    after = _stored(contract_id)
    for key in ("wage", "signature_sha256", "signed_at", "signature_data_uri", "signed_snapshot_sha256"):
        assert after[key] == stored[key]
    assert after["signed_pdf_error"] == "meta may change"
    assert svc.CONTRACT_POST_SIGN_MUTABLE_FIELDS < svc.CONTRACT_SNAPSHOT_EXCLUDED_FIELDS


def test_delivered_at_is_set_once():
    contract_id = _signed_and_delivered()
    svc.signed_contract_pdf_for_download(contract_id, EMPLOYEE)
    first = _stored(contract_id)["delivered_at"]
    assert first
    svc.signed_contract_pdf_for_download(contract_id, EMPLOYEE)
    assert _stored(contract_id)["delivered_at"] == first


# --- F-5: 상태 전이마다 감사 행 1건 -----------------------------------------------------------------
def test_each_state_transition_writes_exactly_one_audit_row(isolated):
    contract = _requested()
    assert [row["details"]["to"] for row in _audit("contract.requested", contract["id"])] == ["requested"]

    isolated["now"] += timedelta(days=svc.CONTRACT_SIGN_LINK_TTL_DAYS + 1)
    svc.request_contract_signature_renewal({"token": contract["sign_token"]}, EMPLOYEE)
    assert len(_audit("contract.signature_renewal_requested", contract["id"])) == 1

    renewed = svc.request_contract_signature(contract["id"], ADMIN)
    svc.sign_contract(_sign_payload(renewed["sign_token"]), EMPLOYEE)
    assert len(_audit("contract.signed", contract["id"])) == 1

    other = _requested(start_date="2026-09-01", contract_date="2026-09-01")
    svc.delete_contract(other["id"], ADMIN)
    assert len(_audit("contract.deleted", other["id"])) == 1
    assert len(_audit("contract.signed", other["id"])) == 0
    assert all(row["details"]["tenant_id"] == TENANT for row in _audit())


# --- F-12: 시간 의존 경로는 같은 시계 하나로 얼린다 -------------------------------------------------
def test_sign_link_expiry_follows_the_single_service_clock(isolated):
    contract = _requested()
    isolated["now"] += timedelta(days=svc.CONTRACT_SIGN_LINK_TTL_DAYS + 1)
    with pytest.raises(HTTPException) as exc:
        svc.sign_contract(_sign_payload(contract["sign_token"]), EMPLOYEE)
    assert exc.value.status_code == 410
    assert _stored(contract["id"])["status"] == "requested"


# --- 회귀: 정상 서명 → 알림·PDF·다운로드 ------------------------------------------------------------
def test_normal_sign_still_notifies_stores_pdf_and_downloads():
    contract = _requested()
    result = svc.sign_contract_and_deliver(_sign_payload(contract["sign_token"]), EMPLOYEE)
    assert result["contract"]["status"] == "signed"
    assert result["signed_pdf"]["status"] == "stored"
    assert result["notify"]
    stored = _stored(contract["id"])
    assert stored["status"] == "signed" and "sign_token" not in stored
    assert stored["signed_snapshot_sha256"] and svc._contract_integrity_problems(stored)[0] == []
    path, _ = svc.signed_contract_pdf_for_download(contract["id"], EMPLOYEE)
    assert path.read_bytes().startswith(b"%PDF")
    assert _stored(contract["id"])["delivered_at"]
