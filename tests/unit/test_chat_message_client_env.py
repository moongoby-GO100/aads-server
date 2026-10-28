"""메시지별 접속 환경(client_env) — 요청 정규화·저장·조회·AI 컨텍스트.

원칙: 접속 환경은 부가 정보다. 잘못된 값은 422 가 아니라 정규화하고,
이전 메시지의 환경을 현재 메시지에 재사용하지 않는다.
"""
from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.models.chat import BranchCreateRequest, ClientEnv, MessageOut, MessageSendRequest
from app.routers import chat as chat_router
from app.services import chat_service
from app.services.client_env import (
    CLIENT_ENV_UNKNOWN_LINE,
    client_env_prompt_block,
    format_client_env_context,
    normalize_client_env,
)

SESSION_ID = str(uuid.uuid4())
TENANT_A = uuid.uuid4()
TENANT_B = uuid.uuid4()
CONTEXT = {"tenant": {"id": str(TENANT_A)}, "user": {"user_id": str(uuid.uuid4())}}

MOBILE_ENV = {
    "device_class": "mobile",
    "viewport_w": 390,
    "viewport_h": 844,
    "orientation": "portrait",
    "os_family": "ios",
    "browser_family": "safari",
    "source": "user_agent",
    "user_override": False,
    "env_uncertain": False,
}

UNCERTAIN_EMPTY_ENV = {
    "device_class": "unknown", "viewport_w": None, "viewport_h": None, "orientation": None,
    "os_family": None, "browser_family": None, "source": "unknown",
    "user_override": False, "env_uncertain": True,
}


# ── 요청 계약 / 정규화 ────────────────────────────────────────────────────────

class FakeRequest:
    def __init__(self, body=None, *, content_type="application/json", form=None):
        self.headers = {"content-type": content_type}
        self._body = body
        self._form = form

    async def json(self):
        return self._body

    async def form(self):
        return self._form


class FakeForm(dict):
    def getlist(self, name):
        return []


@pytest.fixture
def svc_mocks(monkeypatch):
    async def _stream(**kwargs):
        yield "data: {}\n\n"

    m = MagicMock()
    m.get_session = AsyncMock(return_value={"id": SESSION_ID})
    m.get_html_edit_context_state = AsyncMock(return_value={})
    m.send_message_stream = MagicMock(side_effect=lambda **kw: _stream(**kw))
    m.with_background_completion = MagicMock(side_effect=lambda stream, **kw: stream)
    monkeypatch.setattr(chat_router, "svc", m)
    monkeypatch.setattr(chat_router, "is_streaming", lambda sid: False)
    monkeypatch.setattr(
        "app.services.tenant_usage_limits.check_tenant_usage_limit", AsyncMock(return_value=None)
    )
    return m


async def _send(request):
    return await chat_router.send_message(request, contract_version=None, context=CONTEXT)


def _body(**over):
    body = {"session_id": SESSION_ID, "content": "hello"}
    body.update(over)
    return body


async def test_valid_client_env_is_forwarded_unchanged(svc_mocks):
    response = await _send(FakeRequest(_body(client_env=MOBILE_ENV)))

    assert response.status_code == 200
    assert svc_mocks.send_message_stream.call_args.kwargs["client_env"] == MOBILE_ENV


async def test_missing_client_env_is_200_and_none(svc_mocks):
    response = await _send(FakeRequest(_body()))

    assert response.status_code == 200
    assert svc_mocks.send_message_stream.call_args.kwargs["client_env"] is None


async def test_invalid_values_are_normalized_not_rejected(svc_mocks):
    response = await _send(FakeRequest(_body(client_env={
        "device_class": "phone",
        "viewport_w": -1,
        "viewport_h": 10001,
        "orientation": "diagonal",
        "os_family": "BeOS",
        "browser_family": "brave",
        "source": "guess",
        "user_agent": "Mozilla/5.0 (iPhone) SECRET-UA",
        "ip": "10.0.0.1",
        "device_model": "iPhone15,2",
    })))

    assert response.status_code == 200
    env = svc_mocks.send_message_stream.call_args.kwargs["client_env"]
    assert env == {
        "device_class": "unknown",
        "viewport_w": None,
        "viewport_h": None,
        "orientation": None,
        "os_family": "other",
        "browser_family": "other",
        "source": "unknown",
        "user_override": False,
        "env_uncertain": True,
    }
    assert "SECRET-UA" not in repr(env)
    assert "ip" not in env and "device_model" not in env and "user_agent" not in env


@pytest.mark.parametrize("raw", ["mobile", 7, ["pc"], True])
async def test_non_object_client_env_is_dropped_not_422(svc_mocks, raw):
    response = await _send(FakeRequest(_body(client_env=raw)))

    assert response.status_code == 200
    assert svc_mocks.send_message_stream.call_args.kwargs["client_env"] == UNCERTAIN_EMPTY_ENV


async def test_multipart_client_env_json_string_is_normalized(svc_mocks):
    form = FakeForm(
        session_id=SESSION_ID,
        content="hello",
        client_env=json.dumps({**MOBILE_ENV, "device_class": "PHONE", "extra": 1}),
    )
    response = await _send(FakeRequest(content_type="multipart/form-data", form=form))

    assert response.status_code == 200
    env = svc_mocks.send_message_stream.call_args.kwargs["client_env"]
    assert env["device_class"] == "unknown"
    assert env["viewport_w"] == 390 and "extra" not in env


async def test_multipart_garbage_client_env_is_uncertain_not_rejected(svc_mocks):
    form = FakeForm(session_id=SESSION_ID, content="hello", client_env="{not json")
    response = await _send(FakeRequest(content_type="multipart/form-data", form=form))

    assert response.status_code == 200
    assert svc_mocks.send_message_stream.call_args.kwargs["client_env"] == UNCERTAIN_EMPTY_ENV


async def test_multipart_without_client_env_is_none(svc_mocks):
    form = FakeForm(session_id=SESSION_ID, content="hello")
    response = await _send(FakeRequest(content_type="multipart/form-data", form=form))

    assert response.status_code == 200
    assert svc_mocks.send_message_stream.call_args.kwargs["client_env"] is None


async def test_user_override_is_kept_and_wrong_type_is_uncertain(svc_mocks):
    await _send(FakeRequest(_body(client_env={**MOBILE_ENV, "source": "user_set", "user_override": True})))
    env = svc_mocks.send_message_stream.call_args.kwargs["client_env"]
    assert env["user_override"] is True and env["env_uncertain"] is False

    await _send(FakeRequest(_body(client_env={**MOBILE_ENV, "user_override": "yes"})))
    env = svc_mocks.send_message_stream.call_args.kwargs["client_env"]
    assert env["user_override"] is False and env["env_uncertain"] is True


@pytest.mark.parametrize("bad", [
    {"viewport_w": "390"}, {"viewport_h": 0}, {"viewport_w": True}, {"orientation": "diagonal"},
    {"device_class": "phone"}, {"source": "guess"}, {"os_family": 5}, {"user_agent": "Mozilla"},
])
def test_each_rejected_value_marks_env_uncertain(bad):
    assert normalize_client_env({**MOBILE_ENV, **bad})["env_uncertain"] is True


def test_clean_env_is_not_uncertain():
    assert normalize_client_env(MOBILE_ENV)["env_uncertain"] is False


def test_case_and_boundary_values_normalize():
    env = ClientEnv(**{
        "device_class": " Tablet ", "viewport_w": 1, "viewport_h": 10000,
        "orientation": "LANDSCAPE", "os_family": "MacOS", "browser_family": "Samsung",
        "source": "client_hints",
    })
    assert env.model_dump() == {
        "device_class": "tablet", "viewport_w": 1, "viewport_h": 10000,
        "orientation": "landscape", "os_family": "macos", "browser_family": "samsung",
        "source": "client_hints", "user_override": False, "env_uncertain": False,
    }
    assert ClientEnv(viewport_w=0).viewport_w is None
    assert ClientEnv(viewport_w=True).viewport_w is None
    assert ClientEnv(viewport_w="390").viewport_w is None


def test_branch_request_accepts_and_normalizes_client_env():
    req = BranchCreateRequest(content="x", client_env={"device_class": "phone", "viewport_w": 800})
    assert req.client_env.device_class == "unknown"
    assert req.client_env.viewport_w == 800
    assert BranchCreateRequest(content="x").client_env is None
    assert MessageSendRequest(session_id=SESSION_ID, content="x").client_env is None


def test_normalize_client_env_never_keeps_unknown_keys():
    assert normalize_client_env(None) is None
    assert normalize_client_env("") is None
    assert normalize_client_env("garbage") == UNCERTAIN_EMPTY_ENV
    assert "ip" not in normalize_client_env({"ip": "1.2.3.4"})


# ── 저장 → 조회 왕복 / 테넌트 격리 ─────────────────────────────────────────────

class _Txn:
    async def __aenter__(self):
        return None

    async def __aexit__(self, *exc):
        return False


class _Acquire:
    def __init__(self, conn):
        self._conn = conn

    async def __aenter__(self):
        return self._conn

    async def __aexit__(self, *exc):
        return False


class _Pool:
    def __init__(self, conn):
        self._conn = conn

    def acquire(self):
        return _Acquire(self._conn)


class FakeChatDb:
    """chat_messages 한 테이블만 흉내낸다. jsonb 는 asyncpg 처럼 문자열로 돌려준다."""

    def __init__(self):
        self.rows: list[dict] = []
        self.session_tenant: dict[uuid.UUID, uuid.UUID] = {}
        self.updates: list[tuple] = []

    def transaction(self):
        return _Txn()

    async def fetchrow(self, sql, *args):
        assert "INSERT INTO chat_messages" in sql
        assert "client_env" not in sql  # 기존 INSERT 는 그대로. 환경은 값이 있을 때만 UPDATE.
        session_id, execution_id, role, content = args[0], args[1], args[2], args[3]
        row = {
            "id": uuid.uuid4(), "session_id": session_id, "execution_id": execution_id,
            "tenant_id": self.session_tenant[session_id], "role": role, "content": content,
            "model_used": args[4], "intent": args[5], "cost": Decimal("0"),
            "tokens_in": 0, "tokens_out": 0, "bookmarked": False,
            "attachments": "[]", "sources": "[]", "tools_called": "[]",
            "artifact_id": None, "edited_at": None, "reply_to_id": None,
            "branch_id": None, "branch_point_id": None, "is_hidden": False,
            "client_env": None, "created_at": datetime.now(timezone.utc),
        }
        self.rows.append(row)
        return dict(row)

    async def execute(self, sql, *args):
        if "SET client_env" in sql:
            msg_id, payload = args
            self.updates.append((msg_id, payload))
            next(r for r in self.rows if r["id"] == msg_id)["client_env"] = payload

    async def fetch(self, sql, *args):
        assert "FROM chat_messages WHERE session_id = $1 AND tenant_id = $4" in sql
        session_id, tenant_id = args[0], args[3]
        return [
            dict(r) for r in self.rows
            if r["session_id"] == session_id and r["tenant_id"] == tenant_id
        ]


@pytest.fixture
def db(monkeypatch):
    fake = FakeChatDb()
    fake.session_tenant[uuid.UUID(SESSION_ID)] = TENANT_A
    monkeypatch.setattr(chat_service, "get_pool", lambda: _Pool(fake))
    monkeypatch.setattr(chat_service, "_session_has_running_execution", AsyncMock(return_value=False))
    monkeypatch.setattr(
        chat_service, "_hydrate_message_response_durations",
        AsyncMock(side_effect=lambda conn, rows, persist=True: rows),
    )
    monkeypatch.setattr(
        chat_service, "_repair_completed_execution_message_flags",
        AsyncMock(side_effect=lambda conn, rows, *a, **kw: rows),
    )
    return fake


async def _list(tenant_id):
    return await chat_service.list_messages(
        SESSION_ID, tenant_id=str(tenant_id), read_only=True, fields="render"
    )


async def test_roundtrip_request_to_storage_to_query(db):
    req = MessageSendRequest(session_id=SESSION_ID, content="hi", client_env=MOBILE_ENV)
    saved = await chat_service._save_message(
        db, uuid.UUID(SESSION_ID), "user", "hi", client_env=req.client_env.model_dump()
    )

    assert saved["client_env"] == MOBILE_ENV
    assert json.loads(db.updates[0][1]) == MOBILE_ENV

    listed = await _list(TENANT_A)
    assert [m["client_env"] for m in listed] == [MOBILE_ENV]
    assert MessageOut(**listed[0]).client_env == MOBILE_ENV


async def test_missing_client_env_stores_null_and_returns_null(db):
    saved = await chat_service._save_message(db, uuid.UUID(SESSION_ID), "user", "hi")

    assert saved["client_env"] is None
    assert db.updates == []
    listed = await _list(TENANT_A)
    assert listed[0]["client_env"] is None
    assert MessageOut(**listed[0]).client_env is None


async def test_assistant_messages_never_store_client_env(db):
    await chat_service._save_message(
        db, uuid.UUID(SESSION_ID), "assistant", "ok", client_env=MOBILE_ENV
    )
    assert db.updates == []


async def test_other_tenant_cannot_read_client_env(db):
    await chat_service._save_message(
        db, uuid.UUID(SESSION_ID), "user", "hi", client_env=MOBILE_ENV
    )

    assert await _list(TENANT_B) == []
    assert len(await _list(TENANT_A)) == 1


def test_read_paths_keep_tenant_filter_and_expose_column():
    render = chat_service._message_select_fields("render")
    assert "client_env" in render
    # full 은 SELECT * 라서 컬럼이 그대로 나간다.
    assert chat_service._message_select_fields("full") == "*"
    assert "client_env" in chat_service._JSONB_FIELDS


# ── AI 컨텍스트 ───────────────────────────────────────────────────────────────

MOBILE_LINE = "[접속 환경] device=mobile(viewport 390x844, portrait), source=user_agent, 불확실=false"


def test_context_line_with_env():
    assert format_client_env_context(MOBILE_ENV) == MOBILE_LINE


@pytest.mark.parametrize("env", [None, {}, "garbage", {"source": "user_agent"}, {"device_class": "phone"}])
def test_context_line_without_usable_env_says_unknown(env):
    assert format_client_env_context(env) == CLIENT_ENV_UNKNOWN_LINE
    assert CLIENT_ENV_UNKNOWN_LINE == "[접속 환경] 미확인(구 클라이언트 또는 수집 실패)"


def test_context_line_partial_env_does_not_guess_missing_parts():
    line = format_client_env_context({"device_class": "pc", "source": "client_hints"})
    assert line == "[접속 환경] device=pc, source=client_hints, 불확실=false"


def test_context_line_reports_uncertainty():
    line = format_client_env_context({**MOBILE_ENV, "env_uncertain": True})
    assert line.endswith("불확실=true")


def test_prompt_block_forbids_reusing_previous_environment():
    block = client_env_prompt_block(MOBILE_ENV)
    assert "390x844" in block
    assert "이전 메시지의 환경을 이번 메시지에 적용하지 않는다" in block


async def _run_turn(client_env):
    """send_message_stream 한 턴을 돌려 모델에 전달된 system_prompt 를 돌려준다."""
    captured = {}
    session_id = str(uuid.uuid4())
    conn = AsyncMock()

    async def _fetchrow(query, *args):
        if "FROM chat_workspaces w" in query:
            return {
                "workspace_id": uuid.uuid4(), "workspace_name": "AADS",
                "system_prompt": "BASE_SYSTEM", "workspace_settings": {},
                "role_key": "", "session_settings": {},
            }
        if "FROM chat_session_stats" in query:
            return {"cost_total": 0, "message_count": 0}
        if "SELECT settings FROM chat_users" in query:
            return {"settings": {}}
        return None

    conn.fetchrow = AsyncMock(side_effect=_fetchrow)
    conn.fetch = AsyncMock(return_value=[])
    conn.fetchval = AsyncMock(return_value=uuid.uuid4())

    async def _call_stream(*, intent_result, system_prompt, **kwargs):
        captured["system_prompt"] = system_prompt
        yield {"type": "delta", "content": "ok"}
        yield {"type": "done", "model": intent_result.model, "cost": "0", "input_tokens": 1, "output_tokens": 1}

    save = AsyncMock(return_value={"id": uuid.uuid4()})
    with (
        patch("app.services.chat_service.get_pool", return_value=_Pool(conn)),
        patch("app.services.chat_service.create_trace", return_value=None),
        patch("app.services.chat_service._get_last_html_artifact", new=AsyncMock(return_value=None)),
        patch(
            "app.services.context_builder.build_messages_context",
            new=AsyncMock(return_value=([{"role": "user", "content": "q"}], "BASE_SYSTEM")),
        ),
        patch(
            "app.services.intent_router.classify",
            new=AsyncMock(return_value=SimpleNamespace(
                intent="general", model="gpt-5.5", use_tools=False, tool_group=None,
                use_gemini_direct=True, gemini_mode=None,
            )),
        ),
        patch("app.services.model_selector._get_default_llm_model_from_db", new=AsyncMock(return_value="gpt-5.5")),
        patch("app.services.contradiction_detector.detect_contradictions", new=AsyncMock(return_value="")),
        patch("app.services.chat_embedding_service.embed_texts", new=AsyncMock(return_value=[])),
        patch("app.services.semantic_cache.SemanticCache.lookup", new=AsyncMock(return_value=None)),
        patch("app.services.output_validator.validate_response", return_value=SimpleNamespace(is_valid=True)),
        patch("app.services.response_critic.critique_response", new=AsyncMock(return_value=None)),
        patch("app.services.chat_service._save_message", new=save),
        patch("app.services.chat_service._save_and_update_session", new=AsyncMock(return_value=None)),
        patch("app.services.model_selector.call_stream", new=_call_stream),
    ):
        _ = [
            c async for c in chat_service.send_message_stream(
                session_id=session_id, content="q", attachments=[],
                response_mode="fast", client_env=client_env,
            )
        ]
    return captured["system_prompt"], save


@pytest.mark.asyncio
async def test_turn_with_env_puts_one_line_in_system_prompt_and_saves_it():
    prompt, save = await _run_turn(MOBILE_ENV)

    assert MOBILE_LINE in prompt
    user_saves = [c for c in save.await_args_list if c.args[2] == "user"]
    assert user_saves and user_saves[0].kwargs["client_env"] == MOBILE_ENV


@pytest.mark.asyncio
async def test_turn_without_env_says_unknown_and_does_not_reuse_previous_turn():
    first, _ = await _run_turn(MOBILE_ENV)
    second, save = await _run_turn(None)

    assert "390x844" in first
    assert CLIENT_ENV_UNKNOWN_LINE in second
    assert "390x844" not in second and "ios" not in second
    user_saves = [c for c in save.await_args_list if c.args[2] == "user"]
    assert user_saves[0].kwargs["client_env"] is None


# ── 추가 지시(interrupt) 경로 ──────────────────────────────────────────────────

def test_interrupt_request_normalizes_client_env():
    from app.routers.chat import InterruptRequest

    assert InterruptRequest(content="x").client_env is None
    req = InterruptRequest(content="x", client_env={**MOBILE_ENV, "device_class": "phone", "ip": "1.1.1.1"})
    assert req.client_env.device_class == "unknown" and req.client_env.env_uncertain is True
    assert "ip" not in req.client_env.model_dump()


def test_interrupt_queue_carries_env_per_message():
    from app.core import interrupt_queue as q

    sid = f"s-{uuid.uuid4()}"
    q.push_interrupt(sid, "첫째", client_env=MOBILE_ENV)
    q.push_interrupt(sid, "둘째")
    items = q.pop_interrupts(sid)
    assert items[0]["client_env"] == MOBILE_ENV and "client_env" not in items[1]


def test_interrupt_env_lines_only_for_items_with_env():
    from app.services.client_env import interrupt_env_lines

    assert interrupt_env_lines([{"content": "a"}, {"content": "b", "client_env": None}]) == ""
    assert interrupt_env_lines([{"content": "a", "client_env": MOBILE_ENV}, {"content": "b"}]) == MOBILE_LINE


def test_interrupt_insert_and_consumers_are_wired():
    import inspect

    assert "client_env" in inspect.getsource(chat_router.interrupt_session)
    for fn in (chat_service._fetch_persisted_interrupts,):
        assert "client_env" in inspect.getsource(fn)
    src = inspect.getsource(chat_service)
    assert src.count("interrupt_env_lines(") >= 2
    from app.services import model_selector
    assert inspect.getsource(model_selector).count("interrupt_env_lines(") >= 2


async def test_db_recovered_interrupt_carries_its_own_env():
    conn = AsyncMock()
    msg_id = uuid.uuid4()
    conn.fetch = AsyncMock(return_value=[
        {"id": msg_id, "content": "[추가 지시] 표로", "attachments": "[]",
         "client_env": json.dumps(MOBILE_ENV)},
        {"id": uuid.uuid4(), "content": "[추가 지시] 요약", "attachments": "[]", "client_env": None},
    ])
    conn.execute = AsyncMock()

    with patch("app.services.chat_service.get_pool", return_value=_Pool(conn)):
        recovered = await chat_service._fetch_persisted_interrupts(SESSION_ID)

    assert recovered[0]["client_env"] == MOBILE_ENV
    assert "client_env" not in recovered[1]
