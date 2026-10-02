"""정본 게이트 진입점 — 실제 핸들러·HTTP·hook 경로에 예외/타임아웃을 주입한다.

소스 문자열 검사가 아니라 submit_job / submit_batch / add_goal_document 본문과
FastAPI 라우터, canonical_gate_commit.py, pre-commit 게이트 블록을 실제로 실행한다.
증명 대상: ① 게이트가 터지거나 느려도 원 기능은 그대로 통과 ② mode=off 는 게이트 쿼리 0
③ 판정+기록 합산 지연이 300ms 상한 안.
"""
from __future__ import annotations

import asyncio
import os
import subprocess
import sys
import time
import uuid
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Optional

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.services import canonical_gate as cg

_REPO = Path(__file__).resolve().parents[2]
_COMMIT_SCRIPT = _REPO / "scripts" / "canonical_gate_commit.py"
_SESSION = "11111111-2222-4333-8444-555555555555"
_GOAL = "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee"
_CTX = {"tenant": {"id": "tenant-1"}, "user": {"user_id": "u1"}}


class HandlerConn:
    """핸들러 본문이 쓰는 최소 커넥션. 게이트 SQL 만 따로 세고 주입한다."""

    def __init__(self, pool: "HandlerPool"):
        self.pool = pool

    def transaction(self):
        class _Tx:
            async def __aenter__(s):
                return None

            async def __aexit__(s, *exc):
                return False

        return _Tx()

    async def fetchval(self, sql: str, *args: Any):
        return self.pool.handler_fetchval

    async def fetchrow(self, sql: str, *args: Any):
        if "project_document_heads" in sql:
            self.pool.gate_queries.append("judge")
            if self.pool.judge_exc:
                raise self.pool.judge_exc
            if self.pool.judge_delay:
                await asyncio.sleep(self.pool.judge_delay)
            return self.pool.judge_row
        return self.pool.handler_fetchrow

    async def execute(self, sql: str, *args: Any):
        if "canonical_gate_events" in sql:
            self.pool.gate_queries.append("record")
            if self.pool.record_exc:
                raise self.pool.record_exc
            if self.pool.record_delay:
                await asyncio.sleep(self.pool.record_delay)
            self.pool.events.append(args)
        return None


class HandlerPool:
    def __init__(self, *, judge_row=None, judge_exc=None, judge_delay=0.0,
                 record_exc=None, record_delay=0.0):
        self.judge_row = judge_row
        self.judge_exc = judge_exc
        self.judge_delay = judge_delay
        self.record_exc = record_exc
        self.record_delay = record_delay
        self.handler_fetchval = "tenant-1"
        self.handler_fetchrow: Optional[dict] = None
        self.gate_queries: list[str] = []
        self.events: list[tuple] = []
        self.acquires = 0

    def acquire(self):
        pool = self

        class _A:
            async def __aenter__(s):
                pool.acquires += 1
                return HandlerConn(pool)

            async def __aexit__(s, *exc):
                return False

        return _A()


_RUNNER_MISSING = {"goal_approved": 0, "project_approved": 0}


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    monkeypatch.delenv("CANONICAL_GATE_MODE", raising=False)
    monkeypatch.delenv("CANONICAL_GATE_TIMEOUT_MS", raising=False)


@pytest.fixture
def runner_env(monkeypatch):
    """submit_job/submit_batch 본문은 그대로 두고, 게이트와 무관한 주변 DB 헬퍼만 대체한다."""
    import app.api.pipeline_runner as pr
    import app.core.db_pool as db_pool
    import app.services.pipeline_runner_service as prs

    env = SimpleNamespace(
        pool=HandlerPool(judge_row=_RUNNER_MISSING), link=prs.GoalLink(_GOAL, None), pr=pr,
    )

    async def noop(*_a, **_k):
        return None

    async def not_locked(*_a, **_k):
        return False

    async def model(*_a, **_k):
        return "claude-sonnet"

    async def no_orphans(*_a, **_k):
        return []

    async def order(*_a, **_k):
        return (0, 0, 0)

    async def link(job_id, project, **_k):
        if isinstance(env.link, Exception):
            raise env.link
        return env.link

    for name in ("_lock_instruction_hash", "_cleanup_dead_local_runner_processes",
                 "_find_active_duplicate", "_find_active_file_conflict",
                 "_persist_job_goal_context", "_enforce_owner_resolved_gate"):
        monkeypatch.setattr(pr, name, noop)
    monkeypatch.setattr(pr, "check_project_lock", not_locked)
    monkeypatch.setattr(pr, "_get_model_for_size", model)
    monkeypatch.setattr(pr, "_relink_superseded_orphans", no_orphans)
    monkeypatch.setattr(pr, "_resolve_milestone_order", order)
    monkeypatch.setattr(prs, "_link_job_to_goal_explicit", link)
    monkeypatch.setattr(db_pool, "get_pool", lambda: env.pool)
    return env


def _job_req(pr):
    return pr.JobSubmitRequest(project="AADS", instruction="간단한 지시", session_id=_SESSION)


def _batch_req(pr, n=3):
    return pr.BatchSubmitRequest(
        project="AADS", session_id=_SESSION,
        jobs=[pr.BatchJobItem(key=f"K{i}", instruction=f"지시 {i}") for i in range(n)],
    )


def _submit(env):
    return asyncio.run(env.pr.submit_job(_job_req(env.pr), context=_CTX))


def _batch(env, n=3):
    return asyncio.run(env.pr.submit_batch(_batch_req(env.pr, n), context=_CTX))


# ── submit_job: 실제 본문 실행 ────────────────────────────────────────


def test_submit_job_missing_canonical_adds_warning_without_changing_result(runner_env):
    resp = _submit(runner_env)
    assert resp.status == "queued" and resp.job_id.startswith("runner-")
    assert resp.canonical_gate and resp.canonical_gate["verdict"] == "missing_canonical"
    assert runner_env.pool.events and runner_env.pool.events[0][0] == "runner_submit"


@pytest.mark.parametrize("fault", ["judge_exc", "judge_timeout", "record_exc", "both_hang"])
def test_submit_job_survives_injected_gate_faults(runner_env, monkeypatch, fault):
    monkeypatch.setenv("CANONICAL_GATE_TIMEOUT_MS", "60")
    if fault == "judge_exc":
        runner_env.pool = HandlerPool(judge_exc=RuntimeError("db down"))
    elif fault == "judge_timeout":
        runner_env.pool = HandlerPool(judge_row=_RUNNER_MISSING, judge_delay=3.0)
    elif fault == "record_exc":
        runner_env.pool = HandlerPool(judge_row=_RUNNER_MISSING,
                                      record_exc=RuntimeError('relation "canonical_gate_events" does not exist'))
    else:
        runner_env.pool = HandlerPool(judge_row=_RUNNER_MISSING, judge_delay=3.0, record_delay=3.0)
    started = time.monotonic()
    resp = _submit(runner_env)
    assert time.monotonic() - started < 1.0
    assert resp.status == "queued" and resp.job_id.startswith("runner-")
    assert "에 연결되었습니다" in resp.message
    if fault == "record_exc":
        assert resp.canonical_gate  # 기록 실패는 경고를 막지 않는다
    else:
        assert resp.canonical_gate is None


def test_submit_job_survives_gate_pool_unavailable(runner_env, monkeypatch):
    import app.core.db_pool as db_pool

    calls = {"n": 0}

    def get_pool():
        calls["n"] += 1
        if calls["n"] > 1:  # 핸들러의 첫 호출은 성공, 게이트의 호출은 실패
            raise RuntimeError("pool closed")
        return runner_env.pool

    monkeypatch.setattr(db_pool, "get_pool", get_pool)
    resp = _submit(runner_env)
    assert resp.status == "queued" and resp.canonical_gate is None and calls["n"] >= 2


def test_submit_job_mode_off_issues_zero_gate_queries_and_same_result(runner_env, monkeypatch):
    shadow = _submit(runner_env)
    runner_env.pool = HandlerPool(judge_row=_RUNNER_MISSING)
    monkeypatch.setenv("CANONICAL_GATE_MODE", "off")
    off = _submit(runner_env)
    assert runner_env.pool.gate_queries == []
    assert off.canonical_gate is None
    assert (off.status, off.message) == (shadow.status, shadow.message)


def test_submit_job_without_goal_link_never_touches_gate(runner_env):
    runner_env.link = None
    resp = _submit(runner_env)
    assert resp.status == "queued" and resp.canonical_gate is None
    assert runner_env.pool.gate_queries == []


def test_submit_job_goal_link_failure_does_not_run_gate(runner_env):
    runner_env.link = RuntimeError("goal link boom")
    resp = _submit(runner_env)
    assert resp.status == "queued" and resp.canonical_gate is None
    assert runner_env.pool.gate_queries == []


# ── submit_batch: 실제 본문 실행 ──────────────────────────────────────


def test_submit_batch_attaches_warning_per_linked_job(runner_env):
    out = _batch(runner_env)
    assert set(out) == {"parallel_group", "jobs", "message"}
    assert len(out["jobs"]) == 3
    assert all(j["canonical_gate"]["verdict"] == "missing_canonical" for j in out["jobs"])


@pytest.mark.parametrize("fault", ["judge_exc", "record_exc"])
def test_submit_batch_survives_gate_faults_with_identical_shape(runner_env, fault):
    baseline = _batch(runner_env)
    runner_env.pool = (
        HandlerPool(judge_exc=RuntimeError("db down")) if fault == "judge_exc"
        else HandlerPool(judge_row=None, record_exc=RuntimeError("no table"))
    )
    out = _batch(runner_env)
    assert set(out) == set(baseline) and len(out["jobs"]) == 3
    assert all("canonical_gate" not in j for j in out["jobs"])
    assert set(out["jobs"][0]) == set(baseline["jobs"][0]) - {"canonical_gate"}


def test_submit_batch_hung_gate_adds_at_most_one_budget_not_one_per_job(runner_env, monkeypatch):
    monkeypatch.setenv("CANONICAL_GATE_TIMEOUT_MS", "300")
    runner_env.pool = HandlerPool(judge_row=_RUNNER_MISSING, judge_delay=5.0, record_delay=5.0)
    started = time.monotonic()
    out = _batch(runner_env, n=10)
    elapsed = time.monotonic() - started
    assert len(out["jobs"]) == 10
    assert elapsed < 0.9, f"10 잡 배치가 게이트로 {elapsed:.2f}s 지연 — 잡마다 순차 대기 중"


def test_submit_batch_mode_off_issues_zero_gate_queries(runner_env, monkeypatch):
    monkeypatch.setenv("CANONICAL_GATE_MODE", "off")
    out = _batch(runner_env)
    assert len(out["jobs"]) == 3 and runner_env.pool.gate_queries == []
    assert all("canonical_gate" not in j for j in out["jobs"])


# ── 300ms 합산 상한 ───────────────────────────────────────────────────


def test_gate_total_latency_is_capped_by_one_budget_even_if_judge_and_record_hang():
    pool = HandlerPool(judge_row=_RUNNER_MISSING, judge_delay=5.0, record_delay=5.0)
    started = time.monotonic()
    res = asyncio.run(cg.check_runner_submit(
        goal_id=_GOAL, tenant_id="t", project="AADS", job_id="runner-x", pool=pool))
    elapsed = time.monotonic() - started
    assert res is None
    assert elapsed < 0.45, f"판정+기록 합산 {elapsed:.3f}s — 300ms 상한 초과"


# ── 실제 FastAPI 요청 경로 ────────────────────────────────────────────


def _runner_client(env):
    app = FastAPI()
    app.include_router(env.pr.router, prefix="/api/v1")
    app.dependency_overrides[env.pr.require_tenant_member] = lambda: _CTX
    return TestClient(app, raise_server_exceptions=True)


def _post_job(client):
    return client.post("/api/v1/pipeline/jobs", json={
        "project": "AADS", "instruction": "간단한 지시", "session_id": _SESSION,
    })


def test_http_submit_job_response_shape_is_unchanged_without_warning(runner_env):
    runner_env.pool = HandlerPool(judge_row={"goal_approved": 1, "project_approved": 1})
    r = _post_job(_runner_client(runner_env))
    assert r.status_code == 200
    assert set(r.json()) == {"job_id", "status", "message"}  # null canonical_gate 키도 없다


def test_http_submit_job_adds_only_the_optional_field_when_warned(runner_env):
    r = _post_job(_runner_client(runner_env))
    assert r.status_code == 200
    body = r.json()
    assert {"job_id", "status", "message"} <= set(body)
    assert body["canonical_gate"]["verdict"] == "missing_canonical"


def test_http_submit_job_returns_200_when_gate_raises_and_without_goal_link(runner_env, monkeypatch):
    runner_env.pool = HandlerPool(judge_exc=RuntimeError("db down"))
    r = _post_job(_runner_client(runner_env))
    assert r.status_code == 200 and set(r.json()) == {"job_id", "status", "message"}
    runner_env.link = None
    assert _post_job(_runner_client(runner_env)).status_code == 200


def test_http_submit_job_mode_off_issues_zero_gate_queries(runner_env, monkeypatch):
    monkeypatch.setenv("CANONICAL_GATE_MODE", "off")
    r = _post_job(_runner_client(runner_env))
    assert r.status_code == 200 and runner_env.pool.gate_queries == []


def test_http_openapi_keeps_job_route_and_marks_gate_field_optional(runner_env):
    spec = _runner_client(runner_env).get("/openapi.json").json()
    op = spec["paths"]["/api/v1/pipeline/jobs"]["post"]
    assert op["tags"] == ["pipeline-runner"]
    ref = op["responses"]["200"]["content"]["application/json"]["schema"]["$ref"]
    schema = spec["components"]["schemas"][ref.rsplit("/", 1)[1]]
    assert {"job_id", "status", "message"} <= set(schema["properties"])
    assert set(schema.get("required", [])) == {"job_id", "status", "message"}


def _goal_client(monkeypatch, pool):
    from app.routers import goals as goals_mod
    import app.core.db_pool as db_pool

    monkeypatch.setattr(db_pool, "get_pool", lambda: pool)
    pool.handler_fetchrow = {
        "id": 7, "kind": "plan", "doc_path": "docs/plans/x.md", "title": "t", "note": None,
        "document_key": "plan:abc", "version": "1.0.0", "status": "active", "is_latest": True,
        "change_summary": None, "supersedes_id": None, "updated_at": "now",
    }
    app = FastAPI()
    app.include_router(goals_mod.router, prefix="/api/v1")
    app.dependency_overrides[goals_mod.require_tenant_member] = lambda: _CTX
    return TestClient(app, raise_server_exceptions=True)


def _post_doc(client, kind="plan"):
    return client.post(f"/api/v1/goals/{_GOAL}/documents", json={"kind": kind, "doc_path": "docs/plans/x.md"})


@pytest.mark.parametrize("fault", ["judge_exc", "judge_timeout", "record_exc"])
def test_http_goal_document_survives_injected_gate_faults(monkeypatch, fault):
    monkeypatch.setenv("CANONICAL_GATE_TIMEOUT_MS", "60")
    row = {"project": "AADS", "key_match": False, "path_match": False}
    pool = {
        "judge_exc": HandlerPool(judge_exc=RuntimeError("db down")),
        "judge_timeout": HandlerPool(judge_row=row, judge_delay=3.0),
        "record_exc": HandlerPool(judge_row=row, record_exc=RuntimeError("no table")),
    }[fault]
    r = _post_doc(_goal_client(monkeypatch, pool))
    assert r.status_code == 200
    body = r.json()
    assert body["id"] == 7 and body["doc_path"] == "docs/plans/x.md" and body["version"] == "1.0.0"
    assert ("canonical_gate" in body) == (fault == "record_exc")


def test_http_goal_document_warns_with_existing_fields_intact(monkeypatch):
    pool = HandlerPool(judge_row={"project": "AADS", "key_match": False, "path_match": False})
    body = _post_doc(_goal_client(monkeypatch, pool)).json()
    assert body["canonical_gate"]["verdict"] == "missing_canonical" and body["id"] == 7


def test_http_goal_document_mode_off_zero_queries_and_prototype_is_exempt(monkeypatch):
    pool = HandlerPool(judge_row={"project": "AADS", "key_match": False, "path_match": False})
    client = _goal_client(monkeypatch, pool)
    # 프로토타입은 head CHECK 에 없는 kind 라 정본이 될 수 없다 → 경고 없음(기록은 ok)
    body = _post_doc(client, kind="prototype").json()
    assert "canonical_gate" not in body and pool.events and pool.events[-1][3] == "ok"
    monkeypatch.setenv("CANONICAL_GATE_MODE", "off")
    pool.gate_queries.clear()
    assert _post_doc(client).status_code == 200 and pool.gate_queries == []


# ── commit hook: 실제 스크립트·실제 hook 블록 ─────────────────────────


def _staged_repo(tmp_path):
    def git(*a):
        subprocess.run(["git", *a], cwd=tmp_path, check=True, capture_output=True)

    git("init", "-q")
    (tmp_path / "docs/plans").mkdir(parents=True)
    (tmp_path / "docs/plans/A.md").write_text("x", encoding="utf-8")
    git("add", "-A")
    return tmp_path


def _run_script(cwd, env_extra=None, timeout=30):
    env = {**os.environ, **(env_extra or {})}
    env.pop("CANONICAL_GATE_MODE", None)
    env.update({k: v for k, v in (env_extra or {}).items()})
    return subprocess.run([sys.executable, str(_COMMIT_SCRIPT)], cwd=cwd, env=env,
                          capture_output=True, text=True, timeout=timeout)


def test_commit_script_exits_zero_when_git_is_missing(tmp_path):
    repo = _staged_repo(tmp_path)
    res = _run_script(repo, {"PATH": "/nonexistent"})
    assert res.returncode == 0 and "건너뜀" in res.stderr


def test_commit_script_exits_zero_when_git_hangs(tmp_path):
    repo = _staged_repo(tmp_path)
    fake = tmp_path / "fakebin"
    fake.mkdir()
    (fake / "git").write_text("#!/bin/sh\nsleep 60\n", encoding="utf-8")
    (fake / "git").chmod(0o755)
    started = time.monotonic()
    res = _run_script(repo, {"PATH": f"{fake}:{os.environ['PATH']}"})
    assert res.returncode == 0 and time.monotonic() - started < 15  # 스크립트 자체 5s 타임아웃


def _hook_block() -> str:
    hook = (_REPO / "scripts" / "hooks" / "pre-commit").read_text(encoding="utf-8")
    start = hook.index("# ── 정본 게이트 (그림자 모드")
    return hook[start:hook.index("# ── 감사: hook 통과 서명")]


def _run_block(repo, fake_python_body: Optional[str], mode: Optional[str] = None, block: Optional[str] = None):
    """실제 hook 블록을 bash 로 실행. fake_python_body 로 python3 가 죽거나 멈추는 상황을 만든다."""
    env = {**os.environ}
    env.pop("CANONICAL_GATE_MODE", None)
    if mode:
        env["CANONICAL_GATE_MODE"] = mode
    if fake_python_body is not None:
        fake = repo / "fakepy"
        fake.mkdir(exist_ok=True)
        (fake / "python3").write_text("#!/bin/sh\n" + fake_python_body + "\n", encoding="utf-8")
        (fake / "python3").chmod(0o755)
        env["PATH"] = f"{fake}:{env['PATH']}"
    (repo / "scripts").mkdir(exist_ok=True)
    (repo / "scripts" / "canonical_gate_commit.py").write_text(_COMMIT_SCRIPT.read_text(encoding="utf-8"), encoding="utf-8")
    script = (block or _hook_block()) + '\necho "AFTER_BLOCK rc=0"\n'
    return subprocess.run(["bash", "-c", script], cwd=repo, env=env, capture_output=True, text=True, timeout=60)


@pytest.mark.parametrize("body", ["exit 7", "kill -9 $$", "echo boom >&2; exit 1"])
def test_pre_commit_block_passes_when_script_crashes(tmp_path, body):
    res = _run_block(_staged_repo(tmp_path), body)
    assert res.returncode == 0 and "AFTER_BLOCK" in res.stdout


def test_pre_commit_block_passes_when_script_hangs_past_timeout(tmp_path):
    block = _hook_block().replace("timeout 10 ", "timeout 1 ")
    assert block != _hook_block()
    started = time.monotonic()
    res = _run_block(_staged_repo(tmp_path), "sleep 30", block=block)
    assert res.returncode == 0 and "AFTER_BLOCK" in res.stdout and time.monotonic() - started < 15


def test_pre_commit_block_real_script_warns_logs_and_passes(tmp_path):
    repo = _staged_repo(tmp_path)
    res = _run_block(repo, None)
    assert res.returncode == 0 and "AFTER_BLOCK" in res.stdout
    assert "docs/plans/A.md" in res.stderr
    assert (repo / ".git" / "canonical_gate.log").read_text(encoding="utf-8").count("CGv1") == 1


def test_pre_commit_block_mode_off_does_nothing(tmp_path):
    repo = _staged_repo(tmp_path)
    res = _run_block(repo, None, mode="off")
    assert res.returncode == 0 and res.stderr == "" and not (repo / ".git" / "canonical_gate.log").exists()


def test_pre_commit_block_skips_when_script_is_absent(tmp_path):
    repo = _staged_repo(tmp_path)
    block = _hook_block()
    env = {**os.environ}
    res = subprocess.run(["bash", "-c", block + '\necho AFTER_BLOCK\n'], cwd=repo, env=env,
                         capture_output=True, text=True, timeout=30)
    assert res.returncode == 0 and "AFTER_BLOCK" in res.stdout
    assert uuid.UUID(_SESSION)  # 상수 형식 확인(요청 검증 통과 전제)
