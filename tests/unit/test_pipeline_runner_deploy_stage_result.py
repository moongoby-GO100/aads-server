"""DEPLOY_ONLY 종결 결과에 러너 배포 단계(deploy_runs) 기록이 덧붙는지 고정한다.

2026-10-22 runner-f4571e8b: result_output 은 Claude Code 단계의 "릴리스 미실행" 이었는데
러너 배포 단계는 deploy_runs #5843 을 실제로 수행했다. deploy_runs.runner_job_id 가 NULL 이라
연결도 안 됐고, 종결 검토 알림이 result_output 을 인용해 CEO 세션이 "미실행"으로 읽었다.
"""
from __future__ import annotations

import os
import re
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

os.environ.setdefault("JWT_SECRET_KEY", "unit-test-secret")
os.environ.setdefault("E2B_API_KEY", "unit-test-e2b-key")

from app.api import pipeline_runner as pr  # noqa: E402

ROOT = Path(__file__).resolve().parents[2]
JOB = "runner-f4571e8b"
SHA = "b8120d1b375c" + "0" * 28
CLAUDE_TEXT = "## 결과: 릴리스 미실행 (사전 확인만 완료)"
T0 = datetime(2026, 10, 22, 7, 20, tzinfo=timezone.utc)
DEPLOY_ONLY_INSTRUCTION = "DEPLOY_ONLY: true\nDEPLOY_POLICY: bluegreen\n배포만 수행"


def _run_row(**over):
    row = {
        "id": 5843, "release_sha": "b8120d1b375c", "status": "success_partial",
        "phase": "standby_sync", "current_slot": "8000", "candidate_slot": "8001",
        "error_summary": None,
    }
    row.update(over)
    return row


class _World:
    def __init__(self, *, instruction=DEPLOY_ONLY_INSTRUCTION, by_job=None, by_window=None):
        self.instruction = instruction
        self.by_job = by_job
        self.by_window = by_window or []
        self.result_output = CLAUDE_TEXT
        self.window_args = None
        self.queries: list[str] = []
        self.fail = False

    def pool(self):
        world = self

        class _Conn:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *_a):
                return False

            async def fetchrow(self, query, *args):
                world.queries.append(query)
                if world.fail:
                    raise RuntimeError("db down")
                if "FROM pipeline_jobs WHERE job_id" in query:
                    return {
                        "project": "AADS", "instruction": world.instruction,
                        "window_start": T0, "window_end": T0 + timedelta(minutes=20),
                    }
                if "FROM deploy_runs" in query and "runner_job_id = $2" in query:
                    return world.by_job
                raise AssertionError(query)

            async def fetch(self, query, *args):
                world.queries.append(query)
                if "runner_job_id IS NULL" in query:
                    world.window_args = args
                    return world.by_window
                raise AssertionError(query)

            async def execute(self, query, *args):
                world.queries.append(query)
                if "UPDATE pipeline_jobs SET result_output" in query:
                    title = args[2]
                    if title not in world.result_output:
                        world.result_output += args[1]
                    return "UPDATE 1"
                raise AssertionError(query)

        class _Pool:
            def acquire(self):
                return _Conn()

        return _Pool()


@pytest.mark.asyncio
async def test_runner_job_id_match_appends_deploy_run_summary():
    world = _World(by_job=_run_row())

    section, persist = await pr._deploy_stage_section_for_job(world.pool(), JOB, commit_sha=SHA)

    assert persist is True
    assert section.startswith("## 배포 단계 결과(러너 기록)")
    assert "deploy_runs #5843 (runner_job_id 일치)" in section
    assert "b8120d1b375c" in section
    assert "success_partial" in section and "standby_sync" in section
    assert "8000 → 8001" in section
    assert not any("runner_job_id IS NULL" in q for q in world.queries)

    await pr._append_deploy_stage_section(world.pool(), JOB, section)
    await pr._append_deploy_stage_section(world.pool(), JOB, section)
    assert world.result_output.startswith(CLAUDE_TEXT)
    assert world.result_output.count("## 배포 단계 결과(러너 기록)") == 1


@pytest.mark.asyncio
async def test_null_runner_job_id_falls_back_to_release_sha_and_time_window():
    world = _World(by_job=None, by_window=[_run_row(), _run_row(id=5840)])

    section, persist = await pr._deploy_stage_section_for_job(world.pool(), JOB, commit_sha=SHA)

    assert persist is True
    assert "deploy_runs #5843" in section
    assert "release_sha+시간창 매칭" in section
    assert "후보 2건 중 최신" in section
    assert "연결 실패" not in section
    project, sha, start, end, grace = world.window_args
    assert (project, sha, start) == ("AADS", SHA, T0)
    assert end == T0 + timedelta(minutes=20)
    assert grace == pr._DEPLOY_RUN_MATCH_GRACE_SECONDS
    window_query = next(q for q in world.queries if "runner_job_id IS NULL" in q)
    assert "$2 LIKE release_sha || '%'" in window_query


@pytest.mark.asyncio
@pytest.mark.parametrize("commit_sha", [SHA, None])
async def test_match_failure_is_explicit_and_never_reads_as_success(commit_sha):
    world = _World(by_job=None, by_window=[])

    section, persist = await pr._deploy_stage_section_for_job(world.pool(), JOB, commit_sha=commit_sha)

    assert persist is True
    assert "배포 기록 연결 실패" in section
    assert "deploy_runs #" not in section
    assert not re.search(r"성공|success|배포완료", section)


@pytest.mark.asyncio
async def test_lookup_error_is_reported_but_not_persisted():
    world = _World()
    world.fail = True

    section, persist = await pr._deploy_stage_section_for_job(world.pool(), JOB, commit_sha=SHA)

    assert persist is False
    assert "배포 기록 연결 실패" in section and "RuntimeError" in section


@pytest.mark.asyncio
async def test_non_deploy_only_job_gets_nothing():
    world = _World(instruction="일반 코드 작업\n본문에 DEPLOY_ONLY: true 언급")

    section, persist = await pr._deploy_stage_section_for_job(world.pool(), JOB, commit_sha=SHA)

    assert (section, persist) == (None, False)
    assert not any("deploy_runs" in q for q in world.queries)


def test_followup_message_shows_deploy_section_beyond_300_char_preview():
    section = pr._format_deploy_stage_section(_run_row(), linked_by="runner_job_id")
    row = {
        "job_id": JOB, "project": "AADS", "status": "done",
        "output_preview": CLAUDE_TEXT, "instruction_preview": "DEPLOY_ONLY: true",
    }

    msg = pr._terminal_followup_message(row, kind="completed", commit_sha=SHA, deploy_section=section)
    failed = pr._terminal_followup_message(
        {**row, "status": "error"}, kind="failed", commit_sha=SHA, deploy_section=section,
    )
    plain = pr._terminal_followup_message(row, kind="completed", commit_sha=SHA)

    assert CLAUDE_TEXT in msg and "deploy_runs #5843" in msg
    assert msg.index(CLAUDE_TEXT) < msg.index("## 배포 단계 결과(러너 기록)")
    assert "deploy_runs #5843" in failed
    assert "배포 단계 결과" not in plain
    assert "AI 검수 대기" not in msg


# ── 배포 시 runner_job_id 채우기 (셸 계약) ─────────────────────────────


def _deploy_sh() -> str:
    return (ROOT / "deploy.sh").read_text(encoding="utf-8")


def _extract_function(script: str, name: str) -> str:
    start = script.index(f"{name}() {{")
    return script[start:script.index("\n}\n", start) + 3]


def test_deploy_sh_inserts_runner_job_id_guarded_by_unique_index():
    script = _deploy_sh()
    fn = _extract_function(script, "deploy_runner_job_id_sql")

    assert "NOT EXISTS (SELECT 1 FROM deploy_runs WHERE project='AADS' AND runner_job_id=" in fn
    init = _extract_function(script, "deploy_observe_init")
    assert "runner_job_id" in init and "${runner_job_expr}" in init
    assert script.count("${runner_job_expr}") == 2


@pytest.mark.parametrize("raw,expect_null", [
    ("runner-f4571e8b", False), ("", True), ("x'; DROP TABLE deploy_runs;--", True),
])
def test_deploy_runner_job_id_sql_rejects_unsafe_ids(raw, expect_null):
    fn = _extract_function(_deploy_sh(), "deploy_runner_job_id_sql")
    out = subprocess.run(
        ["bash", "-c", fn + "\ndeploy_runner_job_id_sql"],
        env={**os.environ, "AADS_RUNNER_JOB_ID": raw}, capture_output=True, text=True, check=True,
    ).stdout.strip()

    assert (out == "NULL") is expect_null
    if not expect_null:
        assert "'runner-f4571e8b'" in out


@pytest.mark.parametrize("name", ["pipeline-runner.sh", "pipeline-runner.sh.local"])
def test_runner_passes_job_id_to_bluegreen_deploy(name):
    script = (ROOT / "scripts" / name).read_text(encoding="utf-8")
    call = script.index('bash "$worktree_dir/deploy.sh" bluegreen >"$_aads_deploy_log"')

    assert 'AADS_RUNNER_JOB_ID="$job_id"' in script[call - 400:call]
