"""A large Codex prompt must not become an OS argv or a broken HTTP stream."""

import asyncio
import errno
import json
from unittest.mock import AsyncMock

import pytest

from scripts import claude_relay_server as relay


class FakeLease:
    def __init__(self, *args):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False

    def attach_proc(self, *args):
        pass


class FakeResponse:
    def __init__(self, *args, **kwargs):
        self.prepared = True


class FakeStdin:
    def __init__(self):
        self.writes = []
        self.closed = False

    def write(self, value):
        self.writes.append(value)

    async def drain(self):
        pass

    def close(self):
        self.closed = True


class FakeProcess:
    def __init__(self):
        self.pid = 123
        self.returncode = 0
        self.stdin = FakeStdin()
        self.stdout = object()
        self.stderr = None


def setup_relay(monkeypatch):
    monkeypatch.setattr(relay, "_SemaphoreLease", FakeLease)
    monkeypatch.setattr(relay, "_resolve_cli_command", lambda *_: {"argv": ["codex"], "mode": "test"})
    monkeypatch.setattr(relay, "_preflight_cli_command", lambda *_: {"ok": True})
    monkeypatch.setattr(relay, "_load_mcp_template", lambda *_: {"mcpServers": {}})
    monkeypatch.setattr(
        relay, "_resolve_aads_tools_cfg",
        AsyncMock(return_value=({"command": "true"}, {"path_mode": "test"}, [])),
    )
    monkeypatch.setattr(relay, "_build_codex_home", lambda *args, **kwargs: "/tmp")
    monkeypatch.setattr(relay, "_resolve_codex_cwd", lambda *_: "/tmp")
    monkeypatch.setattr(relay, "_materialize_codex_image_attachments", lambda *args: [])
    monkeypatch.setattr(relay, "_OS_TIMEOUT_ENABLED", False)
    monkeypatch.setattr(relay.web, "StreamResponse", FakeResponse)
    monkeypatch.setattr(relay, "_stream_prepare", AsyncMock())
    writer = AsyncMock()
    eof = AsyncMock()
    monkeypatch.setattr(relay, "_stream_write", writer)
    monkeypatch.setattr(relay, "_stream_write_eof", eof)
    return writer, eof


@pytest.mark.asyncio
async def test_large_prompt_is_sent_via_stdin_not_argv(monkeypatch):
    setup_relay(monkeypatch)
    process = FakeProcess()
    spawn = AsyncMock(return_value=process)
    monkeypatch.setattr(relay.asyncio, "create_subprocess_exec", spawn)

    async def lines(*args, **kwargs):
        yield json.dumps({"type": "turn.completed", "usage": {}}).encode()

    monkeypatch.setattr(relay, "_iter_ndjson_lines", lines)
    prompt = "가" * 50000
    request = AsyncMock()
    request.json.return_value = {"model": "gpt-6-astra", "messages_text": prompt, "session_id": "test-session"}

    response = await relay.handle_codex_stream(request)

    assert isinstance(response, FakeResponse)
    assert spawn.call_args.args[-1] == "-"
    assert prompt not in spawn.call_args.args
    assert prompt.encode() in process.stdin.writes[0]
    assert process.stdin.closed


@pytest.mark.asyncio
async def test_spawn_error_after_stream_prepare_returns_ndjson_not_second_http_response(monkeypatch):
    writer, eof = setup_relay(monkeypatch)
    monkeypatch.setattr(
        relay.asyncio, "create_subprocess_exec",
        AsyncMock(side_effect=OSError(errno.E2BIG, "Argument list too long")),
    )
    request = AsyncMock()
    request.json.return_value = {"messages_text": "test", "session_id": "test-session"}

    response = await relay.handle_codex_stream(request)

    assert isinstance(response, FakeResponse)
    event = json.loads(writer.call_args.args[1])
    assert event["type"] == "error"
    assert event["error_type"] == "codex_relay_internal_error"
    eof.assert_awaited_once()
