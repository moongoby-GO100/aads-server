"""AADS-LLM-ROUTE-AUTOSYNC-FROM-REGISTRY-20261001 — llm_models → 채팅 LLM(route_key='llm') 비활성 후보 반영."""
from __future__ import annotations

import asyncio

from app.services import cli_model_autoreg as autoreg


def _row(provider="codex", model_id="gpt-6.1-sol", **over):
    base = {
        "provider": provider,
        "model_id": model_id,
        "execution_model_id": model_id,
        "category": "chat",
        "discovery_source": "codex_cli_cache",
        "is_active": True,
        "is_selectable": True,
        "is_executable": True,
        "verification_status": "verified",
        "retired_at": None,
    }
    base.update(over)
    return base


class FakeConn:
    def __init__(self, registry, prefs):
        self.registry = registry
        # (route_key, provider, model_id) -> {"is_enabled", "is_default", "display_order"}
        self.prefs = dict(prefs)
        self.inserts = []

    async def fetch(self, query, *args):
        if "FROM llm_models" in query:
            return [dict(r) for r in self.registry]
        if "FROM model_routing_preferences" in query:
            return [{"provider": p, "model_id": m} for (rk, p, m) in self.prefs if rk == args[0]]
        if "INSERT INTO model_routing_preferences" in query:
            assert "DO NOTHING" in query and "FALSE, FALSE" in query
            route_key, provider, model_id, order, note, actor = args
            key = (route_key, provider, model_id)
            if key in self.prefs:
                return []
            self.prefs[key] = {"is_enabled": False, "is_default": False, "display_order": order}
            self.inserts.append((key, note, actor))
            return [{"provider": provider, "model_id": model_id}]
        raise AssertionError(query)


def _run(coro):
    return asyncio.run(coro)


def test_new_verified_model_inserted_as_disabled_candidate():
    conn = FakeConn([_row()], {})
    inserted = _run(autoreg.register_chat_llm_candidates(conn))
    assert inserted == [("codex", "gpt-6.1-sol")]
    pref = conn.prefs[("llm", "codex", "gpt-6.1-sol")]
    assert pref == {"is_enabled": False, "is_default": False, "display_order": 200}
    _, note, actor = conn.inserts[0]
    assert actor == autoreg.AUTO_DISCOVERY_ACTOR
    assert "운영 화면에서 켜야 사용" in note


def test_existing_rows_untouched_and_idempotent():
    existing = {
        ("llm", "anthropic", "claude-opus-5-5"): {"is_enabled": True, "is_default": True, "display_order": 1},
        ("llm", "gemini", "gemini-3.8-flash"): {"is_enabled": True, "is_default": False, "display_order": 7},
    }
    before = {k: dict(v) for k, v in existing.items()}
    conn = FakeConn(
        [
            _row("anthropic", "claude-opus-5-5"),
            _row("gemini", "gemini-3.8-flash"),
            _row("codex", "gpt-6-sol"),
        ],
        existing,
    )
    assert _run(autoreg.register_chat_llm_candidates(conn)) == [("codex", "gpt-6-sol")]
    for key, value in before.items():
        assert conn.prefs[key] == value
    assert _run(autoreg.register_chat_llm_candidates(conn)) == []
    enabled = [k for k, v in conn.prefs.items() if v["is_enabled"]]
    assert sorted(enabled) == sorted(before)


def test_excluded_rows_not_inserted():
    registry = [
        _row("openai", "dall-e-3", category="media_image"),
        _row("openai", "text-embedding-3", category="embedding"),
        _row("google", "grounded", category="search"),
        _row("google", "deep", category="research"),
        _row("openai", "gpt-5-2026-01-15", discovery_source="openai_api"),
        _row("gemini", "unverified", verification_status="review_required"),
        _row("gemini", "inactive", is_active=False),
        _row("gemini", "not-selectable", is_selectable=False),
        _row("gemini", "not-executable", is_executable=False),
        _row("gemini", "retired", retired_at="2026-09-01"),
        _row("openai", "gpt-5", discovery_source="openai_api"),
        _row("gemini", "ok-status", verification_status="ok"),
    ]
    conn = FakeConn(registry, {})
    inserted = _run(autoreg.register_chat_llm_candidates(conn))
    assert sorted(inserted) == [("gemini", "ok-status"), ("openai", "gpt-5")]


def test_date_snapshot_kept_for_non_openai_api_source():
    conn = FakeConn([_row("anthropic", "claude-x-2026-01-15", discovery_source="anthropic_api")], {})
    assert _run(autoreg.register_chat_llm_candidates(conn)) == [("anthropic", "claude-x-2026-01-15")]


def test_execution_model_id_match_prevents_duplicate():
    conn = FakeConn(
        [_row("openai", "gpt-alias", execution_model_id="gpt-real")],
        {("llm", "openai", "gpt-real"): {"is_enabled": True, "is_default": False, "display_order": 3}},
    )
    assert _run(autoreg.register_chat_llm_candidates(conn)) == []
    assert conn.inserts == []


def test_autoregister_after_sync_reports_chat_candidates(monkeypatch):
    conn = FakeConn([_row()], {})

    async def _noop(*a, **k):
        return []

    monkeypatch.setattr(autoreg, "register_codex_candidates", _noop)
    monkeypatch.setattr(autoreg, "register_runner_llm_candidates", _noop)
    monkeypatch.setattr(autoreg, "_notify_verified", _noop)
    summary = _run(
        autoreg.autoregister_after_sync(
            conn, model_rows=[], existing_keys=set(), template_codex_ids=[], verified_codex_ids=set()
        )
    )
    assert summary["chat_llm_candidates_inserted"] == ["codex:gpt-6.1-sol"]
