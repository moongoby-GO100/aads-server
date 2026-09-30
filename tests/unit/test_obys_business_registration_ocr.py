"""AADS-OBYS-BIZLICENSE-ORIGINAL-OCR-20260930

사업자등록증 원본 보관 · OCR 제안(자동 저장 금지) · canonical 상호 덮어쓰기 차단.

SQL 은 가짜 연결에서 토큰 단위로만 판별한다(dup_guard 가 완전한 SQL 리터럴을
실제 SQL 로 오인한 사례가 있다).
"""
from __future__ import annotations

import asyncio
import copy
import hashlib
import io
import json
from contextlib import asynccontextmanager
from datetime import date, datetime, timezone
from pathlib import Path
from uuid import UUID

import pytest
from fastapi import HTTPException, UploadFile

from app.api import obys_finance as api
from app.services import obys_registration_ocr as ocr
from app.services import obys_upload_service as service
from app.services import yeoljeong_finance_service as finance

TENANT_A = UUID("15055cac-71b0-45ec-b714-7093dde189ff")
TENANT_B = UUID("2d701a8c-9596-4757-8588-faa4f7837112")
BUSINESS = "biz-eonni-naengmyeon"
PNG = b"\x89PNG\r\n\x1a\n" + b"registration-image" * 8
PDF = b"%PDF-1.4\nregistration-pdf\n"
VALID_NO = "123-45-67891"  # 국세청 검증번호 일치
BAD_NO = "123-45-67890"  # 검증번호 불일치
OCR_TEXT = "\n".join([
    "사 업 자 등 록 증",
    "( 일반과세자 )",
    f"등록번호 : {VALID_NO}",
    "상 호 : 언니냉면 테스트",
    "성 명 : 김테스트        생 년 월 일 : 1980 년 01 월 01 일",
    "개 업 연 월 일 : 2025 년 4 월 1 일",
    "사업장 소재지 : 서울특별시  성북구   테스트로 1",
    "사업의 종류 : 업태 음식점업 종목 한식",
])


def _user(tenant: UUID, role: str = "owner") -> dict:
    return {
        "email": f"{role}@example.com",
        "user_id": f"user-{role}",
        "tenant_id": str(tenant),
        "current_membership": {"tenant_id": str(tenant), "status": "active", "role": role},
    }


class FakeDB:
    def __init__(self) -> None:
        self.businesses = {
            BUSINESS: {
                "id": BUSINESS, "tenant_id": TENANT_A, "name": "언니냉면",
                "registration_no": "기초등록 필요", "representative": "미등록",
                "tax_type": "일반과세", "opened_at": "", "address": "",
            },
        }
        self.documents: list[dict] = []
        self.audits: list[dict] = []


class FakeConn:
    def __init__(self, db: FakeDB) -> None:
        self.db = db

    @asynccontextmanager
    async def transaction(self):
        yield

    async def close(self) -> None:
        return None

    def _business(self, business_id, tenant_id):
        row = self.db.businesses.get(business_id)
        return row if row and row["tenant_id"] == tenant_id else None

    @staticmethod
    def _project(doc, sql):
        # RETURNING/SELECT 가 고른 열만 돌려준다(asyncpg Record 와 같게).
        columns = service.REGISTRATION_DOCUMENT_COLUMNS.split(",")
        if ",stored_filename FROM" in sql:
            columns.append("stored_filename")
        return {column: doc[column] for column in columns}

    def _active(self, tenant_id, business_id):
        rows = [d for d in self.db.documents if d["tenant_id"] == tenant_id and d["business_id"] == business_id and d["deleted_at"] is None]
        return sorted(rows, key=lambda d: d["version"], reverse=True)

    async def fetchval(self, sql, *args):
        if "EXISTS" in sql:
            return self._business(args[0], args[1]) is not None
        if "MAX(version)" in sql:
            versions = [d["version"] for d in self.db.documents if d["tenant_id"] == args[0] and d["business_id"] == args[1]]
            return max(versions, default=0) + 1
        raise AssertionError(sql)

    async def execute(self, sql, *args):
        if "pg_advisory_xact_lock" in sql:
            return "SELECT 1"
        if "deleted_at=NOW()" in sql:
            hits = [d for d in self._active(args[1], args[2]) if d["id"] == args[0]]
            for doc in hits:
                doc["deleted_at"], doc["deleted_by"] = datetime.now(timezone.utc), args[3]
            return f"UPDATE {len(hits)}"
        raise AssertionError(sql)

    async def fetch(self, sql, *args):
        assert "yeoljeong_business_registration_documents" in sql
        return [self._project(d, sql) for d in self._active(args[0], args[1])]

    async def fetchrow(self, sql, *args):
        if "yeoljeong_audit_logs" in sql:
            entry = dict(zip(("business_id", "actor", "action", "resource_type", "resource_id", "details", "ip_address"), args))
            self.db.audits.append(entry)
            return entry
        if "INSERT" in sql and "yeoljeong_business_registration_documents" in sql:
            keys = ("id", "tenant_id", "business_id", "version", "original_filename", "stored_filename",
                    "content_type", "extension", "byte_size", "sha256", "created_by")
            doc = dict(zip(keys, args))
            doc.update(created_at=datetime.now(timezone.utc), deleted_at=None, deleted_by=None)
            self.db.documents.append(doc)
            return self._project(doc, sql)
        if "yeoljeong_business_registration_documents" in sql and "LIMIT 1" in sql:
            active = self._active(args[0], args[1])
            return self._project(active[0], sql) if active else None
        if "yeoljeong_business_registration_documents" in sql:
            hits = [d for d in self._active(args[1], args[2]) if d["id"] == args[0]]
            return self._project(hits[0], sql) if hits else None
        if "yeoljeong_businesses" in sql:
            row = self._business(args[0], args[1])
            return dict(row) if row else None
        raise AssertionError(sql)


@pytest.fixture
def db(tmp_path, monkeypatch):
    fake = FakeDB()

    async def connect():
        return FakeConn(fake)

    monkeypatch.setattr(service, "UPLOAD_ROOT", tmp_path / "uploads")
    monkeypatch.setattr(service, "_connect", connect)
    return fake


def _upload(user, data=PNG, filename="등록증.png", content_type="image/png", business_id=BUSINESS):
    return asyncio.run(service.create_registration_document(
        user=user, business_id=business_id, filename=filename, content_type=content_type, data=data,
    ))


# --- A. 원본 보관 --------------------------------------------------------------
def test_upload_stores_original_under_tenant_business_hash_with_sha256(db, tmp_path):
    document = _upload(_user(TENANT_A))

    business_hash = hashlib.sha256(BUSINESS.encode()).hexdigest()[:32]
    directory = tmp_path / "uploads" / str(TENANT_A) / business_hash / "business_registration"
    stored = list(directory.iterdir())
    assert len(stored) == 1 and stored[0].read_bytes() == PNG
    assert stored[0].name == f"{UUID(document['id']).hex}.png"
    assert document["sha256"] == hashlib.sha256(PNG).hexdigest()
    assert document["original_filename"] == "등록증.png"
    assert document["byte_size"] == len(PNG) and document["extension"] == ".png"
    assert document["uploaded_by"] == "owner@example.com" and document["uploaded_at"]
    assert document["version"] == 1 and document["is_current"] is True and document["status"] == "stored"
    assert "stored_filename" not in document
    assert db.audits[-1]["action"] == "business_registration.upload"


def test_registration_category_stays_out_of_ledger_paths():
    assert service.REGISTRATION_CATEGORY not in service.LEDGER_CATEGORIES
    with pytest.raises(HTTPException) as exc:
        asyncio.run(service.create_upload(
            user=_user(TENANT_A), business_id=BUSINESS, category="business_registration",
            filename="a.pdf", content_type="application/pdf", data=PDF,
        ))
    assert exc.value.status_code == 400


def test_second_upload_keeps_previous_version_and_latest_is_current(db):
    admin = _user(TENANT_A)
    first = _upload(admin)
    second = _upload(admin, data=PDF, filename="재발급.pdf", content_type="application/pdf")
    again = _upload(admin, data=PDF, filename="재발급.pdf", content_type="application/pdf")

    listed = asyncio.run(service.list_registration_documents(user=admin, business_id=BUSINESS))
    assert [d["version"] for d in listed["documents"]] == [2, 1]
    assert listed["current"]["id"] == second["id"] and listed["current"]["is_current"] is True
    assert listed["documents"][1]["id"] == first["id"] and listed["documents"][1]["is_current"] is False
    assert again["status"] == "duplicate" and again["id"] == second["id"]
    assert len(db.documents) == 2


def test_soft_delete_keeps_file_and_row(db, tmp_path):
    admin = _user(TENANT_A)
    first = _upload(admin)
    second = _upload(admin, data=PDF, filename="b.pdf", content_type="application/pdf")
    assert asyncio.run(service.delete_registration_document(user=admin, business_id=BUSINESS, document_id=UUID(second["id"])))

    assert len(db.documents) == 2 and db.documents[1]["deleted_at"] is not None
    assert len(list((tmp_path / "uploads").rglob("*.pdf"))) == 1
    listed = asyncio.run(service.list_registration_documents(user=admin, business_id=BUSINESS))
    assert listed["current"]["id"] == first["id"]
    third = _upload(admin, data=PDF + b"v3", filename="c.pdf", content_type="application/pdf")
    assert third["version"] == 3  # 삭제된 버전 번호는 재사용하지 않는다


@pytest.mark.parametrize(
    ("filename", "content_type", "data"),
    [
        ("virus.exe", "application/octet-stream", b"MZ" + b"0" * 10),
        ("ledger.csv", "text/csv", b"date,amount\n2026-09-30,1"),
        ("fake.png", "image/png", b"not-a-png"),
        ("big.pdf", "application/pdf", b"%PDF-" + b"0" * service.MAX_BYTES),
    ],
)
def test_disallowed_file_or_oversize_is_400(db, filename, content_type, data):
    with pytest.raises(HTTPException) as exc:
        _upload(_user(TENANT_A), data=data, filename=filename, content_type=content_type)
    assert exc.value.status_code == 400
    assert db.documents == []


def test_router_oversize_stream_is_400():
    upload = UploadFile(file=io.BytesIO(b"0" * (service.MAX_BYTES + 1)), filename="big.pdf")
    with pytest.raises(HTTPException) as exc:
        asyncio.run(api._read_registration_upload(upload))
    assert exc.value.status_code == 400


def test_non_admin_is_403_for_every_registration_operation(db):
    document = _upload(_user(TENANT_A))
    member = _user(TENANT_A, role="member")
    document_id = UUID(document["id"])
    calls = [
        lambda: service.create_registration_document(user=member, business_id=BUSINESS, filename="a.png", content_type="image/png", data=PNG),
        lambda: service.list_registration_documents(user=member, business_id=BUSINESS),
        lambda: service.registration_document_download(user=member, business_id=BUSINESS, document_id=document_id),
        lambda: ocr.suggest_from_document(user=member, business_id=BUSINESS, document_id=document_id),
        lambda: service.delete_registration_document(user=member, business_id=BUSINESS, document_id=document_id),
    ]
    for call in calls:
        with pytest.raises(HTTPException) as exc:
            asyncio.run(call())
        assert exc.value.status_code == 403
    with pytest.raises(HTTPException) as router_exc:
        asyncio.run(api.upload_business_registration_document(BUSINESS, UploadFile(file=io.BytesIO(PNG), filename="a.png"), member))
    assert router_exc.value.status_code == 403


def test_other_tenant_admin_gets_404(db):
    document = _upload(_user(TENANT_A))
    other = _user(TENANT_B)
    document_id = UUID(document["id"])
    calls = [
        lambda: service.create_registration_document(user=other, business_id=BUSINESS, filename="a.png", content_type="image/png", data=PNG),
        lambda: service.list_registration_documents(user=other, business_id=BUSINESS),
        lambda: service.registration_document_download(user=other, business_id=BUSINESS, document_id=document_id),
        lambda: ocr.suggest_from_document(user=other, business_id=BUSINESS, document_id=document_id),
        lambda: service.delete_registration_document(user=other, business_id=BUSINESS, document_id=document_id),
    ]
    for call in calls:
        with pytest.raises(HTTPException) as exc:
            asyncio.run(call())
        assert exc.value.status_code == 404
    assert len(db.documents) == 1


def test_download_returns_sanitized_name_and_original_bytes(db):
    admin = _user(TENANT_A)
    document = _upload(admin, filename="등록증 (원본);x.png")
    path, filename, content_type = asyncio.run(service.registration_document_download(
        user=admin, business_id=BUSINESS, document_id=UUID(document["id"]),
    ))
    assert path.read_bytes() == PNG and content_type == "image/png"
    assert filename.startswith("사업자등록증_v1_") and filename.endswith(".png")
    assert not set(filename) & set('/\\";\r\n')


# --- B. OCR 제안 ---------------------------------------------------------------
def test_checksum_rule():
    assert ocr.business_registration_checksum_ok("1234567891")
    assert not ocr.business_registration_checksum_ok("1234567890")
    assert ocr.normalize_registration_no("1234567891") == (VALID_NO, True, None)
    value, valid, reason = ocr.normalize_registration_no(BAD_NO)
    assert value == BAD_NO and valid is False and "체크섬" in reason


def test_parse_extracts_five_fields_and_extras():
    result = ocr.parse_registration_text(OCR_TEXT, 92, today=date(2026, 9, 30))
    assert result["name"]["value"] == "언니냉면 테스트"
    assert result["registration_no"]["value"] == VALID_NO and result["registration_no"]["valid"] is True
    assert result["representative"]["value"] == "김테스트"
    assert result["opened_at"]["value"] == "2025-04-01"
    assert result["address"]["value"] == "서울특별시 성북구 테스트로 1"
    for field in ocr.AUTOFILL_FIELDS:
        assert result[field]["autofill_candidate"] is True
        assert 0 < result[field]["confidence"] <= 1 and result[field]["source"]
    assert result["tax_type"]["value"] == "일반과세" and result["tax_type"]["autofill_candidate"] is False


def test_bad_checksum_is_invalid_and_not_autofill():
    result = ocr.parse_registration_text(OCR_TEXT.replace(VALID_NO, "1234567890"), 0.9)
    item = result["registration_no"]
    assert item["value"] == BAD_NO
    assert item["valid"] is False and item["autofill_candidate"] is False and item["reason"]


def test_opened_at_korean_date_and_future_date():
    assert ocr.normalize_opened_at("2025년 4월 1일", today=date(2026, 9, 30)) == ("2025-04-01", True, None)
    assert ocr.normalize_opened_at("2025. 04. 01", today=date(2026, 9, 30))[0] == "2025-04-01"
    value, valid, reason = ocr.normalize_opened_at("2099 년 01 월 01 일", today=date(2026, 9, 30))
    assert value == "2099-01-01" and valid is False and "미래" in reason
    assert ocr.normalize_opened_at("2025년 2월 30일", today=date(2026, 9, 30))[0] is None
    future = ocr.parse_registration_text(OCR_TEXT.replace("2025 년 4 월 1 일", "2099 년 1 월 1 일"), 0.9, today=date(2026, 9, 30))
    assert future["opened_at"]["valid"] is False and future["opened_at"]["autofill_candidate"] is False


def test_missing_fields_are_null_with_reason_not_guessed():
    result = ocr.parse_registration_text("사 업 자 등 록 증\n상 호 : 언니냉면", 0.9)
    assert result["name"]["value"] == "언니냉면"
    for field in ("registration_no", "representative", "opened_at", "address", "corporate_registration_no", "tax_type"):
        assert result[field]["value"] is None, field
        assert result[field]["reason"], field
        assert result[field]["autofill_candidate"] is False
    empty = ocr.parse_registration_text("", 0.9)
    assert all(item["value"] is None and item["reason"] for item in empty.values())


def test_ocr_suggestion_never_changes_business_record(db, monkeypatch):
    admin = _user(TENANT_A)
    document = _upload(admin)
    before = copy.deepcopy(db.businesses)
    seen = {}

    async def fake_ocr(image_url=None, image_base64=None, language="kor+eng"):
        seen["base64"] = image_base64
        return {"text": OCR_TEXT, "confidence": 0.93, "language": language, "error": None}

    monkeypatch.setattr(ocr.local_ocr_bridge, "ocr_extract", fake_ocr)
    result = asyncio.run(ocr.suggest_from_document(user=admin, business_id=BUSINESS, document_id=UUID(document["id"])))

    assert db.businesses == before
    assert seen["base64"]  # 기존 브리지로 원본을 넘겼다
    assert result["applied"] is False and result["ocr"]["ok"] is True
    assert result["suggested"]["registration_no"]["value"] == VALID_NO
    assert result["current"]["registration_no"] == ""  # 자리표시자는 현재값으로 내보내지 않는다
    assert result["needs_registration_info"] is True


def test_audit_log_has_field_results_without_ocr_text(db, monkeypatch):
    admin = _user(TENANT_A)
    document = _upload(admin)

    async def fake_ocr(image_url=None, image_base64=None, language="kor+eng"):
        return {"text": OCR_TEXT, "confidence": 0.93, "language": language, "error": None}

    monkeypatch.setattr(ocr.local_ocr_bridge, "ocr_extract", fake_ocr)
    asyncio.run(ocr.suggest_from_document(user=admin, business_id=BUSINESS, document_id=UUID(document["id"])))

    entry = db.audits[-1]
    assert entry["action"] == "business_registration.ocr_suggest"
    raw = entry["details"]
    details = json.loads(raw)
    assert details["document_id"] == document["id"] and details["ocr_ok"] is True
    assert details["text_length"] == len(OCR_TEXT) and details["elapsed_ms"] >= 0
    assert details["fields"]["registration_no"] == {"found": True, "valid": True, "value_length": len(VALID_NO)}
    for fragment in ("김테스트", "언니냉면 테스트", VALID_NO, "성북구", "사업의 종류"):
        assert fragment not in raw


def test_ocr_failure_returns_nulls_and_is_audited(db, monkeypatch):
    admin = _user(TENANT_A)
    document = _upload(admin)

    async def broken(*_args, **_kwargs):
        raise RuntimeError("PC Agent 연결 없음 — OCR 불가")

    monkeypatch.setattr(ocr.local_ocr_bridge, "ocr_extract", broken)
    result = asyncio.run(ocr.suggest_from_document(user=admin, business_id=BUSINESS, document_id=UUID(document["id"])))
    assert result["ocr"]["ok"] is False and "PC Agent" in result["ocr"]["error"]
    assert all(item["value"] is None and item["reason"] for item in result["suggested"].values())
    assert json.loads(db.audits[-1]["details"])["ocr_ok"] is False


def test_router_exposes_registration_document_routes():
    paths = {(route.path, method) for route in api.router.routes for method in getattr(route, "methods", ())}
    base = "/yeoljeong-finance/businesses/{business_id}/registration-document"
    assert (base, "POST") in paths
    assert (base, "GET") in paths
    assert (base + "/{document_id}/download", "GET") in paths
    assert (base + "/{document_id}/ocr", "POST") in paths
    assert (base + "/{document_id}", "DELETE") in paths


# --- C. 코드 기본값이 등록값을 덮지 않는다 -----------------------------------------
def test_saved_business_and_branch_names_survive_canonicalize():
    settings = finance._canonicalize_ui_settings({
        "businesses": [{"id": BUSINESS, "name": "언니냉면 성신여대역점", "registrationNo": VALID_NO}],
        "branches": [{"id": "branch-junghwa", "name": "열정국밥 중화본점", "businessId": "biz-other"}],
    })
    business = next(item for item in settings["businesses"] if item["id"] == BUSINESS)
    branch = next(item for item in settings["branches"] if item["id"] == "branch-junghwa")
    assert business["name"] == "언니냉면 성신여대역점"
    assert business["registrationNo"] == VALID_NO
    assert branch["name"] == "열정국밥 중화본점"
    assert branch["businessId"] == "biz-junghwa"  # id 매핑은 그대로 canonical
    # 저장값이 비었을 때만 canonical 상호를 쓴다
    blank = finance._canonicalize_ui_settings({"businesses": [{"id": "biz-mia", "name": "  "}]})
    assert next(item for item in blank["businesses"] if item["id"] == "biz-mia")["name"] == "열정국밥_미아점"
    assert finance.CANONICAL_BUSINESS_IDS == {"biz-junghwa", "biz-sungshin", "biz-eonni-naengmyeon", "biz-mia"}


def test_placeholder_only_business_is_flagged_not_valued():
    settings = finance._canonicalize_ui_settings({
        "businesses": [{"id": "biz-sungshin", "registrationNo": "기초등록 필요", "representative": "미등록"}],
    })
    by_id = {item["id"]: item for item in settings["businesses"]}
    for business_id in ("biz-sungshin", BUSINESS):
        item = by_id[business_id]
        assert item["needs_registration_info"] is True
        assert "registrationNo" in item["missing_registration_fields"]
        assert item["registrationNo"] == "" and item["representative"] == ""
    assert by_id["biz-junghwa"]["needs_registration_info"] is False
    assert by_id["biz-junghwa"]["missing_registration_fields"] == []
    dumped = json.dumps(settings, ensure_ascii=False)
    assert "기초등록 필요" not in dumped
    for seed in finance.CANONICAL_BUSINESSES:
        assert seed["registrationNo"] not in service.REGISTRATION_PLACEHOLDERS


def test_registry_list_flags_placeholder_business(db, monkeypatch):
    class ListConn(FakeConn):
        async def fetch(self, sql, *args):
            return [dict(self.db.businesses[BUSINESS], memo="", entity_type="individual", created_at=None, updated_at=None)]

    async def connect():
        return ListConn(db)

    monkeypatch.setattr(service, "_connect", connect)
    rows = asyncio.run(service.list_businesses(user=_user(TENANT_A)))
    assert rows[0]["registration_no"] == "" and rows[0]["representative"] == ""
    assert rows[0]["needs_registration_info"] is True
    assert rows[0]["missing_registration_fields"] == ["registration_no", "representative", "opened_at", "address"]


def test_ui_uploads_original_and_drops_filename_memo():
    html = Path("app/static/apps/obys/index.html").read_text(encoding="utf-8")
    assert "사업자등록증 파일: ${uploadedFile.name}" not in html
    assert "/registration-document`" in html and "/ocr`" in html
    assert "data-apply-registration-ocr" in html
    assert "businessNeedsRegistrationInfo" in html
    assert 'registrationNo: "기초등록 필요"' not in html
