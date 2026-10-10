"""ACCT-OBYS-JOIN-DYNAMIC-WORKPLACES-20261010 — 직원 가입 화면의 근무지 목록을 DB 기준으로.

매장형(지점 있음)은 지점을 고르고, 사무실형(지점 없음·이커머스)은 근무지를 '사무실' 하나로 자동 지정한다.
오비서 DB 는 인메모리 가짜로 흉내 낸다 — 운영 DB 미접촉.
"""
from __future__ import annotations

import asyncio
import json
import os

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

os.environ.setdefault("JWT_SECRET_KEY", "test-only-secret-key-that-is-at-least-32-bytes-long")

from app.api import obys_finance as api  # noqa: E402

svc = api.svc

EMPLOYER = "15055cac-71b0-45ec-b714-7093dde189ff"
NEW_EMP_TENANT = "d184a18c-78c8-45c7-aee7-bfe79fadf95f"
EMP_EMAIL = "new-hire@example.com"
MISMATCH = "직원의 사업자와 지점 연결이 일치하지 않습니다"

STORE_BIZ = "biz-junghwa"
OFFICE_BIZ = "biz-office-danharu"
UNLINKED_BIZ = "biz-unlinked-chic"
EXPLICIT_OFFICE_BIZ = "biz-explicit-office"
EXPLICIT_BRANCH_BIZ = "biz-explicit-branches"
CHICBLACK_BIZ = "biz-chicblack"
OBYWAI_BIZ = "biz-obywai"
E2E_ID_BIZ = "biz-e2e-m1-a"
E2E_NAME_BIZ = "biz-renamed-test"
E2E_PREFIX_ONLY_BIZ = "biz-e2e-m1-b"

BUSINESSES = {
    "biz-junghwa": ("열정국밥 중화점", ["중화점"], ""),
    "biz-sungshin": ("열정국밥 성신여대점", ["성신여대점"], ""),
    "biz-mia": ("열정국밥_미아점", ["열정국밥_미아점"], ""),
    OFFICE_BIZ: ("단하루", [], ""),
    EXPLICIT_OFFICE_BIZ: ("명시사무실", ["본점"], "office"),
    EXPLICIT_BRANCH_BIZ: ("명시매장", [], "branches"),
    CHICBLACK_BIZ: ("주식회사 시크블랙", [], ""),
    OBYWAI_BIZ: ("주식회사 오비와이", [], ""),
    E2E_ID_BIZ: ("E2E-오비서시험A", ["시험점A"], ""),
    E2E_NAME_BIZ: ("E2E-이름만시험", [], ""),
    E2E_PREFIX_ONLY_BIZ: ("시험용 이름", [], ""),
}
ADDRESSES = {OFFICE_BIZ: "서울특별시 어딘가 1층"}
MAPPING = {key: EMPLOYER for key in BUSINESSES}


def _user(tenant_id: str, role: str, *, email: str, user_id: str) -> dict:
    return {
        "user_id": user_id,
        "email": email,
        "is_admin": False,
        "tenant_id": tenant_id,
        "tenant_role": role,
        "user_role": "user",
        "current_membership": {"tenant_id": tenant_id, "status": "active", "role": role},
    }


ADMIN = _user(EMPLOYER, "owner", email="owner@example.com", user_id="user-owner")
EMPLOYEE = _user(NEW_EMP_TENANT, "owner", email=EMP_EMAIL, user_id="user-new-hire")


@pytest.fixture
def db(tmp_path, monkeypatch):
    for name in ("OBYS_DATABASE_URL", "YEOLJEONG_FINANCE_DATABASE_URL", "OBYS_LEGACY_TENANT_IDS"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(svc, "DATA_DIR", tmp_path / "data")
    monkeypatch.setattr(svc, "UPLOAD_DIR", tmp_path / "data" / "uploads" / "onboarding")
    rows: list[dict] = []

    async def fake_fetch_ledger(name, tenant_id=None):
        return [dict(r) for r in rows if tenant_id and r["tenant_id"] == tenant_id and r.get("_ledger", "employee_join_requests") == name]

    async def fake_upsert_ledger(name, record):
        for index, row in enumerate(rows):
            if row["id"] == record["id"]:
                rows[index] = dict(record)
                return True
        rows.append(dict(record))
        return True

    async def fake_business_tenant_id(business_id):
        return MAPPING.get(business_id)

    async def fake_business_tenant_matches(business_id, tenant_id):
        return MAPPING.get(business_id) == tenant_id

    async def fake_hr_record_tenant(name, row_id):
        return next((r["tenant_id"] for r in rows if r["id"] == row_id), None)

    async def fake_fetch_by_email(email):
        return [dict(r) for r in rows if str(r.get("email") or "").lower() == email]

    async def fake_business_invite_info(business_id):
        if business_id not in BUSINESSES:
            return None
        name, branches, mode = BUSINESSES[business_id]
        return {"name": name, "address": ADDRESSES.get(business_id, ""), "branches": list(branches), "workplace_mode": mode}

    async def fake_join_workplace_businesses():
        return [
            {"id": key, "name": name, "branches": list(branches), "workplace_mode": mode}
            for key, (name, branches, mode) in BUSINESSES.items()
            if key in MAPPING
        ]

    def run_db(coro):
        if coro.__name__.startswith("fake_"):
            return asyncio.run(coro)
        coro.close()
        return None

    monkeypatch.setattr(svc, "_db_available", lambda: True)
    monkeypatch.setattr(svc, "_run_db", run_db)
    monkeypatch.setattr(svc, "_db_fetch_ledger", fake_fetch_ledger)
    monkeypatch.setattr(svc, "_db_upsert_ledger", fake_upsert_ledger)
    monkeypatch.setattr(svc, "_db_business_tenant_id", fake_business_tenant_id)
    monkeypatch.setattr(svc, "_db_business_tenant_matches", fake_business_tenant_matches)
    monkeypatch.setattr(svc, "_db_hr_record_tenant", fake_hr_record_tenant)
    monkeypatch.setattr(svc, "_db_fetch_join_requests_by_email", fake_fetch_by_email)
    monkeypatch.setattr(svc, "_db_business_invite_info", fake_business_invite_info)
    monkeypatch.setattr(svc, "_db_join_workplace_businesses", fake_join_workplace_businesses)
    return rows


def _listing() -> dict[str, dict]:
    payload = asyncio.run(svc.list_join_workplaces())
    return {item["business_id"]: item for item in payload["businesses"]}


def _join(business_id: str, branch: str = "") -> dict:
    payload = {"name": "양재혁", "email": EMP_EMAIL, "business_id": business_id}
    if branch:
        payload["branch"] = branch
    return svc.upsert_join_request(payload, EMPLOYEE)


def _workplace_names(item: dict) -> list[str]:
    return [w["branch"] for w in item["workplaces"]]


# 1. 지점 0개 사업자 → 사무실형, 근무지 '사무실' 하나
def test_zero_branch_business_is_office_with_single_workplace(db):
    item = _listing()[OFFICE_BIZ]
    assert item["mode"] == "office"
    assert item["workplaces"] == [{"branch": "사무실", "label": "사무실"}]


# 2. 지점이 있는 사업자 → 등록된 지점만
def test_business_with_branches_lists_only_its_registered_branches(db):
    listing = _listing()
    assert listing["biz-junghwa"]["mode"] == "branches"
    assert _workplace_names(listing["biz-junghwa"]) == ["중화점"]
    assert _workplace_names(listing["biz-sungshin"]) == ["성신여대점"]
    assert _workplace_names(listing["biz-mia"]) == ["열정국밥_미아점"]


def test_explicit_mode_overrides_the_branch_count(db):
    listing = _listing()
    assert listing[EXPLICIT_OFFICE_BIZ]["mode"] == "office"
    assert _workplace_names(listing[EXPLICIT_OFFICE_BIZ]) == ["사무실"]
    assert listing[EXPLICIT_BRANCH_BIZ]["mode"] == "branches"
    assert listing[EXPLICIT_BRANCH_BIZ]["workplaces"] == []


# 2-1. E2E 시험 사업자는 공개 목록에서 빠진다 (id 접두 'biz-e2e-' 또는 상호 'E2E-' 시작)
def test_e2e_test_businesses_are_hidden_from_the_public_listing(db):
    listing = _listing()
    for hidden in (E2E_ID_BIZ, E2E_NAME_BIZ, E2E_PREFIX_ONLY_BIZ):
        assert hidden not in listing
    assert {"biz-junghwa", "biz-sungshin", "biz-mia", OFFICE_BIZ} <= set(listing)


@pytest.mark.parametrize("business_id", [E2E_ID_BIZ, E2E_NAME_BIZ, E2E_PREFIX_ONLY_BIZ])
@pytest.mark.parametrize("branch", ["", "사무실", "시험점A"])
def test_join_to_an_e2e_test_business_is_rejected(db, business_id, branch):
    with pytest.raises(HTTPException) as excinfo:
        _join(business_id, branch)
    assert excinfo.value.status_code == 400
    assert excinfo.value.detail == "등록되지 않은 사업자입니다"
    assert db == []


def test_e2e_test_business_name_check_is_exact_prefix():
    assert svc._is_e2e_test_business("biz-e2e-x")
    assert svc._is_e2e_test_business("biz-x", "E2E-시험")
    assert not svc._is_e2e_test_business("biz-junghwa", "열정국밥 중화점")
    assert not svc._is_e2e_test_business("biz-chicblack", "주식회사 시크블랙")
    assert not svc._is_e2e_test_business("biz-x", "my E2E-시험")


# 2-2. 지점 없는 시크블랙·오비와이는 목록에 남고 근무지는 '사무실' 로 자동 지정된다
@pytest.mark.parametrize(("business_id", "name"), [(CHICBLACK_BIZ, "주식회사 시크블랙"), (OBYWAI_BIZ, "주식회사 오비와이")])
def test_zero_branch_company_is_listed_as_office(db, business_id, name):
    item = _listing()[business_id]
    assert item["business_name"] == name
    assert item["mode"] == "office"
    assert item["workplaces"] == [{"branch": "사무실", "label": "사무실"}]


@pytest.mark.parametrize("business_id", [CHICBLACK_BIZ, OBYWAI_BIZ])
@pytest.mark.parametrize("sent", ["", "사무실"])
def test_zero_branch_company_join_is_saved_as_office(db, business_id, sent):
    saved = _join(business_id, sent)
    assert saved["business_id"] == business_id
    assert saved["branch"] == "사무실"
    assert saved["tenant_id"] == EMPLOYER


# 3. 테넌트 미연결 사업자는 노출하지 않는다 — 실제 SQL 이 매핑 테이블과 JOIN 하는지까지 확인
def test_tenant_unlinked_business_is_not_exposed(monkeypatch):
    import asyncpg

    monkeypatch.setattr(svc, "_db_available", lambda: True)
    monkeypatch.setattr(svc, "_db_url", lambda: "postgresql://unused")
    all_businesses = {"biz-junghwa": "열정국밥 중화점", UNLINKED_BIZ: "시크블랙", OFFICE_BIZ: "단하루"}
    linked = {"biz-junghwa", OFFICE_BIZ}
    seen_sql: list[str] = []

    class FakeConn:
        async def fetch(self, sql, *args):
            seen_sql.append(sql)
            if "FROM yeoljeong_businesses" in sql:
                joined = "yeoljeong_business_tenant_mapping" in sql
                return [{"id": k, "name": v} for k, v in all_businesses.items() if not joined or k in linked]
            return [{"business_id": "biz-junghwa", "name": "중화점"}]

        async def fetchval(self, sql, *args):
            return json.dumps({"workplace_modes": {"biz-junghwa": "branches"}})

        async def close(self):
            return None

    async def fake_connect(*_args, **_kwargs):
        return FakeConn()

    monkeypatch.setattr(asyncpg, "connect", fake_connect)
    listing = _listing()
    assert set(listing) == {"biz-junghwa", OFFICE_BIZ}
    assert UNLINKED_BIZ not in listing
    assert any("yeoljeong_business_tenant_mapping" in sql for sql in seen_sql)


def test_public_listing_exposes_names_only(db):
    payload = asyncio.run(svc.list_join_workplaces())
    assert set(payload) == {"businesses"}
    for item in payload["businesses"]:
        assert set(item) == {"business_id", "business_name", "mode", "workplaces"}
        for workplace in item["workplaces"]:
            assert set(workplace) == {"branch", "label"}
    assert "서울특별시" not in json.dumps(payload, ensure_ascii=False)


# 4. 목록 밖 (사업자, 근무지) 조합은 400
@pytest.mark.parametrize(
    ("business_id", "branch", "detail"),
    [
        ("biz-junghwa", "성신여대점", MISMATCH),  # 다른 사업자의 지점
        ("biz-junghwa", "사무실", MISMATCH),  # 매장형에 사무실
        ("biz-junghwa", "", "근무 점포를 선택해 주십시오"),  # 매장형은 지점 필수
        (OFFICE_BIZ, "중화점", MISMATCH),  # 사무실형에 지점
        (EXPLICIT_OFFICE_BIZ, "본점", MISMATCH),  # 명시 사무실형은 등록 지점이 있어도 '사무실' 만
        ("biz-does-not-exist", "사무실", "등록되지 않은 사업자입니다"),
    ],
)
def test_join_outside_the_list_is_400(db, business_id, branch, detail):
    with pytest.raises(HTTPException) as excinfo:
        _join(business_id, branch)
    assert excinfo.value.status_code == 400
    assert excinfo.value.detail == detail
    assert db == []


# 5. 사무실형 가입은 branch='사무실' 로 저장
@pytest.mark.parametrize("sent", ["", "사무실", "단하루"])
def test_office_join_is_saved_with_office_workplace(db, sent):
    saved = _join(OFFICE_BIZ, sent)
    assert saved["business_id"] == OFFICE_BIZ
    assert saved["branch"] == "사무실"
    assert saved["tenant_id"] == EMPLOYER


# 6. 기존 열정국밥 3개 점포 흐름 유지
@pytest.mark.parametrize(
    ("business_id", "branch"),
    [("biz-junghwa", "중화점"), ("biz-sungshin", "성신여대점"), ("biz-mia", "열정국밥_미아점")],
)
def test_existing_three_store_joins_are_preserved(db, business_id, branch):
    saved = _join(business_id, branch)
    assert saved["business_id"] == business_id
    assert saved["branch"] == branch


def test_new_branch_is_joinable_immediately(db, monkeypatch):
    new_branch = "신규오픈점"
    with pytest.raises(HTTPException):
        _join("biz-junghwa", new_branch)
    BUSINESSES["biz-junghwa"] = ("열정국밥 중화점", ["중화점", new_branch], "")
    try:
        assert new_branch in _workplace_names(_listing()["biz-junghwa"])
        assert _join("biz-junghwa", new_branch)["branch"] == new_branch
    finally:
        BUSINESSES["biz-junghwa"] = ("열정국밥 중화점", ["중화점"], "")


def test_invite_for_office_business_is_accepted_as_office(db):
    invite = svc.create_invite(
        {"name": "양재혁", "phone": "010-1234-5678", "targets": [{"business_id": OFFICE_BIZ, "branch": ""}]},
        ADMIN,
    )
    result = svc.accept_invite({"token": invite["token"]}, EMPLOYEE)
    assert result["business_id"] == OFFICE_BIZ
    assert result["branch"] == "사무실"


def test_invite_target_office_workplace_is_valid_and_store_branch_is_not(db):
    ok = svc.create_invite({"name": "양재혁", "phone": "010-1234-5678", "targets": [{"business_id": OFFICE_BIZ, "branch": "사무실"}]}, ADMIN)
    assert ok["token"]
    with pytest.raises(HTTPException) as excinfo:
        svc.create_invite({"name": "양재혁", "phone": "010-1234-5678", "targets": [{"business_id": OFFICE_BIZ, "branch": "중화점"}]}, ADMIN)
    assert excinfo.value.status_code == 400


# 7. 사무실 ↔ 지점 이동 (사업자·지점 변경 기능)
def _approved(row_id: str, business_id: str, branch: str) -> dict:
    return {
        "id": row_id,
        "tenant_id": EMPLOYER,
        "status": "approved",
        "name": "양재혁",
        "email": EMP_EMAIL,
        "business_id": business_id,
        "branch": branch,
    }


def test_office_employee_can_be_moved_to_a_branch_after_office_becomes_branches(db):
    db.append(_approved("emp-1", OFFICE_BIZ, "사무실"))
    BUSINESSES[OFFICE_BIZ] = ("단하루", ["강남점"], "branches")
    try:
        plan = svc._plan_employee_assignment("emp-1", {"business_id": OFFICE_BIZ, "branch": "강남점"}, ADMIN)
        assert plan["current"] == {"business_id": OFFICE_BIZ, "branch": "사무실"}
        assert plan["target"]["branch"] == "강남점"
        with pytest.raises(HTTPException) as excinfo:
            svc._plan_employee_assignment("emp-1", {"business_id": OFFICE_BIZ, "branch": ""}, ADMIN)
        assert excinfo.value.status_code == 400
    finally:
        BUSINESSES[OFFICE_BIZ] = ("단하루", [], "")


def test_branch_employee_moved_to_office_business_gets_office_workplace(db):
    db.append(_approved("emp-2", "biz-junghwa", "중화점"))
    plan = svc._plan_employee_assignment("emp-2", {"business_id": OFFICE_BIZ, "branch": ""}, ADMIN)
    assert plan["target"]["branch"] == "사무실"
    assert plan["target"]["business_id"] == OFFICE_BIZ


def test_assignment_targets_list_office_workplace(db, monkeypatch):
    async def fake_assignment_targets(tenant_ids):
        return [{"business_id": OFFICE_BIZ, "business_name": "단하루", "tenant_id": EMPLOYER, "workplace_mode": "office", "branches": ["사무실"]}]

    monkeypatch.setattr(svc, "_db_assignment_targets", fake_assignment_targets)
    monkeypatch.setattr(svc, "_is_platform_principal", lambda _user: True)
    targets = asyncio.run(svc.list_assignment_targets(ADMIN))
    assert targets[0]["branches"] == ["사무실"]
    assert targets[0]["same_tenant"] is True


# 8. 계약서 근무장소 기본값
def test_contract_business_accepts_office_workplace_only_for_office_business(db):
    assert svc._contract_business({"business_id": OFFICE_BIZ, "branch": "사무실"}, None) == (OFFICE_BIZ, "사무실")
    with pytest.raises(HTTPException) as excinfo:
        svc._contract_business({"business_id": "biz-junghwa", "branch": "사무실"}, None)
    assert excinfo.value.status_code == 400


def test_contract_workplace_defaults_to_business_address_for_office(db):
    svc.save_settings(
        {"settings": {"businesses": [{"id": OFFICE_BIZ, "name": "단하루", "address": "서울 서초구 사무실로 1"}], "branches": []}},
        ADMIN,
    )
    filled = svc._fill_contract_reference_data({"business_id": OFFICE_BIZ, "branch": "사무실"}, ADMIN)
    assert filled["workplace"] == "서울 서초구 사무실로 1"
    kept = svc._fill_contract_reference_data({"business_id": OFFICE_BIZ, "branch": "사무실", "workplace": "재택"}, ADMIN)
    assert kept["workplace"] == "재택"


def test_contract_workplace_falls_back_to_office_label_without_address(db):
    filled = svc._fill_contract_reference_data({"business_id": OFFICE_BIZ, "branch": "사무실"}, ADMIN)
    assert filled["workplace"] == "사무실"


# 9. 설정 저장: 근무 형태 보존
def test_ui_settings_keep_a_valid_workplace_mode_and_drop_garbage(db):
    cleaned = svc._canonicalize_ui_settings(
        {"businesses": [{"id": "biz-a", "name": "A", "workplaceMode": "OFFICE"}, {"id": "biz-b", "name": "B", "workplaceMode": "weird"}], "branches": []}
    )
    modes = {item["id"]: item["workplaceMode"] for item in cleaned["businesses"]}
    assert modes["biz-a"] == "office"
    assert modes["biz-b"] == ""


class _FakePool:
    def __init__(self, stored_modes):
        self.stored_modes = stored_modes
        self.saved_extra: dict | None = None

    def acquire(self):
        pool = self

        class Ctx:
            async def __aenter__(self_inner):
                return _FakeConn(pool)

            async def __aexit__(self_inner, *exc):
                return False

        return Ctx()


class _FakeConn:
    def __init__(self, pool):
        self.pool = pool

    async def fetchval(self, sql, *args):
        if "to_regclass" in sql:
            return True
        if "SELECT data FROM yeoljeong_settings" in sql:
            return json.dumps({"workplace_modes": self.pool.stored_modes})
        return None

    def transaction(self):
        class Tx:
            async def __aenter__(self_inner):
                return None

            async def __aexit__(self_inner, *exc):
                return False

        return Tx()

    async def execute(self, sql, *args):
        if "INSERT INTO yeoljeong_settings" in sql:
            self.pool.saved_extra = json.loads(args[0])


def _save_with_pool(tmp_path, monkeypatch, stored_modes, businesses):
    for name in ("OBYS_DATABASE_URL", "YEOLJEONG_FINANCE_DATABASE_URL", "OBYS_LEGACY_TENANT_IDS"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(svc, "DATA_DIR", tmp_path / "data")
    monkeypatch.setattr(svc, "UPLOAD_DIR", tmp_path / "data" / "uploads" / "onboarding")
    monkeypatch.setattr(svc, "_db_available", lambda: False)
    pool = _FakePool(stored_modes)
    monkeypatch.setattr(svc, "_get_pool_or_none", lambda: pool)
    result = asyncio.run(svc.save_settings_persisted({"settings": {"businesses": businesses, "branches": []}}, ADMIN))
    return pool, result


def test_settings_save_persists_the_explicit_workplace_mode(tmp_path, monkeypatch):
    pool, result = _save_with_pool(tmp_path, monkeypatch, {}, [{"id": OFFICE_BIZ, "name": "단하루", "workplaceMode": "office"}])
    assert pool.saved_extra["workplace_modes"] == {OFFICE_BIZ: "office"}
    assert {b["id"]: b["workplaceMode"] for b in result["settings"]["businesses"]}[OFFICE_BIZ] == "office"


def test_settings_save_without_the_key_keeps_the_stored_mode(tmp_path, monkeypatch):
    pool, result = _save_with_pool(tmp_path, monkeypatch, {OFFICE_BIZ: "office"}, [{"id": OFFICE_BIZ, "name": "단하루"}])
    assert pool.saved_extra["workplace_modes"] == {OFFICE_BIZ: "office"}
    assert {b["id"]: b["workplaceMode"] for b in result["settings"]["businesses"]}[OFFICE_BIZ] == "office"


def test_settings_save_with_empty_mode_clears_it_back_to_auto(tmp_path, monkeypatch):
    pool, _result = _save_with_pool(tmp_path, monkeypatch, {OFFICE_BIZ: "office"}, [{"id": OFFICE_BIZ, "name": "단하루", "workplaceMode": ""}])
    assert pool.saved_extra["workplace_modes"] == {}


# 10. 공개 경로: 로그인 없이 열리고, 같은 라우터의 나머지는 여전히 인증이 필요하다
@pytest.fixture
def obys_client(monkeypatch):
    from app import yeoljeong_main

    monkeypatch.setattr(svc, "_db_available", lambda: False)
    return TestClient(yeoljeong_main.app, raise_server_exceptions=False)


def test_public_join_workplaces_route_needs_no_login(obys_client):
    response = obys_client.get("/api/v1/yeoljeong-finance/public/join-workplaces")
    assert response.status_code == 200
    by_id = {item["business_id"]: item for item in response.json()["businesses"]}
    assert [w["branch"] for w in by_id["biz-junghwa"]["workplaces"]] == ["중화점"]
    assert [w["branch"] for w in by_id["biz-sungshin"]["workplaces"]] == ["성신여대점"]
    assert [w["branch"] for w in by_id["biz-mia"]["workplaces"]] == ["열정국밥_미아점"]
    assert all(item["mode"] in ("branches", "office") for item in by_id.values())


@pytest.mark.parametrize(
    "path",
    [
        "/api/v1/yeoljeong-finance/employees/join-requests",
        "/api/v1/yeoljeong-finance/employees/assignment-targets",
        "/api/v1/yeoljeong-finance/public/join-workplaces/",
        "/api/v1/yeoljeong-finance/public",
    ],
)
def test_other_routes_still_require_authentication(obys_client, path):
    assert obys_client.get(path, follow_redirects=False).status_code == 401


def test_main_app_exempts_only_the_exact_public_path():
    source = open("app/main.py", encoding="utf-8").read()
    block = source.split("_PUBLIC_READONLY_EXACT_PATHS = {", 1)[1].split("}", 1)[0]
    assert '"/api/v1/yeoljeong-finance/public/join-workplaces"' in block
    assert "obys_finance.public_router" in source
