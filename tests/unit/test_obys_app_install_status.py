"""오비서 직원 앱 설치·접속 현황 API 회귀 (업무 DB 는 가짜 연결로 대체)."""
import re
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import uuid4

import pytest
from fastapi import HTTPException
from pydantic import ValidationError

from app.services import obys_app_install as svc

TENANT = uuid4()
UA = "Mozilla/5.0 (Linux; Android 14; SM-S911N) AppleWebKit/537.36 Chrome/126.0.0.0 Mobile Safari/537.36 SECRET-DEVICE-ID-123"
T0 = datetime(2026, 11, 5, 9, 0, tzinfo=timezone.utc)


def user(email="me@example.com", role="member", tenant=TENANT):
    return {"email": email, "tenant_id": str(tenant), "user_id": "u1",
            "current_membership": {"tenant_id": str(tenant), "status": "active", "role": role}}


class FakeConn:
    def __init__(self, rows=None):
        self.rows = rows if rows is not None else {}
        self.writes = []
        self.calls = []

    @asynccontextmanager
    async def transaction(self):
        yield

    async def fetchrow(self, sql, business_id, email, tenant):
        return self.rows.get((business_id, email))

    async def fetch(self, sql, tenant, business_id):
        return [r for (b, _), r in self.rows.items() if b == business_id]

    async def execute(self, sql, tenant, business_id, email, platform, mode, standalone_at, now, installed_at, family):
        self.writes.append((tenant, business_id, email, platform, mode, standalone_at, now, installed_at, family))
        old = self.rows.get((business_id, email))
        self.rows[(business_id, email)] = {
            "user_email": email, "platform": platform, "display_mode": mode, "user_agent_family": family,
            "first_standalone_at": (old or {}).get("first_standalone_at") or standalone_at,
            "last_standalone_at": standalone_at or (old or {}).get("last_standalone_at"),
            "appinstalled_at": (old or {}).get("appinstalled_at") or installed_at,
            "last_seen_at": now, "updated_at": now}

    async def close(self):
        pass


@pytest.fixture
def env(monkeypatch):
    conn = FakeConn()

    async def connect():
        return conn

    async def require_business(c, tenant, business_id):
        if business_id != "biz1":
            raise HTTPException(404, "no")

    async def clock_business(u, business_id):
        if business_id != "biz1":
            raise HTTPException(403, "현재 테넌트의 사업자가 아닙니다")

    monkeypatch.setattr(svc.ws.upload_svc, "_connect", connect)
    monkeypatch.setattr(svc.ws.upload_svc, "_require_business", require_business)
    monkeypatch.setattr(svc.ws, "_clock_business", clock_business)
    monkeypatch.setattr(svc.finance, "list_approved_employees", lambda u, b: [
        {"id": "e1", "email": "me@example.com", "name": "나", "branch": "A", "email_masked": "m***@example.com"},
        {"id": "e2", "email": "none@example.com", "name": "무기록", "branch": "B", "email_masked": "n***@example.com"},
        {"id": "e3", "email": "web@example.com", "name": "웹", "branch": "B", "email_masked": "w***@example.com"}])
    return conn


def payload(**kw):
    return svc.PingIn(**{"display_mode": "standalone", "platform": "android", "event": "launch", **kw})


@pytest.mark.asyncio
async def test_own_ping_saved_with_login_email(env):
    assert (await svc.ping(user(), "biz1", payload(), UA, T0)) == {"recorded": True}
    row = env.rows[("biz1", "me@example.com")]
    assert row["display_mode"] == "standalone" and row["last_standalone_at"] == T0


def test_payload_cannot_name_another_user():
    with pytest.raises(ValidationError):
        svc.PingIn(display_mode="standalone", platform="ios", event="launch", user_email="other@example.com")
    with pytest.raises(ValidationError):
        svc.PingIn(display_mode="weird", platform="ios", event="launch")


@pytest.mark.asyncio
async def test_ping_row_belongs_to_caller_only(env):
    await svc.ping(user("a@example.com"), "biz1", payload(), UA, T0)
    assert set(env.rows) == {("biz1", "a@example.com")}


@pytest.mark.asyncio
async def test_non_member_business_is_403(env):
    with pytest.raises(HTTPException) as e:
        await svc.ping(user(), "other-biz", payload(), UA, T0)
    assert e.value.status_code == 403
    assert not env.writes


@pytest.mark.asyncio
async def test_employee_cannot_read_status(env):
    with pytest.raises(HTTPException) as e:
        await svc.status(user(role="member"), "biz1")
    assert e.value.status_code == 403


@pytest.mark.asyncio
async def test_owner_status_lists_every_approved_employee(env):
    await svc.ping(user(), "biz1", payload(), UA, T0)
    await svc.ping(user("web@example.com"), "biz1", payload(display_mode="browser"), UA, T0)
    out = await svc.status(user("boss@example.com", role="owner"), "biz1")
    states = {i["employee_id"]: i["state"] for i in out["items"]}
    assert states == {"e1": "app_used", "e2": "none", "e3": "browser_only"}
    assert out["total"] == 3 and out["app_used"] == 1
    assert "email" not in out["items"][0]


@pytest.mark.asyncio
async def test_installed_without_app_launch_state(env):
    await svc.ping(user(), "biz1", payload(display_mode="browser", event="appinstalled"), UA, T0)
    out = await svc.status(user("boss@example.com", role="admin"), "biz1")
    assert {i["employee_id"]: i["state"] for i in out["items"]}["e1"] == "installed"


@pytest.mark.asyncio
async def test_repeat_same_value_keeps_one_row_and_skips_write(env):
    await svc.ping(user(), "biz1", payload(), UA, T0)
    again = await svc.ping(user(), "biz1", payload(), UA, T0 + timedelta(hours=3))
    assert again == {"recorded": False} and len(env.writes) == 1 and len(env.rows) == 1
    await svc.ping(user(), "biz1", payload(), UA, T0 + timedelta(days=1, minutes=1))
    assert len(env.writes) == 2 and len(env.rows) == 1
    assert env.rows[("biz1", "me@example.com")]["first_standalone_at"] == T0


@pytest.mark.asyncio
async def test_changed_value_writes_immediately(env):
    await svc.ping(user(), "biz1", payload(display_mode="browser"), UA, T0)
    await svc.ping(user(), "biz1", payload(), UA, T0 + timedelta(minutes=5))
    assert len(env.writes) == 2 and len(env.rows) == 1


@pytest.mark.asyncio
async def test_raw_user_agent_never_stored(env):
    await svc.ping(user(), "biz1", payload(), UA, T0)
    stored = repr(env.writes) + repr(env.rows)
    assert "SECRET-DEVICE-ID" not in stored and "Mozilla" not in stored and "SM-S911N" not in stored
    assert env.rows[("biz1", "me@example.com")]["user_agent_family"] == "chrome"


def test_ua_family_names_only():
    assert svc.ua_family("... Edg/120 ...") == "edge"
    assert svc.ua_family("Mozilla Version/17 Safari/605") == "safari"
    assert svc.ua_family(None) == "other"


def test_migration_matches_spec():
    root = Path(__file__).resolve().parents[2]
    sql = (root / "migrations/20261105_obys_app_install_status.sql").read_text(encoding="utf-8")
    assert "PRIMARY KEY (business_id, user_email)" in sql and "current_database() <> 'obys'" in sql
    for col in ("tenant_id", "platform", "display_mode", "first_standalone_at", "last_standalone_at",
                "last_seen_at", "appinstalled_at", "user_agent_family", "updated_at"):
        assert re.search(rf"\b{col}\b", sql)
    assert not re.search(r"\b(ip|ip_address|user_agent|latitude|longitude)\b", sql)
    assert "DROP TABLE IF EXISTS public.obys_app_install_status" in (
        root / "migrations/rollback/20261105_obys_app_install_status.sql").read_text(encoding="utf-8")


def test_router_registered_in_main():
    root = Path(__file__).resolve().parents[2]
    main = (root / "app/yeoljeong_main.py").read_text(encoding="utf-8")
    assert "obys_app_install.router" in main


STATIC = Path(__file__).resolve().parents[2] / "app/static/apps/obys"


def test_employee_screens_load_ping_module():
    """직원용 화면(본 화면·출퇴근)이 수집 모듈을 로드하고 해당 화면의 사업자 키를 넘긴다."""
    for name, key in (("index.html", "yeoljeong-finance-settings-business"), ("clock.html", "obys_clock_business_id")):
        html = (STATIC / name).read_text(encoding="utf-8")
        tag = re.search(r'<script[^>]*modules/app-install-ping\.js[^>]*>', html)
        assert tag, name
        assert key in tag.group(0), name


def test_ping_module_retries_follows_business_and_sends_no_ua():
    js = (STATIC / "modules/app-install-ping.js").read_text(encoding="utf-8")
    assert "/app-install/ping" in js and '"appinstalled"' in js and '"launch"' in js
    assert "setInterval" in js and "storage" in js and "visibilitychange" in js  # 늦은 로그인·사업자 변경 재시도
    assert "bid + suffix" in js  # 사업자별로 보낸 이력을 구분
    assert "5000" in js and "catch" in js
    body = js[js.index("JSON.stringify"):js.index("JSON.stringify") + 120]
    assert "userAgent" not in body and "user_agent" not in body


def test_admin_discards_stale_app_install_response():
    html = (STATIC / "admin.html").read_text(encoding="utf-8")
    start = html.index("async function loadLiveRoute")
    chunk = html[start:html.index("function filteredRows", start)]
    after = chunk[chunk.index("await appInstallLoadV41"):]
    assert "requestId!==state.requestId" in after.split("state.appInstall=")[0]
    fn = html[html.index("async function appInstallLoadV41"):html.index("function liveRow")]
    assert "state." not in fn  # 조회 함수는 공용 상태를 직접 덮어쓰지 않는다


_NODE_HARNESS = r"""
const fs = require('fs');
const scenario = JSON.parse(process.argv[1]);
const calls = []; let now = 1e12; let listFails = scenario.listFails || 0;
const store = Object.assign({aads_token: 'tok-abcdefghijklmnop'}, scenario.store || {});
global.localStorage = {getItem: k => (k in store ? store[k] : null)};
global.document = {cookie: '', hidden: false, currentScript: {dataset: {businessKeys: 'k1'}}, addEventListener(){}};
global.window = {addEventListener(){}};
global.navigator = {userAgent: 'Mozilla Android', standalone: false};
global.matchMedia = () => ({matches: true});
Date.now = () => now;
global.setTimeout = (f) => 0; global.setInterval = () => 0; global.clearTimeout = () => {};
global.fetch = async (url) => {
  calls.push(url);
  if (url.endsWith('/workspaces/businesses')) {
    if (listFails-- > 0) throw new Error('net');
    return {ok: true, status: 200, json: async () => ({businesses: [{id: 'good'}]})};
  }
  const bid = decodeURIComponent(url.split('/obys/')[1].split('/')[0]);
  const st = (scenario.status || {})[bid] || 200;
  return {ok: st < 300, status: st, json: async () => ({})};
};
eval(fs.readFileSync(process.argv[2], 'utf8'));
(async () => {
  for (const step of scenario.steps) {
    if (step.advance) now += step.advance;
    await window.ObysAppInstallPing.send('launch', false);
  }
  console.log(JSON.stringify(calls.map(c => c.replace('/api/v1/', ''))));
})();
"""


def _run_ping(scenario):
    import json
    import shutil
    import subprocess

    node = shutil.which("node")
    if not node:
        import pytest
        pytest.skip("node 없음")
    js = STATIC / "modules/app-install-ping.js"
    out = subprocess.run([node, "-e", _NODE_HARNESS, json.dumps(scenario), str(js)],
                         capture_output=True, text=True, timeout=30, check=True).stdout
    return json.loads(out.strip().splitlines()[-1])


def test_ping_retries_business_list_after_transient_failure():
    calls = _run_ping({"listFails": 1, "steps": [{}, {"advance": 31000}]})
    assert calls == ["workspaces/businesses", "workspaces/businesses", "obys/good/app-install/ping"]


def test_ping_falls_back_when_stored_business_is_rejected():
    calls = _run_ping({"store": {"k1": "stale"}, "status": {"stale": 403}, "steps": [{}, {"advance": 1}]})
    assert calls == ["obys/stale/app-install/ping", "workspaces/businesses", "obys/good/app-install/ping"]


def test_ping_resends_after_ttl_expires():
    calls = _run_ping({"store": {"k1": "good"}, "steps": [{}, {"advance": 1000}, {"advance": 7 * 3600 * 1000}]})
    assert calls == ["obys/good/app-install/ping", "obys/good/app-install/ping"]
