"""Runner auth and model fallback guards without provider credentials."""
import json
import re
import subprocess
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from scripts import runner_auth_policy as policy

ROOT = Path(__file__).resolve().parents[2]


def test_jinah_requires_fresh_verified_receipt_and_quarantine(tmp_path, monkeypatch):
    monkeypatch.setattr(policy, "ACCOUNTS_ROOT", tmp_path)
    monkeypatch.setattr(policy, "HEALTH_FILE", tmp_path / "health.json")
    for name in ("CODEX_OAUTH_JINAH", "CODEX_OAUTH_MAIN"):
        home = tmp_path / name
        home.mkdir()
        (home / "auth.json").write_text("{}")
    (tmp_path / "state.json").write_text(json.dumps({"accounts": [
        {"key_name": "CODEX_OAUTH_JINAH", "is_active": True, "has_auth": True, "priority": 1},
        {"key_name": "CODEX_OAUTH_MAIN", "is_active": True, "has_auth": True, "priority": 2},
    ]}))
    assert policy.select().name == "CODEX_OAUTH_MAIN"
    auth = tmp_path / "CODEX_OAUTH_JINAH" / "auth.json"
    policy._write_health({"CODEX_OAUTH_JINAH": {
        "verified_until": time.time() + 3600, "auth_mtime_ns": auth.stat().st_mtime_ns,
    }})
    assert policy.select().name == "CODEX_OAUTH_JINAH"
    policy.quarantine("CODEX_OAUTH_JINAH")
    assert policy.select().name == "CODEX_OAUTH_MAIN"
    policy.quarantine("CODEX_OAUTH_MAIN")
    assert policy.select() is None
    assert "token" not in policy.HEALTH_FILE.read_text().lower()


def test_probe_failure_does_not_admit_jinah(tmp_path, monkeypatch):
    monkeypatch.setattr(policy, "ACCOUNTS_ROOT", tmp_path)
    monkeypatch.setattr(policy, "HEALTH_FILE", tmp_path / "health.json")
    home = tmp_path / "CODEX_OAUTH_JINAH"
    home.mkdir()
    (home / "auth.json").write_text("{}")
    monkeypatch.setattr(policy.subprocess, "run", lambda *a, **kw: subprocess.CompletedProcess(a, 1, "", "401"))
    assert policy.probe_jinah() is False
    assert policy._health()[policy.JINAH]["quarantined_until"] > time.time()


def _seed_verified_jinah_accounts(tmp_path, monkeypatch):
    monkeypatch.setattr(policy, "ACCOUNTS_ROOT", tmp_path)
    monkeypatch.setattr(policy, "HEALTH_FILE", tmp_path / "health.json")
    home = tmp_path / policy.JINAH
    home.mkdir()
    auth = home / "auth.json"
    auth.write_text("{}")
    main_home = tmp_path / "CODEX_OAUTH_MAIN"
    main_home.mkdir()
    (main_home / "auth.json").write_text("{}")
    (tmp_path / "state.json").write_text(json.dumps({"accounts": [
        {"key_name": policy.JINAH, "is_active": True, "has_auth": True, "priority": 1},
        {"key_name": "CODEX_OAUTH_MAIN", "is_active": True, "has_auth": True, "priority": 2},
    ]}))
    policy._write_health({policy.JINAH: {"verified_until": time.time() + 3600,
                                         "auth_mtime_ns": auth.stat().st_mtime_ns}})
    return auth


def test_failed_probe_revokes_existing_receipt(tmp_path, monkeypatch):
    _seed_verified_jinah_accounts(tmp_path, monkeypatch)
    monkeypatch.setattr(policy.subprocess, "run", lambda *a, **kw: subprocess.CompletedProcess(a, 1, "", "401"))
    assert policy.probe_jinah() is False
    assert policy._health()[policy.JINAH]["quarantined_until"] > time.time()
    assert policy.select().name == "CODEX_OAUTH_MAIN"


def test_probe_exception_revokes_existing_receipt(tmp_path, monkeypatch):
    auth = _seed_verified_jinah_accounts(tmp_path, monkeypatch)
    for error in (subprocess.TimeoutExpired("codex", 45), OSError("cli unavailable")):
        policy._write_health({policy.JINAH: {"verified_until": time.time() + 3600,
                                             "auth_mtime_ns": auth.stat().st_mtime_ns}})
        def fail(*args, **kwargs):
            raise error
        monkeypatch.setattr(policy.subprocess, "run", fail)
        assert policy.probe_jinah() is False
        assert policy._health()[policy.JINAH]["quarantined_until"] > time.time()
        assert all(policy.select().name == "CODEX_OAUTH_MAIN" for _ in range(3))


def test_concurrent_quarantines_preserve_both_accounts(tmp_path, monkeypatch):
    monkeypatch.setattr(policy, "ACCOUNTS_ROOT", tmp_path)
    monkeypatch.setattr(policy, "HEALTH_FILE", tmp_path / "health.json")
    names = ["CODEX_OAUTH_MAIN", policy.JINAH]
    (tmp_path / "state.json").write_text(json.dumps({"accounts": [
        {"key_name": name} for name in names]}))
    with ThreadPoolExecutor(max_workers=2) as pool:
        list(pool.map(policy.quarantine, names))
    assert set(policy._health()) == set(names)


def test_nan_rate_limit_fails_closed(tmp_path, monkeypatch):
    monkeypatch.setattr(policy, "ACCOUNTS_ROOT", tmp_path)
    (tmp_path / "CODEX_OAUTH_MAIN").mkdir()
    (tmp_path / "CODEX_OAUTH_MAIN" / "auth.json").write_text("{}")
    account = {"key_name": "CODEX_OAUTH_MAIN", "is_active": True,
               "has_auth": True, "rate_limited_until_epoch": float("nan")}
    assert not policy.eligible(account, {}, time.time())


def test_shell_model_cycle_excludes_unverified_opus_and_keeps_two_alternatives():
    script = (ROOT / "scripts/pipeline-runner.sh").read_text()
    def shell_function(name):
        match = re.search(rf"^{name}\(\) \{{.*?^\}}", script, re.MULTILINE | re.DOTALL)
        assert match, name
        return match.group()
    functions = "\n".join(shell_function(name) for name in (
        "normalize_runner_model", "append_model_for_attempts", "dedupe_model_cycle_for_attempt_caps"))
    command = ("CLAUDE_MODEL_CONTRACT=\"$1\"\n" + functions + "\n"
               "MODEL_CYCLE=()\nappend_model_for_attempts claude-opus-5-5\n"
               "append_model_for_attempts codex:gpt-5.6-luna\n"
               "append_model_for_attempts codex:gpt-5.6-sol\n"
               "printf '%s\\n' \"${MODEL_CYCLE[@]}\"")
    result = subprocess.run(["bash", "-c", command, "test", str(ROOT / "scripts/claude_model_contract.py")],
                            capture_output=True, text=True, check=True)
    assert result.stdout.splitlines() == ["codex:gpt-5.6-luna", "codex:gpt-5.6-sol"]


def test_two_claude_slot_attempts_still_add_codex_alternatives():
    script = (ROOT / "scripts/pipeline-runner.sh").read_text()
    segment = script.split('    # Two Claude slots are attempts at one model, not alternative model kinds.', 1)[1]
    segment = segment.split('    log "  MODEL_CYCLE_CAPPED', 1)[0]
    command = ("set -e\nMODEL_CYCLE=(claude-sonnet-5 claude-sonnet-5)\n"
               "append_model_for_attempts() { MODEL_CYCLE+=(\"$1\"); }\n"
               "check_cycle() {\n" + segment + "\n}\ncheck_cycle\n" +
               "\nprintf '%s\\n' \"${MODEL_CYCLE[@]}\"")
    result = subprocess.run(["bash", "-c", command], capture_output=True, text=True, check=True)
    assert result.stdout.splitlines()[-2:] == ["codex:gpt-5.6-luna", "codex:gpt-5.6-sol"]


def test_review_excludes_unverified_opus_and_preserves_fallback_order():
    from app.services.code_reviewer import _review_attempt_models

    assert _review_attempt_models([
        "claude-opus-5-5", "codex:gpt-5.6-luna", "claude-opus",
        "codex:gpt-5.6-sol", "codex:gpt-5.6-luna",
    ], "") == ["codex:gpt-5.6-luna", "codex:gpt-5.6-sol"]


def test_runner_classifies_401_before_generic_errors(tmp_path):
    script = (ROOT / "scripts/pipeline-runner.sh").read_text()
    functions = script[script.index("classify_error() {"):script.index("codex_auth_disabled_until() {")]
    err = tmp_path / "stderr"
    out = tmp_path / "stdout"
    err.write_text("401 Unauthorized")
    out.write_text("")
    result = subprocess.run(["bash", "-c", functions + "\nclassify_error 1 \"$1\" \"$2\"",
                             "test", str(err), str(out)], capture_output=True, text=True, check=True)
    assert result.stdout.strip() == "auth_error"


def test_malformed_health_and_missing_state_fail_closed_per_account(tmp_path, monkeypatch):
    monkeypatch.setattr(policy, "ACCOUNTS_ROOT", tmp_path)
    monkeypatch.setattr(policy, "HEALTH_FILE", tmp_path / "health.json")
    assert policy.select() is None
    for name in (policy.JINAH, "CODEX_OAUTH_OTHER"):
        home = tmp_path / name
        home.mkdir()
        (home / "auth.json").write_text("{}")
    (tmp_path / "state.json").write_text(json.dumps({"accounts": [
        {"key_name": policy.JINAH, "is_active": True, "has_auth": True, "priority": "bad"},
        {"key_name": "CODEX_OAUTH_OTHER", "is_active": True, "has_auth": True, "priority": 2},
    ]}))
    policy._write_health({policy.JINAH: {"verified_until": "bad"},
                          "CODEX_OAUTH_OTHER": {"quarantined_until": "bad"}})
    assert policy.select() is None
    policy._write_health({policy.JINAH: {"verified_until": "nan"},
                          "CODEX_OAUTH_OTHER": {"quarantined_until": "nan"}})
    assert policy.select() is None
    policy.quarantine("CODEX_OAUTH_OTHER")
    assert policy.select() is None


def test_auth_error_masking_and_bounded_401():
    line = policy.redact_error_line("ERROR: FileNotFoundError Bearer secret sk-test123 refresh_token=abc")
    assert "FileNotFoundError" in line
    assert "secret" not in line and "sk-test123" not in line and "abc" not in line
    script = (ROOT / "scripts/pipeline-runner.sh").read_text()
    assert "'(^|[^[:alnum:]])401([^[:alnum:]]|$)" in script
    assert 'grep -qiE "401|unauthorized' not in script


def test_runner_model_availability_uses_canonical_alias():
    from scripts.claude_model_contract import runner_model_available

    assert not runner_model_available("claude-opus")
    assert not runner_model_available("claude:claude-opus-5-5")
    assert runner_model_available("claude-opus-5")
    assert runner_model_available("codex:gpt-5.6-luna")
