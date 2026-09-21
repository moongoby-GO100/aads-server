import asyncio
import json
from types import SimpleNamespace

from app.api import watchdog
from app.services import error_book


def test_ingest_known_error_bumps_without_candidate(monkeypatch):
    bumped = []

    async def fake_match(_text):
        return [{"error_key": "known.timeout"}]

    async def fake_bump(key):
        bumped.append(key)

    async def fail_record(*_args, **_kwargs):
        raise AssertionError("known errors must not create candidates")

    monkeypatch.setattr(error_book, "match_error", fake_match)
    monkeypatch.setattr(error_book, "bump_recurrence", fake_bump)
    monkeypatch.setattr(error_book, "record_candidate", fail_record)

    result = asyncio.run(
        error_book.ingest_error_event(
            error_type="timeout",
            source="watchdog",
            message="request timeout",
        )
    )

    assert result == {"status": "matched", "error_keys": ["known.timeout"]}
    assert bumped == ["known.timeout"]


def test_ingest_unknown_error_records_project_and_origin(monkeypatch):
    captured = {}

    async def fake_match(_text):
        return []

    async def fake_record(text, source="", **kwargs):
        captured.update(text=text, source=source, **kwargs)
        return "auto.abc123"

    monkeypatch.setattr(error_book, "match_error", fake_match)
    monkeypatch.setattr(error_book, "record_candidate", fake_record)

    result = asyncio.run(
        error_book.ingest_error_event(
            error_type="api_failure",
            source="orders.api",
            server="contabo14",
            message="request failed",
            project="GO100",
            error_hash="deadbeef",
        )
    )

    assert result == {"status": "candidate", "error_key": "auto.abc123"}
    assert captured["project"] == "GO100"
    assert captured["source"] == "error_log:orders.api"
    assert captured["metadata"]["error_hash"] == "deadbeef"
    assert "request failed" in captured["text"]


def test_record_candidate_serializes_metadata_and_project(monkeypatch):
    calls = []

    class FakePool:
        async def execute(self, sql, *args):
            calls.append((sql, args))

    monkeypatch.setattr("app.core.db_pool.get_pool", lambda: FakePool())

    key = asyncio.run(
        error_book.record_candidate(
            "ERROR request 123 failed",
            source="error_log:test",
            project="go100",
            metadata={"error_hash": "abc"},
        )
    )

    assert key.startswith("auto.")
    _, args = calls[0]
    assert args[0] == "GO100"
    payload = json.loads(args[3])
    assert payload["source"] == "error_log:test"
    assert payload["error_hash"] == "abc"


def test_watchdog_records_json_context_and_syncs_error_book(monkeypatch):
    inserted = {}

    class FakeConn:
        async def fetchrow(self, sql, *args):
            if "SELECT id, occurrence_count" in sql:
                return None
            if "INSERT INTO error_log" in sql:
                inserted["args"] = args
                return {"id": 17}
            raise AssertionError(sql)

    class Acquire:
        async def __aenter__(self):
            return FakeConn()

        async def __aexit__(self, *_args):
            return False

    class FakePool:
        def acquire(self):
            return Acquire()

    synced = {}

    async def fake_ingest(**kwargs):
        synced.update(kwargs)
        return {"status": "candidate", "error_key": "auto.watchdog"}

    monkeypatch.setattr(watchdog.memory_store, "pool", FakePool())
    monkeypatch.setattr(error_book, "ingest_error_event", fake_ingest)

    result = asyncio.run(
        watchdog.report_error(
            watchdog.ErrorReport(
                error_type="synthetic_failure",
                source="unit.test",
                server="contabo116",
                message="synthetic request failed",
                context={"project": "AADS", "attempt": 2},
            ),
            SimpleNamespace(client=None),
            auth=True,
            _rate=None,
        )
    )

    assert result["status"] == "recorded_new"
    assert result["error_book"]["error_key"] == "auto.watchdog"
    assert json.loads(inserted["args"][6]) == {"project": "AADS", "attempt": 2}
    assert synced["error_type"] == "synthetic_failure"
