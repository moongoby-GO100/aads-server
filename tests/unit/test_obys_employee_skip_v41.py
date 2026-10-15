"""ACCT-OBYS-EMPLOYEE-SKIP-V41-REDIRECT-20261015 — 직원은 로그인 후 관리자 V4.1 로 가지 않는다.

index.html 의 shouldOpenV41App 은 직원 계정이면 false 를 돌려 기존 화면(onboarding: 내 계약서·서류)에 머문다.
관리자 미리보기(employeePreview)는 관리자이므로 직원으로 치지 않는다. 동작 검증은 함수 원문을 뽑아 node 로 실행한다.
"""
import re
import shutil
import subprocess
from pathlib import Path

import pytest

INDEX = Path(__file__).resolve().parents[2] / "app" / "static" / "apps" / "obys" / "index.html"
HTML = INDEX.read_text(encoding="utf-8")
NODE = shutil.which("node")


def _function_source(name: str) -> str:
    found = re.search(rf"^    (?:async )?function {name}\(", HTML, re.M)
    assert found, name
    start = found.start()
    open_at = HTML.index(") {", start) + 2
    depth = 0
    for pos in range(open_at, len(HTML)):
        char = HTML[pos]
        if char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return HTML[start : pos + 1]
    raise AssertionError(name)


class TestStaticWiring:
    def test_employee_branch_precedes_final_return(self):
        body = _function_source("shouldOpenV41App")
        employee = body.index("isEmployeeAccount()")
        final = body.rindex("return hasServerAuth();")
        assert employee < final
        assert "return false" in body[employee - 30 : employee + 60]

    def test_preview_is_not_counted_as_employee_account(self):
        account = _function_source("isEmployeeAccount")
        assert "employeePreview" not in account
        experience = _function_source("isEmployeeExperience")
        assert "employeePreview" in experience and "isEmployeeAccount()" in experience
        # 판정 로직이 두 벌이 아니다.
        assert "employee_pending" not in experience

    def test_existing_exceptions_are_kept(self):
        body = _function_source("shouldOpenV41App")
        for needle in (
            'params.get("legacy") === "1"',
            "readPostLoginRedirectFromUrl()",
            'params.has("yf_contract_token")',
            'params.has("yf_invite_token")',
            'params.has("invite_token")',
            'hash.startsWith("invite")',
            "pendingPhoneInviteToken",
        ):
            assert needle in body, needle

    def test_both_call_sites_use_verified_check(self):
        assert "if (await shouldOpenV41AppVerified() && openV41App()) return;" in HTML
        assert "!rememberedRecipeRedirect && await shouldOpenV41AppVerified() && openV41App()" in HTML
        assert len(re.findall(r"shouldOpenV41App\(\) && openV41App\(\)", HTML)) == 0

    def test_login_payload_has_no_role_so_session_is_verified_first(self):
        verified = _function_source("shouldOpenV41AppVerified")
        assert "permissions_verified" in verified and "refreshFinanceSession()" in verified
        refresh = _function_source("refreshFinanceSession")
        assert "payload.permissions_verified = true" in refresh
        assert refresh.index("permissions_verified") < refresh.index("saveAuthSession(payload)")

    def test_saved_admin_view_is_filtered_for_employee(self):
        set_view = _function_source("setView")
        assert "allowedViews()" in set_view
        assert "permitted.includes(view) ? view : defaultView()" in set_view
        assert "employeeViewNames" in _function_source("allowedViews")
        assert 'isEmployeeExperience() ? "onboarding" : "salesLedger"' in _function_source("defaultView")


NODE_HARNESS = r"""
const assert = require('node:assert/strict');
const sources = JSON.parse(process.env.SOURCES);
const cases = JSON.parse(process.env.CASES);
const out = [];
for (const c of cases) {
  const ctx = {
    window: { location: { search: c.search || '', hash: c.hash || '' } },
    URLSearchParams, String, Boolean, Array,
    pendingPhoneInviteToken: c.invite || '',
    employeePreview: c.preview ? { email: 'e@x.kr' } : null,
    authSession: { user: { email: 'a@x.kr' }, permissions: c.permissions || {} },
    fallbackPermissions: { role: 'guest' },
    employeeViewNames: ['attendance', 'onboarding', 'contracts', 'payroll'],
    appViewNames: ['salesLedger', 'onboarding', 'contracts', 'payroll', 'attendance', 'entries'],
    hasServerAuth: () => c.auth !== false,
    readPostLoginRedirectFromUrl: () => '',
    loadEmployeeSelfSignupMap: () => ({}),
    employeePendingPermissions: () => ({ role: 'employee_pending' }),
  };
  const vm = require('node:vm');
  vm.createContext(ctx);
  vm.runInContext(sources.join('\n') + '\nthis.result = shouldOpenV41App();' +
    '\nthis.first = savedView => { const v = savedView || defaultView(); return allowedViews().includes(v) ? v : defaultView(); };', ctx);
  out.push([c.name, ctx.result, ctx.first(c.saved)]);
}
console.log(JSON.stringify(out));
"""


@pytest.mark.skipif(NODE is None, reason="node 미설치 런타임")
def test_shouldOpenV41App_return_values_in_node(tmp_path):
    import json
    import os

    names = ["authPermissions", "isEmployeeAccount", "isEmployeeExperience", "allowedViews", "defaultView", "shouldOpenV41App"]
    sources = [_function_source(n) for n in names]
    cases = [
        {"name": "admin", "permissions": {"role": "owner"}, "saved": "salesLedger"},
        {"name": "employee", "permissions": {"role": "employee"}, "saved": "salesLedger"},
        {"name": "employee_pending", "permissions": {"role": "employee_pending"}},
        {"name": "status_only", "permissions": {"role": "member", "employee_request_status": "approved"}},
        {"name": "admin_preview", "permissions": {"role": "owner"}, "preview": True},
        {"name": "signed_out", "permissions": {}, "auth": False},
        {"name": "employee_invite_link", "permissions": {"role": "employee"}, "search": "?yf_invite_token=abc"},
        {"name": "admin_legacy", "permissions": {"role": "owner"}, "search": "?legacy=1&view=legacy"},
        {"name": "admin_contract_token", "permissions": {"role": "owner"}, "search": "?yf_contract_token=t"},
        {"name": "admin_invite_hash", "permissions": {"role": "owner"}, "hash": "#invite-1"},
    ]
    script = tmp_path / "harness.js"
    script.write_text(NODE_HARNESS, encoding="utf-8")
    result = subprocess.run(
        [NODE, str(script)],
        text=True,
        capture_output=True,
        timeout=30,
        env={"PATH": "/usr/bin:/bin:/usr/local/bin", "SOURCES": json.dumps(sources), "CASES": json.dumps(cases)},
    )
    assert result.returncode == 0, result.stdout + result.stderr
    got = {name: (opens, first) for name, opens, first in json.loads(result.stdout)}
    assert got["admin"][0] is True
    assert got["employee"] == (False, "onboarding")  # 저장된 관리자 뷰는 무시된다
    assert got["employee_pending"][0] is False
    assert got["status_only"][0] is False
    assert got["admin_preview"][0] is True  # 미리보기는 관리자 진입 동작을 바꾸지 않는다
    assert got["signed_out"][0] is False
    assert got["employee_invite_link"][0] is False
    assert got["admin_legacy"][0] is False
    assert got["admin_contract_token"][0] is False
    assert got["admin_invite_hash"][0] is False
