from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from scripts import smart_browser_readonly_e2e as e2e


def test_resolution_exercises_exact_and_vector_without_llm() -> None:
    exact, vector, events = asyncio.run(e2e._exercise_skill_resolution())

    assert exact["route"] == "exact"
    assert exact["reason_code"] == "EXACT_CANONICAL_MATCH"
    assert vector["route"] == "qwen3_vector"
    assert vector["reason_code"] == "QWEN3_VECTOR_MATCH"
    assert [event["reason"] for event in events] == ["exact", "qwen3_vector"]
    assert all(stage.get("cost_usd", 0.0) == 0.0 for result in (exact, vector)
               for stage in result["audit"])


def test_readonly_journey_emits_capture_snapshot_and_chat_artifact(tmp_path: Path) -> None:
    pytest.importorskip("playwright.async_api")
    try:
        result = asyncio.run(e2e.run(tmp_path))
    except Exception as exc:
        if "Executable doesn't exist" in str(exc):
            pytest.skip("Playwright Chromium is not installed")
        raise

    assert result["status"] == "passed"
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
