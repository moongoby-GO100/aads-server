"""ACCT-OBYS-PAYROLL-EMAIL-NULLABLE-20261006 — 급여내역서 employee_email NULL 허용과 사용처 보완.

이메일 없는 행(이름만 있는 행)은 NULL 로 저장되고, 직원 화면·집계·교부에서 제외되며,
초대 수락으로 연결되면 그때부터 직원 목록에 보인다. 오비서 DB 는 인메모리 가짜 — 운영 DB 미접촉.
"""
from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from tests.unit.test_obys_invite_email import (
    ADMIN,
    EMP_EMAIL,
    EMPLOYEE,
    EMPLOYER,
    _invite,
    _payroll,
    _statement,
    _user,
    svc,
)
from tests.unit.test_obys_invite_email import db as invite_db  # noqa: F401  (pytest fixture)



@pytest.fixture(name="db")
def _db(request):
    return request.getfixturevalue("invite_db")


ROOT = Path(__file__).resolve().parents[2]
UP = (ROOT / "migrations/20261006_obys_payroll_email_nullable.sql").read_text(encoding="utf-8")
DOWN = (ROOT / "migrations/rollback/20261006_obys_payroll_email_nullable.down.sql").read_text(encoding="utf-8")


# --- 저장 ------------------------------------------------------------------------------
class _FakeConn:
    def __init__(self):
        self.executed: list[tuple] = []

    async def fetchval(self, *_args):
        return True

    async def execute(self, sql, *args):
        self.executed.append((sql, args))
        return "INSERT 0 1"

    async def close(self):
        pass


def _bound_email(monkeypatch, record: dict):
    import asyncpg

    conn = _FakeConn()

    async def connect(*_a, **_k):
        return conn

    monkeypatch.setattr(asyncpg, "connect", connect)
    monkeypatch.setattr(svc, "_db_url", lambda: "postgresql://fake")
    assert asyncio.run(svc._db_upsert_ledger("payroll_statements", record)) is True
    sql, args = conn.executed[-1]
    assert "INSERT INTO yeoljeong_payroll_statements" in sql
    return args[1]


@pytest.mark.parametrize("raw", ["", "   ", None])
def test_row_without_email_is_bound_as_null(monkeypatch, raw):
    record = _statement("s-1", "양재혁", email="")
    record["employee_email"] = raw
    assert _bound_email(monkeypatch, record) is None


def test_row_with_email_is_bound_lowercased(monkeypatch):
    record = _statement("s-1", "양재혁", email="  Yang@Example.com ")
    assert _bound_email(monkeypatch, record) == "yang@example.com"


def test_save_payroll_without_email_stores_name_only_row_marked_pending(db):
    saved = svc.save_payroll({"employee_name": " 양재혁 ", "business_id": "biz-sungshin", "payroll_month": "2026-09"}, ADMIN)
    assert saved["employee_email"] == ""
    assert saved["employee_email_masked"] == ""
    assert saved["email_pending"] is True
    assert saved["source_contract_id"] in ("", None)
    assert _payroll(db)[saved["id"]]["email_pending"] is True


@pytest.mark.parametrize("name", ["", "   ", None])
def test_save_payroll_without_email_and_name_is_400(db, name):
    with pytest.raises(svc.HTTPException) as exc:
        svc.save_payroll({"employee_name": name, "business_id": "biz-sungshin", "payroll_month": "2026-09"}, ADMIN)
    assert exc.value.status_code == 400
    assert exc.value.detail == "이메일이 없으면 직원 이름이 필요합니다"
    assert db.ledgers["payroll_statements"] == []


def test_save_payroll_with_email_is_not_pending(db):
    saved = svc.save_payroll({"employee_email": "Yang@Example.com", "business_id": "biz-sungshin"}, ADMIN)
    assert saved["employee_email"] == "yang@example.com"
    assert saved["email_pending"] is False


# --- 직원 화면 -------------------------------------------------------------------------
@pytest.mark.parametrize("stored", ["", None])
def test_non_admin_never_sees_rows_without_email(db, stored):
    db.ledgers["payroll_statements"] += [
        _statement("s-null", "양재혁", email=""),
        _statement("s-mine", "양재혁", email=EMP_EMAIL, month="2026-08"),
        _statement("s-other", "김철수", email="other@example.com"),
    ]
    db.ledgers["payroll_statements"][0]["employee_email"] = stored
    own = _user(EMPLOYER, "member", email=EMP_EMAIL, user_id="user-yang")
    assert [r["id"] for r in svc.list_payroll(own)] == ["s-mine"]
    assert svc.list_payroll({**own, "email": ""}) == []
    assert svc.list_payroll({**own, "email": None}) == []
    stranger = _user(EMPLOYER, "member", email="stranger@example.com", user_id="user-stranger")
    assert svc.list_payroll(stranger) == []


def test_admin_still_sees_rows_without_email_flagged_pending(db):
    db.ledgers["payroll_statements"].append(_statement("s-null", "양재혁", email=""))
    rows = svc.list_payroll(ADMIN)
    assert [r["id"] for r in rows] == ["s-null"]


def test_filter_user_never_matches_empty_to_empty():
    rows = [{"id": "a", "employee_email": ""}, {"id": "b"}, {"id": "c", "employee_email": None}]
    assert svc._filter_user(rows, {"email": "", "tenant_role": "member"}, "employee_email") == []


# --- 교부 ------------------------------------------------------------------------------
@pytest.mark.parametrize("email", ["", None, "   "])
def test_delivery_is_refused_without_email_even_when_confirmed(email):
    with pytest.raises(svc.HTTPException) as exc:
        svc.ensure_payroll_deliverable({"employee_email": email, "status": "confirmed"})
    assert exc.value.status_code == 409
    assert exc.value.detail == "직원 계정 연결 전에는 교부할 수 없습니다"


def test_delivery_allowed_with_email():
    svc.ensure_payroll_deliverable({"employee_email": EMP_EMAIL, "status": "confirmed"})


def test_confirmed_status_itself_is_not_blocked_without_email(db):
    saved = svc.save_payroll(
        {"employee_name": "양재혁", "business_id": "biz-sungshin", "status": "confirmed", "payroll_month": "2026-09"}, ADMIN
    )
    assert saved["status"] == "confirmed"
    assert saved["email_pending"] is True


# --- 집계 ------------------------------------------------------------------------------
def test_aggregation_never_matches_none_to_none(db):
    approved = {
        "id": "emp-noemail", "status": "approved", "email": "", "name": "이메일없음", "branch": "성신여대점",
        "business_id": "biz-sungshin", "tenant_id": EMPLOYER,
    }
    mine = {**approved, "id": "emp-yang", "email": EMP_EMAIL, "name": "양재혁"}
    db.ledgers["employee_join_requests"] += [approved, mine]
    db.ledgers.setdefault("onboarding_documents", [])
    db.ledgers.setdefault("contracts", [])
    db.ledgers["payroll_statements"].append(_statement("s-null", "양재혁", email=""))
    by_id = {e["id"]: e for e in svc.list_approved_employees(ADMIN)}
    assert by_id["emp-noemail"]["payroll_statement_count"] == 0
    assert by_id["emp-noemail"]["needs_payroll"] is True
    assert by_id["emp-yang"]["payroll_statement_count"] == 0
    assert by_id["emp-yang"]["needs_payroll"] is True


# --- 초대 수락 후 연결 -------------------------------------------------------------------
@pytest.mark.parametrize("stored", ["", None])
def test_linked_row_becomes_visible_to_employee(db, stored):
    db.ledgers["payroll_statements"].append(_statement("s-sep", "양재혁"))
    db.ledgers["payroll_statements"][0]["employee_email"] = stored
    db.ledgers["payroll_statements"][0]["email_pending"] = True
    invite = _invite(email=EMP_EMAIL)
    own = _user(EMPLOYER, "member", email=EMP_EMAIL, user_id="user-yang")
    assert svc.list_payroll(own) == []
    result = svc.accept_invite({"token": invite["token"]}, EMPLOYEE)
    assert result["payroll_link"]["linked_ids"] == ["s-sep"]
    assert _payroll(db)["s-sep"]["email_pending"] is False
    assert [r["id"] for r in svc.list_payroll(own)] == ["s-sep"]
    svc.ensure_payroll_deliverable(_payroll(db)["s-sep"])


# --- migration SQL 정적 검사 -------------------------------------------------------------
def test_up_migration_drops_not_null_and_normalizes():
    assert "ALTER COLUMN employee_email DROP NOT NULL" in UP
    assert "SET employee_email = NULL WHERE btrim(employee_email) = ''" in UP
    assert "NOT VALID" in UP and "VALIDATE CONSTRAINT ck_yf_payroll_email_normalized" in UP
    assert "employee_email IS NULL OR (employee_email = lower(btrim(employee_email)) AND employee_email <> '')" in UP
    assert "CREATE INDEX IF NOT EXISTS idx_yf_payroll_email_pending" in UP
    assert "WHERE employee_email IS NULL AND deleted_at IS NULL" in UP
    assert UP.index("NOT VALID") < UP.index("VALIDATE CONSTRAINT")


def test_up_migration_has_no_destructive_statement():
    upper = " ".join(UP.upper().split())
    for bad in ("DROP TABLE", "DROP COLUMN", "TRUNCATE", "DELETE FROM"):
        assert bad not in upper


def test_down_migration_refuses_while_null_rows_remain():
    assert "RAISE EXCEPTION" in DOWN
    assert DOWN.index("RAISE EXCEPTION") < DOWN.index("ALTER COLUMN employee_email SET NOT NULL")
    assert "employee_email IS NULL AND deleted_at IS NULL" in DOWN
    assert DOWN.index("DROP CONSTRAINT IF EXISTS ck_yf_payroll_email_normalized") < DOWN.index("ALTER COLUMN employee_email SET NOT NULL")
    assert "DROP INDEX IF EXISTS idx_yf_payroll_email_pending" in DOWN
    assert "DELETE FROM" not in DOWN.upper()


def test_migration_is_not_in_pre_ledger_baseline():
    baseline = (ROOT / "scripts/migrations_auto_apply_baseline.txt").read_text(encoding="utf-8")
    assert "20261006_obys_payroll_email_nullable" not in baseline
