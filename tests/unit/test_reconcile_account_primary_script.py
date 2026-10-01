"""reconcile_account_primary.sh — 인증 헤더 전송과 HTTP 실패 로그."""
import os
import subprocess
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "reconcile_account_primary.sh"

FAKE_CURL = """#!/usr/bin/env bash
out=""
while [ $# -gt 0 ]; do
  case "$1" in
    -o) out="$2"; shift 2;;
    -H) echo "$2" >> "$FAKE_HEADERS"; shift 2;;
    *) shift;;
  esac
done
printf '%s' "$FAKE_BODY" > "$out"
printf '%s' "$FAKE_CODE"
"""


def run(tmp_path, code, body, env_text="AADS_MONITOR_KEY=test-key-123\n"):
    bindir = tmp_path / "bin"
    bindir.mkdir(exist_ok=True)
    curl = bindir / "curl"
    curl.write_text(FAKE_CURL)
    curl.chmod(0o755)
    (tmp_path / ".env").write_text(env_text)
    (tmp_path / ".active_port").write_text("8102")
    env = {
        **os.environ,
        "PATH": f"{bindir}:{os.environ['PATH']}",
        "AADS_SERVER_DIR": str(tmp_path),
        "AADS_RECONCILE_FAIL_STATE": str(tmp_path / "fail"),
        "AADS_MONITOR_KEY": "",
        "FAKE_CODE": code,
        "FAKE_BODY": body,
        "FAKE_HEADERS": str(tmp_path / "headers"),
    }
    p = subprocess.run(["bash", str(SCRIPT)], env=env, capture_output=True, text=True, timeout=30)
    headers = (tmp_path / "headers").read_text() if (tmp_path / "headers").exists() else ""
    return p, headers


def test_sends_monitor_key_from_env_file(tmp_path):
    _, headers = run(tmp_path, "200", '{"ok":true,"results":[]}')
    assert "X-Monitor-Key: test-key-123" in headers


def test_http_401_logs_failure_once(tmp_path):
    p, _ = run(tmp_path, "401", '{"detail":"인증이 필요합니다."}')
    assert "조정 API 실패 (port=8102, http=401)" in p.stdout
    p2, _ = run(tmp_path, "401", '{"detail":"인증이 필요합니다."}')
    assert "조정 API 실패" not in p2.stdout


def test_failure_logged_again_when_status_changes_and_cleared_on_success(tmp_path):
    run(tmp_path, "401", "{}")
    p, _ = run(tmp_path, "500", "{}")
    assert "http=500" in p.stdout
    run(tmp_path, "200", '{"ok":true,"results":[]}')
    assert not (tmp_path / "fail").exists()
    p3, _ = run(tmp_path, "401", "{}")
    assert "http=401" in p3.stdout


def test_2xx_without_results_is_failure(tmp_path):
    p, _ = run(tmp_path, "200", '{"ok":true}')
    assert "조정 API 실패 (port=8102, http=200)" in p.stdout


def test_changed_primary_is_logged(tmp_path):
    body = '{"ok":true,"results":[{"provider":"anthropic","changed":true,"primary":"ANTHROPIC_AUTH_TOKEN_3"}]}'
    p, _ = run(tmp_path, "200", body)
    assert "anthropic: 주계정 → ANTHROPIC_AUTH_TOKEN_3" in p.stdout
    assert "조정 API 실패" not in p.stdout


def test_no_response_logs_http_000(tmp_path):
    p, _ = run(tmp_path, "", "")
    assert "http=000" in p.stdout
