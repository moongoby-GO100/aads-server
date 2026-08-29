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


def test_pipeline_runner_service_requires_screen_e2e_for_visual_work():
    service = (ROOT / "app" / "services" / "pipeline_runner_service.py").read_text(encoding="utf-8")

    assert "브라우저 E2E/화면 검증" in service
    assert "UI, 로그인, 문서/파일 열람, 차트, 대시보드, 프론트 라우트, 캡처가 필요한 작업은 필수" in service
    assert "브라우저 E2E 미실행, API 검증으로 대체" in service
    assert "화면 검증 필수 작업에서 캡처/스냅샷과 폴백 검증이 모두 없으면 완료 보고 금지" in service


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


def test_pipeline_runner_passes_parallel_group_to_work_lock_and_run_job():
    script = _read_script("pipeline-runner.sh")

    assert "RETURNING job_id, project" in script
    assert "COALESCE(parallel_group,'')" in script
    assert "read -r job_id project instruction session_id max_cycles job_model job_size parallel_group" in script
    assert 'run_job "$job_id" "$project" "$instruction" "$session_id" "${max_cycles:-3}" "${job_model:-litellm:minimax-m2.7}" "${job_size:-M}" "${parallel_group:-}" &' in script
    assert "work_lock_scope_param=\"&scope=${parallel_group}\"" in script
    assert '_release_work_lock "$project" "$job_id" "$parallel_group"' in script


def test_pipeline_runner_general_claim_uses_admin_model_column():
    script = _read_script("pipeline-runner.sh")

    assert "model_return_expr=\"COALESCE(NULLIF(worker_model, ''), NULLIF(model, ''), 'auto')\"" in script
    assert "get_db_model_cycle \"$job_size\"" in script


def test_pipeline_runner_allows_codex_56_cli_models_without_fallback():
    script = _read_script("pipeline-runner.sh")

    assert "gpt-5.6-luna" in script
    assert "gpt-5.6-sol" in script
    assert "gpt-5.6-terra" in script
    assert "default|gpt-5.6-luna|gpt-5.6-sol|gpt-5.6-terra|gpt-5.5" in script


def test_pipeline_runner_records_actual_changed_files_to_db():
    script = _read_script("pipeline-runner.sh")

    assert "record_actual_changed_files()" in script
    assert "actual_changed_files=" in script
    assert "git diff --name-only" in script
    assert "git ls-files --others --exclude-standard" in script
    assert "actual_changed_files_recorded" in script
    assert "worktree_path" in script


def test_pipeline_runner_classifies_auth_recovery_before_generic_errors():
    script = _read_script("pipeline-runner.sh")
    marker_block = script[script.index("classify_error()") : script.index("codex_auth_disabled_until()")]

    assert marker_block.index("invalid_refresh_token") < marker_block.index('echo "timeout"')
    assert marker_block.index("login_required") < marker_block.index('echo "auth_error"')
    assert marker_block.index("auth_expired") < marker_block.index('echo "auth_error"')
    assert "persist_auth_recovery()" in script
    assert '"awaiting_user_auth"' in script
    assert '"auth_recovery_pending"' in script


def test_pipeline_runner_auth_recovery_metadata_is_bounded_and_schema_optional():
    script = _read_script("pipeline-runner.sh")

    assert "information_schema.columns" in script
    assert "auth_recovery_state" in script
    assert "auth_recovery_metadata" in script
    assert "'max_retries'" in script
    assert "'retry_after_seconds'" in script
    assert "'bounded',true" in script
