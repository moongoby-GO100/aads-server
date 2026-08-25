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
    assert '"skip_financial_accounts": True' in source
    assert '"require_pc_agent": True' in source
