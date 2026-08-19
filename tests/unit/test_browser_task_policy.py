from pathlib import Path
from types import SimpleNamespace

import pytest

from app.services.agent_vault_service import _row_to_credential, normalize_origin
from app.services.browser_task_gateway import _task_to_dict
from app.services.browser_permission_policy import classify_browser_action, mask_sensitive_value
from app.services.managed_browser import profile_info


def test_permission_policy_denies_secret_reveal():
    decision = classify_browser_action("copy", "copy password to clipboard")

    assert decision.decision == "deny"
    assert decision.risk_level == "critical"


@pytest.mark.parametrize("summary", ["payment approval", "delete account", "upload invoice", "send message"])
def test_permission_policy_asks_for_risky_actions(summary):
    decision = classify_browser_action("click", summary)

    assert decision.decision == "ask"
    assert decision.risk_level == "high"


def test_permission_policy_allows_routine_read_actions():
    decision = classify_browser_action("snapshot", "read dashboard status")

    assert decision.decision == "allow"


def test_mask_sensitive_value_recurses_without_masking_safe_fields():
    payload = {
        "username": "ceo",
        "password": "secret",
        "nested": {"api_key": "k", "memo": "keep"},
        "items": [{"otp": "123456"}],
    }

    masked = mask_sensitive_value(payload)

    assert masked["username"] == "ceo"
    assert masked["password"] == "***MASKED***"
    assert masked["nested"]["api_key"] == "***MASKED***"
    assert masked["nested"]["memo"] == "keep"
    assert masked["items"][0]["otp"] == "***MASKED***"


def test_normalize_origin_uses_scheme_and_host_only():
    assert normalize_origin("https://example.com/path?a=1") == "https://example.com"


def test_managed_browser_profile_info_is_stable_and_isolated():
    first = profile_info("AADS CEO", "https://aads.newtalk.kr/chat")
    second = profile_info("AADS CEO", "https://aads.newtalk.kr/chat")

    assert first == second
    assert first["work_key"] == "AADS-CEO"
    assert first["isolated_profile"] is True


def test_migration_contains_no_destructive_table_ops():
    migration = Path("migrations/122_ohvis_managed_browser_agent_vault.sql").read_text()
    upper = migration.upper()

    assert "DROP TABLE" not in upper
    assert "TRUNCATE" not in upper
    assert "DELETE FROM" not in upper


def test_agent_vault_credential_metadata_jsonb_string_is_dict(monkeypatch):
    monkeypatch.setattr("app.services.agent_vault_service.decrypt_value", lambda value: f"dec:{value}")
    row = {
        "id": "00000000-0000-0000-0000-000000000001",
        "tenant_id": "00000000-0000-0000-0000-000000000002",
        "work_key": "e2e",
        "origin": "https://aads.newtalk.kr",
        "label": "default",
        "username_enc": "user",
        "password_enc": "pass",
        "metadata": '{"source":"e2e","password_note":"***MASKED***"}',
        "is_active": True,
        "last_used_at": None,
        "created_at": None,
        "updated_at": None,
    }

    credential = _row_to_credential(row)

    assert credential["metadata"] == {"source": "e2e", "password_note": "***MASKED***"}
    assert credential["password"] == "********"


def test_browser_task_result_jsonb_string_is_dict():
    row = {
        "id": "00000000-0000-0000-0000-000000000001",
        "tenant_id": "00000000-0000-0000-0000-000000000002",
        "user_id": "admin",
        "session_id": None,
        "work_key": "e2e",
        "target_url": "https://aads.newtalk.kr/chat",
        "status": "completed",
        "current_step": "done",
        "requires_approval": False,
        "approval_request_id": None,
        "result": '{"e2e":true,"token":"***MASKED***"}',
        "error": "",
        "created_at": SimpleNamespace(isoformat=lambda: "2026-08-20T04:17:00+09:00"),
        "updated_at": SimpleNamespace(isoformat=lambda: "2026-08-20T04:18:00+09:00"),
    }

    task = _task_to_dict(row)

    assert task["result"] == {"e2e": True, "token": "***MASKED***"}
