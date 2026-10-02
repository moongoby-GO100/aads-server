"""작업잠금 상한 단일 출처 + 종료 작업 잠금 회수 (AADS-RUNNER-CAPACITY-SSOT-ORPHAN-RECLAIM).

러너 MAX_CONCURRENT_PER_PROJECT 를 `max` 로 넘기면 API 가 그것을 상한으로 쓰고(최대 50),
상한에 걸렸을 때 이미 종료된 작업이 쥔 잠금은 회수해 1회 재시도한다.
"""
import asyncio
import time
from unittest.mock import patch

import pytest

from app.api import ops
from app.services import deploy_lock


class FakeRedis:
    def __init__(self, hashes=None):
        self.hashes = {k: dict(v) for k, v in (hashes or {}).items()}

    def hgetall(self, key):
        return dict(self.hashes.get(key, {}))

    def hset(self, key, field, value):
        self.hashes.setdefault(key, {})[field] = value

    def hdel(self, key, field):
        return 1 if self.hashes.get(key, {}).pop(field, None) is not None else 0

    def expire(self, key, seconds):
        return True


def _holders(n, prefix="job"):
    now = str(time.time())
    return {f"{prefix}-{i}": now for i in range(n)}


def _with(fake):
    return patch.object(deploy_lock, "_get_redis", return_value=fake)


# ── A. max 인자 ───────────────────────────────────────────────

def test_default_limit_is_env_default_when_max_not_passed(monkeypatch):
    monkeypatch.delenv("MAX_CONCURRENT_PER_PROJECT", raising=False)
    monkeypatch.delenv("MAX_CONCURRENT_PER_PROJECT_GO100", raising=False)
    fake = FakeRedis({"work_lock:GO100": _holders(6)})
    with _with(fake):
        res = deploy_lock.acquire_work_lock("GO100", "new")
    assert res["acquired"] is False


def test_max_param_raises_limit_above_env_default(monkeypatch):
    monkeypatch.delenv("MAX_CONCURRENT_PER_PROJECT", raising=False)
    monkeypatch.delenv("MAX_CONCURRENT_PER_PROJECT_GO100", raising=False)
    fake = FakeRedis({"work_lock:GO100": _holders(6)})
    with _with(fake):
        res = deploy_lock.acquire_work_lock("GO100", "new", max_concurrent=20)
    assert res["acquired"] is True
    assert "new" in fake.hashes["work_lock:GO100"]


def test_max_param_is_clamped_to_50():
    assert deploy_lock._resolve_work_lock_limit("GO100", 500) == 50
    fake = FakeRedis({"work_lock:GO100": _holders(50)})
    with _with(fake):
        res = deploy_lock.acquire_work_lock("GO100", "new", max_concurrent=500)
    assert res["acquired"] is False


@pytest.mark.parametrize("bad", [0, -3, None, "abc"])
def test_max_param_below_one_is_ignored(monkeypatch, bad):
    monkeypatch.setenv("MAX_CONCURRENT_PER_PROJECT", "6")
    monkeypatch.delenv("MAX_CONCURRENT_PER_PROJECT_GO100", raising=False)
    assert deploy_lock._resolve_work_lock_limit("GO100", bad) == 6


# ── B. 종료 작업 잠금 회수 ─────────────────────────────────────

def _acquire(fake, statuses=None, fetch_error=None, max_concurrent=3):
    async def _fake_fetch(ids):
        if fetch_error:
            raise fetch_error
        return {i: statuses[i] for i in ids if i in (statuses or {})}

    with _with(fake), patch.object(ops, "_fetch_job_statuses", _fake_fetch):
        return asyncio.run(
            ops.api_acquire_work_lock("GO100", "new", "", max_concurrent)
        )


def test_terminal_holder_is_reclaimed_and_acquire_retried(caplog):
    fake = FakeRedis({"work_lock:GO100": {"a": "9999999999", "b": "9999999999", "c": "9999999999"}})
    caplog.set_level("INFO", logger=deploy_lock.logger.name)
    res = _acquire(fake, {"a": "running", "b": "error", "c": "running"})
    assert res["acquired"] is True
    held = fake.hashes["work_lock:GO100"]
    assert "b" not in held and "new" in held and {"a", "c"} <= set(held)
    assert "work_lock_reclaimed holder=b status=error" in caplog.text


def test_active_holders_are_not_reclaimed():
    fake = FakeRedis({"work_lock:GO100": {"a": "9999999999", "b": "9999999999", "c": "9999999999"}})
    res = _acquire(fake, {"a": "running", "b": "queued", "c": "awaiting_approval"})
    assert res["acquired"] is False
    assert set(fake.hashes["work_lock:GO100"]) == {"a", "b", "c"}


def test_unknown_holder_without_job_row_is_kept():
    fake = FakeRedis({"work_lock:GO100": {"chat-x": "9999999999", "b": "9999999999", "c": "9999999999"}})
    res = _acquire(fake, {"b": "running", "c": "running"})
    assert res["acquired"] is False
    assert "chat-x" in fake.hashes["work_lock:GO100"]


def test_db_failure_does_not_reclaim():
    fake = FakeRedis({"work_lock:GO100": {"a": "9999999999", "b": "9999999999", "c": "9999999999"}})
    res = _acquire(fake, fetch_error=RuntimeError("db down"))
    assert res["acquired"] is False
    assert set(fake.hashes["work_lock:GO100"]) == {"a", "b", "c"}


def test_terminal_set_is_the_shared_one():
    from app.services.pipeline_cleanup import _TERMINAL_STATUSES

    assert deploy_lock.TERMINAL_JOB_STATUSES is _TERMINAL_STATUSES
    assert {"done", "error", "cancelled", "rejected_done"} <= set(deploy_lock.TERMINAL_JOB_STATUSES)


# ── 프로젝트 전용 천장 / 제출 측 dedup ──────────────────────────

def test_project_specific_env_and_nas_cap_the_caller_max(monkeypatch):
    monkeypatch.setenv("MAX_CONCURRENT_PER_PROJECT_GO100", "4")
    assert deploy_lock._resolve_work_lock_limit("GO100", 20) == 4
    monkeypatch.delenv("MAX_CONCURRENT_PER_PROJECT_NAS", raising=False)
    assert deploy_lock._resolve_work_lock_limit("NAS", 20) == 1
    monkeypatch.delenv("MAX_CONCURRENT_PER_PROJECT_KIS", raising=False)
    assert deploy_lock._resolve_work_lock_limit("KIS", 20) == 20


def test_submit_side_dedup_never_matches_cancelled_or_dedup_blocked():
    from app.api import pipeline_runner

    active = set(pipeline_runner._ACTIVE_PIPELINE_STATUSES)
    assert not ({"cancelled", "error", "done", "dedup_blocked"} & active)
