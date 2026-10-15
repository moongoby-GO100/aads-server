"""러너 장중(KST 평일 09:00~15:30) 입장 제한.

(AADS-RUNNER-MARKET-HOURS-ADMISSION-20261015)
시각은 RUNNER_MARKET_HOURS_TEST_NOW_EPOCH 로 고정한다. 기본(GATE 미설정)은 이전 동작과 같아야 한다.
"""
import datetime as dt
import json
import os
import subprocess
import time
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

SCRIPTS = Path(__file__).resolve().parents[2] / "scripts"
SH = SCRIPTS / "pipeline-runner.sh"
SRC = SH.read_text()
KST = ZoneInfo("Asia/Seoul")

FUNCS = (
    "runner_market_hours_now_epoch",
    "runner_market_holiday_kind",
    "runner_market_hours_evaluate",
    "runner_market_hours_active_now",
    "runner_market_hours_is_urgent",
    "runner_market_hours_urgent_sql",
    "runner_market_hours_note_deferred",
    "runner_market_hours_claim_extra",
    "runner_market_hours_reapply_last",
    "runner_policy_int_in_range",
    "runner_policy_warn_once",
    "runner_host_policy_apply",
    "runner_host_policy_refresh",
    "runner_heavy_publish_policy",
    "runner_heavy_install_shims",
    "runner_heavy_job_env",
    "runner_heavy_flush_events",
    "runner_worker_nice_value",
    "_receipt_json_str",
)


def _function(name: str) -> str:
    lines = SRC.splitlines()
    start = next(i for i, l in enumerate(lines) if l.startswith(f"{name}() {{"))
    end = next(i for i in range(start + 1, len(lines)) if lines[i] == "}")
    return "\n".join(lines[start : end + 1])


def kst(y, mo, d, hh, mm, ss=0) -> int:
    return int(dt.datetime(y, mo, d, hh, mm, ss, tzinfo=KST).timestamp())


# 2026-10-14 수, 2026-10-17 토. 2026-10-09(금)는 한글날 휴장으로 시험 파일에 넣는다.
WED = (2026, 10, 14)
SAT = (2026, 10, 17)
HOLIDAY = (2026, 10, 9)

CLEAN_ENV = {
    k: v for k, v in os.environ.items()
    if not k.startswith(("RUNNER_MARKET", "MAX_CONCURRENT", "RUNNER_LOW_PRIORITY", "AADS_HEAVY", "AADS_MARKET"))
}


def bash(body: str, env: dict | None = None, timeout: int = 60, funcs=FUNCS):
    script = "set -eo pipefail\n" + "\n".join(_function(n) for n in funcs) + "\n" + body
    return subprocess.run(
        ["bash", "-c", script], capture_output=True, text=True, timeout=timeout,
        env={**CLEAN_ENV, **(env or {})},
    )


@pytest.fixture
def holidays(tmp_path):
    f = tmp_path / "holidays.txt"
    f.write_text("# KRX 2026\n2026-01-01\n20261009  # 한글날\n2026-12-25\n")
    return f


def evaluate(now: int, env: dict | None = None) -> dict:
    e = {"RUNNER_MARKET_HOURS_GATE": "1", "RUNNER_MARKET_HOURS_TEST_NOW_EPOCH": str(now)}
    e.update(env or {})
    r = bash(
        "log() { echo \"LOG $*\" >&2; }\nRUNNER_HOST_NAME=h\nrunner_market_hours_evaluate\n"
        'echo "$RUNNER_MH_ACTIVE $RUNNER_MH_REASON $RUNNER_MH_UNTIL"\n',
        e,
    )
    assert r.returncode == 0, r.stderr
    active, reason, until = r.stdout.split()
    return {"active": active == "1", "reason": reason, "until": int(until), "stderr": r.stderr}


# ── 시각 · 휴장일 판정 ───────────────────────────────────────────────

def test_weekday_10am_with_known_trading_day_is_active(holidays):
    now = kst(*WED, 10, 0)
    r = evaluate(now, {"RUNNER_MARKET_HOLIDAY_FILE": str(holidays)})
    assert r["active"] and r["reason"] == "window"
    assert r["until"] == kst(*WED, 15, 30)


def test_window_boundaries_start_inclusive_end_exclusive(holidays):
    env = {"RUNNER_MARKET_HOLIDAY_FILE": str(holidays)}
    assert not evaluate(kst(*WED, 8, 59, 59), env)["active"]
    assert evaluate(kst(*WED, 9, 0), env)["active"]
    assert evaluate(kst(*WED, 15, 29, 59), env)["active"]
    assert evaluate(kst(*WED, 15, 29, 59), env)["until"] == kst(*WED, 15, 30)
    assert not evaluate(kst(*WED, 15, 30), env)["active"]


def test_1531_is_outside_window(holidays):
    r = evaluate(kst(*WED, 15, 31), {"RUNNER_MARKET_HOLIDAY_FILE": str(holidays)})
    assert not r["active"] and r["reason"] == "outside_window"


def test_saturday_is_not_market_hours_even_without_holiday_source():
    r = evaluate(kst(*SAT, 10, 0))
    assert not r["active"] and r["reason"] == "weekend"


def test_krx_holiday_10am_is_not_market_hours(holidays):
    r = evaluate(kst(*HOLIDAY, 10, 0), {"RUNNER_MARKET_HOLIDAY_FILE": str(holidays)})
    assert not r["active"] and r["reason"] == "krx_holiday"


def test_holiday_source_missing_is_conservative_market_hours():
    r = evaluate(kst(*WED, 10, 0))
    assert r["active"] and r["reason"] == "holiday_unknown"
    r = evaluate(kst(*HOLIDAY, 10, 0), {"RUNNER_MARKET_HOLIDAY_FILE": "/nonexistent/holidays.txt"})
    assert r["active"] and r["reason"] == "holiday_unknown"


def test_stale_holiday_file_without_current_year_is_a_failed_judgement(tmp_path):
    f = tmp_path / "old.txt"
    f.write_text("2025-10-09\n2025-12-25\n")
    r = evaluate(kst(*HOLIDAY, 10, 0), {"RUNNER_MARKET_HOLIDAY_FILE": str(f)})
    assert r["active"] and r["reason"] == "holiday_unknown"


def test_gate_off_is_never_active(holidays):
    r = evaluate(kst(*WED, 10, 0), {"RUNNER_MARKET_HOURS_GATE": "0", "RUNNER_MARKET_HOLIDAY_FILE": str(holidays)})
    assert not r["active"] and r["reason"] == "gate_off"
    r = evaluate(kst(*WED, 10, 0), {"RUNNER_MARKET_HOURS_GATE": ""})
    assert not r["active"]


def test_window_is_adjustable_by_env(holidays):
    env = {"RUNNER_MARKET_HOLIDAY_FILE": str(holidays), "RUNNER_MARKET_HOURS_START": "1000", "RUNNER_MARKET_HOURS_END": "1200"}
    assert not evaluate(kst(*WED, 9, 30), env)["active"]
    r = evaluate(kst(*WED, 11, 0), env)
    assert r["active"] and r["until"] == kst(*WED, 12, 0)
    assert not evaluate(kst(*WED, 12, 0), env)["active"]


def test_invalid_window_env_falls_back_to_default(holidays):
    env = {"RUNNER_MARKET_HOLIDAY_FILE": str(holidays), "RUNNER_MARKET_HOURS_START": "9am", "RUNNER_MARKET_HOURS_END": "1530"}
    r = evaluate(kst(*WED, 10, 0), env)
    assert r["active"] and "MARKET_HOURS_CONFIG_INVALID" in r["stderr"]
    env = {"RUNNER_MARKET_HOLIDAY_FILE": str(holidays), "RUNNER_MARKET_HOURS_START": "1600", "RUNNER_MARKET_HOURS_END": "1000"}
    assert evaluate(kst(*WED, 10, 0), env)["active"]  # 0900/1530 로 대체


# ── 긴급 판정 ────────────────────────────────────────────────────────

URGENT_CASES = {
    "URGENT_HOTFIX:true\nfix": True,
    "x URGENT_HOTFIX: True": True,
    "PRIORITY: P0\nx": True,
    "PRIORITY:P0": True,
    "PRIORITY: P1": False,
    "PRIORITY: P2": False,
    "PRIORITY: P01": False,
    "URGENT_HOTFIX:false": False,
    "title mentions P0 only": False,
    "": False,
}


@pytest.mark.parametrize("text,want", list(URGENT_CASES.items()))
def test_urgent_classification(text, want):
    r = bash('runner_market_hours_is_urgent "$1" && echo Y || echo N\n'.replace('"$1"', f"'{text}'"), funcs=FUNCS)
    assert r.stdout.strip() == ("Y" if want else "N"), (text, r.stderr)


def test_p1_is_urgent_only_when_opted_in():
    body = 'runner_market_hours_is_urgent "PRIORITY: P1" && echo Y || echo N\n'
    assert bash(body).stdout.strip() == "N"
    assert bash(body, {"RUNNER_MARKET_HOURS_URGENT_P1": "1"}).stdout.strip() == "Y"
    sql = bash('runner_market_hours_urgent_sql p.instruction\n', {"RUNNER_MARKET_HOURS_URGENT_P1": "1"}).stdout
    assert "P1" in sql and "P0" in sql and "URGENT_HOTFIX" in sql and "p.instruction" in sql
    sql = bash('runner_market_hours_urgent_sql p.instruction\n').stdout
    assert "P1" not in sql


# ── claim 조건 ───────────────────────────────────────────────────────

def _claim_script(count: str, extra_env: str = "") -> str:
    return f"""
log() {{ echo "LOG $*" >&2; }}
sql_escape() {{ echo "'$1'"; }}
RUNNER_HOST_NAME=host-a
RUNNER_MARKET_HOURS_GATE=1
RUNNER_MH_ACTIVE=1
RUNNER_MH_UNTIL=$(( $(date +%s) + 600 ))
RUNNER_MH_RESUME_HHMM=15:30
MAX_CONCURRENT_SERVER=20
{extra_env}
SQLLOG=$(mktemp)
db_exec() {{
  printf '%s\\n----\\n' "$1" >> "$SQLLOG"
  case "$1" in
    *"SELECT count(*)"*) {count} ;;
    *"SELECT p.job_id"*) printf 'runner-aaa\\nrunner-bbb\\n' ;;
  esac
}}
record_runner_event() {{ echo "EVENT $1 $2 $9" >&2; }}
"""


def test_claim_is_urgent_only_when_non_urgent_running_reaches_cap():
    r = bash(_claim_script("echo 1") + 'runner_market_hours_claim_extra "AND project IN (\'AADS\')"\necho "EXTRA=$RUNNER_MH_CLAIM_EXTRA"\ncat "$SQLLOG"\n')
    assert r.returncode == 0, r.stderr
    assert "EXTRA=AND (COALESCE(p.instruction" in r.stdout
    assert "PRIORITY:[[:space:]]*P0" in r.stdout
    # 비긴급 실행 수는 긴급을 빼고 센다. 서버 모드는 프로젝트 필터를 그대로 쓴다.
    assert "NOT (COALESCE(instruction" in r.stdout and "AND project IN ('AADS')" in r.stdout
    assert "MARKET_HOURS_DEFERRED job=runner-aaa" in r.stderr and "MARKET_HOURS_DEFERRED job=runner-bbb" in r.stderr
    assert "시작" in r.stderr and "15:30" in r.stderr


def test_claim_unrestricted_below_cap():
    r = bash(_claim_script("echo 0") + 'runner_market_hours_claim_extra ""\necho "EXTRA=[$RUNNER_MH_CLAIM_EXTRA]"\n')
    assert "EXTRA=[]" in r.stdout and "MARKET_HOURS_DEFERRED" not in r.stderr


def test_claim_cap_is_configurable_and_zero_means_urgent_only():
    body = 'runner_market_hours_claim_extra ""\necho "EXTRA=[$RUNNER_MH_CLAIM_EXTRA]"\n'
    r = bash(_claim_script("echo 2", "RUNNER_MARKET_HOURS_MAX_CONCURRENT=3") + body)
    assert "EXTRA=[]" in r.stdout
    r = bash(_claim_script("echo 3", "RUNNER_MARKET_HOURS_MAX_CONCURRENT=3") + body)
    assert "EXTRA=[AND " in r.stdout
    r = bash(_claim_script("echo 0", "RUNNER_MARKET_HOURS_MAX_CONCURRENT=0") + body)
    assert "EXTRA=[AND " in r.stdout


def test_claim_count_failure_is_conservative():
    r = bash(_claim_script("return 1") + 'runner_market_hours_claim_extra ""\necho "EXTRA=[$RUNNER_MH_CLAIM_EXTRA]"\n')
    assert "EXTRA=[AND " in r.stdout


def test_claim_no_restriction_when_gate_off_or_outside_window():
    body = 'runner_market_hours_claim_extra ""\necho "EXTRA=[$RUNNER_MH_CLAIM_EXTRA]"\ncat "$SQLLOG" | wc -l\n'
    r = bash(_claim_script("echo 9", "RUNNER_MARKET_HOURS_GATE=0") + body)
    assert r.stdout.split()[0] == "EXTRA=[]" and r.stdout.split()[1] == "0"  # DB 도 안 건드린다
    r = bash(_claim_script("echo 9", "RUNNER_MH_ACTIVE=0") + body)
    assert r.stdout.split()[0] == "EXTRA=[]"
    r = bash(_claim_script("echo 9", "RUNNER_MH_UNTIL=$(( $(date +%s) - 1 ))") + body)
    assert r.stdout.split()[0] == "EXTRA=[]"  # 캐시된 창이 이미 끝났다


def test_deferred_event_once_per_job_per_window_and_reset_on_exit():
    body = (
        'EVLOG=$(mktemp)\nrecord_runner_event() { echo "EVENT $1 $2" >> "$EVLOG"; }\n'
        'runner_market_hours_claim_extra ""\nrunner_market_hours_claim_extra ""\n'
        'echo "FIRST=$(grep -c "^EVENT" "$EVLOG")"\n'
    )
    r = bash(_claim_script("echo 5") + body)
    assert r.returncode == 0, r.stderr
    assert "FIRST=2" in r.stdout  # 작업 2건 × 호출 2회 = 이벤트 2건
    ev = [l for l in r.stderr.splitlines() if "MARKET_HOURS_DEFERRED job=" in l]
    assert len(ev) == 2


def test_deferred_event_metadata_exposes_status_text():
    script = _claim_script("echo 5")
    r = bash(script + 'EVLOG=$(mktemp)\nrecord_runner_event() { echo "$2|$9" >> "$EVLOG"; }\nrunner_market_hours_claim_extra ""\nhead -1 "$EVLOG"\n')
    ev, meta = r.stdout.strip().split("|", 1)
    assert ev == "market_hours_deferred"
    m = json.loads(meta)
    assert m["wait_reason"] == "market_hours_deferred" and m["reason"] == "MARKET_HOURS_DEFERRED"
    assert m["status_text"] == "장중 대기, 15:30 이후 시작"


def test_nothing_is_killed_or_updated_by_the_gate():
    for fn in ("runner_market_hours_claim_extra", "runner_market_hours_note_deferred", "runner_market_hours_evaluate"):
        body = _function(fn)
        assert "kill" not in body and "UPDATE" not in body and "DELETE" not in body, fn
    r = bash(_claim_script("echo 5") + 'runner_market_hours_claim_extra ""\ngrep -ciE "update|delete|insert" "$SQLLOG" || true\n')
    assert r.stdout.strip().splitlines()[-1] == "0"


def test_wired_before_claim_in_main_loop_and_claim_uses_extra():
    main = _function("main")
    assert main.index("runner_market_hours_claim_extra") < main.index("claim_queued_job")
    assert main.index("runner_host_policy_refresh") < main.index("runner_market_hours_claim_extra")
    assert "${RUNNER_MH_CLAIM_EXTRA:-}" in _function("_claim_queued_job")
    claim = SRC.split("claim_queued_job() (\n", 1)[1].split("\n)\n", 1)[0]
    assert "RUNNER_MARKET" not in claim  # 서버 상한 로직은 그대로


# ── 정책 파일 · shim ─────────────────────────────────────────────────

def _policy_env(lane: Path, now: int, **extra) -> dict:
    e = {
        "RUNNER_MARKET_HOURS_GATE": "1",
        "RUNNER_MARKET_HOURS_TEST_NOW_EPOCH": str(now),
    }
    e.update(extra)
    return e


def _apply_script(lane: Path, row: str = "") -> str:
    return f"""
RUNNER_HEAVY_DIR={lane}
RUNNER_POLICY_REFRESH_SEC=0
RUNNER_HOST_NAME=host-a
MAX_CONCURRENT_SERVER=20
log() {{ echo "LOG $*" >&2; }}
sql_escape() {{ echo "'$1'"; }}
STATE={lane}.state
mkdir -p "$STATE"
printf 't' > "$STATE/ready"
printf '{row}' > "$STATE/row"
db_exec() {{
  [[ -f "$STATE/fail" ]] && return 1
  case "$1" in *to_regclass*) cat "$STATE/ready" ;; *) cat "$STATE/row" ;; esac
}}
runner_host_policy_refresh
"""


def test_market_hours_publish_hold_even_without_policy_row(tmp_path, holidays):
    lane = tmp_path / "lane"
    now = kst(*WED, 10, 0)
    r = bash(_apply_script(lane), _policy_env(lane, now, RUNNER_MARKET_HOLIDAY_FILE=str(holidays)))
    assert r.returncode == 0, r.stderr
    pol = dict(l.split("=") for l in (lane / "policy").read_text().split())
    assert pol["market_hold_until"] == str(kst(*WED, 15, 30))
    assert pol["slots"] == "0" and pol["urgent"] == "0"
    assert not (lane / "reuse").exists()
    assert "MARKET_HOURS_ENTER" in r.stderr and "MARKET_HOURS_POLICY" in r.stderr


def test_market_hours_keeps_row_slots_and_urgent_reservation(tmp_path, holidays):
    lane = tmp_path / "lane"
    now = kst(*WED, 10, 0)
    r = bash(_apply_script(lane, "4\\x1e10\\x1e3\\x1e1\\x1e\\n"), _policy_env(lane, now, RUNNER_MARKET_HOLIDAY_FILE=str(holidays)))
    assert r.returncode == 0, r.stderr
    pol = dict(l.split("=") for l in (lane / "policy").read_text().split())
    assert (pol["slots"], pol["urgent"], pol["revision"]) == ("3", "1", "4")
    assert "market_hold_until" in pol


@pytest.mark.parametrize("when", ["sat", "holiday", "after"])
def test_no_hold_outside_market_hours(tmp_path, holidays, when):
    lane = tmp_path / "lane"
    now = {"sat": kst(*SAT, 10, 0), "holiday": kst(*HOLIDAY, 10, 0), "after": kst(*WED, 15, 31)}[when]
    r = bash(_apply_script(lane), _policy_env(lane, now, RUNNER_MARKET_HOLIDAY_FILE=str(holidays)))
    assert r.returncode == 0, r.stderr
    assert not (lane / "policy").exists()


def test_gate_off_policy_file_is_exactly_as_before(tmp_path, holidays):
    lane = tmp_path / "lane"
    now = kst(*WED, 10, 0)
    env = _policy_env(lane, now, RUNNER_MARKET_HOURS_GATE="0", RUNNER_MARKET_HOLIDAY_FILE=str(holidays))
    r = bash(_apply_script(lane), env)
    assert r.returncode == 0, r.stderr
    assert not (lane / "policy").exists() and "MARKET_HOURS" not in r.stderr
    lane2 = tmp_path / "lane2"
    r = bash(_apply_script(lane2, "2\\x1e10\\x1e2\\x1e1\\x1e\\n"), env)
    assert (lane2 / "policy").read_text() == "slots=2\nurgent=1\nrevision=2\n"
    assert "MARKET_HOURS" not in r.stderr


def test_window_end_clears_hold_on_next_refresh(tmp_path, holidays):
    lane = tmp_path / "lane"
    env = _policy_env(lane, kst(*WED, 15, 29), RUNNER_MARKET_HOLIDAY_FILE=str(holidays))
    script = _apply_script(lane) + 'RUNNER_MARKET_HOURS_TEST_NOW_EPOCH=%d\nrunner_host_policy_refresh\n' % kst(*WED, 15, 31)
    r = bash(script, env)
    assert r.returncode == 0, r.stderr
    assert not (lane / "policy").exists()
    assert "MARKET_HOURS_EXIT" in r.stderr


def test_db_failure_still_refreshes_market_hold(tmp_path, holidays):
    lane = tmp_path / "lane"
    env = _policy_env(lane, kst(*WED, 10, 0), RUNNER_MARKET_HOLIDAY_FILE=str(holidays))
    script = _apply_script(lane).replace("runner_host_policy_refresh\n", 'touch "$STATE/fail"\nrunner_host_policy_refresh\n', 1)
    r = bash(script, env)
    assert r.returncode == 0, r.stderr
    assert "market_hold_until" in (lane / "policy").read_text()


def _install(tmp_path: Path) -> Path:
    lane = tmp_path / "lane"
    r = bash(f"RUNNER_HEAVY_DIR={lane}\nrunner_heavy_install_shims\n")
    assert r.returncode == 0, r.stderr
    return lane


def _real_bin(tmp_path: Path) -> Path:
    real = tmp_path / "real"
    real.mkdir()
    for name in ("pytest", "py.test", "python3", "python", "npm", "npx", "node", "tsc", "next", "bash"):
        f = real / name
        f.write_text(f'#!/bin/sh\necho "REAL {name} HELD=${{AADS_HEAVY_HELD-unset}} ARGS=$*"\n')
        f.chmod(0o755)
    return real


def _env(lane: Path, real: Path, **extra) -> dict:
    e = {
        "PATH": f"{lane}/bin:{real}:/usr/bin:/bin",
        "AADS_HEAVY_LANE_DIR": str(lane),
        "AADS_HEAVY_POLL_SEC": "0.1",
        "HOME": str(lane.parent),
    }
    e.update(extra)
    return e


def _policy(lane: Path, slots=0, urgent=0, hold_until=0, hold_wait=1800):
    txt = f"slots={slots}\nurgent={urgent}\nrevision=1\n"
    if hold_until:
        txt += f"market_hold_until={hold_until}\nmarket_hold_max_wait={hold_wait}\n"
    (lane / "policy").write_text(txt)


def _run(cmd, env, timeout=30):
    return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, env=env)


def _events(lane: Path, job="nojob"):
    f = lane / "events" / f"{job}.jsonl"
    if not f.exists():
        return []
    return [(l.split("\t", 1)[0], json.loads(l.split("\t", 1)[1])) for l in f.read_text().splitlines()]


def test_non_urgent_heavy_waits_until_hold_expires_then_runs(tmp_path):
    lane, real = _install(tmp_path), _real_bin(tmp_path)
    _policy(lane, hold_until=int(time.time()) + 3)
    t0 = time.time()
    r = _run([str(lane / "bin/pytest"), "-q"], _env(lane, real, AADS_MARKET_URGENT="0", AADS_HEAVY_JOB_ID="runner-t1", AADS_HEAVY_EVENT_INTERVAL_SEC="1"))
    waited = time.time() - t0
    assert r.returncode == 0 and "REAL pytest" in r.stdout, (r.stdout, r.stderr)
    assert 1.5 <= waited < 10, waited
    evs = _events(lane, "runner-t1")
    assert evs and evs[0][0] == "heavy_lane_market_hours_deferred"
    meta = evs[0][1]
    assert meta["wait_reason"] == "market_hours_deferred" and meta["reason"] == "MARKET_HOURS_DEFERRED"
    assert meta["status_text"].startswith("장중 대기, ") and meta["status_text"].endswith(" 이후 시작")
    assert meta["urgent"] == 0 and meta["next_check_at"]


def test_urgent_heavy_passes_hold_immediately(tmp_path):
    lane, real = _install(tmp_path), _real_bin(tmp_path)
    _policy(lane, hold_until=int(time.time()) + 60)
    t0 = time.time()
    r = _run([str(lane / "bin/pytest"), "-q"], _env(lane, real, AADS_MARKET_URGENT="1"))
    assert r.returncode == 0 and "REAL pytest" in r.stdout and time.time() - t0 < 2
    assert _events(lane) == []


def test_urgent_still_uses_reserved_slot_when_row_has_slots(tmp_path):
    lane, real = _install(tmp_path), _real_bin(tmp_path)
    _policy(lane, slots=2, urgent=1, hold_until=int(time.time()) + 60)
    r = _run([str(lane / "bin/pytest")], _env(lane, real, AADS_MARKET_URGENT="1", AADS_HEAVY_URGENT="1"))
    assert "HELD=slot-1" in r.stdout, r.stdout  # 예약 슬롯(마지막 번호)부터 잡는다


def test_hold_waits_even_when_row_has_no_slots_and_normal_job_passes_after_expiry(tmp_path):
    lane, real = _install(tmp_path), _real_bin(tmp_path)
    _policy(lane, slots=0, hold_until=int(time.time()) - 5)  # 이미 지난 창
    t0 = time.time()
    r = _run([str(lane / "bin/pytest")], _env(lane, real, AADS_MARKET_URGENT="0"))
    assert "REAL pytest" in r.stdout and time.time() - t0 < 2


def test_non_heavy_commands_pass_during_hold(tmp_path):
    lane, real = _install(tmp_path), _real_bin(tmp_path)
    _policy(lane, hold_until=int(time.time()) + 60)
    env = _env(lane, real, AADS_MARKET_URGENT="0")
    for name, args in (("npm", ["--version"]), ("node", ["server.js"]), ("bash", ["-c", "echo hi"]),
                       ("python3", ["-c", "print(1)"]), ("python3", ["scripts/x.py"]), ("python3", ["-m", "http.server"])):
        t0 = time.time()
        r = _run([str(lane / "bin" / name), *args], env)
        assert f"REAL {name}" in r.stdout and time.time() - t0 < 1.5, (name, args, r.stdout, r.stderr)


@pytest.mark.parametrize("name,args", [
    ("pytest", ["-q"]), ("py.test", ["-q"]), ("python3", ["-m", "pytest", "-q", "tests/"]),
    ("python", ["-m", "pytest"]), ("python3", ["-X", "dev", "-m", "pytest"]),
    ("npm", ["test"]), ("tsc", ["--noEmit"]), ("bash", ["scripts/run_unit_tests.sh", "t.py"]),
])
def test_heavy_classification_is_held(tmp_path, name, args):
    lane, real = _install(tmp_path), _real_bin(tmp_path)
    _policy(lane, hold_until=int(time.time()) + 60, hold_wait=1)
    r = _run([str(lane / "bin" / name), *args], _env(lane, real, AADS_MARKET_URGENT="0"))
    assert r.returncode == 75, (name, args, r.returncode, r.stdout)
    assert f"REAL {name}" not in r.stdout


def test_hold_wait_cap_refuses_instead_of_running_and_says_so(tmp_path):
    lane, real = _install(tmp_path), _real_bin(tmp_path)
    _policy(lane, hold_until=int(time.time()) + 60, hold_wait=1)
    r = _run([str(lane / "bin/pytest")], _env(lane, real, AADS_MARKET_URGENT="0", AADS_HEAVY_JOB_ID="runner-t2"))
    assert r.returncode == 75 and "REAL pytest" not in r.stdout
    assert "MARKET_HOURS_DEFERRED" in r.stderr and "실행하지 않았습니다" in r.stderr
    assert [e for e, _ in _events(lane, "runner-t2")][-1] == "heavy_lane_market_hours_refused"


def test_hold_released_when_runner_removes_it(tmp_path):
    lane, real = _install(tmp_path), _real_bin(tmp_path)
    _policy(lane, hold_until=int(time.time()) + 300)
    p = subprocess.Popen([str(lane / "bin/pytest")], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                         env=_env(lane, real, AADS_MARKET_URGENT="0"))
    time.sleep(0.8)
    assert p.poll() is None  # 아직 대기
    _policy(lane)  # 러너가 창 종료를 반영
    out, _ = p.communicate(timeout=10)
    assert p.returncode == 0 and "REAL pytest" in out


def test_running_command_is_not_interrupted_when_window_opens(tmp_path):
    lane = _install(tmp_path)
    real = tmp_path / "real"
    real.mkdir()
    long = real / "pytest"
    long.write_text("#!/bin/sh\nsleep 2\necho FINISHED\n")
    long.chmod(0o755)
    _policy(lane)  # hold 없음 — 창 밖에서 시작
    env = _env(lane, real, AADS_MARKET_URGENT="0")
    p = subprocess.Popen([str(lane / "bin/pytest")], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=env)
    time.sleep(0.6)
    _policy(lane, hold_until=int(time.time()) + 300)  # 실행 중에 창 진입
    out, err = p.communicate(timeout=15)
    assert p.returncode == 0 and "FINISHED" in out, (out, err)


def test_runner_apply_never_signals_processes():
    for fn in ("runner_host_policy_apply", "runner_heavy_publish_policy", "runner_market_hours_reapply_last"):
        assert "kill" not in _function(fn), fn


# ── 작업 환경 · nice ─────────────────────────────────────────────────

def test_job_env_marks_market_urgency_only_when_gate_on(tmp_path):
    lane = _install(tmp_path)
    (lane / "policy").write_text("slots=0\nurgent=0\nrevision=1\n")
    cases = {"URGENT_HOTFIX:true": "1", "PRIORITY: P0": "1", "PRIORITY: P1": "0", "PRIORITY: P2": "0"}
    for instr, want in cases.items():
        r = bash(f'RUNNER_HEAVY_DIR={lane}\nrunner_heavy_job_env "$1" runner-x\necho "${{AADS_MARKET_URGENT-unset}}"\n'.replace('"$1"', f"'{instr}'"),
                 {"RUNNER_MARKET_HOURS_GATE": "1"})
        assert r.stdout.strip() == want, (instr, r.stderr)
    r = bash(f'RUNNER_HEAVY_DIR={lane}\nrunner_heavy_job_env "PRIORITY: P0" runner-x\necho "${{AADS_MARKET_URGENT-unset}}"\n')
    assert r.stdout.strip() == "unset"


def _nice(instr: str, gate: str, active: bool, urgent_p1: str = "0") -> str:
    until = "$(( $(date +%s) + 600 ))" if active else "0"
    body = f'RUNNER_MH_ACTIVE={1 if active else 0}\nRUNNER_MH_UNTIL={until}\nrunner_worker_nice_value \'{instr}\'\n'
    return bash(body, {"RUNNER_MARKET_HOURS_GATE": gate, "RUNNER_MARKET_HOURS_URGENT_P1": urgent_p1}).stdout.strip()


def test_non_urgent_nice_is_19_during_market_hours_and_urgent_unchanged():
    assert _nice("PRIORITY: P1", "1", True) == "19"
    assert _nice("PRIORITY: P2", "1", True) == "19"
    assert _nice("PRIORITY: P0", "1", True) == "0"
    assert _nice("URGENT_HOTFIX:true", "1", True) == "10"  # 긴급은 기존 규칙 그대로(P0/P1 태그 없으면 기본 nice)
    assert _nice("PRIORITY: P1", "1", True, urgent_p1="1") == "0"


def test_nice_unchanged_when_gate_off_or_outside_window():
    assert _nice("PRIORITY: P1", "0", True) == "0"
    assert _nice("PRIORITY: P2", "0", True) == "10"
    assert _nice("PRIORITY: P1", "1", False) == "0"
    assert _nice("PRIORITY: P2", "1", False) == "10"


# ── 이벤트 flush ─────────────────────────────────────────────────────

def test_flush_logs_market_hours_deferred_and_records_event(tmp_path):
    lane = _install(tmp_path)
    line = '\t'.join(["heavy_lane_market_hours_deferred", json.dumps({"event": "heavy_lane_market_hours_deferred", "wait_reason": "market_hours_deferred", "status_text": "장중 대기, 15:30 이후 시작"}, ensure_ascii=False)])
    (lane / "events").mkdir(exist_ok=True)
    (lane / "events/runner-z.jsonl").write_text(line + "\n")
    r = bash(
        f"""RUNNER_HEAVY_DIR={lane}
RUNNER_HOST_NAME=h
log() {{ echo "LOG $*" >&2; }}
runner_cpu_context_json() {{ printf '{{"snapshot_status":"unavailable"}}'; }}
record_runner_event() {{ echo "EVT $1 $2 $9"; }}
runner_heavy_flush_events
"""
    )
    assert r.returncode == 0, r.stderr
    assert "MARKET_HOURS_DEFERRED job=runner-z" in r.stderr
    evt = [l for l in r.stdout.splitlines() if l.startswith("EVT")][0]
    assert "EVT runner-z heavy_lane_market_hours_deferred" in evt and "장중 대기, 15:30 이후 시작" in evt


# ── 스크립트 일반 ────────────────────────────────────────────────────

def test_script_valid_local_copy_in_sync_and_default_is_off():
    assert subprocess.run(["bash", "-n", str(SH)]).returncode == 0
    assert SRC == (SCRIPTS / "pipeline-runner.sh.local").read_text()
    # 새 코드는 모두 GATE=1 뒤에 있다 — 기본값 0 이 이전 동작을 보존한다.
    assert 'RUNNER_MARKET_HOURS_GATE:-0}" == "1"' in _function("runner_market_hours_active_now")
    assert "${RUNNER_MARKET_HOURS_GATE:-0}" in _function("runner_host_policy_refresh")
