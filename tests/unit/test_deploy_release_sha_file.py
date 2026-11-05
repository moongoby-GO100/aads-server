"""deploy.sh prepare_release_context — 릴리스 컨텍스트에 .release_sha 를 쓰는 행동 검증."""
import re
import subprocess
from pathlib import Path

import pytest

DEPLOY = Path(__file__).parents[2] / "deploy.sh"


def _func_source() -> str:
    text = DEPLOY.read_text()
    m = re.search(r"^prepare_release_context\(\) \{\n.*?^\}\n", text, re.S | re.M)
    assert m, "prepare_release_context not found"
    return m.group(0)


def _run(tmp_path, sha):
    repo = tmp_path / "repo"
    repo.mkdir()
    env = {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t",
           "GIT_COMMITTER_EMAIL": "t@t", "PATH": "/usr/bin:/bin:/usr/local/bin", "HOME": str(tmp_path)}
    (repo / "a.txt").write_text("x")
    for cmd in (["git", "init", "-q"], ["git", "add", "a.txt"], ["git", "commit", "-qm", "i"]):
        subprocess.run(cmd, cwd=repo, env=env, check=True, capture_output=True)
    script = f"""
set -euo pipefail
COMPOSE_DIR={repo}
AADS_RELEASE_SHA={sha!r}
RELEASE_CONTEXT_DIR=""
cleanup_release_context() {{ :; }}
emit_release_context_manifest() {{ :; }}
require_release_context_within_limit() {{ :; }}
{_func_source()}
prepare_release_context >/dev/null
echo "$RELEASE_CONTEXT_DIR"
"""
    out = subprocess.run(["bash", "-c", script], env=env, check=True, capture_output=True, text=True).stdout
    return Path(out.strip().splitlines()[-1])


def test_release_sha_file_written(tmp_path):
    ctx = _run(tmp_path, "abc123def456")
    try:
        assert (ctx / "a.txt").exists()
        assert (ctx / ".release_sha").read_text() == "abc123def456\n"
    finally:
        subprocess.run(["rm", "-rf", str(ctx)])


@pytest.mark.parametrize("sha", ["unknown", ""])
def test_release_sha_file_absent_when_unknown(tmp_path, sha):
    ctx = _run(tmp_path, sha)
    try:
        assert (ctx / "a.txt").exists()
        assert not (ctx / ".release_sha").exists()
    finally:
        subprocess.run(["rm", "-rf", str(ctx)])
