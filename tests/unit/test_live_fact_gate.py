import asyncio
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import app.services.live_fact_gate as gate

NOW = datetime(2026, 9, 19, 8, 0, tzinfo=UTC)


def _record(**overrides):
    item = {
        "id": uuid4(),
        "tenant_id": uuid4(),
        "fact_type": "price",
        "entity_key": "product-1",
        "variant_key": "red-xl",
        "account_context_hash": gate.hash_account_context("acct-a"),
        "source_url": "https://shop.example/items/1",
        "revalidator_key": "test.provider",
        "observed_value": {"amount": 1000, "currency": "KRW"},
        "observed_at": NOW - timedelta(seconds=5),
        "expires_at": NOW + timedelta(seconds=30),
        "revalidated_at": NOW - timedelta(seconds=5),
        "freshness_status": "CURRENT",
        "evidence_id": "evidence-1",
        "version": 1,
    }
    item.update(overrides)
    return item


def test_display_fact_redacts_expired_value_and_supplies_retry():
    result = gate.display_fact(_record(expires_at=NOW - timedelta(seconds=1)), now=NOW)
    assert result["freshness_status"] == "STALE"
    assert result["value"] is None
    assert result["retry_action"]["action"] == "revalidate_fact"


def test_display_fact_rejects_variant_and_account_context_mismatch():
    result = gate.display_fact(
        _record(), now=NOW,
        expected_context={"variant_key": "blue-m", "account_context_hash": gate.hash_account_context("acct-b")},
    )
    assert result["freshness_status"] == "CONFLICT"
    assert result["value"] is None


def test_display_fact_requires_evidence_and_revalidation_timestamp():
    assert gate.display_fact(_record(evidence_id=None), now=NOW)["freshness_status"] == "UNAVAILABLE"
    assert gate.display_fact(_record(revalidated_at=None), now=NOW)["freshness_status"] == "UNAVAILABLE"


def test_display_fact_decodes_asyncpg_jsonb_and_rejects_hash_mismatch():
    value = {"amount": 1000, "currency": "KRW"}
    decoded = gate.display_fact(
        _record(observed_value=json.dumps(value), observed_value_hash=gate.value_hash(value)),
        now=NOW,
    )
    conflicted = gate.display_fact(
        _record(observed_value=json.dumps(value), observed_value_hash="0" * 64),
        now=NOW,
    )

    assert decoded["freshness_status"] == "CURRENT"
    assert decoded["value"] == value
    assert conflicted["freshness_status"] == "CONFLICT"
    assert conflicted["value"] is None


def test_source_url_drops_query_credentials_and_rejects_userinfo():
    assert gate.normalize_source_url("HTTPS://Shop.Example/items/1?token=secret#part") == "https://shop.example/items/1"
    try:
        gate.normalize_source_url("https://user:secret@shop.example/items/1")
    except gate.LiveFactError as exc:
        assert exc.code == "INVALID_FACT_SOURCE"
    else:
        raise AssertionError("credential-bearing source URL must be rejected")


def test_sse_replay_drops_value_after_ttl_expiry():
    event = "data: " + json.dumps({"live_facts": [{
        "fact_id": "fact-1", "fact_type": "stock", "value": 8,
        "freshness_status": "CURRENT", "evidence_id": "e1",
        "observed_at": (NOW - timedelta(minutes=1)).isoformat(),
        "revalidated_at": (NOW - timedelta(minutes=1)).isoformat(),
        "expires_at": (NOW - timedelta(seconds=1)).isoformat(),
    }]}) + "\n\n"
    sanitized = json.loads(gate.sanitize_sse_event(event, now=NOW)[6:-2])
    fact = sanitized["live_facts"][0]
    assert fact["freshness_status"] == "STALE"
    assert fact["value"] is None


def test_source_or_entity_change_is_conflict(monkeypatch):
    record = _record()

    async def load(**_kwargs):
        return dict(record)

    async def persist(_record_value, *, status, now, evidence):
        return {"freshness_status": status.value, "value": None, "evidence": evidence, "now": now}

    async def provider(_record_value):
        return {
            "value": {"amount": 900}, "source_url": "https://evil.example/items/1",
            "entity_key": "product-2", "variant_key": "red-xl",
            "account_context_hash": record["account_context_hash"],
            "evidence_id": "evidence-2", "observed_at": NOW, "expires_at": NOW + timedelta(seconds=30),
        }

    monkeypatch.setattr(gate, "_load_fact", load)
    monkeypatch.setattr(gate, "_persist_revalidation", persist)
    gate.register_fact_revalidator("test.provider", provider)
    try:
        result = asyncio.run(gate.revalidate_live_fact(
            tenant_id=str(record["tenant_id"]), fact_id=str(record["id"]), now=NOW,
        ))
    finally:
        gate.unregister_fact_revalidator("test.provider")
    assert result["freshness_status"] == "CONFLICT"
    assert result["value"] is None


def test_concurrent_non_force_revalidation_calls_source_once(monkeypatch):
    record = _record(expires_at=NOW - timedelta(seconds=1), freshness_status="STALE")
    calls = 0

    async def load(**_kwargs):
        return dict(record)

    async def persist(_record_value, *, status, now, evidence):
        record.update({
            "observed_value": evidence["value"], "observed_at": now,
            "expires_at": now + timedelta(seconds=30), "revalidated_at": now,
            "freshness_status": status.value, "evidence_id": evidence["evidence_id"],
        })
        return gate.display_fact(record, now=now)

    async def provider(_record_value):
        nonlocal calls
        calls += 1
        await asyncio.sleep(0.01)
        return {
            "value": 7, "source_url": record["source_url"], "entity_key": record["entity_key"],
            "variant_key": record["variant_key"], "account_context_hash": record["account_context_hash"],
            "evidence_id": "evidence-new", "observed_at": NOW, "expires_at": NOW + timedelta(seconds=30),
        }

    monkeypatch.setattr(gate, "_load_fact", load)
    monkeypatch.setattr(gate, "_persist_revalidation", persist)
    gate.register_fact_revalidator("test.provider", provider)

    async def exercise():
        return await asyncio.gather(*[
            gate.revalidate_live_fact(
                tenant_id=str(record["tenant_id"]), fact_id=str(record["id"]), force=False, now=NOW,
            ) for _ in range(2)
        ])

    try:
        results = asyncio.run(exercise())
    finally:
        gate.unregister_fact_revalidator("test.provider")
    assert calls == 1
    assert {item["freshness_status"] for item in results} == {"CURRENT"}


def test_payload_gate_redacts_known_live_fields(monkeypatch):
    async def revalidate(**kwargs):
        return {
            "fact_id": kwargs["fact_id"], "freshness_status": "UNAVAILABLE", "value": None,
            "retry_action": {"action": "revalidate_fact", "fact_id": kwargs["fact_id"]},
        }

    monkeypatch.setattr(gate, "revalidate_live_fact", revalidate)
    payload = {"live_fact_ids": [str(uuid4())], "price": 1000, "nested": {"stock": 3, "label": "item"}}
    result = asyncio.run(gate.guard_payload_for_display(payload, tenant_id=str(uuid4())))
    assert result["freshness_gate"]["status"] == "BLOCKED"
    assert result["price"] is None and result["nested"]["stock"] is None
    assert result["nested"]["label"] == "item"


def test_migration_has_tenant_context_evidence_and_append_only_event_ledger():
    sql = (Path(__file__).resolve().parents[2] / "migrations/20260919_g5_live_fact_revalidation.sql").read_text()
    for token in (
        "tenant_id UUID NOT NULL", "entity_key TEXT NOT NULL", "variant_key TEXT NOT NULL",
        "account_context_hash", "expires_at TIMESTAMPTZ NOT NULL", "evidence_id TEXT",
        "browser_live_fact_events", "freshness_status IN ('CURRENT','STALE','UNAVAILABLE','CONFLICT')",
    ):
        assert token in sql


def test_browser_artifact_and_sse_final_display_paths_use_freshness_gate():
    root = Path(__file__).resolve().parents[2]
    browser_api = (root / "app/api/browser_tasks.py").read_text()
    artifact_api = (root / "app/api/artifacts.py").read_text()
    chat_service = (root / "app/services/chat_service.py").read_text()
    redis_stream = (root / "app/services/redis_stream.py").read_text()
    assert "guard_payload_for_display" in browser_api
    assert "guard_payload_for_display" in artifact_api
    assert "_guard_artifact_for_display" in chat_service
    assert "sanitize_sse_event" in redis_stream
