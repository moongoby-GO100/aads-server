"""Runner-owned Codex base synchronization, without a network service."""
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]


def function(source, name):
    start = source.index(f"{name}() {{")
    return source[start:source.index("\n}\n", start) + 3]


@pytest.mark.parametrize("name", ["pipeline-runner.sh", "pipeline-runner.sh.local"])
def test_sync_and_rebase_guards(name, tmp_path):
    source = (ROOT / "scripts" / name).read_text()
    script = tmp_path / "test.sh"
    script.write_text("RUNNER_BASE_BRANCH=main\n" + function(source, "runner_codex_sync_base") +
                      function(source, "runner_codex_rebase_changed_base") + r'''
log() { :; }
git_fetch_origin_with_retry() { [[ "$FETCH_OK" == yes ]]; }
git() {
    shift 2
    case "$1" in
        rev-parse) if [[ "$2" == --show-toplevel ]]; then printf '%s\n' "$TOP"; else printf '%s\n' new; fi ;;
        merge-base) return "${ANCESTOR_RC:-0}" ;;
        rebase) printf '%s\n' "$*" >> "$CALLS"; printf '%s\n' "${REBASE_OUTPUT:-}"; return "${REBASE_RC:-0}" ;;
    esac
}
export TOP="$1" CALLS="$2" FETCH_OK=yes
[[ "$(runner_codex_sync_base "$TOP")" == new ]]
FETCH_OK=no; ! runner_codex_sync_base "$TOP"; FETCH_OK=yes
runner_codex_rebase_changed_base "$TOP" "$TOP" old old
[[ ! -e "$CALLS" ]]
! runner_codex_rebase_changed_base "$TOP" /tmp old new
runner_codex_rebase_changed_base "$TOP" "$TOP" old new
grep -q -- '--autostash --onto new old HEAD' "$CALLS"
REBASE_OUTPUT='Applying autostash resulted in conflicts'; ! runner_codex_rebase_changed_base "$TOP" "$TOP" old new
ANCESTOR_RC=1; ! runner_codex_rebase_changed_base "$TOP" "$TOP" old new
''')
    result = subprocess.run(["bash", str(script), str(tmp_path), str(tmp_path / "calls")], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr


def test_runner_copies_and_failed_initial_sync_fallback():
    a, b = [(ROOT / "scripts" / name).read_text() for name in ("pipeline-runner.sh", "pipeline-runner.sh.local")]
    for name in ("runner_codex_sync_base", "runner_codex_rebase_changed_base"):
        assert function(a, name) == function(b, name)
    for source in (a, b):
        assert 'RUNNER_BASE_BRANCH="main"' in source
        assert 'codex_base_before="${codex_base_before:-$pre_exec_sha}"' in source
        assert '"${codex_base_before:-$pre_exec_sha}" "$codex_base_after"' in source
