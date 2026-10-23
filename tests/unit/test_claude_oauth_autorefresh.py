import asyncio
import json
import os
import subprocess
import time
from pathlib import Path

import pytest

from app.core.claude_oauth_credentials import (
    classify_auth_error,
    is_slot_busy_text,
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


@pytest.mark.parametrize("expires_in", [-10, 300])
def test_docker_credential_lock_contention_fails_without_unlocked_execution(tmp_path, expires_in):
    import fcntl

    credential = tmp_path / ".credentials.json"
    _write_credential(credential, expires_at=time.time() + expires_in)
    lock = tmp_path / "credentials.lock"
    wrapper = Path(__file__).resolve().parents[2] / "scripts" / "claude-docker-wrapper.sh"
    env = dict(os.environ, CLAUDE_SLOT_CREDENTIALS_FILE=str(credential),
               CLAUDE_SLOT_CREDENTIAL_LOCK_FILE=str(lock), CLAUDE_OAUTH_SLOT="2",
               CLAUDE_SLOT_REFRESH_LOCK_WAIT_SEC="0.05")
    with lock.open("w") as held:
        fcntl.flock(held, fcntl.LOCK_EX | fcntl.LOCK_NB)
        result = subprocess.run(["bash", str(wrapper), "--version"], env=env,
                                capture_output=True, text=True, timeout=5)
    assert result.returncode == 75
    assert "oauth_slot_busy" in result.stderr
    assert "unit-access-value" not in result.stderr


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
    _write_credential(credential, expires_at=time.time() + 120)
    fake_cli = tmp_path / "fake-claude"
    fake_cli.write_text(
        """#!/usr/bin/env python3
import json
import os
import sys
import time
from pathlib import Path

path = Path(os.environ["HOME"]) / ".claude" / ".credentials.json"
payload = json.loads(path.read_text())
if sys.argv[1:3] == ["-p", "ping"]:
    time.sleep(0.3)
    payload["generation"] = int(payload.get("generation", 0)) + 1
    payload["claudeAiOauth"]["expiresAt"] = int((time.time() + 28800) * 1000)
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
        "CLAUDE_SLOT_REFRESH_BIN": str(fake_cli),
    })

    processes = [
        subprocess.Popen(
            [str(wrapper), str(fake_cli), "call"],
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        for _ in range(2)
    ]
    completed = [process.communicate(timeout=10) for process in processes]

    assert [process.returncode for process in processes] == [0, 0], completed
    assert json.loads(credential.read_text())["generation"] == 1  # one refresher, not two
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

    assert "claude_slot_credential.py" in wrapper
    assert "prepare --mode docker" in wrapper
    assert "CLAUDE_SLOT_CREDENTIAL_MODE=1" in wrapper
    assert "docker cp" in wrapper
    assert " watch --mode docker" in wrapper  # single refresh owner pushes accessToken only
    assert " reap --call-id" in wrapper and "--container" in wrapper  # in-container cleanup
    assert "AADS_CLAUDE_CALL_ID" in wrapper
    assert " finalize " not in wrapper  # the model CLI no longer writes credentials back
    assert "flock" not in wrapper  # the model call must never hold a credential lock
    active_wrapper = (
        Path(__file__).resolve().parents[2]
        / "scripts"
        / "claude-docker-wrapper-active.sh"
    ).read_text(encoding="utf-8")
    assert 'exec "${SCRIPT_DIR}/claude-docker-wrapper.sh" "$@"' in active_wrapper


def test_docker_wrapper_owner_refresh_result_is_committed_before_the_call(tmp_path):
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

if command[0] != "timeout":
    raise SystemExit(0)  # model call: the refresh already happened before it
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

    async def slot_records(**kwargs):
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


def _busy_call_stream_env(monkeypatch, relay):
    from app.services import model_selector

    async def no_db_key(*args, **kwargs):
        return ""

    async def available_models():
        return {"claude-fable-5-1", "claude-opus", "claude-sonnet", "gpt-5.6-sol"}

    async def no_registry_row(*args, **kwargs):
        return None

    async def slot_records(**kwargs):
        return {
            "1": {"priority": 1, "key_name": "slot-one"},
            "2": {"priority": 2, "key_name": "slot-two"},
        }

    monkeypatch.setattr(model_selector, "_get_db_key", no_db_key)
    monkeypatch.setattr(model_selector, "get_available_model_ids", available_models)
    monkeypatch.setattr(model_selector, "_get_registered_model_row", no_registry_row)
    monkeypatch.setattr(model_selector, "_get_claude_slot_records", slot_records)
    monkeypatch.setattr(model_selector, "_stream_cli_relay", relay)
    return model_selector


@pytest.mark.asyncio
async def test_busy_slot_moves_to_next_account_without_isolation_or_cooldown(monkeypatch):
    from app.services.intent_router import IntentResult

    calls, cooled, sdk_calls = [], [], []

    async def relay(model, *args, oauth_slot=None, **kwargs):
        calls.append(oauth_slot)
        if oauth_slot == "1":
            yield {"type": "error", "content": "oauth_slot_busy: slot1 credential renewal in progress (waited 30s)",
                   "error_type": "oauth_slot_busy"}
            return
        yield {"type": "delta", "content": "ok"}
        yield {"type": "done", "model": model, "input_tokens": 1, "output_tokens": 1}

    async def stale_sdk(*args, **kwargs):
        sdk_calls.append(args)
        yield {"type": "error", "content": "stale SDK token used"}

    model_selector = _busy_call_stream_env(monkeypatch, relay)
    monkeypatch.setattr(model_selector, "_stream_agent_sdk", stale_sdk)
    monkeypatch.setattr(model_selector, "_mark_slot_cooldown", lambda *a, **k: cooled.append(a))

    events = [event async for event in model_selector.call_stream(
        IntentResult(intent="cto_strategy", model="claude-fable-5-1", use_tools=True, tool_group="all"),
        "system", [{"role": "user", "content": "ping"}],
    )]

    assert calls == ["1", "2"]
    assert events[-1]["type"] == "done"
    assert not cooled and not sdk_calls
    assert model_selector._is_slot_available("1")


@pytest.mark.asyncio
async def test_busy_only_exhaustion_is_reported_as_renewal_wait_not_outage(monkeypatch):
    from app.services.intent_router import IntentResult

    alerts, cooled = [], []

    async def relay(model, *args, oauth_slot=None, **kwargs):
        yield {"type": "error", "content": "oauth_slot_busy: slot%s credential renewal in progress (waited 30s)" % oauth_slot,
               "error_type": "oauth_slot_busy"}

    async def alert(*args, **kwargs):
        alerts.append(kwargs)

    model_selector = _busy_call_stream_env(monkeypatch, relay)
    monkeypatch.setattr(model_selector, "_alert_no_slots_left", alert)
    monkeypatch.setattr(model_selector, "_mark_slot_cooldown", lambda *a, **k: cooled.append(a))

    events = [event async for event in model_selector.call_stream(
        IntentResult(intent="cto_strategy", model="claude-fable-5-1", use_tools=True, tool_group="all"),
        "system", [{"role": "user", "content": "explicit"}], model_override="claude-fable-5-1",
    )]

    text = "".join(e.get("content", "") for e in events if e["type"] == "delta")
    assert "인증 갱신 대기" in text and "잠시 후 다시 시도" in text
    assert "전체 LLM 장애" not in text
    assert events[-1]["stream_status"] == "interrupted"
    assert alerts and alerts[0]["busy_only"] is True and set(alerts[0]["busy_slots"]) == {"1", "2"}
    assert not cooled


@pytest.mark.asyncio
async def test_real_auth_and_quota_failures_keep_their_existing_handling(monkeypatch):
    from app.services import model_selector as models

    assert models._is_cli_auth_error("OAuth token has expired. Please log in again")
    assert models._is_cli_auth_error("401 unauthorized: refresh token reused")
    assert models._is_cli_auth_error("oauth_slot_unavailable")
    assert not models._is_oauth_slot_busy_error("401 unauthorized: refresh token reused")
    assert not models._is_oauth_slot_busy_error("You've hit your limit · resets 3pm")
    busy = "oauth_slot_busy: slot2 credential renewal in progress (waited 30s)"
    lowered = busy.lower()
    assert not any(k in lowered for k in ("429", "rate", "limit", "overloaded", "quota"))
    assert models._classify_claude_auth_error(busy) == "error"
    assert is_slot_busy_text(busy)
    assert not is_slot_busy_text("401 unauthorized")
    assert classify_auth_error("oauth_slot_busy: waited, token expired 401") == "error"
    assert classify_auth_error("oauth_slot_unavailable: slot2 credential renewal did not complete") == "error"


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


@pytest.mark.asyncio
@pytest.mark.parametrize('slots,explicit,quota', [
    (('1',), False, {}), (('1','2'), False, {}),
    (('1',), True, {}), (('1','2'), True, {}),
    (('1','2'), False, {'2': 'weekly limit'}),
])
async def test_busy_terminal_never_enters_provider_fallback(monkeypatch, slots, explicit, quota):
    from app.services.intent_router import IntentResult
    from app.services import oauth_usage_tracker, error_book
    from app.core import db_pool

    calls, alerts = [], []

    async def relay(model, *args, oauth_slot=None, **kwargs):
        calls.append(oauth_slot)
        yield {'type': 'error', 'content': 'oauth_slot_busy: credential renewal in progress',
               'error_type': 'oauth_slot_busy'}

    models = _busy_call_stream_env(monkeypatch, relay)

    async def records(**kwargs):
        return {s: {'priority': int(s), 'key_name': 'unit-slot-'+s} for s in slots}

    async def exhausted():
        return quota

    async def alert(*args, **kwargs):
        alerts.append(kwargs)

    def forbidden(*args, **kwargs):
        pytest.fail('busy-only must not call SDK/Codex/LiteLLM, DB error log, isolation or account mutation')

    async def forbidden_async(*args, **kwargs):
        forbidden()

    async def no_policy(**kwargs):
        return None, "unit policy disabled"

    async def no_shadow(**kwargs):
        return None

    from app.core import feature_flags
    from app.services import slot_projects

    async def slot_order(slots, project):
        return slots

    monkeypatch.setattr(slot_projects, 'order_slots_for_project', slot_order)

    async def flag_default(key, default=True):
        return False

    monkeypatch.setattr(feature_flags, 'get_flag', flag_default)
    monkeypatch.setattr(models, '_resolve_governed_intent_model', no_policy)
    monkeypatch.setattr(models, '_record_ceiling_shadow', no_shadow)
    monkeypatch.setattr(models, '_get_claude_slot_records', records)
    monkeypatch.setattr(oauth_usage_tracker, 'get_exhausted_slots', exhausted)
    monkeypatch.setattr(models, '_alert_no_slots_left', alert)
    for name in ('_stream_agent_sdk', '_stream_codex_relay', '_stream_litellm', '_mark_slot_cooldown'):
        monkeypatch.setattr(models, name, forbidden)
    monkeypatch.setattr(models, '_mark_key_rate_limited', forbidden_async)
    async def clear_session(*args, **kwargs):
        pass

    monkeypatch.setattr(models, '_relay_clear_aads_session_for_oauth_fallback', clear_session)
    monkeypatch.setattr(db_pool, 'get_pool', forbidden)
    monkeypatch.setattr(error_book, 'ingest_error_event', forbidden_async)

    async def collect():
        return [e async for e in models.call_stream(
            IntentResult(intent='cto_strategy', model='claude-fable-5-1', use_tools=True, tool_group='all'),
            'system', [{'role': 'user', 'content': 'fake only'}],
            model_override='claude-fable-5-1' if explicit else None,
        )]

    events = await asyncio.wait_for(collect(), timeout=5)
    assert sorted(calls) == sorted(set(slots) - set(quota))
    text = ''.join(e.get('content', '') for e in events)
    for phrase in ('Claude 일시 장애', '전체 LLM 장애', 'All LLM providers failed', 'provider_fallback_exhausted'):
        assert phrase not in str(events)
    assert '인증 갱신 대기' in text
    if quota:
        assert '한도 소진이나 인증 만료가 아닙니다' not in text
    assert [e['type'] for e in events[-2:]] == ['interrupted', 'error']
    for event in events[-2:]:
        assert event['terminal'] is True
        assert event['reason'] == event['error_type'] == event['content'] == 'oauth_slot_busy'
    assert events[-1]['stream_status'] == 'interrupted'
    assert alerts[0]['busy_only'] is True


@pytest.mark.asyncio
@pytest.mark.parametrize('hard_error', ['401 OAuth access token has been revoked', '429 quota exceeded', 'provider unavailable'])
async def test_busy_mixed_with_hard_failure_keeps_provider_fallback(monkeypatch, hard_error):
    from app.services.intent_router import IntentResult
    from app.services import oauth_usage_tracker
    from app.core import db_pool

    calls, cooled, limited, alerts = [], [], [], []

    async def relay(model, *args, oauth_slot=None, **kwargs):
        calls.append(('relay', oauth_slot))
        yield {'type': 'error', 'content': 'oauth_slot_busy: renewal in progress' if oauth_slot == '1' else hard_error}

    models = _busy_call_stream_env(monkeypatch, relay)

    async def exhausted():
        return {}

    async def alert(*args, **kwargs):
        alerts.append(kwargs)

    async def clear(*args, **kwargs):
        pass

    async def rate(*args, **kwargs):
        limited.append(args)

    async def codex(*args, **kwargs):
        calls.append(('codex', args[0]))
        yield {'type': 'delta', 'content': 'fake fallback ok'}
        yield {'type': 'done', 'model': args[0]}

    def no_db():
        raise RuntimeError('unit DB disabled')

    def forbidden(*args, **kwargs):
        pytest.fail('busy primary slot must not enter SDK or paid fallback')

    monkeypatch.setattr(oauth_usage_tracker, 'get_exhausted_slots', exhausted)
    monkeypatch.setattr(models, '_alert_no_slots_left', alert)
    monkeypatch.setattr(models, '_relay_clear_aads_session_for_oauth_fallback', clear)
    monkeypatch.setattr(models, '_mark_slot_cooldown', lambda *a, **k: cooled.append(a))
    monkeypatch.setattr(models, '_mark_key_rate_limited', rate)
    monkeypatch.setattr(models, '_stream_codex_relay', codex)
    monkeypatch.setattr(models, '_stream_agent_sdk', forbidden)
    monkeypatch.setattr(models, '_stream_litellm', forbidden)
    monkeypatch.setattr(db_pool, 'get_pool', no_db)

    async def collect():
        return [e async for e in models.call_stream(
            IntentResult(intent='cto_strategy', model='claude-fable-5-1', use_tools=True, tool_group='all'),
            'system', [{'role': 'user', 'content': 'fake mixed only'}],
        )]

    events = await asyncio.wait_for(collect(), timeout=5)
    assert events[-1]['type'] == 'done'
    assert calls.count(('relay', '1')) == 1
    assert ('codex', 'gpt-5.6-sol') in calls
    assert alerts[0]['busy_only'] is False
    assert all(args[0] == '2' for args in cooled)
    if hard_error.startswith('429'):
        assert cooled and limited
    else:
        assert not cooled and not limited
