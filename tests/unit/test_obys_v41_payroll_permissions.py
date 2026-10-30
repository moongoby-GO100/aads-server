"""직원 급여 HTTP 조회의 본인·월·권한 경계. 파일 fixture만 사용한다."""
import pytest

from tests.unit.test_obys_payroll_state_machine import (
    ADMIN, EMPLOYEE, OTHER_EMPLOYEE, _client, isolated, statement, svc,  # noqa: F401  (isolated: pytest fixture)
)


@pytest.mark.usefixtures("isolated")
def test_employee_month_and_forged_identity_cannot_expose_other_payroll():
    own = svc.save_payroll(statement(), ADMIN)
    svc.confirm_payroll(own["id"], ADMIN)
    other = svc.save_payroll(statement(employee_email=OTHER_EMPLOYEE["email"]), ADMIN)
    svc.confirm_payroll(other["id"], ADMIN)
    client = _client(EMPLOYEE)
    response = client.get("/yeoljeong-finance/payroll", params={
        "payroll_month": "2026-10", "employee_email": OTHER_EMPLOYEE["email"], "include_history": True,
    })
    assert response.status_code == 200
    assert [r["id"] for r in response.json()["statements"]] == [own["id"]]
    assert client.get("/yeoljeong-finance/payroll", params={"payroll_month": "2026-09"}).json()["statements"] == []
    assert client.get(f"/yeoljeong-finance/payroll/{other['id']}/versions").status_code == 403
    assert client.post(f"/yeoljeong-finance/payroll/{own['id']}/confirm").status_code == 403
