"""중단된 턴도 CLI 대화를 이어받을 수 있어야 한다.

2026-09-15 08:20, #310 주도 세션의 1차 시도가 도구 중간에 끊겼다. 릴레이는
그 CLI 세션 id 를 **시작 직후에 이미 알고 있었는데**, 저장은 "종료코드 0"
일 때만 해서 통째로 버렸다. 2차 시도는 `resume=False` 로 처음부터 갔고
17분치 도구 작업 4건이 사라졌다.

게다가 이어받기가 실패하면 **이유를 가리지 않고** 매핑을 지웠다. 와치독이
끊었든 클라이언트가 닫혔든 상관없이, 한 번 삐끗하면 대화가 영구히 버려졌다.
"""
import inspect

import pytest

from scripts import claude_relay_server as relay


def _body(fn) -> str:
    return "\n".join(
        line for line in inspect.getsource(fn).splitlines()
        if not line.strip().startswith("#")
    )


def test_missing_conversation_error_is_narrow():
    assert relay._is_missing_conversation_error(
        "No conversation found with session ID: abc") is True
    # 다른 실패는 대화가 없어진 것이 아니다 — 매핑을 버릴 사유가 못 된다.
    assert relay._is_missing_conversation_error("CLI exited with code 143") is False
    assert relay._is_missing_conversation_error("429 rate limit") is False
    assert relay._is_missing_conversation_error("") is False


def test_remember_writes_and_is_idempotent(monkeypatch):
    saved = []
    monkeypatch.setattr(relay, "_session_map", {})
    monkeypatch.setattr(relay, "_save_session_map", lambda: saved.append(1))

    relay._remember_cli_session("aads-1", "2", "claude-opus-5", "cli-1")
    assert relay._session_map == {"aads-1@2@claude-opus-5": "cli-1"}
    assert len(saved) == 1

    # 같은 값을 다시 넣어도 파일을 또 쓰지 않는다.
    relay._remember_cli_session("aads-1", "2", "claude-opus-5", "cli-1")
    assert len(saved) == 1


def test_remember_ignores_empty_inputs(monkeypatch):
    monkeypatch.setattr(relay, "_session_map", {})
    monkeypatch.setattr(relay, "_save_session_map", lambda: None)
    relay._remember_cli_session("", "2", "claude-opus-5", "cli-1")
    relay._remember_cli_session("aads-1", "2", "claude-opus-5", "")
    assert relay._session_map == {}


def test_slot_is_part_of_the_key(monkeypatch):
    """일찍 저장해도 다른 계정이 남의 대화를 이어받으면 안 된다."""
    monkeypatch.setattr(relay, "_session_map", {})
    monkeypatch.setattr(relay, "_save_session_map", lambda: None)
    relay._remember_cli_session("aads-1", "1", "claude-opus-5", "cli-slot1")
    relay._remember_cli_session("aads-1", "2", "claude-opus-5", "cli-slot2")
    assert relay._session_map["aads-1@1@claude-opus-5"] == "cli-slot1"
    assert relay._session_map["aads-1@2@claude-opus-5"] == "cli-slot2"


def test_mapping_is_saved_at_init_not_only_on_clean_exit():
    src = inspect.getsource(relay)
    init_block = src.split('event.get("subtype") == "init"', 1)[1][:600]
    assert "_remember_cli_session" in init_block
    # 옛 조건(종료코드 0 일 때만 저장)이 남아 있으면 안 된다.
    assert "if proc.returncode == 0 and aads_session_id and captured_cli_session_id" not in src


def test_repeated_resume_failure_is_bounded():
    """대화가 못 쓰게 됐는데 매핑만 남으면 영원히 같은 실패를 반복한다."""
    assert relay._RESUME_SKIP_AFTER < relay._RESUME_DROP_AFTER
    src = inspect.getsource(relay)
    assert "_resume_failures[_stale_key] = _resume_failures.get(_stale_key, 0) + 1" in src
    assert "Dropped unusable session mapping" in src
