from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest


def test_resolution_exercises_exact_and_vector_without_llm() -> None:
    pytest.importorskip("asyncpg")
    from scripts import smart_browser_readonly_e2e as e2e

    exact, vector, events = asyncio.run(e2e._exercise_skill_resolution())

    assert exact["route"] == "exact"
    assert exact["reason_code"] == "EXACT_CANONICAL_MATCH"
    assert vector["route"] == "qwen3_vector"
    assert vector["reason_code"] == "QWEN3_VECTOR_MATCH"
    assert [event["reason"] for event in events] == ["exact", "qwen3_vector"]
    assert all(stage.get("cost_usd", 0.0) == 0.0 for result in (exact, vector)
               for stage in result["audit"])


def test_readonly_journey_emits_capture_snapshot_and_chat_artifact(tmp_path: Path) -> None:
    from scripts import smart_browser_readonly_e2e as e2e

    pytest.importorskip("playwright.async_api")
    pytest.importorskip("asyncpg")
    try:
        result = asyncio.run(e2e.run(tmp_path))
    except Exception as exc:
        if "Executable doesn't exist" in str(exc):
            pytest.skip("Playwright Chromium is not installed")
        raise

    assert result["status"] == "passed"
    assert result["browser_e2e_executed"] is True
    assert all(result["checks"].values())
    assert result["write_actions"] == 0
    assert result["llm_calls"] == 0
    assert result["llm_cost_usd"] == 0.0
    assert result["route"] == {"exact": "exact", "vector": "qwen3_vector"}
    assert all(Path(path).is_file() for path in result["screenshots"].values())
    assert Path(result["aria_snapshot"]).is_file()
    artifact = json.loads(Path(result["chat_artifact"]).read_text(encoding="utf-8"))
    assert artifact["candidate"]["status"] == "candidate"
    assert artifact["candidate_plus_one"] == {
        "version": "2", "status": "candidate", "active_version": "1",
        "active_preserved": True,
    }
    assert artifact["reason_codes"] == [
        "EXACT_CANONICAL_MATCH", "QWEN3_VECTOR_MATCH", "critical_required_anchor_missing",
    ]


def test_browser_failure_writes_ordered_api_fallback_artifacts(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    from scripts import smart_browser_readonly_e2e as e2e

    # Test the fallback helper directly: module import/browser launch failures
    # must have the same read-only diagnostic result and no LLM/write side effect.
    monkeypatch.setattr(e2e, "_http_status", lambda url: {"url": url, "status": 200, "reachable": True})
    result = e2e._browser_failure_result(
        output_dir=tmp_path, error=RuntimeError("Executable doesn't exist"), started=0,
    )

    assert result["status"] == "degraded"
    assert result["message"] == "브라우저 E2E 미실행, API 검증으로 대체"
    assert result["fallback"]["fallback_chain"] == ["http_status", "api_health", "container_process"]
    assert result["llm_calls"] == result["write_actions"] == 0
    assert Path(result["fallback_artifact"]).is_file()
    artifact = json.loads(Path(result["chat_artifact"]).read_text(encoding="utf-8"))
    assert artifact["reason_codes"] == ["BROWSER_CAPTURE_UNAVAILABLE"]
