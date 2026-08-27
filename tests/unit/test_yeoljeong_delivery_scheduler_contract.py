from pathlib import Path


def test_main_registers_baemin_full_backfill_auto_collect_jobs():
    source = Path("app/main.py").read_text(encoding="utf-8")

    assert "delivery_auto_collect_baemin_full_backfill" in source
    assert "delivery_auto_collect_pc_agent_catchup" in source
    assert "delivery_auto_collect_peer_agent" in source
    assert '"services": ["baemin"]' in source
    assert '"mode": "full_backfill"' in source


def test_main_registers_coupangeats_auto_collect_catchup_job():
    source = Path("app/main.py").read_text(encoding="utf-8")

    assert 'DEFAULT_DELIVERY_AUTO_COLLECT_SERVICES = ["coupangeats", "yogiyo", "ddangyo", "baemin"]' in source
    assert "selected_services = _delivery_auto_collect_services(services)" in source
    assert "delivery_auto_collect_coupangeats_catchup" in source
    assert "_delivery_auto_collect_coupangeats_catchup_due" in source
    assert '"reason": "coupangeats_catchup"' in source
    assert '"services": ["coupangeats"]' in source


def test_delivery_auto_collect_daemon_runs_child_with_timeout():
    source = Path("app/main.py").read_text(encoding="utf-8")

    assert "scripts\" / \"yeoljeong_auto_collect.py" in source
    assert "YEOLJEONG_DELIVERY_AUTO_COLLECT_TIMEOUT_SECONDS" in source
    assert "delivery_auto_collect_timeout" in source
    assert "--attempt-timeout-seconds" in source
    assert "--browser-agent-id" in source
    assert "--force-recreate-sessions" in source


def test_delivery_auto_collect_daemon_honors_dedicated_pc_agent_env():
    source = Path("app/main.py").read_text(encoding="utf-8")

    assert "YEOLJEONG_DELIVERY_AUTO_COLLECT_AGENT_ID" in source
    assert "YEOLJEONG_DELIVERY_AUTO_COLLECT_EXCLUDED_AGENT_IDS" in source
    assert "preferred_agent_id = os.getenv" in source
    assert "agent_id=preferred_agent_id" in source
    assert "delivery_auto_collect_skip: preferred_agent_excluded" in source
    assert "PC_AGENT_EXCLUDED" in source


def test_main_registers_coupangeats_catchup_auto_collect_job():
    source = Path("app/main.py").read_text(encoding="utf-8")
    cli_source = Path("scripts/yeoljeong_auto_collect.py").read_text(encoding="utf-8")

    assert "delivery_auto_collect_coupangeats_catchup" in source
    assert '"reason": "coupangeats_catchup"' in source
    assert '"services": ["coupangeats"]' in source
    assert 'service="coupangeats"' in source
    assert "delivery_auto_collect_catchup_skip: coupangeats_recent_or_running" in source
    assert 'DEFAULT_SERVICES = ("coupangeats", "yogiyo", "ddangyo", "baemin")' in cli_source


def test_baemin_full_backfill_payload_contract_is_resource_limited():
    source = Path("app/main.py").read_text(encoding="utf-8")

    assert "DEFAULT_BAEMIN_SECURITY_BLOCK_COOLDOWN_MINUTES = 45" in source
    assert "selected_services = [service for service in selected_services if service != \"baemin\"]" in source
    assert "YEOLJEONG_DELIVERY_COUPANGEATS_PRIORITY_OVER_BAEMIN" in source
    assert "_delivery_auto_collect_coupangeats_priority_active(statuses)" in source
    assert "delivery_auto_collect_skip: baemin_deferred_for_coupangeats_priority" in source
    assert "YEOLJEONG_BAEMIN_BACKFILL_FROM" in source
    assert "YEOLJEONG_BAEMIN_BACKFILL_MAX_ORDERS" in source
    assert "YEOLJEONG_BAEMIN_BACKFILL_MAX_REVIEWS" in source
    assert "YEOLJEONG_BAEMIN_BACKFILL_WINDOW_DAYS" in source
    assert "YEOLJEONG_BAEMIN_BACKFILL_BATCH_LIMIT" in source
    assert '"window_days": _env_int("YEOLJEONG_BAEMIN_BACKFILL_WINDOW_DAYS", 1)' in source
    assert '"max_backfill_runs": _env_int("YEOLJEONG_BAEMIN_BACKFILL_BATCH_LIMIT", 1)' in source
    assert '"skip_financial_accounts": True' in source
    assert '"require_pc_agent": True' in source
    assert "YEOLJEONG_BAEMIN_SECURITY_BLOCK_COOLDOWN_MINUTES" in source
    assert "YEOLJEONG_BAEMIN_FORCE_RECREATE_SESSIONS" in source
    assert "delivery_auto_collect_skip: baemin_security_block_cooldown" in source
    assert '"max_orders": _env_int("YEOLJEONG_BAEMIN_BACKFILL_MAX_ORDERS", 20)' in source


def test_delivery_auto_collect_system_user_has_internal_admin_claim():
    source = Path("app/main.py").read_text(encoding="utf-8")

    assert '"user_id": "system-daemon"' in source
    assert '"user_role": "system"' in source
    assert '"is_internal_admin": True' in source


def test_bank_auto_collect_is_single_owner_and_locked():
    source = Path("app/main.py").read_text(encoding="utf-8")

    assert "_is_active_api_container_for_background_jobs()" in source
    assert "bank_auto_collect_skip: inactive_api_container" in source
    assert "YEOLJEONG_DELIVERY_SYNC_LOCK_PATH" in source
    assert "bank_auto_collect_skip: delivery_sync_running" in source
    assert "YEOLJEONG_BANK_AUTO_COLLECT_LOCK_PATH" in source
    assert ".bank_auto_collect.lock" in source
    assert "bank_auto_collect_skip: already_running" in source


def test_delivery_auto_collect_is_single_active_slot_owner():
    source = Path("app/main.py").read_text(encoding="utf-8")

    delivery_block = source.split("async def _run_delivery_auto_collect", 1)[1].split(
        "async def _run_bank_auto_collect", 1
    )[0]

    assert "_is_active_api_container_for_background_jobs()" in delivery_block
    assert "delivery_auto_collect_skip: inactive_api_container" in delivery_block
    assert 'os.getenv("AADS_CONTAINER_NAME", "")' in delivery_block
    assert 'os.getenv("AADS_PUBLIC_PORT", "")' in delivery_block
