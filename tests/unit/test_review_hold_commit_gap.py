"""review_hold 산출물의 commit_hash 누락 회귀 방지."""

import shutil
import subprocess
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[2]

SHARED_BLOCK_BEGIN = "# ─── SHARED-BLOCK BEGIN: job_diff_contract"
SHARED_BLOCK_END = "# ─── SHARED-BLOCK END: job_diff_contract"


def _read(name: str) -> str:
    return (ROOT / "scripts" / name).read_text(encoding="utf-8")


def _shared_block(name: str) -> str:
    script = _read(name)
    start = script.index(SHARED_BLOCK_BEGIN)
    end = script.index(SHARED_BLOCK_END)
    return script[start:end]


def _sweeper_helpers() -> str:
    """스위퍼의 공유 블록 + review_hold 복구 헬퍼 전부 (최상단 부작용 코드 제외)."""
    script = _read("review-hold-sweeper.sh")
    start = script.index(SHARED_BLOCK_BEGIN)
    end = script.index("# 재시도 추적 컬럼")
    return script[start:end]


def test_runner_commits_before_review_and_keeps_hash_on_hold():
    script = _read("pipeline-runner.sh")
    commit = script.index(
        'approval_commit_sha=$(commit_job_worktree_for_approval "$job_id"'
    )
    review = script.index('local review_verdict="APPROVE"', commit)
    hold = script.index('if [[ "$review_verdict" != "APPROVE" ]]', review)
    hold_update_end = script.index('record_runner_event "$job_id" "job_terminal"', hold)

    assert commit < review < hold
    assert "commit_hash=NULL" not in script[hold:hold_update_end].replace(" ", "")
    assert "SET commit_hash=NULL" not in script[hold:hold_update_end]


def test_runner_no_changes_exits_before_commit_without_empty_commit():
    script = _read("pipeline-runner.sh")
    no_changes = script.index('NO_CHANGES job=$job_id')
    no_changes_return = script.index("return 1", no_changes)
    commit = script.index(
        'approval_commit_sha=$(commit_job_worktree_for_approval "$job_id"'
    )

    assert no_changes < no_changes_return < commit
    assert "--allow-empty" not in script


def test_sweeper_recovers_commit_before_promotion_and_fails_closed_without_artifact():
    script = _read("review-hold-sweeper.sh")
    approve = script.index('if [[ "$verdict" == "APPROVE" ]]')
    recover = script.index('ensure_review_hold_commit "$job_id"', approve)
    promote = script.index("SET status='awaiting_approval'", recover)
    helper = script[script.index("ensure_review_hold_commit() {"):script.index(
        "# 재시도 추적 컬럼"
    )]

    assert approve < recover < promote
    assert "REVIEW_HOLD_NO_ARTIFACT" in helper
    assert "commit_hash='${current_sha}'" in helper
    assert "--no-verify" not in helper
    assert "--allow-empty" not in helper


def test_sweeper_does_not_create_new_commits_for_dirty_worktrees():
    """AADS-SWEEPER-COMMITHASH-P0: 검수받지 않은 변경이 승인 큐로 새지 않도록,
    스위퍼는 워크트리에 새 커밋을 만들지 않고 기존 HEAD 만 읽는다."""
    script = _read("review-hold-sweeper.sh")
    helper = script[script.index("ensure_review_hold_commit() {"):script.index(
        "# 재시도 추적 컬럼"
    )]

    assert 'git -C "$worktree_dir" commit -m' not in helper
    assert 'git -C "$worktree_dir" add -A' not in helper
    assert "REVIEW_HOLD_DIRTY" in helper
    assert "status --porcelain" in helper
    assert "review_feedback=COALESCE(review_feedback,'')" in helper


def test_no_hardcoded_job_id_recovery_path():
    """AADS-SWEEPER-COMMITHASH-P0 금지: 이미 NULL 로 올라간 기존 행을 특정
    job_id 하드코딩으로 임의 UPDATE 하는 경로를 다시 두지 않는다."""
    script = _read("review-hold-sweeper.sh")

    assert "repair_known_commit_gap_jobs" not in script
    assert "runner-061e59d7" not in script
    assert "runner-2136ce20" not in script


# ── AADS-REVIEWHOLD-DIRTY-STRAND-P0: 공유 계약 / 순환 의존 ────────────────


def test_diff_contract_block_is_byte_identical_in_both_scripts():
    """스위퍼가 '러너와 동일하게' 정규화한다는 계약을 글자로 고정한다.

    한쪽만 고치면 멀쩡한 산출물이 diff drift 로 폐기되거나(러너만 바뀜),
    검수받지 않은 변경이 일치로 통과한다(스위퍼만 바뀜). 둘 다 조용한 사고라
    비교 규칙이 갈라지는 순간 여기서 막는다.
    """
    runner_block = _shared_block("pipeline-runner.sh")
    sweeper_block = _shared_block("review-hold-sweeper.sh")

    assert runner_block, "러너에 job_diff_contract 공유 블록이 없다"
    assert runner_block == sweeper_block
    for fn in ("capture_job_diff_text() {", "normalize_job_diff() {", "resolve_job_base_sha() {"):
        assert fn in runner_block


def test_neither_script_sources_or_executes_the_other():
    """순환 의존 금지 — 러너는 최상단에서 flock 을 잡고 말미에서 main 을 부른다.
    스위퍼가 그것을 source/실행 하면 스위퍼가 러너를 기동시킨다. 반대도 같다.
    두 스크립트는 오직 pipeline_jobs 의 플래그로만 만난다.
    """
    pairs = (
        ("review-hold-sweeper.sh", "pipeline-runner.sh"),
        ("pipeline-runner.sh", "review-hold-sweeper.sh"),
    )
    for script_name, other in pairs:
        # 주석을 뺀 실행 줄만 본다 — 공유 블록 주석은 상대 파일명을 설명한다.
        code = "\n".join(
            line for line in _read(script_name).splitlines()
            if not line.lstrip().startswith("#")
        )
        assert other not in code, f"{script_name} 이 {other} 를 코드에서 참조한다"


def test_runner_captures_git_diff_through_the_shared_function_only():
    """러너가 캡처 규칙을 인라인으로 다시 쓰면 공유 블록이 죽은 코드가 된다."""
    script = _read("pipeline-runner.sh")
    body = script[script.index(SHARED_BLOCK_END):]

    assert 'git_diff=$(capture_job_diff_text "$workdir" "$pre_exec_sha")' in body
    # 인라인 복제본이 남아 있으면 안 된다.
    assert "head -c 45000" not in body
    assert 'git_diff=$(git diff HEAD 2>/dev/null | head -c 50000)' not in body


def test_sweeper_dirty_path_routes_to_diff_comparison_not_to_promotion():
    helper = _sweeper_helpers()
    dirty_gate = helper.index('status --porcelain')
    route = helper.index('review_hold_dirty_recovery "$job_id" "$worktree_dir"', dirty_gate)
    clean_promote = helper.index("commit_hash='${current_sha}'", dirty_gate)

    # dirty 판정이 clean HEAD 승격보다 먼저 와야 dirty 산출물이 승격되지 않는다.
    assert dirty_gate < route < clean_promote
    assert "capture_job_diff_text" in helper
    assert "normalize_job_diff" in helper


# ── 실제 동작 — 캡처/정규화/분기 판정 ──────────────────────────────────


@pytest.fixture(scope="module")
def helper_file(tmp_path_factory):
    if shutil.which("git") is None or shutil.which("sha256sum") is None or shutil.which("awk") is None:
        pytest.skip("git/sha256sum/awk 미설치 환경 — 동작 테스트 생략")
    d = tmp_path_factory.mktemp("review_hold_helpers")
    f = d / "helpers.sh"
    sweeper = _read("review-hold-sweeper.sh")
    sql_escape = sweeper[sweeper.index("sql_escape() {"):sweeper.index("\n}\n", sweeper.index("sql_escape() {")) + 3]
    f.write_text(
        "set -eo pipefail\n"
        + sql_escape
        + "\n"
        + _sweeper_helpers()
        + """
# ── 테스트 스텁 ────────────────────────────────────────────────────────
log() { echo "[log] $*" >&2; }
db_query() {
    case "$1" in
        *"COALESCE(git_diff,'')"*)     cat "$STUB_STORED_DIFF" ;;
        *"COALESCE(error_detail,'')"*) cat "$STUB_ERROR_DETAIL" ;;
        *)                             printf '\\n' ;;
    esac
}
db_exec() {
    printf '%s\\n===SQL===\\n' "$1" >> "$STUB_SQL_LOG"
    case "$1" in
        *"error_detail='review_hold_recovery_pending'"*)
            printf 'review_hold_recovery_pending' > "$STUB_ERROR_DETAIL" ;;
    esac
    return 0
}
""",
        encoding="utf-8",
    )
    return f


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args], check=True, capture_output=True, text=True
    ).stdout.strip()


@pytest.fixture
def worktree(tmp_path):
    repo = tmp_path / "wt"
    repo.mkdir()
    _git_init = subprocess.run(
        ["git", "init", "-q", "-b", "main", str(repo)], check=True, capture_output=True
    )
    assert _git_init.returncode == 0
    _git(repo, "config", "user.email", "runner@aads.local")
    _git(repo, "config", "user.name", "AADS Runner Test")
    (repo / "f.txt").write_text("v1\n", encoding="utf-8")
    _git(repo, "add", "f.txt")
    _git(repo, "commit", "-m", "base")
    # 검수받은 산출물 = 미커밋 변경 (구 러너가 남긴 dirty 워크트리)
    (repo / "f.txt").write_text("v2\n", encoding="utf-8")
    return repo


def _run_dirty_recovery(helper_file: Path, tmp_path: Path, worktree: Path, stored_diff: str):
    stored = tmp_path / "stored.diff"
    stored.write_text(stored_diff, encoding="utf-8")
    err_detail = tmp_path / "error_detail"
    err_detail.write_text("review_infra_failed", encoding="utf-8")
    sql_log = tmp_path / "sql.log"
    sql_log.write_text("", encoding="utf-8")

    proc = subprocess.run(
        [
            "bash",
            "-c",
            f'source "{helper_file}"; '
            f'rc=0; review_hold_dirty_recovery "runner-deadbeef" "{worktree}" "0.9" "2" || rc=$?; exit "$rc"',
        ],
        capture_output=True,
        text=True,
        env={
            "PATH": "/usr/local/bin:/usr/bin:/bin",
            "STUB_STORED_DIFF": str(stored),
            "STUB_ERROR_DETAIL": str(err_detail),
            "STUB_SQL_LOG": str(sql_log),
            "HOME": str(tmp_path),
        },
    )
    return proc.returncode, sql_log.read_text(encoding="utf-8"), proc.stderr


def _captured_diff(helper_file: Path, worktree: Path) -> str:
    return subprocess.run(
        ["bash", "-c", f'source "{helper_file}"; capture_job_diff_text "{worktree}" ""'],
        check=True,
        capture_output=True,
        text=True,
        env={"PATH": "/usr/local/bin:/usr/bin:/bin"},
    ).stdout


def test_dirty_recovery_hands_matching_diff_to_the_runner(helper_file, tmp_path, worktree):
    """검수받은 diff 와 워크트리가 같으면 스위퍼는 확정하지 않고 표시만 남긴다.

    저장값에는 psql 이 붙이는 끝줄 개행이 있고 워크트리 캡처에는 없다 —
    그 차이로 drift 오판이 나면 멀쩡한 산출물이 통째로 폐기되므로 여기서 고정한다.
    """
    stored = _captured_diff(helper_file, worktree) + "\n"
    rc, sql, _ = _run_dirty_recovery(helper_file, tmp_path, worktree, stored)

    assert rc == 10, f"기대 10(러너 인계), 실제 {rc}"
    assert "error_detail='review_hold_recovery_pending'" in sql
    assert "review_flag_category=NULL" in sql
    # 인계는 종결이 아니다 — 상태를 error 로 굳히거나 승인 큐로 올리면 안 된다.
    assert "status='error'" not in sql
    assert "status='awaiting_approval'" not in sql


def test_dirty_recovery_terminates_on_diff_drift(helper_file, tmp_path, worktree):
    """검수 뒤에 워크트리가 바뀌었으면 승인 큐로 올리지 않고 terminal 로 끝낸다."""
    stored = _captured_diff(helper_file, worktree)
    (worktree / "f.txt").write_text("v3-someone-else-touched-this\n", encoding="utf-8")
    rc, sql, _ = _run_dirty_recovery(helper_file, tmp_path, worktree, stored)

    assert rc == 11, f"기대 11(terminal), 실제 {rc}"
    assert "status='error'" in sql
    assert "error_detail='review_hold_diff_drift'" in sql
    assert "review_flag_category=NULL" in sql
    assert "error_detail='review_hold_recovery_pending'" not in sql


def test_dirty_recovery_holds_when_stored_diff_is_empty(helper_file, tmp_path, worktree):
    """빈 diff 두 개의 해시가 같다고 승격시키면 아무 변경 없는 잡이 승인 큐로 간다."""
    rc, sql, _ = _run_dirty_recovery(helper_file, tmp_path, worktree, "")

    assert rc == 1, f"기대 1(판정 불가 보류), 실제 {rc}"
    assert "status='error'" not in sql
    assert "error_detail='review_hold_recovery_pending'" not in sql


def test_normalize_ignores_blob_hashes_but_not_content(helper_file, tmp_path, worktree):
    """index(blob) 줄은 무시하고 내용 줄은 무시하지 않는다."""
    captured = _captured_diff(helper_file, worktree)
    assert "index " in captured

    def _hash(text: str) -> str:
        return subprocess.run(
            ["bash", "-c", f'source "{helper_file}"; normalize_job_diff | sha256sum'],
            input=text,
            check=True,
            capture_output=True,
            text=True,
            env={"PATH": "/usr/local/bin:/usr/bin:/bin"},
        ).stdout

    swapped_blob = "\n".join(
        "index 1111111..2222222 100644" if line.startswith("index ") else line
        for line in captured.splitlines()
    )
    changed_body = captured.replace("+v2", "+v2-tampered")

    assert _hash(captured) == _hash(swapped_blob)
    assert _hash(captured) != _hash(changed_body)
