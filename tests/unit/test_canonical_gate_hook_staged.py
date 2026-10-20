"""M3 — 정본 게이트 pre-commit 블록을 실제 `git commit`(스테이징된 fixture)으로 검증한다.

임시 저장소에 scripts/hooks/pre-commit 의 정본 게이트 블록만 hook 으로 설치하고, git 이 직접 hook 을
호출하게 한다(cwd·환경을 git 이 정한다). 지키려는 것:
  - docs/reports/*.md 는 경고·기록 대상이다. 에셋(png)·다른 경로는 아니다.
  - 기록은 워크트리별 git-dir 이 아니라 공용 git 디렉터리에 남는다(러너 워크트리 폐기 후에도 생존).
  - 스크립트가 없거나 죽으면 말없이 넘기지 않고 경고한다. 그래도 커밋은 막지 않는다(그림자 모드).
  - 미등록·실행 실패·enforce 값 어느 것도 커밋을 차단하지 않는다.
"""
from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parents[2]
_HOOK = _REPO / "scripts" / "hooks" / "pre-commit"
_SCRIPT = _REPO / "scripts" / "canonical_gate_commit.py"
_REPORT = "docs/reports/20261020_AADS_한글_보고서.md"


def _block() -> str:
    text = _HOOK.read_text(encoding="utf-8")
    return text[text.index("# ── 정본 게이트 (그림자 모드"):text.index("# ── 감사: hook 통과 서명")]


def _env(**extra: str) -> dict:
    env = {k: v for k, v in os.environ.items() if not k.startswith(("GIT_", "CANONICAL_GATE"))}
    env.update(
        GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@example.com",
        GIT_COMMITTER_NAME="t", GIT_COMMITTER_EMAIL="t@example.com",
    )
    env.update(extra)
    return env


def _git(cwd: Path, *args: str, env: dict | None = None, check: bool = True):
    res = subprocess.run(["git", *args], cwd=cwd, env=env or _env(), capture_output=True, text=True, timeout=60)
    if check and res.returncode != 0:
        raise AssertionError(f"git {args} failed: {res.stderr}")
    return res


@pytest.fixture()
def repo(tmp_path):
    root = tmp_path / "main"
    root.mkdir()
    _git(root, "init", "-q", "-b", "main")
    (root / "scripts").mkdir()
    (root / "scripts" / "canonical_gate_commit.py").write_text(_SCRIPT.read_text(encoding="utf-8"), encoding="utf-8")
    (root / "README.md").write_text("x", encoding="utf-8")
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "init")
    hook = root / ".git" / "hooks" / "pre-commit"
    hook.write_text(
        "#!/bin/bash\nYELLOW='\\033[1;33m'\nNC='\\033[0m'\n" + _block() + "\nexit 0\n", encoding="utf-8")
    hook.chmod(0o755)
    return root


def _stage(root: Path, rel: str, body: str = "내용") -> None:
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(body.encode("utf-8"))
    _git(root, "add", "-A")


def _log(root: Path) -> list[str]:
    p = root / ".git" / "canonical_gate.log"
    return p.read_text(encoding="utf-8").splitlines() if p.exists() else []


def test_report_markdown_commit_warns_logs_and_succeeds(repo):
    _stage(repo, _REPORT)
    res = _git(repo, "commit", "-q", "-m", "add report", check=False)
    assert res.returncode == 0
    assert _REPORT in res.stderr and "정본 게이트" in res.stderr and "차단 없음" in res.stderr
    lines = _log(repo)
    assert len(lines) == 1
    f = lines[0].split("\t")
    assert len(f) == 9 and f[0] == "CGv1" and f[2] == "commit" and f[4] == _REPORT
    assert f[5] == "unknown" and f[6] == "shadow" and f[7] == "A"
    assert _git(repo, "log", "-1", "--format=%s").stdout.strip() == "add report"


def test_assets_and_non_candidate_paths_do_not_warn(repo):
    _stage(repo, "docs/reports/shot.png", "png")
    _stage(repo, "docs/specs/x/spec.md")
    _stage(repo, "reports/20261020_old.md")
    res = _git(repo, "commit", "-q", "-m", "no candidates", check=False)
    assert res.returncode == 0 and "정본 게이트" not in res.stderr and _log(repo) == []


def test_existing_design_path_still_warns(repo):
    _stage(repo, "docs/design/20261020_AADS_설계.md")
    res = _git(repo, "commit", "-q", "-m", "design", check=False)
    assert res.returncode == 0 and "docs/design/20261020_AADS_설계.md" in res.stderr


def test_linked_worktree_commit_logs_to_common_dir(repo, tmp_path):
    wt = tmp_path / "wt-runner"
    _git(repo, "worktree", "add", "-q", "-b", "feat", str(wt))
    _stage(wt, _REPORT)
    res = _git(wt, "commit", "-q", "-m", "wt report", check=False)
    assert res.returncode == 0 and _REPORT in res.stderr
    assert len(_log(repo)) == 1, "공용 .git 에 남아야 한다"
    gitdir = Path(_git(wt, "rev-parse", "--git-dir").stdout.strip())
    assert gitdir != repo / ".git" and not (gitdir / "canonical_gate.log").exists(), "워크트리 전용 위치에 남으면 폐기 때 사라진다"
    _git(repo, "worktree", "remove", "--force", str(wt))
    assert len(_log(repo)) == 1, "워크트리를 지워도 기록은 남는다"


def test_missing_script_warns_but_does_not_block(repo):
    (repo / "scripts" / "canonical_gate_commit.py").unlink()
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "drop script", check=False)  # 삭제 커밋 자체도 통과
    _stage(repo, _REPORT)
    res = _git(repo, "commit", "-q", "-m", "report without gate script", check=False)
    assert res.returncode == 0
    assert "canonical_gate_commit.py 가 이 체크아웃에 없어" in res.stderr and "차단 없음" in res.stderr


def test_crashing_script_warns_but_does_not_block(repo):
    (repo / "scripts" / "canonical_gate_commit.py").write_text("import sys\nsys.exit(7)\n", encoding="utf-8")
    _stage(repo, _REPORT)
    res = _git(repo, "commit", "-q", "-m", "report crash", check=False)
    assert res.returncode == 0
    assert "실행 실패 또는 시간 초과(rc=7)" in res.stderr


def test_mode_off_is_silent_even_if_script_missing(repo):
    (repo / "scripts" / "canonical_gate_commit.py").unlink()
    _stage(repo, _REPORT)
    res = _git(repo, "commit", "-q", "-m", "off", env=_env(CANONICAL_GATE_MODE="off"), check=False)
    assert res.returncode == 0 and "정본 게이트" not in res.stderr and _log(repo) == []


@pytest.mark.parametrize("mode", ["shadow", "enforce", "bogus"])
def test_unregistered_document_never_blocks_in_any_mode(repo, mode):
    _stage(repo, _REPORT)
    res = _git(repo, "commit", "-q", "-m", f"mode {mode}", env=_env(CANONICAL_GATE_MODE=mode), check=False)
    assert res.returncode == 0, "미등록 때문에 차단하면 안 된다"
    assert len(_log(repo)) == 1


def test_hook_block_has_no_exit_that_could_block():
    block = _block()
    assert "exit 1" not in block and "exit 2" not in block
    assert "--no-verify" not in block


def test_candidate_patterns_stay_in_sync_and_cover_reports():
    from app.services.canonical_gate import CANDIDATE_PATH_RE, is_candidate_path

    ns: dict = {}
    exec(compile(_SCRIPT.read_text(encoding="utf-8").split("LOG_NAME")[0], "gate", "exec"), ns)  # noqa: S102
    assert ns["CANDIDATE_RE"].pattern == CANDIDATE_PATH_RE.pattern
    assert is_candidate_path(_REPORT) and not is_candidate_path("docs/reports/shot.png")
