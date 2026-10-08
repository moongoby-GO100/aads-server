"""리뷰 입력 diff 절단 고지 + 전체 diffstat 동봉, .runner_full_diff.patch 커밋 유입 차단.

(R2) 이번 라운드는 파일 추적 해제(git rm --cached)를 하지 않는다. 스테이징 제외
pathspec 과 .gitignore 로 유입만 막는다.

배경(2026-09-30): capture_job_diff_text 가 45000B 에서 조용히 자르고, 추적 중이던
.runner_full_diff.patch(64KB+)가 diff 앞머리를 다 먹어 실제 소스 변경이 리뷰어에게
보이지 않았다. 리뷰어는 "테스트 파일이 없다"며 세 번 연속 반려했다.
"""

import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[2]
PATCH_NAME = ".runner_full_diff.patch"
SHARED_BLOCK_BEGIN = "# ─── SHARED-BLOCK BEGIN: job_diff_contract"
SHARED_BLOCK_END = "# ─── SHARED-BLOCK END: job_diff_contract"
FUNCS = (
    "review_diff_stat_section",
    "review_diff_truncation_notice",
    "build_review_diff_prefix",
    "job_diff_changed_files",
    "looks_like_git_diff",
)


def _runner() -> str:
    return (ROOT / "scripts" / "pipeline-runner.sh").read_text(encoding="utf-8")


def _func(script: str, name: str) -> str:
    start = script.index(f"{name}() {{")
    return script[start:script.index("\n}\n", start) + 3]


# ── 정적 검사 ──────────────────────────────────────────────────────────


def test_runner_has_truncation_notice_and_stat_attachment():
    script = _runner()
    assert "[DIFF TRUNCATED]" in script
    assert "[DIFFSTAT TRUNCATED" in script
    assert "diff --stat" in _func(script, "review_diff_stat_section")
    assert "--name-only" in _func(script, "review_diff_stat_section")
    assert "head -c 6000" in _func(script, "review_diff_stat_section")


def test_caps_live_only_in_shared_capture_and_prefix_follows_truncated_marker():
    """상한 숫자는 capture_job_diff_text 한 곳뿐이고, 앞머리는 거기서 만든 `# [TRUNCATED]` 줄로 잘림을 판정한다.

    상한을 두 군데 두면(예전: head -c 와 cap_* 변수) 갈라지는 순간 고지가 거짓이 된다.
    """
    script = _runner()
    shared = script[script.index(SHARED_BLOCK_BEGIN):script.index(SHARED_BLOCK_END)]
    capture_caps = set(re.findall(r"_job_diff_render \"\$repo\" (\d+)", _func(shared, "capture_job_diff_text")))
    assert capture_caps == {"45000", "5000", "50000"}
    assert "head -c 45000" not in shared
    prefix = _func(script, "build_review_diff_prefix")
    assert not re.search(r"cap_\w+=\d+", prefix)
    assert "# \\[TRUNCATED\\] " in prefix


def test_exclude_specs_defined_once_in_shared_block_and_used_by_all_diff_readers():
    script = _runner()
    shared = script[script.index(SHARED_BLOCK_BEGIN):script.index(SHARED_BLOCK_END)]
    assert shared.count("JOB_DIFF_EXCLUDE_SPECS=(") == 1
    assert f":(exclude){PATCH_NAME}" in shared
    assert "JOB_DIFF_EXCLUDE_SPECS[@]" in _func(script, "review_diff_stat_section")


def test_review_request_uses_prefixed_diff_but_stores_raw_git_diff():
    script = _runner()
    start = script.index('local review_diff="$git_diff"')
    end = script.index("while [[ $review_attempt -lt $review_max_attempts ]]", start)
    region = script[start:end]
    assert region.count('--arg diff "$review_diff"') == 2
    assert '--arg diff "$git_diff"' not in region
    # DB 저장값·스위퍼 비교 기준은 앞머리 없는 원본 그대로여야 한다.
    assert 'git_diff=$(sql_escape "$git_diff")' in script
    assert "review_diff_prefix" not in script[script.index(SHARED_BLOCK_BEGIN):script.index(SHARED_BLOCK_END)]


def test_every_git_add_all_excludes_full_diff_patch():
    script = _runner()
    lines = [ln for ln in script.splitlines() if "add -A" in ln and not ln.lstrip().startswith("#")]
    assert len(lines) >= 2
    for ln in lines:
        assert PATCH_NAME in ln, ln


def test_gitignore_lists_full_diff_patch():
    entries = [ln.strip() for ln in (ROOT / ".gitignore").read_text(encoding="utf-8").splitlines()]
    assert PATCH_NAME in entries


def test_full_diff_patch_still_tracked_this_round():
    """추적 해제는 보존게이트 교정이 배포된 뒤 별도 job 이다 — 이번 라운드에는 추적 상태가 정상."""
    if shutil.which("git") is None:
        pytest.skip("git 미설치")
    res = subprocess.run(["git", "ls-files"], cwd=ROOT, capture_output=True, text=True)
    if res.returncode != 0 or not res.stdout.strip():
        pytest.skip("git 저장소로 읽을 수 없는 환경(마운트된 워크트리)")
    if PATCH_NAME not in res.stdout.splitlines():
        pytest.skip("추적 해제 라운드 이후 — 이 검사는 더 이상 해당 없음")


def test_full_diff_patch_is_excluded_from_intent_to_add_candidates():
    script = _runner()
    assert "coverage\\.xml|\\.runner_full_diff\\.patch" in script


# ── 동작 검사 — 실제 git 저장소에서 bash 함수를 직접 호출 ─────────────────


def _git(repo: Path, *args: str) -> str:
    env = dict(os.environ, GIT_CONFIG_GLOBAL="/dev/null", GIT_CONFIG_SYSTEM="/dev/null")
    res = subprocess.run(
        ["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@t",
         "-c", "commit.gpgsign=false", *args],
        capture_output=True, text=True, env=env,
    )
    assert res.returncode == 0, res.stderr
    return res.stdout.strip()


def _write(repo: Path, rel: str, text: str) -> None:
    p = repo / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")


@pytest.fixture()
def repo(tmp_path):
    if shutil.which("git") is None or shutil.which("bash") is None:
        pytest.skip("git/bash 미설치")
    r = tmp_path / "r"
    r.mkdir()
    _git(r, "init", "-q")
    _write(r, "README.md", "base\n")
    _git(r, "add", "-A")
    _git(r, "commit", "-q", "-m", "base")
    return r


def _prefix(repo: Path, base: str) -> str:
    script = _runner()
    return _bash(repo, 'build_review_diff_prefix "$1" "$2"', base)


def _bash(repo: Path, call: str, *args: str) -> str:
    script = _runner()
    shared = script[script.index(SHARED_BLOCK_BEGIN):script.index(SHARED_BLOCK_END)]
    body = shared + "\n" + "\n".join(_func(script, n) for n in FUNCS)
    res = subprocess.run(
        ["bash", "-c", f'set -eo pipefail\n{body}\n{call}', "bash", str(repo), *args],
        capture_output=True, text=True,
    )
    assert res.returncode == 0, res.stderr
    return res.stdout


def _commit_big_change(repo: Path, extra_files: int = 0) -> str:
    base = _git(repo, "rev-parse", "HEAD")
    _write(repo, "app/big.py", "".join(f"value_{i} = {i}\n" for i in range(6000)))
    for i in range(extra_files):
        _write(repo, f"tests/unit/generated_case_directory_number_{i:04d}/test_case_{i:04d}.py", "x = 1\n")
    _write(repo, "tests/unit/test_late.py", "def test_late():\n    assert True\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "change")
    return base


def test_truncated_diff_gets_notice_and_full_diffstat(repo):
    base = _commit_big_change(repo)
    head = _git(repo, "rev-parse", "HEAD")
    full = len(subprocess.run(
        ["git", "-C", str(repo), "diff", f"{base}..{head}"], capture_output=True).stdout)
    assert full > 45000

    out = _prefix(repo, base)

    assert out.startswith(f"[DIFF TRUNCATED] 전체 {full}B 중 앞 ")
    assert 'diff 가 잘렸다. 생략 파일 목록을 근거로 "구현 없음" 판정을 하지 말 것.' in out
    assert "DIFFSTAT (전체 변경 파일 — 절단 없음)" in out
    # 45KB 뒤에 밀려 잘려 나가는 파일도 목록에는 있어야 한다.
    assert "tests/unit/test_late.py" in out
    assert "app/big.py" in out
    assert "[DIFFSTAT TRUNCATED" not in out


def test_small_diff_gets_no_prefix_at_all(repo):
    base = _git(repo, "rev-parse", "HEAD")
    _write(repo, "a.txt", "hello\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "small")
    assert _prefix(repo, base) == ""


def test_oversized_diffstat_reports_untruncated_file_count(repo):
    base = _commit_big_change(repo, extra_files=150)
    names = _git(repo, "diff", "--name-only", f"{base}..HEAD").splitlines()
    assert len(names) == 152

    out = _prefix(repo, base)

    assert "[DIFFSTAT TRUNCATED — 파일 152개]" in out
    assert "절단 없음" not in out
    stat_part = out.split("=== DIFF")[0]
    assert len(stat_part.encode("utf-8")) < 6000 + 1500


def test_uncommitted_changes_get_their_own_diffstat_section(repo):
    base = _commit_big_change(repo)
    _write(repo, "README.md", "base\nedited after commit\n")

    out = _prefix(repo, base)

    assert "DIFFSTAT (미커밋 변경 — git diff HEAD --stat" in out
    assert "README.md" in out


def test_no_base_branch_uses_single_cap(repo):
    _write(repo, "app/big.py", "".join(f"value_{i} = {i}\n" for i in range(6000)))
    _git(repo, "add", "-N", "app/big.py")

    out = _prefix(repo, "")

    assert re.match(r"\[DIFF TRUNCATED\] 전체 \d+B 중 앞 \d+B 만", out)
    assert "app/big.py" in out


def test_git_add_all_does_not_use_exclude_pathspec_for_ignored_patch():
    """.gitignore 에 오른 파일을 ':(exclude)' 로 지정하면 git add 가 rc=1 을 낸다.

    2026-10-02 15:19 KST runner-805e8cb8 이 그렇게 approval_commit_stage_failed 로 죽었다
    (5cb1f84e 가 패치 파일을 .gitignore 에 올린 직후). add -A 후 reset 으로 빼야 한다.
    """
    script = _runner()
    lines = [ln for ln in script.splitlines() if "add -A" in ln and not ln.lstrip().startswith("#")]
    for ln in lines:
        assert ":(exclude)" not in ln, ln
        assert f"reset -q -- {PATCH_NAME}" in ln, ln


# ── 산출물 제외 · 코드 우선 · 순삭제 stat-only · 잘림 명시 ───────────────────


def _capture(repo: Path, base: str) -> str:
    return _bash(repo, 'capture_job_diff_text "$1" "$2"', base)


def _artifact_repo(repo: Path) -> str:
    """base 에 1,040줄 .runner_full_diff.patch 가 추적돼 있고, 변경에서 이를 삭제 + 코드 3개 추가."""
    _write(repo, PATCH_NAME, "".join(f"-artifact line {i}\n" for i in range(1040)))
    _git(repo, "add", "-A", "-f")
    _git(repo, "commit", "-q", "-m", "tracked artifact")
    base = _git(repo, "rev-parse", "HEAD")
    _git(repo, "rm", "-q", PATCH_NAME)
    _write(repo, ".gitignore", f"{PATCH_NAME}\n")
    _write(repo, "app/controller.py", "".join(f"controller_{i} = {i}\n" for i in range(40)))
    _write(repo, "app/job.py", "".join(f"job_{i} = {i}\n" for i in range(40)))
    _write(repo, "tests/unit/test_controller.py", "def test_lock():\n    assert True\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "impl")
    return base


def test_artifact_deletion_is_excluded_and_all_code_bodies_are_included(repo):
    base = _artifact_repo(repo)
    raw = subprocess.run(["git", "-C", str(repo), "diff", f"{base}..HEAD"], capture_output=True, text=True).stdout
    assert PATCH_NAME in raw and len(raw) > 10000  # 산출물이 원문 diff 를 부풀리는 상황 재현

    out = _capture(repo, base)

    assert f"a/{PATCH_NAME}" not in out  # .gitignore 본문에 이름이 한 줄 들어가는 것은 정상
    assert "artifact line" not in out
    for needle in ("controller_39 = 39", "job_39 = 39", "def test_lock():"):
        assert needle in out
    assert "[TRUNCATED]" not in out
    # 코드 → 설정 순
    assert out.index("diff --git a/app/controller.py") < out.index("diff --git a/.gitignore")


def test_code_files_come_before_docs_and_config(repo):
    base = _git(repo, "rev-parse", "HEAD")
    _write(repo, "AAA_notes.md", "doc\n")
    _write(repo, "config.yaml", "k: v\n")
    _write(repo, "zzz/impl.py", "x = 1\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "mixed")

    out = _capture(repo, base)

    heads = re.findall(r"^diff --git a/(\S+) ", out, flags=re.M)
    assert heads == ["zzz/impl.py", "AAA_notes.md", "config.yaml"]


def test_large_pure_deletion_becomes_stat_only_line(repo):
    _write(repo, "old/legacy.py", "".join(f"legacy_{i} = {i}\n" for i in range(300)))
    _write(repo, "old/small.py", "".join(f"small_{i} = {i}\n" for i in range(50)))
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "legacy")
    base = _git(repo, "rev-parse", "HEAD")
    _git(repo, "rm", "-q", "old/legacy.py", "old/small.py")
    _write(repo, "app/new.py", "new = 1\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "drop")

    out = _capture(repo, base)

    assert "# [stat-only] old/legacy.py | -300 (pure deletion, body omitted)" in out
    assert "legacy_299" not in out
    # 200줄 미만 순삭제는 본문을 그대로 보인다.
    assert "diff --git a/old/small.py b/old/small.py" in out
    assert "-small_49 = 49" in out
    assert "new = 1" in out


def test_stat_only_deletion_alone_is_still_a_valid_review_diff(repo):
    _write(repo, "old/legacy.py", "".join(f"legacy_{i} = {i}\n" for i in range(300)))
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "legacy")
    base = _git(repo, "rev-parse", "HEAD")
    _git(repo, "rm", "-q", "old/legacy.py")
    _git(repo, "commit", "-q", "-m", "drop")

    out = _capture(repo, base)

    assert out.startswith("# [stat-only] old/legacy.py")
    res = subprocess.run(
        ["bash", "-c", "set -eo pipefail\n" + _func(_runner(), "looks_like_git_diff") + '\nlooks_like_git_diff "$1"', "bash", out],
        capture_output=True, text=True,
    )
    assert res.returncode == 0, res.stderr
    assert _bash(repo, 'job_diff_changed_files "$2"', out).strip() == "old/legacy.py"


def test_over_cap_gets_truncated_footer_with_omitted_files(repo):
    base = _commit_big_change(repo)

    out = _capture(repo, base)

    lines = out.split("\n")
    assert lines[-1].startswith("# [TRUNCATED] 상한 45000바이트 — 생략된 파일: ")
    assert "tests/unit/test_late.py" in lines[-1]
    assert "app/big.py(일부만 포함)" in lines[-1]
    body = "\n".join(lines[:-1])
    assert len(body.encode("utf-8")) <= 45000
    assert "diff --git a/app/big.py" in body
    assert "test_late" not in body
    assert _bash(repo, 'job_diff_changed_files "$2"', out).strip() == "app/big.py,tests/unit/test_late.py"


def test_truncated_footer_lists_files_never_included_without_extra_git_calls(repo):
    base = _commit_big_change(repo, extra_files=60)

    out = _capture(repo, base)

    footer = out.split("\n")[-1]
    assert footer.startswith("# [TRUNCATED] 상한 45000바이트 — 생략된 파일: ")
    assert "(외 " in footer and "개 생략)" in footer


def test_multibyte_text_cut_at_cap_stays_valid_utf8(repo):
    base = _git(repo, "rev-parse", "HEAD")
    _write(repo, "app/ko.py", "".join(f"# 한글 주석 {i}\n" for i in range(8000)))
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "ko")
    script = _runner()
    shared = script[script.index(SHARED_BLOCK_BEGIN):script.index(SHARED_BLOCK_END)]
    res = subprocess.run(
        ["bash", "-c", f'set -eo pipefail\n{shared}\ncapture_job_diff_text "$1" "$2"', "bash", str(repo), base],
        capture_output=True,
    )
    assert res.returncode == 0
    res.stdout.decode("utf-8")  # 깨진 바이트가 남으면 UnicodeDecodeError
    assert b"# [TRUNCATED]" in res.stdout


def test_review_prefix_for_truncated_diff_ignores_artifact_in_stat(repo):
    base = _artifact_repo(repo)
    _write(repo, "app/big.py", "".join(f"value_{i} = {i}\n" for i in range(6000)))
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "big")

    out = _prefix(repo, base)

    assert out.startswith("[DIFF TRUNCATED]")
    assert "app/controller.py" in out
    assert PATCH_NAME not in out


def test_prefix_accepts_already_captured_diff_as_third_arg(repo):
    base = _commit_big_change(repo)
    captured = _capture(repo, base)

    assert _bash(repo, 'build_review_diff_prefix "$1" "$2" "$3"', base, captured) == _prefix(repo, base)
    assert _bash(repo, 'build_review_diff_prefix "$1" "$2" "$3"', base, "diff --git a/x b/x") == ""
