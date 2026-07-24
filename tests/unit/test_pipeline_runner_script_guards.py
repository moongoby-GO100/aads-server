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


def test_pipeline_runner_service_codex_invocation_avoids_deprecated_full_auto():
    service = (ROOT / "app" / "services" / "pipeline_runner_service.py").read_text(encoding="utf-8")

    assert '"--sandbox",' in service
    assert '"workspace-write",' in service
    assert '"--full-auto",' not in service


def test_pipeline_runner_claims_legacy_coding_phase_queue_items():
    script = _read_script("pipeline-runner.sh")

    assert "p.status='queued' AND p.phase IN ('queued','coding')" in script
    assert "status='queued' AND phase IN ('queued','coding')" in script


def test_pipeline_runner_claude_oauth_avoids_litellm_proxy_env():
    script = _read_script("pipeline-runner.sh")
    legacy_api_key_name = "ANTHROPIC_" + "API_KEY"

    assert "export CLAUDE_CODE_OAUTH_TOKEN=\"$TOKEN_2\"" in script
    assert "export CLAUDE_CODE_OAUTH_TOKEN=\"$TOKEN_1\"" in script
    assert f"unset {legacy_api_key_name} 2>/dev/null || true" in script
    assert "unset ANTHROPIC_BASE_URL 2>/dev/null || true" in script


def test_pipeline_runner_maps_internal_claude_ids_to_cli_aliases():
    script = _read_script("pipeline-runner.sh")

    assert "normalize_claude_cli_model()" in script
    assert "claude-sonnet*|sonnet)" in script
    assert "claude-haiku*|haiku)" in script
    assert "claude-opus*|opus)" in script
    assert "claude_cli_model=$(normalize_claude_cli_model \"$current_model\")" in script
    assert "local claude_args=(--model \"$claude_cli_model\" -p --output-format text)" in script


def test_pipeline_runner_read_only_no_diff_completes_without_approval():
    script = _read_script("pipeline-runner.sh")

    assert "is_read_only_instruction()" in script
    assert "NO_CHANGES_READ_ONLY job=$job_id" in script
    assert "status='done', phase='done'" in script
    assert "completed_at=NOW()" not in script
    assert "읽기[[:space:]]*전용" in script
    assert "read-only 작업 완료 — 변경사항 0건이 정상 조건" in script
