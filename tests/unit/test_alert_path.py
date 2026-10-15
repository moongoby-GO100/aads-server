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
KEYS = ["ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_AUTH_TOKEN_2", "ANTHROPIC_AUTH_TOKEN_3", "ANTHROPIC_AUTH_TOKEN_4"]
CALLERS = ["claude_token_keeper.sh", "disk_guard.sh", "disk_cleanup_v2.sh", "active_slot_watchdog.sh"]


def _run(cmd, env, **kw):
    return subprocess.run(cmd, env=env, capture_output=True, text=True, timeout=60, **kw)


def _base_env(tmp_path):
    env = {k: v for k, v in os.environ.items() if not k.startswith(("TELEGRAM_", "AADS_", "CLAUDE_"))}
    env["AADS_NOTIFY_LOG"] = str(tmp_path / "notify.log")
    env["AADS_NOTIFY_ENV_FILES"] = str(tmp_path / "none.env")
    env["AADS_NOTIFY_STATE_DIR"] = str(tmp_path / "notify-state")
    env["AADS_NOTIFY_CHANNELS"] = "telegram"  # 채널별 테스트는 각자 명시한다
    return env


def _fake_docker(tmp_path, exec_out="", exec_rc=0, inspect_rc=1):
    """실제 컨테이너·DB 를 건드리지 않는 가짜 docker. 호출은 docker_calls.txt 에 남는다."""
    bindir = tmp_path / "bin"
    bindir.mkdir(exist_ok=True)
    calls = tmp_path / "docker_calls.txt"
    (tmp_path / "docker_exec_out.txt").write_text(exec_out)
    docker = bindir / "docker"
    docker.write_text(
        '#!/bin/bash\n'
        f'printf "%s\\n" "$*" >> {calls}\n'
        'if [ "$1" = inspect ]; then exit ' + str(inspect_rc) + '; fi\n'
        'if [ "$1" = exec ]; then\n'
        f'  cat > {tmp_path}/docker_stdin.txt\n'
        f'  printf "%s" "$(cat {tmp_path}/docker_exec_out.txt)"\n'
        f'  exit {exec_rc}\n'
        'fi\n'
        'exit 0\n'
    )
    docker.chmod(0o755)
    return bindir, calls


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
    assert r.returncode == 1
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


# ── 다채널: 앱 내 알림함(1순위) → 텔레그램(보조) ──

def _notify_env(tmp_path, channels="inapp,telegram", docker_out="AADS_NOTIFY_RESULT=ok\n", docker_rc=0):
    env = _base_env(tmp_path)
    env["AADS_NOTIFY_CHANNELS"] = channels
    env["AADS_NOTIFY_CONTAINER"] = "fake-container"
    env["TELEGRAM_BOT_TOKEN"] = "123:ABC"
    env["TELEGRAM_CHAT_ID"] = "1"
    dbin, dcalls = _fake_docker(tmp_path, exec_out=docker_out, exec_rc=docker_rc)
    cbin, ccalls = _fake_curl(tmp_path)
    env["PATH"] = f"{dbin}:{env['PATH']}"
    return env, dcalls, ccalls


def test_notify_inapp_first_and_telegram_not_tried_on_success(tmp_path):
    env, dcalls, ccalls = _notify_env(tmp_path)
    r = _run(["bash", str(NOTIFY), "🔴 슬롯3 갱신 실패\n두번째 줄"], env)
    assert r.returncode == 0, r.stderr
    assert "exec -i" in dcalls.read_text() and "fake-container" in dcalls.read_text()
    assert not ccalls.exists()  # 앱 내 성공 → 보조 채널은 건드리지 않는다
    # 메시지·시크릿은 docker 인자로 노출되지 않는다(환경변수 이름만 전달)
    assert "슬롯3" not in dcalls.read_text() and "123:ABC" not in dcalls.read_text()
    assert "ohvis_alert.notify" in (tmp_path / "docker_stdin.txt").read_text()
    assert "OK aads_notify[inapp]" in (tmp_path / "notify.log").read_text()


def test_notify_falls_back_to_telegram_when_inapp_fails(tmp_path):
    env, dcalls, ccalls = _notify_env(tmp_path, docker_out="boom", docker_rc=1)
    r = _run(["bash", str(NOTIFY), "hello"], env)
    assert r.returncode == 0, r.stderr
    assert "앱 내 알림함 저장 실패" in r.stderr  # 실패한 채널도 ERROR 로 남는다
    assert ccalls.exists()
    assert "OK aads_notify[telegram]" in (tmp_path / "notify.log").read_text()


def test_notify_inapp_result_fail_counts_as_failure(tmp_path):
    env, _, ccalls = _notify_env(tmp_path, channels="inapp", docker_out="AADS_NOTIFY_RESULT=fail\n")
    r = _run(["bash", str(NOTIFY), "hello"], env)
    assert r.returncode == 1
    assert "앱 내 알림함 저장 실패" in r.stderr and "모든 채널 실패" in r.stderr


def test_notify_exit1_with_per_channel_errors_when_all_fail(tmp_path):
    env, _, _ = _notify_env(tmp_path, docker_out="boom", docker_rc=1)
    bindir, _ = _fake_curl(tmp_path, body="", rc=60)
    r = _run(["bash", str(NOTIFY), "hello"], env)
    assert r.returncode == 1
    assert "앱 내 알림함 저장 실패" in r.stderr
    assert "텔레그램 전송 실패(curl rc=60)" in r.stderr
    assert "모든 채널 실패" in r.stderr
    assert "123:ABC" not in r.stderr
    log = (tmp_path / "notify.log").read_text()
    assert log.count("ERROR") == 3


def test_notify_dedupes_same_message_within_window(tmp_path):
    env, dcalls, _ = _notify_env(tmp_path, channels="inapp")
    assert _run(["bash", str(NOTIFY), "같은 메시지"], env).returncode == 0
    assert _run(["bash", str(NOTIFY), "같은 메시지"], env).returncode == 0
    assert dcalls.read_text().count("exec -i") == 1  # 두 번째는 억제
    assert "SUPPRESSED" in (tmp_path / "notify.log").read_text()
    assert _run(["bash", str(NOTIFY), "다른 메시지"], env).returncode == 0
    assert dcalls.read_text().count("exec -i") == 2


def test_notify_dedupe_expires_and_failed_send_is_not_recorded(tmp_path):
    env, dcalls, _ = _notify_env(tmp_path, channels="inapp")
    assert _run(["bash", str(NOTIFY), "반복"], env).returncode == 0
    for f in (tmp_path / "notify-state").iterdir():
        old = time.time() - 61 * 60
        os.utime(f, (old, old))
    assert _run(["bash", str(NOTIFY), "반복"], env).returncode == 0
    assert dcalls.read_text().count("exec -i") == 2  # 60분 경과 → 다시 발송

    sub = tmp_path / "failcase"
    sub.mkdir()
    env2, dcalls2, _ = _notify_env(sub, channels="inapp", docker_out="boom", docker_rc=1)
    assert _run(["bash", str(NOTIFY), "실패건"], env2).returncode == 1
    assert _run(["bash", str(NOTIFY), "실패건"], env2).returncode == 1
    assert dcalls2.read_text().count("exec -i") == 2  # 실패는 억제하지 않고 재시도한다


def test_notify_dedupe_disabled_with_zero(tmp_path):
    env, dcalls, _ = _notify_env(tmp_path, channels="inapp")
    env["AADS_NOTIFY_DEDUPE_MIN"] = "0"
    _run(["bash", str(NOTIFY), "m"], env)
    _run(["bash", str(NOTIFY), "m"], env)
    assert dcalls.read_text().count("exec -i") == 2


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
    # 실제 컨테이너·DB 를 건드리지 않는다(활성 조회는 전부 active, DB 동기화는 inspect 실패로 건너뜀).
    dbin, dcalls = _fake_docker(
        tmp_path, exec_out="".join(f"{k}=active\n" for k in KEYS))
    env["PATH"] = f"{dbin}:{env['PATH']}"
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


# ── claude_token_keeper: 비활성 슬롯(llm_api_keys.is_active=false) 건너뛰기 ──

def _set_active_rows(tmp_path, **states):
    (tmp_path / "docker_exec_out.txt").write_text(
        "".join(f"{k}={v}\n" for k, v in states.items()))


def test_keeper_skips_inactive_slot_without_refresh_or_alert(tmp_path):
    env, cred, sent = _keeper_env(tmp_path)
    cred_marker = tmp_path / "claude_ran.txt"
    fake_claude = Path(env["CLAUDE_REAL_BIN"])
    fake_claude.write_text(f'#!/bin/bash\necho ran >> {cred_marker}\nexit 1\n')
    _write_cred(cred, -120)  # 활성이었다면 갱신 실패 → 알림 대상
    _set_active_rows(tmp_path, ANTHROPIC_AUTH_TOKEN="inactive")
    state = tmp_path / "state"
    state.mkdir()
    (state / "slot1.fails").write_text("2")

    for _ in range(4):
        out = _keeper(env)
        assert "slot1: 비활성(DB) — 건너뜀" in out
    assert not cred_marker.exists()  # CLI 갱신을 시도하지 않는다
    assert _sent(sent) == []
    assert "갱신 실패" not in out
    assert not (state / "slot1.fails").exists()  # 묵은 연속 실패 횟수는 비운다


def test_keeper_only_skips_the_inactive_slot(tmp_path):
    env, cred, sent = _keeper_env(tmp_path)
    del env["CLAUDE_TOKEN_KEEPER_SLOT"]
    cred2 = tmp_path / "slots" / "slot2" / ".claude" / ".credentials.json"
    cred2.parent.mkdir(parents=True)
    _write_cred(cred, 300)
    _write_cred(cred2, 300)
    _set_active_rows(tmp_path, ANTHROPIC_AUTH_TOKEN="active", ANTHROPIC_AUTH_TOKEN_2="inactive")
    out = _keeper(env)
    assert "slot2: 비활성(DB) — 건너뜀" in out
    assert "slot1: 비활성" not in out


def test_keeper_failopen_when_db_lookup_fails(tmp_path):
    env, cred, sent = _keeper_env(tmp_path)
    _write_cred(cred, -120)
    dbin, _ = _fake_docker(tmp_path, exec_out="Traceback: connection refused", exec_rc=1)
    out = _keeper(env)
    assert "슬롯 활성 여부 DB 조회 실패" in out and "기존 동작(갱신 시도) 유지" in out
    assert "비활성(DB)" not in out
    assert "slot1: 만료까지" in out and "갱신 실패" in out  # 갱신 시도는 그대로 진행
    _keeper(env)
    _keeper(env)
    assert len(_sent(sent)) == 1  # 알림 경로도 그대로


def test_keeper_failopen_when_key_row_missing(tmp_path):
    env, cred, sent = _keeper_env(tmp_path)
    _write_cred(cred, -120)
    _set_active_rows(tmp_path, ANTHROPIC_AUTH_TOKEN_4="active")  # slot1 행 없음
    out = _keeper(env)
    assert "비활성(DB)" not in out and "slot1: 만료까지" in out


def test_keeper_slot_key_mapping_matches_db_key_names():
    src = KEEPER.read_text()
    assert '1) printf \'%s\' "ANTHROPIC_AUTH_TOKEN"' in src
    assert "2|3|4) printf 'ANTHROPIC_AUTH_TOKEN_%s'" in src
