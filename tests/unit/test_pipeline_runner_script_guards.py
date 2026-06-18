from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]


def _read_script(name: str) -> str:
    return (ROOT / "scripts" / name).read_text(encoding="utf-8")


def test_pipeline_runner_scripts_keep_git_diff_precheck_guards():
    for script_name in ("pipeline-runner.sh", "pipeline-runner.sh.local"):
        script = _read_script(script_name)

        assert "looks_like_git_diff()" in script
        assert "NO_CHANGES job=$job_id" in script
        assert "awaiting_approval 차단" in script
        assert "INVALID_GIT_DIFF" in script
        assert "AI_REVIEW_PRECHECK_FAIL" in script
        assert "review_needs_retry=\"true\"" in script


def test_local_pipeline_runner_template_stays_synced_with_primary_runner():
    primary = _read_script("pipeline-runner.sh")
    local_template = _read_script("pipeline-runner.sh.local")

    assert local_template == primary


def test_pipeline_runner_cli_invocation_avoids_known_noninteractive_failures():
    script = _read_script("pipeline-runner.sh")

    assert "exec --sandbox workspace-write --ephemeral -C \"$workdir\"" in script
    assert "exec --full-auto --ephemeral -C \"$workdir\"" not in script
    assert "if [[ \"${EUID:-$(id -u)}\" -ne 0 ]]; then" in script
    assert "claude_args+=(--dangerously-skip-permissions)" in script
    assert "claude --model \"$current_model\" --dangerously-skip-permissions" not in script
