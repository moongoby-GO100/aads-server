from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from app.api.directives import get_model_for_size


ROOT = Path(__file__).resolve().parents[2]
SIZES = ("XS", "S", "M", "L", "XL")


@pytest.mark.parametrize("size", SIZES)
def test_generated_directives_default_to_sonnet_5(size):
    assert get_model_for_size(size) == "claude-sonnet-5"


@pytest.mark.asyncio
@pytest.mark.parametrize("size", SIZES)
async def test_runner_db_failure_default_matches_sonnet_5(monkeypatch, size):
    from app.api import pipeline_runner

    monkeypatch.setattr(
        pipeline_runner,
        "_get_model_cycle_for_size",
        AsyncMock(return_value=[]),
    )
    assert await pipeline_runner._get_model_for_size(object(), size) == "claude-sonnet-5"


def test_shell_runner_and_legacy_trigger_do_not_default_to_sonnet_46():
    runner = (ROOT / "scripts/pipeline-runner.sh").read_text(encoding="utf-8")
    runner_local = (ROOT / "scripts/pipeline-runner.sh.local").read_text(encoding="utf-8")
    trigger = (ROOT / "scripts/auto_trigger.sh").read_text(encoding="utf-8")
    executor = (ROOT / "scripts/claude_exec.sh").read_text(encoding="utf-8")

    assert runner == runner_local
    assert 'claude_fb="claude-sonnet-5"' in runner
    assert 'claude_primary="claude-sonnet-5"' in runner
    assert '${model:-claude-sonnet-5}' in trigger
    assert 'MODEL="${3:-claude-sonnet-5}"' in executor
