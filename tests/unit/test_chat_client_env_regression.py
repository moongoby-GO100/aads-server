# ruff: noqa: F811  (기존 테스트 모듈의 pytest 픽스처를 import 해 인자로 받는 패턴)
"""접속 환경(client_env) 회귀 2차 — 요청 → 저장 → 조회 → AI 컨텍스트를 한 줄로 잇는 시나리오.

기존 test_chat_message_client_env.py 가 구간별로 검증한 것을, 여기서는 라우터 입력부터
조회 결과까지 끊김 없이 이어서 확인한다. 이미 있는 단일 구간 시나리오는 다시 쓰지 않는다.
"""
from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone

import pytest

from app.models.chat import MessageOut
from app.services import chat_service
from app.services.client_env import (
    CLIENT_ENV_UNKNOWN_LINE,
    format_client_env_context,
)
from tests.unit.test_chat_message_client_env import (  # noqa: F401  (픽스처 재사용)
    CONTEXT,
    MOBILE_ENV,
    MOBILE_LINE,
    SESSION_ID,
    TENANT_A,
    TENANT_B,
    FakeForm,
    FakeRequest,
    _run_turn,
    db,
    svc_mocks,
)

PC_ENV = {
    "device_class": "pc",
    "viewport_w": 1920,
    "viewport_h": 1080,
    "orientation": "landscape",
    "os_family": "windows",
    "browser_family": "chrome",
    "source": "user_agent",
    "user_override": False,
    "env_uncertain": False,
}
PC_LINE = "[접속 환경] device=pc(viewport 1920x1080, landscape), source=user_agent, 불확실=false"

JSON_MODE = "json"
MULTIPART_MODE = "multipart"
MODES = [JSON_MODE, MULTIPART_MODE]


def _request(mode, *, env=None, has_env=True):
    """같은 client_env 를 JSON 본문 또는 multipart 폼 필드(JSON 문자열)로 싣는다."""
    if mode == JSON_MODE:
        body = {"session_id": SESSION_ID, "content": "hello"}
        if has_env:
            body["client_env"] = env
        return FakeRequest(body)
    form = FakeForm(session_id=SESSION_ID, content="hello")
    if has_env:
        form["client_env"] = env if isinstance(env, str) else json.dumps(env)
    return FakeRequest(content_type="multipart/form-data", form=form)


async def _post(svc_mocks, mode, *, env=None, has_env=True):
    """라우터 send_message 를 통과시키고 서비스 계층이 받은 client_env 를 돌려준다."""
    from app.routers import chat as chat_router

    response = await chat_router.send_message(
        _request(mode, env=env, has_env=has_env), contract_version=None, context=CONTEXT
    )
    assert response.status_code == 200
    return svc_mocks.send_message_stream.call_args.kwargs["client_env"]


async def _save_user(db, forwarded_env, content="hello", session_id=SESSION_ID):
    return await chat_service._save_message(
        db, uuid.UUID(session_id), "user", content, client_env=forwarded_env
    )


async def _list(tenant_id, session_id=SESSION_ID):
    return await chat_service.list_messages(
        session_id, tenant_id=str(tenant_id), read_only=True, fields="render"
    )


# ── ① 요청 → 저장 → 조회 왕복 (JSON · multipart 각각) ──────────────────────────

@pytest.mark.parametrize("mode", MODES)
async def test_roundtrip_through_router_storage_and_query(svc_mocks, db, mode):
    forwarded = await _post(svc_mocks, mode, env=MOBILE_ENV)
    assert forwarded == MOBILE_ENV

    await _save_user(db, forwarded)

    listed = await _list(TENANT_A)
    assert [m["client_env"] for m in listed] == [MOBILE_ENV]
    assert MessageOut(**listed[0]).client_env == MOBILE_ENV
    assert format_client_env_context(listed[0]["client_env"]) == MOBILE_LINE


# ── ② 같은 세션에서 PC → 모바일: 메시지별 독립 저장, 컨텍스트가 메시지별로 바뀜 ─────

@pytest.mark.parametrize("mode", MODES)
async def test_pc_then_mobile_in_same_session_are_stored_independently(svc_mocks, db, mode):
    pc = await _post(svc_mocks, mode, env=PC_ENV)
    await _save_user(db, pc, content="pc 에서 보낸 메시지")
    mobile = await _post(svc_mocks, mode, env=MOBILE_ENV)
    await _save_user(db, mobile, content="모바일에서 보낸 메시지")

    listed = await _list(TENANT_A)
    assert [m["content"] for m in listed] == ["pc 에서 보낸 메시지", "모바일에서 보낸 메시지"]
    assert [m["client_env"] for m in listed] == [PC_ENV, MOBILE_ENV]
    assert [format_client_env_context(m["client_env"]) for m in listed] == [PC_LINE, MOBILE_LINE]
    # 두 번째 저장이 첫 메시지의 환경을 덮어쓰지 않았다.
    assert [json.loads(payload)["device_class"] for _, payload in db.updates] == ["pc", "mobile"]


async def test_pc_turn_then_mobile_turn_changes_system_prompt_without_reuse():
    pc_prompt, _ = await _run_turn(PC_ENV)
    mobile_prompt, mobile_save = await _run_turn(MOBILE_ENV)

    assert PC_LINE in pc_prompt
    assert MOBILE_LINE in mobile_prompt
    assert "1920x1080" not in mobile_prompt and "device=pc" not in mobile_prompt
    user_saves = [c for c in mobile_save.await_args_list if c.args[2] == "user"]
    assert user_saves[0].kwargs["client_env"] == MOBILE_ENV


async def test_mobile_turn_then_pc_turn_does_not_leak_back():
    mobile_prompt, _ = await _run_turn(MOBILE_ENV)
    pc_prompt, _ = await _run_turn(PC_ENV)

    assert "390x844" in mobile_prompt
    assert PC_LINE in pc_prompt
    assert "390x844" not in pc_prompt and "device=mobile" not in pc_prompt


# ── ③ client_env 누락 → NULL 저장 + 미확인 문구 (JSON · multipart 각각) ──────────

@pytest.mark.parametrize("mode", MODES)
async def test_missing_env_through_router_stores_null_and_says_unknown(svc_mocks, db, mode):
    forwarded = await _post(svc_mocks, mode, has_env=False)
    assert forwarded is None

    saved = await _save_user(db, forwarded)

    assert saved["client_env"] is None
    assert db.updates == []
    listed = await _list(TENANT_A)
    assert listed[0]["client_env"] is None
    assert format_client_env_context(listed[0]["client_env"]) == CLIENT_ENV_UNKNOWN_LINE


# ── ④ 잘못된 값 → 버려지고 env_uncertain=true, 저장본에도 원문이 남지 않음 ──────────

BAD_ENV = {
    "device_class": "phone",
    "viewport_w": "390",
    "viewport_h": 844,
    "orientation": "portrait",
    "os_family": "ios",
    "browser_family": "safari",
    "source": "user_agent",
    "user_agent": "Mozilla/5.0 (iPhone) SECRET-UA",
    "ip": "10.0.0.1",
}


@pytest.mark.parametrize("mode", MODES)
async def test_invalid_values_are_dropped_and_flagged_in_storage(svc_mocks, db, mode):
    forwarded = await _post(svc_mocks, mode, env=BAD_ENV)

    assert forwarded["device_class"] == "unknown"
    assert forwarded["viewport_w"] is None
    assert forwarded["viewport_h"] == 844
    assert forwarded["env_uncertain"] is True
    assert not {"user_agent", "ip"} & set(forwarded)

    await _save_user(db, forwarded)

    stored_payload = db.updates[0][1]
    assert "SECRET-UA" not in stored_payload and "10.0.0.1" not in stored_payload
    listed = await _list(TENANT_A)
    assert listed[0]["client_env"]["env_uncertain"] is True
    assert listed[0]["client_env"]["device_class"] == "unknown"
    assert listed[0]["client_env"]["viewport_w"] is None
    assert MessageOut(**listed[0]).client_env == listed[0]["client_env"]


# ── ⑤ 2026-10-22 이전 구메시지(client_env 컬럼 NULL) 조회 호환 ─────────────────────

def _legacy_row(content, *, with_key=True):
    row = {
        "id": uuid.uuid4(), "session_id": uuid.UUID(SESSION_ID), "execution_id": None,
        "tenant_id": TENANT_A, "role": "user", "content": content,
        "model_used": None, "intent": None, "cost": 0, "tokens_in": 0, "tokens_out": 0,
        "bookmarked": False, "attachments": "[]", "sources": "[]", "tools_called": "[]",
        "artifact_id": None, "edited_at": None, "reply_to_id": None, "branch_id": None,
        "branch_point_id": None, "is_hidden": False,
        "created_at": datetime(2026, 10, 1, 9, 0, tzinfo=timezone.utc),
    }
    if with_key:
        row["client_env"] = None
    return row


async def test_legacy_null_rows_are_listed_alongside_new_rows(db):
    db.rows.append(_legacy_row("구메시지"))
    await _save_user(db, MOBILE_ENV, content="신메시지")

    listed = await _list(TENANT_A)

    assert [m["content"] for m in listed] == ["구메시지", "신메시지"]
    assert [m["client_env"] for m in listed] == [None, MOBILE_ENV]
    assert [MessageOut(**m).client_env for m in listed] == [None, MOBILE_ENV]
    assert format_client_env_context(listed[0]["client_env"]) == CLIENT_ENV_UNKNOWN_LINE
    assert format_client_env_context(listed[1]["client_env"]) == MOBILE_LINE


async def test_legacy_row_without_client_env_key_still_validates(db):
    db.rows.append(_legacy_row("키 없는 구메시지", with_key=False))

    listed = await _list(TENANT_A)

    assert "client_env" not in listed[0]
    assert MessageOut(**listed[0]).client_env is None


# ── ⑥ 다른 테넌트 메시지의 client_env 는 조회되지 않음 ────────────────────────────

async def test_each_tenant_reads_only_its_own_client_env(db):
    session_b = str(uuid.uuid4())
    db.session_tenant[uuid.UUID(session_b)] = TENANT_B
    await _save_user(db, PC_ENV, content="A 의 메시지")
    await _save_user(db, MOBILE_ENV, content="B 의 메시지", session_id=session_b)

    a_view = await _list(TENANT_A)
    b_view = await _list(TENANT_B, session_id=session_b)
    assert [m["client_env"] for m in a_view] == [PC_ENV]
    assert [m["client_env"] for m in b_view] == [MOBILE_ENV]

    # 세션 ID 를 알고 있어도 다른 테넌트로는 어느 쪽 환경도 보이지 않는다.
    assert await _list(TENANT_B) == []
    assert await _list(TENANT_A, session_id=session_b) == []
    assert "client_env" not in json.dumps(await _list(TENANT_B))
