"""M1 baseline decisions use injected observations only."""

from datetime import datetime, timedelta, timezone

import pytest

from scripts.llm_baseline_verify import CATALOG_COLUMNS_SQL, CATALOG_SQL, DISCOVERY_SQL, collect

NOW = datetime(2026, 9, 29, 6, 0, tzinfo=timezone.utc)


@pytest.fixture
def observations():
    return {
        "relay": {"claude_model_contract": {"models": ["claude-sonnet-5-5"]}},
        "slots": {
            "aads-server": {"image_digest": "sha256:abc", "cli_version": "2.1.0 (Claude Code)"},
            "aads-server-green": {"image_digest": "sha256:abc", "cli_version": "2.1.0 (Claude Code)"},
        },
        "catalog": {"selectable_unverified": 0, "active_null_tier": 0,
                    "active_null_fallback_group": 0},
        "discovery": [{"provider": "anthropic", "status": "ok", "error": "",
                       "last_run_at": NOW.isoformat(), "last_success_at": NOW.isoformat()}],
    }


def check(observations):
    def database(sql):
        if sql == CATALOG_COLUMNS_SQL:
            return observations.get("columns", ["tier", "fallback_group"])
        return observations["catalog"] if sql == CATALOG_SQL else observations["discovery"]

    return collect(relay_reader=lambda: observations["relay"],
                   slot_reader=lambda slot: observations["slots"][slot],
                   db_reader=database, now=NOW)


def test_stale_contract_detected(observations):
    observations["relay"]["claude_model_contract"]["models"] = []
    result = check(observations)
    assert "claude-sonnet-5-5" in result["checks"]["relay_contract"]["stale_models"]
    assert result["ok"] is False


def test_matching_contract_passes(observations):
    # The fixture supplies all exact IDs so no runtime model is inferred.
    from scripts.claude_model_contract import EXACT_MODEL_IDS
    observations["relay"]["claude_model_contract"]["models"] = sorted(EXACT_MODEL_IDS)
    assert check(observations)["checks"]["relay_contract"]["ok"] is True


def test_slot_digest_mismatch(observations):
    observations["slots"]["aads-server-green"]["image_digest"] = "sha256:def"
    assert check(observations)["checks"]["slots"]["digest_match"] is False


def test_slot_cli_version_mismatch(observations):
    observations["slots"]["aads-server-green"]["cli_version"] = "2.2.0 (Claude Code)"
    assert check(observations)["checks"]["slots"]["cli_version_match"] is False


def test_null_tier_detected(observations):
    observations["catalog"]["active_null_tier"] = 1
    result = check(observations)
    assert result["checks"]["catalog"]["active_null_tier"] == 1
    assert "catalog" in result["incomplete"]


def test_missing_catalog_column_is_incomplete_without_sql_reference(observations):
    queries = []

    def database(sql):
        queries.append(sql)
        if sql == CATALOG_COLUMNS_SQL:
            return ["tier"]
        if sql == CATALOG_SQL:
            pytest.fail("catalog aggregate must not run with a missing column")
        return observations["discovery"]

    result = collect(relay_reader=lambda: observations["relay"],
                     slot_reader=lambda slot: observations["slots"][slot],
                     db_reader=database, now=NOW)
    assert result["checks"]["catalog"] == {
        "status": "column_missing", "missing_columns": ["fallback_group"], "ok": False}
    assert result["checks"]["discovery"]["providers"]
    assert DISCOVERY_SQL in queries
    assert "catalog" in result["incomplete"]


def test_discovery_stale(observations):
    observations["discovery"][0]["last_success_at"] = (NOW - timedelta(days=2)).isoformat()
    result = check(observations)
    assert result["checks"]["discovery"]["providers"][0]["age_minutes"] == 2880
    assert "discovery" in result["incomplete"]


@pytest.mark.parametrize(("raw", "expected"), [
    ("oauth_runtime_only_models_api_unavailable token=secret", "models_api_unavailable"),
    ("google_routes_disabled token=secret", "provider_disabled"),
    ("HTTP 429 token=secret", "http_4xx"),
    ("HTTP 503 token=secret", "http_5xx"),
    ("request timed out token=secret", "timeout"),
    ("postgres://user:secret@host", "unclassified"),
])
def test_discovery_error_is_redacted_even_with_injected_row(observations, raw, expected):
    observations["discovery"][0]["error"] = raw
    result = check(observations)
    assert result["checks"]["discovery"]["providers"][0]["error_class"] == expected
    assert "secret" not in str(result)
    assert "'error', l.error" not in DISCOVERY_SQL


def test_probe_failure_is_incomplete(observations):
    result = collect(relay_reader=lambda: observations["relay"],
                     slot_reader=lambda slot: (_ for _ in ()).throw(OSError("secret")),
                     db_reader=lambda sql: ["tier", "fallback_group"] if sql == CATALOG_COLUMNS_SQL
                     else observations["catalog"] if sql == CATALOG_SQL
                     else observations["discovery"], now=NOW)
    assert result["ok"] is False
    assert result["errors"]["slots"] == "OSError"
    assert "secret" not in str(result)
