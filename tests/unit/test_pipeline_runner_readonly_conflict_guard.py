"""AADS-RUNNER-READONLY-RESULT-RELAY-CONFLICT-GUARD-20261022.

(A) READ-ONLY 작업의 diff 0건은 정상이다. runner-f3303c56 은 `MODE:` 줄이 없다는 이유로
    cancelled/no_changes 로 종결돼 판정 보고가 채팅에 전달되지 않았다.
(B) 동일 파일 충돌 가드가 "중복 수정 금지" 문맥에만 등장한 경로와, TARGET 저장소가 다른 경로를
    수정 대상으로 읽어 runner-c4a7b961 이 무관한 runner-cb5f65e2 를 기다렸다.
"""
import asyncio
import os
import shutil
import subprocess
from pathlib import Path

import pytest

os.environ.setdefault("JWT_SECRET_KEY", "unit-test-secret")
os.environ.setdefault("E2B_API_KEY", "unit-test-e2b-key")

from app.api.pipeline_runner import (  # noqa: E402
    _actual_scope_files,
    _extract_target_files,
    _find_active_file_conflict,
    _parse_write_scope,
)
from app.services.pipeline_runner_service import _is_read_only_instruction  # noqa: E402
from tests.unit.test_pipeline_runner_nochanges_guard_r5 import (  # noqa: E402
    HARNESS_TAIL,
    REAL_STUBS,
    _extract_diff_contract_block,
    _extract_no_changes_block,
    _init_repo,
)

ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = ("pipeline-runner.sh", "pipeline-runner.sh.local")
PR_PY = "app/api/pipeline_runner.py"
SERVER_PR = "server:app/api/pipeline_runner.py"


def _read_script(name: str = "pipeline-runner.sh") -> str:
    return (ROOT / "scripts" / name).read_text(encoding="utf-8")


# ── (A) READ-ONLY 판정: 쉘과 파이썬이 같은 규칙 ──────────────────────────────

READ_ONLY_CASES = [
    ("MODE: READ_ONLY\nTASK_ID: X\n본문", True),
    ("TASK_ID: X\n   mode :  read-only   \n본문", True),
    ("MODE: READONLY\n본문", True),
    ("[READ-ONLY] 러너 판정 검증\nTASK_ID: X\n본문", True),
    ("\n\nREAD-ONLY 검증 작업\n본문", True),
    ("TASK_ID: X\nTITLE: READ-ONLY 러너 검증\n본문", True),
    ("TASK_ID: X\n제목: READ_ONLY 점검\n본문", True),
    ("TASK_ID: X\nTITLE: [READ-ONLY] 러너 판정 검증\n본문", True),
    ("TASK_ID: X\n유형: READ_ONLY\n본문", True),
    ("TASK_ID: X\n**TITLE:** read-only 검증\n본문", True),
    # 머리표 값 중간의 토큰은 읽기전용이 아니다 (읽기전용을 주제로 다루는 쓰기 작업)
    ("TASK_ID: X\nTITLE: 러너 2건 교정 — READ-ONLY 작업 결과가 묻히는 문제\n본문", False),
    ("TASK_ID: X\n제목: 읽기 READ_ONLY 점검\n본문", False),
    ("TASK_ID: X\n유형: 구현 (READ_ONLY 판정 수정)\n본문", False),
    ("TASK_ID: X\nTITLE: READ_ONLY_FILES 처리 수정\n본문", False),
    ("TASK_ID: X\nTITLE: READ_ONLY_PLUS 검증\n본문", False),
    # 본문 자연어·합성어·범위 머리표·20줄 밖은 읽기전용이 아니다
    ("TASK_ID: X\nTITLE: 새 파일 추가\n\n기존 파일은 수정하지 말고 새 파일만.\nread-only 로 두라, do not modify", False),
    ("TASK_ID: X\nREAD_ONLY_FILES: app/a.py\n본문", False),
    ("READ_ONLY_FILES: app/a.py\n본문", False),
    ("MODE: READ_ONLY_PLUS\n본문", False),
    ("TASK_ID: X\n이 작업은 MODE: READ_ONLY 가 아니다", False),
    ("TASK_ID: X\n" + "\n".join(f"line {i}" for i in range(40)) + "\nMODE: READ_ONLY\n", False),
    ("TASK_ID: X\n" + "\n".join(f"line {i}" for i in range(25)) + "\nTITLE: READ-ONLY\n", False),
    ("이 작업은 MODE: READ_ONLY 가 아니다", False),
    ("READ_ONLY_FILES: app/a.py", False),
    ("", False),
]


@pytest.mark.parametrize("text,expected", READ_ONLY_CASES)
def test_python_read_only_marker_rules(text, expected):
    assert _is_read_only_instruction(text) is expected


@pytest.fixture(scope="module")
def ro_fn(tmp_path_factory):
    if shutil.which("bash") is None:
        pytest.skip("bash 미설치 환경")
    script = _read_script()
    start = script.index("_RO_TOKEN_ERE=")
    end = script.index("\n}\n", script.index("is_read_only_instruction() {")) + len("\n}\n")
    fn_file = tmp_path_factory.mktemp("ro_fn") / "fn.sh"
    fn_file.write_text("set -eo pipefail\n" + script[start:end], encoding="utf-8")
    return fn_file


@pytest.mark.parametrize("text,expected", READ_ONLY_CASES)
def test_shell_read_only_marker_rules_match_python(ro_fn, text, expected):
    proc = subprocess.run(
        ["bash", "-c", f'source "{ro_fn}"; is_read_only_instruction "$INSTR"'],
        capture_output=True, text=True, env={"PATH": "/usr/bin:/bin", "INSTR": text},
    )
    assert (proc.returncode == 0) is expected, proc.stderr


def test_scripts_identical_and_python_mirror_noted():
    assert _read_script("pipeline-runner.sh") == _read_script("pipeline-runner.sh.local")
    assert "_is_read_only_instruction 과 같은 규칙" in _read_script()


# ── (A) diff 0건 종결 동작 (실제 no_changes 블록 실행) ───────────────────────


@pytest.fixture()
def harness(tmp_path):
    if not (shutil.which("bash") and shutil.which("git")):
        pytest.skip("bash/git 미설치 환경")
    script = _read_script()
    start = script.index("_RO_TOKEN_ERE=")
    end = script.index("\n}\n", script.index("is_read_only_instruction() {")) + len("\n}\n")
    ro_functions = script[start:end]
    stubs = REAL_STUBS.format(
        log_file=str(tmp_path / "runner.log"), db_log=str(tmp_path / "db.log"),
        event_log=str(tmp_path / "events.log"), chat_log=str(tmp_path / "chat.log"),
    )
    stubs = "\n".join(
        ln for ln in stubs.splitlines() if not ln.startswith("is_read_only_instruction()")
    )
    for name in ("runner.log", "db.log", "events.log", "chat.log"):
        (tmp_path / name).write_text("", encoding="utf-8")
    fn_file = tmp_path / "harness.sh"
    fn_file.write_text(
        "set -eo pipefail\n"
        + _extract_diff_contract_block(script)
        + stubs + "\n" + ro_functions
        + "is_deploy_only_instruction() { return 1; }\n"
        + "runner_handover_record() { :; }\n_reap_job_temp_pg() { :; }\n_reap_job_test_containers() { :; }\n"
        + (HARNESS_TAIL % _extract_no_changes_block(script)),
        encoding="utf-8",
    )
    return fn_file, tmp_path


def _run(harness, instruction, output_text="판정: PASS — 검증 보고 전문"):
    fn_file, tmp_path = harness
    repo = tmp_path / "wt"
    pre_sha = _init_repo(repo)
    env = {
        "PATH": "/usr/bin:/bin:/usr/local/bin",
        "INSTR": instruction, "WORKDIR": str(repo), "PRE_SHA": pre_sha,
        "OUTPUT_FILE": str(tmp_path / "out.txt"),
        "NO_CHANGES_RECHECK_MAX_ATTEMPTS": "1", "NO_CHANGES_RECHECK_SLEEP_SECONDS": "0",
    }
    proc = subprocess.run(["bash", str(fn_file)], capture_output=True, text=True, env=env)
    db = (tmp_path / "db.log").read_text(encoding="utf-8")
    chat = (tmp_path / "chat.log").read_text(encoding="utf-8")
    return proc, db, chat


@pytest.mark.parametrize(
    "instruction",
    ["[READ-ONLY] 러너 검증\nTASK_ID: X", "TASK_ID: X\nTITLE: READ-ONLY 검증", "MODE: READ_ONLY\nTASK_ID: X"],
)
def test_read_only_zero_diff_is_done_and_result_relayed(harness, instruction):
    proc, db, chat = _run(harness, instruction)
    assert proc.returncode == 0, proc.stderr
    assert "status='done'" in db and "status='cancelled'" not in db
    assert "result_output=" in db
    assert "read-only 작업 완료" in chat and "stub output" in chat
    assert "변경사항 0건으로 작업 종결" not in chat


def test_modifying_job_zero_diff_keeps_cancelled_no_changes(harness):
    proc, db, chat = _run(harness, "TASK_ID: X\nTITLE: 일반 수정 작업\n기존 파일은 수정하지 말 것")
    assert proc.returncode == 1, proc.stderr
    assert "status='cancelled'" in db and "phase='no_changes'" in db
    assert "변경사항 0건으로 작업 종결" in chat


# ── (B) 충돌 가드: 파일 수집 근거 ────────────────────────────────────────────


def test_prohibition_only_path_is_not_a_write():
    text = (
        "TARGET: /root/aads/aads-server\n"
        f"scripts/pipeline-runner.sh 를 고친다. {PR_PY} 는 중복 수정 금지.\n"
    )
    files = _extract_target_files(text)
    assert "server:scripts/pipeline-runner.sh" in files
    assert SERVER_PR not in files


@pytest.mark.parametrize(
    "sentence",
    [
        f"{PR_PY} 는 수정 금지",
        f"{PR_PY} 는 건드리지 마라",
        f"{PR_PY} 중복 수정 금지",
        f"({PR_PY} 는 중복 수정 금지)",
        f"{PR_PY} 건드리지 않는다, 대신 다른 곳을 고친다",
        f"수정 금지 파일: {PR_PY}",
        f"금지: {PR_PY}, app/main.py",
        f"Do not modify {PR_PY}",
    ],
)
def test_prohibition_contexts_do_not_create_conflict_scope(sentence):
    assert _extract_target_files(sentence + "\n") == set()


@pytest.mark.parametrize(
    "sentence",
    [
        f"{PR_PY} 삭제 금지, 로직 수정한다",
        f"{PR_PY} 를 고친다. 다른 파일 수정 금지",
        f"건드리지 않고 {PR_PY} 를 수정한다",
        f"{PR_PY} 의 금지 목록에 항목을 추가한다",
    ],
)
def test_real_modification_is_still_a_write(sentence):
    assert _extract_target_files(sentence + "\n") == {SERVER_PR}


def test_path_in_both_prohibition_and_real_context_stays_a_write():
    text = f"{PR_PY} 는 중복 수정 금지.\n그러나 {PR_PY} 의 import 한 줄은 고친다.\n"
    assert _extract_target_files(text) == {SERVER_PR}


def test_other_repo_bare_path_is_excluded_when_target_declared():
    """runner-cb5f65e2 모양: TARGET 이 dashboard 인데 본문에 서버 경로가 맨 경로로 등장."""
    text = (
        "TARGET: /root/aads/aads-dashboard\n"
        "src/components/chat/ChatArtifactPanel.tsx 를 고친다.\n"
        f"참고로 {PR_PY} 의 응답 형식을 따른다.\n"
    )
    scope = _parse_write_scope(text)
    assert set(scope.write) == {"dashboard:src/components/chat/ChatArtifactPanel.tsx"}
    assert ("server:app/api/pipeline_runner.py", "other_repo") in scope.skipped
    assert "타 저장소 1건 제외" in scope.basis


def test_explicit_other_repo_path_is_kept():
    text = (
        "TARGET: /root/aads/aads-dashboard\n"
        f"/root/aads/aads-server/{PR_PY} 와 src/app/page.tsx 를 같이 고친다.\n"
    )
    assert _extract_target_files(text) == {SERVER_PR, "dashboard:src/app/page.tsx"}


def test_no_target_declared_keeps_legacy_default_repo():
    assert _extract_target_files(f"{PR_PY} 를 고친다.\n") == {SERVER_PR}
    both = "TARGET: /root/aads/aads-server + /root/aads/aads-dashboard\nsrc/app/page.tsx 와 app/main.py 수정\n"
    assert _extract_target_files(both) == {"dashboard:src/app/page.tsx", "server:app/main.py"}


def test_declared_target_files_take_priority_over_prose():
    text = (
        "TARGET: /root/aads/aads-server\n"
        "TARGET_FILES: scripts/pipeline-runner.sh\n"
        f"{PR_PY} 를 고친다.\n"
    )
    scope = _parse_write_scope(text)
    assert scope.explicit and "server:scripts/pipeline-runner.sh" in scope.write
    assert SERVER_PR not in scope.write
    assert scope.basis.startswith("TARGET_FILES 선언")


def test_actual_changed_files_are_repo_tagged_only_when_target_known():
    dash = "TARGET: /root/aads/aads-dashboard\n본문"
    assert _actual_scope_files(dash, ["src/a.tsx", " "]) == {"dashboard:src/a.tsx"}
    assert _actual_scope_files(dash, '["src/a.tsx"]') == {"dashboard:src/a.tsx"}
    assert _actual_scope_files(dash, []) == set()
    assert _actual_scope_files("본문", ["src/a.tsx"]) == set()


# ── (B) _find_active_file_conflict 통합 ─────────────────────────────────────


class _Conn:
    def __init__(self, rows):
        self.rows = rows

    async def fetchrow(self, *_a):
        return None

    async def fetch(self, query, *_a):
        return self.rows if "FROM pipeline_jobs" in query else []


def _row(instruction, actual=None, job_id="runner-active"):
    return {
        "job_id": job_id, "instruction": instruction, "status": "running",
        "phase": "executing", "actual_changed_files": actual or [],
    }


def _conflict(incoming, rows):
    scope = _parse_write_scope(incoming)
    return asyncio.run(_find_active_file_conflict(
        _Conn(rows), project="AADS", target_files=set(scope.write),
        tenant_id="t", incoming_instruction=incoming, incoming_basis=scope.basis,
    ))


INCOMING_SERVER = f"TARGET: /root/aads/aads-server\n{PR_PY} 의 라우트를 고친다.\n"
CB5F_LIKE = (
    "TARGET: /root/aads/aads-dashboard\n"
    "src/components/chat/ChatArtifactPanel.tsx 를 고친다.\n"
    f"{PR_PY} 는 중복 수정 금지.\n"
)


def test_runner_c4a7b961_vs_cb5f65e2_shape_is_not_a_conflict():
    assert _conflict(INCOMING_SERVER, [_row(CB5F_LIKE)]) is None


def test_other_target_repo_mention_is_not_a_conflict_even_without_prohibition_words():
    existing = (
        "TARGET: /root/aads/aads-dashboard\n"
        f"src/app/page.tsx 를 고친다. 서버 {PR_PY} 의 응답을 읽는다.\n"
    )
    assert _conflict(INCOMING_SERVER, [_row(existing)]) is None


def test_real_same_file_conflict_is_kept_with_basis():
    existing = f"TARGET: /root/aads/aads-server\n{PR_PY} 에 엔드포인트를 추가한다.\n"
    conflict = _conflict(INCOMING_SERVER, [_row(existing)])
    assert conflict and conflict["job_id"] == "runner-active"
    assert conflict["overlap"] == [SERVER_PR]
    assert "본문 경로 추정" in conflict["basis"] and "runner-active" in conflict["basis"]


def test_actual_worktree_files_win_over_prose_for_existing_job():
    existing = f"TARGET: /root/aads/aads-server\n{PR_PY} 를 고친다.\n"
    # 선언은 pipeline_runner.py 지만 실제로는 다른 파일만 바뀐 작업 → 충돌 아님
    assert _conflict(INCOMING_SERVER, [_row(existing, actual=["app/main.py"])]) is None
    # 실제 변경 파일에 포함되면 선언이 없어도 충돌
    only_prose = "TARGET: /root/aads/aads-server\n다른 작업\n"
    hit = _conflict(INCOMING_SERVER, [_row(only_prose, actual=[PR_PY])])
    assert hit and "실제 worktree 변경 파일" in hit["basis"]
