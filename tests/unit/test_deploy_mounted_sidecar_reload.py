"""Static contract tests for mounted-sidecar reload during blue/green cutover."""

from pathlib import Path


DEPLOY = (Path(__file__).parents[2] / "deploy.sh").read_text(encoding="utf-8")


def _function_body(name: str) -> str:
    return DEPLOY.split(f"{name}() {{", 1)[1].split("\n}\n", 1)[0]


def test_required_absent_sidecar_fails_closed():
    body = _function_body("reload_mounted_sidecars")
    assert 'DEPLOY_MOUNTED_SIDECARS:-yeoljeong-finance' in body
    assert 'failure="required sidecar absent: ${sidecar}"' in body
    assert 'deploy_phase_end "mounted_sidecar_reload" "error" "$failure"' in body
    assert "return 1" in body


def test_optional_absent_sidecar_is_skipped():
    body = _function_body("reload_mounted_sidecars")
    assert 'DEPLOY_MOUNTED_SIDECAR_OPTIONAL:-' in body
    assert 'if [[ "$is_optional" == "true" ]]; then' in body
    assert 'optional mounted sidecar absent: ${sidecar}' in body


def test_public_health_failure_is_a_failed_reload_phase():
    body = _function_body("reload_mounted_sidecars")
    assert 'DEPLOY_SIDECAR_PUBLIC_CHECK_URL:-http://127.0.0.1/api/v1/yeoljeong-finance/health/live' in body
    assert 'DEPLOY_SIDECAR_HEALTH_WAIT:-60' in body
    assert "200|401|403" in body
    assert 'failure="mounted sidecar public health failed: ${sidecar} (${public_url}, http=${public_code})"' in body
    assert 'notify "❌ Blue-Green 인증 실패: ${failure}"' in body


def test_sidecar_internal_warmup_has_bounded_polling():
    body = _function_body("reload_mounted_sidecars")
    assert 'DEPLOY_SIDECAR_HEALTH_INTERVAL:-2' in body
    assert 'until curl -sf --max-time 5 "$internal_url"' in body
    assert 'health_elapsed=$((health_elapsed + health_interval))' in body


def test_nginx_lock_is_released_before_sidecar_restart_wait():
    cutover_success = DEPLOY.index('deploy_phase_end "nginx_cutover" "success"')
    reload_call = DEPLOY.index("reload_mounted_sidecars", cutover_success)
    release_lock = DEPLOY.index("release_nginx_switch_lock", cutover_success)
    standby_sync = DEPLOY.index("sync_standby_slot_after_drain", reload_call)
    assert release_lock < reload_call < standby_sync
