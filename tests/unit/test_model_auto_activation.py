"""발견 모델 자동 활성화(실행 가능성 판정)·Anthropic 수집 폴백·회사별 신선도 노출 회귀 테스트."""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock

import pytest

from app.api import llm_models as llm_models_api
from app.services import model_registry
from app.services.model_registry import ProbeOutcome

NOW = datetime(2026, 11, 5, 3, 0, tzinfo=timezone.utc)


def _key_rows(*providers):
    return [
        {
            "id": idx, "provider": provider, "key_name": f"{provider.upper()}_KEY", "priority": 1,
            "is_active": True, "rate_limited_until": None, "last_used_at": NOW, "last_verified_at": NOW,
        }
        for idx, provider in enumerate(providers, start=1)
    ]


def _discovered(provider, model_id, raw=None, key_providers=None):
    key_state = model_registry._build_key_state(_key_rows(*(key_providers or [provider])))
    return model_registry._discovered_model_row(
        provider=provider, model_id=model_id, display_name=model_id, key_state=key_state,
        raw=raw or {}, source=f"{provider}_api",
    )


def _prober(results):
    calls = []

    async def probe(row):
        calls.append((row["provider"], row["model_id"]))
        return results.get(row["model_id"], ProbeOutcome(True))

    probe.calls = calls
    return probe


def _prior(model_id, provider, **fields):
    base = {
        "provider": provider, "model_id": model_id, "is_active": False, "is_executable": False,
        "activation_source": "review_required", "verification_status": "review_required",
        "discovery_source": f"{provider}_api", "retired_at": None, "activation_probe": None,
    }
    base.update(fields)
    return {(provider, model_id): base}


# ── (a) kimi/deepseek: 호출 성공 시 활성, 실패 시 review_required + 사유 ─────────────

@pytest.mark.asyncio
@pytest.mark.parametrize("provider,model_id", [("kimi", "kimi-k3-preview"), ("deepseek", "deepseek-v4")])
async def test_discovered_model_is_inactive_until_probe_and_active_on_success(provider, model_id):
    row = _discovered(provider, model_id)
    assert row["is_active"] is False and row["is_executable"] is False
    assert row["verification_status"] == "review_required"
    assert row["metadata"]["activation_candidate"] is True

    probe = _prober({})
    summary = await model_registry.apply_activation_probes([row], {}, now=NOW, prober=probe)

    assert probe.calls == [(provider, model_id)]
    assert summary["activated"] == [f"{provider}:{model_id}"]
    assert row["is_active"] and row["is_selectable"] and row["is_executable"]
    assert row["verification_status"] == "verified"
    assert row["metadata"]["activation_probe"]["status"] == "ok"
    assert row["metadata"]["requires_admin_review"] is False


@pytest.mark.asyncio
@pytest.mark.parametrize("provider,model_id", [("kimi", "kimi-k3-preview"), ("deepseek", "deepseek-v4")])
async def test_probe_failure_leaves_review_required_with_reason(provider, model_id):
    row = _discovered(provider, model_id)
    probe = _prober({model_id: ProbeOutcome(False, "http_404", False)})

    summary = await model_registry.apply_activation_probes([row], {}, now=NOW, prober=probe)

    assert summary["failed"] == [f"{provider}:{model_id}=http_404"]
    assert row["is_active"] is False and row["is_executable"] is False
    assert row["verification_status"] == "review_required"
    assert row["metadata"]["activation_review_reason"] == "http_404"
    assert row["metadata"]["activation_probe"] == {
        "status": "failed", "at": NOW.isoformat(), "error": "http_404", "transient": False, "max_tokens": 16,
    }


@pytest.mark.asyncio
async def test_excluded_model_types_and_missing_key_are_never_called():
    embedding = _discovered("deepseek", "deepseek-embedding-v1")
    tts = _discovered("kimi", "kimi-tts-1")
    no_key = _discovered("deepseek", "deepseek-v5", key_providers=["kimi"])
    probe = _prober({})

    await model_registry.apply_activation_probes([embedding, tts, no_key], {}, now=NOW, prober=probe)

    assert probe.calls == []
    assert embedding["metadata"]["activation_review_reason"] == "not_text_model"
    assert tts["metadata"]["activation_review_reason"] == "not_text_model"
    assert no_key["metadata"]["activation_review_reason"] == "no_runtime_key"
    assert not any(r["is_active"] for r in (embedding, tts, no_key))


@pytest.mark.asyncio
async def test_litellm_alias_follows_same_rule_and_google_alias_stays_blocked():
    alias = _discovered("litellm", "alias-fast-chat")
    google_alias = _discovered(
        "litellm", "alias-google-chat", raw={"litellm_params": {"model": "gemini/gemini-3-flash"}},
    )
    probe = _prober({})

    await model_registry.apply_activation_probes(
        [alias, google_alias], {}, now=NOW, prober=probe, google_routes_enabled=False,
    )

    assert probe.calls == [("litellm", "alias-fast-chat")]
    assert alias["is_active"] is True
    assert google_alias["is_active"] is False
    assert google_alias["metadata"]["activation_review_reason"] == "google_routes_disabled"


@pytest.mark.asyncio
async def test_judged_models_are_not_called_again_and_failures_retry_after_24h():
    ok_row = _discovered("kimi", "kimi-judged-ok")
    fresh_fail = _discovered("kimi", "kimi-fail-recent")
    old_fail = _discovered("kimi", "kimi-fail-old")
    prior = {}
    prior.update(_prior("kimi-judged-ok", "kimi", is_active=True, is_executable=True, activation_source="db",
                        verification_status="verified", activation_probe={"status": "ok", "at": NOW.isoformat()}))
    prior.update(_prior("kimi-fail-recent", "kimi", activation_probe={
        "status": "failed", "at": (NOW - timedelta(hours=3)).isoformat(), "error": "http_400", "transient": False}))
    prior.update(_prior("kimi-fail-old", "kimi", activation_probe={
        "status": "failed", "at": (NOW - timedelta(hours=25)).isoformat(), "error": "http_400", "transient": False}))
    probe = _prober({})

    await model_registry.apply_activation_probes([ok_row, fresh_fail, old_fail], prior, now=NOW, prober=probe)

    assert probe.calls == [("kimi", "kimi-fail-old")]
    assert ok_row["is_active"] is True
    assert fresh_fail["is_active"] is False
    assert fresh_fail["metadata"]["activation_review_reason"] == "http_400"
    assert old_fail["is_active"] is True


@pytest.mark.asyncio
async def test_transient_failure_retries_after_one_hour_and_halts_provider():
    rows = [_discovered("deepseek", f"deepseek-m{i}") for i in range(3)]
    probe = _prober({"deepseek-m0": ProbeOutcome(False, "http_429", True)})

    summary = await model_registry.apply_activation_probes(rows, {}, now=NOW, prober=probe)

    assert probe.calls == [("deepseek", "deepseek-m0")]
    assert sorted(summary["deferred"]) == ["deepseek:deepseek-m1", "deepseek:deepseek-m2"]

    retry_row = _discovered("deepseek", "deepseek-m0")
    prior = _prior("deepseek-m0", "deepseek", activation_probe={
        "status": "failed", "at": (NOW - timedelta(minutes=30)).isoformat(), "error": "http_429", "transient": True})
    probe2 = _prober({})
    await model_registry.apply_activation_probes([retry_row], prior, now=NOW, prober=probe2)
    assert probe2.calls == []
    prior[("deepseek", "deepseek-m0")]["activation_probe"]["at"] = (NOW - timedelta(minutes=61)).isoformat()
    await model_registry.apply_activation_probes([retry_row], prior, now=NOW, prober=probe2)
    assert probe2.calls == [("deepseek", "deepseek-m0")]


@pytest.mark.asyncio
async def test_previously_active_discovered_row_is_carried_not_demoted():
    row = _discovered("kimi", "kimi-legacy-active")
    prior = _prior("kimi-legacy-active", "kimi", is_active=True, is_executable=True, activation_source="db",
                   verification_status="verified")
    probe = _prober({})

    summary = await model_registry.apply_activation_probes([row], prior, now=NOW, prober=probe)

    assert probe.calls == []
    assert summary["carried_active"] == ["kimi:kimi-legacy-active"]
    assert row["is_active"] is True and row["verification_status"] == "verified"


# ── (b) 수동으로 끈 모델은 되살리지 않는다 ───────────────────────────────────────

@pytest.mark.asyncio
async def test_manually_disabled_model_is_not_restored_or_probed():
    never_probed = _discovered("kimi", "kimi-admin-off")
    probed_ok_before = _discovered("deepseek", "deepseek-admin-off")
    prior = {}
    prior.update(_prior("kimi-admin-off", "kimi", activation_source="manual"))
    prior.update(_prior("deepseek-admin-off", "deepseek", activation_source="manual",
                        activation_probe={"status": "ok", "at": (NOW - timedelta(days=2)).isoformat()}))
    probe = _prober({})

    summary = await model_registry.apply_activation_probes(
        [never_probed, probed_ok_before], prior, now=NOW, prober=probe,
    )

    assert probe.calls == []
    assert sorted(summary["manual_kept"]) == ["deepseek:deepseek-admin-off", "kimi:kimi-admin-off"]
    for row in (never_probed, probed_ok_before):
        assert row["is_active"] is False and row["is_selectable"] is False and row["is_executable"] is False
        assert row["activation_source"] == "manual"


@pytest.mark.asyncio
async def test_probe_ok_row_disabled_by_admin_stays_off_even_without_manual_source():
    row = _discovered("kimi", "kimi-off-after-ok")
    prior = _prior("kimi-off-after-ok", "kimi", activation_source="db",
                   activation_probe={"status": "ok", "at": (NOW - timedelta(days=1)).isoformat()})
    probe = _prober({})

    await model_registry.apply_activation_probes([row], prior, now=NOW, prober=probe)

    assert probe.calls == []
    assert row["is_active"] is False


@pytest.mark.asyncio
async def test_skip_keys_for_manually_enabled_rows_are_untouched():
    row = _discovered("kimi", "kimi-admin-on")
    before = dict(row)
    probe = _prober({})

    await model_registry.apply_activation_probes(
        [row], {}, now=NOW, prober=probe, skip_keys=[("kimi", "kimi-admin-on")],
    )

    assert probe.calls == []
    assert row["is_active"] == before["is_active"]


# ── (c) provider 당 호출 5건 상한 ───────────────────────────────────────────────

@pytest.mark.asyncio
async def test_per_provider_call_cap_is_five_and_rest_deferred():
    kimi = [_discovered("kimi", f"kimi-n{i:02d}") for i in range(8)]
    deepseek = [_discovered("deepseek", f"deepseek-n{i}") for i in range(2)]
    probe = _prober({})

    summary = await model_registry.apply_activation_probes(kimi + deepseek, {}, now=NOW, prober=probe)

    assert summary["probed"] == {"kimi": 5, "deepseek": 2}
    assert len([c for c in probe.calls if c[0] == "kimi"]) == 5
    assert len(summary["deferred"]) == 3
    deferred_rows = [r for r in kimi if r["metadata"].get("activation_review_reason") == "probe_deferred"]
    assert len(deferred_rows) == 3 and not any(r["is_active"] for r in deferred_rows)
    assert sum(1 for r in kimi if r["is_active"]) == 5


@pytest.mark.asyncio
async def test_deferred_models_are_called_in_next_run_without_repeating_judged_ones():
    rows = [_discovered("kimi", f"kimi-n{i:02d}") for i in range(7)]
    probe1 = _prober({})
    await model_registry.apply_activation_probes(rows, {}, now=NOW, prober=probe1)
    judged = set(probe1.calls)
    assert len(judged) == 5

    prior = {}
    for row in rows:
        probe_meta = row["metadata"].get("activation_probe")
        prior.update(_prior(
            row["model_id"], "kimi", is_active=bool(row["is_active"]), is_executable=bool(row["is_executable"]),
            activation_source=row["activation_source"], activation_probe=probe_meta,
        ))
    next_rows = [_discovered("kimi", f"kimi-n{i:02d}") for i in range(7)]
    probe2 = _prober({})
    await model_registry.apply_activation_probes(next_rows, prior, now=NOW + timedelta(hours=6), prober=probe2)

    assert set(probe2.calls).isdisjoint(judged)
    assert len(probe2.calls) == 2
    assert all(r["is_active"] for r in next_rows)


# ── 템플릿·코덱스 기존 규칙 유지 ─────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_codex_activates_only_when_host_probe_verified():
    verified = _discovered("codex", "gpt-9-verified", key_providers=[])
    pending = _discovered("codex", "gpt-9-pending", key_providers=[])
    probe = _prober({})

    await model_registry.apply_activation_probes(
        [verified, pending], {}, now=NOW, prober=probe, verified_codex_ids=["gpt-9-verified"],
    )

    assert probe.calls == []
    assert verified["is_active"] is True
    assert pending["is_active"] is False
    assert pending["metadata"]["activation_review_reason"] == "awaiting_host_cli_probe"


def test_template_rows_are_not_touched_by_probe_step():
    template_rows, _ = model_registry.build_registry_snapshots(_key_rows("anthropic"))
    snapshot = [dict(r) for r in template_rows]
    import asyncio

    probe = _prober({})
    asyncio.run(model_registry.apply_activation_probes(template_rows, {}, now=NOW, prober=probe))

    assert probe.calls == []
    assert [r["is_active"] for r in template_rows] == [r["is_active"] for r in snapshot]


# ── (d) Anthropic 수집 폴백 ─────────────────────────────────────────────────────

DOC_TEXT = """
| Claude Fable 5.1 | claude-fable-5-1 | anthropic.claude-fable-5-1-v1:0 |
Vertex: claude-fable-5-1@20261101, claude-sonnet-5@latest
`claude-opus-5-5` and `claude-haiku-4-5-20251001` ; claude-code is a tool, claude-3-5-sonnet-20241022.
Aliases: claude-sonnet-5. See https://docs.anthropic.com/claude-agent-sdk
"""


def test_extract_claude_model_ids_keeps_plain_ids_and_drops_cloud_forms():
    ids = model_registry.extract_claude_model_ids(DOC_TEXT)

    assert "claude-fable-5-1" in ids
    assert "claude-opus-5-5" in ids
    assert "claude-haiku-4-5-20251001" in ids
    assert "claude-sonnet-5" in ids
    assert "claude-code" not in ids
    assert not any("@" in i or ":" in i or "anthropic." in i for i in ids)
    assert ids == sorted(set(ids))


@pytest.fixture
def anthropic_oauth_runtime(monkeypatch):
    async def _key_records(_provider, include_rate_limited=False):
        return [{"key_name": "ANTHROPIC_AUTH_TOKEN", "value": "sk-ant-oat01-test"}]

    from app.core import llm_key_provider

    monkeypatch.setattr(llm_key_provider, "get_provider_key_records", _key_records)


@pytest.mark.asyncio
async def test_anthropic_uses_oauth_models_list_when_available(monkeypatch, anthropic_oauth_runtime):
    docs = AsyncMock(side_effect=AssertionError("docs must not be read when oauth list works"))
    monkeypatch.setattr(model_registry, "_list_anthropic_models_via_oauth", AsyncMock(return_value=[
        {"model_id": "claude-oauth-listed-1", "display_name": "X", "raw": {}},
    ]))
    monkeypatch.setattr(model_registry, "_read_anthropic_docs_model_ids", docs)

    rows, result = await model_registry._fetch_anthropic_models()

    assert [r["model_id"] for r in rows] == ["claude-oauth-listed-1"]
    assert result["status"] == "ok"
    assert result["discovery_source"] == "anthropic_oauth_api"
    docs.assert_not_called()


@pytest.mark.asyncio
async def test_anthropic_falls_back_to_docs_candidates_when_oauth_list_rejected(monkeypatch, anthropic_oauth_runtime):
    monkeypatch.setattr(model_registry, "_list_anthropic_models_via_oauth", AsyncMock(side_effect=RuntimeError("401")))
    monkeypatch.setattr(model_registry, "_read_anthropic_docs_model_ids", AsyncMock(
        return_value=(["claude-fable-5-1", "claude-opus-5-5"], 1_000.0),
    ))

    rows, result = await model_registry._fetch_anthropic_models()

    assert [r["model_id"] for r in rows] == ["claude-fable-5-1", "claude-opus-5-5"]
    assert all(r["raw"]["source"] == "anthropic_models_docs" for r in rows)
    assert result["status"] == "ok"
    assert result["discovery_source"] == "anthropic_docs"
    assert result["oauth_models_api"] == "RuntimeError"


@pytest.mark.asyncio
async def test_anthropic_docs_candidates_activate_only_after_call_check():
    passing = _discovered("anthropic", "claude-fable-5-1")
    failing = _discovered("anthropic", "claude-ghost-9")
    probe = _prober({"claude-ghost-9": ProbeOutcome(False, "http_404", False)})

    await model_registry.apply_activation_probes([passing, failing], {}, now=NOW, prober=probe)

    assert passing["is_active"] is True and passing["verification_status"] == "verified"
    assert failing["is_active"] is False
    assert failing["metadata"]["activation_review_reason"] == "http_404"


@pytest.mark.asyncio
async def test_anthropic_skipped_when_oauth_and_docs_both_unavailable(monkeypatch, anthropic_oauth_runtime):
    monkeypatch.setattr(model_registry, "_list_anthropic_models_via_oauth", AsyncMock(side_effect=RuntimeError("401")))
    monkeypatch.setattr(model_registry, "_read_anthropic_docs_model_ids", AsyncMock(side_effect=RuntimeError("down")))

    rows, result = await model_registry._fetch_anthropic_models()

    assert rows == []
    assert result["status"] == "skipped"
    assert result["error"] == "oauth_runtime_only_models_api_unavailable"


def test_anthropic_probe_does_not_read_api_key_env():
    import inspect

    source = inspect.getsource(model_registry._probe_anthropic_oauth)
    assert "ANTHROPIC_API_KEY" not in source
    assert "sk-ant-api03" not in inspect.getsource(model_registry)


# ── (e) 신선도 노출 필드 ────────────────────────────────────────────────────────

def _model_row(provider, model_id, *, first_seen, verification="review_required", source=None, probe=None):
    return {
        "provider": provider, "model_id": model_id, "first_seen_at": first_seen, "retired_at": None,
        "verification_status": verification, "discovery_source": source or f"{provider}_api", "activation_probe": probe,
    }


def _freshness_inputs():
    freshness = {
        "kimi": {"state": "ok", "reason_class": "", "last_attempt_at": NOW.isoformat(),
                 "last_success_at": NOW.isoformat()},
        "anthropic": {"state": "skipped", "reason_class": "oauth_runtime_only",
                      "last_attempt_at": NOW.isoformat(), "last_success_at": None},
        "deepseek": {"state": "error", "reason_class": "http_401", "last_attempt_at": NOW.isoformat(),
                     "last_success_at": (NOW - timedelta(days=3)).isoformat()},
        "openai": {"state": "error", "reason_class": "timeout", "last_attempt_at": NOW.isoformat(),
                   "last_success_at": (NOW - timedelta(hours=2)).isoformat()},
    }
    rows = [
        _model_row("kimi", "kimi-new-ok", first_seen=NOW - timedelta(days=1), verification="verified",
                   probe={"status": "ok", "at": (NOW - timedelta(days=1)).isoformat()}),
        _model_row("kimi", "kimi-new-pending", first_seen=NOW - timedelta(days=2)),
        _model_row("kimi", "kimi-old-pending", first_seen=NOW - timedelta(days=30)),
        _model_row("kimi", "kimi-old-ok", first_seen=NOW - timedelta(days=30), verification="verified",
                   probe={"status": "ok", "at": (NOW - timedelta(days=20)).isoformat()}),
        _model_row("kimi", "kimi-template", first_seen=NOW - timedelta(days=1), source="template"),
    ]
    return rows, freshness


def test_summarize_model_freshness_fields():
    rows, freshness = _freshness_inputs()

    report = model_registry.summarize_model_freshness(
        model_rows=rows, freshness=freshness, keyed_providers=["kimi", "anthropic", "mystery", "openai"], now=NOW,
    )

    kimi = report["providers"]["kimi"]
    assert kimi["last_success_at"] == NOW.isoformat()
    assert kimi["new_models_7d"] == 2
    assert kimi["auto_activated_7d"] == 1
    assert kimi["review_pending_7d"] == 1
    assert kimi["review_pending_total"] == 2
    assert report["window_days"] == 7
    assert report["keyed_providers_without_collector"] == ["mystery"]
    stuck = {item["provider"]: item for item in report["skipped_or_failed_24h"]}
    assert set(stuck) == {"anthropic", "deepseek"}
    assert stuck["anthropic"]["reason_class"] == "oauth_runtime_only"
    assert stuck["anthropic"]["hours_since_success"] is None
    assert stuck["deepseek"]["hours_since_success"] == 72.0
    assert report["skipped_or_failed_24h_providers"] == ["anthropic", "deepseek"]
    assert "openai" not in stuck
    assert report["providers"]["codex"]["has_collector"] is True


def test_summarize_model_freshness_ignores_retired_and_empty_inputs():
    retired = _model_row("kimi", "kimi-gone", first_seen=NOW)
    retired["retired_at"] = NOW

    report = model_registry.summarize_model_freshness(
        model_rows=[retired], freshness={}, keyed_providers=[], now=NOW,
    )

    assert report["providers"]["kimi"]["new_models_7d"] == 0
    assert report["skipped_or_failed_24h"] == []
    assert report["keyed_providers_without_collector"] == []


class _Acquire:
    def __init__(self, conn):
        self.conn = conn

    async def __aenter__(self):
        return self.conn

    async def __aexit__(self, *exc):
        return False


class _Pool:
    def __init__(self, conn):
        self.conn = conn

    def acquire(self):
        return _Acquire(self.conn)


class _ReportConn:
    def __init__(self, model_rows):
        self.model_rows = model_rows

    async def fetch(self, query, *args):
        assert "FROM llm_models" in query
        return self.model_rows


@pytest.fixture
def freshness_db(monkeypatch):
    rows, freshness = _freshness_inputs()
    model_registry.invalidate_registry_cache()
    monkeypatch.setattr(model_registry, "get_pool", lambda: _Pool(_ReportConn(rows)))
    monkeypatch.setattr(model_registry, "_fetch_key_rows", AsyncMock(return_value=_key_rows("kimi", "mystery")))
    monkeypatch.setattr(model_registry, "list_provider_discovery_freshness", AsyncMock(return_value=freshness))
    yield
    model_registry.invalidate_registry_cache()


@pytest.mark.asyncio
async def test_get_model_freshness_report_reads_db_state(freshness_db):
    report = await model_registry.get_model_freshness_report()

    assert report["providers"]["kimi"]["review_pending_total"] == 2
    assert "mystery" in report["keyed_providers_without_collector"]
    assert "anthropic" in report["skipped_or_failed_24h_providers"]


@pytest.mark.asyncio
async def test_provider_summary_endpoint_adds_freshness_and_keeps_existing_fields(monkeypatch, freshness_db):
    monkeypatch.setattr(llm_models_api, "list_provider_summaries", AsyncMock(return_value=[
        {"provider": "kimi", "active_model_count": 1, "runtime_executable": True},
    ]))
    monkeypatch.setattr(llm_models_api, "_fetch_last_registry_sync", AsyncMock(return_value=None))
    monkeypatch.setattr(llm_models_api, "list_provider_discovery_freshness", AsyncMock(return_value={
        "kimi": {"state": "ok", "latest_confirmed": True},
    }))

    body = await llm_models_api.get_provider_summary()

    for existing in ("providers", "latest_confirmed_providers", "not_latest_confirmed_providers", "stale_providers",
                     "total", "active_provider_count", "runtime_executable_provider_count"):
        assert existing in body
    assert body["latest_confirmed_providers"] == ["kimi"]
    kimi = body["providers"][0]
    assert kimi["discovery_freshness"]["state"] == "ok"
    assert kimi["model_freshness"]["new_models_7d"] == 2
    assert kimi["model_freshness"]["auto_activated_7d"] == 1
    assert kimi["model_freshness"]["review_pending_7d"] == 1
    assert body["keyed_providers_without_collector"] == ["mystery"]
    assert body["skipped_or_failed_24h_providers"] == ["anthropic", "deepseek"]


@pytest.mark.asyncio
async def test_provider_summary_survives_freshness_report_failure(monkeypatch):
    monkeypatch.setattr(llm_models_api, "list_provider_summaries", AsyncMock(return_value=[{"provider": "kimi"}]))
    monkeypatch.setattr(llm_models_api, "_fetch_last_registry_sync", AsyncMock(return_value=None))
    monkeypatch.setattr(llm_models_api, "list_provider_discovery_freshness", AsyncMock(return_value={}))
    monkeypatch.setattr(llm_models_api, "get_model_freshness_report", AsyncMock(side_effect=RuntimeError("db")))

    body = await llm_models_api.get_provider_summary()

    assert body["total"] == 1
    assert body["keyed_providers_without_collector"] == []
    assert body["skipped_or_failed_24h_providers"] == []


def test_ops_health_check_exposes_model_freshness_without_dropping_fields():
    from pathlib import Path

    source = (Path(llm_models_api.__file__).parent / "ops.py").read_text(encoding="utf-8")
    assert '"model_freshness": model_freshness' in source
    health_src = source[source.index("async def health_check"):]
    health_src = health_src[: health_src.index('"checked_at"')]
    assert "get_model_freshness_report" not in health_src
    assert "get_cached_model_freshness" in health_src
    assert '"pipeline_healthy": pipeline_healthy and all_containers_ok' in source
    assert '"stalled_count": stalled_count' in source


# ── 재작업: AGY 행 구글 게이트 오분류 / 삭제 심볼 호환 / health-check 캐시 전용 ───────────

def test_antigravity_rows_are_not_google_api_routes_but_gemini_and_litellm_aliases_are():
    agy = {"provider": "antigravity", "model_id": "gemini-3.8-flash-high", "metadata": {}}
    assert model_registry._is_google_route(agy) is False
    assert model_registry._is_google_route({"provider": "gemini", "model_id": "gemini-3-flash"}) is True
    alias = {
        "provider": "litellm", "model_id": "alias-google-chat",
        "metadata": {"raw": {"litellm_params": {"model": "vertex_ai/gemini-3-pro"}}},
    }
    assert model_registry._is_google_route(alias) is True


@pytest.mark.asyncio
async def test_antigravity_gemini_named_row_is_not_marked_google_routes_disabled():
    row = {
        "provider": "antigravity", "model_id": "gemini-3.8-flash-high", "is_active": False,
        "metadata": {"model_source": "discovery", "activation_candidate": True, "execution_backend": "agy_cli"},
    }
    probe = _prober({})

    await model_registry.apply_activation_probes(
        [row], {}, now=NOW, prober=probe, google_routes_enabled=False,
    )

    assert row["metadata"].get("activation_review_reason") != "google_routes_disabled"
    assert probe.calls == [("antigravity", "gemini-3.8-flash-high")]


@pytest.mark.asyncio
async def test_gemini_provider_and_litellm_gemini_alias_still_google_routes_disabled():
    gemini = _discovered("gemini", "gemini-3-flash")
    alias = _discovered("litellm", "alias-g", raw={"litellm_params": {"model": "gemini/gemini-3-flash"}})
    probe = _prober({})

    await model_registry.apply_activation_probes(
        [gemini, alias], {}, now=NOW, prober=probe, google_routes_enabled=False,
    )

    assert probe.calls == []
    assert gemini["metadata"]["activation_review_reason"] == "google_routes_disabled"
    assert alias["metadata"]["activation_review_reason"] == "google_routes_disabled"


def test_legacy_symbols_remain_and_delegate_to_new_logic():
    assert model_registry._is_auto_executable_discovered("openai", "gpt-5.5") is True
    assert model_registry._is_auto_executable_discovered("openai", "gpt-image-2") is False
    assert model_registry._is_auto_executable_discovered("anthropic", "claude-opus-5") is True
    assert model_registry._is_auto_executable_discovered("gemini", "gemini-3-flash", {"supportedGenerationMethods": ["embedContent"]}) is False
    for provider, model_id in [("openai", "gpt-5.5"), ("anthropic", "claude-opus-5"), ("kimi", "kimi-k3")]:
        assert model_registry._is_auto_executable_discovered(provider, model_id) == model_registry._is_text_model_candidate(provider, model_id)
    assert model_registry._anthropic_key_supports_models_api("ANTHROPIC_AUTH_TOKEN") is True
    assert model_registry._anthropic_key_supports_models_api("") is False


@pytest.mark.asyncio
async def test_openai_compat_discovery_companies_go_through_activation_gate():
    groq = _discovered("groq", "llama-4-scout")
    together = _discovered("together", "meta-llama/llama-5")
    assert set(model_registry._OPENAI_COMPAT_DISCOVERY_BASE_URLS) >= {"groq", "together", "cerebras", "sambanova", "mistral", "openrouter", "minimax"}
    assert groq["is_active"] is False and together["is_active"] is False
    probe = _prober({})

    await model_registry.apply_activation_probes([groq, together], {}, now=NOW, prober=probe)

    assert probe.calls == [("groq", "llama-4-scout")]
    assert groq["is_active"] is True and groq["metadata"]["activation_probe"]["status"] == "ok"
    assert together["is_active"] is False
    assert together["metadata"]["activation_review_reason"] == "no_execution_path"


@pytest.mark.asyncio
async def test_activation_cap_applies_to_openai_compat_companies():
    rows = [_discovered("groq", f"llama-{i}") for i in range(8)]
    probe = _prober({})

    summary = await model_registry.apply_activation_probes(rows, {}, now=NOW, prober=probe)

    assert len(probe.calls) == 5
    assert len(summary["deferred"]) == 3


@pytest.mark.asyncio
async def test_cached_freshness_not_ready_without_touching_db_pool(monkeypatch):
    def boom():
        raise AssertionError("health-check path must not acquire the DB pool")

    monkeypatch.setattr(model_registry, "get_pool", boom)
    monkeypatch.setattr(model_registry, "_freshness_snapshot", None)

    assert model_registry.get_cached_model_freshness() == {"status": "not_ready"}


@pytest.mark.asyncio
async def test_refresh_fills_snapshot_and_failure_is_swallowed(monkeypatch, freshness_db):
    monkeypatch.setattr(model_registry, "_freshness_snapshot", None)
    await model_registry.refresh_model_freshness_cache()
    assert model_registry.get_cached_model_freshness()["providers"]["kimi"]["review_pending_total"] == 2

    monkeypatch.setattr(model_registry, "_freshness_snapshot", None)
    monkeypatch.setattr(model_registry, "get_pool", lambda: (_ for _ in ()).throw(RuntimeError("db")))
    await model_registry.refresh_model_freshness_cache()
    assert model_registry.get_cached_model_freshness() == {"status": "not_ready"}


# ── 3차 재작업: 키 복구·Google 정책 순서·자동활성 끔 상태 보존 ───────────────────────

_OK_PROBE = {"status": "ok", "at": (NOW - timedelta(days=2)).isoformat(), "error": "", "transient": False}


@pytest.mark.asyncio
async def test_probe_success_then_key_unavailable_then_key_restored_reactivates_after_reprobe():
    provider, model_id = "kimi", "kimi-k3-recover"

    # 1) 성공 이력이 있는 활성 모델의 키가 사라진다 → 시스템이 끈다(관리자 표식 아님).
    no_key = _discovered(provider, model_id, key_providers=["deepseek"])
    prior_active = _prior(model_id, provider, is_active=True, is_executable=True, activation_source="db",
                          verification_status="verified", activation_probe=_OK_PROBE)
    off_probe = _prober({})
    await model_registry.apply_activation_probes([no_key], prior_active, now=NOW, prober=off_probe)
    assert no_key["is_active"] is False
    assert no_key["metadata"]["activation_review_reason"] == "no_runtime_key"
    assert no_key["metadata"]["deactivation_source"] == "system"
    assert no_key["activation_source"] != "manual"
    assert off_probe.calls == []

    # 2) 그 상태가 저장된 뒤 키가 복구되면 수동 고정이 아니라 재-probe 후 다시 켠다.
    stored = _prior(model_id, provider, is_active=False, activation_source=no_key["activation_source"],
                    activation_probe=no_key["metadata"]["activation_probe"],
                    deactivation_source=no_key["metadata"]["deactivation_source"])
    restored = _discovered(provider, model_id)
    probe = _prober({})
    summary = await model_registry.apply_activation_probes([restored], stored, now=NOW, prober=probe)

    assert probe.calls == [(provider, model_id)]
    assert summary["manual_kept"] == []
    assert restored["is_active"] is True and restored["activation_source"] == "db"
    assert restored["metadata"].get("deactivation_source") is None


@pytest.mark.asyncio
async def test_system_disabled_model_stays_off_when_reprobe_fails():
    row = _discovered("kimi", "kimi-k3-recover-fail")
    stored = _prior("kimi-k3-recover-fail", "kimi", activation_source="fallback",
                    activation_probe=_OK_PROBE, deactivation_source="system")
    probe = _prober({"kimi-k3-recover-fail": ProbeOutcome(False, "http_401")})

    await model_registry.apply_activation_probes([row], stored, now=NOW, prober=probe)

    assert probe.calls == [("kimi", "kimi-k3-recover-fail")]
    assert row["is_active"] is False
    assert row["metadata"]["activation_review_reason"] == "http_401"


@pytest.mark.asyncio
async def test_admin_disabled_model_stays_off_after_key_restored():
    admin_manual = _discovered("kimi", "kimi-admin-off-restored")
    admin_marked = _discovered("deepseek", "deepseek-admin-off-restored")
    prior = {}
    prior.update(_prior("kimi-admin-off-restored", "kimi", activation_source="manual", activation_probe=_OK_PROBE))
    prior.update(_prior("deepseek-admin-off-restored", "deepseek", activation_source="db",
                        activation_probe=_OK_PROBE, deactivation_source="admin"))
    probe = _prober({})

    await model_registry.apply_activation_probes([admin_manual, admin_marked], prior, now=NOW, prober=probe)

    assert probe.calls == []
    assert admin_manual["is_active"] is False and admin_manual["activation_source"] == "manual"
    assert admin_marked["is_active"] is False and admin_marked["activation_source"] == "manual"


@pytest.mark.asyncio
async def test_google_policy_blocks_litellm_gemini_alias_even_with_probe_history_or_active_prior():
    raw = {"litellm_params": {"model": "gemini/gemini-3-flash"}}
    with_history = _discovered("litellm", "alias-g-history", raw=raw)
    active_prior = _discovered("litellm", "alias-g-active", raw=raw)
    system_off = _discovered("litellm", "alias-g-system-off", raw=raw)
    prior = {}
    prior.update(_prior("alias-g-history", "litellm", activation_source="db", is_active=True, is_executable=True,
                        activation_probe=_OK_PROBE))
    prior.update(_prior("alias-g-active", "litellm", activation_source="db", is_active=True, is_executable=True))
    prior.update(_prior("alias-g-system-off", "litellm", activation_probe=_OK_PROBE, deactivation_source="system"))
    probe = _prober({})

    await model_registry.apply_activation_probes(
        [with_history, active_prior, system_off], prior, now=NOW, prober=probe, google_routes_enabled=False,
    )

    assert probe.calls == []
    for row in (with_history, active_prior, system_off):
        assert row["is_active"] is False
        assert row["metadata"]["activation_review_reason"] == "google_routes_disabled"


@pytest.mark.asyncio
async def test_google_policy_does_not_block_antigravity_rows_and_allows_when_enabled():
    agy = _discovered("antigravity", "gemini-3-pro-agy")
    agy_prior = _prior("gemini-3-pro-agy", "antigravity", is_active=True, is_executable=True,
                       activation_source="db", activation_probe=_OK_PROBE)
    alias = _discovered("litellm", "alias-g-enabled", raw={"litellm_params": {"model": "gemini/gemini-3-flash"}})
    alias_prior = _prior("alias-g-enabled", "litellm", is_active=True, is_executable=True, activation_source="db",
                         activation_probe=_OK_PROBE)
    prior = {**agy_prior, **alias_prior}

    await model_registry.apply_activation_probes(
        [agy], agy_prior, now=NOW, prober=_prober({}), google_routes_enabled=False,
    )
    await model_registry.apply_activation_probes(
        [alias], prior, now=NOW, prober=_prober({}), google_routes_enabled=True,
    )

    assert agy["metadata"].get("activation_review_reason") != "google_routes_disabled"
    assert agy["is_active"] is True
    assert alias["is_active"] is True


@pytest.mark.asyncio
async def test_activation_disabled_flag_keeps_existing_state_and_blocks_only_new_activation():
    kept = _discovered("kimi", "kimi-auto-active")
    fresh = _discovered("kimi", "kimi-brand-new")
    prior = _prior("kimi-auto-active", "kimi", is_active=True, is_executable=True, activation_source="db",
                   verification_status="verified", activation_probe=_OK_PROBE)
    probe = _prober({})

    summary = await model_registry.apply_activation_probes(
        [kept, fresh], prior, now=NOW, prober=probe, activation_enabled=False,
    )

    assert probe.calls == []
    assert kept["is_active"] is True and kept["activation_source"] == "db"
    assert kept["verification_status"] == "verified"
    assert kept["metadata"]["activation_probe"] == _OK_PROBE
    assert summary["carried_active"] == ["kimi:kimi-auto-active"]
    assert fresh["is_active"] is False
    assert fresh["metadata"]["activation_review_reason"] == "auto_activation_disabled"


class _SyncTx:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


class _SyncConn:
    """sync 가 쓰는 SQL 만 흉내 낸다. llm_models UPSERT 결과를 다음 sync 의 활성화 상태로 돌려준다."""

    def __init__(self):
        self.models: dict[tuple[str, str], dict] = {}

    def transaction(self):
        return _SyncTx()

    async def fetch(self, query, *args):
        if "activation_source = 'manual'" in query:
            return []
        if query.strip() == "SELECT provider, model_id FROM llm_models":
            return [{"provider": p, "model_id": m} for p, m in self.models]
        if "activation_probe" in query:
            return [
                {
                    "provider": p, "model_id": m, "is_active": v["is_active"], "is_executable": v["is_executable"],
                    "activation_source": v["activation_source"], "verification_status": v["verification_status"],
                    "discovery_source": v["discovery_source"], "retired_at": None,
                    "activation_probe": (v["metadata"].get("activation_probe") and
                                         json.dumps(v["metadata"]["activation_probe"])),
                    "deactivation_source": v["metadata"].get("deactivation_source"),
                }
                for (p, m), v in self.models.items()
            ]
        if "status = 'verified'" in query or "MAX(created_at) AS last_success_at" in query:
            return []
        if "INSERT INTO llm_model_candidates" in query or "SELECT provider, model_id, execution_model_id" in query:
            return []
        if "FROM model_routing_preferences" in query or "INSERT INTO model_routing_preferences" in query:
            return []
        raise AssertionError(f"unexpected fetch: {query[:80]}")

    async def execute(self, query, *args):
        if "INSERT INTO llm_models" in query:
            self.models[(args[0], args[1])] = {
                "is_active": args[11], "activation_source": args[12], "metadata": json.loads(args[14]),
                "discovery_source": args[16], "verification_status": args[17], "is_executable": args[22],
            }
        return "OK"


@pytest.mark.asyncio
async def test_sync_twice_with_auto_activate_off_keeps_active_state_and_probe_history(monkeypatch):
    conn = _SyncConn()
    row_template = _discovered("kimi", "kimi-auto-active")
    conn.models[("kimi", "kimi-auto-active")] = {
        "is_active": True, "activation_source": "db", "metadata": {"activation_probe": _OK_PROBE},
        "discovery_source": "kimi_api", "verification_status": "verified", "is_executable": True,
    }

    class _Acq:
        async def __aenter__(self_inner):
            return conn

        async def __aexit__(self_inner, *exc):
            return False

    class _P:
        def acquire(self_inner):
            return _Acq()

    async def fake_discover(key_rows, **kwargs):
        run = {"provider": "kimi", "status": "ok", "state": "ok", "count": 1, "model_source": "discovery",
               "last_success_at": NOW.isoformat()}
        return [dict(row_template, metadata=dict(row_template["metadata"]))], [run]

    monkeypatch.setenv("LLM_MODEL_AUTO_ACTIVATE", "0")
    monkeypatch.setattr(model_registry, "get_pool", lambda: _P())
    monkeypatch.setattr(model_registry, "_fetch_key_rows", AsyncMock(return_value=_key_rows("kimi")))
    monkeypatch.setattr(model_registry, "append_key_audit_log", AsyncMock())
    monkeypatch.setattr(model_registry, "discover_provider_model_rows", fake_discover)
    monkeypatch.setattr("app.services.ohvis_alert.notify", AsyncMock(return_value=True))

    for _ in range(2):
        result = await model_registry.sync_model_registry(triggered_by="test", reason="unit")
        assert result["ok"] is True
        saved = conn.models[("kimi", "kimi-auto-active")]
        assert saved["is_active"] is True and saved["is_executable"] is True
        assert saved["activation_source"] == "db"
        assert saved["metadata"]["activation_probe"] == _OK_PROBE
