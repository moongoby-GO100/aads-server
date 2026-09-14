"""세션 간 협업 회귀 테스트.

2026-09-14. 담당 다섯이 서로 물어볼 방법이 없어 만들었다. 실제 왕복
시험에서 버그 셋을 잡았고 전부 여기 고정한다.

**1. 답을 실행 id 로 특정한다.**
   첫 구현은 대상 세션의 "가장 최근 assistant 메시지" 를 답으로 집었다.
   그랬더니 이 질문과 **무관한 중단 안내문**이 회신으로 갔다. 무관한 답을
   전달하는 것은 답이 없는 것보다 나쁘다 — 물어본 담당이 그걸 근거로
   판단한다.

**2. 백그라운드 태스크 참조를 붙든다.**
   `asyncio.create_task` 결과를 버리면 GC 가 중간에 거둬 간다. 같은 날
   `chat_messages.embedding` 이 그 방식으로 소리 없이 실패하고 있었다
   (assistant 메시지의 9.6% 만 임베딩).

**3. 루프를 세 겹으로 막는다.**
   A→B→A→B 가 끝없이 돈다. 같은 날 resume 자동 재시도가 시간당 1,256회까지
   간 것이 같은 종류의 사고다.
"""
import inspect

from app.services import session_relay


def test_answer_is_tied_to_execution():
    """가장 최근 메시지를 답으로 집으면 안 된다."""
    src = inspect.getsource(session_relay._run_relay)
    assert "execution_id = $1::uuid" in src, "실행 id 로 답을 특정하지 않는다"
    assert "ORDER BY created_at DESC LIMIT 1\",\n            target_session_id" not in src, (
        "세션의 최신 메시지를 그대로 답으로 쓰고 있다"
    )
    assert "실행 id 를 잡지 못했다" in src, "실행 id 없이 진행하고 있다"


def test_interrupted_is_not_an_answer():
    """중단·플레이스홀더를 답으로 전달하면 안 된다."""
    src = inspect.getsource(session_relay._run_relay)
    for bad in ("streaming_placeholder", "interrupted_partial",
                "resume_failure_notice", "stale_empty_placeholder"):
        assert bad in src, f"{bad} 를 답에서 걸러내지 않는다"


def test_background_task_reference_is_held():
    """태스크를 버리면 GC 가 중간에 거둬 간다."""
    src = inspect.getsource(session_relay.ask)
    assert "_running.add(task)" in src, "태스크 참조를 붙들지 않는다"
    assert "add_done_callback" in src


def test_three_loop_guards_exist():
    src = inspect.getsource(session_relay.ask)
    assert "hop_limit" in src, "홉 제한이 없다"
    assert "self_target" in src, "자기 자신 차단이 없다"
    assert "already_in_flight" in src, "같은 쌍 중복 차단이 없다"
    assert "target_busy" in src, "진행 중인 세션 보호가 없다"


def test_reply_triggers_next_action():
    """회신이 다음 행동을 유발해야 한다.

    2026-09-14 첫 구현은 회신을 assistant 메시지로 넣었다. 루프를 막으려는
    의도였는데 **물어본 담당이 그걸 읽고 움직이지 않았다.** 실측: 회신이
    18:08:56 에 도착했고 그 뒤 실행이 0건이었다. 답만 놓여 있었다.

    협업의 목적은 답을 받는 것이 아니라 받은 답으로 다음을 하는 것이다.
    루프는 회신을 죽여서가 아니라 **홉 수**로 막는다.
    """
    src = inspect.getsource(session_relay._deliver_answer)
    body = src[src.index("from app.services import chat_service"):]
    assert "send_message_stream" in body, "회신이 다음 행동을 유발하지 않는다"
    assert "system_trigger" in body, "응답률 96% 가 확인된 경로를 쓰지 않는다"


def test_delivery_waits_for_busy_origin():
    """진행 중인 응답에 회신을 밀어 넣으면 그 응답이 버려진다.

    오늘 세션 5090a247 에서 `stale_superseded_by_newer_user_message` 로
    54,301자짜리 진행 중 응답이 사라지는 것을 봤다.
    """
    src = inspect.getsource(session_relay._deliver_answer)
    assert "_target_is_busy" in src, "물어본 쪽이 바쁜지 확인하지 않는다"
    assert "_DELIVER_WAIT_TRIES" in src


def test_reply_tells_what_to_do_next():
    """답만 던지면 무엇을 하라는 건지 모른다."""
    src = inspect.getsource(session_relay._run_relay)
    assert "이제 할 일" in src, "회신에 다음 행동 안내가 없다"
    assert "ask_session" in src


def test_hop_limit_is_bounded():
    assert 1 <= session_relay.MAX_HOP <= 5


def test_context_is_capped():
    """A 의 맥락이 통째로 B 에 가면 B 의 다음 대화가 오염된다."""
    src = inspect.getsource(session_relay._build_question)
    assert "CONTEXT_LIMIT" in src


def test_target_stays_in_same_workspace():
    """프로젝트를 넘어 부르면 맥락이 섞인다."""
    src = inspect.getsource(session_relay._resolve_target)
    assert "workspace_id" in src, "워크스페이스 경계를 확인하지 않는다"


def test_set_streaming_unbound_masking_is_fixed():
    """조기 실패가 UnboundLocalError 로 가려지면 안 된다.

    `set_streaming` 은 `send_message_stream` 중간에서 import 된다. 그 전에
    예외가 나면 finally 가 UnboundLocalError 를 던져 **진짜 오류를 가린다.**
    2026-09-14 첫 시험에서 실패 사유가
    "cannot access local variable 'set_streaming'" 으로만 남았다.
    """
    from app.services import chat_service

    src = inspect.getsource(chat_service.send_message_stream)
    tail = src[src.rindex("finally:"):]
    assert "_clear_streaming" in tail, "finally 가 여전히 마스킹한다"
    assert "import set_streaming as _clear_streaming" in tail
