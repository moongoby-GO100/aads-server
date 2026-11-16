"""오비서 로그인은 레거시 화면(index.html)을 거치지 않고 새 화면 login.html 로 바로 들어간다.

2026-11-16: /login 이 index.html 을 열고, 로그인 뒤 shouldOpenV41App() 이 다시 admin.html 로 넘겨
'레거시 → 새 화면' 깜빡임이 생겼다.
"""
from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app import yeoljeong_main

ROOT = Path(__file__).resolve().parents[2]
OBYS = ROOT / "app" / "static" / "apps" / "obys"
LOGIN = "/static/apps/obys/login.html"
client = TestClient(yeoljeong_main.app, follow_redirects=False)


def test_root_and_login_redirect_to_new_screen():
    for path in ("/", "/login"):
        r = client.get(path)
        assert r.status_code in (302, 307), path
        assert r.headers["location"] == LOGIN, path


def test_login_redirect_keeps_query_and_needs_no_token():
    r = client.get("/login?redirect=/unni-naengmyeon/recipes")
    assert r.status_code == 302
    assert r.headers["location"] == f"{LOGIN}?redirect=/unni-naengmyeon/recipes"


def test_recipes_without_token_goes_to_new_login():
    r = client.get("/unni-naengmyeon/recipes")
    assert r.status_code == 302
    assert r.headers["location"].startswith(LOGIN + "?redirect=")


def test_login_page_is_served_and_reuses_recovery_and_existing_api():
    r = client.get(LOGIN)
    assert r.status_code == 200
    html = r.text
    assert html.count('type="password"') == 1
    assert "/modules/account-recovery.js" in html and "/modules/login-route.js" in html
    assert 'data-ar-open="reset"' in html and 'data-ar-open="find-id"' in html
    assert 'name="keep"' in html
    assert "/api/v1/auth/login" in html and "/api/v1/yeoljeong-finance/session" in html
    assert "index.html" not in html


# 로그인 링크로 쓰이는 index.html 참조는 없어야 한다(초대·계약 토큰·직원 미리보기 링크는 로그인이 아니므로 제외).
_LEGACY_LOGIN = re.compile(r"apps/obys/index\.html(?:#login)?[\"'`)\s>]")
_FILES = [
    "admin.html", "clock.html", "attendance-admin.html",
    "modules/attendance-corrections.js", "modules/contract-editor-v41.js",
]


@pytest.mark.parametrize("name", _FILES)
def test_no_legacy_login_link(name):
    text = (OBYS / name).read_text(encoding="utf-8")
    hits = [m.group(0) for m in _LEGACY_LOGIN.finditer(text)]
    assert hits == [], f"{name}: {hits}"


def test_session_expiry_and_logout_go_to_login_html():
    admin = (OBYS / "admin.html").read_text(encoding="utf-8")
    assert re.search(r"function clearAuthAndOpenLogin\(\).*?location\.replace\(\"/static/apps/obys/login\.html\"\)", admin)
    clock = (OBYS / "clock.html").read_text(encoding="utf-8")
    assert 'window.location.replace("/static/apps/obys/login.html")' in clock
    assert "login.html" in (OBYS / "modules/attendance-corrections.js").read_text(encoding="utf-8")
    assert "employee.html" in admin  # 직원이 admin.html 에 들어오면 직원 새 화면으로


def test_apache_login_rewrites_target_new_screen():
    conf = (ROOT / "config/apache/fb-cafe24.conf").read_text(encoding="utf-8")
    assert "RewriteRule ^/login$ /static/apps/obys/index.html" not in conf
    assert conf.count("RewriteRule ^/login$ /static/apps/obys/login.html [R=302,QSA,L]") == 2
    assert conf.count("RewriteRule ^/$ /static/apps/obys/login.html [R=302,L]") == 2


@pytest.mark.skipif(shutil.which("node") is None, reason="node 없음")
def test_role_routing_and_redirect_safety():
    script = r"""
const R = require(process.argv[1]);
const emp = {permissions:{role:"employee"}}, pend = {permissions:{role:"employee_pending"}};
const mgr = {permissions:{role:"owner",can_manage_onboarding:true}};
const empMgr = {permissions:{role:"employee",can_manage_onboarding:true}};
const legacy = {legacy_access:true, permissions:{role:"employee"}};
const legacyPend = {legacy_access:true, permissions:{role:"employee_pending"}};
const legacyOwner = {legacy_access:true, permissions:{role:"owner",can_manage_onboarding:true}};
const legacyEmpMgr = {legacy_access:true, permissions:{role:"employee",can_manage_onboarding:true}};
const nonLegacyEmp = {legacy_access:false, permissions:{role:"employee"}};
const out = {
  admin: R.destinationAfterLogin(mgr, ""), none: R.destinationAfterLogin(null, ""), noneAdmin: R.destinationAfterLogin(null, "", true),
  noneRecipe: R.destinationAfterLogin(null, "/unni-naengmyeon/recipes", false),
  emp: R.destinationAfterLogin(emp, ""), pend: R.destinationAfterLogin(pend, ""),
  empMgr: R.destinationAfterLogin(empMgr, ""), legacy: R.destinationAfterLogin(legacy, ""),
  legacyPend: R.destinationAfterLogin(legacyPend, ""), legacyOwner: R.destinationAfterLogin(legacyOwner, ""),
  legacyEmpMgr: R.destinationAfterLogin(legacyEmpMgr, ""), nonLegacyEmp: R.destinationAfterLogin(nonLegacyEmp, ""),
  recipe: R.destinationAfterLogin(emp, "/unni-naengmyeon/recipes?x=1"),
  evil: ["https://evil.com", "//evil.com", "/unni-naengmyeon/recipes\\x", "/unni-naengmyeon/recipes\r\nX:1",
         "javascript:alert(1)", "/unni-naengmyeon/recipesX", "/admin"].map(v => R.safeRedirectPath(v)),
  fromSearch: R.redirectFromSearch("?next=/unni-naengmyeon/recipes%23a"),
  evilSearch: R.redirectFromSearch("?redirect=https://evil.com"),
  url: R.loginUrl("/unni-naengmyeon/recipes"), urlBad: R.loginUrl("https://evil.com"),
};
console.log(JSON.stringify(out));
"""
    res = subprocess.run(["node", "-e", script, str(OBYS / "modules/login-route.js")], capture_output=True, text=True, timeout=30)
    assert res.returncode == 0, res.stderr
    out = json.loads(res.stdout)
    assert out["admin"] == "/static/apps/obys/admin.html#home"
    # 권한 확인 실패(session 없음)는 관리자 화면이 아니라 이동 없음("")이다. 서버가 관리자로 확인한 로그인만 관리자 화면.
    assert out["none"] == "" and out["noneRecipe"] == ""
    assert out["noneAdmin"] == "/static/apps/obys/admin.html#home"
    assert out["emp"] == "/static/apps/obys/employee.html" and out["pend"] == out["emp"]
    assert out["empMgr"].endswith("admin.html#home")
    # legacy_access 는 레거시 원장 접근 테넌트 여부일 뿐 역할이 아니다 — 서버 역할(permissions)로만 판정한다.
    emp_path = "/static/apps/obys/employee.html"
    assert out["legacy"] == emp_path and out["legacyPend"] == emp_path and out["nonLegacyEmp"] == emp_path
    assert out["legacyOwner"].endswith("admin.html#home") and out["legacyEmpMgr"].endswith("admin.html#home")
    assert out["recipe"] == "/unni-naengmyeon/recipes?x=1"
    assert out["evil"] == ["", "", "", "", "", "", ""]
    assert out["fromSearch"] == "/unni-naengmyeon/recipes#a"
    assert out["evilSearch"] == ""
    assert out["url"].startswith(LOGIN + "?redirect=") and out["urlBad"] == LOGIN


def test_login_race_and_failed_session_guards():
    html = (OBYS / "login.html").read_text(encoding="utf-8")
    # 지적1: 자동 진입은 제출이 시작됐거나 저장 토큰이 바뀌었으면 결과를 버리고 clearToken 도 하지 않는다.
    auto = html[html.index("async function autoEnter"):]
    assert "startedAttempt !== attempt" in auto and "storedToken() !== token" in auto
    assert auto.index("stale) return") < auto.index("clearToken()")
    assert "++attempt" in html and "myAttempt !== attempt" in html
    # 지적3: 세션 확인 실패(null)는 관리자 화면 이동이 아니라 오류 안내.
    assert "go(null, true)" in html and "계정 권한을 확인하지 못했습니다" in html
    assert "go(payload.is_admin ? null : session)" not in html


def test_tenant_switch_respects_keep_login_off():
    admin = (OBYS / "admin.html").read_text(encoding="utf-8")
    # 지적2: 테넌트 전환 경로가 localStorage 에 토큰을 무조건 쓰지 않는다(전환 토큰 저장은 한 함수로).
    assert admin.count('localStorage.setItem("aads_token"') == 1
    assert admin.count("persistSwitchedTokenV41(result.token)") == 2
    fn = admin[admin.index("function persistSwitchedTokenV41"):]
    fn = fn[:fn.index("async function leaveForEmployeeEntryV41")]
    assert 'const keep=Boolean(localStorage.getItem("aads_token")' in fn and "if(keep)" in fn
