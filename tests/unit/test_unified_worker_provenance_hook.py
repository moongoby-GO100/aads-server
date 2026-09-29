"""통합 컴포넌트 배포 워커의 release provenance 훅 계약.

워커는 인증 확정(success/release_certified) 직후 record-release-provenance.sh 를
정확히 한 번 부른다. 레코더 실패는 배포 성공을 뒤집지 않고, 인증되지 않은
상태(success_partial 등)에서는 부르지 않는다. DB 에 직접 INSERT 하지 않는다 —
인증 가드는 레코더 SQL 이 쥔다.
"""

from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace

import pytest

ROOT = Path(__file__).parents[2]
sys.path.insert(0, str(ROOT))

# app.services.__init__ 의 런타임 의존성(asyncpg 등) 없이 targets 모듈만 로드한다.
services_package = ModuleType("app.services")
services_package.__path__ = [str(ROOT / "app/services")]
sys.modules.setdefault("app.services", services_package)

_spec = importlib.util.spec_from_file_location(
    "unified_worker_under_test", ROOT / "scripts/unified_component_deploy_worker.py"
)
assert _spec and _spec.loader
worker = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(worker)

ROW = {"project": "FOOD", "component": "store-assistant", "release_sha": "abcdef1",
       "status": "queued", "target_env": "production"}


def _target(tmp_path: Path, executor: str = "local"):
    (tmp_path / ".git").mkdir(exist_ok=True)
    return SimpleNamespace(executor=executor, repo_path=str(tmp_path), key=("FOOD", "store-assistant"))


@pytest.fixture
def recorder_calls(monkeypatch):
    calls: list[list[str]] = []

    def fake_run(argv, **kwargs):
        calls.append(list(argv))
        return subprocess.CompletedProcess(argv, 0, "", "[provenance] recorded rows=0")

    monkeypatch.setattr(worker.subprocess, "run", fake_run)
    return calls


def _drive_main(monkeypatch, tmp_path):
    """main() 를 DB·SSH 없이 끝까지 돌린다. update_run 기록을 돌려준다."""
    updates: list[tuple[str, str]] = []
    target = _target(tmp_path)
    monkeypatch.setattr(worker, "row_for_run", lambda run_id: dict(ROW))
    monkeypatch.setattr(worker, "get_execution_target", lambda p, c: target)
    monkeypatch.setattr(worker, "acquire_lease", lambda *a: None)
    monkeypatch.setattr(worker, "release_lease", lambda *a: None)
    monkeypatch.setattr(worker, "heartbeat", lambda *a: None)
    monkeypatch.setattr(worker, "preflight", lambda *a: None)
    monkeypatch.setattr(worker, "deploy", lambda *a: None)
    monkeypatch.setattr(worker, "verify_health", lambda *a: None)
    monkeypatch.setattr(worker, "update_run", lambda run_id, status, phase, detail="": updates.append((status, phase)))
    monkeypatch.setattr(sys, "argv", ["worker", "--run-id", "7", "--lock-file", str(tmp_path / "lock")])
    return worker.main(), updates


def test_certified_run_calls_recorder_exactly_once(monkeypatch, tmp_path, recorder_calls):
    rc, updates = _drive_main(monkeypatch, tmp_path)

    assert rc == 0
    assert updates[-1] == ("success", "release_certified")
    assert len(recorder_calls) == 1
    argv = recorder_calls[0]
    assert argv[1].endswith("scripts/record-release-provenance.sh")
    assert argv[argv.index("--deploy-run-id") + 1] == "7"
    assert argv[argv.index("--project") + 1] == "FOOD"
    assert argv[argv.index("--component") + 1] == "store-assistant"
    assert argv[argv.index("--release-ref") + 1] == "abcdef1"
    assert argv[argv.index("--repo") + 1] == str(tmp_path)


def test_recorder_failure_keeps_deploy_success(monkeypatch, tmp_path):
    def failing_run(argv, **kwargs):
        return subprocess.CompletedProcess(argv, 5, "", "[provenance] FAIL: could not persist")

    monkeypatch.setattr(worker.subprocess, "run", failing_run)
    rc, updates = _drive_main(monkeypatch, tmp_path)
    assert rc == 0
    assert updates[-1] == ("success", "release_certified")
    assert not any(status == "failed" for status, _ in updates)


def test_recorder_exception_keeps_deploy_success(monkeypatch, tmp_path):
    def exploding_run(argv, **kwargs):
        raise subprocess.TimeoutExpired(argv, 90)

    monkeypatch.setattr(worker.subprocess, "run", exploding_run)
    rc, updates = _drive_main(monkeypatch, tmp_path)
    assert rc == 0
    assert updates[-1] == ("success", "release_certified")


def test_success_partial_does_not_call_recorder(tmp_path, recorder_calls):
    worker.record_release_provenance(7, dict(ROW), _target(tmp_path), "success_partial")
    assert recorder_calls == []


def test_failed_deploy_never_calls_hook(monkeypatch, tmp_path):
    hook_calls: list[tuple] = []
    updates: list[tuple[str, str]] = []
    target = _target(tmp_path)
    monkeypatch.setattr(worker, "row_for_run", lambda run_id: dict(ROW))
    monkeypatch.setattr(worker, "get_execution_target", lambda p, c: target)
    monkeypatch.setattr(worker, "acquire_lease", lambda *a: None)
    monkeypatch.setattr(worker, "release_lease", lambda *a: None)
    monkeypatch.setattr(worker, "heartbeat", lambda *a: None)
    monkeypatch.setattr(worker, "preflight", lambda *a: None)
    monkeypatch.setattr(worker, "deploy", lambda *a: (_ for _ in ()).throw(RuntimeError("boom")))
    monkeypatch.setattr(worker, "update_run", lambda run_id, status, phase, detail="": updates.append((status, phase)))
    monkeypatch.setattr(worker, "record_release_provenance", lambda *a: hook_calls.append(a))
    monkeypatch.setattr(sys, "argv", ["worker", "--run-id", "7", "--lock-file", str(tmp_path / "lock")])

    assert worker.main() == 1
    assert updates[-1] == ("failed", "deploying")
    assert hook_calls == []


def test_ssh_target_without_local_git_is_skipped(tmp_path, recorder_calls):
    remote = SimpleNamespace(executor="ssh", repo_path="/root/kis-autotrade-v4", key=("GO100", "backend"))
    worker.record_release_provenance(7, dict(ROW), remote, "success")
    assert recorder_calls == []


def test_worker_never_inserts_provenance_itself():
    source = (ROOT / "scripts/unified_component_deploy_worker.py").read_text()
    assert "INSERT INTO deploy_release_provenance" not in source
