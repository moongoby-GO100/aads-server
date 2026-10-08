"""stale_base 자동 rebase — 추가 전용 허용 목록(HANDOVER.md) 겹침 예외 테스트.

배경(2026-10-08): 오늘 origin/main 러너 커밋 8건 중 6건이 HANDOVER.md 를 건드려
병행 러너끼리 이 파일에서 거의 항상 겹친다. runner-da20bef8, runner-8abceeea 는
겹친 파일이 HANDOVER.md 하나뿐이었고 텍스트 충돌도 없었는데 AUTO_REBASE_SKIP 으로
끝났다.

고정하는 계약
  - 겹친 파일이 전부 허용 목록에 속하고 이 잡의 변경이 삭제 줄 0 이면 시도한다.
  - 하나라도 허용 목록 밖이면, 삭제가 있으면, 충돌하면 종전처럼 막고 원 SHA 로 남는다.
  - 시도 결과에서 허용 목록 밖 경로 내용이 승인 SHA 와 다르면 원복한다.
  - 허용 목록 파일의 문맥 줄만 달라진 rebase 결과는 patch-id 가 같다(승인 상속).
  - rebase 가 만든 커밋도 post-rewrite 훅으로 pre-commit 서명을 이어받는다.
"""

import os
import re
import shutil
import stat
import subprocess
import uuid
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = ("pipeline-runner.sh", "pipeline-runner.sh.local")
SHA_RE = re.compile(r"^[0-9a-f]{40}$")

pytestmark = pytest.mark.skipif(
    shutil.which("git") is None or shutil.which("comm") is None, reason="git/comm 필요"
)


def _read_script(name: str = "pipeline-runner.sh") -> str:
    return (ROOT / "scripts" / name).read_text(encoding="utf-8")


def _extract_function(script: str, name: str) -> str:
    start = script.index(f"{name}() {{")
    return script[start : script.index("\n}\n", start) + len("\n}\n")]


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args], check=True, capture_output=True, text=True
    ).stdout.strip()


HANDOVER_BASE = "H1\n" + "".join(f"pad{i}\n" for i in range(1, 11)) + "tail\n"


@pytest.fixture(scope="module")
def fn_file(tmp_path_factory):
    script = _read_script()
    path = tmp_path_factory.mktemp("append_only_fn") / "fn.sh"
    path.write_text(
        "set -eo pipefail\n"
        'log() { echo "$*" >&2; }\n'
        # DB 스텁: 표식 조회는 항상 "없음"
        "db_exec() { echo f; }\n"
        'record_runner_event() { echo "EVENT $*" >> "${EVENT_LOG:-/dev/null}"; }\n'
        + _extract_function(script, "classify_push_state")
        + _extract_function(script, "attempt_stale_base_rebase")
        + _extract_function(script, "commit_patch_id"),
        encoding="utf-8",
    )
    return path


def _build_case(tmp_path: Path, job_id: str, base: dict, job: dict, incoming: dict, hooks: bool = False):
    remote = tmp_path / "remote.git"
    clone = tmp_path / "clone"
    subprocess.run(["git", "init", "--bare", "-b", "main", str(remote)], check=True, capture_output=True)
    subprocess.run(["git", "clone", str(remote), str(clone)], check=True, capture_output=True)
    _git(clone, "config", "user.email", "runner@aads.local")
    _git(clone, "config", "user.name", "AADS Runner Test")
    if hooks:
        hook = clone / ".git" / "hooks" / "post-rewrite"
        shutil.copy(ROOT / "scripts" / "hooks" / "post-rewrite", hook)
        hook.chmod(hook.stat().st_mode | stat.S_IXUSR)

    for name, body in base.items():
        (clone / name).write_text(body, encoding="utf-8")
    _git(clone, "add", "-A")
    _git(clone, "commit", "-m", "base")
    _git(clone, "push", "origin", "main")

    worktree = Path(f"/tmp/aads-wt-{job_id}")
    _git(clone, "worktree", "add", "--detach", str(worktree), "HEAD")
    for name, body in job.items():
        (worktree / name).write_text(body, encoding="utf-8")
    _git(worktree, "add", "-A")
    _git(worktree, "commit", "-m", "job commit")
    job_sha = _git(worktree, "rev-parse", "HEAD")

    for name, body in incoming.items():
        (clone / name).write_text(body, encoding="utf-8")
    _git(clone, "add", "-A")
    _git(clone, "commit", "-m", "incoming")
    _git(clone, "push", "origin", "main")
    return clone, worktree, job_sha


@pytest.fixture
def make_case(tmp_path):
    made = []

    def _make(base, job, incoming, hooks=False):
        job_id = f"test{uuid.uuid4().hex[:10]}"
        clone, worktree, job_sha = _build_case(tmp_path, job_id, base, job, incoming, hooks)
        made.append((clone, worktree))
        return job_id, clone, worktree, job_sha

    yield _make
    for clone, worktree in made:
        shutil.rmtree(worktree, ignore_errors=True)
        subprocess.run(["git", "-C", str(clone), "worktree", "prune"], capture_output=True)


def _run(fn_file, worktree, sha, job_id, tmp_path, extra_env=None, path_prefix=None):
    base_env = {k: v for k, v in os.environ.items() if not k.startswith("AUTO_REBASE_")}
    env = {**base_env, "EVENT_LOG": str(tmp_path / "events.log"), **(extra_env or {})}
    if path_prefix:
        env["PATH"] = f"{path_prefix}:{env['PATH']}"
    return subprocess.run(
        ["bash", "-c", f'source "{fn_file}"; attempt_stale_base_rebase "{worktree}" "{sha}" "{job_id}"'],
        capture_output=True, text=True, env=env,
    )


def _handover_append_case(make_case):
    """잡은 HANDOVER.md 끝에 줄을 더하고, origin/main 은 문맥 줄(pad9)을 바꾼다 — 텍스트 충돌은 없다."""
    job_h = HANDOVER_BASE + "job entry\n"
    inc_h = HANDOVER_BASE.replace("pad9\n", "pad9 incoming\n")
    return make_case(
        {"HANDOVER.md": HANDOVER_BASE, "other.txt": "o\n"},
        {"HANDOVER.md": job_h, "job_only.txt": "job\n"},
        {"HANDOVER.md": inc_h, "inc_only.txt": "inc\n"},
    )


# (a) HANDOVER.md 만 추가 겹침 → 자동 rebase 성공
def test_handover_append_only_overlap_is_rebased(fn_file, make_case, tmp_path):
    job_id, _clone, worktree, job_sha = _handover_append_case(make_case)

    proc = _run(fn_file, worktree, job_sha, job_id, tmp_path)

    assert proc.returncode == 0, proc.stderr
    new_sha = proc.stdout.strip()
    assert SHA_RE.match(new_sha) and new_sha != job_sha
    assert "AUTO_REBASE_OK" in proc.stderr
    assert "append_only_overlap=HANDOVER.md" in proc.stderr
    assert "AUTO_REBASE_SKIP" not in proc.stderr
    events = (tmp_path / "events.log").read_text(encoding="utf-8")
    assert "auto_rebase_append_only_overlap" in events and '"append_only_overlap":"HANDOVER.md"' in events
    state = subprocess.run(
        ["bash", "-c", f'source "{fn_file}"; classify_push_state "{worktree}" "{new_sha}"'],
        check=True, capture_output=True, text=True,
    ).stdout.strip()
    assert state == "fast_forward"
    handover = (worktree / "HANDOVER.md").read_text(encoding="utf-8")
    assert "pad9 incoming\n" in handover and handover.endswith("job entry\n")
    assert (worktree / "job_only.txt").read_text(encoding="utf-8") == "job\n"
    assert (worktree / "inc_only.txt").read_text(encoding="utf-8") == "inc\n"


# (b) HANDOVER.md 에 삭제 줄 포함 → SKIP
def test_handover_with_deleted_line_is_skipped(fn_file, make_case, tmp_path):
    job_h = HANDOVER_BASE.replace("tail\n", "tail edited\n")
    inc_h = HANDOVER_BASE.replace("pad1\n", "pad1 incoming\n")
    job_id, _clone, worktree, job_sha = make_case(
        {"HANDOVER.md": HANDOVER_BASE}, {"HANDOVER.md": job_h}, {"HANDOVER.md": inc_h}
    )

    proc = _run(fn_file, worktree, job_sha, job_id, tmp_path)

    assert proc.returncode == 1
    assert "AUTO_REBASE_SKIP" in proc.stderr and "추가 전용 아님" in proc.stderr
    assert "AUTO_REBASE_OK" not in proc.stderr
    assert _git(worktree, "rev-parse", "HEAD") == job_sha


# (c) 다른 파일도 겹침 → SKIP
def test_other_file_overlap_alongside_handover_is_skipped(fn_file, make_case, tmp_path):
    job_id, _clone, worktree, job_sha = make_case(
        {"HANDOVER.md": HANDOVER_BASE, "app.py": "a\n" + "x\n" * 10 + "z\n"},
        {"HANDOVER.md": HANDOVER_BASE + "job entry\n", "app.py": "job a\n" + "x\n" * 10 + "z\n"},
        {"HANDOVER.md": HANDOVER_BASE.replace("pad1\n", "pad1 inc\n"), "app.py": "a\n" + "x\n" * 10 + "inc z\n"},
    )

    proc = _run(fn_file, worktree, job_sha, job_id, tmp_path)

    assert proc.returncode == 1
    assert "AUTO_REBASE_SKIP" in proc.stderr and "app.py" in proc.stderr
    assert "AUTO_REBASE_OK" not in proc.stderr
    assert _git(worktree, "rev-parse", "HEAD") == job_sha


# (d) HANDOVER.md 텍스트 충돌 → abort, 원 SHA 복귀
def test_handover_text_conflict_aborts_and_restores_sha(fn_file, make_case, tmp_path):
    job_id, _clone, worktree, job_sha = make_case(
        {"HANDOVER.md": HANDOVER_BASE},
        {"HANDOVER.md": HANDOVER_BASE + "job entry\n"},
        {"HANDOVER.md": HANDOVER_BASE + "incoming entry\n"},
    )

    proc = _run(fn_file, worktree, job_sha, job_id, tmp_path)

    assert proc.returncode == 1
    assert "AUTO_REBASE_APPEND_ONLY" in proc.stderr
    assert "AUTO_REBASE_FAIL" in proc.stderr
    assert _git(worktree, "rev-parse", "HEAD") == job_sha
    assert _git(worktree, "status", "--porcelain") == ""
    assert not (tmp_path / "events.log").exists()


# (e) AUTO_REBASE_STALE_BASE=0 → 비활성
def test_kill_switch_disables_append_only_rebase(fn_file, make_case, tmp_path):
    job_id, _clone, worktree, job_sha = _handover_append_case(make_case)

    proc = _run(fn_file, worktree, job_sha, job_id, tmp_path, {"AUTO_REBASE_STALE_BASE": "0"})

    assert proc.returncode == 1 and proc.stdout == ""
    assert _git(worktree, "rev-parse", "HEAD") == job_sha


def test_live_repository_is_still_refused(fn_file, make_case, tmp_path):
    job_id, clone, _worktree, job_sha = _handover_append_case(make_case)

    proc = _run(fn_file, clone, job_sha, job_id, tmp_path)

    assert proc.returncode == 1


# 허용 목록 확장
def test_allowlist_extension_via_env(fn_file, make_case, tmp_path):
    notes = "N1\n" + "".join(f"n{i}\n" for i in range(1, 11)) + "end\n"
    args = (
        {"NOTES.md": notes},
        {"NOTES.md": notes + "job note\n"},
        {"NOTES.md": notes.replace("n9\n", "n9 incoming\n")},
    )
    job_id, _clone, worktree, job_sha = make_case(*args)
    proc = _run(fn_file, worktree, job_sha, job_id, tmp_path)
    assert proc.returncode == 1 and "AUTO_REBASE_SKIP" in proc.stderr

    proc = _run(fn_file, worktree, job_sha, job_id, tmp_path, {"AUTO_REBASE_APPEND_ONLY_FILES": "NOTES.md"})
    assert proc.returncode == 0, proc.stderr
    assert "append_only_overlap=NOTES.md" in proc.stderr


def test_malformed_allowlist_entries_are_ignored(fn_file, make_case, tmp_path):
    job_id, _clone, worktree, job_sha = make_case(
        {"NOTES.md": "a\n"}, {"NOTES.md": "a\nb\n"}, {"NOTES.md": "z\na\n"}
    )
    proc = _run(fn_file, worktree, job_sha, job_id, tmp_path,
                {"AUTO_REBASE_APPEND_ONLY_FILES": "../NOTES.md /NOTES.md NOTES.md;x"})
    assert proc.returncode == 1 and "AUTO_REBASE_SKIP" in proc.stderr


# 허용 목록 밖 경로가 달라지면 원복한다
def test_unexpected_non_allowlist_change_after_rebase_is_reverted(fn_file, make_case, tmp_path):
    job_id, _clone, worktree, job_sha = _handover_append_case(make_case)
    real_git = shutil.which("git")
    shim_dir = tmp_path / "shim"
    shim_dir.mkdir()
    shim = shim_dir / "git"
    shim.write_text(
        f'#!/bin/bash\n"{real_git}" "$@"; rc=$?\n'
        'if [[ "$3" == "rebase" && "$*" != *"--abort"* && $rc -eq 0 ]]; then\n'
        f'  echo sneaky > "$2/sneaky.txt"; "{real_git}" -C "$2" add sneaky.txt\n'
        f'  "{real_git}" -C "$2" commit -q --amend --no-edit\n'
        "fi\nexit $rc\n",
        encoding="utf-8",
    )
    shim.chmod(0o755)

    proc = _run(fn_file, worktree, job_sha, job_id, tmp_path, path_prefix=str(shim_dir))

    assert proc.returncode == 1, proc.stderr
    assert "AUTO_REBASE_REVERT" in proc.stderr and "AUTO_REBASE_OK" not in proc.stderr
    assert _git(worktree, "rev-parse", "HEAD") == job_sha


# 승인 상속(patch-id)
def _pid(fn_file, repo, sha, extra_env=None):
    out = subprocess.run(
        ["bash", "-c", f'source "{fn_file}"; commit_patch_id "{repo}" "{sha}"'],
        capture_output=True, text=True, check=True, env={**os.environ, **(extra_env or {})},
    )
    return out.stdout.strip()


def test_patch_id_ignores_allowlist_context_changes(fn_file, make_case, tmp_path):
    job_id, _clone, worktree, job_sha = _handover_append_case(make_case)
    new_sha = _run(fn_file, worktree, job_sha, job_id, tmp_path).stdout.strip()
    assert SHA_RE.match(new_sha)

    raw = lambda sha: subprocess.run(  # noqa: E731
        f'git -C "{worktree}" diff {sha}^ {sha} | git -C "{worktree}" patch-id --stable',
        shell=True, capture_output=True, text=True,
    ).stdout.split()[0]
    assert raw(job_sha) != raw(new_sha), "전제: 문맥 줄 때문에 종전 patch-id 는 달라져야 한다"

    old_pid, new_pid = _pid(fn_file, worktree, job_sha), _pid(fn_file, worktree, new_sha)
    assert SHA_RE.match(old_pid) and old_pid == new_pid


def test_patch_id_differs_when_added_lines_differ(fn_file, make_case, tmp_path):
    job_id, _clone, worktree, job_sha = _handover_append_case(make_case)
    new_sha = _run(fn_file, worktree, job_sha, job_id, tmp_path).stdout.strip()
    _git(worktree, "checkout", "-q", "--detach", f"{new_sha}^")
    (worktree / "HANDOVER.md").write_text(
        (worktree / "HANDOVER.md").read_text(encoding="utf-8") + "DIFFERENT entry\n", encoding="utf-8"
    )
    (worktree / "job_only.txt").write_text("job\n", encoding="utf-8")
    _git(worktree, "add", "-A")
    _git(worktree, "commit", "-q", "-m", "job commit")
    other = _git(worktree, "rev-parse", "HEAD")

    assert _pid(fn_file, worktree, other) != _pid(fn_file, worktree, new_sha)


def test_patch_id_differs_when_non_allowlist_file_differs(fn_file, make_case, tmp_path):
    job_id, _clone, worktree, job_sha = _handover_append_case(make_case)
    new_sha = _run(fn_file, worktree, job_sha, job_id, tmp_path).stdout.strip()
    _git(worktree, "checkout", "-q", "--detach", f"{new_sha}^")
    (worktree / "HANDOVER.md").write_text(
        (worktree / "HANDOVER.md").read_text(encoding="utf-8") + "job entry\n", encoding="utf-8"
    )
    (worktree / "job_only.txt").write_text("CHANGED\n", encoding="utf-8")
    _git(worktree, "add", "-A")
    _git(worktree, "commit", "-q", "-m", "job commit")
    other = _git(worktree, "rev-parse", "HEAD")

    assert _pid(fn_file, worktree, other) != _pid(fn_file, worktree, new_sha)


def test_patch_id_without_allowlist_files_matches_plain_git_patch_id(fn_file, make_case, tmp_path):
    _job_id, clone, _worktree, _job_sha = _handover_append_case(make_case)
    head = _git(clone, "rev-parse", "HEAD")  # inc_only.txt + HANDOVER.md → HANDOVER 포함 커밋은 제외
    _git(clone, "checkout", "-q", "--detach", head)
    (clone / "plain.txt").write_text("plain\n", encoding="utf-8")
    _git(clone, "add", "plain.txt")
    _git(clone, "commit", "-q", "-m", "plain")
    plain = _git(clone, "rev-parse", "HEAD")
    want = subprocess.run(
        f'git -C "{clone}" diff {plain}^ {plain} | git -C "{clone}" patch-id --stable',
        shell=True, capture_output=True, text=True,
    ).stdout.split()[0]

    assert _pid(fn_file, clone, plain) == want


# pre-push 서명 (R-PUSH / git.rebase_strips_precommit_signature)
def test_auto_rebased_commit_inherits_precommit_signature_via_post_rewrite(fn_file, make_case, tmp_path):
    job_id, clone, worktree, job_sha = make_case(
        {"HANDOVER.md": HANDOVER_BASE, "other.txt": "o\n"},
        {"HANDOVER.md": HANDOVER_BASE + "job entry\n"},
        {"HANDOVER.md": HANDOVER_BASE.replace("pad9\n", "pad9 incoming\n")},
        hooks=True,
    )
    marks = clone / ".git" / "hook_verified"
    marks.mkdir(exist_ok=True)
    (marks / job_sha).write_text("verified\n", encoding="utf-8")

    proc = _run(fn_file, worktree, job_sha, job_id, tmp_path)

    assert proc.returncode == 0, proc.stderr
    new_sha = proc.stdout.strip()
    assert (marks / new_sha).is_file(), "rebase 가 만든 커밋에 서명이 옮겨지지 않았다 — pre-push 가 막는다"


# 스크립트 계약
def test_runner_scripts_stay_byte_identical_and_contract_markers_present():
    assert _read_script("pipeline-runner.sh") == _read_script("pipeline-runner.sh.local")
    for name in SCRIPTS:
        script = _read_script(name)
        fn = _extract_function(script, "attempt_stale_base_rebase")
        assert "AUTO_REBASE_APPEND_ONLY_FILES" in fn and "append_only_overlap=" in fn
        assert '"auto_rebase_append_only_overlap"' in fn
        assert "AUTO_REBASE_STALE_BASE" in fn
        assert "push --force" not in script and "--force-with-lease" not in script
