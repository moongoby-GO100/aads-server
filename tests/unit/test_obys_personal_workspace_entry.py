"""직원이 개인 워크스페이스 owner 토큰으로 들어와도 /session 이 직원 진입을 알려 주는지 검사한다.

2026-11-05: 직원 가입 직후 브라우저가 가입 때 받은 개인 워크스페이스 토큰을 계속 써서
/session 이 owner 로 응답했고, 사장 화면(admin.html)이 열렸다.
"""
from __future__ import annotations

import asyncio

import pytest

from app import auth as auth_module
from app.services import obys_upload_service as service

TENANT = "11111111-1111-1111-1111-111111111111"
EMPLOYER = "15055cac-0000-0000-0000-000000000000"


def _user(role: str = "owner", **extra) -> dict:
    user = {
        "user_id": "u-1",
        "email": "staff@example.com",
        "name": "직원",
        "tenant_id": TENANT,
        "current_membership": {"tenant_id": TENANT, "status": "active", "role": role},
    }
    user.update(extra)
    return user


def _session(user: dict) -> dict:
    return asyncio.run(service.session_for_current_user(user))


@pytest.fixture
def entry(monkeypatch):
    calls: dict = {"result": {"personal_workspace": False, "redirect_tenant_id": None}, "error": None, "args": None}

    async def fake_entry(user_id, tenant_id):
        calls["args"] = (user_id, tenant_id)
        if calls["error"]:
            raise calls["error"]
        return calls["result"]

    monkeypatch.setattr(auth_module, "personal_workspace_entry", fake_entry)
    return calls


@pytest.fixture
def own_request(monkeypatch):
    holder: dict = {"row": None, "error": None, "called": 0}

    async def fake_own(user):
        holder["called"] += 1
        if holder["error"]:
            raise holder["error"]
        return holder["row"]

    monkeypatch.setattr(service, "_own_employee_request_any_tenant", fake_own)
    return holder


def test_approved_employee_on_personal_workspace_gets_redirect(entry, own_request):
    entry["result"] = {"personal_workspace": True, "redirect_tenant_id": EMPLOYER}

    session = _session(_user())

    assert entry["args"] == ("u-1", TENANT)
    assert session["entry_tenant_redirect"] == {
        "tenant_id": EMPLOYER,
        "reason": "personal_workspace_default_elsewhere",
    }
    assert own_request["called"] == 0


def test_pending_employee_on_personal_workspace_becomes_employee_session(entry, own_request):
    entry["result"] = {"personal_workspace": True, "redirect_tenant_id": None}
    own_request["row"] = {"id": "req-1", "status": "pending", "tenant_id": EMPLOYER}

    session = _session(_user())

    assert session["permissions"]["role"] == "employee_pending"
    assert session["permissions"]["employee_request_status"] == "pending"
    assert session["permissions"]["can_manage_onboarding"] is False
    assert session["employee_request_tenant_id"] == EMPLOYER
    assert "entry_tenant_redirect" not in session


def test_real_company_owner_is_untouched(entry, own_request):
    entry["result"] = {"personal_workspace": False, "redirect_tenant_id": None}
    own_request["row"] = {"id": "req-1", "status": "pending", "tenant_id": EMPLOYER}

    session = _session(_user())

    assert session["permissions"]["role"] == "owner"
    assert session["permissions"]["can_manage_onboarding"] is True
    assert "entry_tenant_redirect" not in session
    assert own_request["called"] == 0


def test_personal_workspace_without_employee_request_stays_owner(entry, own_request):
    entry["result"] = {"personal_workspace": True, "redirect_tenant_id": None}

    session = _session(_user())

    assert session["permissions"]["role"] == "owner"
    assert "entry_check" not in session


def test_approved_request_without_redirect_is_not_downgraded(entry, own_request):
    entry["result"] = {"personal_workspace": True, "redirect_tenant_id": None}
    own_request["row"] = {"id": "req-1", "status": "approved", "tenant_id": EMPLOYER}

    session = _session(_user())

    assert session["permissions"]["role"] == "owner"


def test_entry_check_failure_is_reported_not_swallowed(entry, own_request):
    entry["error"] = RuntimeError("auth db down")

    session = _session(_user())

    assert session["entry_check"] == "failed"
    assert session["permissions"]["role"] == "owner"


def test_join_request_lookup_failure_is_reported(entry, own_request):
    entry["result"] = {"personal_workspace": True, "redirect_tenant_id": None}
    own_request["error"] = RuntimeError("obys db down")

    session = _session(_user())

    assert session["entry_check"] == "failed"
    assert session["permissions"]["role"] == "owner"


def test_internal_admin_skips_personal_workspace_check(entry, own_request):
    session = _session(_user(is_internal_admin=True))

    assert entry["args"] is None
    assert session["permissions"]["role"] == "owner"


def test_member_session_keeps_existing_current_tenant_lookup(entry, own_request, monkeypatch):
    async def fake_current(user, tenant_id):
        return {"id": "req-2", "status": "pending", "tenant_id": TENANT}

    monkeypatch.setattr(service, "_own_employee_request", fake_current)

    session = _session(_user(role="member"))

    assert entry["args"] is None
    assert own_request["called"] == 0
    assert session["permissions"]["role"] == "employee_pending"


def test_move_default_and_entry_share_personal_workspace_rule():
    import inspect

    move_src = inspect.getsource(auth_module._move_default_off_personal_workspace)
    entry_src = inspect.getsource(auth_module.personal_workspace_entry)
    assert "_PERSONAL_WORKSPACE_SQL" in move_src
    assert "_PERSONAL_WORKSPACE_SQL" in entry_src
    sql = auth_module._PERSONAL_WORKSPACE_SQL
    assert "t.created_by = $2::text" in sql and "o.user_id <> $2::text" in sql


def test_admin_guard_sends_employee_sessions_to_employee_screen():
    from pathlib import Path

    from tests.unit.test_obys_employee_preview_menu import _function_source
    from tests.unit.test_obys_login_entry_parallel import run_js

    admin = (Path(__file__).resolve().parents[2] / "app/static/apps/obys/admin.html").read_text()
    run_js("""const assert=require('node:assert/strict');
    let replaced=null, switched=null, failSwitch=false; const store={}, document={cookie:''};
    const location={replace(url){replaced=url}}, localStorage={setItem(k,v){store[k]=v}};
    const organizationApi=async(path,options)=>{if(failSwitch)throw Error('403');switched=[path,options.method];return {token:'employer'}};
    """ + _function_source(admin, "leaveForEmployeeEntryV41") + """
    (async()=>{
      // 승인 후: 서버가 준 시작 조직으로 토큰을 바꾸고 직원 화면으로 간다.
      assert.equal(await leaveForEmployeeEntryV41({entry_tenant_redirect:{tenant_id:'t-emp'},permissions:{role:'owner'}}),true);
      assert.deepEqual(switched,['/tenants/t-emp/switch','POST']);assert.equal(store.aads_token,'employer');
      assert.equal(replaced,'/static/apps/obys/index.html');
      // 전환 실패: 기존 화면 유지
      replaced=null;failSwitch=true;
      assert.equal(await leaveForEmployeeEntryV41({entry_tenant_redirect:{tenant_id:'t-emp'}}),false);assert.equal(replaced,null);
      // 승인 전 대기 직원: 직원 화면
      assert.equal(await leaveForEmployeeEntryV41({permissions:{role:'employee_pending',employee_request_status:'pending'}}),true);
      assert.equal(replaced,'/static/apps/obys/index.html');
      // 실제 사장·관리 권한 직원·레거시는 그대로
      replaced=null;
      assert.equal(await leaveForEmployeeEntryV41({permissions:{role:'owner',can_manage_onboarding:true}}),false);
      assert.equal(await leaveForEmployeeEntryV41({permissions:{role:'store_manager',employee_request_status:'approved',can_manage_onboarding:true}}),false);
      assert.equal(await leaveForEmployeeEntryV41({legacy_access:true,permissions:{role:'employee'}}),false);
      assert.equal(replaced,null);
    })().catch(e=>{console.error(e);process.exitCode=1});
    """)
