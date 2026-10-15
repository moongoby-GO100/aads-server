"""FR-13a 러너 승인 전 사전검증 — 게이트 공유, base 신선도(실제 git), depends_on, 엔드포인트, 409 회귀."""
from __future__ import annotations

import asyncio
import subprocess
from pathlib import Path

import pytest
from fastapi import HTTPException

from app.api import pipeline_runner as pr
from app.services import runner_approval_precheck as svc

SHA = "a" * 40
DIFF = "diff --git a/x.py b/x.py\n+1\n"
APPROVE_REVIEW = {"verdict": "APPROVE", "score": 9, "flag_category": None, "needs_retry": False}


def run(coro):
    return asyncio.run(coro)


def by_key(checks):
    return {c["key"]: c for c in checks}


def gates(**kw):
    base = dict(deploy_only=False, git_diff=DIFF, commit_hash=SHA, changed_files=["x.py"], review=APPROVE_REVIEW)
    base.update(kw)
    return by_key(svc.evaluate_approval_gates(**base))


# ── 게이트 판정 (approve_or_reject 와 공유) ───────────────────────────────


def test_gates_all_pass():
    g = gates()
    assert {k: v["status"] for k, v in g.items()} == {
        "git_diff": "pass", "commit_hash": "pass", "changed_files": "pass", "ai_review": "pass",
    }


@pytest.mark.parametrize(
    "override,key,detail",
    [
        ({"git_diff": ""}, "git_diff", "승인 차단: 유효한 git diff가 없습니다"),
        ({"commit_hash": "abc"}, "commit_hash", "승인 차단: 승인용 commit_hash가 없습니다"),
        ({"commit_hash": ""}, "commit_hash", "승인 차단: 승인용 commit_hash가 없습니다"),
        ({"changed_files": []}, "changed_files", "승인 차단: 실제 변경 파일 목록이 없습니다"),
        ({"review": None}, "ai_review", "승인 차단: AI 리뷰 결과가 없습니다"),
        (
            {"review": {"verdict": "REJECT", "score": 3, "flag_category": None, "needs_retry": True}},
            "ai_review",
            "승인 차단: AI 리뷰 미통과 (REJECT, score=3)",
        ),
        (
            {"review": {"verdict": "REJECT", "score": 3, "flag_category": "security", "needs_retry": True}},
            "ai_review",
            "승인 차단: AI 리뷰 미통과 (REJECT, score=3), category=security",
        ),
    ],
)
def test_gate_failures_carry_the_409_message(override, key, detail):
    g = gates(**override)
    assert g[key] == {"key": key, "status": "fail", "detail": detail}


def test_first_failure_follows_original_order():
    checks = svc.evaluate_approval_gates(
        deploy_only=False, git_diff="", commit_hash="", changed_files=[], review=None,
    )
    assert svc.first_failure(checks)["key"] == "git_diff"


def test_deploy_only_skips_all_gates():
    checks = svc.evaluate_approval_gates(
        deploy_only=True, git_diff="", commit_hash="", changed_files=[], review=None,
    )
    assert svc.first_failure(checks) is None


def test_deploy_only_detection_uses_first_20_lines():
    assert svc.is_deploy_only_instruction("DEPLOY_ONLY: true\nx")
    assert not svc.is_deploy_only_instruction("\n" * 25 + "DEPLOY_ONLY: true")
    assert pr._is_deploy_only_instruction("DEPLOY_ONLY: true\nx")
    assert not pr._is_deploy_only_instruction("\n" * 25 + "DEPLOY_ONLY: true")


# ── depends_on ────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "parent,status,expected",
    [
        (None, None, "pass"),
        ("runner-p", "done", "pass"),
        ("runner-p", "error", "fail"),
        ("runner-p", "cancelled", "fail"),
        ("runner-p", "running", "warn"),
        ("runner-p", None, "warn"),
    ],
)
def test_depends_on(parent, status, expected):
    assert svc.check_depends_on(parent, status)["status"] == expected


# ── base 신선도: 실제 임시 git 저장소 ─────────────────────────────────────


def _git(repo: Path, *args: str) -> str:
    out = subprocess.run(
        ["git", "-C", str(repo), "-c", "user.email=t@t", "-c", "user.name=t", *args],
        check=True, capture_output=True, text=True,
    )
    return out.stdout.strip()


def _commit(repo: Path, name: str, content: str = "x") -> str:
    (repo / name).write_text(content)
    _git(repo, "add", name)
    _git(repo, "commit", "-q", "-m", f"c {name}")
    return _git(repo, "rev-parse", "HEAD")


@pytest.fixture()
def repo(tmp_path):
    r = tmp_path / "repo"
    r.mkdir()
    _git(r, "init", "-q", "-b", "main")
    _commit(r, "base.txt")
    return r


def _origin_main_at(repo: Path, sha: str):
    _git(repo, "update-ref", "refs/remotes/origin/main", sha)


def test_base_fast_forward_passes(repo):
    base = _git(repo, "rev-parse", "HEAD")
    _origin_main_at(repo, base)
    sha = _commit(repo, "a.txt")
    res = run(svc.check_base_freshness(str(repo), sha, ["a.txt"]))
    assert res["status"] == "pass" and "fast-forward" in res["detail"]


def test_base_already_present_passes(repo):
    sha = _commit(repo, "a.txt")
    _origin_main_at(repo, sha)
    assert run(svc.check_base_freshness(str(repo), sha, ["a.txt"]))["status"] == "pass"


def test_base_stale_with_same_file_fails(repo):
    base = _git(repo, "rev-parse", "HEAD")
    sha = _commit(repo, "shared.txt", "mine")
    _git(repo, "checkout", "-q", "-b", "other", base)
    upstream = _commit(repo, "shared.txt", "theirs")
    _origin_main_at(repo, upstream)
    res = run(svc.check_base_freshness(str(repo), sha, ["shared.txt"]))
    assert res["status"] == "fail"
    assert "stale_base" in res["detail"] and "shared.txt" in res["detail"]


def test_base_stale_without_overlap_warns(repo):
    base = _git(repo, "rev-parse", "HEAD")
    sha = _commit(repo, "mine.txt")
    _git(repo, "checkout", "-q", "-b", "other", base)
    upstream = _commit(repo, "theirs.txt")
    _origin_main_at(repo, upstream)
    res = run(svc.check_base_freshness(str(repo), sha, ["mine.txt"]))
    assert res["status"] == "warn" and "stale_base" in res["detail"]


def test_base_overlap_falls_back_to_commit_files_when_list_empty(repo):
    base = _git(repo, "rev-parse", "HEAD")
    sha = _commit(repo, "shared.txt", "mine")
    _git(repo, "checkout", "-q", "-b", "other", base)
    _origin_main_at(repo, _commit(repo, "shared.txt", "theirs"))
    assert run(svc.check_base_freshness(str(repo), sha, []))["status"] == "fail"


def test_base_unknown_commit_warns(repo):
    _origin_main_at(repo, _git(repo, "rev-parse", "HEAD"))
    assert run(svc.check_base_freshness(str(repo), "b" * 40, []))["status"] == "warn"


def test_base_missing_origin_main_warns(repo):
    sha = _commit(repo, "a.txt")
    assert run(svc.check_base_freshness(str(repo), sha, []))["status"] == "warn"


def test_base_not_a_repo_warns(tmp_path):
    assert run(svc.check_base_freshness(str(tmp_path), SHA, []))["status"] == "warn"
    assert run(svc.check_base_freshness("", SHA, []))["status"] == "warn"


def test_git_timeout_is_warn_not_exception(repo, monkeypatch):
    async def slow(*a, **k):
        return 124, ""

    monkeypatch.setattr(svc, "_run_git", slow)
    res = run(svc.check_base_freshness(str(repo), SHA, []))
    assert res["status"] == "warn"


def test_git_has_ten_second_timeout():
    assert svc.GIT_TIMEOUT_SEC == 10


def test_total_git_time_is_bounded(repo, monkeypatch):
    async def hang(*a, **k):
        await asyncio.sleep(5)
        return 0, ""

    monkeypatch.setattr(svc, "GIT_TIMEOUT_SEC", 0.05)
    monkeypatch.setattr(svc, "_run_git", hang)
    res = run(svc.check_base_freshness(str(repo), SHA, []))
    assert res["status"] == "warn" and "중단" in res["detail"]


def test_run_git_kills_process_on_timeout(repo, monkeypatch):
    monkeypatch.setattr(svc, "GIT_TIMEOUT_SEC", 0.0001)
    rc, out = run(svc._run_git(str(repo), "rev-parse", "HEAD"))
    assert rc in (0, 124)


# ── precheck() 통합 (가짜 conn) ────────────────────────────────────────


class FakeConn:
    def __init__(self, job, review=APPROVE_REVIEW, parent_status=None, busy=None):
        self.job, self.review, self.parent_status, self.busy = job, review, parent_status, busy
        self.executed = []

    async def fetchrow(self, sql, *args):
        if "FROM code_reviews" in sql:
            return self.review
        if "FROM pipeline_jobs" in sql:
            return self.job
        raise AssertionError(sql)

    async def fetchval(self, sql, *args):
        if "status = 'deploying'" in sql:
            return self.busy
        if "SELECT status FROM pipeline_jobs" in sql:
            return self.parent_status
        raise AssertionError(sql)

    async def execute(self, sql, *args):
        self.executed.append(sql)
        raise AssertionError("precheck 는 읽기 전용이어야 한다")


def job(**kw):
    base = dict(
        job_id="runner-1", project="AADS", status="awaiting_approval", instruction="do",
        git_diff=DIFF, commit_hash=SHA, actual_changed_files=["x.py"], depends_on=None,
    )
    base.update(kw)
    return base


def pre(conn, repo_dir=""):
    return run(svc.precheck("runner-1", conn=conn, tenant_id="t", repo_dir=repo_dir))


def test_precheck_missing_job_is_none():
    assert pre(FakeConn(None)) is None


def test_precheck_not_awaiting_short_circuits():
    res = pre(FakeConn(job(status="done")))
    assert res["ok"] is False
    assert [c["key"] for c in res["checks"]] == ["status"]


def test_precheck_ok_with_warn_only():
    res = pre(FakeConn(job()))
    assert res["ok"] is True
    keys = [c["key"] for c in res["checks"]]
    assert keys == [
        "status", "git_diff", "commit_hash", "changed_files", "ai_review",
        "base_freshness", "depends_on", "deploy_in_progress",
    ]
    assert by_key(res["checks"])["base_freshness"]["status"] == "warn"


def test_precheck_without_git_uses_db_hint_as_warn_only():
    conn = FakeConn(job(started_at="2026-10-15T00:00:00+00:00"))

    async def fetch(sql, *args):
        assert "actual_changed_files ?|" in sql
        return [{"job_id": "runner-other", "actual_changed_files": ["x.py"]}]

    conn.fetch = fetch
    res = pre(conn)
    fresh = by_key(res["checks"])["base_freshness"]
    assert fresh["status"] == "warn" and "runner-other" in fresh["detail"]
    assert res["ok"] is True


def test_precheck_reports_every_failure_not_only_first():
    res = pre(FakeConn(job(git_diff="", commit_hash=""), review=None))
    failed = {c["key"] for c in res["checks"] if c["status"] == "fail"}
    assert res["ok"] is False
    assert failed == {"git_diff", "commit_hash", "ai_review"}


def test_precheck_dead_parent_fails_and_busy_deploy_warns():
    res = pre(FakeConn(job(depends_on="runner-p"), parent_status="error", busy="runner-9"))
    c = by_key(res["checks"])
    assert c["depends_on"]["status"] == "fail"
    assert c["deploy_in_progress"]["status"] == "warn"
    assert res["ok"] is False


def test_precheck_deploy_only_skips_gates_and_git():
    res = pre(FakeConn(job(instruction="DEPLOY_ONLY: true", git_diff="", commit_hash="", actual_changed_files=[]), review=None))
    assert res["ok"] is True


def test_precheck_decodes_json_string_changed_files(repo):
    base = _git(repo, "rev-parse", "HEAD")
    sha = _commit(repo, "shared.txt", "mine")
    _git(repo, "checkout", "-q", "-b", "other", base)
    _origin_main_at(repo, _commit(repo, "shared.txt", "theirs"))
    res = pre(FakeConn(job(commit_hash=sha, actual_changed_files='["shared.txt"]')), repo_dir=str(repo))
    assert by_key(res["checks"])["base_freshness"]["status"] == "fail"


# ── 엔드포인트 ─────────────────────────────────────────────────────────


class FakePool:
    def __init__(self, conn):
        self.conn = conn

    def acquire(self):
        pool = self

        class _Ctx:
            async def __aenter__(self_inner):
                return pool.conn

            async def __aexit__(self_inner, *exc):
                return False

        return _Ctx()


CONTEXT = {"tenant": {"id": "t"}, "user": {"user_id": "u"}, "membership": {"role": "owner"}}


def test_endpoint_returns_precheck_and_404(monkeypatch):
    import app.core.db_pool as db_pool

    monkeypatch.setattr(db_pool, "get_pool", lambda: FakePool(FakeConn(job())))
    monkeypatch.setattr("app.core.project_config.get_workdir", lambda p: "")
    out = run(pr.approval_precheck("runner-1", context=CONTEXT))
    assert out["job_id"] == "runner-1" and out["ok"] is True

    monkeypatch.setattr(db_pool, "get_pool", lambda: FakePool(FakeConn(None)))
    with pytest.raises(HTTPException) as e:
        run(pr.approval_precheck("runner-1", context=CONTEXT))
    assert e.value.status_code == 404

    with pytest.raises(HTTPException) as e:
        run(pr.approval_precheck("bad id", context=CONTEXT))
    assert e.value.status_code == 400


def test_endpoint_registered_on_both_paths_as_get_only():
    paths = {
        r.path: r.methods for r in pr.router.routes if getattr(r, "path", "").endswith("/approval-precheck")
    }
    assert paths == {
        "/pipeline/jobs/{job_id}/approval-precheck": {"GET"},
        "/pipeline-runner/jobs/{job_id}/approval-precheck": {"GET"},
    }


# ── approve_or_reject 기존 409 경로 회귀 ───────────────────────────────────


class ApproveConn(FakeConn):
    def __init__(self, job, review=APPROVE_REVIEW):
        super().__init__(job, review)
        self.executed = []

    async def fetchrow(self, sql, *args):
        if "FROM code_reviews" in sql:
            return self.review
        return self.job

    async def fetchval(self, sql, *args):
        return True

    async def execute(self, sql, *args):
        self.executed.append(sql)
        return "UPDATE 1"

    def transaction(self):
        class _Tx:
            async def __aenter__(self_inner):
                return None

            async def __aexit__(self_inner, *exc):
                return False

        return _Tx()


@pytest.fixture()
def approve_env(monkeypatch):
    import app.core.db_pool as db_pool
    import app.services.e2e_verify as e2e

    async def col_exists(conn, name):
        return True

    async def no_gate(*a, **k):
        return None

    monkeypatch.setattr(pr, "_pipeline_column_exists", col_exists)
    monkeypatch.setattr(e2e, "apply_screen_approval_gate", no_gate)

    def install(conn):
        monkeypatch.setattr(db_pool, "get_pool", lambda: FakePool(conn))

    return install


def approve(action="approve"):
    return run(pr.approve_or_reject("runner-1", pr.JobApproveRequest(action=action), context=CONTEXT))


@pytest.mark.parametrize(
    "overrides,review,detail",
    [
        ({"git_diff": ""}, APPROVE_REVIEW, "승인 차단: 유효한 git diff가 없습니다"),
        ({"commit_hash": None}, APPROVE_REVIEW, "승인 차단: 승인용 commit_hash가 없습니다"),
        ({"actual_changed_files": []}, APPROVE_REVIEW, "승인 차단: 실제 변경 파일 목록이 없습니다"),
        ({}, None, "승인 차단: AI 리뷰 결과가 없습니다"),
        (
            {},
            {"verdict": "REJECT", "score": 2, "flag_category": "bug", "needs_retry": True},
            "승인 차단: AI 리뷰 미통과 (REJECT, score=2), category=bug",
        ),
    ],
)
def test_approve_keeps_409_messages(approve_env, overrides, review, detail):
    conn = ApproveConn(job(**overrides), review)
    approve_env(conn)
    with pytest.raises(HTTPException) as e:
        approve()
    assert e.value.status_code == 409 and e.value.detail == detail
    assert conn.executed == []


def test_approve_not_awaiting_is_400(approve_env):
    approve_env(ApproveConn(job(status="done")))
    with pytest.raises(HTTPException) as e:
        approve()
    assert e.value.status_code == 400 and e.value.detail == "승인 대기 상태가 아닙니다"


def test_approve_success_and_precheck_agree(approve_env):
    conn = ApproveConn(job())
    approve_env(conn)
    out = approve()
    assert out["action"] == "approve"
    assert any("UPDATE pipeline_jobs" in s for s in conn.executed)
    assert pre(FakeConn(job()))["ok"] is True


def test_reject_skips_gates(approve_env):
    conn = ApproveConn(job(git_diff="", commit_hash=None, actual_changed_files=[]), review=None)
    approve_env(conn)
    assert approve("reject")["action"] == "reject"
