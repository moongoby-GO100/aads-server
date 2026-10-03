"""세션별 능동 추론 강도: 정책·세션 독립·공급사 body 증거·CLI ack·보류 변경. 공급사 호출 없음."""
import asyncio
import json
import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from app.services import session_effort as se


class MemStore:
    """asyncpg 없이 PgEffortStore 와 같은 계약을 지키는 인메모리 저장소."""

    def __init__(self):
        self.sessions = {}      # session_id -> {"tenant", "mode", "manual", "pending"}
        self.executions = {}    # execution_id -> dict(session_id, status, error_message, effort, running)
        self.order = []

    def add_session(self, tenant="t1"):
        sid = str(uuid.uuid4())
        self.sessions[sid] = {"tenant": tenant, "mode": "auto", "manual": None, "pending": None}
        return sid

    def add_execution(self, sid, *, status="running", error=None, effort=None):
        eid = str(uuid.uuid4())
        self.executions[eid] = {
            "session_id": sid, "status": status, "error_message": error, "effort": effort,
            "completed": status not in ("running", "retrying"),
        }
        self.order.append(eid)
        return eid

    @staticmethod
    def _setting(s):
        return {"effort_mode": s["mode"], "effort_manual": s["manual"], "effort_pending": s["pending"]}

    async def get_session_setting(self, session_id):
        s = self.sessions.get(session_id)
        return self._setting(s) if s else None

    async def promote_pending(self, session_id):
        s = self.sessions.get(session_id)
        if not s:
            return None
        if s["pending"]:
            s["mode"] = s["pending"]["mode"]
            s["manual"] = s["pending"]["level"] or None
            s["pending"] = None
        return self._setting(s)

    async def has_running_execution(self, session_id):
        return any(
            e["session_id"] == session_id and e["status"] in ("running", "retrying") and not e["completed"]
            for e in self.executions.values()
        )

    async def set_session_setting(self, session_id, tenant_id, mode, level, *, pending):
        s = self.sessions.get(session_id)
        if not s or s["tenant"] != tenant_id:
            return None
        if pending:
            s["pending"] = {"mode": mode, "level": level or "", "requested_at": "now"}
        else:
            s["mode"], s["manual"], s["pending"] = mode, level, None
        return self._setting(s)

    async def get_execution_record(self, execution_id):
        return (self.executions.get(execution_id) or {}).get("effort")

    async def save_execution_record(self, execution_id, record):
        self.executions[execution_id]["effort"] = json.loads(json.dumps(record))

    async def recent_executions(self, session_id, exclude_execution_id, limit):
        out = []
        for eid in reversed(self.order):
            e = self.executions[eid]
            if e["session_id"] != session_id or eid == exclude_execution_id:
                continue
            out.append({"id": eid, "status": e["status"], "error_message": e["error_message"], "effort": e["effort"]})
        return out[:limit]

    async def latest_execution_record(self, session_id):
        for eid in reversed(self.order):
            e = self.executions[eid]
            if e["session_id"] == session_id and e["effort"]:
                return e["effort"]
        return None


# ── 자동 정책 ────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("intent,content,expected", [
    ("status_check", "지금 진행 상황 알려줘", "medium"),
    ("casual", "안녕", "medium"),
    ("diagnosis", "이 오류 원인 분석해줘", "high"),
    ("code_modify", "이 함수 수정해줘", "high"),
    ("design", "새 결제 모듈 설계해줘", "xhigh"),
    ("cto_verify", "핵심 검증 돌려줘", "xhigh"),
    ("greeting", "전체 구조 아키텍처 검토해줘", "xhigh"),
    ("greeting", "서버 로그에서 버그 원인 찾아줘", "high"),
])
def test_auto_policy_levels(intent, content, expected):
    level, reason = se.decide_auto_effort(intent=intent, content=content)
    assert level == expected
    assert reason and len(reason) < 80


def test_auto_policy_repeated_error_raises_to_xhigh():
    level, reason = se.decide_auto_effort(intent="status_check", content="다시", repeated_errors=se.REPEAT_ERROR_THRESHOLD)
    assert level == "xhigh"
    assert str(se.REPEAT_ERROR_THRESHOLD) in reason
    below, _ = se.decide_auto_effort(intent="status_check", content="다시", repeated_errors=se.REPEAT_ERROR_THRESHOLD - 1)
    assert below == "medium"


def test_auto_policy_follow_up_lowers_when_difficulty_drops():
    first, _ = se.decide_auto_effort(intent="design", content="결제 모듈 설계")
    follow, _ = se.decide_auto_effort(intent="status_check", content="진행 상황 알려줘", previous_level=first)
    assert (first, follow) == ("xhigh", "medium")


def test_auto_policy_continue_keeps_previous_level():
    level, _ = se.decide_auto_effort(intent="casual", content="계속", previous_level="high")
    assert level == "high"


def test_repeated_error_count_requires_same_signature_and_stops_at_success():
    err = lambda m, st="failed": {"status": st, "error_message": m}  # noqa: E731
    assert se.count_repeated_errors([err("timeout 1"), err("timeout 2"), err("timeout 3")]) == 3
    assert se.count_repeated_errors([err("timeout 1"), err("db down"), err("timeout 3")]) == 1
    assert se.count_repeated_errors([err("timeout 1"), err("x", "completed"), err("timeout 3")]) == 1
    assert se.count_repeated_errors([err("", "failed")]) == 0


# ── 공급사별 지원 범위·clamp ────────────────────────────────────────────────

def test_clamp_picks_nearest_supported_and_says_so():
    value, note = se.clamp_to_supported("xhigh", ("low", "medium", "high", "max"))
    assert value in ("high", "max") and "xhigh" in note and value in note
    assert se.clamp_to_supported("high", ("low", "medium", "high")) == ("high", "")
    assert se.clamp_to_supported("medium", ())[0] is None


def test_support_tables_by_model():
    assert "xhigh" in se.supported_levels(se.PATH_ANTHROPIC, "claude-opus-5-5")
    assert "xhigh" not in se.supported_levels(se.PATH_ANTHROPIC, "claude-opus-4-6")
    assert se.supported_levels(se.PATH_ANTHROPIC, "claude-haiku-4-5-20251001") == ()
    assert "xhigh" in se.supported_levels(se.PATH_OPENAI_CHAT, "gpt-6-sol")
    assert se.supported_levels(se.PATH_OPENAI_CHAT, "gemini-flash") == ()


def test_openai_tools_force_none_and_record_says_so():
    d = se.EffortDecision(mode="manual", requested_effort="high", reason="r")
    res = se.resolve_for_provider(d, path=se.PATH_OPENAI_CHAT, model="gpt-6-sol", has_tools=True)
    assert res.value == "none" and res.apply_path == se.APPLY_DIRECT_API and "none" in res.note
    res = se.resolve_for_provider(d, path=se.PATH_OPENAI_CHAT, model="gpt-6-sol", has_tools=False)
    assert res.value == "high"


def test_clamp_on_opus46_xhigh_and_revalidate_on_model_swap():
    d = se.EffortDecision(mode="manual", requested_effort="xhigh", reason="r")
    on_new = se.resolve_for_provider(d, path=se.PATH_ANTHROPIC, model="claude-opus-5-5")
    on_old = se.resolve_for_provider(d, path=se.PATH_ANTHROPIC, model="claude-opus-4-6")
    on_haiku = se.resolve_for_provider(d, path=se.PATH_ANTHROPIC, model="claude-haiku-4-5-20251001")
    assert on_new.value == "xhigh" and not on_new.note
    assert on_old.value != "xhigh" and "xhigh" in on_old.note
    assert on_haiku.value is None and on_haiku.apply_path == se.APPLY_UNSUPPORTED


# ── 세션 설정: 독립·테넌트·보류 ──────────────────────────────────────────────

def test_validate_setting_rules():
    assert se.validate_setting("auto", None) == ("auto", None)
    assert se.validate_setting("manual", "high") == ("manual", "high")
    for bad in (("manual", None), ("manual", "low"), ("manual", "max"), ("auto", "high"), ("turbo", None)):
        with pytest.raises(se.EffortSettingError):
            se.validate_setting(*bad)


@pytest.mark.asyncio
async def test_put_changes_only_target_session_and_enforces_tenant():
    store = MemStore()
    a, b = store.add_session("t1"), store.add_session("t1")
    out = await se.update_session_setting(session_id=a, tenant_id="t1", mode="manual", level="xhigh", store=store)
    assert out["mode"] == "manual" and out["level"] == "xhigh" and out["status"] == se.SETTING_APPLIED
    assert store.sessions[b]["mode"] == "auto" and store.sessions[b]["manual"] is None
    assert await se.update_session_setting(session_id=a, tenant_id="other", mode="auto", level=None, store=store) is None
    assert store.sessions[a]["mode"] == "manual"


@pytest.mark.asyncio
async def test_change_during_running_execution_is_pending_and_not_retroactive():
    store = MemStore()
    sid = store.add_session()
    running = store.add_execution(sid)
    d1 = await se.begin_request(session_id=sid, execution_id=running, intent="status_check", content="진행 상황", store=store)
    assert d1.mode == "auto" and d1.requested_effort == "medium"

    out = await se.update_session_setting(session_id=sid, tenant_id="t1", mode="manual", level="xhigh", store=store)
    assert out["status"] == se.SETTING_PENDING and out["pending"]["level"] == "xhigh"
    assert store.sessions[sid]["mode"] == "auto"  # 현재 설정은 그대로

    # 같은 실행의 재호출(추가지시 등)은 기록을 재사용하고 보류 변경을 소비하지 않는다.
    again = await se.begin_request(session_id=sid, execution_id=running, intent="design", content="설계", store=store)
    assert again.requested_effort == "medium" and store.sessions[sid]["pending"] is not None

    store.executions[running].update(status="completed", completed=True)
    nxt = store.add_execution(sid)
    d2 = await se.begin_request(session_id=sid, execution_id=nxt, intent="status_check", content="상태", store=store)
    assert (d2.mode, d2.requested_effort, d2.reason) == ("manual", "xhigh", "사용자 수동 고정")
    assert store.sessions[sid]["pending"] is None and store.sessions[sid]["mode"] == "manual"


@pytest.mark.asyncio
async def test_manual_stays_until_returned_to_auto():
    store = MemStore()
    sid = store.add_session()
    await se.update_session_setting(session_id=sid, tenant_id="t1", mode="manual", level="medium", store=store)
    for intent in ("design", "diagnosis", "status_check"):
        eid = store.add_execution(sid, status="completed")
        d = await se.begin_request(session_id=sid, execution_id=eid, intent=intent, content="x", store=store)
        assert (d.mode, d.requested_effort) == ("manual", "medium")
    await se.update_session_setting(session_id=sid, tenant_id="t1", mode="auto", level=None, store=store)
    eid = store.add_execution(sid, status="completed")
    d = await se.begin_request(session_id=sid, execution_id=eid, intent="design", content="아키텍처", store=store)
    assert (d.mode, d.requested_effort) == ("auto", "xhigh")


@pytest.mark.asyncio
async def test_two_sessions_decide_independently():
    store = MemStore()
    a, b = store.add_session(), store.add_session()
    await se.update_session_setting(session_id=a, tenant_id="t1", mode="manual", level="xhigh", store=store)
    da = await se.begin_request(session_id=a, execution_id=store.add_execution(a), intent="casual", content="안녕", store=store)
    db = await se.begin_request(session_id=b, execution_id=store.add_execution(b), intent="casual", content="안녕", store=store)
    assert (da.requested_effort, db.requested_effort) == ("xhigh", "medium")


@pytest.mark.asyncio
async def test_repeated_identical_errors_escalate_in_auto_mode():
    store = MemStore()
    sid = store.add_session()
    for i in range(se.REPEAT_ERROR_THRESHOLD):
        store.add_execution(sid, status="failed", error=f"relay timeout after {600 + i}s")
    d = await se.begin_request(session_id=sid, execution_id=store.add_execution(sid), intent="status_check", content="다시 해줘", store=store)
    assert d.requested_effort == "xhigh" and "반복" in d.reason


@pytest.mark.asyncio
async def test_store_failure_never_blocks_chat():
    store = MemStore()
    store.promote_pending = AsyncMock(side_effect=RuntimeError("db down"))
    sid = store.add_session()
    assert await se.begin_request(session_id=sid, execution_id=None, intent="x", content="y", store=store) is None
    assert await se.begin_request(session_id=None, execution_id=None, intent="x", content="y", store=store) is None


# ── 실행별 기록·적용 증거 ────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_record_has_required_fields_and_no_hidden_reasoning():
    store = MemStore()
    sid = store.add_session()
    eid = store.add_execution(sid)
    d = await se.begin_request(session_id=sid, execution_id=eid, intent="design", content="설계", store=store)
    res = se.resolve_for_provider(d, path=se.PATH_ANTHROPIC, model="claude-opus-5-5")
    ev = await se.record_applied(d, path=se.PATH_ANTHROPIC, model="claude-opus-5-5", resolution=res, sent="xhigh", store=store)
    assert ev["type"] == "effort_status"
    for key in ("requested_effort", "effective_effort", "mode", "policy_version", "reason", "applied_at", "apply_path"):
        assert key in ev
    assert ev["policy_version"] == se.POLICY_VERSION and ev["apply_path"] == se.APPLY_DIRECT_API
    assert "thinking" not in ev and "attempts" not in ev
    saved = store.executions[eid]["effort"]
    assert saved["effective_effort"] == "xhigh" and saved["state"] == "applied"


@pytest.mark.asyncio
async def test_unsent_value_is_never_marked_applied():
    d = se.EffortDecision(mode="auto", requested_effort="high", reason="r")
    res = se.resolve_for_provider(d, path=se.PATH_ANTHROPIC, model="claude-opus-5-5")
    ev = await se.record_applied(d, path=se.PATH_ANTHROPIC, model="claude-opus-5-5", resolution=res, sent=None, store=MemStore())
    assert ev["apply_path"] == se.APPLY_UNSUPPORTED and ev["effective_effort"] is None and ev["state"] == "unsupported"


@pytest.mark.asyncio
async def test_same_request_repeated_is_not_rerecorded_but_model_swap_is():
    store = MemStore()
    sid = store.add_session()
    eid = store.add_execution(sid)
    d = await se.begin_request(session_id=sid, execution_id=eid, intent="design", content="설계", store=store)
    kw = dict(path=se.PATH_ANTHROPIC, store=store)
    r_new = se.resolve_for_provider(d, path=se.PATH_ANTHROPIC, model="claude-opus-5-5")
    assert await se.record_applied(d, model="claude-opus-5-5", resolution=r_new, sent="xhigh", **kw)
    assert await se.record_applied(d, model="claude-opus-5-5", resolution=r_new, sent="xhigh", **kw) is None
    r_old = se.resolve_for_provider(d, path=se.PATH_ANTHROPIC, model="claude-opus-4-6")
    ev = await se.record_applied(d, model="claude-opus-4-6", resolution=r_old, sent=r_old.value, **kw)
    assert ev and ev["effective_effort"] == r_old.value != "xhigh" and "xhigh" in ev["reason"]
    assert len(d.attempts) == 2


@pytest.mark.asyncio
async def test_finalize_marks_unreported_path_unsupported_once():
    d = se.EffortDecision(mode="auto", requested_effort="medium", reason="r")
    store = MemStore()
    sid = store.add_session()
    d.execution_id = store.add_execution(sid)
    ev = await se.finalize_unreported(d, "gemini-flash", store=store)
    assert ev["apply_path"] == se.APPLY_UNSUPPORTED
    assert await se.finalize_unreported(d, "gemini-flash", store=store) is None


@pytest.mark.asyncio
async def test_session_view_exposes_setting_and_latest_record():
    store = MemStore()
    sid = store.add_session()
    await se.update_session_setting(session_id=sid, tenant_id="t1", mode="manual", level="high", store=store)
    eid = store.add_execution(sid)
    d = await se.begin_request(session_id=sid, execution_id=eid, intent="x", content="y", store=store)
    await se.record_applied(
        d, path=se.PATH_ANTHROPIC, model="claude-opus-5-5",
        resolution=se.resolve_for_provider(d, path=se.PATH_ANTHROPIC, model="claude-opus-5-5"),
        sent="high", store=store,
    )
    view = await se.session_view(sid, store=store)
    assert view["mode"] == "manual" and view["level"] == "high"
    assert view["latest_execution"]["effective_effort"] == "high" and "attempts" not in view["latest_execution"]


# ── 공급사 요청 body 증거 (model_selector) ───────────────────────────────────

class _Stop(BaseException):
    """공급사 호출 직전에 생성기를 끝내기 위한 신호."""


async def _drain(gen):
    events = []
    try:
        async for ev in gen:
            events.append(ev)
    except _Stop:
        pass
    return events


def _anthropic_intent(use_thinking=True):
    return SimpleNamespace(
        intent="design", model="claude-sonnet-5-5", use_tools=False, tool_group="",
        use_extended_thinking=use_thinking,
    )


async def _run_anthropic(monkeypatch, decision, *, alias="claude-sonnet-5-5", use_thinking=True):
    from app.services import model_selector as ms

    captured = {}

    def _stream(**kwargs):
        captured.update(kwargs)
        raise _Stop()

    monkeypatch.setattr(ms, "_anthropic", SimpleNamespace(messages=SimpleNamespace(stream=_stream)))
    monkeypatch.setattr(ms, "_EXTENDED_THINKING_ENABLED", True)
    token = se.set_current_decision(decision)
    try:
        events = await _drain(ms._stream_anthropic(
            _anthropic_intent(use_thinking), alias, "sys", [{"role": "user", "content": "hi"}], None,
        ))
    finally:
        se.reset_current_decision(token)
    return captured, events


@pytest.mark.asyncio
@pytest.mark.parametrize("level", ["medium", "high", "xhigh"])
async def test_anthropic_request_body_carries_effort_and_event_matches(monkeypatch, level):
    d = se.EffortDecision(mode="manual", requested_effort=level, reason="사용자 수동 고정")
    body, events = await _run_anthropic(monkeypatch, d)
    assert body["output_config"] == {"effort": level}
    ev = [e for e in events if e.get("type") == "effort_status"][-1]
    assert ev["effective_effort"] == body["output_config"]["effort"] == level
    assert ev["apply_path"] == se.APPLY_DIRECT_API


@pytest.mark.asyncio
async def test_anthropic_effort_sent_even_without_thinking(monkeypatch):
    d = se.EffortDecision(mode="auto", requested_effort="medium", reason="r")
    body, events = await _run_anthropic(monkeypatch, d, use_thinking=False)
    assert body["output_config"] == {"effort": "medium"} and "thinking" not in body
    assert any(e.get("type") == "effort_status" for e in events)


@pytest.mark.asyncio
async def test_anthropic_without_decision_keeps_legacy_behavior(monkeypatch):
    body, events = await _run_anthropic(monkeypatch, None)
    assert body["output_config"] == {"effort": "xhigh"}
    assert not [e for e in events if e.get("type") == "effort_status"]
    body, _ = await _run_anthropic(monkeypatch, None, use_thinking=False)
    assert "output_config" not in body


def test_openai_body_gets_reasoning_effort_and_tools_force_none():
    from app.services import model_selector as ms

    d = se.EffortDecision(mode="manual", requested_effort="high", reason="r")
    no_tools = se.resolve_for_provider(d, path=se.PATH_OPENAI_CHAT, model="gpt-6-sol", has_tools=False)
    body = ms._prepare_openai_chat_request(
        {"model": "gpt-6-sol", "max_tokens": 10, "temperature": 0.2}, "gpt-6-sol", direct=True, requested_effort=no_tools.value,
    )
    assert body["reasoning_effort"] == "high" and "temperature" not in body
    with_tools = se.resolve_for_provider(d, path=se.PATH_OPENAI_CHAT, model="gpt-6-sol", has_tools=True)
    body = ms._prepare_openai_chat_request(
        {"model": "gpt-6-sol", "max_tokens": 10, "tools": [{"type": "function"}]}, "gpt-6-sol", direct=True,
        requested_effort=with_tools.value,
    )
    assert body["reasoning_effort"] == "none"


def _fake_http_client(lines, captured):
    class _Resp:
        status_code = 200

        async def aiter_lines(self):
            for line in lines:
                yield line

        async def aread(self):
            return b""

    class _Stream:
        async def __aenter__(self):
            return _Resp()

        async def __aexit__(self, *exc):
            return False

    client = AsyncMock()
    client.get.return_value = Mock(status_code=200, json=lambda: {})

    def _stream(method, url, **kw):
        captured["url"], captured["json"] = url, kw.get("json")
        return _Stream()

    client.stream = _stream
    factory = Mock()
    factory.return_value.__aenter__ = AsyncMock(return_value=client)
    factory.return_value.__aexit__ = AsyncMock(return_value=False)
    return factory


@pytest.mark.asyncio
async def test_openai_stream_records_value_actually_in_request_body(monkeypatch):
    from app.services import model_selector as ms

    captured = {}
    monkeypatch.setattr(ms.httpx, "AsyncClient", _fake_http_client(["data: [DONE]"], captured))
    d = se.EffortDecision(mode="manual", requested_effort="xhigh", reason="r")
    token = se.set_current_decision(d)
    try:
        events = await _drain(ms._stream_litellm_openai(
            "gpt-6-sol", "sys", [{"role": "user", "content": "hi"}], None,
            base_url="https://api.openai.com/v1", api_key="test",
        ))
    finally:
        se.reset_current_decision(token)
    ev = [e for e in events if e.get("type") == "effort_status"][-1]
    assert captured["json"]["reasoning_effort"] == "xhigh" == ev["effective_effort"]
    assert ev["apply_path"] == se.APPLY_DIRECT_API


@pytest.mark.asyncio
async def test_non_effort_model_on_openai_path_is_unsupported_not_applied(monkeypatch):
    from app.services import model_selector as ms

    captured = {}
    monkeypatch.setattr(ms.httpx, "AsyncClient", _fake_http_client(["data: [DONE]"], captured))
    d = se.EffortDecision(mode="manual", requested_effort="xhigh", reason="r")
    token = se.set_current_decision(d)
    try:
        events = await _drain(ms._stream_litellm_openai(
            "deepseek-chat", "sys", [{"role": "user", "content": "hi"}], None,
            base_url="http://litellm.local", api_key="test",
        ))
    finally:
        se.reset_current_decision(token)
    ev = [e for e in events if e.get("type") == "effort_status"][-1]
    assert ev["apply_path"] == se.APPLY_UNSUPPORTED and ev["effective_effort"] is None


# ── CLI 릴레이: 다음 실행 인자 + ack ────────────────────────────────────────

def _relay_client(lines, captured):
    from scripts.claude_model_contract import CONTRACT_VERSION

    factory = _fake_http_client(lines, captured)
    client = factory.return_value.__aenter__.return_value
    client.get.return_value = Mock(
        status_code=200, json=lambda: {"claude_model_contract": {"version": CONTRACT_VERSION}},
    )
    return factory


async def _run_cli(monkeypatch, lines, decision, model="claude-opus"):
    from app.services import model_selector as ms

    captured = {}
    monkeypatch.setattr(ms.httpx, "AsyncClient", _relay_client(lines, captured))
    monkeypatch.setattr(ms, "_load_relay_shared_secret", lambda: "s")
    token = se.set_current_decision(decision)
    try:
        events = await _drain(ms._stream_cli_relay_once(model, "sys", [{"role": "user", "content": "hi"}]))
    finally:
        se.reset_current_decision(token)
    return captured, events


@pytest.mark.asyncio
async def test_cli_relay_sends_effort_argument_and_records_cli_next_run_only_after_ack(monkeypatch):
    d = se.EffortDecision(mode="manual", requested_effort="high", reason="r")
    lines = [json.dumps({"type": "effort_ack", "effort": "high"}), json.dumps({"type": "heartbeat"})]
    captured, events = await _run_cli(monkeypatch, lines, d)
    assert captured["json"]["effort"] == "high"
    ev = [e for e in events if e.get("type") == "effort_status"]
    assert len(ev) == 1 and ev[0]["apply_path"] == se.APPLY_CLI_NEXT_RUN and ev[0]["effective_effort"] == "high"
    assert not [e for e in events if e.get("type") == "effort_ack"]


@pytest.mark.asyncio
async def test_old_relay_without_ack_is_never_marked_applied(monkeypatch):
    d = se.EffortDecision(mode="manual", requested_effort="high", reason="r")
    lines = [json.dumps({"type": "system", "subtype": "init", "session_id": "x"})]
    _, events = await _run_cli(monkeypatch, lines, d)
    ev = [e for e in events if e.get("type") == "effort_status"]
    assert len(ev) == 1 and ev[0]["apply_path"] == se.APPLY_UNSUPPORTED and ev[0]["effective_effort"] is None
    assert "확인하지 않음" in ev[0]["reason"]


@pytest.mark.asyncio
async def test_cli_unsupported_model_sends_no_effort_argument(monkeypatch):
    d = se.EffortDecision(mode="manual", requested_effort="high", reason="r")
    captured, events = await _run_cli(monkeypatch, [], d, model="claude-haiku")
    assert "effort" not in captured.get("json", {"effort": None}) or captured["json"].get("effort") is None
    ev = [e for e in events if e.get("type") == "effort_status"]
    assert ev and ev[0]["apply_path"] == se.APPLY_UNSUPPORTED


def test_relay_normalizes_effort_and_passes_only_valid_values():
    from scripts import claude_relay_server as relay

    assert relay._normalize_effort("HIGH ", relay._CLAUDE_EFFORT_VALUES) == "high"
    assert relay._normalize_effort("max", relay._CLAUDE_EFFORT_VALUES) == "max"
    assert relay._normalize_effort("max", relay._CODEX_EFFORT_VALUES) == ""
    assert relay._normalize_effort("bogus; rm -rf /", relay._CLAUDE_EFFORT_VALUES) == ""
    assert relay._normalize_effort(None, relay._CLAUDE_EFFORT_VALUES) == ""


@pytest.mark.asyncio
async def test_relay_launches_claude_with_effort_flag_and_acks_after_spawn(monkeypatch):
    from scripts import claude_relay_server as relay

    proc = Mock(pid=123, returncode=0, stderr=None)
    proc.stdin.drain = AsyncMock()
    launch = AsyncMock(return_value=proc)
    monkeypatch.setattr(asyncio, "create_subprocess_exec", launch)
    for name, value in (
        ("_DIRECT_OAUTH_ENABLED", False), ("_semaphore", asyncio.Semaphore(15)), ("_session_map", {}),
    ):
        monkeypatch.setattr(relay, name, value)
    monkeypatch.setattr(relay, "_resolve_cli_command", lambda *_: {"mode": "test"})
    monkeypatch.setattr(relay, "_preflight_cli_command", lambda *_: {"ok": True})
    monkeypatch.setattr(relay, "_load_mcp_template", lambda *_: {"mcpServers": {}})
    monkeypatch.setattr(relay, "_resolve_aads_tools_cfg", AsyncMock(return_value=({"command": "fake"}, {}, [])))
    monkeypatch.setattr(relay, "_build_mcp_config", lambda *a, **kw: None)
    monkeypatch.setattr(relay, "_claude_argv_for_slot", lambda *a: ["fake-claude"])
    monkeypatch.setattr(relay, "_build_claude_env", lambda *a: {})
    monkeypatch.setattr(relay, "_stream_prepare", AsyncMock())
    writer = AsyncMock()
    monkeypatch.setattr(relay, "_stream_write", writer)
    monkeypatch.setattr(relay, "_stream_write_eof", AsyncMock())
    monkeypatch.setattr(relay, "_save_session_map", lambda: None)

    async def lines(*a, **kw):
        if False:
            yield b""

    monkeypatch.setattr(relay, "_iter_ndjson_lines", lines)
    for effort, expect_flag in (("high", True), ("bogus", False)):
        launch.reset_mock()
        writer.reset_mock()
        request = AsyncMock()
        request.json.return_value = {"model": "claude-opus", "messages_text": "t", "session_id": "s", "effort": effort}
        await relay.handle_stream(request)
        argv = launch.call_args.args
        assert ("--effort" in argv) is expect_flag
        if expect_flag:
            assert argv[argv.index("--effort") + 1] == "high"
            first = json.loads(writer.call_args_list[0].args[1])
            assert first == {"type": "effort_ack", "effort": "high"}
        else:
            assert not any(b"effort_ack" in c.args[1] for c in writer.call_args_list)


@pytest.mark.asyncio
async def test_decision_inherited_by_other_session_call_is_ignored(monkeypatch):
    d = se.EffortDecision(mode="manual", requested_effort="high", reason="r", session_id="session-A")
    token = se.set_current_decision(d)
    try:
        assert se.current_decision("session-A") is d
        assert se.current_decision("session-B") is None
        assert se.current_decision(None) is d
    finally:
        se.reset_current_decision(token)
    assert se.current_decision("session-A") is None
