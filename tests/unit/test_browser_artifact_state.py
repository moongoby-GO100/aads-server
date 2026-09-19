from app.services.browser_artifact_state import build_browser_artifact_status


def test_artifact_status_exposes_recovery_without_internal_ids():
    result = build_browser_artifact_status(
        task={"id": "task-1", "status": "failed", "target_url": "https://shop.example", "error": "selector changed",
              "result": {"site_knowledge": {"learning_state": "rediscover", "evidence_refs": ["object://e/1"]},
                         "freshness_gate": {"status": "STALE"}}},
        frame={"current_step": "구조 다시 찾기", "metadata": {"source": "self_hosted_playwright", "progress_percent": 62}},
        events=[],
    )
    assert result["execution_actor"] == "Browser"
    assert result["learning_state"] == "rediscover"
    assert result["freshness_status"] == "STALE"
    assert result["progress_percent"] == 62
    assert result["can_retry"] is True
    assert "skill_id" not in result
