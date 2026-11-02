"""New-tenant payroll API access preserves role, tenant and legacy boundaries."""
import pytest

from app.core.obys_tenant import _is_tenant_scoped_path
from tests.unit.test_obys_tenant_owner_contracts_gate import (
    _client, _employee, OWNER_A, OWNER_B, MEMBER_A,
    TENANT_A, TENANT_B, BIZ_A, BIZ_B, BASE,
)

pytest_plugins = ["tests.unit.test_obys_tenant_owner_contracts_gate"]

PATH = BASE + "/payroll"
ROW_ID = "b2c3d4e5-0000-4000-8000-000000000001"


@pytest.mark.parametrize("method,path", [
    ("GET", PATH), ("POST", PATH), ("GET", PATH+"/defaults"),
    ("POST", PATH+"/draft-from-attendance"),
    *[("GET", PATH+"/"+ROW_ID+"/"+action) for action in ("versions", "pdf")],
    *[("POST", PATH+"/"+ROW_ID+"/"+action) for action in ("confirm", "pay", "deliver")],
])
def test_payroll_ui_routes_are_explicitly_scoped(method, path):
    assert _is_tenant_scoped_path(path, method)


@pytest.mark.parametrize("method,path", [
    ("DELETE", PATH+"/"+ROW_ID), ("PUT", PATH), ("POST", PATH+"/defaults"),
    ("GET", PATH+"/draft-from-attendance"), ("POST", PATH+"/delivery-consent"),
    ("GET", PATH+"-extra"), ("GET", PATH+"/defaults/pdf"),
    ("POST", PATH+"/"+ROW_ID+"/confirm/extra"), ("GET", BASE+"/settings"),
])
def test_unreviewed_and_similar_routes_remain_blocked(method, path):
    assert not _is_tenant_scoped_path(path, method)


def payload(business=BIZ_A, employee="staff-a@example.com"):
    return dict(business_id=business,employee_email=employee,payroll_month="2026-11",
                gross_pay=100000,taxable_pay=100000,non_tax_meal_allowance=0,
                tax_withholding=1000,insurance_deduction=2000,other_deduction=0)


def test_nonlegacy_owner_can_save_and_read_only_own_tenant_payroll(db):
    db.rows["employee_join_requests"] = [
        _employee("employee-a", TENANT_A, "staff-a@example.com", BIZ_A),
        _employee("employee-b", TENANT_B, "staff-b@example.com", BIZ_B),
    ]
    with _client(OWNER_A) as owner:
        saved = owner.post(PATH, json=payload())
        assert saved.status_code == 200, saved.text
        statement = saved.json()["statement"]
        assert statement["tenant_id"] == TENANT_A
        assert statement["net_pay"] == 97000
        assert owner.get(PATH).json()["statements"][0]["id"] == statement["id"]
        assert owner.get(PATH+"/"+statement["id"]+"/versions").status_code == 200
        # Explicitly opening the payroll paths must not open legacy settings.
        assert owner.get(BASE+"/settings").status_code == 403
    with _client(OWNER_B) as other:
        assert other.get(PATH).json()["statements"] == []
        assert other.get(PATH+"/"+statement["id"]+"/versions").status_code == 404
        collision = {**payload(BIZ_B,"staff-b@example.com"), "id":statement["id"]}
        assert other.post(PATH,json=collision).status_code == 404
    assert db.rows["payroll_statements"][0]["tenant_id"] == TENANT_A


def test_member_cannot_create_or_confirm_payroll_even_after_path_gate_opens(db):
    db.rows["employee_join_requests"] = [_employee("employee-a",TENANT_A,MEMBER_A["email"],BIZ_A)]
    with _client(MEMBER_A) as member:
        assert member.post(PATH,json=payload(employee=MEMBER_A["email"])).status_code == 403
        assert member.post(PATH+"/"+ROW_ID+"/confirm").status_code == 403
        assert member.get(PATH).json()["statements"] == []
