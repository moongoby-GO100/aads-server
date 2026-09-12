import asyncio
import json
import os
import subprocess
import time
from pathlib import Path

import pytest

from app.core.claude_oauth_credentials import (
    format_auth001_slot,
    read_slot_auth_status,
    record_slot_validation,
    redact_secret_text,
)
from scripts import claude_relay_server as relay


def _write_credential(path: Path, *, expires_at: float, generation: int = 0) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps({
            "claudeAiOauth": {
                "accessToken": "unit-access-value",
                "refreshToken": "unit-refresh-value",
                "expiresAt": int(expires_at * 1000),
            },
            "generation": generation,
        }),
        encoding="utf-8",
    )
    path.chmod(0o600)


def test_slot_credentials_precede_stale_env_token(monkeypatch, tmp_path):
    monkeypatch.setattr(relay, "_SLOT_HOME_ROOT", tmp_path)
    credential = tmp_path / "slot2" / ".claude" / ".credentials.json"
    _write_credential(credential, expires_at=time.time() + 3600)
    monkeypatch.setattr(
        relay,
        "_read_oauth_tokens",
        lambda: ("env-one", "stale-env-two", "2", "one", "two"),
    )

    selected = relay._pick_auth(preferred_slot="2")

    assert selected["source"] == "slot_credentials"
    assert selected["slot"] == "2"
    assert selected["token"] == ""


def test_env_token_is_only_used_when_slot_file_is_absent(monkeypatch, tmp_path):
    monkeypatch.setattr(relay, "_SLOT_HOME_ROOT", tmp_path)
    monkeypatch.setattr(
        relay,
        "_read_oauth_tokens",
        lambda: ("", "env-fallback-two", "2", "one", "two"),
    )

    selected = relay._pick_auth(preferred_slot="2")

    assert selected["source"] == "env_fallback"
    assert selected["token"] == "env-fallback-two"

    incomplete = tmp_path / "slot2" / ".claude" / ".credentials.json"
    incomplete.parent.mkdir(parents=True)
    incomplete.write_text(json.dumps({"claudeAiOauth": {"accessToken": "incomplete"}}))
    selected = relay._pick_auth(preferred_slot="2")
    assert selected["source"] == "none"
    assert selected["token"] == ""


def test_explicit_slot_never_silently_executes_another_account(monkeypatch, tmp_path):
    monkeypatch.setattr(relay, "_SLOT_HOME_ROOT", tmp_path)
    slot_one = tmp_path / "slot1" / ".claude" / ".credentials.json"
    _write_credential(slot_one, expires_at=time.time() + 3600)
    monkeypatch.setattr(
        relay,
        "_read_oauth_tokens",
        lambda: ("", "", "1", "one", "two"),
    )

    selected = relay._pick_auth(preferred_slot="2")

    assert selected["source"] == "none"
    assert selected["slot"] == "0"


def test_recent_revoked_slot_is_skipped_until_explicit_recovery(monkeypatch, tmp_path):
    monkeypatch.setattr(relay, "_SLOT_HOME_ROOT", tmp_path)
    for slot in ("1", "2"):
        credential = tmp_path / f"slot{slot}" / ".claude" / ".credentials.json"
        _write_credential(credential, expires_at=time.time() + 28_800)
    record_slot_validation(
        tmp_path / "slot1" / ".claude" / ".credentials.json",
        ok=False,
        error="OAuth access token has been revoked",
    )
    monkeypatch.setattr(
        relay,
        "_read_oauth_tokens",
        lambda: ("", "", "1", "one", "two"),
    )

    selected = relay._pick_auth()
    recovery = relay._pick_auth(preferred_slot="1", allow_auth_recovery=True)

    assert selected["slot"] == "2"
    assert selected["source"] == "slot_credentials"
    assert recovery["slot"] == "1"
    assert recovery["source"] == "slot_credentials"


def test_docker_and_host_modes_receive_refreshable_slot_home(monkeypatch, tmp_path):
    monkeypatch.setattr(relay, "_DIRECT_OAUTH_ENABLED", True)
    monkeypatch.setattr(relay, "_SLOT_HOME_ROOT", tmp_path)
    credential = tmp_path / "slot1" / ".claude" / ".credentials.json"
    _write_credential(credential, expires_at=time.time() + 3600)
    slot_wrapper = tmp_path / "slot-wrapper"
    slot_wrapper.write_text("#!/bin/sh\nexec \"$@\"\n")
    slot_wrapper.chmod(0o755)
    monkeypatch.setattr(relay, "_SLOT_CREDENTIAL_WRAPPER", slot_wrapper)

    docker_env = relay._build_claude_env("stale-env", "1", "docker_wrapper")
    host_env = relay._build_claude_env("stale-env", "1", "explicit_path")
    host_argv = relay._claude_argv_for_slot(
        {"mode": "explicit_path", "argv": ["/opt/claude"]}, "1"
    )

    for cli_env in (docker_env, host_env):
        assert "CLAUDE_CODE_OAUTH_TOKEN" not in cli_env
        assert cli_env["CLAUDE_SLOT_CREDENTIALS_FILE"] == str(credential)
        assert cli_env["CLAUDE_OAUTH_SLOT"] == "1"
    assert host_argv == [str(slot_wrapper), "/opt/claude"]


def test_host_refreshes_are_serialized_and_atomically_recovered(tmp_path):
    credential = tmp_path / "slot2" / ".claude" / ".credentials.json"
    _write_credential(credential, expires_at=time.time() + 3600)
    fake_cli = tmp_path / "fake-claude"
    fake_cli.write_text(
        """#!/usr/bin/env python3
import json
import os
import time
from pathlib import Path

path = Path(os.environ["HOME"]) / ".claude" / ".credentials.json"
payload = json.loads(path.read_text())
generation = int(payload.get("generation", 0))
time.sleep(0.15)
payload["generation"] = generation + 1
path.write_text(json.dumps(payload))
""",
        encoding="utf-8",
    )
    fake_cli.chmod(0o755)
    wrapper = Path(__file__).resolve().parents[2] / "scripts" / "claude-slot-credentials-wrapper.sh"
    env = dict(os.environ)
    env.update({
        "CLAUDE_SLOT_CREDENTIALS_FILE": str(credential),
        "CLAUDE_SLOT_CREDENTIAL_LOCK_FILE": str(credential.parent / ".credentials.lock"),
        "CLAUDE_OAUTH_SLOT": "2",
    })

    processes = [
        subprocess.Popen(
            [str(wrapper), str(fake_cli)],
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        for _ in range(2)
    ]
    completed = [process.communicate(timeout=10) for process in processes]

    assert [process.returncode for process in processes] == [0, 0], completed
    assert json.loads(credential.read_text())["generation"] == 2
    assert not list(credential.parent.glob("*.tmp"))


def test_far_future_credentials_keep_same_slot_calls_concurrent(tmp_path):
    credential = tmp_path / "slot1" / ".claude" / ".credentials.json"
    _write_credential(credential, expires_at=time.time() + 28_800)
    concurrency_dir = tmp_path / "concurrency"
    concurrency_dir.mkdir()
    fake_cli = tmp_path / "fake-concurrent-claude"
    fake_cli.write_text(
        """#!/usr/bin/env python3
import os
import time
from pathlib import Path

root = Path(os.environ["CONCURRENCY_DIR"])
(root / str(os.getpid())).write_text("started")
deadline = time.time() + 2
while len(list(root.iterdir())) < 2 and time.time() < deadline:
    time.sleep(0.02)
raise SystemExit(0 if len(list(root.iterdir())) >= 2 else 9)
""",
        encoding="utf-8",
    )
    fake_cli.chmod(0o755)
    wrapper = Path(__file__).resolve().parents[2] / "scripts" / "claude-slot-credentials-wrapper.sh"
    env = dict(os.environ)
    env.update({
        "CONCURRENCY_DIR": str(concurrency_dir),
        "CLAUDE_SLOT_CREDENTIALS_FILE": str(credential),
        "CLAUDE_SLOT_CREDENTIAL_LOCK_FILE": str(credential.parent / ".credentials.lock"),
        "CLAUDE_OAUTH_SLOT": "1",
    })

    processes = [
        subprocess.Popen([str(wrapper), str(fake_cli)], env=env)
        for _ in range(2)
    ]
    returncodes = [process.wait(timeout=10) for process in processes]

    assert returncodes == [0, 0]


def test_docker_wrapper_stages_and_atomically_syncs_credentials():
    wrapper = (
        Path(__file__).resolve().parents[2] / "scripts" / "claude-docker-wrapper.sh"
    ).read_text(encoding="utf-8")

    assert "flock -x 9" in wrapper
    assert "CLAUDE_SLOT_CREDENTIAL_MODE=1" in wrapper
    assert "docker cp" in wrapper
    assert 'mv -f -- "$staged" "$CREDENTIAL_FILE"' in wrapper
    active_wrapper = (
        Path(__file__).resolve().parents[2]
        / "scripts"
        / "claude-docker-wrapper-active.sh"
    ).read_text(encoding="utf-8")
    assert 'exec "${SCRIPT_DIR}/claude-docker-wrapper.sh" "$@"' in active_wrapper


def test_docker_wrapper_recovers_cli_refresh_result(tmp_path):
    credential = tmp_path / "slot1" / ".claude" / ".credentials.json"
    _write_credential(credential, expires_at=time.time() - 60)
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    fake_docker = fake_bin / "docker"
    fake_docker.write_text(
        """#!/usr/bin/env python3
import json
import os
import re
import shutil
import sys
from pathlib import Path

root = Path(os.environ["FAKE_DOCKER_ROOT"])
args = sys.argv[1:]

def mapped(container_path):
    return root / container_path.lstrip("/")

if args[0] == "cp":
    source, destination = args[1], args[2]
    if ":" in source:
        shutil.copy2(mapped(source.split(":", 1)[1]), destination)
    else:
        shutil.copy2(source, mapped(destination.split(":", 1)[1]))
    raise SystemExit(0)

if args[0] != "exec":
    raise SystemExit(2)
index = 1
if args[index] == "-i":
    index += 1
container_env = {}
while index < len(args) and args[index] == "-e":
    key, value = args[index + 1].split("=", 1)
    container_env[key] = value
    index += 2
index += 1
command = args[index:]
if command[:2] == ["sh", "-lc"]:
    target = re.search(r"'(/tmp/[^']+)'", command[2]).group(1)
    if "mkdir -p" in command[2]:
        mapped(target).mkdir(parents=True, exist_ok=True)
    elif "rm -rf" in command[2]:
        shutil.rmtree(mapped(target), ignore_errors=True)
    raise SystemExit(0)

home = container_env["HOME"]
path = mapped(home) / ".claude" / ".credentials.json"
payload = json.loads(path.read_text())
payload["generation"] = int(payload.get("generation", 0)) + 1
payload["claudeAiOauth"]["expiresAt"] += 7_200_000
path.write_text(json.dumps(payload))
if container_env.get("CLAUDE_CODE_OAUTH_TOKEN"):
    raise SystemExit(3)
""",
        encoding="utf-8",
    )
    fake_docker.chmod(0o755)
    wrapper = Path(__file__).resolve().parents[2] / "scripts" / "claude-docker-wrapper.sh"
    env = dict(os.environ)
    env.update({
        "PATH": str(fake_bin) + os.pathsep + env["PATH"],
        "FAKE_DOCKER_ROOT": str(tmp_path / "container"),
        "CLAUDE_SLOT_CREDENTIALS_FILE": str(credential),
        "CLAUDE_SLOT_CREDENTIAL_LOCK_FILE": str(credential.parent / ".credentials.lock"),
        "CLAUDE_OAUTH_SLOT": "1",
    })

    result = subprocess.run(
        [str(wrapper), "--version"],
        env=env,
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    refreshed = json.loads(credential.read_text())
    assert refreshed["generation"] == 1
    assert refreshed["claudeAiOauth"]["expiresAt"] > int(time.time() * 1000)


@pytest.mark.asyncio
async def test_401_gets_one_refresh_retry_before_fallback(monkeypatch):
    calls = []

    async def fake_once(*args, **kwargs):
        calls.append((kwargs.get("oauth_slot"), kwargs.get("force_oauth_refresh")))
        if len(calls) == 1:
            yield {"type": "error", "content": "HTTP 401 OAuth access token has been revoked"}
        else:
            yield {"type": "done", "model": "claude-sonnet"}

    monkeypatch.setattr("app.services.model_selector._stream_cli_relay_once", fake_once)
    from app.services.model_selector import _stream_cli_relay

    events = [event async for event in _stream_cli_relay(
        "claude-sonnet", "system", [{"role": "user", "content": "ping"}],
        oauth_slot="2",
    )]

    assert calls == [("2", None), ("2", True)]
    assert [event["type"] for event in events] == ["retry_progress", "done"]
    assert events[0]["reason"] == "oauth_refresh"


@pytest.mark.asyncio
async def test_second_401_stops_same_slot_retry(monkeypatch):
    calls = []

    async def always_revoked(*args, **kwargs):
        calls.append((kwargs.get("oauth_slot"), kwargs.get("force_oauth_refresh")))
        yield {"type": "error", "content": "401 unauthorized: refresh token reused"}

    monkeypatch.setattr("app.services.model_selector._stream_cli_relay_once", always_revoked)
    from app.services.model_selector import _stream_cli_relay

    events = [event async for event in _stream_cli_relay(
        "claude-sonnet", "system", [{"role": "user", "content": "ping"}],
        oauth_slot="2",
    )]

    assert calls == [("2", None), ("2", True)]
    assert events[-1]["type"] == "error"
    assert events[-1]["auth_retry_used"] is True


@pytest.mark.asyncio
async def test_auth_failure_falls_back_account_then_gpt_without_stale_sdk(monkeypatch):
    from app.services import model_selector
    from app.services.intent_router import IntentResult

    calls = []

    async def no_db_key(*args, **kwargs):
        return ""

    async def available_models():
        return {"claude-fable-5-1", "claude-opus", "claude-sonnet", "gpt-5.6-sol"}

    async def no_registry_row(*args, **kwargs):
        return None

    async def slot_records():
        return {
            "1": {"priority": 1, "key_name": "slot-one"},
            "2": {"priority": 2, "key_name": "slot-two"},
        }

    async def revoked_cli(model, *args, oauth_slot=None, **kwargs):
        calls.append(("claude", model, oauth_slot))
        yield {"type": "error", "content": "401 OAuth access token has been revoked"}

    async def stale_sdk(*args, **kwargs):
        calls.append(("sdk", args[0], None))
        yield {"type": "error", "content": "stale SDK token used"}

    async def healthy_gpt(model, *args, **kwargs):
        calls.append(("gpt", model, None))
        yield {"type": "delta", "content": "fallback ok"}
        yield {"type": "done", "model": model, "input_tokens": 1, "output_tokens": 1}

    monkeypatch.setattr(model_selector, "_get_db_key", no_db_key)
    monkeypatch.setattr(model_selector, "get_available_model_ids", available_models)
    monkeypatch.setattr(model_selector, "_get_registered_model_row", no_registry_row)
    monkeypatch.setattr(model_selector, "_get_claude_slot_records", slot_records)
    monkeypatch.setattr(model_selector, "_stream_cli_relay", revoked_cli)
    monkeypatch.setattr(model_selector, "_stream_agent_sdk", stale_sdk)
    monkeypatch.setattr(model_selector, "_stream_codex_relay", healthy_gpt)

    events = [event async for event in model_selector.call_stream(
        IntentResult(
            intent="cto_strategy",
            model="claude-fable-5-1",
            use_tools=True,
            tool_group="all",
        ),
        "system",
        [{"role": "user", "content": "ping"}],
    )]

    assert calls == [
        ("claude", "claude-fable-5-1", "1"),
        ("claude", "claude-fable-5-1", "2"),
        ("gpt", "gpt-5.6-sol", None),
    ]
    assert events[-1]["type"] == "done"

    calls.clear()
    terminal_events = [event async for event in model_selector.call_stream(
        IntentResult(
            intent="cto_strategy",
            model="claude-fable-5-1",
            use_tools=True,
            tool_group="all",
        ),
        "system",
        [{"role": "user", "content": "explicit model"}],
        model_override="claude-fable-5-1",
    )]
    assert terminal_events[-2]["type"] == "interrupted"
    assert terminal_events[-2]["terminal"] is True
    assert terminal_events[-1]["stream_status"] == "interrupted"


def test_secret_redaction_covers_bearer_and_credential_fields():
    source = (
        "Authorization: Bearer opaque-value-with-many-characters "
        'accessToken="another-private-value" refreshToken=refresh-private-value'
    )

    redacted = redact_secret_text(source)

    assert "opaque-value" not in redacted
    assert "another-private" not in redacted
    assert "refresh-private" not in redacted
    assert redacted.count("[REDACTED]") == 3


def test_auth001_never_marks_revoked_or_expired_credential_healthy(tmp_path):
    credential = tmp_path / ".credentials.json"
    _write_credential(credential, expires_at=time.time() - 60)
    record_slot_validation(
        credential,
        ok=False,
        error="OAuth access token has been revoked",
        checked_at=time.time(),
    )

    status = read_slot_auth_status(credential, now_epoch=time.time())
    line = format_auth001_slot("2", status)

    assert status["status"] == "revoked"
    assert status["healthy"] is False
    assert "❌revoked" in line
    assert "✅" not in line


def test_auth001_requires_future_expiry_refresh_token_and_recent_success(tmp_path):
    credential = tmp_path / ".credentials.json"
    now = time.time()
    _write_credential(credential, expires_at=now + 3600)
    record_slot_validation(credential, ok=True, checked_at=now)

    status = read_slot_auth_status(credential, now_epoch=now + 10)

    assert status["status"] == "ready"
    assert status["healthy"] is True
    assert status["refresh_token_present"] is True
    assert status["validation"]["recent"] is True
