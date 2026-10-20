from __future__ import annotations

import json
import os
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from scripts import aag_scheduler_state as st

REPO = Path(__file__).resolve().parents[2]
# 2026-10-20 is a Tuesday. 12:00Z = 21:00 KST (outside trading hours).
OFF = datetime(2026, 10, 20, 12, 0, tzinfo=UTC)
# 01:00Z = 10:00 KST (inside).
MARKET = datetime(2026, 10, 20, 1, 0, tzinfo=UTC)


def _later(minutes: int, base: datetime = OFF) -> datetime:
    return base + timedelta(minutes=minutes)


def _read(tmp_path: Path, project: str = "GO100") -> dict:
    return json.loads((tmp_path / "scheduler" / f"{project}.json").read_text())


def _dead_pid() -> int:
    proc = subprocess.Popen(["true"])
    proc.wait()
    return proc.pid


# ---- market window ---------------------------------------------------------------
@pytest.mark.parametrize("when,inside", [
    (datetime(2026, 10, 19, 23, 0, tzinfo=UTC), True),    # Tue 08:00 KST
    (datetime(2026, 10, 19, 22, 59, tzinfo=UTC), False),  # Tue 07:59 KST
    (datetime(2026, 10, 20, 6, 39, tzinfo=UTC), True),    # 15:39 KST
    (datetime(2026, 10, 20, 6, 40, tzinfo=UTC), False),   # 15:40 KST
    (datetime(2026, 10, 24, 1, 0, tzinfo=UTC), False),    # Saturday 10:00 KST
    (datetime(2026, 10, 25, 1, 0, tzinfo=UTC), False),    # Sunday
])
def test_market_window_boundaries(when, inside):
    assert st.in_market_window(when) is inside


# ---- begin: duplicate / market / takeover --------------------------------------
def test_first_begin_runs_and_records_owner(tmp_path):
    code, state = st.begin(tmp_path, "GO100", "a", now=OFF, market_guard=True, pid=os.getpid())
    assert code == st.EXIT_OK and state["status"] == "running" and state["owner"] == "a"
    assert _read(tmp_path)["heartbeat_at"] == "2026-10-20T12:00:00Z"


def test_market_window_skip_is_exit_7_and_not_success(tmp_path):
    code, state = st.begin(tmp_path, "GO100", "a", now=MARKET, market_guard=True)
    assert code == st.EXIT_SKIP == 7
    assert state["status"] == "skipped" and state["reason"] == "market_window"
    assert state["last_success_at"] is None
    assert st.freshness_evidence_at(state) is None


def test_market_guard_off_does_not_skip(tmp_path):
    code, _ = st.begin(tmp_path, "AADS", "a", now=MARKET, market_guard=False)
    assert code == st.EXIT_OK


def test_second_live_owner_is_skipped_and_first_stays_owner(tmp_path):
    st.begin(tmp_path, "GO100", "a", now=OFF, pid=os.getpid())
    code, state = st.begin(tmp_path, "GO100", "b", now=_later(5), pid=os.getpid())
    assert code == st.EXIT_SKIP
    disk = _read(tmp_path)
    assert disk["owner"] == "a" and disk["status"] == "running"
    assert disk["last_skip_reason"] == "running_owner" and disk["skips_total"] == 1
    assert state["abandoned_runs"] == 0


def test_stale_heartbeat_is_taken_over_and_counted_as_failure(tmp_path):
    st.begin(tmp_path, "GO100", "a", now=OFF, pid=os.getpid())
    code, state = st.begin(tmp_path, "GO100", "b", now=_later(45), pid=os.getpid())
    assert code == st.EXIT_OK and state["owner"] == "b"
    assert state["abandoned_runs"] == 1 and state["recovered_from"]["owner"] == "a"
    assert state["last_failure_reason"] == "abandoned_run" and state["consecutive_failures"] == 1
    assert state["last_success_at"] is None


def test_dead_pid_is_taken_over_before_heartbeat_goes_stale(tmp_path):
    st.begin(tmp_path, "GO100", "a", now=OFF, pid=_dead_pid())
    code, state = st.begin(tmp_path, "GO100", "b", now=_later(1), pid=os.getpid())
    assert code == st.EXIT_OK and state["recovered_from"]["owner"] == "a"


def test_heartbeat_keeps_run_alive_against_takeover(tmp_path):
    st.begin(tmp_path, "GO100", "a", now=OFF, pid=os.getpid())
    st.heartbeat(tmp_path, "GO100", "a", now=_later(25))
    code, _ = st.begin(tmp_path, "GO100", "b", now=_later(45), pid=os.getpid())
    assert code == st.EXIT_SKIP


def test_heartbeat_from_non_owner_is_rejected(tmp_path):
    st.begin(tmp_path, "GO100", "a", now=OFF, pid=os.getpid())
    code, _ = st.heartbeat(tmp_path, "GO100", "b", now=_later(1))
    assert code == st.EXIT_OWNER_LOST
    assert _read(tmp_path)["heartbeat_at"] == "2026-10-20T12:00:00Z"


# ---- finish: only success is freshness evidence --------------------------------
def test_success_records_freshness_evidence(tmp_path):
    st.begin(tmp_path, "GO100", "a", now=OFF, pid=os.getpid())
    code, state = st.finish(tmp_path, "GO100", "a", "success", exit_code=0, now=_later(3))
    assert code == st.EXIT_OK and state["last_success_at"] == "2026-10-20T12:03:00Z"
    assert st.freshness_evidence_at(state) == "2026-10-20T12:03:00Z"


def test_failure_does_not_touch_success_time_and_counts(tmp_path):
    st.begin(tmp_path, "GO100", "a", now=OFF, pid=os.getpid())
    st.finish(tmp_path, "GO100", "a", "success", now=_later(3))
    st.begin(tmp_path, "GO100", "a", now=_later(60), pid=os.getpid())
    _, state = st.finish(tmp_path, "GO100", "a", "failed", reason="ingest_failed rc=8", now=_later(62))
    assert state["last_success_at"] == "2026-10-20T12:03:00Z"
    assert state["last_failure_reason"] == "ingest_failed rc=8" and state["consecutive_failures"] == 1


def test_skip_after_success_never_advances_freshness(tmp_path):
    st.begin(tmp_path, "GO100", "a", now=OFF, pid=os.getpid())
    st.finish(tmp_path, "GO100", "a", "success", now=_later(3))
    st.record_skip(tmp_path, "GO100", "already_running", now=_later(400))
    state = st.status(tmp_path, "GO100", now=_later(401))
    assert state["status"] == "skipped"
    assert state["freshness_evidence_at"] == "2026-10-20T12:03:00Z"
    assert state["last_skip_reason"] == "already_running"


def test_skip_against_live_run_leaves_owner_untouched(tmp_path):
    st.begin(tmp_path, "GO100", "a", now=OFF, pid=os.getpid())
    st.record_skip(tmp_path, "GO100", "already_running", now=_later(2))
    disk = _read(tmp_path)
    assert disk["status"] == "running" and disk["owner"] == "a" and disk["skips_total"] == 1


def test_partial_is_neither_success_nor_failure_evidence(tmp_path):
    st.begin(tmp_path, "ALL", "a", now=OFF, pid=os.getpid())
    _, state = st.finish(tmp_path, "ALL", "a", "partial", reason="skipped=GO100", now=_later(1))
    assert state["status"] == "partial" and state["last_success_at"] is None
    assert state["consecutive_failures"] == 0


def test_finish_by_non_owner_after_takeover_is_rejected(tmp_path):
    st.begin(tmp_path, "GO100", "a", now=OFF, pid=os.getpid())
    st.begin(tmp_path, "GO100", "b", now=_later(45), pid=os.getpid())
    code, _ = st.finish(tmp_path, "GO100", "a", "success", now=_later(46))
    assert code == st.EXIT_OWNER_LOST
    assert _read(tmp_path)["last_success_at"] is None


def test_invalid_outcome_rejected(tmp_path):
    with pytest.raises(ValueError):
        st.finish(tmp_path, "GO100", "a", "ok")


# ---- catch-up ------------------------------------------------------------------
def test_catchup_due_when_never_succeeded_or_old_only():
    blank = st._blank("GO100")
    assert st.catchup_due(blank, OFF, 600) is True
    assert st.catchup_due(blank, OFF, 0) is False
    recent = {**blank, "last_success_at": st.iso(_later(-30))}
    assert st.catchup_due(recent, OFF, 600) is False
    old = {**blank, "last_success_at": st.iso(_later(-700))}
    assert st.catchup_due(old, OFF, 600) is True


def test_skips_do_not_postpone_catchup(tmp_path):
    st.begin(tmp_path, "GO100", "a", now=OFF, pid=os.getpid())
    st.finish(tmp_path, "GO100", "a", "success", now=OFF)
    st.record_skip(tmp_path, "GO100", "market_window", now=_later(700))
    assert st.status(tmp_path, "GO100", now=_later(701), catchup_after_minutes=600)["catchup_due"]


def test_begin_flags_catchup(tmp_path):
    _, state = st.begin(tmp_path, "GO100", "a", now=OFF, catchup_after_minutes=600, pid=os.getpid())
    assert state["catchup"] is True


# ---- robustness / safety -------------------------------------------------------
def test_corrupt_state_file_is_reported_not_trusted(tmp_path):
    path = tmp_path / "scheduler"
    path.mkdir(parents=True)
    (path / "GO100.json").write_text("{not json")
    state = st.status(tmp_path, "GO100", now=OFF)
    assert state["status"] == "corrupt" and state["freshness_evidence_at"] is None
    code, _ = st.begin(tmp_path, "GO100", "a", now=OFF, pid=os.getpid())
    assert code == st.EXIT_OK
    assert _read(tmp_path)["status"] == "running"


def test_foreign_schema_is_treated_as_corrupt(tmp_path):
    (tmp_path / "scheduler").mkdir(parents=True)
    (tmp_path / "scheduler" / "GO100.json").write_text(json.dumps({"schema": "other", "status": "success"}))
    assert st.status(tmp_path, "GO100", now=OFF)["status"] == "corrupt"


def test_state_dir_inside_repo_is_refused():
    with pytest.raises(st.StateDirError):
        st.resolve_state_dir(str(REPO / "state"))


def test_state_file_has_no_secret_fields_and_atomic_write(tmp_path):
    st.begin(tmp_path, "GO100", "a", now=OFF, pid=os.getpid())
    keys = set(_read(tmp_path))
    assert keys == set(st._blank("GO100"))
    assert not [k for k in keys if any(w in k for w in ("token", "secret", "password", "key"))]
    assert not list((tmp_path / "scheduler").glob(".tmp-*"))


def test_reason_is_sanitised_and_bounded(tmp_path):
    st.record_skip(tmp_path, "GO100", "bad\nreason\x00" + "x" * 500, now=OFF)
    reason = _read(tmp_path)["last_skip_reason"]
    assert "\n" not in reason and "\x00" not in reason and len(reason) <= 200


def test_invalid_project_name_rejected(tmp_path):
    with pytest.raises(ValueError):
        st.begin(tmp_path, "../evil", "a", now=OFF)


# ---- CLI contract used by the shell scripts --------------------------------
def _cli(tmp_path, *args):
    return subprocess.run(
        [sys.executable, str(REPO / "scripts" / "aag_scheduler_state.py"),
         "--state-dir", str(tmp_path), *args],
        capture_output=True, text=True, timeout=30,
    )


def test_cli_exit_codes_and_status_field(tmp_path):
    off = ["--now", "2026-10-20T12:00:00Z"]
    market = ["--now", "2026-10-20T01:00:00Z"]
    assert _cli(tmp_path, *market, "begin", "GO100", "--owner", "a", "--market-guard").returncode == 7
    assert _cli(tmp_path, *off, "begin", "GO100", "--owner", "a", "--market-guard",
                "--pid", str(os.getpid())).returncode == 0
    assert _cli(tmp_path, *off, "begin", "GO100", "--owner", "b", "--market-guard",
                "--pid", str(os.getpid())).returncode == 7
    assert _cli(tmp_path, *off, "heartbeat", "GO100", "--owner", "b").returncode == 3
    assert _cli(tmp_path, *off, "finish", "GO100", "--owner", "a", "--outcome", "success").returncode == 0
    field = _cli(tmp_path, *off, "status", "GO100", "--field", "last_success_at")
    assert field.stdout.strip() == "2026-10-20T12:00:00Z"
    assert _cli(tmp_path, "--now", "2026-10-20T12:00:00Z", "market-window").returncode == 0
    assert _cli(tmp_path, *market, "market-window").returncode == 7


def test_cli_rejects_repo_state_dir_and_bad_time(tmp_path):
    repo_dir = subprocess.run(
        [sys.executable, str(REPO / "scripts" / "aag_scheduler_state.py"),
         "--state-dir", str(REPO / "x"), "status", "GO100"],
        capture_output=True, text=True, timeout=30)
    assert repo_dir.returncode == 2
    assert _cli(tmp_path, "--now", "garbage", "status", "GO100").returncode == 2
