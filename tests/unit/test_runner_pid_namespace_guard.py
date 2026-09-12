"""러너 PID 생존 판정이 컨테이너 PID namespace에 속지 않는지 검증한다.

API가 자체 PID namespace에서 돌면 호스트 러너 PID가 /proc 에 보이지 않아
살아 있는 작업이 process_died 로 종결된 사고가 있었다(runner-a13e6f37).
"""
import os

import pytest

os.environ.setdefault("JWT_SECRET_KEY", "unit-test-secret")
os.environ.setdefault("E2B_API_KEY", "unit-test-e2b-key")

from app.api import pipeline_runner


@pytest.fixture(autouse=True)
def _clear_namespace_cache():
    pipeline_runner._runner_pid_namespace_visible.cache_clear()
    yield
    pipeline_runner._runner_pid_namespace_visible.cache_clear()


def test_namespace_visible_outside_container(monkeypatch):
    monkeypatch.setattr(os.path, "exists", lambda path: path != "/.dockerenv")
    assert pipeline_runner._runner_pid_namespace_visible() is True


def test_namespace_hidden_in_private_pid_namespace(monkeypatch, tmp_path):
    comm = tmp_path / "comm"
    comm.write_text("uvicorn\n", encoding="utf-8")
    monkeypatch.setattr(os.path, "exists", lambda path: path == "/.dockerenv")
    monkeypatch.setattr(
        pipeline_runner, "open", lambda *a, **k: comm.open(encoding="utf-8"), raising=False
    )
    assert pipeline_runner._runner_pid_namespace_visible() is False


def test_namespace_visible_with_host_pid_mode(monkeypatch, tmp_path):
    comm = tmp_path / "comm"
    comm.write_text("systemd\n", encoding="utf-8")
    monkeypatch.setattr(os.path, "exists", lambda path: path == "/.dockerenv")
    monkeypatch.setattr(
        pipeline_runner, "open", lambda *a, **k: comm.open(encoding="utf-8"), raising=False
    )
    assert pipeline_runner._runner_pid_namespace_visible() is True


def test_pid_liveness_is_unknown_when_namespace_hidden(monkeypatch):
    monkeypatch.setattr(pipeline_runner, "_runner_pid_namespace_visible", lambda: False)
    # 호스트 PID 는 컨테이너 /proc 에 없다. 죽었다고 단정하면 안 된다.
    assert pipeline_runner._local_pid_alive(424898) is None


def test_pid_liveness_is_reported_when_namespace_visible(monkeypatch):
    monkeypatch.setattr(pipeline_runner, "_runner_pid_namespace_visible", lambda: True)
    assert pipeline_runner._local_pid_alive(os.getpid()) is True
    monkeypatch.setattr(os.path, "exists", lambda path: False)
    assert pipeline_runner._local_pid_alive(999999) is False


def test_missing_pid_stays_none():
    assert pipeline_runner._local_pid_alive(None) is None
    assert pipeline_runner._local_pid_alive(0) is None
