"""알림 경로 복구 검증 — 실제 텔레그램·Docker·DB 를 건드리지 않는다.

배경: 호스트 스크립트 4개가 존재하지 않는 알림 스크립트를 조용히 건너뛰어,
슬롯1 OAuth 갱신 거부(2026-10-14 18:00 KST~)가 한 번도 알려지지 않았다.
"""
import json
import os
import re
import subprocess
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = ROOT / "scripts"
NOTIFY = SCRIPTS / "aads_notify.sh"
KEEPER = SCRIPTS / "claude_token_keeper.sh"
CALLERS = ["claude_token_keeper.sh", "disk_guard.sh", "disk_cleanup_v2.sh", "active_slot_watchdog.sh"]


def _run(cmd, env, **kw):
    return subprocess.run(cmd, env=env, capture_output=True, text=True, timeout=60, **kw)


def _base_env(tmp_path):
    env = {k: v for k, v in os.environ.items() if not k.startswith(("TELEGRAM_", "AADS_", "CLAUDE_"))}
    env["AADS_NOTIFY_LOG"] = str(tmp_path / "notify.log")
    env["AADS_NOTIFY_ENV_FILES"] = str(tmp_path / "none.env")
    return env


def _fake_curl(tmp_path, body='{"ok":true}', rc=0):
    bindir = tmp_path / "bin"
    bindir.mkdir(exist_ok=True)
    calls = tmp_path / "curl_calls.txt"
    curl = bindir / "curl"
    curl.write_text(
        '#!/bin/bash\n'
        f'printf "%s\\n" "$*" >> {calls}\n'
        'cat >> ' + str(tmp_path / "curl_stdin.txt") + '\n'
        f"printf '%s' '{body}'\n"
        f"exit {rc}\n"
    )
    curl.chmod(0o755)
    return bindir, calls


def test_no_callers_reference_missing_script():
    for name in CALLERS:
        src = (SCRIPTS / name).read_text()
        assert "send_disk_alert" not in src, name
        assert "aads_notify.sh" in src, name
    assert not (SCRIPTS / "send_disk_alert.sh").exists()


def test_no_hardcoded_telegram_secret():
    src = NOTIFY.read_text()
    assert not re.search(r"\d{8,}:[A-Za-z0-9_-]{30,}", src)


def test_notify_without_credentials_logs_error(tmp_path):
    env = _base_env(tmp_path)
    r = _run(["bash", str(NOTIFY), "테스트"], env)
    assert r.returncode == 2
    assert "ERROR" in r.stderr and "알림 미발송" in r.stderr
    assert "ERROR" in (tmp_path / "notify.log").read_text()


def test_notify_reads_env_file_and_sends(tmp_path):
    envfile = tmp_path / "x.env"
    envfile.write_text("OTHER=1\nTELEGRAM_BOT_TOKEN='123:ABC'\nTELEGRAM_CHAT_ID=\"-42\"\n")
    env = _base_env(tmp_path)
    env["AADS_NOTIFY_ENV_FILES"] = str(envfile)
    bindir, calls = _fake_curl(tmp_path)
    env["PATH"] = f"{bindir}:{env['PATH']}"
    r = _run(["bash", str(NOTIFY), "hello"], env)
    assert r.returncode == 0, r.stderr
    argv = calls.read_text()
    assert "123:ABC" not in argv  # 토큰은 인자(프로세스 목록)로 노출되지 않는다
    assert "chat_id=-42" in argv
    assert "123:ABC" in (tmp_path / "curl_stdin.txt").read_text()
    assert "OK" in (tmp_path / "notify.log").read_text()


def test_notify_send_failure_logs_error(tmp_path):
    env = _base_env(tmp_path)
    env["TELEGRAM_BOT_TOKEN"] = "123:ABC"
    env["TELEGRAM_CHAT_ID"] = "1"
    bindir, _ = _fake_curl(tmp_path, body='{"ok":false,"description":"Unauthorized"}')
    env["PATH"] = f"{bindir}:{env['PATH']}"
    r = _run(["bash", str(NOTIFY), "hello"], env)
    assert r.returncode == 1
    assert "ERROR" in r.stderr and "123:ABC" not in r.stderr


def test_caller_logs_error_when_alert_script_missing(tmp_path):
    guard = (SCRIPTS / "disk_guard.sh").read_text()
    fn = guard.split("send_alert() {", 1)[1].split("\n}\n", 1)[0]
    harness = (
        f'ALERT_SCRIPT={tmp_path}/does-not-exist.sh\n'
        'send_alert() {' + fn + '\n}\n'
        'send_alert "디스크 90%"; echo "rc=$?"\n'
    )
    r = _run(["bash", "-c", harness], _base_env(tmp_path))
    assert "ERROR" in r.stdout and "디스크 90%" in r.stdout
    assert "rc=1" in r.stdout


# ── claude_token_keeper 연속 실패 임계치 (가짜 슬롯 + 가짜 알림 스크립트) ──

def _keeper_env(tmp_path):
    env = _base_env(tmp_path)
    slots = tmp_path / "slots"
    cred = slots / "slot1" / ".claude" / ".credentials.json"
    cred.parent.mkdir(parents=True)
    fake_claude = tmp_path / "claude"
    fake_claude.write_text("#!/bin/bash\nexit 1\n")
    fake_claude.chmod(0o755)
    notify = tmp_path / "fake_notify.sh"
    sent = tmp_path / "sent.txt"
    notify.write_text(f'#!/bin/bash\necho "$1" >> {sent}\n')
    env.update({
        "CLAUDE_RELAY_SLOT_HOME_ROOT": str(slots),
        "CLAUDE_REAL_BIN": str(fake_claude),
        "CLAUDE_TOKEN_KEEPER_LOCK": str(tmp_path / "keeper.lock"),
        "CLAUDE_TOKEN_KEEPER_SLOT": "1",
        "CLAUDE_TOKEN_ALERT_STATE_DIR": str(tmp_path / "state"),
        "AADS_ALERT_SCRIPT": str(notify),
    })
    return env, cred, sent


def _write_cred(cred, expires_in_min):
    exp_ms = int((time.time() + expires_in_min * 60) * 1000)
    cred.write_text(json.dumps({"claudeAiOauth": {
        "accessToken": "sk-ant-oat01-x", "refreshToken": "sk-ant-ort01-y", "expiresAt": exp_ms}}))


def _keeper(env):
    r = _run(["bash", str(KEEPER)], env)
    assert r.returncode == 0, r.stdout + r.stderr
    return r.stdout


def _sent(sent):
    return sent.read_text().splitlines() if sent.exists() else []


def test_keeper_alerts_after_three_failures_then_throttles_then_recovers(tmp_path):
    env, cred, sent = _keeper_env(tmp_path)
    _write_cred(cred, -120)  # 이미 만료, 갱신 불가(가짜 CLI 는 아무것도 안 한다)

    _keeper(env)
    _keeper(env)
    assert _sent(sent) == []  # 2회 연속까진 알리지 않는다

    out = _keeper(env)
    assert len(_sent(sent)) == 1 and "slot1" in _sent(sent)[0] and "3회" in _sent(sent)[0], out

    _keeper(env)
    _keeper(env)
    assert len(_sent(sent)) == 1  # 같은 상태는 폭주하지 않는다

    alerted = tmp_path / "state" / "slot1.alerted"
    alerted.write_text(str(int(time.time()) - 6 * 3600 - 5))  # 6시간 경과
    _keeper(env)
    assert len(_sent(sent)) == 2

    _write_cred(cred, 300)  # 회복
    _keeper(env)
    msgs = _sent(sent)
    assert len(msgs) == 3 and "회복" in msgs[-1]
    _keeper(env)
    assert len(_sent(sent)) == 3  # 회복 알림은 1회뿐
    assert not alerted.exists()


def test_keeper_logs_error_when_alert_script_missing(tmp_path):
    env, cred, sent = _keeper_env(tmp_path)
    env["AADS_ALERT_SCRIPT"] = str(tmp_path / "missing.sh")
    _write_cred(cred, -120)
    _keeper(env)
    _keeper(env)
    out = _keeper(env)
    assert "ERROR: 알림 전송 실패" in out
    # 실패한 알림은 "알린 것"으로 기록하지 않아 다음 주기에 다시 시도한다.
    assert not (tmp_path / "state" / "slot1.alerted").exists()
