from pathlib import Path
from types import SimpleNamespace

import pytest

from app.services import browser_task_gateway as browser_gateway
from app.services.agent_vault_service import _row_to_credential, normalize_origin
from app.services.browser_task_gateway import (
    _approval_token_audit_payload,
    _permission_decision_audit_payload,
    _scope_allows_action,
    _target_supports_self_hosted_capture,
    _task_to_dict,
)
from app.services.browser_recipe_registry import (
    build_recipe_concurrency_key,
    build_resource_claim,
    build_recipe_dry_run_plan,
    compute_recipe_hash,
    evaluate_recipe_run_admission,
    normalize_concurrency_policy,
    normalize_recipe_payload,
    normalize_resource_policy,
)
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


def test_permission_policy_routes_captcha_model_analysis_to_approval():
    decision = classify_browser_action(
        "captcha_model_analysis",
        "read captcha for approved same-page automation",
        {"challenge_kind": "captcha", "captcha_value_source": "vision"},
    )

    assert decision.decision == "ask"
    assert decision.risk_level == "high"


def test_approval_scope_allows_approved_captcha_model_analysis():
    allowed, reason = _scope_allows_action(
        {
            "origin": "https://boss.ddangyo.com",
            "challenge_kinds": ["captcha"],
            "allow_model_challenge_analysis": True,
        },
        action_type="captcha_model_analysis",
        origin="https://boss.ddangyo.com/login",
        payload={"challenge_kind": "captcha", "captcha_value_source": "vision"},
    )

    assert allowed is True
    assert reason == "approved_scope_match"


def test_approval_scope_blocks_unapproved_captcha_model_analysis():
    allowed, reason = _scope_allows_action(
        {"origin": "https://boss.ddangyo.com", "challenge_kinds": ["captcha"]},
        action_type="captcha_model_analysis",
        origin="https://boss.ddangyo.com/login",
        payload={"challenge_kind": "captcha", "captcha_value_source": "vision"},
    )

    assert allowed is False
    assert reason == "model_challenge_analysis_not_approved"


def test_permission_decision_audit_payload_records_actor_scope_without_secret():
    row = {
        "work_key": "yeoljeong-delivery-ddangyo",
        "origin": "https://boss.ddangyo.com",
        "action_type": "captcha_model_analysis",
        "action_summary": "read captcha for approved automation",
        "approval_scope": {"origin": "https://boss.ddangyo.com", "password": "do-not-log"},
        "max_executions": 3,
        "decided_at": SimpleNamespace(isoformat=lambda: "2026-08-28T08:07:00+09:00"),
    }

    payload = _permission_decision_audit_payload(
        row,
        request_id="req-1",
        decision="approved",
        decided_by="ceo@example.com",
        reason="CEO approved ddangyo page automation",
        approval_scope=None,
        max_executions=None,
        approval_token_issued=True,
    )

    assert payload["decided_by"] == "ceo@example.com"
    assert payload["origin"] == "https://boss.ddangyo.com"
    assert payload["approval_scope"]["password"] == "***MASKED***"
    assert payload["approval_token_issued"] is True


def test_approval_token_audit_payload_records_use_without_token_or_value():
    row = {
        "request_id": "00000000-0000-0000-0000-000000000001",
        "created_by": "ceo@example.com",
        "created_at": SimpleNamespace(isoformat=lambda: "2026-08-28T08:08:00+09:00"),
        "work_key": "yeoljeong-delivery-ddangyo",
        "origin": "https://boss.ddangyo.com",
        "action_summary": "approved captcha automation",
        "approval_scope": {"challenge_kinds": ["captcha"], "captcha_value": "2468"},
        "max_executions": 2,
        "used_executions": 0,
        "expires_at": SimpleNamespace(isoformat=lambda: "2026-08-28T08:18:00+09:00"),
    }

    payload = _approval_token_audit_payload(
        row,
        action_type="captcha_model_analysis",
        origin="https://boss.ddangyo.com/login",
        selector="#captcha",
        reason="approved_scope_match",
        status="approved",
        used_executions=1,
    )

    assert payload["approved_by"] == "ceo@example.com"
    assert payload["approved_origin"] == "https://boss.ddangyo.com"
    assert payload["actual_origin"] == "https://boss.ddangyo.com/login"
    assert payload["used_executions"] == 1
    assert "approval_token" not in payload
    assert payload["approval_scope"]["captcha_value"] == "***MASKED***"


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


def test_managed_browser_profile_is_origin_scoped():
    first = profile_info("AADS CEO", "https://aads.newtalk.kr/chat")
    second = profile_info("AADS CEO", "https://aads.newtalk.kr/agent-vault?work_key=aads")

    assert first["origin"] == "https://aads.newtalk.kr"
    assert first["profile_key"] == second["profile_key"]
    assert first["profile_dir"] == second["profile_dir"]


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


def test_self_hosted_live_capture_only_accepts_http_targets():
    assert _target_supports_self_hosted_capture("https://aads.newtalk.kr/browser-tasks") is True
    assert _target_supports_self_hosted_capture("http://localhost:8100/health") is True
    assert _target_supports_self_hosted_capture("about:blank") is False
    assert _target_supports_self_hosted_capture("file:///tmp/report.html") is False


@pytest.mark.asyncio
async def test_live_frame_capture_prefers_self_hosted_playwright(monkeypatch):
    tenant_id = "00000000-0000-0000-0000-000000000002"
    task_id = "00000000-0000-0000-0000-000000000001"

    async def fake_get_browser_task(*, tenant_id: str, task_id: str):
        return {
            "id": task_id,
            "tenant_id": tenant_id,
            "work_key": "ohvis-login",
            "target_url": "https://aads.newtalk.kr/login",
            "current_step": "login",
        }

    async def fake_self_hosted(task):
        return {"status": "captured", "source": "self_hosted_playwright", "frame": {"task_id": task["id"]}}

    async def fail_pc_agent(**kwargs):
        raise AssertionError("pc agent should not be called when self-hosted capture succeeds")

    monkeypatch.setattr(browser_gateway, "get_browser_task", fake_get_browser_task)
    monkeypatch.setattr(browser_gateway, "_capture_self_hosted_playwright_frame", fake_self_hosted)
    monkeypatch.setattr(browser_gateway, "_capture_pc_agent_frame", fail_pc_agent)

    result = await browser_gateway.capture_browser_task_live_frame(tenant_id=tenant_id, task_id=task_id)

    assert result["status"] == "captured"
    assert result["source"] == "self_hosted_playwright"


@pytest.mark.asyncio
async def test_live_frame_capture_uses_pc_agent_as_fallback(monkeypatch):
    tenant_id = "00000000-0000-0000-0000-000000000002"
    task_id = "00000000-0000-0000-0000-000000000001"

    async def fake_get_browser_task(*, tenant_id: str, task_id: str):
        return {
            "id": task_id,
            "tenant_id": tenant_id,
            "work_key": "pc-only",
            "target_url": "about:blank",
            "current_step": "legacy session",
        }

    async def fake_self_hosted(task):
        return {"status": "skipped", "reason": "unsupported_target_url"}

    async def fake_pc_agent(*, tenant_id: str, task: dict):
        return {"status": "captured", "source": "pc_agent_browser_screenshot", "frame": {"task_id": task["id"]}}

    monkeypatch.setattr(browser_gateway, "get_browser_task", fake_get_browser_task)
    monkeypatch.setattr(browser_gateway, "_capture_self_hosted_playwright_frame", fake_self_hosted)
    monkeypatch.setattr(browser_gateway, "_capture_pc_agent_frame", fake_pc_agent)

    result = await browser_gateway.capture_browser_task_live_frame(tenant_id=tenant_id, task_id=task_id)

    assert result["status"] == "captured"
    assert result["source"] == "pc_agent_browser_screenshot"
    assert result["fallback_from"]["reason"] == "unsupported_target_url"


def test_browser_recipe_normalizes_concurrency_and_resource_policy():
    recipe = normalize_recipe_payload(
        {
            "recipe_id": "delivery.ddangyo.sales_collect",
            "version": "v1",
            "allowed_origins": ["https://boss.ddangyo.com/login", "https://boss.ddangyo.com/dashboard"],
            "work_key_template": "ddangyo sales",
            "concurrency_policy": {"max_parallel_runs": 50, "queue_strategy": "bad", "conflict_keys": []},
            "resource_policy": {"runtime": "self_hosted_playwright", "max_memory_mb": 64},
        }
    )

    assert recipe["allowed_origins"] == ["https://boss.ddangyo.com"]
    assert recipe["work_key_template"] == "ddangyo-sales"
    assert recipe["concurrency_policy"] == {
        "max_parallel_runs": 20,
        "queue_strategy": "fifo",
        "conflict_keys": ["work_key", "origin"],
    }
    assert recipe["resource_policy"]["runtime"] == "self_hosted_playwright"
    assert recipe["resource_policy"]["max_memory_mb"] == 256


def test_browser_recipe_hash_is_stable_for_equivalent_payloads():
    payload = {
        "recipe_id": "delivery.ddangyo.sales_collect",
        "version": "v1",
        "allowed_origins": ["https://boss.ddangyo.com"],
        "resource_policy": {"runtime": "pc_agent"},
    }
    first = normalize_recipe_payload(payload)
    second = normalize_recipe_payload({**payload, "title": "Different display title"})

    assert compute_recipe_hash(first) == compute_recipe_hash(second)


def test_browser_recipe_dry_run_reports_approval_and_runtime_budget():
    plan = build_recipe_dry_run_plan(
        {
            "recipe_id": "delivery.ddangyo.sales_collect",
            "version": "v1",
            "allowed_origins": ["https://boss.ddangyo.com"],
            "resource_policy": {
                "runtime": "self_hosted_playwright",
                "max_browser_contexts": 3,
                "max_memory_mb": 2048,
                "max_runtime_seconds": 1200,
            },
            "concurrency_policy": {"max_parallel_runs": 3, "queue_strategy": "priority"},
            "risk_actions": [
                {
                    "action_type": "captcha_model_analysis",
                    "summary": "read captcha for approved automation",
                    "payload": {"challenge_kind": "captcha", "captcha_value_source": "vision"},
                },
                {"action_type": "copy", "summary": "copy password to clipboard"},
            ],
            "capture_rules": {"dom_table": True},
            "upload_rules": {"file_hash_required": True},
        },
        target_url="https://boss.ddangyo.com/login",
    )

    assert plan["runtime"] == "self_hosted_playwright"
    assert plan["concurrency_policy"]["max_parallel_runs"] == 3
    assert plan["resource_policy"]["max_memory_mb"] == 2048
    assert len(plan["required_approvals"]) == 1
    assert len(plan["blocked_actions"]) == 1
    assert plan["artifact_capture"] is True
    assert plan["upload_enabled"] is True


def test_browser_recipe_policy_helpers_clamp_values():
    assert normalize_concurrency_policy({"max_parallel_runs": 0})["max_parallel_runs"] == 1
    assert normalize_resource_policy({"runtime": "unknown", "artifact_budget_mb": 99999}) == {
        "runtime": "auto",
        "max_browser_contexts": 1,
        "max_memory_mb": 1024,
        "max_runtime_seconds": 900,
        "artifact_budget_mb": 10240,
    }


def test_browser_recipe_concurrency_key_uses_configured_conflict_keys():
    recipe = {
        "recipe_id": "delivery.ddangyo.sales_collect",
        "version": "v1",
        "service": "delivery",
        "allowed_origins": ["https://boss.ddangyo.com"],
        "work_key_template": "ddangyo sales",
        "concurrency_policy": {"conflict_keys": ["service", "origin"]},
    }

    assert build_recipe_concurrency_key(recipe, target_url="https://boss.ddangyo.com/login") == (
        "service:delivery|origin:https://boss.ddangyo.com"
    )


def test_browser_recipe_admission_starts_when_capacity_available():
    recipe = {
        "recipe_id": "delivery.ddangyo.sales_collect",
        "version": "v1",
        "allowed_origins": ["https://boss.ddangyo.com"],
        "concurrency_policy": {"max_parallel_runs": 2},
        "resource_policy": {"runtime": "self_hosted_playwright", "max_memory_mb": 1536},
    }

    admission = evaluate_recipe_run_admission(recipe, active_runs=1, target_url="https://boss.ddangyo.com/login")

    assert admission["decision"] == "start"
    assert admission["status"] == "running"
    assert admission["resource_claim"] == {
        "runtime": "self_hosted_playwright",
        "browser_contexts": 1,
        "memory_mb": 1536,
        "runtime_seconds": 900,
        "artifact_budget_mb": 256,
    }


def test_browser_recipe_admission_queues_or_rejects_on_conflict():
    base = {
        "recipe_id": "delivery.ddangyo.sales_collect",
        "version": "v1",
        "allowed_origins": ["https://boss.ddangyo.com"],
        "concurrency_policy": {"max_parallel_runs": 1, "queue_strategy": "fifo"},
    }

    assert evaluate_recipe_run_admission(base, active_runs=1)["decision"] == "queue"
    assert evaluate_recipe_run_admission(
        {**base, "concurrency_policy": {"max_parallel_runs": 1, "queue_strategy": "reject_on_conflict"}},
        active_runs=1,
    )["decision"] == "reject"


def test_browser_recipe_resource_claim_matches_budget_fields():
    recipe = {
        "recipe_id": "bank.statement_collect",
        "version": "v1",
        "allowed_origins": ["https://bank.example.com"],
        "resource_policy": {
            "runtime": "pc_agent",
            "max_browser_contexts": 2,
            "max_memory_mb": 2048,
            "max_runtime_seconds": 1800,
            "artifact_budget_mb": 512,
        },
    }

    assert build_resource_claim(recipe) == {
        "runtime": "pc_agent",
        "browser_contexts": 2,
        "memory_mb": 2048,
        "runtime_seconds": 1800,
        "artifact_budget_mb": 512,
    }
