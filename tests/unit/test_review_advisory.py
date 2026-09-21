import asyncio

import pytest

from app.services import review_advisory


def test_advisory_verdict_uses_authoritative_review_weights() -> None:
    payload = {
        "correctness": 0.9,
        "security": 0.8,
        "scope_compliance": 0.7,
        "preservation": 0.8,
        "quality": 0.7,
    }

    verdict, score = review_advisory._advisory_verdict(payload)

    assert verdict == "APPROVE"
    assert score == pytest.approx(0.8)


@pytest.mark.parametrize(
    "payload",
    [None, {}, {"correctness": 2}, {"correctness": "not-a-number"}],
)
def test_advisory_verdict_rejects_incomplete_or_invalid_payload(payload) -> None:
    assert review_advisory._advisory_verdict(payload) == (None, None)


def test_schedule_keeps_task_alive_and_never_blocks_primary(monkeypatch) -> None:
    async def scenario() -> None:
        started = asyncio.Event()
        release = asyncio.Event()

        async def fake_run(**kwargs):
            started.set()
            await release.wait()

        monkeypatch.setattr(review_advisory, "_run_advisory", fake_run)

        review_advisory.schedule_review_advisory(
            project="AADS",
            job_id="runner-shadow-test",
            prompt="review",
            primary_verdict="APPROVE",
            primary_model="codex:gpt-5.6-luna",
            diff_size=10,
        )
        await asyncio.wait_for(started.wait(), timeout=1)
        assert any(task.get_name() == "review-advisory-runner-shadow-test"
                   for task in review_advisory._ADVISORY_TASKS)

        release.set()
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        assert not review_advisory._ADVISORY_TASKS

    asyncio.run(scenario())


def test_authoritative_reviewer_stays_cli_only() -> None:
    from app.services.code_reviewer import _is_cli_review_model

    assert _is_cli_review_model("codex:gpt-5.6-luna") is True
    assert _is_cli_review_model("claude-sonnet-5") is True
    assert _is_cli_review_model("pc-qwen38-27b") is False
    assert _is_cli_review_model("litellm:pc-qwen38-27b") is False
