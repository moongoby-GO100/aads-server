"""P0: AI 검수 verdict 유실 차단 회귀 테스트.

배경: 동기 HTTP 1회 왕복으로만 검수 결과를 전달하면, 호출측이 먼저 타임아웃해도
서버는 계속 검수를 진행해 verdict를 계산하지만 그 결과를 아무도 회수하지 못하고
review_hold(REVIEW_API_UNAVAILABLE)로 유실됐다. 이 테스트는 다음을 검증한다.
  1) 요청을 먼저 DB에 영속화한 뒤(202) 백그라운드 태스크로 실행하며, 그 태스크는
     원 HTTP 요청/응답 수명주기와 무관하게 끝까지 실행되어 verdict를 DB에 남긴다.
  2) 폴링 엔드포인트가 그 DB 상태를 그대로 반환한다.
  3) 같은 job_id에 동일 payload로 이미 완료/진행 중인 요청이 있으면 재호출 없이
     그 요청을 재사용한다(중복 LLM 호출 방지).

app.api.code_review 는 app.core.db_pool / app.services.code_reviewer 를 함수
내부에서 지연 import하므로, 실제 "app" 패키지를 건드리지 않고 별도 모듈 이름으로
파일을 직접 로드해 sys.modules 오염(다른 테스트 파일에 영향)을 피한다.
"""
import asyncio
import importlib.util
import sys
import types
from pathlib import Path
from unittest.mock import AsyncMock

ROOT = Path(__file__).resolve().parents[2]


def _load_code_review(monkeypatch, store: dict):
    """app/api/code_review.py 를 격리된 모듈로 로드하고 DB pool을 가짜로 주입."""
    fake_db_pool_mod = types.ModuleType("app.core.db_pool")
    fake_db_pool_mod.get_pool = lambda: _FakePool(store)
    monkeypatch.setitem(sys.modules, "app.core.db_pool", fake_db_pool_mod)

    module_name = "code_review_under_test_async_persistence"
    spec = importlib.util.spec_from_file_location(
        module_name, ROOT / "app" / "api" / "code_review.py"
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, module_name, module)
    spec.loader.exec_module(module)
    return module


class _FakeConn:
    def __init__(self, store: dict):
        self.store = store

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def fetchrow(self, query: str, *args):
        if "SELECT request_id, status FROM code_review_requests" in query:
            job_id, payload_hash = args
            candidates = [
                dict(row, request_id=rid)
                for rid, row in self.store.items()
                if row["job_id"] == job_id
                and row["payload_sha256"] == payload_hash
                and row["status"] in ("queued", "running", "completed")
            ]
            return candidates[0] if candidates else None

        if "SELECT status, payload_sha256 FROM code_review_requests" in query:
            (request_id,) = args
            row = self.store.get(request_id)
            if not row:
                return None
            return {"status": row["status"], "payload_sha256": row["payload_sha256"]}

        if "SET status='running'" in query and "RETURNING" in query:
            (request_id,) = args
            row = self.store.get(request_id)
            if not row or row["status"] != "queued":
                return None
            row["status"] = "running"
            row["attempts"] = row.get("attempts", 0) + 1
            return {
                "job_id": row["job_id"],
                "project": row["project"],
                "diff": row["diff"],
                "instruction": row["instruction"],
                "files_changed": row["files_changed"],
            }

        if "SELECT request_id, job_id, project, status, verdict, score" in query:
            (request_id,) = args
            row = self.store.get(request_id)
            if not row:
                return None
            return dict(row, request_id=request_id)

        raise AssertionError(f"unexpected fetchrow query: {query[:80]}")

    async def fetchval(self, query: str, *args):
        if "INSERT INTO code_review_requests" in query:
            (request_id, job_id, project, diff, instruction, files_changed, payload_hash) = args
            if request_id in self.store:
                return None
            self.store[request_id] = {
                "job_id": job_id,
                "project": project,
                "diff": diff,
                "instruction": instruction,
                "files_changed": files_changed,
                "payload_sha256": payload_hash,
                "status": "queued",
                "verdict": None,
                "score": None,
                "feedback": None,
                "issues": None,
                "flag_category": None,
                "failure_stage": None,
                "needs_retry": False,
                "model_used": None,
                "error_detail": None,
                "attempts": 0,
                "created_at": None,
                "started_at": None,
                "completed_at": None,
                "updated_at": None,
            }
            return request_id
        raise AssertionError(f"unexpected fetchval query: {query[:80]}")

    async def execute(self, query: str, *args):
        if "SET status='queued', error_detail='stale" in query:
            return
        if "SET status='completed'" in query:
            (request_id, verdict, score, feedback, issues, flag_category,
             failure_stage, needs_retry, model_used) = args
            row = self.store.get(request_id)
            if row and row["status"] == "running":
                row.update(
                    status="completed",
                    verdict=verdict,
                    score=score,
                    feedback=feedback,
                    issues=issues,
                    flag_category=flag_category,
                    failure_stage=failure_stage,
                    needs_retry=needs_retry,
                    model_used=model_used,
                )
            return
        if "SET status='failed'" in query:
            (request_id, error_detail) = args
            row = self.store.get(request_id)
            if row and row["status"] == "running":
                row.update(status="failed", error_detail=error_detail, needs_retry=True)
            return
        raise AssertionError(f"unexpected execute query: {query[:80]}")


class _FakePool:
    def __init__(self, store: dict):
        self.store = store

    def acquire(self):
        return _FakeConn(self.store)


def test_background_review_persists_verdict_after_caller_stops_waiting(monkeypatch):
    """POST 응답 이후(호출측이 더 기다리지 않아도) 백그라운드 태스크가 verdict를 DB에 남긴다."""
    asyncio.run(_background_review_persists_verdict_after_caller_stops_waiting(monkeypatch))


async def _background_review_persists_verdict_after_caller_stops_waiting(monkeypatch):
    store: dict = {}
    code_review = _load_code_review(monkeypatch, store)

    from dataclasses import dataclass

    @dataclass
    class _FakeVerdict:
        verdict: str = "APPROVE"
        score: float = 0.91
        feedback: dict = None
        issues: list = None
        flag_category: str = None
        failure_stage: str = None
        needs_retry: bool = False
        model_used: str = "qwen-turbo"

        def __post_init__(self):
            self.feedback = self.feedback or {"summary": "ok"}
            self.issues = self.issues or []

    slow_review = AsyncMock(return_value=_FakeVerdict())
    fake_reviewer_mod = types.ModuleType("app.services.code_reviewer")
    fake_reviewer_mod.review_code_diff = slow_review
    monkeypatch.setitem(sys.modules, "app.services.code_reviewer", fake_reviewer_mod)

    req = code_review.AsyncCodeReviewRequest(
        job_id="runner-disconnect-test",
        project="AADS",
        diff="diff --git a/a.py b/a.py\n--- a/a.py\n+++ b/a.py\n@@ -1 +1,2 @@\n a\n+b\n",
        instruction="test",
        files_changed=["a.py"],
    )

    accepted = await code_review.create_code_review_request(req)
    assert accepted.status == "queued"
    assert store[accepted.request_id]["status"] == "queued"

    # "호출측 curl이 먼저 타임아웃"을 흉내낸다: 원 HTTP 핸들러 쪽에서는 더 이상
    # accepted 응답을 기다리지 않지만, 서버가 스케줄한 백그라운드 태스크는
    # asyncio.create_task로 독립 실행 중이므로 계속 진행된다.
    assert len(code_review._review_tasks) == 1
    bg_task = next(iter(code_review._review_tasks))
    await bg_task

    slow_review.assert_awaited_once()
    persisted = store[accepted.request_id]
    assert persisted["status"] == "completed"
    assert persisted["verdict"] == "APPROVE"
    assert persisted["score"] == 0.91
    assert persisted["model_used"] == "qwen-turbo"

    polled = await code_review.get_code_review_request(accepted.request_id)
    assert polled["status"] == "completed"
    assert polled["verdict"] == "APPROVE"


def test_create_code_review_request_reuses_completed_verdict_for_same_job_and_payload(monkeypatch):
    """같은 job_id + 동일 payload 재요청은 새 LLM 호출 없이 기존 완료 요청을 재사용한다."""
    asyncio.run(
        _create_code_review_request_reuses_completed_verdict_for_same_job_and_payload(monkeypatch)
    )


async def _create_code_review_request_reuses_completed_verdict_for_same_job_and_payload(monkeypatch):
    store: dict = {}
    code_review = _load_code_review(monkeypatch, store)

    import uuid

    existing_id = uuid.uuid4()
    payload_hash = code_review._payload_sha256(
        code_review.AsyncCodeReviewRequest(
            job_id="runner-dedupe-test",
            project="AADS",
            diff="diff --git a/x.py b/x.py\n--- a/x.py\n+++ b/x.py\n@@ -1 +1,2 @@\n x\n+y\n",
            instruction="dedupe test",
            files_changed=["x.py"],
        )
    )
    store[existing_id] = {
        "job_id": "runner-dedupe-test",
        "project": "AADS",
        "diff": "diff --git a/x.py b/x.py\n--- a/x.py\n+++ b/x.py\n@@ -1 +1,2 @@\n x\n+y\n",
        "instruction": "dedupe test",
        "files_changed": ["x.py"],
        "payload_sha256": payload_hash,
        "status": "completed",
        "verdict": "APPROVE",
        "score": 0.88,
        "feedback": {}, "issues": [], "flag_category": None, "failure_stage": None,
        "needs_retry": False, "model_used": "qwen-turbo", "error_detail": None,
        "attempts": 1, "created_at": None, "started_at": None,
        "completed_at": None, "updated_at": None,
    }

    review_scheduled = AsyncMock()
    monkeypatch.setattr(code_review, "_schedule_review_request", lambda request_id: review_scheduled(request_id))

    req = code_review.AsyncCodeReviewRequest(
        job_id="runner-dedupe-test",
        project="AADS",
        diff="diff --git a/x.py b/x.py\n--- a/x.py\n+++ b/x.py\n@@ -1 +1,2 @@\n x\n+y\n",
        instruction="dedupe test",
        files_changed=["x.py"],
        request_id=None,  # 새 클라이언트/재시도 — 기존 request_id를 모른다.
    )

    accepted = await code_review.create_code_review_request(req)

    assert accepted.request_id == existing_id
    assert accepted.status == "completed"
    review_scheduled.assert_not_called()
    assert len(store) == 1  # 새 행이 추가되지 않았다
