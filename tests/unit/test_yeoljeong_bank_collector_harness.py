from __future__ import annotations

import importlib.util
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
HARNESS_PATH = ROOT / "app" / "services" / "yeoljeong_bank_collector_harness.py"


def _reload_harness(monkeypatch):
    monkeypatch.delenv("YEOLJEONG_BANK_AUTO_COLLECT_AGENT_ID", raising=False)
    monkeypatch.delenv("YEOLJEONG_BANK_BROWSER_AGENT_ID", raising=False)
    monkeypatch.setenv("YEOLJEONG_BANK_COLLECTOR_TRACE_ENABLED", "0")
    spec = importlib.util.spec_from_file_location("yeoljeong_bank_collector_harness_test", HARNESS_PATH)
    harness = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(harness)
    return harness


def test_normalize_shinhan_request_forces_easyview_and_bank_only(monkeypatch):
    harness = _reload_harness(monkeypatch)

    payload = harness.normalize_shinhan_windows_collector_request(
        {
            "services": ["baemin"],
            "portal_url": "https://bizbank.shinhan.com/main.html",
            "business_id": "biz-mia",
            "branch": "열정국밥_미아점",
            "browser_agent_id": "62405e70-e98",
            "bank_browser_timeout_seconds": 60,
        }
    )

    assert payload["services"] == ["shinhan_business"]
    assert payload["bank_only"] is True
    assert payload["skip_financial_accounts"] is False
    assert payload["require_pc_agent"] is True
    assert payload["browser_agent_id"] == "62405e70-e98"
    assert payload["portal_url"] == harness.SHINHAN_EASYVIEW_LOGIN_URL
    assert payload["login_url"] == harness.SHINHAN_EASYVIEW_LOGIN_URL
    assert payload["bank_browser_timeout_seconds"] == harness.DEFAULT_BROWSER_TIMEOUT_SECONDS


def test_missing_windows_agent_blocks_before_security_or_collection(monkeypatch):
    harness = _reload_harness(monkeypatch)

    called = {"security": 0, "collection": 0}
    monkeypatch.setattr(harness, "_graph_available", lambda: False)
    monkeypatch.setattr(harness, "_run_security_preflight", lambda *args, **kwargs: called.__setitem__("security", 1))
    monkeypatch.setattr(harness, "_run_collection", lambda *args, **kwargs: called.__setitem__("collection", 1))

    result = harness.run_shinhan_windows_collector_harness(
        {"business_id": "biz-mia", "branch": "열정국밥_미아점"},
        {"email": "system@aads.local", "is_admin": True},
    )

    assert result["status"] == "configuration_required"
    assert result["errors"] == ["WINDOWS_COLLECTOR_AGENT_REQUIRED"]
    assert result["completion"]["completed"] is False
    assert called == {"security": 0, "collection": 0}


def test_harness_runs_security_preflight_and_verifies_imported_rows(monkeypatch):
    harness = _reload_harness(monkeypatch)
    calls = {}

    monkeypatch.setattr(harness, "_graph_available", lambda: False)
    monkeypatch.setattr(
        harness,
        "_run_security_preflight",
        lambda payload, agent_id: {
            "checked": "1",
            "ready": True,
            "ahnlab_detected": "1",
            "veraport_detected": "1",
            "agent_id": agent_id,
        },
    )

    def fake_run_collection(payload, user, timeout_seconds):
        calls["payload"] = payload
        calls["timeout_seconds"] = timeout_seconds
        calls["user"] = user
        return {
            "bank_collections": [
                {
                    "service": "bank",
                    "bank_account_id": "bank-shinhan-mia",
                    "status": "completed",
                    "collected_rows": 2,
                    "imported_rows": 2,
                    "duplicate_rows": 0,
                    "error_code": "",
                }
            ],
            "bank_totals": {"imported_rows": 2, "collected_rows": 2, "duplicate_rows": 0},
        }

    monkeypatch.setattr(harness, "_run_collection", fake_run_collection)

    result = harness.run_shinhan_windows_collector_harness(
        {
            "business_id": "biz-mia",
            "branch": "열정국밥_미아점",
            "bank_account_id": "bank-shinhan-mia",
            "browser_agent_id": "62405e70-e98",
            "attempt_timeout_seconds": 777,
            "bank_browser_work_key": "yeoljeong-bank-shinhan-mia",
        },
        {"email": "system@aads.local", "is_admin": True},
        skip_security_preflight=False,
    )

    assert result["status"] == "completed"
    assert result["completion"]["completed"] is True
    assert result["completion"]["imported_rows"] == 2
    assert result["completion"]["completion_condition"] == "imported_rows_gt_0"
    assert result["runtime_contract"]["job_type"] == "financial_exclusive"
    assert calls["payload"]["bank_only"] is True
    assert calls["payload"]["services"] == ["shinhan_business"]
    assert calls["payload"]["browser_agent_id"] == "62405e70-e98"
    assert calls["payload"]["portal_url"] == harness.SHINHAN_EASYVIEW_LOGIN_URL
    assert calls["timeout_seconds"] == 777


def test_queue_only_returns_queued_completion_contract(monkeypatch):
    harness = _reload_harness(monkeypatch)
    monkeypatch.setattr(harness, "_graph_available", lambda: False)
    monkeypatch.setattr(harness, "_run_security_preflight", lambda payload, agent_id: {"checked": "1", "ready": True})
    monkeypatch.setattr(
        harness,
        "_enqueue_collection",
        lambda payload, user: {"queued": True, "count": 1, "items": [{"service": "shinhan_business"}]},
    )

    result = harness.run_shinhan_windows_collector_harness(
        {"browser_agent_id": "62405e70-e98"},
        {"email": "system@aads.local", "is_admin": True},
        queue_only=True,
    )

    assert result["status"] == "queued"
    assert result["collection_result"]["queued"] is True
    assert result["completion"]["completed"] is False
    assert result["completion"]["completion_condition"] == "queued_for_windows_collector"
