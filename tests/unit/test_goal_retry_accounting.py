"""재작업 제안·실제 실행 분리와 계보별 상한 (acceptance T1~T16 중 DB 없이 증명되는 부분).

장부 함수·UNIQUE·동시성(T4/T5/T6/T10/T12/T14/T15)은 실제 PostgreSQL 이 필요해
tests/integration/test_goal_retry_execution_ledger_postgres.py 에서 검증한다.
"""
from __future__ import annotations

import asyncio
import importlib
import json
import re
import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from app.services import deferred_reaction_freshness as freshness
from app.services import goal_failure_retry as gfr
from app.services import next_step_proposals
from tests.unit.test_goal_failure_retry import GOAL_ID, MILESTONE_ID, FakeConn

ROOT = Path(__file__).parents[2]
RUNNER = ROOT / "scripts/pipeline-runner.sh"


@pytest.fixture
def conn():
    return FakeConn()


def _item(failed_task_id="job-1", card_id="q1", **extra):
    return {"failed_task_id": failed_task_id, "card_id": card_id, "card_kind": "auto_queue",
            "at": (datetime.now(UTC) - timedelta(hours=1)).isoformat(), **extra}


def _ensure(conn, failed_task_id="job-1"):
    return asyncio.run(gfr.ensure_retry_candidate(
        conn, milestone_id=MILESTONE_ID, goal_id=GOAL_ID,
        failed_task_id=failed_task_id, reason="linked_task_failed",
    ))


# ── T1/T3/T4/T16: 고유 후보·실제 실행·남은 한도·다음 행동 ─────────────────────────────

def test_t1_same_queue_recorded_twice_is_one_unique_candidate_with_no_execution():
    evidence = {"retry_candidates": [_item(failed=True), _item(failed=True)]}
    summary = gfr.summarize_retry_accounting(evidence, {})
    assert summary["unique_candidates"] == 1
    assert summary["executions"] == 0 and summary["remaining"] == 2
    assert summary["exhausted"] is False


def test_t3_approved_card_without_runner_job_is_not_an_execution():
    summary = gfr.summarize_retry_accounting(
        {"retry_candidates": [_item(card_id="card-1", card_kind="approval")]}, {})
    assert summary["executions"] == 0 and summary["live_candidates"] == 1
    assert "실행 없음" in gfr.format_retry_accounting(summary)


def test_t4_t6_executions_are_per_lineage_and_remaining_follows_the_busiest_lineage():
    one = gfr.summarize_retry_accounting({}, {"X": 1})
    assert (one["executions"], one["remaining"]) == (1, 1)
    two = gfr.summarize_retry_accounting({}, {"X": 2, "Y": 0})
    assert two["exhausted"] is True and two["lineages"]["Y"]["remaining"] == 2
    assert two["lineages"]["X"]["remaining"] == 0


def test_t16_user_text_has_all_four_parts_and_says_no_execution_when_zero():
    zero = gfr.format_retry_accounting(gfr.summarize_retry_accounting(
        {"retry_candidates": [_item()]}, {}))
    assert re.fullmatch(r"고유 후보 1 · 실제 실행 실행 없음 0/2 · 남은 한도 2 · 다음 행동 .+", zero)
    used = gfr.format_retry_accounting(gfr.summarize_retry_accounting({}, {"X": 2}))
    assert "실제 실행 2/2" in used and "남은 한도 0" in used and "다음 행동" in used
    assert "실행 없음" not in used


# ── T2/T12: 죽은 큐는 이력에 남고 한도를 쓰지 않는다 ─────────────────────────────────

def test_t2_skipped_stale_queue_is_history_not_execution(conn):
    conn.queues["q1"] = {"status": "skipped_stale"}
    candidates = [_item()]
    changed = asyncio.run(gfr._refresh_candidates(conn, candidates, GOAL_ID, datetime.now(UTC)))
    assert changed is True
    assert candidates[0]["failed"] is True and candidates[0]["terminal_reason"] == "skipped_stale"
    assert candidates[0]["terminal_at"]
    summary = gfr.summarize_retry_accounting({"retry_candidates": candidates}, {})
    assert summary["executions"] == 0 and summary["unique_candidates"] == 1 and summary["live_candidates"] == 0


# ── T7/T8: 유효한 승계가 있으면 다시 제출하지 않는다 ─────────────────────────────────

def _successor(status, phase, project="GO100", link="job-1"):
    return {"job_id": "runner-succ0001", "status": status, "phase": phase, "project": project,
            "instruction": f"MILESTONE_ID: {MILESTONE_ID}\nRETRY_OF_LINK: {link}\n"}


def _never_propose(monkeypatch):
    async def propose(**_kwargs):
        raise AssertionError("successor exists — must not propose again")
    monkeypatch.setattr(next_step_proposals, "propose", propose)


@pytest.mark.parametrize("status,phase,project", [
    ("running", "claude_code_work", "GO100"),
    ("queued", "queued", "GO100"),
    ("done", "done", "AADS"),  # 릴리스 인증 전 done 은 아직 진행 중
])
def test_t7_active_successor_blocks_resubmission(conn, monkeypatch, status, phase, project):
    _never_propose(monkeypatch)
    conn.successors = [_successor(status, phase, project)]
    result = _ensure(conn)
    assert result == {"created": False, "why": "successor_active", "successor": "runner-succ0001"}
    assert conn.evidence.get("retry_candidates", []) == []


def test_t8_completed_successor_blocks_resubmission_and_writes_no_exhaustion_note(conn, monkeypatch):
    _never_propose(monkeypatch)
    conn.executions = {"job-1": 2}
    conn.successors = [_successor("done", "done")]
    result = _ensure(conn)
    assert result["why"] == "successor_completed"
    assert conn.dispatch_note is None


def test_failed_or_unrelated_successor_does_not_block(conn, monkeypatch):
    async def propose(**_kwargs):
        return {"cards": [{"id": "card-1"}]}
    monkeypatch.setattr(next_step_proposals, "propose", propose)
    conn.successors = [_successor("error", "error"), _successor("running", "claude_code_work", link="job-9")]
    assert _ensure(conn)["created"] is True


def test_successor_check_uses_line_anchored_marker():
    row = _successor("running", "claude_code_work")
    row["instruction"] = "본문 설명: RETRY_OF_LINK: job-1 처럼 쓰면 된다"
    assert gfr.parse_retry_link(row["instruction"]) is None
    assert gfr.parse_retry_link("a\nRETRY_OF_LINK: job-1  \r\nb") == "job-1"


# ── 계보 뿌리 / T6 / T14 ───────────────────────────────────────────────────────────────

def test_lineage_root_follows_retry_chain_within_hop_limit(conn):
    conn.job_instructions = {
        "job-3": "RETRY_OF_LINK: job-2\n", "job-2": "RETRY_OF_LINK: job-1\n", "job-1": "원래 실패",
    }
    assert asyncio.run(gfr.lineage_root(conn, GOAL_ID, "job-3")) == "job-1"
    assert asyncio.run(gfr.lineage_root(conn, GOAL_ID, "job-unknown")) == "job-unknown"
    loop = {"a": "RETRY_OF_LINK: b\n", "b": "RETRY_OF_LINK: a\n"}
    conn.job_instructions = loop
    assert asyncio.run(gfr.lineage_root(conn, GOAL_ID, "a")) in {"a", "b"}  # 순환에서도 끝난다


def test_t6_new_failure_on_same_milestone_has_independent_budget(conn, monkeypatch):
    async def propose(**_kwargs):
        return {"cards": [{"id": "card-y"}]}
    monkeypatch.setattr(next_step_proposals, "propose", propose)
    conn.executions = {"job-x": 2}
    assert _ensure(conn, "job-x")["why"] == "retry_exhausted"
    assert _ensure(conn, "job-y")["created"] is True


def test_t14_env_cap_cannot_exceed_two(monkeypatch):
    monkeypatch.setenv("GOAL_FAILED_LINK_RETRY_MAX", "99")
    try:
        assert importlib.reload(gfr)._MAX_RETRY == 2
    finally:
        monkeypatch.delenv("GOAL_FAILED_LINK_RETRY_MAX")
        importlib.reload(gfr)


def test_proposals_do_not_consume_the_execution_limit(conn, monkeypatch):
    async def propose(**kwargs):
        return {"cards": [{"id": f"card-{len(conn.evidence.get('retry_candidates', []))}"}]}
    monkeypatch.setattr(next_step_proposals, "propose", propose)
    assert all(_ensure(conn, f"job-{i}")["created"] for i in range(4))  # 후보 4개, 실행 0
    assert gfr.summarize_retry_accounting(conn.evidence, {})["executions"] == 0


def test_auto_queue_reuse_is_recorded_on_the_candidate(conn, monkeypatch):
    async def propose(**_kwargs):
        return {"cards": [], "auto": [{"queued": True, "queue_id": "q9", "queue_reused": True}]}
    monkeypatch.setattr(next_step_proposals, "propose", propose)
    conn.queues["q9"] = {"status": "pending"}
    assert _ensure(conn)["created"] is True
    item = conn.evidence["retry_candidates"][0]
    assert item["card_kind"] == "auto_queue" and item["queue_reused"] is True
    assert item["lineage"] == "job-1"


# ── T9: 완료된 마일스톤의 소진 문구 ──────────────────────────────────────────────────────

OLD_NOTE = "재작업 후보 2회 소진 — 사람이 확인해야 한다"


def test_t9_completed_milestone_note_is_history_and_not_a_blocker():
    assert gfr.split_dispatch_note("completed", OLD_NOTE) == (None, OLD_NOTE)
    assert gfr.split_dispatch_note("blocked", OLD_NOTE) == (OLD_NOTE, None)
    assert gfr.split_dispatch_note("completed", "다른 안내") == ("다른 안내", None)
    assert gfr.is_exhaustion_note("재작업 실제 실행 2/2 소진 — 사람이 확인해야 한다")


def test_t9_read_model_neutralizes_without_touching_stored_values(conn):
    ms = [{"id": MILESTONE_ID, "status": "completed", "dispatch_note": OLD_NOTE,
           "evidence": {"retry_candidates": [_item()]}}]
    out = asyncio.run(gfr.attach_retry_accounting(_LedgerConn(), GOAL_ID, ms))
    assert out[0]["dispatch_note"] is None and out[0]["dispatch_note_history"] == OLD_NOTE
    assert out[0]["evidence"]["retry_candidates"][0]["card_id"] == "q1"
    assert out[0]["retry_accounting"]["next_action"] == "완료됨 — 조치 없음"
    assert out[0]["retry_accounting"]["exhausted"] is False


def test_read_model_reports_ledger_counts_per_milestone():
    ms = [{"id": MILESTONE_ID, "status": "blocked", "dispatch_note": None,
           "evidence": {"retry_candidates": [_item()]}}]
    out = asyncio.run(gfr.attach_retry_accounting(_LedgerConn({MILESTONE_ID: {"job-1": 2}}), GOAL_ID, ms))
    assert out[0]["retry_accounting"]["executions"] == 2
    assert out[0]["retry_accounting"]["text"].startswith("고유 후보 1 · 실제 실행 2/2 · 남은 한도 0")


def test_read_model_survives_missing_ledger_table():
    ms = [{"id": MILESTONE_ID, "status": "blocked", "dispatch_note": None, "evidence": {"retry_candidates": [_item()]}}]
    out = asyncio.run(gfr.attach_retry_accounting(_LedgerConn(fail=True), GOAL_ID, ms))
    assert out[0]["retry_accounting"]["executions"] == 0


class _Tx:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *_):
        return False


class _LedgerConn:
    def __init__(self, ledger=None, fail=False):
        self.ledger, self.fail = ledger or {}, fail

    def transaction(self):
        return _Tx()

    async def fetch(self, query, *args):
        assert "tenant_id = $1::uuid" in query  # T10: 읽기도 tenant 조건을 가진다
        if self.fail:
            raise RuntimeError('relation "goal_retry_executions" does not exist')
        return [{"milestone_id": m, "lineage": k, "used": v}
                for m, lineages in self.ledger.items() for k, v in lineages.items()]


def test_archive_moves_note_to_history_only_for_completed_milestones():
    class Archive:
        def __init__(self, status):
            self.row = {"tenant_id": GOAL_ID, "dispatch_note": OLD_NOTE,
                        "evidence": {"retry_candidates": [_item()]}} if status == "completed" else None
            self.updates = []

        async def fetchrow(self, query, *args):
            assert "status = 'completed'" in query
            return self.row

        async def execute(self, query, *args):
            self.updates.append((query, args))

    done = Archive("completed")
    assert asyncio.run(gfr.archive_exhaustion_note(done, MILESTONE_ID)) is True
    query, args = done.updates[0]
    saved = json.loads(args[1])
    assert "dispatch_note = NULL" in query and "tenant_id = $3::uuid" in query and args[2] == GOAL_ID
    assert saved["retry_history"][0]["note"] == OLD_NOTE
    assert saved["retry_candidates"][0]["card_id"] == "q1"  # 후보·실패 증거는 그대로
    assert asyncio.run(gfr.archive_exhaustion_note(Archive("blocked"), MILESTONE_ID)) is False


# ── T13: 시작 전 거절은 재작업 실패와 구분 ────────────────────────────────────────────────

def test_t13_submit_rejection_is_not_counted_as_a_rework_failure():
    assert gfr.classify_pre_start_failure(
        charged=False, started=False, error_text="범위 파서 거절: TARGET_FILES 충돌") == "submit_rejected"
    assert gfr.classify_pre_start_failure(charged=False, started=False, error_text="other") == "not_started"
    assert gfr.classify_pre_start_failure(charged=False, started=True, error_text="x") == "observed_start_uncharged"
    assert gfr.classify_pre_start_failure(charged=True, started=True, error_text="범위") == "execution_failed"


# ── T11: stale 가드와 재작업 본문 ─────────────────────────────────────────────────────────

RETRY_BODY = (
    "[다음단계 1] [재작업 runner-0a1b2c3d] 마일스톤 — linked_task_failed\n"
    "실패한 job id: runner-0a1b2c3d\n사유: linked_task_failed\n"
    f"GOAL_ID: {GOAL_ID}\nMILESTONE_ID: {MILESTONE_ID}\n"
    "RETRY_OF_LINK: runner-0a1b2c3d\nREMEDIATION_PURPOSE: failed_link_retry\n"
)


class _JobsConn:
    def __init__(self, statuses):
        self.statuses = statuses

    async def fetch(self, query, ids):
        return [{"job_id": j, "status": self.statuses[j], "updated_at": datetime.now(UTC)}
                for j in ids if j in self.statuses]


def test_t11_failed_job_id_inside_rework_body_is_not_a_stale_target():
    message, reason = asyncio.run(freshness.annotate_or_skip(_JobsConn({"runner-0a1b2c3d": "error"}), RETRY_BODY))
    assert reason is None and message == RETRY_BODY


def test_t11_guard_still_skips_normal_stale_bodies():
    conn = _JobsConn({"runner-0a1b2c3d": "error"})
    _, reason = asyncio.run(freshness.annotate_or_skip(conn, "승인하세요: runner-0a1b2c3d 를 계속"))
    assert reason and reason.startswith("stale_jobs")
    # 표식 한 줄만으로는 면제되지 않는다 — 두 줄이 모두 줄 단위로 있어야 한다
    only_link = "RETRY_OF_LINK: runner-0a1b2c3d\n"
    assert asyncio.run(freshness.annotate_or_skip(conn, only_link))[1].startswith("stale_jobs")
    inline = "설명 REMEDIATION_PURPOSE: failed_link_retry 와 RETRY_OF_LINK: runner-0a1b2c3d"
    assert asyncio.run(freshness.annotate_or_skip(conn, inline))[1].startswith("stale_jobs")


def test_t11_other_terminal_job_in_a_rework_body_is_still_checked():
    body = RETRY_BODY + "참고: runner-99999999 도 확인\n"
    conn = _JobsConn({"runner-0a1b2c3d": "error", "runner-99999999": "done"})
    assert asyncio.run(freshness.annotate_or_skip(conn, body))[1].startswith("stale_jobs: runner-99999999=done")


# ── 러너: 모델 시작 직전 차감 훅 ─────────────────────────────────────────────────────────

def _helper_source() -> str:
    text = RUNNER.read_text(encoding="utf-8")
    start = text.index("retry_execution_charge_or_abort() {")
    end = text.index("\n}\n", start) + 3
    return text[start:end]


def _run_helper(instruction, db_output, tmp_path):
    log_file = tmp_path / "calls.log"
    log_file.write_text("")
    script = f"""
set -uo pipefail
log() {{ :; }}
db_exec() {{ echo exec >> {log_file}; printf '%s' $'{db_output.replace(chr(0x1e), chr(92) + 'x1e')}'; }}
db_update() {{ echo update >> {log_file}; }}
post_to_chat() {{ echo chat >> {log_file}; }}
record_runner_event() {{ echo event >> {log_file}; }}
_abort_job_terminal_before_work() {{ echo abort >> {log_file}; }}
{_helper_source()}
retry_execution_charge_or_abort runner-job00001 sess proj grp {json.dumps(instruction)} codex:x
echo "rc=$?"
"""
    out = subprocess.run(["bash", "-c", script], capture_output=True, text=True, timeout=20)
    rc = out.stdout.strip().splitlines()[-1]
    calls = ";".join(log_file.read_text().split())
    return f"{rc} calls={calls}", out.stderr


def test_runner_hook_skips_non_retry_jobs_without_touching_the_ledger(tmp_path):
    line, _ = _run_helper("일반 작업\nGOAL_ID: g\n", "", tmp_path)
    assert line == "rc=0 calls="
    line, _ = _run_helper("본문 중간 RETRY_OF_LINK: runner-aaaaaaa1 언급", "", tmp_path)
    assert line == "rc=0 calls="


@pytest.mark.parametrize("outcome", ["charged", "already_charged", "not_retry"])
def test_runner_hook_lets_charged_jobs_start(outcome, tmp_path):
    line, err = _run_helper("RETRY_OF_LINK: runner-aaaaaaa1\n", f"{outcome}\x1e\x1erunner-aaaaaaa1", tmp_path)
    assert line.startswith("rc=0 calls=exec"), err
    assert "abort" not in line and "update" not in line


@pytest.mark.parametrize("db_output", [
    "cap_exceeded\x1e2\x1erunner-aaaaaaa1", "scope_mismatch\x1e\x1e", "tenant_missing\x1e\x1e",
    "milestone_missing\x1e\x1e", "job_not_found\x1e\x1e", "",
])
def test_runner_hook_fails_closed_before_any_model_launch(db_output, tmp_path):
    line, err = _run_helper("RETRY_OF_LINK: runner-aaaaaaa1\n", db_output, tmp_path)
    assert line == "rc=1 calls=exec;update;chat;abort", err


def test_runner_hook_precedes_every_model_launch_and_is_not_in_start_or_claim():
    text = RUNNER.read_text(encoding="utf-8")
    hook = "retry_execution_charge_or_abort \"$job_id\""
    positions = [m.start() for m in re.finditer(re.escape(hook), text)]
    assert len(positions) == 3
    launches = {
        "codex": text.index('timeout "$MAX_RUNTIME" codex "${codex_args[@]}"'),
        "litellm": text.index("litellm_runner.py \\\n                    --model"),
        "claude": text.index('"$RUNNER_CLAUDE_CLI_BIN" "${claude_args[@]}"'),
    }
    for hook_at, launch in zip(positions, sorted(launches.values())):
        assert hook_at < launch
        assert launch - hook_at < 6000  # 가드·continue 이후 실제 launch 바로 앞이다
    # 시작 전이/claim/모델 시도 알림 어디에서도 차감하지 않는다
    for anchor in ("mark_job_started_or_abort() {", "guard_claimed_before_worktree() {"):
        body = text[text.index(anchor):text.index("\n}\n", text.index(anchor))]
        assert "retry_execution_charge" not in body
    assert positions[0] > text.index('record_runner_event "$job_id" "model_attempt_started"')
    assert positions[2] > text.index('log "MODEL_CONTRACT_REJECTED')
