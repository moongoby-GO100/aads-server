"""원격 핸드오버 HTTP 요청의 User-Agent 회귀 테스트."""
import importlib.util
from pathlib import Path
from unittest.mock import MagicMock

import pytest


@pytest.mark.parametrize("auth_env", ["AADS_HANDOVER_WRITE_KEY", "AADS_MONITOR_KEY"])
def test_http_fallback_sets_runner_user_agent(monkeypatch, auth_env):
    path = Path(__file__).resolve().parents[2] / "scripts" / "runner_handover_write.py"
    spec = importlib.util.spec_from_file_location("runner_handover_write", path)
    helper = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(helper)
    monkeypatch.setenv("AADS_API_URL", "https://handover.invalid")
    monkeypatch.delenv("AADS_HANDOVER_WRITE_KEY", raising=False)
    monkeypatch.delenv("AADS_MONITOR_KEY", raising=False)
    monkeypatch.setenv(auth_env, "unit-test-key")
    opener = MagicMock()
    response = opener.return_value.__enter__.return_value
    response.status = 201
    response.read.return_value = b'{"entry": {"revision": 1}, "changed": true}'

    result = helper.http_fallback_write(
        {"job_id": "ua-test", "project": "AADS", "stage": "done", "job": {}},
        opener=opener,
    )

    assert result["ok"] is True
    opener.assert_called_once()
    request = opener.call_args.args[0]
    user_agent = request.get_header("User-agent")
    assert user_agent
    assert user_agent == "aads-runner-handover/1"
    assert not user_agent.startswith("Python-urllib")
