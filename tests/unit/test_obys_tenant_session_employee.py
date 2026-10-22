"""OBYS-M1-TENANT-SESSION-EMPLOYEE-ROLE-20261022 — 비레거시 테넌트 /session 의 직원 판정.

tenant_session_for_user 는 멤버십 role 만 돌려줘서 승인 직원(membership=member)이 관리자 V4.1 로 갔다.
/session 은 이제 본인 직원 가입요청(현재 테넌트 한정)을 덧입힌다. 가입요청이 없으면 응답이 그대로다.
"""
import asyncio
import json
import re
import shutil
import subprocess
from pathlib import Path
from uuid import UUID, uuid4

import pytest

from app.api import obys_finance
from app.services import obys_upload_service as upload
from app.services import yeoljeong_finance_service as finance

INDEX = Path(__file__).resolve().parents[2] / "app" / "static" / "apps" / "obys" / "index.html"
NODE = shutil.which("node")


def _user(role: str = "member", **extra) -> dict:
    tenant_id = str(uuid4())
    user = {
        "user_id": "u-1",
        "email": "Staff2@newtalk.kr",
        "name": "직원2",
        "tenant_id": tenant_id,
        "current_tenant": {"id": tenant_id, "name": "엠원"},
        "current_membership": {"tenant_id": tenant_id, "role": role, "status": "active"},
    }
    user.update(extra)
    return user


def _request(status: str, role: str = "employee") -> dict:
    return {"id": "jr-1", "email": "staff2@newtalk.kr", "status": status, "role": role}


@pytest.fixture
def fake_request(monkeypatch):
    calls: list[tuple[str, UUID]] = []
    holder: dict = {"row": None, "error": None}

    async def _fake(user, tenant_id):
        calls.append((str(user.get("email")), tenant_id))
        if holder["error"]:
            raise holder["error"]
        return holder["row"]

    monkeypatch.setattr(upload, "_own_employee_request", _fake)
    holder["calls"] = calls
    return holder


def _session(user: dict) -> dict:
    return asyncio.run(upload.session_for_current_user(user))


class TestSessionForCurrentUser:
    def test_approved_employee_gets_employee_role_and_status(self, fake_request):
        fake_request["row"] = _request("approved")
        user = _user("member")
        session = _session(user)
        perms = session["permissions"]
        assert perms["role"] == "employee"
        assert perms["employee_request_status"] == "approved"
        assert perms["employee_request_id"] == "jr-1"
        assert perms["can_manage_settings"] is False
        assert perms["can_upload_own_documents"] is True
        assert fake_request["calls"] == [("Staff2@newtalk.kr", UUID(user["tenant_id"]))]

    @pytest.mark.parametrize("role", ["owner", "admin"])
    def test_owner_and_admin_are_unchanged_and_skip_lookup(self, fake_request, role):
        fake_request["row"] = _request("approved")
        user = _user(role)
        assert _session(user) == upload.tenant_session_for_user(user)
        assert fake_request["calls"] == []

    def test_member_without_request_is_byte_identical(self, fake_request):
        user = _user("member")
        got = _session(user)
        assert json.dumps(got, sort_keys=True) == json.dumps(upload.tenant_session_for_user(user), sort_keys=True)
        assert "employee_request_status" not in got["permissions"]
        assert len(fake_request["calls"]) == 1

    @pytest.mark.parametrize("status", ["pending", "rejected"])
    def test_pending_and_rejected_follow_legacy_rule(self, fake_request, status):
        fake_request["row"] = _request(status)
        perms = _session(_user("member"))["permissions"]
        assert perms["role"] == f"employee_{status}"
        assert perms["employee_request_status"] == status
        assert perms["can_edit_local_data"] is False
        assert perms["can_upload_own_documents"] is (status != "rejected")

    @pytest.mark.parametrize("role", ["employee", "viewer", "member", "admin", "bogus"])
    def test_approved_access_role_matches_legacy_session_for_user(self, fake_request, role):
        row = _request("approved", role)
        fake_request["row"] = row
        assert _session(_user("member"))["permissions"] == {
            **upload.tenant_session_for_user(_user("member"))["permissions"],
            **finance.employee_request_permissions(row),
        }
        assert _session(_user("member"))["permissions"]["role"] == finance._employee_access_role(role)

    def test_existing_keys_are_kept(self, fake_request):
        fake_request["row"] = _request("approved")
        user = _user("member")
        base = upload.tenant_session_for_user(user)
        got = _session(user)
        assert set(base) <= set(got)
        assert set(base["user"]) == set(got["user"])
        assert got["user"] == base["user"] and got["tenant"] == base["tenant"]
        assert set(base["permissions"]) <= set(got["permissions"])

    @pytest.mark.parametrize("extra", [{"is_internal_admin": True}, {"user_role": "ceo"}, {"user_role": "ADMIN"}, {"user_role": "system"}])
    def test_privileged_principal_is_never_an_employee(self, fake_request, extra):
        fake_request["row"] = _request("approved")
        user = _user("member", **extra)
        assert _session(user) == upload.tenant_session_for_user(user)
        assert fake_request["calls"] == []

    def test_blank_email_skips_lookup(self, fake_request):
        fake_request["row"] = _request("approved")
        user = _user("member", email="  ")
        assert _session(user) == upload.tenant_session_for_user(user)
        assert fake_request["calls"] == []

    def test_lookup_failure_falls_back_to_membership_session(self, fake_request, caplog):
        fake_request["error"] = ConnectionError("db down")
        user = _user("member")
        assert _session(user) == upload.tenant_session_for_user(user)
        assert "직원 가입요청 조회 실패" in caplog.text

    def test_inactive_membership_still_forbidden(self, fake_request):
        user = _user("member")
        user["current_membership"]["status"] = "suspended"
        with pytest.raises(Exception) as exc:
            _session(user)
        assert getattr(exc.value, "status_code", None) == 403
        assert fake_request["calls"] == []


class TestLookupContract:
    def test_sql_is_tenant_scoped_and_reads_no_legacy_file(self):
        sql = upload.OWN_EMPLOYEE_REQUEST_SQL
        assert "h.tenant_id = $1" in sql and "lower(h.employee_email) = $2" in sql
        assert "h.deleted_at IS NULL" in sql
        assert "yeoljeong_business_tenant_mapping" in sql and "m.tenant_id = h.tenant_id" in sql
        source = Path(upload.__file__).read_text(encoding="utf-8")
        body = source.split("async def _own_employee_request(", 1)[1].split("\nasync def session_for_current_user", 1)[0]
        assert "_read_hr" not in body and "_read_file_rows" not in body and "session_for_user" not in body

    def test_row_is_converted_with_the_same_mapper_as_legacy_ledger(self, monkeypatch):
        tenant = uuid4()
        row = {
            "id": "jr-9", "employee_email": "staff2@newtalk.kr", "business_id": "biz-1", "role": "employee",
            "status": "approved", "request_payload": {}, "tenant_id": tenant,
        }
        seen: dict = {}

        class _Conn:
            async def fetchrow(self, sql, *args):
                seen["args"] = args
                return row

            async def close(self):
                seen["closed"] = True

        async def _connect():
            return _Conn()

        monkeypatch.setattr(upload, "_connect", _connect)
        got = asyncio.run(upload._own_employee_request({"email": " Staff2@Newtalk.kr "}, tenant))
        assert seen["args"] == (tenant, "staff2@newtalk.kr") and seen["closed"] is True
        assert got["id"] == "jr-9" and got["status"] == "approved" and got["role"] == "employee"


class TestSessionEndpointRegression:
    """/session 라우트 → index.html isEmployeeAccount() 까지 한 줄로 묶는다."""

    def _route(self, monkeypatch, user, row):
        async def _fake(_user_, _tenant_):
            return row

        monkeypatch.setattr(upload, "_own_employee_request", _fake)
        monkeypatch.setattr(obys_finance, "is_legacy_obys_tenant", lambda _u: False)
        return asyncio.run(obys_finance.get_session(current_user=user))

    def test_route_uses_the_employee_aware_session_for_non_legacy_tenant(self, monkeypatch):
        got = self._route(monkeypatch, _user("member"), _request("approved"))
        assert got["permissions"]["employee_request_status"] == "approved"
        assert got["permissions"]["role"] == "employee"

    @pytest.mark.skipif(NODE is None, reason="node 미설치 런타임")
    @pytest.mark.parametrize(
        "membership_role,row,expected",
        [
            ("member", _request("approved"), True),
            ("member", _request("pending"), True),
            ("member", _request("rejected"), True),
            ("member", None, False),
            ("owner", _request("approved"), False),
            ("admin", None, False),
        ],
    )
    def test_front_end_judgement_on_real_route_response(self, monkeypatch, tmp_path, membership_role, row, expected):
        html = INDEX.read_text(encoding="utf-8")
        found = re.search(r"^    function isEmployeeAccount\(", html, re.M)
        assert found
        start = found.start()
        open_at = html.index(") {", start) + 2
        depth = 0
        for pos in range(open_at, len(html)):
            depth += {"{": 1, "}": -1}.get(html[pos], 0)
            if depth == 0:
                end = pos + 1
                break
        source = html[start:end]
        session = self._route(monkeypatch, _user(membership_role), row)
        script = tmp_path / "judge.js"
        script.write_text(
            "const vm=require('node:vm');const ctx={authPermissions:()=>JSON.parse(process.env.PERMS),String,Boolean,Array};"
            "vm.createContext(ctx);vm.runInContext(process.env.SRC+'\\nthis.r=isEmployeeAccount();',ctx);console.log(JSON.stringify(ctx.r));",
            encoding="utf-8",
        )
        result = subprocess.run(
            [NODE, str(script)], text=True, capture_output=True, timeout=30,
            env={"PATH": "/usr/bin:/bin:/usr/local/bin", "SRC": source, "PERMS": json.dumps(session["permissions"])},
        )
        assert result.returncode == 0, result.stdout + result.stderr
        assert json.loads(result.stdout) is expected
