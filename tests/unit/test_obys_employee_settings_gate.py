"""OBYS-M1-EMPLOYEE-SETTINGS-GATE-20261022 — 직원 화면의 레거시 /settings 403 처리.

GET /settings 는 테넌트 구분 없는 전역 JSON(사업자·계좌·직원)을 돌려주므로 게이트에 열지 않는다.
대신 index.html loadServerSettings 가 직원 계정이면 호출하지 않고, 설정 권한 없는 계정의 403 은 조용히 넘긴다.
"""
import importlib.util
import json
import re
import shutil
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from app.core.obys_tenant import (
    _DEFAULT_LEGACY_TENANT_IDS,
    _TENANT_SCOPED_PREFIXES,
    _is_tenant_scoped_path,
    require_legacy_obys_access,
)

ROOT = Path(__file__).resolve().parents[2]
HTML = (ROOT / "app" / "static" / "apps" / "obys" / "index.html").read_text(encoding="utf-8")
NODE = shutil.which("node")
BASE = "/api/v1/yeoljeong-finance"
NEW_TENANT = "00000000-0000-0000-0000-000000000123"


def _request(path: str, method: str = "GET") -> SimpleNamespace:
    return SimpleNamespace(url=SimpleNamespace(path=path), method=method)


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    monkeypatch.delenv("OBYS_LEGACY_TENANT_IDS", raising=False)


class TestBackendSettingsStaysClosed:
    @pytest.mark.parametrize("user", [
        {"tenant_id": NEW_TENANT, "is_admin": False, "role": "employee"},
        {"tenant_id": NEW_TENANT, "is_admin": True, "role": "owner"},
    ])
    @pytest.mark.parametrize("method", ["GET", "PUT"])
    async def test_new_tenant_cannot_read_or_write_global_settings(self, user, method):
        with pytest.raises(HTTPException) as exc_info:
            await require_legacy_obys_access(_request(f"{BASE}/settings", method), user)
        assert exc_info.value.status_code == 403

    @pytest.mark.parametrize("method", ["GET", "PUT"])
    async def test_legacy_tenant_settings_unchanged(self, method):
        user = {"tenant_id": _DEFAULT_LEGACY_TENANT_IDS[0], "is_admin": False}
        assert await require_legacy_obys_access(_request(f"{BASE}/settings", method), user) is user

    def test_settings_not_in_scoped_prefixes_or_whitelists(self):
        assert not any(f"{BASE}/settings".startswith(prefix) for prefix in _TENANT_SCOPED_PREFIXES)
        for method in ("GET", "PUT", "POST"):
            assert _is_tenant_scoped_path(f"{BASE}/settings", method) is False
            assert _is_tenant_scoped_path(f"{BASE}/settings/anything", method) is False


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
    def test_employee_guard_precedes_settings_request(self):
        body = _function_source("loadServerSettings")
        assert body.index("isEmployeeAccount()") < body.index('financeApi("/settings")')
        assert "Number(err?.status) === 403 && !canManageSettings()" in body

    def test_callers_and_function_names_preserved(self):
        for name in ("loadServerSettings", "persistSettingsToServer", "hasMeaningfulSettings", "reloadDataAfterTenantSwitch"):
            assert re.search(rf"function {name}\(", HTML), name
        assert HTML.count("기초설정 불러오기 실패") == 4


NODE_HARNESS = r"""
const vm = require('node:vm');
const sources = JSON.parse(process.env.SOURCES);
const cases = JSON.parse(process.env.CASES);
(async () => {
  const out = [];
  for (const c of cases) {
    const calls = [];
    const ctx = {
      Number, Array, Boolean, Error,
      state: { settings: { businesses: [{ id: 'local' }] } },
      hasServerAuth: () => c.auth !== false,
      isEmployeeAccount: () => Boolean(c.employee),
      canManageSettings: () => Boolean(c.manager),
      mergeSettings: s => ({ merged: true, ...s }),
      saveState: () => calls.push('save'), initSelects: () => {}, renderAll: () => {},
      financeApi: async path => {
        calls.push(path);
        if (c.status) { const e = new Error('blocked'); e.status = c.status; throw e; }
        return { settings: c.payload };
      },
    };
    vm.createContext(ctx);
    vm.runInContext(sources.join('\n') + '\nthis.run = loadServerSettings;', ctx);
    let result = null, threw = null;
    try { result = await ctx.run(); } catch (e) { threw = e.status || e.message; }
    out.push([c.name, calls, threw, result === null, Boolean(ctx.state.settings.merged)]);
  }
  console.log(JSON.stringify(out));
})();
"""


@pytest.mark.skipif(NODE is None, reason="node 미설치 런타임")
def test_loadServerSettings_behaviour_in_node(tmp_path):
    sources = [_function_source("loadServerSettings"), _function_source("hasMeaningfulSettings")]
    cases = [
        {"name": "employee", "employee": True, "status": 403},
        {"name": "employee_ok", "employee": True, "payload": {"businesses": [{"id": "x"}]}},
        {"name": "member_403", "status": 403},
        {"name": "manager_403", "manager": True, "status": 403},
        {"name": "member_500", "status": 500},
        {"name": "manager_ok", "manager": True, "payload": {"businesses": [{"id": "x"}]}},
        {"name": "signed_out", "auth": False},
    ]
    script = tmp_path / "harness.js"
    script.write_text(NODE_HARNESS, encoding="utf-8")
    result = subprocess.run(
        [NODE, str(script)], text=True, capture_output=True, timeout=30,
        env={"PATH": "/usr/bin:/bin:/usr/local/bin", "SOURCES": json.dumps(sources), "CASES": json.dumps(cases)},
    )
    assert result.returncode == 0, result.stdout + result.stderr
    got = {name: (calls, threw, is_null, merged) for name, calls, threw, is_null, merged in json.loads(result.stdout)}
    assert got["employee"] == ([], None, True, False)  # 호출 자체를 하지 않는다
    assert got["employee_ok"] == ([], None, True, False)  # 전역 설정을 상태에 합치지 않는다
    assert got["member_403"] == (["/settings"], None, True, False)  # 토스트 없이 빈 설정
    assert got["manager_403"] == (["/settings"], 403, True, False)  # 관리자 동작은 종전 그대로(오류 전파)
    assert got["member_500"] == (["/settings"], 500, True, False)
    assert got["manager_ok"][0] == ["/settings", "save"] and got["manager_ok"][3] is True
    assert got["signed_out"] == ([], None, True, False)


def _load_verifier():
    spec = importlib.util.spec_from_file_location("verify_obys_m1", ROOT / "scripts" / "verify_obys_m1_employee_entry_e2e.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _staff_result(portal=True, onboarding=False, error_text="", entries=("홈", "계약서", "급여명세", "근태", "입사서류")):
    session = {"http": 200, "role": "employee", "employee_request_status": "approved", "can_manage_settings": False, "is_admin": False}
    view = {
        "path": "/static/apps/obys/index.html", "title": "오비서", "headings": [],
        "onboarding_visible": onboarding, "portal_visible": portal, "portal_entries": list(entries),
        "auth_gate_visible": False, "recovery_visible": False,
    }
    return {
        "login_submitted": True, "login_ok": True, "login_http": 200, "error_text": error_text,
        "session_responses": [{**session, "viewport": "pc"}, {**session, "viewport": "mobile"}],
        "pc": view, "mobile": view,
    }


class TestVerifierRecognisesEmployeePortal:
    def test_portal_home_with_contract_and_document_entries_passes(self):
        verdict, basis = _load_verifier().judge("staff", _staff_result())
        assert verdict == "PASS", basis

    def test_mobile_tab_label_seoryu_is_enough(self):
        verdict, basis = _load_verifier().judge("staff", _staff_result(entries=("홈", "계약서", "급여", "근태", "서류")))
        assert verdict == "PASS", basis

    def test_settings_failure_toast_fails_employee(self):
        text = "기초설정 불러오기 실패: 이 계정에는 오비서 레거시 데이터 접근 권한이 없습니다."
        verdict, basis = _load_verifier().judge("staff", _staff_result(error_text=text))
        assert verdict == "FAIL" and "기초설정" in basis

    @pytest.mark.parametrize("viewport", ["pc", "mobile"])
    def test_transient_settings_failure_fails_employee(self, viewport):
        result = _staff_result()
        result[viewport] = {**result[viewport], "settings_failure_seen": True}
        verdict, basis = _load_verifier().judge("staff", result)
        assert verdict == "FAIL" and "기초설정" in basis

    @pytest.mark.skipif(NODE is None, reason="node 미설치 런타임")
    def test_observer_remembers_removed_toast_without_storing_text(self):
        observer = _load_verifier().SETTINGS_OBSERVER
        harness = """
const assert = require('node:assert/strict');
global.window = {};
global.document = {getElementById: () => null};
const target = {nodeType: 1, closest: () => target};
let notify;
global.MutationObserver = class {
    constructor(callback) { notify = callback; }
    observe() {}
};
""" + observer + """
assert.equal(window.__obysSettingsFailureSeen, false);
notify([{target, oldValue: null, addedNodes: [{textContent: '정상 안내'}]}]);
assert.equal(window.__obysSettingsFailureSeen, false);
notify([{target: {nodeType: 1, closest: () => null}, oldValue: null,
         addedNodes: [{textContent: '기초설정 불러오기 실패: script source'}]}]);
assert.equal(window.__obysSettingsFailureSeen, false);
notify([{target, oldValue: null, addedNodes: [{textContent: '기초설정 불러오기 실패: secret'}]}]);
notify([{target, oldValue: null, addedNodes: [{textContent: ''}]}]);
assert.deepEqual(window, {__obysSettingsFailureSeen: true});
"""
        proc = subprocess.run([NODE, "-e", harness], capture_output=True, text=True, timeout=10)
        assert proc.returncode == 0, proc.stderr

    def test_no_portal_and_no_onboarding_still_fails(self):
        verdict, _ = _load_verifier().judge("staff", _staff_result(portal=False, entries=()))
        assert verdict == "FAIL"

    def test_portal_without_document_entry_fails(self):
        verdict, _ = _load_verifier().judge("staff", _staff_result(entries=("홈", "계약서")))
        assert verdict == "FAIL"
