"""ACCT-OBYS-PAYROLL-EMPLOYEE-HIDE-DRAFT-20261016 — 직원 급여 조회는 확정 이상만 반환한다. 운영 DB 미접촉."""
from __future__ import annotations

import pytest

from tests.unit.test_obys_invite_email import (
    ADMIN,
    EMP_EMAIL,
    EMPLOYER,
    _statement,
    _user,
    svc,
)
from tests.unit.test_obys_invite_email import db as invite_db  # noqa: F401  (pytest fixture)


@pytest.fixture(name="db")
def _db(request):
    return request.getfixturevalue("invite_db")


OWN = _user(EMPLOYER, "member", email=EMP_EMAIL, user_id="user-yang")


def _seed(db):
    db.ledgers["payroll_statements"] += [
        _statement("s-draft", "양재혁", email=EMP_EMAIL, month="2026-06", status="draft"),
        _statement("s-blank", "양재혁", email=EMP_EMAIL, month="2026-07", status=""),
        _statement("s-none", "양재혁", email=EMP_EMAIL, month="2026-08", status=None),
        _statement("s-cancel", "양재혁", email=EMP_EMAIL, month="2026-09", status="cancelled"),
        _statement("s-weird", "양재혁", email=EMP_EMAIL, month="2026-09", status="unknown-state"),
        _statement("s-conf", "양재혁", email=EMP_EMAIL, month="2026-10", status="confirmed"),
        _statement("s-paid", "양재혁", email=EMP_EMAIL, month="2026-11", status="Paid "),
        _statement("s-other-conf", "김철수", email="other@example.com", status="confirmed"),
    ]


def test_employee_sees_only_confirmed_or_later_own_rows(db):
    _seed(db)
    assert {r["id"] for r in svc.list_payroll(OWN)} == {"s-conf", "s-paid"}


def test_employee_with_only_draft_rows_gets_empty_list(db):
    db.ledgers["payroll_statements"].append(_statement("s-draft", "양재혁", email=EMP_EMAIL, status="draft"))
    assert svc.list_payroll(OWN) == []


def test_admin_still_sees_every_status(db):
    _seed(db)
    assert {r["id"] for r in svc.list_payroll(ADMIN)} == {
        "s-draft", "s-blank", "s-none", "s-cancel", "s-weird", "s-conf", "s-paid", "s-other-conf",
    }


def test_other_employee_rows_stay_hidden(db):
    _seed(db)
    assert "s-other-conf" not in {r["id"] for r in svc.list_payroll(OWN)}


def test_visible_status_set_is_allow_list():
    assert svc.PAYROLL_EMPLOYEE_VISIBLE_STATUSES == frozenset({"confirmed", "paid"})
