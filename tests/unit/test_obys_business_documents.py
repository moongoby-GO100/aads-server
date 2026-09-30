"""AADS-OBYS-BUSINESS-DOCUMENTS-20260930 — 사업자 서류 실제 보관·목록·수정·삭제·다운로드."""
import hashlib
import os
import sys
import types
from datetime import datetime, timedelta
from urllib.parse import quote

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

os.environ.setdefault("JWT_SECRET_KEY", "test-only-secret-key-that-is-at-least-32-bytes-long")

from app.api import obys_finance as api

svc = api.svc
TENANT = "15055cac-71b0-45ec-b714-7093dde189ff"
OTHER_TENANT = "2a1b3c4d-0000-4000-8000-000000000001"
MEMBERSHIP = {"tenant_id": TENANT, "status": "active"}
ADMIN = {"email": "owner@example.com", "is_admin": True, "tenant_id": TENANT, "current_membership": MEMBERSHIP}
EMPLOYEE = {"email": "member@example.com", "is_admin": False, "tenant_id": TENANT, "current_membership": MEMBERSHIP}
OTHER_TENANT_ADMIN = {
    "email": "boss@other.example.com",
    "is_admin": True,
    "tenant_id": OTHER_TENANT,
    "current_membership": {"tenant_id": OTHER_TENANT, "status": "active"},
}
PDF = b"%PDF-1.4\n% business registration\n" + b"0" * 2048


def _disable_db(coroutine):
    close = getattr(coroutine, "close", None)
    if close:
        close()
    return None


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    """파일 모드로만 돈다 — 운영 DB·업로드 경로를 건드리지 않는다."""
    for name in ("OBYS_DATABASE_URL", "YEOLJEONG_FINANCE_DATABASE_URL"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("OBYS_UPLOAD_ROOT", str(tmp_path / "upload-root"))
    monkeypatch.setattr(svc, "DATA_DIR", tmp_path / "data")
    monkeypatch.setattr(svc, "UPLOAD_DIR", tmp_path / "data" / "uploads" / "onboarding")
    monkeypatch.setattr(svc, "_run_db", _disable_db)
    clock = {"now": datetime(2026, 9, 30, 9, 0, tzinfo=svc.KST)}

    def fake_now():
        clock["now"] += timedelta(minutes=1)
        return clock["now"].isoformat(timespec="seconds")

    monkeypatch.setattr(svc, "_now", fake_now)
    return tmp_path


def _client(user=ADMIN):
    app = FastAPI()
    app.include_router(api.router)
    app.dependency_overrides[api.get_current_user] = lambda: user
    return TestClient(app)


def _upload(user=ADMIN, *, business_id="biz-mia", document_type="business_registration", data=PDF,
            filename="사업자등록증.pdf", content_type="application/pdf", **fields):
    return _client(user).post(
        "/yeoljeong-finance/business-documents",
        data={"business_id": business_id, "document_type": document_type, **fields},
        files={"file": (filename, data, content_type)},
    )


def _rows():
    return svc._read_file_rows("business_documents")


def _list(business_id="biz-mia", user=ADMIN):
    return _client(user).get("/yeoljeong-finance/business-documents", params={"business_id": business_id})


# --- 저장 ------------------------------------------------------------------


def test_upload_stores_file_on_disk_with_matching_sha256_and_size(isolated):
    response = _upload(issue_date="2026-01-02", memo="세무서 발급본")
    assert response.status_code == 201
    document = response.json()["document"]
    assert document["sha256"] == hashlib.sha256(PDF).hexdigest()
    assert document["byte_size"] == len(PDF)
    assert document["status"] == "current"
    assert document["document_label"] == "사업자등록증"
    assert "stored_path" not in document  # 서버 경로는 화면으로 내보내지 않는다

    row = _rows()[0]
    stored = isolated / "upload-root" / row["stored_path"]
    assert stored.is_file()
    assert stored.read_bytes() == PDF
    assert row["stored_path"].startswith(f"{TENANT}/business_documents/")
    assert oct(stored.stat().st_mode & 0o777) == "0o600"
    assert "사업자등록증" not in row["stored_path"]  # 원본 파일명은 경로에 쓰지 않는다


def test_reupload_same_type_keeps_previous_as_superseded(isolated):
    first = _upload(data=PDF).json()["document"]
    second = _upload(data=PDF + b"v2", memo="정정 발급").json()["document"]

    rows = {row["id"]: row for row in _rows()}
    assert len(rows) == 2  # 이전 행이 지워지지 않는다
    assert rows[first["id"]]["status"] == "superseded"
    assert rows[second["id"]]["status"] == "current"
    assert (isolated / "upload-root" / rows[first["id"]]["stored_path"]).is_file()

    listed = _list().json()
    assert [doc["id"] for doc in listed["documents"]] == [second["id"], first["id"]]
    assert listed["summary"]["biz-mia"]["has_required"] is True

    # 다른 종류는 서로의 current 를 건드리지 않는다.
    lease = _upload(document_type="lease_contract").json()["document"]
    assert lease["status"] == "current"
    assert {row["id"]: row for row in _rows()}[second["id"]]["status"] == "current"


def test_patch_changes_only_metadata_not_file(isolated):
    document = _upload(expires_at="2027-01-01").json()["document"]
    before = _rows()[0]
    response = _client().patch(
        f"/yeoljeong-finance/business-documents/{document['id']}",
        json={"expires_at": "2028-12-31", "memo": "갱신 확인"},
    )
    assert response.status_code == 200
    updated = response.json()["document"]
    assert updated["expires_at"] == "2028-12-31"
    assert updated["memo"] == "갱신 확인"
    after = _rows()[0]
    for field in ("stored_path", "sha256", "byte_size", "original_filename", "content_type", "document_type", "issue_date"):
        assert after[field] == before[field], field

    # 파일 필드는 PATCH 스키마에 없다 — 보내면 거절한다.
    rejected = _client().patch(
        f"/yeoljeong-finance/business-documents/{document['id']}",
        json={"sha256": "0" * 64},
    )
    assert rejected.status_code == 422


def test_patch_rejects_unknown_type_and_bad_dates():
    document = _upload().json()["document"]
    url = f"/yeoljeong-finance/business-documents/{document['id']}"
    assert _client().patch(url, json={"document_type": "not-a-type"}).status_code == 400
    assert _client().patch(url, json={"expires_at": "2026/13/40"}).status_code == 400
    assert _client().patch(url, json={"issue_date": "2027-01-02", "expires_at": "2027-01-01"}).status_code == 400


def test_delete_is_soft_and_previous_revision_becomes_current(isolated):
    first = _upload(data=PDF).json()["document"]
    second = _upload(data=PDF + b"v2").json()["document"]
    response = _client().delete(f"/yeoljeong-finance/business-documents/{second['id']}")
    assert response.status_code == 200

    rows = {row["id"]: row for row in _rows()}
    assert set(rows) == {first["id"], second["id"]}  # 행은 남는다
    assert rows[second["id"]]["deleted_at"]
    assert rows[second["id"]]["status"] == "deleted"
    assert (isolated / "upload-root" / rows[second["id"]]["stored_path"]).is_file()  # 원본도 남는다
    assert rows[first["id"]]["status"] == "current"

    listed = _list().json()["documents"]
    assert [doc["id"] for doc in listed] == [first["id"]]
    assert _client().get(f"/yeoljeong-finance/business-documents/{second['id']}/download").status_code == 404
    assert _client().delete(f"/yeoljeong-finance/business-documents/{second['id']}").status_code == 404


def test_download_returns_original_filename_and_content_type():
    image = b"\x89PNG\r\n\x1a\n" + b"1" * 512
    document = _upload(document_type="bankbook", data=image, filename="통장 사본.png", content_type="image/png").json()["document"]
    response = _client().get(f"/yeoljeong-finance/business-documents/{document['id']}/download")
    assert response.status_code == 200
    assert response.content == image
    assert response.headers["content-type"] == "image/png"
    assert quote("통장 사본.png") in response.headers["content-disposition"]
    assert response.headers["cache-control"] == "no-store"


def test_download_refuses_tampered_file(isolated):
    document = _upload().json()["document"]
    (isolated / "upload-root" / _rows()[0]["stored_path"]).write_bytes(b"tampered")
    assert _client().get(f"/yeoljeong-finance/business-documents/{document['id']}/download").status_code == 409


def test_upload_over_10mb_is_413_and_leaves_nothing(isolated):
    response = _upload(data=b"0" * (10 * 1024 * 1024 + 1))
    assert response.status_code == 413
    assert _rows() == []
    assert not (isolated / "upload-root" / TENANT / "business_documents").exists()


def test_upload_rejects_empty_file_unknown_type_and_extension():
    assert _upload(data=b"").status_code == 400
    assert _upload(document_type="unknown").status_code == 400
    assert _upload(filename="run.exe", content_type="application/octet-stream").status_code == 400
    assert _rows() == []


# --- 만료 ------------------------------------------------------------------


def test_expiry_flags_for_expiring_and_expired_documents():
    today = datetime.now(svc.KST).date()
    soon = _upload(document_type="fire_insurance", expires_at=(today + timedelta(days=10)).isoformat()).json()["document"]
    past = _upload(document_type="lease_contract", expires_at=(today - timedelta(days=1)).isoformat()).json()["document"]
    later = _upload(document_type="hygiene_training", expires_at=(today + timedelta(days=90)).isoformat()).json()["document"]
    edge = _upload(document_type="seal_certificate", expires_at=(today + timedelta(days=30)).isoformat()).json()["document"]

    docs = {doc["id"]: doc for doc in _list().json()["documents"]}
    assert docs[soon["id"]]["expiry_status"] == "expiring" and docs[soon["id"]]["is_expiring"]
    assert docs[soon["id"]]["expiry_label"] == "만료 임박"
    assert docs[past["id"]]["expiry_status"] == "expired" and docs[past["id"]]["is_expired"]
    assert docs[past["id"]]["expiry_label"] == "만료"
    assert docs[later["id"]]["expiry_status"] == ""
    assert docs[edge["id"]]["expiry_status"] == "expiring"

    summary = _list().json()["summary"]["biz-mia"]
    assert summary["expiring"] == 2 and summary["expired"] == 1
    assert summary["needs_documents"] is True  # 사업자등록증이 없다


def test_list_returns_server_owned_type_catalog_and_required_flag():
    payload = _list().json()
    labels = [item["label"] for item in payload["document_types"]]
    assert labels == [
        "사업자등록증", "영업신고증", "통장사본", "임대차계약서", "위생교육수료증", "화재보험증서",
        "법인등기부등본", "인감증명서", "대표자신분증", "세무대리인위임장", "통신판매업신고증", "기타",
    ]
    assert payload["required_document_type"] == "business_registration"
    assert payload["documents"] == []

    _upload()
    everything = _client().get("/yeoljeong-finance/business-documents").json()
    assert everything["summary"]["biz-mia"]["needs_documents"] is False


# --- 권한 ------------------------------------------------------------------


def test_non_admin_is_forbidden_for_every_operation():
    document = _upload().json()["document"]
    client = _client(EMPLOYEE)
    assert client.get("/yeoljeong-finance/business-documents").status_code == 403
    assert _list(user=EMPLOYEE).status_code == 403
    assert _upload(EMPLOYEE).status_code == 403
    assert client.get(f"/yeoljeong-finance/business-documents/{document['id']}/download").status_code == 403
    assert client.patch(f"/yeoljeong-finance/business-documents/{document['id']}", json={"memo": "x"}).status_code == 403
    assert client.delete(f"/yeoljeong-finance/business-documents/{document['id']}").status_code == 403
    assert _rows()[0]["memo"] == "" and not _rows()[0]["deleted_at"]


def test_non_admin_upload_is_rejected_before_body_is_read(monkeypatch):
    async def must_not_read(file):
        raise AssertionError("본문을 읽기 전에 권한에서 막혀야 한다")

    monkeypatch.setattr(api, "_read_limited_upload", must_not_read)
    assert _upload(EMPLOYEE).status_code == 403
    assert _upload(ADMIN, business_id="biz-not-ours").status_code == 403


def test_other_tenant_is_forbidden_at_router_and_service():
    document = _upload().json()["document"]
    client = _client(OTHER_TENANT_ADMIN)
    assert client.get("/yeoljeong-finance/business-documents", params={"business_id": "biz-mia"}).status_code == 403
    assert _upload(OTHER_TENANT_ADMIN).status_code == 403
    assert client.get(f"/yeoljeong-finance/business-documents/{document['id']}/download").status_code == 403
    assert client.patch(f"/yeoljeong-finance/business-documents/{document['id']}", json={"memo": "x"}).status_code == 403
    assert client.delete(f"/yeoljeong-finance/business-documents/{document['id']}").status_code == 403

    # 라우터 게이트와 별개로 서비스 자체도 다른 테넌트 행을 막는다.
    for call in (
        lambda: svc.get_business_document_file(document["id"], OTHER_TENANT_ADMIN),
        lambda: svc.update_business_document(document["id"], {"memo": "x"}, OTHER_TENANT_ADMIN),
        lambda: svc.delete_business_document(document["id"], OTHER_TENANT_ADMIN),
    ):
        with pytest.raises(svc.HTTPException) as exc:
            call()
        assert exc.value.status_code == 403
    # 다른 테넌트의 목록에는 이 테넌트의 행이 섞이지 않는다.
    assert svc._business_document_rows(OTHER_TENANT, None) == []
    assert _rows()[0]["memo"] == "" and not _rows()[0]["deleted_at"]


def test_foreign_business_is_forbidden_for_every_operation():
    assert _list(business_id="biz-not-ours").status_code == 403
    assert _upload(business_id="biz-not-ours").status_code == 403

    # 테넌트는 같지만 이 테넌트 사업자가 아닌 행(매핑이 끊긴 사업자 등).
    document = _upload().json()["document"]
    rows = _rows()
    rows[0]["business_id"] = "biz-not-ours"
    svc._write_file_rows("business_documents", rows)
    client = _client()
    assert client.get(f"/yeoljeong-finance/business-documents/{document['id']}/download").status_code == 403
    assert client.patch(f"/yeoljeong-finance/business-documents/{document['id']}", json={"memo": "x"}).status_code == 403
    assert client.delete(f"/yeoljeong-finance/business-documents/{document['id']}").status_code == 403
    assert all(doc["business_id"] != "biz-not-ours" for doc in client.get("/yeoljeong-finance/business-documents").json()["documents"])


# --- DB 모드: WHERE 절 이중 차단 --------------------------------------------


class _FakeConn:
    def __init__(self, log):
        self.log = log

    async def execute(self, query, *args):
        self.log.append((query, args))
        return "UPDATE 1"

    async def fetchval(self, query, *args):
        self.log.append((query, args))
        return True

    async def fetch(self, query, *args):
        self.log.append((query, args))
        return []

    def transaction(self):
        conn = self

        class _Tx:
            async def __aenter__(self):
                return conn

            async def __aexit__(self, *exc):
                return False

        return _Tx()

    async def close(self):
        return None


def test_db_queries_bind_tenant_and_business(monkeypatch):
    import asyncio

    log: list = []

    async def connect(*args, **kwargs):
        return _FakeConn(log)

    monkeypatch.setitem(sys.modules, "asyncpg", types.SimpleNamespace(connect=connect))
    monkeypatch.setenv("OBYS_DATABASE_URL", "postgresql://fake/obys")
    record = {
        "id": "7c9e6679-7425-40de-944b-e07fc1f90ae7", "tenant_id": TENANT, "business_id": "biz-mia",
        "document_type": "business_registration", "document_label": "사업자등록증", "issue_date": "",
        "expires_at": "2027-01-01", "memo": "m", "updated_at": "2026-09-30T09:00:00+09:00",
    }
    assert asyncio.run(svc._db_business_document_update(record, "business_registration")) is True
    assert asyncio.run(svc._db_business_document_soft_delete(record, "2026-09-30T09:00:00+09:00")) is True
    asyncio.run(svc._db_business_documents_fetch(TENANT, "biz-mia"))

    writes = [(q, a) for q, a in log if q.lstrip().upper().startswith("UPDATE")]
    guarded = [(q, a) for q, a in writes if "WHERE id = $1" in q]
    assert len(guarded) == 2
    for query, args in guarded:
        assert "tenant_id = $2" in query and "business_id = $3" in query
        assert str(args[1]) == TENANT and args[2] == "biz-mia"
    for query, args in writes:
        assert str(args[0]) == TENANT or str(args[1]) == TENANT
    reads = [(q, a) for q, a in log if "yeoljeong_business_tenant_mapping" in q]
    assert reads and str(reads[0][1][0]) == TENANT and reads[0][1][1] == "biz-mia"
