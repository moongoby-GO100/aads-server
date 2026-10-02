"""정본 강제 게이트 그림자 모드 — 판정 규칙, fail-open, mode=off 무동작, 응답 불변.

핵심 약속: 게이트가 무슨 짓을 하든 원래 요청/커밋/제출은 성공한다.
"""
from __future__ import annotations

import asyncio
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from app.services import canonical_gate as cg

_REPO = Path(__file__).resolve().parents[2]
_COMMIT_SCRIPT = _REPO / "scripts" / "canonical_gate_commit.py"


class FakeConn:
    def __init__(self, pool: "FakePool"):
        self.pool = pool

    async def fetchrow(self, sql: str, *args: Any):
        self.pool.queries.append(("fetchrow", sql))
        if self.pool.fetch_exc:
            raise self.pool.fetch_exc
        if self.pool.fetch_delay:
            await asyncio.sleep(self.pool.fetch_delay)
        return self.pool.row

    async def execute(self, sql: str, *args: Any):
        self.pool.queries.append(("execute", sql))
        self.pool.events.append(args)
        if self.pool.exec_exc:
            raise self.pool.exec_exc


class _Acquire:
    def __init__(self, pool: "FakePool"):
        self.pool = pool

    async def __aenter__(self):
        self.pool.acquires += 1
        return FakeConn(self.pool)

    async def __aexit__(self, *exc):
        return False


class FakePool:
    def __init__(self, row=None, fetch_exc=None, exec_exc=None, fetch_delay=0.0):
        self.row = row
        self.fetch_exc = fetch_exc
        self.exec_exc = exec_exc
        self.fetch_delay = fetch_delay
        self.queries: list[tuple[str, str]] = []
        self.events: list[tuple] = []
        self.acquires = 0

    def acquire(self):
        return _Acquire(self)


def _goal_row(key=False, path=False):
    return {"project": "AADS", "key_match": key, "path_match": path}


def _runner_row(goal=0, project=0):
    return {"goal_approved": goal, "project_approved": project}


def _doc_kwargs(pool):
    return dict(
        goal_id="g1", tenant_id="t1", ref="42", kind="plan",
        path="docs/plans/x.md", document_key="plan:abc", pool=pool,
    )


def _runner_kwargs(pool):
    return dict(goal_id="g1", tenant_id="t1", project="AADS", job_id="runner-1", pool=pool)


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    monkeypatch.delenv("CANONICAL_GATE_MODE", raising=False)
    monkeypatch.delenv("CANONICAL_GATE_TIMEOUT_MS", raising=False)


# ── 판정 규칙 ─────────────────────────────────────────────────────────


def test_goal_doc_rules():
    assert cg.judge_goal_document_rows(_goal_row(), kind="plan")[0] == cg.VERDICT_MISSING
    assert cg.judge_goal_document_rows(_goal_row(key=True), kind="plan")[0] == cg.VERDICT_OK
    assert cg.judge_goal_document_rows(_goal_row(path=True), kind="plan")[0] == cg.VERDICT_OK
    assert cg.judge_goal_document_rows(None, kind="plan")[0] == cg.VERDICT_UNKNOWN
    # 정본 head 의 kind CHECK 에 없는 prototype 은 정본이 될 수 없다 → 거짓 양성 방지
    assert cg.judge_goal_document_rows(_goal_row(), kind="prototype")[0] == cg.VERDICT_OK


def test_runner_rules_missing_only_when_goal_and_project_have_zero_approved():
    assert cg.judge_runner_goal_rows(_runner_row(0, 0))[0] == cg.VERDICT_MISSING
    assert cg.judge_runner_goal_rows(_runner_row(1, 0))[0] == cg.VERDICT_OK
    assert cg.judge_runner_goal_rows(_runner_row(0, 3))[0] == cg.VERDICT_OK
    assert cg.judge_runner_goal_rows(None)[0] == cg.VERDICT_UNKNOWN


def test_candidate_path_rule():
    for p in ("docs/prd/a.md", "docs/plans/x/y.md", "docs/design/d.md", "docs/contracts/c.md"):
        assert cg.is_candidate_path(p)
    for p in ("docs/specs/a.md", "app/docs/plans/a.md", "reports/a.md", "docs/plansX/a.md"):
        assert not cg.is_candidate_path(p)


def test_hook_script_regex_matches_service_regex():
    src = _COMMIT_SCRIPT.read_text(encoding="utf-8")
    assert f'CANDIDATE_RE = re.compile(r"{cg.CANDIDATE_PATH_RE.pattern}")' in src


def test_mode_parsing(monkeypatch):
    assert cg.get_mode() == "shadow"
    monkeypatch.setenv("CANONICAL_GATE_MODE", "OFF")
    assert cg.get_mode() == "off"
    monkeypatch.setenv("CANONICAL_GATE_MODE", "enforce")
    assert cg.get_mode() == "enforce"
    monkeypatch.setenv("CANONICAL_GATE_MODE", "bogus")
    assert cg.get_mode() == "shadow"


# ── 경고·기록 ─────────────────────────────────────────────────────────


def test_missing_returns_warning_and_records_event():
    pool = FakePool(row=_goal_row())
    warn = asyncio.run(cg.check_goal_document(**_doc_kwargs(pool)))
    assert warn and warn["verdict"] == "missing_canonical" and warn["entrypoint"] == "goal_api"
    assert len(pool.events) == 1
    entrypoint, project, ref, verdict, _detail, mode = pool.events[0]
    assert (entrypoint, project, ref, verdict, mode) == ("goal_api", "AADS", "42", "missing_canonical", "shadow")


def test_ok_records_but_returns_no_warning():
    pool = FakePool(row=_goal_row(path=True))
    assert asyncio.run(cg.check_goal_document(**_doc_kwargs(pool))) is None
    assert pool.events and pool.events[0][3] == "ok"


def test_runner_missing_warns_and_records():
    pool = FakePool(row=_runner_row(0, 0))
    warn = asyncio.run(cg.check_runner_submit(**_runner_kwargs(pool)))
    assert warn and warn["entrypoint"] == "runner_submit"
    assert pool.events[0][:4] == ("runner_submit", "AADS", "runner-1", "missing_canonical")


def test_enforce_behaves_like_shadow(monkeypatch):
    monkeypatch.setenv("CANONICAL_GATE_MODE", "enforce")
    pool = FakePool(row=_runner_row(0, 0))
    warn = asyncio.run(cg.check_runner_submit(**_runner_kwargs(pool)))
    assert warn and warn["verdict"] == "missing_canonical"  # 차단/예외 없이 경고만


# ── fail-open ─────────────────────────────────────────────────────────


@pytest.mark.parametrize("which", ["goal", "runner"])
def test_judge_exception_passes_through_and_records_unknown(which):
    pool = FakePool(fetch_exc=RuntimeError("db down"))
    if which == "goal":
        res = asyncio.run(cg.check_goal_document(**_doc_kwargs(pool)))
    else:
        res = asyncio.run(cg.check_runner_submit(**_runner_kwargs(pool)))
    assert res is None
    assert pool.events and pool.events[0][3] == "unknown"


@pytest.mark.parametrize("which", ["goal", "runner"])
def test_timeout_passes_through_and_records_unknown(monkeypatch, which):
    monkeypatch.setenv("CANONICAL_GATE_TIMEOUT_MS", "20")
    pool = FakePool(row=_runner_row(0, 0), fetch_delay=1.0)
    if which == "goal":
        res = asyncio.run(cg.check_goal_document(**_doc_kwargs(pool)))
    else:
        res = asyncio.run(cg.check_runner_submit(**_runner_kwargs(pool)))
    assert res is None
    assert pool.events[0][3] == "unknown"
    assert "timeout" in pool.events[0][4]


def test_record_failure_is_swallowed_and_warning_still_returned():
    """canonical_gate_events 가 아직 없는 환경(마이그레이션 전)."""
    pool = FakePool(row=_runner_row(0, 0), exec_exc=RuntimeError("relation does not exist"))
    warn = asyncio.run(cg.check_runner_submit(**_runner_kwargs(pool)))
    assert warn and warn["verdict"] == "missing_canonical"


def test_pool_unavailable_passes_through(monkeypatch):
    def boom():
        raise RuntimeError("pool not initialised")

    import app.core.db_pool as db_pool

    monkeypatch.setattr(db_pool, "get_pool", boom)
    kw = _runner_kwargs(None)
    assert asyncio.run(cg.check_runner_submit(**kw)) is None


def test_gate_internal_bug_cannot_escape(monkeypatch):
    """판정 함수에 예외를 주입해도 진입점 래퍼는 raise 하지 않는다."""

    def broken(*_a, **_k):
        raise ValueError("injected")

    monkeypatch.setattr(cg, "judge_goal_document_rows", broken)
    monkeypatch.setattr(cg, "judge_runner_goal_rows", broken)
    monkeypatch.setattr(cg, "_warning", broken)
    pool = FakePool(row=_goal_row())
    assert asyncio.run(cg.check_goal_document(**_doc_kwargs(pool))) is None
    pool = FakePool(row=_runner_row(0, 0))
    assert asyncio.run(cg.check_runner_submit(**_runner_kwargs(pool))) is None


# ── mode=off ──────────────────────────────────────────────────────────


def test_mode_off_issues_zero_queries_and_never_touches_pool(monkeypatch):
    monkeypatch.setenv("CANONICAL_GATE_MODE", "off")
    pool = FakePool(row=_goal_row())
    assert asyncio.run(cg.check_goal_document(**_doc_kwargs(pool))) is None
    assert asyncio.run(cg.check_runner_submit(**_runner_kwargs(pool))) is None
    assert pool.queries == [] and pool.acquires == 0

    import app.core.db_pool as db_pool

    def boom():
        raise AssertionError("get_pool must not be called when mode=off")

    monkeypatch.setattr(db_pool, "get_pool", boom)
    assert asyncio.run(cg.check_runner_submit(**_runner_kwargs(None))) is None


# ── 진입점 배선: 응답 기존 필드 불변 + 게이트 예외에도 통과 ──────────────


def test_submit_response_schema_keeps_existing_fields_and_omits_gate_by_default():
    from app.api.pipeline_runner import JobSubmitResponse

    resp = JobSubmitResponse(job_id="runner-1", status="queued", message="m")
    assert resp.model_dump(exclude_none=True) == {"job_id": "runner-1", "status": "queued", "message": "m"}
    warn = {"verdict": "missing_canonical"}
    assert JobSubmitResponse(job_id="j", status="queued", message="m", canonical_gate=warn).canonical_gate == warn


def test_submit_job_passes_when_gate_raises(monkeypatch):
    """제출 결과(job_id/status/message)는 게이트 결과와 무관해야 한다 — 소스 배선 검증."""
    src = (_REPO / "app" / "api" / "pipeline_runner.py").read_text(encoding="utf-8")
    start = src.index("# 정본 게이트(그림자): 목표가 확정된 제출만")
    block = src[start:start + 700]
    # 게이트 호출은 goal_link 확정 뒤에만, 반환값은 canonical_gate 한 필드로만 쓴다
    assert "if goal_link:" in block and "check_runner_submit" in block
    assert 'status="queued", message=msg, canonical_gate=gate' in block
    assert "raise" not in block and "HTTPException" not in block


def test_goal_document_endpoint_wiring_is_outside_transaction_and_non_blocking():
    src = (_REPO / "app" / "routers" / "goals.py").read_text(encoding="utf-8")
    start = src.index("# 정본 게이트(그림자): 트랜잭션 밖에서")
    block = src[start:start + 600]
    assert "check_goal_document" in block
    assert 'result["canonical_gate"] = gate' in block
    assert "raise" not in block
    before = src[:start].rstrip().splitlines()[-1]
    assert before.startswith("    result = dict(row)"), "게이트는 async with 블록 밖(들여쓰기 4)이어야 한다"


def test_goal_document_handler_returns_row_fields_and_survives_gate_failure(monkeypatch):
    """add_goal_document 를 직접 호출: 게이트가 터져도 기존 필드 그대로 반환."""
    from app.routers import goals as goals_mod

    row = {
        "id": 7, "kind": "plan", "doc_path": "docs/plans/x.md", "title": "t", "note": None,
        "document_key": "plan:abc", "version": "1.0.0", "status": "active", "is_latest": True,
        "change_summary": None, "supersedes_id": None, "updated_at": "now",
    }

    class _Tx:
        async def __aenter__(self):
            return None

        async def __aexit__(self, *e):
            return False

    class Conn:
        async def fetchval(self, *_a, **_k):
            return 1

        async def execute(self, *_a, **_k):
            return None

        async def fetchrow(self, *_a, **_k):
            return dict(row)

        def transaction(self):
            return _Tx()

    class P:
        def acquire(self):
            class A:
                async def __aenter__(s):
                    return Conn()

                async def __aexit__(s, *e):
                    return False

            return A()

    import app.core.db_pool as db_pool

    monkeypatch.setattr(db_pool, "get_pool", lambda: P())
    monkeypatch.setattr(goals_mod, "_tenant_id", lambda ctx: "t1")

    gate_pool = FakePool(fetch_exc=RuntimeError("db down"))
    orig = cg.check_goal_document

    async def with_pool(**k):
        return await orig(pool=gate_pool, **k)

    monkeypatch.setattr(cg, "check_goal_document", with_pool)
    req = goals_mod.GoalDocRequest(kind="plan", doc_path="docs/plans/x.md")
    out = asyncio.run(goals_mod.add_goal_document("g1", req, context={}))
    for k, v in row.items():
        assert out[k] == v
    assert "canonical_gate" not in out


# ── pre-commit 진입점 ─────────────────────────────────────────────────


def _git(cwd, *args):
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True).stdout


def _repo_with_staged(tmp_path, files: dict[str, str]):
    _git(tmp_path, "init", "-q")
    for rel, text in files.items():
        p = tmp_path / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8")
    _git(tmp_path, "add", "-A")
    return tmp_path


def _run_commit_script(cwd, **env):
    import os

    e = {**os.environ, **env}
    e.pop("CANONICAL_GATE_MODE", None) if "CANONICAL_GATE_MODE" not in env else None
    return subprocess.run(
        [sys.executable, str(_COMMIT_SCRIPT)], cwd=cwd, env=e, capture_output=True, text=True,
    )


def test_commit_script_warns_logs_fixed_format_and_exits_zero(tmp_path):
    repo = _repo_with_staged(tmp_path, {
        "docs/plans/A.md": "x", "docs/prd/B.md": "y", "docs/specs/C.md": "z", "app/x.py": "1",
    })
    res = _run_commit_script(repo)
    assert res.returncode == 0
    assert "docs/plans/A.md" in res.stderr and "docs/prd/B.md" in res.stderr
    assert "docs/specs/C.md" not in res.stderr
    lines = (repo / ".git" / "canonical_gate.log").read_text(encoding="utf-8").splitlines()
    assert len(lines) == 2
    for line in lines:
        f = line.split("\t")
        assert len(f) == 9 and f[0] == "CGv1" and f[2] == "commit"
        assert f[3] == "AADS" and f[5] == "unknown" and f[6] == "shadow" and f[7] == "A"
        assert len(f[8]) == 40
    assert {line.split("\t")[4] for line in lines} == {"docs/plans/A.md", "docs/prd/B.md"}


def test_commit_script_off_is_noop(tmp_path):
    repo = _repo_with_staged(tmp_path, {"docs/plans/A.md": "x"})
    res = _run_commit_script(repo, CANONICAL_GATE_MODE="off")
    assert res.returncode == 0 and res.stderr == ""
    assert not (repo / ".git" / "canonical_gate.log").exists()


def test_commit_script_exits_zero_outside_git_repo(tmp_path):
    res = _run_commit_script(tmp_path)
    assert res.returncode == 0


def test_commit_script_exits_zero_when_log_unwritable(tmp_path):
    repo = _repo_with_staged(tmp_path, {"docs/design/D.md": "x"})
    (repo / ".git" / "canonical_gate.log").mkdir()  # append 불가 → 예외
    res = _run_commit_script(repo)
    assert res.returncode == 0


def test_pre_commit_hook_block_cannot_change_exit_code():
    hook = (_REPO / "scripts" / "hooks" / "pre-commit").read_text(encoding="utf-8")
    start = hook.index("# ── 정본 게이트 (그림자 모드")
    end = hook.index("# ── 감사: hook 통과 서명")
    block = hook[start:end]
    assert "canonical_gate_commit.py" in block and "|| true" in block
    assert "exit " not in block.replace("종료코드", "")
    # 기존 차단 게이트들보다 뒤, 서명 기록보다 앞
    assert hook.index("AAG L1/L2 구조 게이트") < start < hook.index("HOOK_SIG=\"hook-verified:$(date +%s)\"\n# 호스트")


def test_migration_and_rollback_exist_and_are_not_applied_by_code():
    mig = (_REPO / "migrations" / "20261002_canonical_gate_events.sql").read_text(encoding="utf-8")
    for col in ("occurred_at", "entrypoint", "project", "ref", "verdict", "detail", "mode"):
        assert col in mig
    for ep in ("goal_api", "commit", "runner_submit"):
        assert ep in mig
    assert "DROP TABLE" in (_REPO / "migrations" / "rollback" / "20261002_canonical_gate_events.down.sql").read_text(encoding="utf-8")
