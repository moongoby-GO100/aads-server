"""Antigravity(agy) 호스트 직접 실행 relay + model_selector 라우팅.

2026-10-12: relay 가 존재하지 않는 antigravity-test 컨테이너로 docker exec 하고 있었고,
모델 매핑은 낡은 gemini-3.5 계열이었다. 호스트 agy 직접 실행 + 최신 11종 id 로 수리했다.
2026-10-13: 프롬프트를 argv(-p) 대신 stdin(stream-json NDJSON)으로 넘겨 120KB 413 한도를 없앴다.
"""
from __future__ import annotations

import inspect
import json
import os
import stat
import sys
from pathlib import Path

import pytest

from app.services import model_selector
from app.services.intent_router import IntentResult
from tests.unit.test_relay_diagnostics import _load_claude_relay_module

NATIVE_MODELS = [
    "gemini-3.8-flash-high", "gemini-3.8-flash-medium", "gemini-3.8-flash-low",
    "gemini-3.7-flash-high", "gemini-3.7-flash-medium", "gemini-3.7-flash-low",
    "gemini-3.6-flash-high", "gemini-3.6-flash-medium", "gemini-3.6-flash-low",
    "gemini-3.1-pro-high", "gemini-3.1-pro-low",
]
ALIASES = {
    "antigravity": "gemini-3.8-flash-medium",
    "antigravity-flash": "gemini-3.8-flash-medium",
    "antigravity-pro": "gemini-3.1-pro-high",
}


@pytest.fixture(scope="module")
def relay():
    return _load_claude_relay_module()


# ── relay: 매핑 ────────────────────────────────────────────────────────────
@pytest.mark.parametrize("model_id", NATIVE_MODELS)
def test_native_models_pass_through_to_agy(relay, model_id):
    assert relay._resolve_antigravity_model(model_id) == model_id


@pytest.mark.parametrize("alias,target", sorted(ALIASES.items()))
def test_legacy_aliases_point_to_latest(relay, alias, target):
    assert relay._resolve_antigravity_model(alias) == target


@pytest.mark.parametrize("bad", ["gemini-3.5-flash", "gemini-3.8-flash", "", None, "gemini-3.8-flash-ultra"])
def test_unknown_model_is_not_silently_defaulted(relay, bad):
    assert relay._resolve_antigravity_model(bad) is None


def test_map_has_exactly_native_plus_aliases(relay):
    assert set(relay._ANTIGRAVITY_MODEL_MAP) == set(NATIVE_MODELS) | set(ALIASES)


# ── relay: 명령 구성 ───────────────────────────────────────────────────────
def test_command_is_host_direct_sandboxed_with_model_and_timeout(relay):
    cmd = relay._build_antigravity_command("/usr/local/bin/agy", "gemini-3.8-flash-high")
    assert cmd[0] == "/usr/local/bin/agy"
    assert "docker" not in cmd and "exec" not in cmd
    assert "--dangerously-skip-permissions" not in cmd
    assert "--sandbox" in cmd
    assert cmd[cmd.index("--model") + 1] == "gemini-3.8-flash-high"
    assert "--print-timeout" in cmd
    assert cmd[cmd.index("--input-format") + 1] == "stream-json"
    assert cmd[cmd.index("--output-format") + 1] == "stream-json"


def test_command_has_no_prompt_and_empty_p_flag_is_last(relay):
    cmd = relay._build_antigravity_command("agy", "gemini-3.8-flash-high")
    assert cmd[-1] == "-p="  # 셸 표기 -p="" 와 동일. -p 뒤에 다른 플래그가 오면 그것을 프롬프트로 먹는다.
    assert "-p" not in cmd and "--print" not in cmd and "--prompt" not in cmd
    assert cmd == [
        "agy", "--input-format", "stream-json", "--output-format", "stream-json",
        "--model", "gemini-3.8-flash-high", "--print-timeout", relay._ANTIGRAVITY_PRINT_TIMEOUT,
        "--sandbox", "-p=",
    ]


def test_stdin_payload_is_single_user_event_line(relay):
    prompt = '한글 "따옴표"\n둘째 줄\t탭 \\ 역슬래시'
    payload = relay._build_antigravity_stdin_payload(prompt)
    assert payload.endswith(b"\n") and payload.count(b"\n") == 1
    assert json.loads(payload) == {"event": "user", "message": {"content": prompt}}


def test_handler_source_has_no_docker_or_skip_permissions(relay):
    src = inspect.getsource(relay.handle_antigravity_stream) + inspect.getsource(relay._build_antigravity_command)
    assert "docker" not in src
    assert "antigravity-test" not in src
    assert "--dangerously-skip-permissions" not in src
    assert "mkdtemp" in src and "rmtree" in src
    assert "_ANTIGRAVITY_HARD_TIMEOUT_SEC" in src
    assert "_get_antigravity_semaphore" in src


def test_only_one_antigravity_endpoint_exists(relay):
    src = inspect.getsource(relay)
    assert '"/antigravity-stream"' in src
    assert "/agy-stream" not in src


def test_concurrency_default_is_two(relay):
    assert relay._ANTIGRAVITY_MAX_CONCURRENT == int(os.getenv("AGY_RELAY_MAX_CONCURRENT", "2"))
    if "AGY_RELAY_MAX_CONCURRENT" not in os.environ:
        assert relay._ANTIGRAVITY_MAX_CONCURRENT == 2


def test_child_env_drops_relay_secrets(relay, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_AUTH_TOKEN", "sk-ant-oat01-secret")
    monkeypatch.setenv("CLAUDE_RELAY_SHARED_SECRET", "s3cret")
    env = relay._antigravity_clean_env()
    assert "ANTHROPIC_AUTH_TOKEN" not in env
    assert "CLAUDE_RELAY_SHARED_SECRET" not in env
    assert "PATH" in env and "HOME" in env


# ── relay: 핸들러 동작 (가짜 agy 스크립트) ─────────────────────────────────
def _fake_agy(tmp_path: Path, body: str) -> Path:
    path = tmp_path / "fake-agy"
    path.write_text("#!/bin/bash\n" + body)
    path.chmod(path.stat().st_mode | stat.S_IEXEC)
    return path


INIT_EVENT = {
    "event": "init", "conversation_id": "c1",
    "init": {"model": "m", "cwd": "/w", "tools": ["browser_click_element", "run_command"], "permission_mode": "default"},
}


def _step(step_type, state, **extra):
    return {"event": "step_update", "step_update": {
        "conversation_id": "c1", "step_index": 1, "state": state, "step_type": step_type, **extra}}


def _result(status="SUCCESS", response="", error=None):
    body = {"conversation_id": "c1", "status": status, "response": response}
    if error is not None:
        body["error"] = error
    return {"event": "result", "result": body}


def _emit(*events):
    """가짜 agy 가 stream-json 이벤트를 한 줄씩 stdout 으로 내보내게 하는 bash 조각."""
    return "".join("echo '%s'\n" % json.dumps(e, ensure_ascii=False) for e in events)


def _ok_script(text, record=None):
    """stdin 을 끝까지 읽어(record 에 저장) 정상 stream-json 응답을 내는 가짜 agy."""
    sink = str(record) if record else "/dev/null"
    return f"cat > {sink}\n" + _emit(
        INIT_EVENT, _step("user_input", "DONE"),
        _step("agent_response", "ACTIVE", text_delta=text),
        _step("agent_response", "DONE", text_delta="\n"),
        _result(response=text + "\n"),
    )


async def _call(relay, monkeypatch, tmp_path, script_body, payload=None, hard_timeout=None):
    aiohttp_mod = pytest.importorskip("aiohttp")
    import asyncio
    from aiohttp import web
    from aiohttp.test_utils import TestClient, TestServer

    agy = _fake_agy(tmp_path, script_body)
    monkeypatch.setenv("AGY_BIN", str(agy))
    monkeypatch.setattr(relay, "_semaphore", asyncio.Semaphore(4))
    monkeypatch.setattr(relay, "_antigravity_semaphore", None)
    if hard_timeout is not None:
        monkeypatch.setattr(relay, "_ANTIGRAVITY_HARD_TIMEOUT_SEC", hard_timeout)
    app = web.Application()
    app.router.add_post("/antigravity-stream", relay.handle_antigravity_stream)
    body = payload or {"model": "gemini-3.8-flash-high", "messages_text": "ping", "session_id": "t1"}
    async with TestClient(TestServer(app)) as client:
        resp = await client.post("/antigravity-stream", json=body)
        text = await resp.text()
        events = [json.loads(line) for line in text.splitlines() if line.strip()] if resp.status == 200 else []
        return resp.status, events, text


@pytest.mark.asyncio
async def test_success_streams_ndjson_contract_and_cleans_workdir(relay, monkeypatch, tmp_path):
    record = tmp_path / "record.txt"
    stdin_record = tmp_path / "stdin.txt"
    script = (
        f'echo "$@" > {record}\n'
        f'pwd >> {record}\n'
        f'env >> {record}\n'
    ) + _ok_script("hello from agy", stdin_record)
    monkeypatch.setenv("ANTHROPIC_AUTH_TOKEN", "sk-ant-oat01-secret")
    status, events, _ = await _call(relay, monkeypatch, tmp_path, script)
    assert status == 200
    kinds = [e["type"] for e in events]
    assert kinds[-1] == "result" and "error" not in kinds
    text = "".join(e["text"] for e in events if e["type"] == "assistant")
    assert text == "hello from agy\n"
    assert events[-1]["result"] == "hello from agy"
    lines = record.read_text().splitlines()
    assert "--model gemini-3.8-flash-high" in lines[0]
    assert "--sandbox" in lines[0] and "--dangerously-skip-permissions" not in lines[0]
    assert lines[0].endswith("-p=")
    workdir = lines[1]
    assert Path(workdir).name.startswith("agy-relay-")
    assert not Path(workdir).exists()
    assert "sk-ant-oat01-secret" not in record.read_text()
    assert json.loads(stdin_record.read_text()) == {"event": "user", "message": {"content": "ping"}}


@pytest.mark.asyncio
async def test_prompt_goes_to_stdin_not_argv_or_logs(relay, monkeypatch, tmp_path, caplog):
    record = tmp_path / "argv.txt"
    stdin_record = tmp_path / "stdin.txt"
    sentinel = "prompt-sentinel-marker-zz"
    payload = {"model": "gemini-3.8-flash-high", "system_prompt": "sys " + sentinel, "messages_text": "한글 질문\n둘째 줄 " + sentinel}
    script = f'printf "%s\\n" "$@" > {record}\n' + _ok_script("ok", stdin_record)
    with caplog.at_level("DEBUG"):
        status, events, _ = await _call(relay, monkeypatch, tmp_path, script, payload=payload)
    assert status == 200 and events[-1]["type"] == "result"
    assert sentinel not in record.read_text()
    assert sentinel not in caplog.text
    expected_prompt = "[SYSTEM]\n" + payload["system_prompt"] + "\n\n[USER]\n" + payload["messages_text"]
    raw = stdin_record.read_bytes()
    assert raw.endswith(b"\n") and raw.count(b"\n") == 1
    assert json.loads(raw) == {"event": "user", "message": {"content": expected_prompt}}


@pytest.mark.asyncio
async def test_nonzero_exit_returns_error_event(relay, monkeypatch, tmp_path):
    status, events, _ = await _call(relay, monkeypatch, tmp_path, 'echo "boom" >&2\nexit 3\n')
    assert status == 200
    assert events[-1]["type"] == "error"
    assert "code 3" in events[-1]["content"]
    assert not any(e["type"] == "result" for e in events)


@pytest.mark.asyncio
async def test_timeout_returns_error_event_and_kills_child(relay, monkeypatch, tmp_path):
    status, events, _ = await _call(relay, monkeypatch, tmp_path, "sleep 30\n", hard_timeout=0.5)
    assert status == 200
    assert events[-1] == {"type": "error", "content": "Antigravity CLI timeout"}


@pytest.mark.asyncio
async def test_auth_prompt_becomes_error_without_leaking_url(relay, monkeypatch, tmp_path):
    script = 'echo "Authentication required. Please visit the URL to log in:"\necho "  https://accounts.google.com/x"\nsleep 30\n'
    status, events, text = await _call(relay, monkeypatch, tmp_path, script, hard_timeout=5)
    assert events[-1]["type"] == "error"
    assert "authentication" in events[-1]["content"].lower()
    assert "accounts.google.com" not in text


@pytest.mark.asyncio
async def test_empty_output_is_error(relay, monkeypatch, tmp_path):
    status, events, _ = await _call(relay, monkeypatch, tmp_path, "exit 0\n")
    assert events[-1]["type"] == "error"


@pytest.mark.asyncio
async def test_missing_binary_is_error_event(relay, monkeypatch, tmp_path):
    monkeypatch.setattr(relay, "_resolve_antigravity_binary", lambda: "")
    pytest.importorskip("aiohttp")
    import asyncio
    from aiohttp import web
    from aiohttp.test_utils import TestClient, TestServer

    monkeypatch.setattr(relay, "_semaphore", asyncio.Semaphore(4))
    monkeypatch.setattr(relay, "_antigravity_semaphore", None)
    app = web.Application()
    app.router.add_post("/antigravity-stream", relay.handle_antigravity_stream)
    async with TestClient(TestServer(app)) as client:
        resp = await client.post("/antigravity-stream", json={"model": "antigravity", "messages_text": "x"})
        events = [json.loads(line) for line in (await resp.text()).splitlines() if line.strip()]
    assert events[-1]["type"] == "error" and "not installed" in events[-1]["content"]


@pytest.mark.asyncio
async def test_unknown_model_rejected_before_spawn(relay, monkeypatch, tmp_path):
    marker = tmp_path / "spawned"
    status, events, text = await _call(
        relay, monkeypatch, tmp_path, f"touch {marker}\necho hi\n",
        payload={"model": "gemini-3.5-flash", "messages_text": "x"},
    )
    assert status == 400
    assert "unknown_antigravity_model" in text
    assert not marker.exists()


# ── relay: 프로세스 그룹 정리 (부모가 먼저 끝나도 자식이 파이프를 쥔 경우) ──
def _pid_alive(pid: int) -> bool:
    try:
        stat_text = Path(f"/proc/{pid}/stat").read_text()
    except (FileNotFoundError, ProcessLookupError):
        return False
    return stat_text.rsplit(")", 1)[1].split()[0] != "Z"


async def _wait_dead(pid: int, seconds: float = 3.0) -> bool:
    import asyncio

    for _ in range(int(seconds / 0.05)):
        if not _pid_alive(pid):
            return True
        await asyncio.sleep(0.05)
    return not _pid_alive(pid)


@pytest.mark.asyncio
async def test_orphan_child_holding_pipes_is_killed_after_parent_exits(relay, monkeypatch, tmp_path):
    pidfile = tmp_path / "child.pid"
    script = f'sleep 60 &\necho $! > {pidfile}\necho "partial"\nexit 0\n'
    status, events, _ = await _call(relay, monkeypatch, tmp_path, script, hard_timeout=0.7)
    assert status == 200
    assert events[-1] == {"type": "error", "content": "Antigravity CLI timeout"}
    child = int(pidfile.read_text())
    assert await _wait_dead(child), "parent 종료 뒤에도 자식이 살아남았다"


@pytest.mark.asyncio
async def test_detached_child_is_killed_even_on_successful_run(relay, monkeypatch, tmp_path):
    pidfile = tmp_path / "child.pid"
    script = (
        f'sleep 60 >/dev/null 2>&1 </dev/null &\necho $! > {pidfile}\n'
        + _ok_script("done")
    )
    status, events, _ = await _call(relay, monkeypatch, tmp_path, script)
    assert events[-1]["type"] == "result" and events[-1]["result"] == "done"
    assert await _wait_dead(int(pidfile.read_text()))


@pytest.mark.asyncio
async def test_orphan_cleanup_releases_agy_slot(relay, monkeypatch, tmp_path):
    script = 'sleep 60 &\necho x\nexit 0\n'
    await _call(relay, monkeypatch, tmp_path, script, hard_timeout=0.5)
    assert relay._antigravity_semaphore._value == relay._ANTIGRAVITY_MAX_CONCURRENT


# ── relay: stderr 인증·URL 마스킹 ──────────────────────────────────────────
STDERR_AUTH = (
    "echo \"Error: authentication required. Run 'agy' to log in, then retry.\" >&2\n"
    "echo 'Open https://accounts.google.com/o/oauth2/v2/auth?client_id=CID123&state=ST456&code=CODE789' >&2\n"
)


def _assert_no_leak(text: str):
    for leaked in ("accounts.google.com", "client_id", "CID123", "ST456", "CODE789", "oauth2"):
        assert leaked not in text, leaked


@pytest.mark.asyncio
async def test_stderr_auth_failure_is_normalized_without_url(relay, monkeypatch, tmp_path):
    status, events, text = await _call(relay, monkeypatch, tmp_path, STDERR_AUTH + "exit 1\n")
    assert events[-1] == {"type": "error", "content": "Antigravity CLI authentication required"}
    _assert_no_leak(text)


@pytest.mark.asyncio
async def test_stderr_auth_with_empty_stdout_and_exit_zero(relay, monkeypatch, tmp_path):
    status, events, text = await _call(relay, monkeypatch, tmp_path, STDERR_AUTH + "exit 0\n")
    assert events[-1] == {"type": "error", "content": "Antigravity CLI authentication required"}
    _assert_no_leak(text)


@pytest.mark.asyncio
async def test_stderr_auth_hang_reports_auth_not_timeout(relay, monkeypatch, tmp_path):
    status, events, text = await _call(relay, monkeypatch, tmp_path, STDERR_AUTH + "sleep 30\n", hard_timeout=0.7)
    assert events[-1] == {"type": "error", "content": "Antigravity CLI authentication required"}
    _assert_no_leak(text)


@pytest.mark.asyncio
async def test_stderr_non_auth_error_masks_urls_and_tokens(relay, monkeypatch, tmp_path):
    script = "echo 'fetch failed https://example.com/path?token=abc123secret&x=1' >&2\nexit 2\n"
    status, events, text = await _call(relay, monkeypatch, tmp_path, script)
    assert events[-1]["type"] == "error" and "code 2" in events[-1]["content"]
    assert "example.com" not in text and "abc123secret" not in text


def test_mask_helper_covers_urls_queries_and_bearer(relay):
    masked = relay._antigravity_mask_sensitive(
        "go to https://a.b/c?d=e#f then code=XYZ and Bearer ya29.AbCd-123 AIza" + "x" * 30)
    for leaked in ("a.b", "XYZ", "ya29", "AbCd", "AIza"):
        assert leaked not in masked


def test_model_text_mentioning_authentication_is_not_blocked(relay):
    assert not relay._antigravity_stderr_is_auth_failure("")
    assert not any(m in "OAuth 설명: authentication required 는 401 입니다" for m in relay._ANTIGRAVITY_AUTH_MARKERS)


# ── relay: stream-json 출력 해석 ───────────────────────────────────────────
@pytest.mark.asyncio
async def test_meta_events_do_not_leak_into_chat_body(relay, monkeypatch, tmp_path):
    script = "cat >/dev/null\n" + _emit(
        INIT_EVENT,
        _step("user_input", "DONE"),
        _step("thinking", "ACTIVE", text_delta="SECRET-THINKING"),
        _step("tool", "ACTIVE", tool_name="run_command", tool_info={"name": "run_command"}),
        _step("tool", "DONE", tool_name="run_command"),
        _step("agent_response", "ACTIVE", text_delta="답변 "),
        _step("agent_response", "DONE", text_delta="본문\n"),
        _result(response="답변 본문\n"),
    ) + "echo 'not json at all'\n"
    status, events, text = await _call(relay, monkeypatch, tmp_path, script)
    assert status == 200
    body = "".join(e["text"] for e in events if e["type"] == "assistant")
    assert body == "답변 본문\n"
    assert events[-1] == {**events[-1], "type": "result", "result": "답변 본문"}
    for leaked in ("browser_click_element", "run_command", "tools", "permission_mode", "SECRET-THINKING",
                   "step_update", "not json at all", "conversation_id"):
        assert leaked not in text, leaked


@pytest.mark.asyncio
async def test_result_response_is_used_when_no_text_delta(relay, monkeypatch, tmp_path):
    script = "cat >/dev/null\n" + _emit(INIT_EVENT, _result(response="only in result\n"))
    status, events, _ = await _call(relay, monkeypatch, tmp_path, script)
    assert "".join(e["text"] for e in events if e["type"] == "assistant") == "only in result\n"
    assert events[-1]["type"] == "result" and events[-1]["result"] == "only in result"


@pytest.mark.asyncio
async def test_result_response_is_not_duplicated_after_deltas(relay, monkeypatch, tmp_path):
    status, events, _ = await _call(relay, monkeypatch, tmp_path, _ok_script("once"))
    assert "".join(e["text"] for e in events if e["type"] == "assistant") == "once\n"
    assert events[-1]["result"] == "once"


@pytest.mark.asyncio
async def test_result_error_takes_failure_path_and_masks_detail(relay, monkeypatch, tmp_path):
    script = "cat >/dev/null\n" + _emit(
        INIT_EVENT,
        _step("agent_response", "ACTIVE", text_delta="partial"),
        _result(status="ERROR", error="model failed see https://example.com/x?token=abc123secret"),
    )
    status, events, text = await _call(relay, monkeypatch, tmp_path, script)
    assert status == 200
    assert events[-1]["type"] == "error" and "Antigravity CLI error" in events[-1]["content"]
    assert not any(e["type"] == "result" for e in events)
    assert "example.com" not in text and "abc123secret" not in text


@pytest.mark.asyncio
async def test_result_error_with_auth_marker_is_normalized(relay, monkeypatch, tmp_path):
    script = "cat >/dev/null\n" + _emit(_result(status="ERROR", error="authentication failed or timed out"))
    status, events, _ = await _call(relay, monkeypatch, tmp_path, script)
    assert events[-1] == {"type": "error", "content": "Antigravity CLI authentication required"}


@pytest.mark.asyncio
async def test_success_without_any_text_is_empty_response_error(relay, monkeypatch, tmp_path):
    script = "cat >/dev/null\n" + _emit(INIT_EVENT, _step("tool", "DONE", tool_name="run_command"), _result(response=""))
    status, events, _ = await _call(relay, monkeypatch, tmp_path, script)
    assert events[-1] == {"type": "error", "content": "Antigravity CLI returned empty response"}


# ── relay: 프롬프트 크기 (stdin 전달, argv 한도 없음) ───────────────────────
def _chat_payload(system_chars: int, turns: int, hangul_ratio: float = 1.0):
    hangul = int(system_chars * hangul_ratio)
    system = "가" * hangul + "a" * (system_chars - hangul)
    history = "\n\n".join(f"[{'user' if i % 2 == 0 else 'assistant'}] " + "나다라" * 100 for i in range(turns))
    return {"model": "gemini-3.8-flash-high", "system_prompt": system, "messages_text": history, "session_id": "size"}


def _expected_prompt(payload):
    return "[SYSTEM]\n" + payload["system_prompt"] + "\n\n[USER]\n" + payload["messages_text"]


@pytest.mark.asyncio
async def test_200kb_prompt_passes_without_413_and_arrives_intact(relay, monkeypatch, tmp_path):
    stdin_record = tmp_path / "stdin.bin"
    payload = {"model": "gemini-3.8-flash-high", "system_prompt": "a" * 100_000,
               "messages_text": "한" * 33_400, "session_id": "big"}
    expected = _expected_prompt(payload)
    assert len(expected.encode()) > 200_000
    status, events, _ = await _call(
        relay, monkeypatch, tmp_path, _ok_script("big-ok", stdin_record), payload=payload)
    assert status == 200 and events[-1]["type"] == "result" and events[-1]["result"] == "big-ok"
    raw = stdin_record.read_bytes()
    assert raw.count(b"\n") == 1
    assert json.loads(raw)["message"]["content"] == expected


@pytest.mark.asyncio
async def test_prod_max_korean_system_prompt_is_accepted_now(relay, monkeypatch, tmp_path):
    """한글 50,000자 시스템 프롬프트(150,000B)는 예전 argv 한도에서 413 이었다. 이제 stdin 으로 통과한다."""
    stdin_record = tmp_path / "stdin.bin"
    payload = _chat_payload(50_000, 20)
    assert len(_expected_prompt(payload).encode()) > 150_000
    status, events, _ = await _call(
        relay, monkeypatch, tmp_path, _ok_script("fine", stdin_record), payload=payload)
    assert status == 200 and events[-1]["type"] == "result"
    assert json.loads(stdin_record.read_bytes())["message"]["content"] == _expected_prompt(payload)


@pytest.mark.asyncio
async def test_prompt_over_safety_limit_is_413_before_spawn(relay, monkeypatch, tmp_path):
    marker = tmp_path / "spawned"
    monkeypatch.setattr(relay, "_ANTIGRAVITY_MAX_PROMPT_BYTES", 5_000)
    payload = {"model": "gemini-3.8-flash-high", "messages_text": "가" * 2_000, "session_id": "over"}
    status, events, text = await _call(
        relay, monkeypatch, tmp_path, f"touch {marker}\n" + _ok_script("x"), payload=payload)
    assert status == 413
    body = json.loads(text)
    assert body["error"] == "antigravity_prompt_too_large"
    assert body["prompt_bytes"] > 5_000 and body["limit_bytes"] == 5_000
    assert "argv" not in body["detail"]
    assert not marker.exists()


@pytest.mark.asyncio
async def test_prompt_exactly_at_safety_limit_is_accepted(relay, monkeypatch, tmp_path):
    payload = {"model": "gemini-3.8-flash-high", "messages_text": "q" * 1_000, "session_id": "edge"}
    monkeypatch.setattr(relay, "_ANTIGRAVITY_MAX_PROMPT_BYTES", 1_000)
    status, events, _ = await _call(relay, monkeypatch, tmp_path, _ok_script("edge-ok"), payload=payload)
    assert status == 200 and events[-1]["type"] == "result"


def test_prompt_limit_default_is_safety_cap_not_argv_limit(relay):
    if "ANTIGRAVITY_MAX_PROMPT_BYTES" not in os.environ:
        assert relay._ANTIGRAVITY_MAX_PROMPT_BYTES == 2_000_000
    assert relay._ANTIGRAVITY_MAX_PROMPT_BYTES == int(os.getenv("ANTIGRAVITY_MAX_PROMPT_BYTES", "2000000"))


@pytest.mark.asyncio
async def test_stdin_broken_pipe_without_result_is_failure(relay, monkeypatch, tmp_path):
    """agy 가 stdin 을 읽지 않고 끝나면(BrokenPipe) 결과 이벤트가 없는 한 실패로 처리한다."""
    payload = {"model": "gemini-3.8-flash-high", "messages_text": "x" * 900_000, "session_id": "pipe"}
    status, events, _ = await _call(relay, monkeypatch, tmp_path, "exit 0\n", payload=payload)
    assert status == 200
    assert events[-1]["type"] == "error"
    assert not any(e["type"] == "result" for e in events)


@pytest.mark.asyncio
async def test_large_prompt_with_slow_reader_does_not_deadlock(relay, monkeypatch, tmp_path):
    """stdout 을 먼저 쓰기 시작하는 agy 에도 stdin 쓰기와 stdout 읽기가 병행되어 교착이 없다."""
    stdin_record = tmp_path / "stdin.bin"
    payload = {"model": "gemini-3.8-flash-high", "messages_text": "y" * 600_000, "session_id": "dl"}
    chatter = "".join(
        _emit(_step("agent_response", "ACTIVE", text_delta="z" * 80)) for _ in range(3))
    script = chatter + f"cat > {stdin_record}\n" + _emit(_result(response="done\n"))
    status, events, _ = await _call(relay, monkeypatch, tmp_path, script, payload=payload, hard_timeout=20)
    assert status == 200 and events[-1]["type"] == "result"
    assert len(stdin_record.read_bytes()) > 600_000


# ── model_selector: 집합·표시명 ────────────────────────────────────────────
def test_selector_set_has_all_native_ids_and_aliases():
    assert set(NATIVE_MODELS) | set(ALIASES) <= model_selector._ANTIGRAVITY_MODELS
    for model_id in NATIVE_MODELS:
        assert model_selector._ANTIGRAVITY_MODEL_DISPLAY[model_id]


def test_antigravity_ids_never_collide_with_litellm_gemini_ids():
    assert not (model_selector._ANTIGRAVITY_MODELS & model_selector._GEMINI_MODELS)
    for plain in ("gemini-3.8-flash", "gemini-3.7-flash", "gemini-3.6-flash"):
        assert plain in model_selector._GEMINI_MODELS
        assert plain not in model_selector._ANTIGRAVITY_MODELS


def test_relay_map_and_selector_set_agree(relay):
    assert set(relay._ANTIGRAVITY_MODEL_MAP) == model_selector._ANTIGRAVITY_MODELS


# ── model_selector: 라우팅 순서 ────────────────────────────────────────────
def _row(provider, model_id, backend=None):
    return {
        "provider": provider, "model_id": model_id, "is_active": True, "is_executable": True,
        "verification_status": "verified",
        "metadata": {"execution_backend": backend} if backend else {},
    }


def _patch_routing(monkeypatch, rows, calls, antigravity_events=None, codex_events=None):
    async def registered(active_only=False):
        return rows

    async def models():
        return {row["model_id"] for row in rows}

    async def key(*_a, **_k):
        return ""

    async def antigravity(model, *_a, **_k):
        calls.append(("antigravity", model))
        for event in antigravity_events or [{"type": "delta", "content": "agy-ok"}, {"type": "done", "model": model}]:
            yield event

    async def litellm(model, *_a, **_k):
        calls.append(("litellm", model))
        yield {"type": "delta", "content": "lite-ok"}
        yield {"type": "done", "model": model}

    async def litellm_openai(model, *_a, **_k):
        calls.append(("litellm_openai", model))
        yield {"type": "done", "model": model}

    async def codex(model, *_a, **_k):
        calls.append(("codex", model))
        for event in codex_events or [{"type": "delta", "content": "codex-ok"}, {"type": "done", "model": model}]:
            yield event

    async def codex_fallback_model():
        return "gpt-5.5"

    monkeypatch.setattr(model_selector, "_get_db_key", key)
    monkeypatch.setattr(model_selector, "_list_registered_models", registered)
    monkeypatch.setattr(model_selector, "get_available_model_ids", models)
    monkeypatch.setattr(model_selector, "_stream_antigravity_relay", antigravity)
    monkeypatch.setattr(model_selector, "_stream_litellm", litellm)
    monkeypatch.setattr(model_selector, "_stream_litellm_openai", litellm_openai)
    monkeypatch.setattr(model_selector, "_stream_codex_relay", codex)
    monkeypatch.setattr(model_selector, "_get_codex_cli_fallback_model_from_db", codex_fallback_model)


async def _run(selected):
    return [event async for event in model_selector.call_stream(
        IntentResult(intent="code_modify", model=selected, use_tools=False, tool_group=""),
        "system", [{"role": "user", "content": "hello"}], model_override=selected,
    )]


@pytest.mark.asyncio
@pytest.mark.parametrize("model_id", NATIVE_MODELS)
async def test_native_ids_route_to_antigravity_relay(monkeypatch, model_id):
    calls = []
    _patch_routing(monkeypatch, [_row("antigravity", model_id)], calls)
    events = await _run(model_id)
    assert calls == [("antigravity", model_id)]
    assert events[-1]["type"] == "done"


@pytest.mark.asyncio
async def test_plain_gemini_id_keeps_litellm_route(monkeypatch):
    calls = []
    rows = [_row("gemini", "gemini-3.8-flash"), _row("antigravity", "gemini-3.8-flash-high")]
    _patch_routing(monkeypatch, rows, calls)
    await _run("gemini-3.8-flash")
    assert [c[0] for c in calls] == ["litellm"]
    assert calls[0][1] == "gemini-3.8-flash"


@pytest.mark.asyncio
async def test_plain_gemini_id_with_litellm_proxy_row_stays_litellm(monkeypatch):
    calls = []
    rows = [_row("gemini", "gemini-3.8-flash", "litellm_proxy")]
    _patch_routing(monkeypatch, rows, calls)
    await _run("gemini-3.8-flash")
    assert [c[0] for c in calls] == ["litellm_openai"]


@pytest.mark.asyncio
async def test_antigravity_wins_even_if_registry_row_says_litellm_proxy(monkeypatch):
    calls = []
    model_id = "gemini-3.8-flash-high"
    _patch_routing(monkeypatch, [_row("antigravity", model_id, "litellm_proxy")], calls)
    await _run(model_id)
    assert calls == [("antigravity", model_id)]


@pytest.mark.asyncio
async def test_antigravity_failure_falls_back_to_codex_chain(monkeypatch):
    calls = []
    model_id = "gemini-3.1-pro-high"
    _patch_routing(
        monkeypatch, [_row("antigravity", model_id)], calls,
        antigravity_events=[{"type": "error", "content": "Antigravity CLI timeout"}],
    )
    events = await _run(model_id)
    assert calls == [("antigravity", model_id), ("codex", "gpt-5.5")]
    assert any("Codex CLI gpt-5.5" in str(e.get("content", "")) for e in events)
    assert events[-1]["type"] == "done" and events[-1]["model"] == model_id


# ── relay: /health ─────────────────────────────────────────────────────────
def test_health_reports_antigravity_host_direct(relay, monkeypatch):
    import asyncio

    monkeypatch.setattr(relay, "_relay_secret_ok", lambda request: True)
    monkeypatch.setattr(relay, "_DIRECT_OAUTH_ENABLED", False)
    monkeypatch.setattr(relay, "_read_oauth_tokens", lambda: ("", "", "", "", ""))
    response = asyncio.run(relay.handle_health(object()))
    payload = json.loads(response.body)
    assert payload["antigravity_cmd_mode"] == "host_direct"
