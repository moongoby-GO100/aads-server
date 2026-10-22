"""대신 시연(demo_runner)·조각 분할(fragment_splitter)·읽기 전용 자동 승인 (T3).

실제 외부 사이트는 부르지 않는다. 실행기는 단계 payload 를 받아 정해 둔 결과를 돌려주는 모의 객체다.
AC-03(쓰기 단계 멈춤) · AC-04(이유별 재시연) · AC-06(조각 재사용) · AC-09(비밀번호 변경 시 재시도 없음).
"""
from __future__ import annotations

import asyncio
import json
import uuid
from typing import Any

import pytest

from app.services import automation_demo_runner as runner
from app.services import automation_draft_flow as flow
from app.services import automation_draft_service as svc
from app.services import automation_fragment_splitter as splitter
from app.services.work_recipe import approval_notifier, registration, store
from app.services.work_recipe.guard import RiskLevel
from tests.unit.test_automation_drafts_api import ALICE, TENANT_A, Store

PASSWORD = "S3cr3t-Pw!xyz"
SK_LIKE = "sk" + "-" + "ant-oat01-abcdefghijklmnop"
USERNAME = "owner-login-01"
CRED = str(uuid.uuid4())
SESSION = str(uuid.uuid4())
DOMAIN = "store.coupangeats.com"


def run(coro):
    return asyncio.run(coro)


# ── 모의 실행기 ────────────────────────────────────────────────────────────

class FakeExecutor:
    """단계 설명(label)별로 결과를 돌려준다. 받은 payload 를 모두 남긴다(비밀값 검사용)."""

    def __init__(self, script: dict[str, list[dict[str, Any]]] | None = None, *, alive: bool = False,
                 rows: str = '- row "2026-10-13 120,000원"\n- row "2026-10-12 98,000원"') -> None:
        self.script = {k: list(v) for k, v in (script or {}).items()}
        self.calls: list[dict[str, Any]] = []
        self.alive = alive
        self.rows = rows
        self.url = f"https://{DOMAIN}/login"

    def session_alive(self) -> bool:
        return self.alive

    async def __call__(self, payload: dict[str, Any]) -> dict[str, Any]:
        self.calls.append(dict(payload))
        key = str(payload.get("description") or "")
        queue = self.script.get(key)
        if queue:
            result = queue.pop(0)
            if result.get("ok"):
                result.setdefault("evidence", self._evidence())
            return result
        if payload["action"] == "press":
            self.url = f"https://{DOMAIN}/home"
        elif payload["action"] == "navigate" and payload.get("purpose") != "login":
            self.url = str(payload.get("url") or self.url)
        output = self.rows if payload["action"] == "snapshot" else None
        return {"ok": True, "output": output, "evidence": self._evidence()}

    def _evidence(self) -> dict[str, Any]:
        return {"url": self.url, "dom": {"text": "매출 현황"},
                "screenshot": {"status": "captured", "url": f"/shots/{len(self.calls)}.png"}}


class World:
    def __init__(self) -> None:
        self.store = Store()
        self.executor = FakeExecutor()
        self.emitted: list[tuple[str, str, dict[str, Any]]] = []
        self.frames: list[dict[str, Any]] = []
        self.login_fragment: dict[str, Any] | None = None
        self.credential: dict[str, Any] | None = {"username": USERNAME, "password": PASSWORD}
        self.launched: list[str] = []
        self.registrations: list[dict[str, Any]] = []
        self.decisions: list[dict[str, Any]] = []
        self.notified: list[str] = []
        self.recipes: list[dict[str, Any]] = []
        self.approved_history: list[dict[str, Any]] = []

    def deps(self) -> runner.DemoDeps:
        async def load(tenant_id, credential_id):
            return dict(self.credential) if self.credential else None

        async def find_login(**kwargs):
            return self.login_fragment

        async def emit(session_id, event_type, payload):
            self.emitted.append((session_id, event_type, payload))

        async def open_frames(draft):
            return "task-1"

        async def push(**kwargs):
            self.frames.append(kwargs)

        return runner.DemoDeps(
            executor_factory=lambda draft, allowed: self.executor, load_credential=load,
            find_login_fragment=find_login, emit=emit, open_frames=open_frames, push_frame=push,
        )


@pytest.fixture
def world(monkeypatch):
    monkeypatch.setenv(svc.DRAFTS_FLAG, "true")
    monkeypatch.delenv(svc.AUTO_APPROVE_READ_FLAG, raising=False)
    w = World()
    for name in ("create_draft", "get_draft", "transition", "update_fields", "list_drafts"):
        monkeypatch.setattr(svc, name, getattr(w.store, name))
    monkeypatch.setattr(flow, "_launch_demo", lambda draft_id, tenant_id: w.launched.append(draft_id))

    async def request_registration(recipe, *, tenant_id, requested_by=""):
        dry = registration.build_dry_run(recipe, proposed_version=1)
        row = {"id": str(uuid.uuid4()), "status": "pending", "name": recipe.name, "domain": recipe.domain,
               "requested_by": requested_by, "dry_run": dry, "spec": recipe.to_dict()}
        w.registrations.append(row)
        return row

    async def decide_registration(registration_id, *, tenant_id, decision, decided_by, reason=""):
        w.decisions.append({"id": registration_id, "decision": decision, "decided_by": decided_by})
        return {"id": registration_id, "status": "approved", "decided_by": decided_by}

    async def list_registrations(*, tenant_id, status="pending"):
        if status == "approved":
            return list(w.approved_history)
        return [r for r in w.registrations if r["status"] == status]

    async def list_recipes(**kwargs):
        return list(w.recipes)

    async def notify(row, **kwargs):
        w.notified.append(row["name"])
        return True

    async def get_cred(*, tenant_id, credential_id, domain):
        return {"id": credential_id, "display_name": "사장님 계정"}

    monkeypatch.setattr(registration, "request_registration", request_registration)
    monkeypatch.setattr(registration, "decide_registration", decide_registration)
    monkeypatch.setattr(registration, "list_registrations", list_registrations)
    monkeypatch.setattr(store, "list_recipes", list_recipes)
    monkeypatch.setattr(approval_notifier, "notify_pending_created", notify)
    monkeypatch.setattr(flow.linker, "get_credential_for_domain", get_cred)
    return w


def actor() -> flow.Actor:
    return flow.Actor(TENANT_A, ALICE)


def new_draft(w: World, *, state="demonstrating", plan=None, credential=True, output_kind="table", **extra) -> str:
    intent = {"site_name": "쿠팡이츠", "domain": DOMAIN, "url": f"https://{DOMAIN}/login",
              "task": "어제 매출", "output_kind": output_kind}
    row = run(w.store.create_draft(tenant_id=TENANT_A, owner_user=ALICE, title="쿠팡이츠 어제 매출 받기",
                                   intent_text="쿠팡이츠 어제 매출", intent=intent, session_id=SESSION))
    did = row["id"]
    ref = {"plan": plan} if plan is not None else {}
    w.store.set_state(did, state, credential_id=CRED if credential else None, recording_ref=json.dumps(ref),
                      demo_attempts=extra.pop("demo_attempts", 0), **extra)
    return did


def demo(w: World, did: str) -> runner.Outcome:
    return run(runner.run_demo(did, tenant_id=TENANT_A, deps=w.deps()))


def row(w: World, did: str) -> dict[str, Any]:
    data = dict(w.store.rows[did])
    for key in ("recording_ref", "evidence", "intent"):
        if isinstance(data.get(key), str):
            data[key] = json.loads(data[key])
    return data


LOGIN = [
    {"action": "navigate", "url": f"https://{DOMAIN}/login", "purpose": "login", "label": "쿠팡이츠"},
    {"action": "fill", "purpose": "login", "credential_field": "username", "label": "아이디",
     "selector": "#loginId", "value": "{{secret.username}}"},
    {"action": "fill", "purpose": "login", "credential_field": "password", "label": "비밀번호",
     "selector": "#password", "value": "{{secret.password}}"},
    {"action": "press", "purpose": "login", "login_submit": True, "label": "로그인", "selector": "#password",
     "value": "Enter"},
]
NAV = [{"action": "click", "purpose": "navigate", "label": "매출 관리", "selector": "a.sales-menu"}]
QUERY = [
    {"action": "fill", "purpose": "query", "role": "date", "label": "조회 날짜", "selector": "#date",
     "value": "2026-10-13"},
    {"action": "snapshot", "purpose": "query", "label": "결과", "selector": "table"},
]


def plan(*parts: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return runner.number([step for part in parts for step in part])


# ── 1. 정상 시연 ───────────────────────────────────────────────────────────

def test_full_demo_reaches_result_review_with_preview_and_live_frames(world):
    did = new_draft(world, plan=plan(LOGIN, NAV, QUERY))
    outcome = demo(world, did)
    assert outcome.status == "done"
    data = row(world, did)
    assert data["state"] == "result_review"
    assert data["evidence"]["preview_rows"] == ["2026-10-13 120,000원", "2026-10-12 98,000원"]
    assert data["evidence"]["final_screenshot_url"].startswith("/shots/")
    assert data["recording_ref"]["completed"] == [1, 2, 3, 4, 5, 6, 7]
    texts = [p["text"] for _, _, p in world.emitted]
    assert "로그인하는 중" in texts and "'매출 관리' 메뉴를 누르는 중" in texts and "날짜를 고르는 중" in texts
    assert all(sid == SESSION and ev == "automation_draft.progress" for sid, ev, _ in world.emitted)
    assert world.frames and all(f["task_id"] == "task-1" for f in world.frames)


def test_preview_is_capped_at_five_rows(world):
    world.executor.rows = "\n".join(f'- row "r{i}"' for i in range(9))
    did = new_draft(world, plan=plan(NAV, QUERY), credential=False)
    demo(world, did)
    assert row(world, did)["evidence"]["preview_rows"] == ["r0", "r1", "r2", "r3", "r4"]


def test_default_plan_uses_secret_references_and_respects_limits():
    steps = runner.default_plan({"domain": DOMAIN, "url": f"https://{DOMAIN}/login"}, has_credential=True,
                                login_fragment=None)
    assert [s["purpose"] for s in steps] == ["login", "login", "login", "login", "query"]
    assert {s.get("value") for s in steps if s.get("credential_field")} == {"{{secret.username}}", "{{secret.password}}"}
    assert len(steps) <= runner.MAX_STEPS
    with pytest.raises(ValueError):
        runner.number([{"action": "snapshot"}] * (runner.MAX_STEPS + 1))


def test_plan_over_25_steps_fails_without_running(world):
    did = new_draft(world, plan=[{"action": "snapshot", "purpose": "query"}] * 26, credential=False)
    assert demo(world, did).failure_code == "layout_changed"
    assert world.executor.calls == [] and row(world, did)["state"] == "failed"


def test_run_is_cut_at_time_limit(world, monkeypatch):
    monkeypatch.setattr(runner, "RUN_SECONDS", 0.05)

    class Slow(FakeExecutor):
        async def __call__(self, payload):
            await asyncio.sleep(1)
            return {"ok": True}

    world.executor = Slow()
    did = new_draft(world, plan=plan(NAV, QUERY), credential=False)
    outcome = demo(world, did)
    assert (outcome.failure_code, outcome.reason) == ("transient", "demo_timeout")
    assert row(world, did)["state"] == "failed"


def test_executor_start_failure_ends_in_failed_not_stuck(world):
    deps = world.deps()

    def broken(draft, allowed):
        raise RuntimeError("bridge down")

    deps.executor_factory = broken
    did = new_draft(world, plan=plan(NAV, QUERY), credential=False)
    outcome = run(runner.run_demo(did, tenant_id=TENANT_A, deps=deps))
    assert (outcome.failure_code, outcome.reason) == ("transient", "executor_unavailable")
    assert row(world, did)["state"] == "failed"


def test_unexpected_step_error_ends_in_failed(world):
    class Boom(FakeExecutor):
        async def __call__(self, payload):
            raise KeyError("boom")

    world.executor = Boom()
    did = new_draft(world, plan=plan(NAV, QUERY), credential=False)
    assert demo(world, did).reason == "runner_error" and row(world, did)["state"] == "failed"


def test_runner_does_nothing_unless_demonstrating(world):
    did = new_draft(world, state="credential_ready", plan=plan(NAV, QUERY))
    assert demo(world, did).status == "skipped" and world.executor.calls == []


# ── 2. AC-03 쓰기 단계 멈춤 ────────────────────────────────────────────────

SAVE = [{"action": "click", "purpose": "query", "label": "저장", "selector": "button.save"}]


def test_ac03_write_step_pauses_before_execution_then_proceed_runs_it(world):
    did = new_draft(world, plan=plan(NAV, SAVE, QUERY[1:]), credential=False)
    outcome = demo(world, did)
    assert outcome.status == "paused" and outcome.risk == RiskLevel.WRITE_EXTERNAL.value
    assert [c["description"] for c in world.executor.calls] == ["매출 관리"]  # 저장 단계는 실행하지 않았다
    data = row(world, did)
    assert data["state"] == "paused_risk" and data["recording_ref"]["paused_seq"] == 2
    card = flow.view(data)["card"]
    assert card["paused_step"] == "저장" and [b["id"] for b in card["buttons"]] == ["proceed", "stop_here", "cancel"]

    run(flow.risk_decision(actor(), did, "proceed"))
    assert world.launched == [did]
    approvals = row(world, did)["recording_ref"]["approved_risk"]
    assert list(approvals) == ["2"] and approvals["2"] == runner.step_fingerprint(row(world, did)["recording_ref"]["plan"][1])
    world.executor.calls.clear()
    assert demo(world, did).status == "done"
    assert "저장" in [c["description"] for c in world.executor.calls]
    data = row(world, did)
    assert data["state"] == "result_review" and data["max_risk"] == "WRITE_EXTERNAL"
    saved = data["recording_ref"]["recorded"]["2"]["step"]
    assert saved["risk"] == "WRITE_EXTERNAL"  # 등록 시 그 단계는 승인 필수로 남는다


def test_ac03_irreversible_step_pauses_even_inside_approved_login_fragment():
    step = {"action": "click", "label": "결제", "approved_fragment": "x_01_login", "risk": "WRITE_EXTERNAL"}
    assert runner.needs_pause(step, runner.step_risk(step, DOMAIN))
    login_step = {"action": "click", "label": "로그인", "approved_fragment": "x_01_login", "risk": "WRITE_EXTERNAL"}
    assert not runner.needs_pause(login_step, runner.step_risk(login_step, DOMAIN))


PAY = [{"action": "click", "label": "결제", "selector": "button.pay", "purpose": "login",
        "approved_fragment": "coupangeats_01_login"}]


def test_irreversible_step_in_reused_login_fragment_pauses_in_run(world):
    """리뷰 지적 1: 승인된 로그인 조각이라도 되돌릴 수 없는 단계는 실행 전에 멈춘다(실행기 허용 목록에도 없다)."""
    allowed_seen: list[set[int]] = []
    fragment = [{**step, "approved_fragment": "coupangeats_01_login"} for step in LOGIN]
    p = plan(fragment, PAY, QUERY[1:])
    did = new_draft(world, plan=p)
    deps = world.deps()
    deps.executor_factory = lambda draft, allowed: (allowed_seen.append(set(allowed)), world.executor)[1]
    outcome = run(runner.run_demo(did, tenant_id=TENANT_A, deps=deps))
    assert outcome.status == "paused" and outcome.seq == 5 and outcome.risk == RiskLevel.IRREVERSIBLE.value
    assert "결제" not in [c["description"] for c in world.executor.calls]
    assert 5 not in allowed_seen[0]


def test_approval_does_not_survive_redemo_or_changed_step(world):
    """리뷰 지적 2: 새 시연은 승인을 비우고, 같은 번호라도 내용이 바뀐 단계는 다시 멈춘다."""
    p = plan(NAV, SAVE, QUERY[1:])
    p[1]["role"] = "menu"
    did = new_draft(world, plan=p, credential=False)
    demo(world, did)
    run(flow.risk_decision(actor(), did, "proceed"))
    assert row(world, did)["recording_ref"]["approved_risk"]
    # 같은 번호의 단계 내용을 바꾸면(wrong_menu 와 같은 효과) 옛 승인은 맞지 않는다.
    ref = row(world, did)["recording_ref"]
    ref["plan"][1].update(selector="text=\"삭제\"", label="삭제")
    world.store.rows[did]["recording_ref"] = json.dumps(ref)
    world.executor.calls.clear()
    outcome = demo(world, did)
    assert outcome.status == "paused" and outcome.seq == 2
    assert "삭제" not in [c["description"] for c in world.executor.calls]
    # 이유별 재시연(새 시연)은 내용이 같은 단계라도 옛 승인을 가져가지 않는다.
    ref = row(world, did)["recording_ref"]
    ref["approved_risk"] = {"2": runner.step_fingerprint(ref["plan"][1])}
    world.store.rows[did]["recording_ref"] = json.dumps(ref)
    world.store.set_state(did, "revising")
    run(flow.redemo(actor(), did, "other"))
    data = row(world, did)["recording_ref"]
    assert data["approved_risk"] == {} and "approved_risk_seqs" not in data
    world.executor.calls.clear()
    outcome = demo(world, did)
    assert outcome.status == "paused" and outcome.seq == 2
    assert "삭제" not in [c["description"] for c in world.executor.calls]


def test_start_demo_again_clears_previous_approvals(world):
    did = new_draft(world, state="failed", plan=plan(NAV, SAVE), credential=False, failure_code="transient")
    ref = {"plan": plan(NAV, SAVE), "approved_risk": {"2": runner.step_fingerprint(plan(NAV, SAVE)[1])}}
    world.store.rows[did]["recording_ref"] = json.dumps(ref)
    run(flow.start_demo(actor(), did, "failed"))
    assert row(world, did)["recording_ref"]["approved_risk"] == {}


SUBMIT = [{"action": "click", "purpose": "query", "label": "제출", "selector": "button.submit"}]
SEND = [{"action": "click", "purpose": "query", "label": "전송", "selector": "button.send"}]


def test_resume_does_not_rerun_completed_external_write(world):
    """리뷰 지적 3: 승인된 제출 뒤 다음 위험 단계에서 멈췄다 진행해도, 끝난 제출은 다시 내지 않는다."""
    did = new_draft(world, plan=plan(NAV, SUBMIT, SEND, QUERY[1:]), credential=False)
    assert demo(world, did).seq == 2
    run(flow.risk_decision(actor(), did, "proceed"))
    world.executor.calls.clear()
    assert demo(world, did).seq == 3  # 제출(2)은 실행, 전송(3)에서 멈춤
    assert [c["description"] for c in world.executor.calls].count("제출") == 1
    assert row(world, did)["recording_ref"]["approved_risk"] == {}  # 승인은 한 번 쓰고 소모된다
    run(flow.risk_decision(actor(), did, "proceed"))
    world.executor.calls.clear()
    assert demo(world, did).status == "done"
    calls = [c["description"] for c in world.executor.calls]
    assert "제출" not in calls and calls.count("전송") == 1 and "매출 관리" in calls
    assert sorted(row(world, did)["recording_ref"]["recorded"], key=int) == ["1", "2", "3", "4"]


def test_reused_login_fragment_restores_submit_step():
    """리뷰 지적 4: 저장된 조각에 표시가 없어도, 로그인 값을 채운 뒤의 누르기는 로그인 제출이다."""
    fragment = {"name": "coupangeats_01_login", "spec": {"steps": [
        {"action": "navigate", "url": f"https://{DOMAIN}/login", "description": "열기"},
        {"action": "fill", "selector": "#pw", "value": "{{secret.password}}", "description": "비번"},
        {"action": "press", "selector": "#pw", "value": "Enter", "description": "로그인"},
    ]}}
    p = runner.default_plan({"domain": DOMAIN}, has_credential=True, login_fragment=fragment)
    assert [bool(s.get("login_submit")) for s in p[:3]] == [False, False, True]


def _fragment_login(world):
    world.login_fragment = {"name": "coupangeats_01_login", "spec": {"steps": [
        {"action": "navigate", "url": f"https://{DOMAIN}/login", "description": "열기"},
        {"action": "fill", "selector": "#pw", "value": "{{secret.password}}", "description": "비번"},
        {"action": "press", "selector": "#pw", "value": "Enter", "description": "로그인"},
    ]}}


def test_reused_login_submit_detects_wrong_password_screen(world):
    _fragment_login(world)
    world.executor.script = {"로그인": [{"ok": True, "evidence": {
        "url": f"https://{DOMAIN}/login", "dom": {"text": "아이디 또는 비밀번호가 일치하지 않습니다"}}}]}
    did = new_draft(world)
    outcome = demo(world, did)
    assert outcome.status == "failed" and outcome.failure_code == "auth_failed"
    assert [c["description"] for c in world.executor.calls].count("로그인") == 1


def test_reused_login_submit_is_not_retried_on_transient(world):
    _fragment_login(world)
    world.executor.script = {"로그인": [{"ok": False, "error": "Timeout 30000ms exceeded"}]}
    did = new_draft(world)
    outcome = demo(world, did)
    assert outcome.status == "failed" and outcome.failure_code == "transient"
    assert [c["description"] for c in world.executor.calls].count("로그인") == 1


def test_recorded_login_submit_keeps_its_marker(world):
    did = new_draft(world, plan=plan(LOGIN, QUERY))
    assert demo(world, did).status == "done"
    assert row(world, did)["recording_ref"]["recorded"]["4"]["step"]["login_submit"] is True


def test_stop_here_moves_to_review_with_progress_so_far(world):
    did = new_draft(world, plan=plan(NAV, SAVE, QUERY[1:]), credential=False)
    demo(world, did)
    body = run(flow.risk_decision(actor(), did, "stop_here"))
    assert body["draft"]["state"] == "result_review" and world.launched == []


def test_executor_gate_block_is_treated_as_pause(world):
    world.executor.script = {"매출 관리": [{"ok": False, "status": "blocked", "risk": "WRITE_EXTERNAL",
                                            "recovery": {"reason": "approval_required"}}]}
    did = new_draft(world, plan=plan(NAV, QUERY), credential=False)
    assert demo(world, did).status == "paused"


# ── 3. 실패 6분류 ──────────────────────────────────────────────────────────

@pytest.mark.parametrize("result,step,code", [
    ({"ok": False, "recovery": {"reason": "server_ip_blocked"}}, {"action": "navigate"}, "blocked"),
    ({"ok": False, "error": "HTTP 403 Forbidden"}, {"action": "navigate"}, "blocked"),
    ({"ok": False, "error": "TimeoutError: waiting for locator('a.sales')"}, {"action": "click"}, "layout_changed"),
    ({"ok": False, "error": "net::ERR_CONNECTION_RESET"}, {"action": "navigate"}, "transient"),
    ({"ok": False, "error": "보안문자를 입력해 주세요 captcha"}, {"action": "click"}, "challenge"),
])
def test_classify_error(result, step, code):
    assert runner.classify_error(step, result) == code


def test_inspect_success_detects_login_bounce_challenge_and_block_page():
    submit = {"action": "press", "login_submit": True}
    login_url = f"https://{DOMAIN}/login"
    bounced = {"ok": True, "evidence": {"url": login_url, "dom": {"text": "로그인"}}}
    assert runner.inspect_success(submit, bounced, pre_url=login_url) == "auth_failed"
    wrong = {"ok": True, "evidence": {"url": f"https://{DOMAIN}/x", "dom": {"text": "비밀번호가 일치하지 않습니다"}}}
    assert runner.inspect_success(submit, wrong, pre_url=login_url) == "auth_failed"
    home = {"ok": True, "evidence": {"url": f"https://{DOMAIN}/home", "dom": {"text": "매출"}}}
    assert runner.inspect_success(submit, home, pre_url=login_url) is None
    assert runner.inspect_success({"action": "click"}, {"ok": True, "server_access": {"blocked": True}}, pre_url="") == "blocked"


LOGIN_URL = f"https://{DOMAIN}/login"
PROBE = "로그인 결과 확인"


def _bounce(text="로그인", aria=""):
    ev = {"url": LOGIN_URL, "dom": {"text": text}}
    if aria:
        ev["aria"] = {"text": aria}
    return {"ok": True, "evidence": ev}


@pytest.fixture
def fast_settle(monkeypatch):
    monkeypatch.setattr(runner, "LOGIN_SETTLE_SECONDS", 0.3)
    monkeypatch.setattr(runner, "LOGIN_POLL_SECONDS", 0.01)


def test_inspect_success_accepts_login_form_gone_and_new_email_failure_text():
    submit = {"action": "press", "login_submit": True}
    form = _bounce(aria='- textbox "이메일"\n- textbox "비밀번호"')
    gone = _bounce(aria='- heading "대시보드"')
    assert runner.inspect_success(submit, form, pre_url=LOGIN_URL) == "auth_failed"
    assert runner.inspect_success(submit, gone, pre_url=LOGIN_URL) is None
    wrong = {"ok": True, "evidence": {"url": f"https://{DOMAIN}/x", "dom": {"text": "이메일 또는 비밀번호가 올바르지 않습니다"}}}
    assert runner.inspect_success(submit, wrong, pre_url=LOGIN_URL) == "auth_failed"


def test_spa_delayed_redirect_url_change_is_success_after_polling(world, fast_settle):
    world.executor.script = {
        "로그인": [_bounce()],
        PROBE: [_bounce(), _bounce(), {"ok": True, "evidence": {"url": f"https://{DOMAIN}/home", "dom": {"text": "매출"}}}],
    }
    did = new_draft(world, plan=plan(LOGIN, QUERY))
    assert demo(world, did).status == "done"
    descriptions = [c["description"] for c in world.executor.calls]
    assert descriptions.count("로그인") == 1 and descriptions.count(PROBE) == 3


def test_spa_delayed_login_form_disappearance_is_success(world, fast_settle):
    form = '- textbox "아이디"\n- textbox "비밀번호"'
    world.executor.script = {
        "로그인": [_bounce(aria=form)],
        PROBE: [_bounce(aria=form), _bounce(aria='- heading "매출 현황"')],
    }
    did = new_draft(world, plan=plan(LOGIN, QUERY))
    assert demo(world, did).status == "done"


def test_login_bounce_without_signal_fails_after_bounded_polling(world, fast_settle):
    world.executor.script = {"로그인": [_bounce()], PROBE: [_bounce() for _ in range(200)]}
    did = new_draft(world, plan=plan(LOGIN, QUERY))
    outcome = demo(world, did)
    assert outcome.status == "failed" and outcome.failure_code == "auth_failed"
    probes = [c for c in world.executor.calls if c["description"] == PROBE]
    assert 1 <= len(probes) < 200
    assert "매출 관리" not in [c["description"] for c in world.executor.calls]


def test_email_or_password_failure_text_fails_immediately_without_polling(world, fast_settle):
    world.executor.script = {"로그인": [_bounce("이메일 또는 비밀번호가 올바르지 않습니다")]}
    did = new_draft(world, plan=plan(LOGIN, QUERY))
    outcome = demo(world, did)
    assert outcome.failure_code == "auth_failed"
    assert PROBE not in [c["description"] for c in world.executor.calls]


def test_challenge_during_login_polling_stops_with_challenge(world, fast_settle):
    world.executor.script = {"로그인": [_bounce()], PROBE: [_bounce("인증번호를 입력해 주세요")]}
    did = new_draft(world, plan=plan(LOGIN, QUERY))
    assert demo(world, did).failure_code == "challenge"


def test_transient_is_retried_once_automatically(world):
    world.executor.script = {"매출 관리": [{"ok": False, "error": "net::ERR_TIMED_OUT"}]}
    did = new_draft(world, plan=plan(NAV, QUERY), credential=False)
    assert demo(world, did).status == "done"
    assert [c["description"] for c in world.executor.calls].count("매출 관리") == 2
    assert row(world, did)["recording_ref"]["auto_retried"] is True


def test_transient_twice_fails_as_transient(world):
    flaky = {"ok": False, "error": "net::ERR_TIMED_OUT"}
    world.executor.script = {"매출 관리": [dict(flaky), dict(flaky), dict(flaky)]}
    did = new_draft(world, plan=plan(NAV, QUERY), credential=False)
    assert demo(world, did).failure_code == "transient"
    assert [c["description"] for c in world.executor.calls].count("매출 관리") == 2


@pytest.mark.parametrize("result,code", [
    ({"ok": False, "error": "TimeoutError: waiting for locator('a.sales-menu')"}, "layout_changed"),
    ({"ok": False, "recovery": {"reason": "server_ip_blocked"}}, "blocked"),
    ({"ok": True, "evidence": {"url": f"https://{DOMAIN}/x", "dom": {"text": "보안문자 captcha 입력"}}}, "challenge"),
])
def test_non_transient_failures_are_not_retried(world, result, code):
    world.executor.script = {"매출 관리": [result, {"ok": True}]}
    did = new_draft(world, plan=plan(NAV, QUERY), credential=False)
    assert demo(world, did).failure_code == code
    assert [c["description"] for c in world.executor.calls].count("매출 관리") == 1
    assert row(world, did)["failure_code"] == code


def test_empty_result_when_table_has_no_rows(world):
    world.executor.rows = "조회된 내역이 없습니다"
    did = new_draft(world, plan=plan(NAV, QUERY), credential=False)
    assert demo(world, did).failure_code == "empty_result"


@pytest.mark.parametrize("code,suggested", [
    ("blocked", True), ("challenge", True), ("auth_failed", False), ("transient", False),
])
def test_pc_lane_is_only_suggested_for_blocked_and_challenge(code, suggested):
    card = svc.build_card("failed", failure_code=code)
    assert ("suggestions" in card) is suggested
    if suggested:
        assert card["suggestions"] == [{"id": "continue_on_pc", "label": "내 PC에서 계속하기"}]


# ── 4. AC-09 비밀번호가 바뀌었을 때 ────────────────────────────────────────

def test_ac09_wrong_password_submits_login_once_and_blocks_retry(world):
    world.executor.script = {"로그인": [{"ok": True, "evidence": {
        "url": f"https://{DOMAIN}/login", "dom": {"text": "아이디 또는 비밀번호를 확인해 주세요"}}}]}
    did = new_draft(world, plan=plan(LOGIN, NAV, QUERY))
    outcome = demo(world, did)
    assert outcome.failure_code == "auth_failed"
    assert [c["description"] for c in world.executor.calls].count("로그인") == 1
    assert "매출 관리" not in [c["description"] for c in world.executor.calls]
    data = row(world, did)
    assert data["state"] == "failed" and data["failure_code"] == "auth_failed"
    with pytest.raises(flow.FlowError) as err:
        run(flow.start_demo(actor(), did, "failed"))
    assert (err.value.status_code, err.value.code) == (409, "login_retry_blocked")
    assert world.launched == []
    relinked = run(flow.choose_credential(actor(), did, credential_id=CRED))
    assert relinked["draft"]["state"] == "credential_ready"


# ── 4-1. 로그인 없이 진행한 초안 ────────────────────────────────────────────

PUBLIC_URL = f"https://{DOMAIN}/forms/post"
USER_FORM = '- textbox "아이디"\n- textbox "비밀번호"'


def no_login_draft(w: World, **kw) -> str:
    did = new_draft(w, state="credential_ready", credential=False, **kw)
    data = row(w, did)
    w.store.rows[did]["intent"] = json.dumps({**data["intent"], "url": PUBLIC_URL, "login_required": False})
    return did


def test_no_login_demo_plan_has_no_login_step_and_reaches_review(world):
    did = no_login_draft(world)
    started = run(flow.start_demo(actor(), did, "credential_ready"))
    assert started["draft"]["state"] == "demonstrating" and world.launched == [did]
    assert demo(world, did).status == "done"
    steps = row(world, did)["recording_ref"]["plan"]
    assert steps and all(step.get("purpose") != "login" for step in steps)
    assert not any(step.get("credential_field") for step in steps)
    assert [c["description"] for c in world.executor.calls].count("로그인") == 0
    assert row(world, did)["state"] == "result_review"
    assert row(world, did)["credential_id"] is None


def test_no_login_draft_meeting_login_screen_fails_as_auth_failed_and_can_relink(world):
    world.executor.script = {"쿠팡이츠": [_bounce(aria=USER_FORM)]}
    did = no_login_draft(world)
    run(flow.start_demo(actor(), did, "credential_ready"))
    outcome = demo(world, did)
    assert outcome.status == "failed" and outcome.failure_code == "auth_failed"
    data = row(world, did)
    assert data["state"] == "failed" and data["failure_code"] == "auth_failed"
    view = flow.view(world.store.rows[did])
    assert view["card"]["body"] == svc.NO_LOGIN_AUTH_FAILED_BODY
    assert "relink_login" in [b["id"] for b in view["card"]["buttons"]]
    with pytest.raises(flow.FlowError) as err:
        run(flow.start_demo(actor(), did, "failed"))
    assert err.value.code == "login_retry_blocked"
    with pytest.raises(flow.FlowError) as both:
        run(flow.choose_credential(actor(), did, credential_id=CRED, no_login=True))
    assert both.value.status_code == 422

    relinked = run(flow.choose_credential(actor(), did, credential_id=CRED))
    assert relinked["draft"]["state"] == "credential_ready"
    assert relinked["draft"]["credential_id"] == CRED
    assert relinked["draft"]["intent"]["login_required"] is True
    assert "plan" not in row(world, did)["recording_ref"]

    run(flow.start_demo(actor(), did, "credential_ready"))
    assert demo(world, did).status == "done"
    steps = row(world, did)["recording_ref"]["plan"]
    assert any(step.get("login_submit") for step in steps)
    assert [c["description"] for c in world.executor.calls].count("로그인") == 1


def test_no_login_redirect_to_login_address_is_also_auth_failed(world):
    world.executor.script = {"쿠팡이츠": [_bounce("로그인 해 주세요")]}
    did = no_login_draft(world)
    run(flow.start_demo(actor(), did, "credential_ready"))
    assert demo(world, did).failure_code == "auth_failed"


def test_inspect_success_only_flags_login_screen_when_user_chose_no_login():
    step = {"action": "navigate", "url": PUBLIC_URL, "purpose": "navigate"}
    form = _bounce(aria=USER_FORM)
    assert runner.inspect_success(step, form, pre_url="", no_login=True) == "auth_failed"
    assert runner.inspect_success(step, form, pre_url="") is None
    plain = {"ok": True, "evidence": {"url": PUBLIC_URL, "dom": {"text": "주문 양식"},
                                      "aria": {"text": '- textbox "고객명"\n- textbox "전화번호"'}}}
    assert runner.inspect_success(step, plain, pre_url="", no_login=True) is None
    asked_for_login = {"action": "navigate", "url": LOGIN_URL, "purpose": "navigate"}
    assert runner.inspect_success(asked_for_login, _bounce(), pre_url="", no_login=True) is None


def test_challenge_also_blocks_retry(world):
    did = new_draft(world, state="failed", plan=plan(NAV, QUERY), failure_code="challenge")
    with pytest.raises(flow.FlowError) as err:
        run(flow.start_demo(actor(), did, "failed"))
    assert err.value.code == "login_retry_blocked"


def test_missing_credential_is_auth_failed_without_running(world):
    world.credential = None
    did = new_draft(world, plan=plan(LOGIN, QUERY))
    assert demo(world, did).failure_code == "auth_failed" and world.executor.calls == []


def test_login_submit_is_not_auto_retried_on_transient(world):
    world.executor.script = {"로그인": [{"ok": False, "error": "net::ERR_CONNECTION_RESET"}]}
    did = new_draft(world, plan=plan(LOGIN, QUERY))
    assert demo(world, did).failure_code == "transient"
    assert [c["description"] for c in world.executor.calls].count("로그인") == 1


# ── 5. 시연 횟수·재시작 ────────────────────────────────────────────────────

def test_three_demos_per_draft(world):
    did = new_draft(world, state="credential_ready", plan=plan(NAV, QUERY), demo_attempts=2)
    body = run(flow.start_demo(actor(), did, "credential_ready"))
    assert body["draft"]["state"] == "demonstrating" and world.launched == [did]
    assert world.store.rows[did]["demo_attempts"] == 3
    world.store.set_state(did, "failed", failure_code="transient")
    with pytest.raises(flow.FlowError) as err:
        run(flow.start_demo(actor(), did, "failed"))
    assert err.value.code == "demo_limit_reached"


def test_resume_index_starts_from_last_successful_fragment():
    p = plan(LOGIN, NAV, QUERY)  # login 1-4, navigate 5, query 6-7
    assert runner.resume_index(p, set(), session_alive=True) == 0
    assert runner.resume_index(p, {1, 2, 3, 4}, session_alive=True) == 4  # 로그인 건너뜀
    assert runner.resume_index(p, {1, 2, 3, 4}, session_alive=False) == 0
    assert runner.resume_index(p, {1, 2, 3, 4, 5, 6}, session_alive=True) == 4  # 이동 조각부터 다시
    assert runner.resume_index(p, {1, 2, 3, 4, 5}, session_alive=False) == 0


def test_retry_after_failure_skips_login_when_session_alive(world):
    world.executor = FakeExecutor(alive=True)
    p = plan(LOGIN, NAV, QUERY)
    did = new_draft(world, plan=p)
    ref = {"plan": p, "completed": [1, 2, 3, 4], "credential_used": CRED}
    world.store.rows[did]["recording_ref"] = json.dumps(ref)
    assert demo(world, did).status == "done"
    assert "로그인" not in [c["description"] for c in world.executor.calls]
    assert [c["description"] for c in world.executor.calls][0] == "매출 관리"


def test_changed_credential_restarts_from_scratch(world):
    world.executor = FakeExecutor(alive=True)
    p = plan(LOGIN, NAV, QUERY)
    did = new_draft(world, plan=p)
    world.store.rows[did]["recording_ref"] = json.dumps({"plan": p, "completed": [1, 2, 3, 4], "credential_used": "old"})
    demo(world, did)
    assert [c["description"] for c in world.executor.calls][:2] == ["쿠팡이츠", "아이디"]


# ── 6. AC-04 이유별 재시연 ─────────────────────────────────────────────────

def test_ac04_wrong_date_changes_only_the_date_step_and_redemos_once(world):
    p = plan(LOGIN, NAV, QUERY)
    did = new_draft(world, plan=p)
    demo(world, did)
    run(flow.review(actor(), did, ok=False, reason_code="wrong_date"))
    assert row(world, did)["state"] == "revising"
    before = row(world, did)["recording_ref"]["plan"]
    body = run(flow.redemo(actor(), did, "wrong_date", {"date": "2026-10-12"}))
    assert body["draft"]["state"] == "demonstrating" and world.launched == [did]
    after = row(world, did)["recording_ref"]
    changed = [i for i, (a, b) in enumerate(zip(before, after["plan"])) if a != b]
    assert changed == [5] and after["plan"][5]["value"] == "2026-10-12"
    assert after["revised_seqs"] == [6] and after["completed"] == [1, 2, 3, 4, 5]
    world.executor = FakeExecutor(alive=True)
    assert demo(world, did).status == "done"
    calls = [(c["description"], c.get("value")) for c in world.executor.calls]
    assert ("조회 날짜", "2026-10-12") in calls and ("로그인", "Enter") not in calls


@pytest.mark.parametrize("reason,hint,code", [
    ("wrong_date", {}, "date_required"), ("wrong_date", {"date": "어제"}, "date_required"),
    ("wrong_menu", {}, "menu_required"), ("nope", {}, "reason_code_invalid"),
])
def test_redemo_rejects_bad_reason_or_hint(world, reason, hint, code):
    did = new_draft(world, state="revising", plan=plan(NAV, QUERY))
    with pytest.raises(flow.FlowError) as err:
        run(flow.redemo(actor(), did, reason, hint))
    assert err.value.code == code


def test_wrong_menu_only_changes_menu_step():
    p = plan(NAV, QUERY)
    p[0]["role"] = "menu"
    revised, changed = flow.revise_plan(p, "wrong_menu", {"menu": "정산 내역"})
    assert changed == [1] and revised[0]["label"] == "정산 내역" and revised[1:] == p[1:]


# ── 7. 조각 분할·재사용·등록 (AC-06) ───────────────────────────────────────

def test_domain_slug_and_names():
    assert splitter.domain_slug(DOMAIN) == "coupangeats"
    assert splitter.domain_slug("www.hometax.go.kr") == "hometax"
    assert splitter.domain_slug("wing.coupang.com") == "coupang"
    frags = splitter.split_steps([{"purpose": p, "step": {"action": "snapshot"}} for p in
                                  ("login", "login", "navigate", "query")])
    splitter.assign_names(frags, slug="coupangeats", used_numbers={1}, reusable_login=None)
    assert [f.name for f in frags] == ["coupangeats_02_login", "coupangeats_03_navigate", "coupangeats_04_query"]


def test_split_infers_purposes_without_tags():
    recorded = [{"step": s} for s in (
        {"action": "navigate", "url": "https://a.com"}, {"action": "fill", "value": "{{secret.username}}"},
        {"action": "fill", "value": "{{secret.password}}"}, {"action": "press", "value": "Enter"},
        {"action": "click", "selector": "a"}, {"action": "fill", "value": "2026-10-13"},
        {"action": "snapshot"},
    )]
    assert [f.purpose for f in splitter.split_steps(recorded)] == ["login", "navigate", "query"]


def _demo_then_confirm(world, *parts):
    did = new_draft(world, plan=plan(*parts))
    assert demo(world, did).status == "done"
    return did, run(flow.review(actor(), did, ok=True))


def test_review_ok_registers_three_fragments_for_a_new_site(world):
    did, body = _demo_then_confirm(world, LOGIN, NAV, QUERY)
    names = [r["name"] for r in world.registrations]
    assert names == ["coupangeats_01_login", "coupangeats_02_navigate", "coupangeats_03_query"]
    assert all(r["requested_by"] == ALICE for r in world.registrations)  # 사람, 채팅 세션 아님
    login = world.registrations[0]["spec"]
    assert login["metadata"]["credential_id"] == CRED and login["metadata"]["fragment_purpose"] == "login"
    assert world.registrations[2]["spec"]["metadata"]["requires"] == names[:2]
    assert world.registrations[2]["spec"]["metadata"]["screen_e2e"]["screen_verified"] is True
    assert body["draft"]["state"] == "pending_approval" and world.notified == names
    data = row(world, did)
    assert data["recipe_names"] == names and len(data["registration_ids"]) == 3
    assert data["evidence"]["screen_verified"] is True


def test_ac06_reuses_approved_login_fragment_of_same_credential(world):
    world.recipes = [{"name": "coupangeats_01_login", "domain": DOMAIN, "max_risk": "WRITE_EXTERNAL",
                      "spec": {"metadata": {"fragment_purpose": "login", "credential_id": CRED}}}]
    did, body = _demo_then_confirm(world, LOGIN, QUERY)
    assert [r["name"] for r in world.registrations] == ["coupangeats_02_query"]  # 새 등록은 조회 조각만
    assert row(world, did)["recipe_names"] == ["coupangeats_01_login", "coupangeats_02_query"]
    assert world.registrations[0]["spec"]["metadata"]["requires"] == ["coupangeats_01_login"]


def test_login_fragment_of_other_credential_is_not_reused(world):
    world.recipes = [{"name": "coupangeats_01_login", "domain": DOMAIN,
                      "spec": {"metadata": {"fragment_purpose": "login", "credential_id": "someone-else"}}}]
    _demo_then_confirm(world, LOGIN, QUERY)
    assert [r["name"] for r in world.registrations] == ["coupangeats_02_login", "coupangeats_03_query"]


def test_demo_plan_logs_in_with_reusable_login_fragment(world):
    world.login_fragment = {"name": "coupangeats_01_login", "spec": {"steps": [
        {"action": "navigate", "url": f"https://{DOMAIN}/login", "risk": "WRITE_EXTERNAL", "description": "열기"},
        {"action": "fill", "selector": "#pw", "value": "{{secret.password}}", "risk": "WRITE_EXTERNAL",
         "description": "비번"},
    ]}}
    did = new_draft(world)
    assert demo(world, did).status == "done"
    filled = [c for c in world.executor.calls if c["action"] == "fill"]
    assert filled[0]["value"] == PASSWORD  # 실행 직전에만 채워진다
    p = row(world, did)["recording_ref"]["plan"]
    assert p[0]["approved_fragment"] == "coupangeats_01_login" and p[-1]["action"] == "snapshot"


def test_review_without_recording_stays_registering(world):
    did = new_draft(world, state="result_review", plan=plan(NAV, QUERY))
    body = run(flow.review(actor(), did, ok=True))
    assert body["draft"]["state"] == "registering" and world.registrations == []


# ── 8. 읽기 전용 자동 승인 (D1) ────────────────────────────────────────────

HUMAN = {"domain": DOMAIN, "decided_by": "ceo@example.com"}


def _read_only_demo(world):
    return _demo_then_confirm(world, NAV, QUERY)


def test_auto_approves_read_only_with_same_domain_human_history(world, monkeypatch):
    monkeypatch.setenv(svc.AUTO_APPROVE_READ_FLAG, "true")
    world.approved_history = [HUMAN]
    did = new_draft(world, plan=plan(NAV, QUERY), credential=False)
    demo(world, did)
    body = run(flow.review(actor(), did, ok=True))
    assert [d["decided_by"] for d in world.decisions] == ["policy:read_same_domain"] * 2
    assert world.notified == [] and body["draft"]["state"] == "active"


@pytest.mark.parametrize("flag,history,plan_parts", [
    ("false", [HUMAN], (NAV, QUERY)),                                       # 플래그 꺼짐
    ("true", [], (NAV, QUERY)),                                             # 같은 도메인 이력 없음
    ("true", [{"domain": "other.com", "decided_by": "ceo@example.com"}], (NAV, QUERY)),
    ("true", [{"domain": DOMAIN, "decided_by": "policy:read_same_domain"}], (NAV, QUERY)),  # 정책 이력은 이력 아님
    ("true", [HUMAN], (NAV, SAVE, QUERY[1:])),                              # 쓰기 단계 포함
])
def test_auto_approve_needs_every_condition(world, monkeypatch, flag, history, plan_parts):
    monkeypatch.setenv(svc.AUTO_APPROVE_READ_FLAG, flag)
    world.approved_history = history
    did = new_draft(world, plan=plan(*plan_parts), credential=False)
    if demo(world, did).status == "paused":
        run(flow.risk_decision(actor(), did, "proceed"))
        demo(world, did)
    body = run(flow.review(actor(), did, ok=True))
    if plan_parts[1] is SAVE:
        assert [d["decided_by"] for d in world.decisions] == ["policy:read_same_domain"]  # 읽기 조각만
        assert world.notified == ["coupangeats_02_query"] and body["draft"]["state"] == "pending_approval"
    else:
        assert world.decisions == [] and body["draft"]["state"] == "pending_approval"


# ── 9. 비밀값 0건 ──────────────────────────────────────────────────────────

def test_no_secret_value_anywhere_after_full_flow(world):
    did, body = _demo_then_confirm(world, LOGIN, NAV, QUERY)
    assert any(c.get("value") == PASSWORD for c in world.executor.calls)  # 실행기에는 실제 값이 갔다
    stored = json.dumps([world.store.rows, world.store.events, world.registrations, world.emitted, world.frames,
                         body], ensure_ascii=False, default=str)
    assert PASSWORD not in stored and USERNAME not in stored
    recorded = row(world, did)["recording_ref"]["recorded"]
    assert recorded["3"]["step"]["value"] == "{{secret.password}}"
    assert recorded["2"]["step"]["value"] == "{{secret.username}}"
    inputs = {i["name"]: i for i in world.registrations[0]["spec"]["inputs"]}
    assert inputs["secret.password"]["secret"] is True
    assert svc.find_secret(row(world, did)["recording_ref"]) is None


def test_failed_run_does_not_store_secret_either(world):
    world.executor.script = {"비밀번호": [{"ok": False, "error": f"fill failed for {PASSWORD}"}]}
    did = new_draft(world, plan=plan(LOGIN, QUERY))
    demo(world, did)
    stored = json.dumps([world.store.rows, world.store.events, world.emitted], ensure_ascii=False, default=str)
    assert PASSWORD not in stored


def test_progress_sentences_have_no_forbidden_terms():
    forbidden = ("selector", "work_key", "lane", "recipe", "registration", "#", "input[")
    for step in plan(LOGIN, NAV, QUERY, SAVE) + [{"action": "download"}, {"action": "select", "label": "매장"}]:
        sentence = runner.describe(step, site_name="쿠팡이츠")
        assert all(term not in sentence for term in forbidden), sentence


# ── 9. 사용자 할 일(task) → 입력·클릭·제출 단계 (T3-A) ─────────────────────

SUBMIT_TASK = "피자 주문 폼에 이름 입력하고 주문 제출"
FORM_STEPS = [
    {"action": "fill", "selector": "input[name=custname]", "value": "홍길동", "label": "이름",
     "risk": "READ", "purpose": "query"},
    {"action": "click", "selector": "button >> text=Order", "label": "주문 제출", "risk": "READ", "purpose": "query"},
]


class Planner:
    """계획기(LLM) 대역. 화면 글을 받아 정해 둔 출력을 돌려준다."""

    def __init__(self, output: Any = None, *, error: Exception | None = None) -> None:
        self.output = json.dumps(output if output is not None else FORM_STEPS, ensure_ascii=False) \
            if not isinstance(output, str) else output
        self.error = error
        self.pages: list[str] = []

    async def __call__(self, intent: dict[str, Any], page: str) -> Any:
        self.pages.append(page)
        if self.error:
            raise self.error
        return self.output


def demo_planned(w: World, did: str, planner: Planner | None) -> runner.Outcome:
    deps = w.deps()
    deps.plan_steps = planner
    return run(runner.run_demo(did, tenant_id=TENANT_A, deps=deps))


def called(w: World) -> list[str]:
    return [str(c.get("description")) for c in w.executor.calls]


def test_default_deps_wire_the_planner():
    assert runner.default_deps().plan_steps is not None


def test_user_flow_plan_gets_submit_step_and_pauses_before_submitting(world, monkeypatch):
    async def ask(text, tenant_id):
        return {"task": SUBMIT_TASK, "output_kind": "screen"}

    async def find(*, tenant_id, domain):
        return [{"id": CRED, "display_name": "사장님 계정", "host": "httpbin.org"}]

    monkeypatch.setattr(flow.parser, "_ask_llm", ask)
    monkeypatch.setattr(flow, "list_recipes", lambda **kw: asyncio.sleep(0, result=[]))
    monkeypatch.setattr(flow.linker, "find_credentials", find)
    started = run(flow.start(actor(), "https://httpbin.org/forms/post 에서 피자 주문 폼에 이름 입력하고 주문 제출"))
    did = started["draft"]["id"]
    assert started["draft"]["intent"]["task"] == SUBMIT_TASK
    run(flow.confirm_intent(actor(), did, "intent_draft"))
    run(flow.choose_credential(actor(), did, credential_id=CRED))
    assert row(world, did)["state"] == "credential_ready"
    run(flow.start_demo(actor(), did, "credential_ready"))
    assert world.launched == [did] and row(world, did)["state"] == "demonstrating"

    planner = Planner()
    outcome = demo_planned(world, did, planner)
    assert (outcome.status, outcome.risk) == ("paused", RiskLevel.WRITE_EXTERNAL.value)
    data = row(world, did)
    assert data["state"] == "paused_risk"
    assert "주문 제출" not in called(world)  # 제출 단계는 실행기에 한 번도 가지 않았다
    assert "이름" in called(world)  # 앞선 입력은 했다
    plan_now = data["recording_ref"]["plan"]
    submit = next(s for s in plan_now if s.get("label") == "주문 제출")
    assert data["recording_ref"]["paused_seq"] == submit["seq"]
    assert submit["risk"] == "WRITE_EXTERNAL" and submit["purpose"] == "navigate"
    assert plan_now[-1]["action"] == "snapshot" and plan_now[-1]["seq"] == len(plan_now)
    assert data["recording_ref"]["plan_state"] == "expanded" and "plan_fallback" not in data["evidence"]
    assert len(planner.pages) == 1 and "row" in planner.pages[0]
    assert data["recording_ref"]["paused_label"] == "주문 제출"


def test_planner_risk_and_purpose_labels_cannot_lower_the_risk(world):
    did = new_draft(world, credential=False)
    steps = [{"action": "click", "selector": "button.go", "label": "제출", "risk": "READ", "purpose": "query"}]
    outcome = demo_planned(world, did, Planner(steps))
    assert outcome.status == "paused" and outcome.risk == RiskLevel.WRITE_EXTERNAL.value
    assert "제출" not in called(world)
    saved = row(world, did)["recording_ref"]["plan"]
    assert [s["purpose"] for s in saved][1:3] == ["navigate", "query"]
    assert saved[1]["risk"] == "WRITE_EXTERNAL"


def test_enter_key_after_typing_is_never_a_free_read(world):
    did = new_draft(world, credential=False)
    steps = [{"action": "fill", "selector": "#q", "value": "abc", "label": "검색"},
             {"action": "press", "selector": "#q", "value": "Enter", "label": "검색", "risk": "READ"}]
    outcome = demo_planned(world, did, Planner(steps))
    assert outcome.status == "paused" and outcome.seq == 3
    assert [c["action"] for c in world.executor.calls] == ["navigate", "snapshot", "fill"]


def test_read_only_task_steps_run_to_the_end(world):
    did = new_draft(world, credential=False)
    steps = [{"action": "fill", "selector": "#date", "value": "2026-10-13", "label": "조회 날짜", "role": "date"},
             {"action": "click", "selector": "button.search", "label": "조회"}]
    assert demo_planned(world, did, Planner(steps)).status == "done"
    assert called(world)[-1] == "결과" and "조회" in called(world)


def test_redemo_keeps_write_steps_and_their_risk(world):
    did = new_draft(world, credential=False)
    steps = [{"action": "click", "selector": "a.menu", "label": "제출", "role": "menu"},
             {"action": "fill", "selector": "#date", "value": "2026-10-13", "label": "조회 날짜", "role": "date"},
             {"action": "click", "selector": "button.go", "label": "보내기"}]
    planner = Planner(steps)
    assert demo_planned(world, did, planner).status == "paused"
    world.store.set_state(did, "revising")
    run(flow.redemo(actor(), did, "wrong_menu", {"menu": "정산 내역"}))
    plan_menu = row(world, did)["recording_ref"]["plan"]
    assert plan_menu[1]["label"] == "정산 내역" and plan_menu[1]["risk"] == "WRITE_EXTERNAL"
    assert plan_menu[3]["risk"] == "WRITE_EXTERNAL"
    world.executor.calls.clear()
    assert demo_planned(world, did, planner).status == "paused"
    assert len(planner.pages) == 1  # 같은 계획을 쓴다 — 다시 짜지 않는다
    assert "보내기" not in called(world) and 'text="정산 내역"' not in [c.get("selector") for c in world.executor.calls]

    world.store.set_state(did, "revising")
    run(flow.redemo(actor(), did, "wrong_date", {"date": "2026-10-12"}))
    plan_date = row(world, did)["recording_ref"]["plan"]
    assert plan_date[2]["value"] == "2026-10-12"
    assert [s["risk"] for s in plan_date if s["label"] in ("정산 내역", "보내기")] == ["WRITE_EXTERNAL"] * 2
    world.executor.calls.clear()
    assert demo_planned(world, did, planner).status == "paused"
    assert "보내기" not in called(world)


def test_no_automatic_approval_after_pause(world):
    did = new_draft(world, credential=False)
    planner = Planner([{"action": "click", "selector": "button.go", "label": "제출"}])
    assert demo_planned(world, did, planner).status == "paused"
    world.executor.calls.clear()
    assert demo_planned(world, did, planner).status == "skipped"  # paused_risk 상태에서는 아무것도 하지 않는다
    world.store.set_state(did, "demonstrating")  # 사람의 [진행] 없이 상태만 되돌린 경우
    assert demo_planned(world, did, planner).status == "paused"
    assert "제출" not in called(world) and len(planner.pages) == 1
    assert row(world, did)["recording_ref"].get("approved_risk") in (None, {})


def test_submit_runs_only_after_explicit_proceed(world):
    did = new_draft(world, credential=False)
    planner = Planner([{"action": "click", "selector": "button.go", "label": "제출"}])
    demo_planned(world, did, planner)
    run(flow.risk_decision(actor(), did, "proceed"))
    world.executor.calls.clear()
    assert demo_planned(world, did, planner).status == "done"
    assert called(world).count("제출") == 1


FALLBACK_CASES = [
    ("planner_raises", Planner(error=RuntimeError("llm down")), "planner_unavailable"),
    ("no_output", Planner("") , "invalid_schema"),
    ("not_json", Planner("죄송합니다"), "invalid_schema"),
    ("empty_list", Planner([]), "planner_empty"),
    ("navigate_not_allowed", Planner([{"action": "navigate", "selector": "x", "url": "https://evil.example"}]),
     "invalid_schema"),
    ("missing_selector", Planner([{"action": "click", "label": "x"}]), "invalid_schema"),
    ("fill_without_value", Planner([{"action": "fill", "selector": "#a"}]), "invalid_schema"),
    ("bad_key", Planner([{"action": "press", "selector": "#a", "value": "Control+Alt+Delete"}]), "invalid_schema"),
    ("bad_role", Planner([{"action": "click", "selector": "#a", "role": "date"}]), "invalid_schema"),
    ("secret_reference", Planner([{"action": "fill", "selector": "#a", "value": "{{secret.password}}"}]),
     "unsafe_value"),
    ("too_many", Planner([{"action": "click", "selector": "#a"}] * 30), "too_many_steps"),
]


@pytest.mark.parametrize("name,planner,reason", FALLBACK_CASES, ids=[c[0] for c in FALLBACK_CASES])
def test_planner_failure_falls_back_to_navigate_and_snapshot(world, name, planner, reason):
    planner.pages.clear()
    did = new_draft(world, credential=False)
    assert demo_planned(world, did, planner).status == "done"
    data = row(world, did)
    assert [s["action"] for s in data["recording_ref"]["plan"]] == ["navigate", "snapshot"]
    assert data["evidence"]["plan_fallback"] == reason and data["recording_ref"]["plan_state"] == "fallback"
    assert [c["action"] for c in world.executor.calls] == ["navigate", "snapshot", "snapshot"]


def test_missing_planner_or_task_is_a_fallback_not_an_error(world):
    did = new_draft(world, credential=False)
    assert demo_planned(world, did, None).status == "done"
    assert "plan_state" not in row(world, did)["recording_ref"]


def test_secret_in_task_is_never_sent_to_the_planner(world):
    did = new_draft(world, credential=False)
    world.store.rows[did]["intent"] = json.dumps({**json.loads(world.store.rows[did]["intent"]),
                                                  "task": f"주문 제출 키 {SK_LIKE}"})
    planner = Planner()
    assert demo_planned(world, did, planner).status == "done"
    assert planner.pages == [] and row(world, did)["evidence"]["plan_fallback"] == "task_has_secret"


def test_screen_probe_failure_falls_back(world):
    world.executor.script = {"화면 확인": [{"ok": False, "error": "timeout"}]}
    did = new_draft(world, credential=False)
    planner = Planner()
    assert demo_planned(world, did, planner).status == "done"
    assert planner.pages == [] and row(world, did)["evidence"]["plan_fallback"] == "snapshot_failed"


def test_login_password_typed_by_the_planner_is_rejected(world):
    did = new_draft(world)
    steps = [{"action": "fill", "selector": "#a", "value": PASSWORD, "label": "메모"}]
    assert demo_planned(world, did, Planner(steps)).status == "done"
    data = row(world, did)
    assert data["evidence"]["plan_fallback"] == "unsafe_value" and PASSWORD not in json.dumps(data, default=str)


def test_task_steps_never_carry_secret_values_into_the_stored_plan(world):
    did = new_draft(world)
    steps = [{"action": "fill", "selector": "#a", "value": "홍길동", "label": "이름"},
             {"action": "click", "selector": "button.go", "label": "제출"}]
    demo_planned(world, did, Planner(steps))
    blob = json.dumps(row(world, did), ensure_ascii=False, default=str)
    assert PASSWORD not in blob
    assert "{{secret.password}}" in blob  # 로그인 값은 참조로만 남는다


def test_parse_task_steps_drops_planner_markings_and_floors_risk():
    steps, reason = runner.parse_task_steps(
        '```json\n[{"action":"fill","selector":"#n","value":"a","label":"이름","purpose":"login",'
        '"login_submit":true,"approved_fragment":"x","credential_field":"password"},'
        '{"action":"click","selector":"button","label":"다음","risk":"READ"}]\n```',
        task=SUBMIT_TASK, domain="httpbin.org",
    )
    assert reason == ""
    assert set(steps[0]) == {"action", "selector", "value", "label", "purpose"} and steps[0]["purpose"] == "navigate"
    assert steps[1]["risk"] == "WRITE_EXTERNAL"  # 사용자 문장이 '제출' — 입력 뒤 누르기의 하한
    assert runner.parse_task_steps({"steps": FORM_STEPS}, task="조회", domain="httpbin.org")[1] == ""


def test_default_planner_uses_central_llm_client(monkeypatch):
    seen: dict[str, Any] = {}

    async def fake_call(prompt, **kwargs):
        seen.update(prompt=prompt, **kwargs)
        return "[]"

    from app.core import anthropic_client

    monkeypatch.setattr(anthropic_client, "call_llm_with_fallback", fake_call)
    out = run(runner._default_plan_steps({"site_name": "httpbin", "task": SUBMIT_TASK}, "- textbox \"name\""))
    assert out == "[]" and SUBMIT_TASK in seen["prompt"] and "textbox" in seen["prompt"]
