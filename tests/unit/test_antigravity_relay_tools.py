"""Antigravity(agy) relay — 채팅 MCP 도구 모드·러너 작업 폴더 모드·Pro 별칭 (AADS-AGY-TOOLS-AND-PRO-ALIAS-20261106).

2026-11-06 이전 relay 는 매 요청 빈 mkdtemp 폴더에서 도구 없는 에이전트로만 agy 를 돌렸다.
그래서 채팅 AGY 는 AADS 도구를 못 쓰고, 러너 AGY 는 워크트리 파일을 고칠 수 없었다.
실제 agy 는 부르지 않는다 — 가짜 agy 스크립트로 argv·cwd·작업 폴더 내용·출력 변환을 검증한다.
stream-json 샘플은 2026-11-06 호스트 agy 실측 출력을 그대로 옮겼다.
"""
from __future__ import annotations

import json
import os
import stat
from pathlib import Path

import pytest

from tests.unit.test_relay_diagnostics import _load_claude_relay_module

MODEL = "gemini-3.8-flash-high"
SERVER = "aads-tools"


@pytest.fixture(scope="module")
def relay():
    return _load_claude_relay_module()


def _fake_agy(tmp_path: Path, body: str) -> Path:
    path = tmp_path / "fake-agy"
    path.write_text("#!/bin/bash\n" + body)
    path.chmod(path.stat().st_mode | stat.S_IEXEC)
    return path


def _emit(*events):
    return "".join("echo '%s'\n" % json.dumps(e, ensure_ascii=False).replace("'", "'\\''") for e in events)


def _step(step_type, state, index=1, **extra):
    return {"event": "step_update", "step_update": {
        "conversation_id": "095aff25-482e", "step_index": index, "state": state, "step_type": step_type, **extra}}


def _result(response="", status="SUCCESS", **extra):
    return {"event": "result", "result": {"conversation_id": "095aff25-482e", "status": status,
                                           "response": response, **extra}}


MCP_INFO = {"name": "call_mcp_tool", "parameters": {
    "Arguments": {"query": "SELECT 1"}, "ServerName": "aads-tools_aads-tools", "ToolName": "query_database"}}
MCP_ACTIVE = _step("tool", "ACTIVE", index=2, tool_name="call_mcp_tool", tool_info=MCP_INFO)
MCP_DONE = _step("tool", "DONE", index=2, tool_name="call_mcp_tool",
                 tool_info={**MCP_INFO, "output": '{"rows": [[1]]}'})


def _record_script(record: Path, extra: str = "") -> str:
    """argv·cwd·작업 폴더 내용을 record 에 남기는 가짜 agy 머리말."""
    return (
        f'printf "%s\\n" "$@" > {record}.argv\n'
        f'pwd > {record}.cwd\n'
        f'(find . -path ./.git -prune -o -type f -print | sort) > {record}.files\n'
        f'if [ -f .agents/plugins/aads-tools/mcp_config.json ]; then cp .agents/plugins/aads-tools/mcp_config.json {record}.mcp; fi\n'
        f'if [ -f .agents/agents/aads-chat-tools.md ]; then cp .agents/agents/aads-chat-tools.md {record}.agent; fi\n'
        + extra
    )


def _ok(text="답"):
    return "cat >/dev/null\n" + _emit(_step("agent_response", "DONE", text_delta=text), _result(response=text))


@pytest.fixture
def mcp_template(relay, monkeypatch, tmp_path):
    template = tmp_path / "mcp_template.json"
    template.write_text(json.dumps({"mcpServers": {SERVER: {"command": "/bin/true", "args": []}}}))
    monkeypatch.setattr(relay, "MCP_TEMPLATE", template)
    return template


async def _call(relay, monkeypatch, tmp_path, script_body, payload):
    pytest.importorskip("aiohttp")
    import asyncio
    from aiohttp import web
    from aiohttp.test_utils import TestClient, TestServer

    monkeypatch.setenv("AGY_BIN", str(_fake_agy(tmp_path, script_body)))
    monkeypatch.setattr(relay, "_semaphore", asyncio.Semaphore(4))
    monkeypatch.setattr(relay, "_antigravity_semaphore", None)
    app = web.Application()
    app.router.add_post("/antigravity-stream", relay.handle_antigravity_stream)
    async with TestClient(TestServer(app)) as client:
        resp = await client.post("/antigravity-stream", json=payload)
        text = await resp.text()
        events = [json.loads(line) for line in text.splitlines() if line.strip()] if resp.status == 200 else []
        return resp.status, events, text


def _argv(record: Path) -> list[str]:
    return Path(str(record) + ".argv").read_text().splitlines()


def _settings(argv: list[str]) -> dict:
    return json.loads(argv[argv.index("--settings") + 1])


# ── 1. Pro 별칭 / 사라진 모델 ──────────────────────────────────────────────
def test_pro_alias_points_to_gemini_4_argon_high(relay):
    assert relay._resolve_antigravity_model("antigravity-pro") == "gemini-4-argon-high"
    assert "gemini-4-argon-high" in relay._ANTIGRAVITY_NATIVE_MODELS


@pytest.mark.asyncio
@pytest.mark.parametrize("model_id", ["gemini-3.1-pro-high", "gemini-3.7-flash-low", "gemini-3.6-flash-medium"])
async def test_retired_model_request_is_400_before_spawn(relay, monkeypatch, tmp_path, model_id):
    marker = tmp_path / "spawned"
    status, _, text = await _call(relay, monkeypatch, tmp_path, f"touch {marker}\n",
                                  {"model": model_id, "messages_text": "hi"})
    assert status == 400
    assert json.loads(text)["error"] == "retired_antigravity_model"
    assert not marker.exists()


# ── 2. 채팅 도구 모드 명령 ─────────────────────────────────────────────────
@pytest.mark.asyncio
@pytest.mark.parametrize("trigger", [{"allow_tools": True}, {"tools": [{"name": "query_database"}]}])
async def test_tools_request_uses_tools_agent_with_session_mcp_plugin(relay, monkeypatch, tmp_path, mcp_template, trigger):
    record = tmp_path / "rec"
    payload = {"model": MODEL, "messages_text": "hi", "session_id": "sess-8bf0405a", **trigger}
    status, events, _ = await _call(relay, monkeypatch, tmp_path, _record_script(record) + _ok(), payload)
    assert status == 200 and events[-1]["type"] == "result" and events[-1]["mode"] == "tools"
    argv = _argv(record)
    assert argv[argv.index("--agent") + 1] == relay._ANTIGRAVITY_TOOLS_AGENT
    assert "--dangerously-skip-permissions" not in argv and "--mode" not in argv
    assert "--sandbox" in argv and argv[-1] == "-p="
    assert argv[argv.index("--print-timeout") + 1] == "%ds" % (int(relay._ANTIGRAVITY_TOOLS_HARD_TIMEOUT_SEC) - 30)
    perms = _settings(argv)["permissions"]
    # AADS 플러그인 서버만 허용 — 전역 MCP(mcp(*))나 셸은 허용하지 않는다.
    assert perms["allow"] == ["mcp(aads-tools_aads-tools/*)"]
    assert perms["deny"] == ["command(*)"]
    # Claude 경로와 같은 템플릿·같은 세션 id 가 주입된 MCP 설정
    mcp = json.loads(Path(str(record) + ".mcp").read_text())
    assert mcp["mcpServers"][SERVER]["command"] == "/bin/true"
    assert mcp["mcpServers"][SERVER]["env"]["AADS_SESSION_ID"] == "sess-8bf0405a"
    agent_md = Path(str(record) + ".agent").read_text()
    front = agent_md.split("---")[1]
    assert "tools: []" in front and "mainAgent: true" in front
    # inheritMcp: false 는 워크스페이스 플러그인 MCP 까지 끊는다(2026-11-06 실측) — 넣지 않는다.
    assert "inheritMcp" not in front


def test_tools_agent_blocks_builtin_tools(relay):
    front = relay._ANTIGRAVITY_TOOLS_AGENT_MD.split("---")[1]
    # 내장 도구(셸·파일 쓰기)는 tools: [] 로 없앤다. MCP 는 tools 항목이 아니라 플러그인으로 붙는다.
    assert "tools: []" in front
    assert "call_mcp_tool" not in front and "run_command" not in front


@pytest.mark.asyncio
async def test_no_tools_request_keeps_text_agent_command_exactly(relay, monkeypatch, tmp_path, mcp_template):
    record = tmp_path / "rec"
    agy = tmp_path / "fake-agy"
    status, events, _ = await _call(relay, monkeypatch, tmp_path, _record_script(record) + _ok(),
                                    {"model": MODEL, "messages_text": "hi", "session_id": "s1"})
    assert status == 200 and events[-1]["type"] == "result"
    assert "mode" not in events[-1]
    # "$@" 에는 argv[0](agy 경로)이 없다.
    assert _argv(record) == relay._build_antigravity_command(str(agy), MODEL, agent=relay._ANTIGRAVITY_TEXT_AGENT)[1:]
    assert "plugins" not in Path(str(record) + ".files").read_text()


@pytest.mark.asyncio
async def test_allow_tools_false_overrides_tools_list(relay, monkeypatch, tmp_path, mcp_template):
    record = tmp_path / "rec"
    payload = {"model": MODEL, "messages_text": "hi", "session_id": "s1", "tools": [{"name": "x"}], "allow_tools": False}
    await _call(relay, monkeypatch, tmp_path, _record_script(record) + _ok(), payload)
    argv = _argv(record)
    assert argv[argv.index("--agent") + 1] == relay._ANTIGRAVITY_TEXT_AGENT and "--settings" not in argv


def test_session_only_switch_is_off_by_default(relay, monkeypatch):
    assert relay._antigravity_tools_requested({}, "sess") is relay._ANTIGRAVITY_TOOLS_BY_SESSION
    monkeypatch.setattr(relay, "_ANTIGRAVITY_TOOLS_BY_SESSION", True)
    assert relay._antigravity_tools_requested({}, "sess") is True
    assert relay._antigravity_tools_requested({}, "") is False
    assert relay._antigravity_tools_requested({"allow_tools": False}, "sess") is False
    if "AGY_RELAY_TOOLS_BY_SESSION" not in os.environ:
        monkeypatch.undo()
        assert relay._ANTIGRAVITY_TOOLS_BY_SESSION is False


# ── 3. MCP 설정 실패 → 텍스트 모드 + 경고 ───────────────────────────────────
@pytest.mark.asyncio
async def test_mcp_config_failure_falls_back_to_text_with_warning_event(relay, monkeypatch, tmp_path, caplog):
    def broken(_session_id):
        raise ValueError("template unreadable")

    monkeypatch.setattr(relay, "_load_mcp_template", broken)
    record = tmp_path / "rec"
    with caplog.at_level("WARNING"):
        status, events, _ = await _call(relay, monkeypatch, tmp_path, _record_script(record) + _ok(),
                                        {"model": MODEL, "messages_text": "hi", "session_id": "s1", "allow_tools": True})
    assert status == 200
    warnings = [e for e in events if e["type"] == "system" and e.get("subtype") == "warning"]
    assert warnings and warnings[0]["code"] == "antigravity_tools_fallback_text"
    assert "template unreadable" in warnings[0]["content"]
    assert events[-1]["type"] == "result" and "mode" not in events[-1]
    argv = _argv(record)
    assert argv[argv.index("--agent") + 1] == relay._ANTIGRAVITY_TEXT_AGENT and "--settings" not in argv
    assert "plugins" not in Path(str(record) + ".files").read_text()
    assert "falling back to text" in caplog.text


@pytest.mark.asyncio
async def test_empty_mcp_template_also_falls_back(relay, monkeypatch, tmp_path):
    monkeypatch.setattr(relay, "_load_mcp_template", lambda _sid: {"mcpServers": {}})
    status, events, _ = await _call(relay, monkeypatch, tmp_path, _ok(),
                                    {"model": MODEL, "messages_text": "hi", "allow_tools": True})
    assert any(e.get("code") == "antigravity_tools_fallback_text" for e in events)
    assert events[-1]["type"] == "result"


# ── 4. 러너 작업 폴더 모드 ─────────────────────────────────────────────────
@pytest.fixture
def wt_root(relay, monkeypatch, tmp_path):
    monkeypatch.setattr(relay, "_ANTIGRAVITY_WORKSPACE_PREFIX", str(tmp_path / "aads-wt-"))
    worktree = tmp_path / "aads-wt-runner-abc"
    worktree.mkdir()
    (worktree / "app.py").write_text("x = 1\n")
    return worktree


@pytest.mark.asyncio
async def test_runner_workspace_runs_in_worktree_with_edit_and_shell_permissions(relay, monkeypatch, tmp_path, wt_root):
    record = tmp_path / "rec"
    script = _record_script(record) + "cat >/dev/null\n" + _emit(
        _step("tool", "ACTIVE", index=1, tool_name="write_file", tool_info={"name": "write_file", "parameters": {"Path": "app.py"}}),
        _step("tool", "DONE", index=1, tool_name="write_file", tool_info={"name": "write_file", "parameters": {"Path": "app.py"}}),
        _result(response=""),
    )
    payload = {"model": MODEL, "messages_text": "fix", "session_id": "runner-abc", "workspace": str(wt_root)}
    status, events, _ = await _call(relay, monkeypatch, tmp_path, script, payload)
    assert status == 200
    # 파일만 고치고 말이 없는 러너 턴은 실패가 아니다 — 판정은 러너가 diff·테스트로 한다.
    assert events[-1]["type"] == "result" and events[-1]["mode"] == "runner"
    assert events[-1]["tool_calls"] == 1 and events[-1]["agy_model"] == MODEL
    assert Path(str(record) + ".cwd").read_text().strip() == str(wt_root)
    argv = _argv(record)
    assert argv[argv.index("--mode") + 1] == "accept-edits"
    assert "--agent" not in argv and "--dangerously-skip-permissions" not in argv
    assert argv[argv.index("--print-timeout") + 1] == "%ds" % (int(relay._ANTIGRAVITY_RUNNER_HARD_TIMEOUT_SEC) - 30)
    perms = _settings(argv)["permissions"]
    assert perms["allow"] == ["command(*)"]
    for forbidden in ("git commit", "git push", "git add", "docker build", "docker compose", "kill"):
        assert "command(%s)" % forbidden in perms["deny"]
    # 러너 워크트리는 러너 소유 — 지우지 않고, .agents 같은 흔적도 남기지 않는다.
    assert wt_root.is_dir() and (wt_root / "app.py").exists()
    assert not (wt_root / ".agents").exists()
    assert Path(str(record) + ".files").read_text().split() == ["./app.py"]


@pytest.mark.asyncio
async def test_runner_subdirectory_of_worktree_is_allowed(relay, monkeypatch, tmp_path, wt_root):
    sub = wt_root / "scripts"
    sub.mkdir()
    record = tmp_path / "rec"
    status, events, _ = await _call(relay, monkeypatch, tmp_path, _record_script(record) + _ok("done"),
                                    {"model": MODEL, "messages_text": "fix", "workspace": str(sub)})
    assert status == 200 and events[-1]["mode"] == "runner"
    assert Path(str(record) + ".cwd").read_text().strip() == str(sub)


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["outside", "missing", "prefix_only", "file", "dotdot", "symlink"])
async def test_runner_workspace_outside_allowed_root_is_400(relay, monkeypatch, tmp_path, wt_root, kind):
    outside = tmp_path / "elsewhere"
    outside.mkdir()
    bare = tmp_path / "aads-wt-"
    bare.mkdir()
    a_file = tmp_path / "aads-wt-x.log"
    a_file.write_text("log")
    link = tmp_path / "aads-wt-link"
    link.symlink_to(outside)
    workspace = {
        "outside": str(outside),
        "missing": str(tmp_path / "aads-wt-nope"),
        "prefix_only": str(bare),
        "file": str(a_file),
        "dotdot": str(wt_root / ".." / "elsewhere"),
        "symlink": str(link),
    }[kind]
    marker = tmp_path / "spawned"
    status, _, text = await _call(relay, monkeypatch, tmp_path, f"touch {marker}\n",
                                  {"model": MODEL, "messages_text": "fix", "workspace": workspace})
    assert status == 400 and json.loads(text)["error"] == "invalid_antigravity_workspace"
    assert not marker.exists()
    assert outside.is_dir()


def test_default_workspace_root_is_runner_worktrees(relay):
    if "AGY_RELAY_WORKSPACE_PREFIX" not in os.environ:
        assert relay._ANTIGRAVITY_WORKSPACE_PREFIX == "/tmp/aads-wt-"


def test_mode_timeouts_are_separate_from_text_timeout(relay):
    env = os.environ
    if "AGY_RELAY_HARD_TIMEOUT_SEC" not in env:
        assert relay._ANTIGRAVITY_HARD_TIMEOUT_SEC == 270
    if "AGY_RELAY_TOOLS_HARD_TIMEOUT_SEC" not in env:
        assert relay._ANTIGRAVITY_TOOLS_HARD_TIMEOUT_SEC == 600
    if "AGY_RELAY_RUNNER_HARD_TIMEOUT_SEC" not in env:
        assert relay._ANTIGRAVITY_RUNNER_HARD_TIMEOUT_SEC == 3600
    # lease watchdog(기본 5400초)이 러너 턴을 먼저 죽이지 않아야 한다.
    assert relay._ANTIGRAVITY_RUNNER_HARD_TIMEOUT_SEC < relay._MAX_LEASE_SEC
    if "AGY_RELAY_MAX_CONCURRENT" not in env:
        assert relay._ANTIGRAVITY_MAX_CONCURRENT == 2


# ── 5. stream-json 도구 이벤트 → Claude 형식 tool_use / tool_result ─────────
def test_interpret_mcp_tool_steps(relay):
    kind, use = relay._antigravity_interpret_line(json.dumps(MCP_ACTIVE))
    assert kind == "tool_use"
    assert use == {"id": "agy_095aff25_2", "name": "mcp__aads-tools__query_database", "input": {"query": "SELECT 1"}}
    kind, res = relay._antigravity_interpret_line(json.dumps(MCP_DONE))
    assert kind == "tool_result"
    assert res["tool_use_id"] == "agy_095aff25_2" and res["content"] == '{"rows": [[1]]}' and res["is_error"] is False


def test_interpret_tool_error_step(relay):
    err = _step("tool", "ERROR", index=2, tool_name="call_mcp_tool", tool_info={
        **MCP_INFO, "error": {"type": "TOOL_ERROR", "message": "permission check failed for mcp"}})
    kind, res = relay._antigravity_interpret_line(json.dumps(err))
    assert kind == "tool_result" and res["is_error"] is True and "permission check failed" in res["content"]


def test_interpret_builtin_tool_keeps_name_and_parameters(relay):
    step = _step("tool", "ACTIVE", index=4, tool_name="run_command",
                 tool_info={"name": "run_command", "parameters": {"CommandLine": "wc -c hello.txt"}})
    assert relay._antigravity_interpret_line(json.dumps(step)) == (
        "tool_use", {"id": "agy_095aff25_4", "name": "run_command", "input": {"CommandLine": "wc -c hello.txt"}})


@pytest.mark.asyncio
async def test_tool_mode_stream_emits_claude_shaped_tool_events(relay, monkeypatch, tmp_path, mcp_template):
    script = "cat >/dev/null\n" + _emit(
        _step("user_input", "DONE", index=0),
        MCP_ACTIVE, MCP_DONE,
        _step("agent_response", "DONE", index=3, text_delta="1"),
        _result(response="1"),
    )
    status, events, _ = await _call(relay, monkeypatch, tmp_path, script,
                                    {"model": MODEL, "messages_text": "q", "session_id": "s", "allow_tools": True})
    assert status == 200
    use = [e for e in events if e["type"] == "assistant" and "message" in e]
    res = [e for e in events if e["type"] == "user"]
    assert use == [{"type": "assistant", "message": {"role": "assistant", "content": [{
        "type": "tool_use", "id": "agy_095aff25_2", "name": "mcp__aads-tools__query_database",
        "input": {"query": "SELECT 1"}}]}}]
    assert res == [{"type": "user", "message": {"role": "user", "content": [{
        "type": "tool_result", "tool_use_id": "agy_095aff25_2", "content": '{"rows": [[1]]}', "is_error": False}]}}]
    assert events.index(use[0]) < events.index(res[0])
    # 본문 텍스트는 기존 계약 그대로(assistant/subtype=text)
    assert "".join(e["text"] for e in events if e.get("subtype") == "text") == "1"
    assert events[-1]["type"] == "result" and events[-1]["tool_calls"] == 1 and events[-1]["tool_errors"] == 0


@pytest.mark.asyncio
async def test_tool_result_without_active_step_gets_tool_use_first(relay, monkeypatch, tmp_path, mcp_template):
    script = "cat >/dev/null\n" + _emit(MCP_DONE, _step("agent_response", "DONE", index=3, text_delta="ok"), _result("ok"))
    _, events, _ = await _call(relay, monkeypatch, tmp_path, script,
                               {"model": MODEL, "messages_text": "q", "allow_tools": True})
    kinds = [(e["type"], e["message"]["content"][0]["type"]) for e in events if "message" in e]
    assert kinds == [("assistant", "tool_use"), ("user", "tool_result")]


@pytest.mark.asyncio
async def test_denied_actions_in_tool_mode_emit_warning(relay, monkeypatch, tmp_path, mcp_template):
    script = "cat >/dev/null\n" + _emit(
        _step("agent_response", "DONE", text_delta="못 했습니다"),
        _result(response="못 했습니다", denied_actions=[{"action": "mcp", "display_name": "CallMcpTool"}]),
    )
    _, events, _ = await _call(relay, monkeypatch, tmp_path, script,
                               {"model": MODEL, "messages_text": "q", "allow_tools": True})
    assert any(e.get("code") == "antigravity_denied_actions" for e in events)


@pytest.mark.asyncio
async def test_tool_mode_without_any_answer_text_is_still_an_error(relay, monkeypatch, tmp_path, mcp_template):
    script = "cat >/dev/null\n" + _emit(MCP_ACTIVE, MCP_DONE, _result(response=""))
    _, events, _ = await _call(relay, monkeypatch, tmp_path, script,
                               {"model": MODEL, "messages_text": "q", "allow_tools": True})
    assert events[-1] == {"type": "error", "content": "Antigravity CLI returned empty response"}


# ── 6. 요청 종료 시 임시 MCP 설정·폴더 삭제 ─────────────────────────────────
@pytest.mark.asyncio
@pytest.mark.parametrize("ending", ["ok", "error"])
async def test_temp_mcp_config_and_workdir_are_removed(relay, monkeypatch, tmp_path, mcp_template, ending):
    record = tmp_path / "rec"
    tail = _ok() if ending == "ok" else "cat >/dev/null\nexit 7\n"
    status, events, _ = await _call(relay, monkeypatch, tmp_path, _record_script(record) + tail,
                                    {"model": MODEL, "messages_text": "q", "session_id": "s", "allow_tools": True})
    assert status == 200
    workdir = Path(Path(str(record) + ".cwd").read_text().strip())
    assert workdir.name.startswith("agy-relay-")
    assert "./.agents/plugins/aads-tools/mcp_config.json" in Path(str(record) + ".files").read_text()
    assert not workdir.exists()
    assert events[-1]["type"] == ("result" if ending == "ok" else "error")
