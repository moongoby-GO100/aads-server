"""이미지 첨부 턴의 stdin 은 stream-json user envelope(JSONL) 이어야 한다.

2026-10-06 실측 — content 배열을 그대로 stdin 에 쓰면 claude CLI 가 오류도
출력도 없이 rc=0 으로 끝나 이미지 턴이 partial_len=0 으로 중단됐다.
envelope `{"type":"user","message":{"role":"user","content":[...]}}` + "\\n" 은 정상 종료.
"""
import importlib.util
import json
import pathlib
import sys

REPO = pathlib.Path(__file__).resolve().parents[2]
RELAY_PATH = REPO / "scripts" / "claude_relay_server.py"

sys.path.insert(0, str(REPO / "scripts"))
_spec = importlib.util.spec_from_file_location("claude_relay_server_under_test", RELAY_PATH)
relay = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(relay)

IMAGE_BLOCK = {
    "type": "image",
    "source": {"type": "base64", "media_type": "image/png", "data": "iVBORw0KGgo="},
}
TEXT_BLOCK = {"type": "text", "text": "이 이미지 설명해줘"}


def _parse(payload):
    assert payload.endswith("\n")
    assert payload.count("\n") == 1
    return json.loads(payload)


def test_new_session_payload_is_user_envelope_with_image_preserved():
    payload = relay._build_stream_json_stdin([TEXT_BLOCK, IMAGE_BLOCK], system_prompt="SYS", is_resume=False)
    msg = _parse(payload)
    assert msg["type"] == "user"
    assert msg["message"]["role"] == "user"
    content = msg["message"]["content"]
    assert content[0]["type"] == "text"
    assert content[0]["text"].startswith("[SYSTEM PROMPT]\nSYS")
    assert content[1:] == [TEXT_BLOCK, IMAGE_BLOCK]


def test_new_session_without_system_prompt_adds_no_block():
    msg = _parse(relay._build_stream_json_stdin([IMAGE_BLOCK], system_prompt="", is_resume=False))
    assert msg["message"]["content"] == [IMAGE_BLOCK]


def test_resume_payload_is_same_envelope_without_system_prompt():
    payload = relay._build_stream_json_stdin([TEXT_BLOCK, IMAGE_BLOCK], system_prompt="SYS", is_resume=True)
    msg = _parse(payload)
    assert msg["type"] == "user"
    assert msg["message"]["role"] == "user"
    assert msg["message"]["content"] == [TEXT_BLOCK, IMAGE_BLOCK]


def test_input_blocks_are_not_mutated():
    blocks = [TEXT_BLOCK, IMAGE_BLOCK]
    relay._build_stream_json_stdin(blocks, system_prompt="SYS", is_resume=False)
    assert blocks == [TEXT_BLOCK, IMAGE_BLOCK]


def test_handle_stream_uses_envelope_helper_for_both_branches():
    src = RELAY_PATH.read_text()
    assert src.count("_build_stream_json_stdin(content_blocks, system_prompt, is_resume)") == 1
    assert "stdin_payload = json.dumps(content_blocks)" not in src
    assert "stdin_payload = json.dumps(blocks)" not in src


def test_failure_detail_is_masked_and_limited():
    secret = "sk-ant-oat01-" + "A" * 40
    detail = relay._cli_failure_detail("boom " + secret, ['{"type":"error","content":"x"}'])
    assert secret not in detail
    assert "stderr=" in detail and "error_events=" in detail
    long_detail = relay._cli_failure_detail("x" * 5000, [])
    assert len(long_detail) <= 1000
    assert relay._cli_failure_detail("", []) == ""


def test_abnormal_end_is_logged_in_handle_stream():
    src = RELAY_PATH.read_text()
    assert "CLI abnormal end:" in src
    assert "not saw_result or proc.returncode != 0 or last_result_error" in src
