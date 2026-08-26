from pathlib import Path


def test_main_registers_baemin_full_backfill_auto_collect_jobs():
    source = Path("app/main.py").read_text(encoding="utf-8")

    assert "delivery_auto_collect_baemin_full_backfill" in source
    assert "delivery_auto_collect_pc_agent_catchup" in source
    assert "delivery_auto_collect_peer_agent" in source
    assert '"services": ["baemin"]' in source
    assert '"mode": "full_backfill"' in source


def test_baemin_full_backfill_payload_contract_is_resource_limited():
    source = Path("app/main.py").read_text(encoding="utf-8")

    assert "YEOLJEONG_BAEMIN_BACKFILL_FROM" in source
    assert "YEOLJEONG_BAEMIN_BACKFILL_MAX_ORDERS" in source
    assert "YEOLJEONG_BAEMIN_BACKFILL_MAX_REVIEWS" in source
    assert "YEOLJEONG_BAEMIN_BACKFILL_WINDOW_DAYS" in source
    assert "YEOLJEONG_BAEMIN_BACKFILL_BATCH_LIMIT" in source
    assert '"window_days": _env_int("YEOLJEONG_BAEMIN_BACKFILL_WINDOW_DAYS", 1)' in source
    assert '"max_backfill_runs": _env_int("YEOLJEONG_BAEMIN_BACKFILL_BATCH_LIMIT", 1)' in source
    assert '"skip_financial_accounts": True' in source
    assert '"require_pc_agent": True' in source


def test_delivery_auto_collect_system_user_has_internal_admin_claim():
    source = Path("app/main.py").read_text(encoding="utf-8")

    assert '"user_id": "system-daemon"' in source
    assert '"user_role": "system"' in source
    assert '"is_internal_admin": True' in source


def test_bank_auto_collect_is_single_owner_and_locked():
    source = Path("app/main.py").read_text(encoding="utf-8")

    assert "_is_active_api_container_for_background_jobs()" in source
    assert "bank_auto_collect_skip: inactive_api_container" in source
    assert "YEOLJEONG_BANK_AUTO_COLLECT_LOCK_PATH" in source
    assert ".bank_auto_collect.lock" in source
    assert "bank_auto_collect_skip: already_running" in source
