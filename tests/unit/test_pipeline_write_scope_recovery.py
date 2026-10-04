"""파일명 오탐 의존 대기 복구: 권위 있는 쓰기 범위 + 잘못된 자동 edge 의 dry-run/적용/롤백.

실측 체인(2026-10-04): runner-e266ec09 → 4fddd8cf → c1c59525 → 2066618c(awaiting_approval, /docs).
자동 추론 edge 는 "같은 파일" 이라는 이유로 걸리지만, 근거가 본문 basename(page.tsx)뿐이었다면
실제 교집합이 없는 무관한 작업을 막는다. 명시 depends_on 과 실제 동일 파일 충돌은 건드리지 않는다.
"""
from __future__ import annotations

import os

import pytest

os.environ.setdefault("JWT_SECRET_KEY", "unit-test-secret")
os.environ.setdefault("E2B_API_KEY", "unit-test-e2b-key")

from app.api import pipeline_runner as pr  # noqa: E402

AUTO = "file_conflict_auto_dependency"


def _job(job_id, instruction, *, status="queued", depends_on=None, logs=None):
    return {"job_id": job_id, "status": status, "depends_on": depends_on,
            "instruction": instruction, "logs": logs or []}


def _auto(parent):
    return [{"event": AUTO, "depends_on": parent}]


# ── 쓰기 범위 ────────────────────────────────────────────────────────────


def test_directive_false_positive_case_returns_only_declared_file():
    files = pr._extract_target_files("TARGET_FILES: src/app/docs/page.tsx\nDESCRIPTION: page.tsx")
    assert files == {"dashboard:src/app/docs/page.tsx"}


def test_external_absolute_read_only_path_does_not_demote_scope():
    text = (
        "TARGET_FILES: app/api/pipeline_runner.py, tests/unit/test_x.py\n"
        "READ_ONLY_FILES: app/services/chat_service.py, scripts/pipeline-runner.sh, /root/aads/AGENTS.md\n"
        "AGENTS.md 와 chat_service.py 를 참고하고 page.tsx 문구는 설명일 뿐이다.\n"
    )
    scope = pr._parse_write_scope(text)
    assert scope.explicit is True and scope.error == ""
    assert scope.write == {"server:app/api/pipeline_runner.py", "server:tests/unit/test_x.py"}
    assert pr._extract_target_files(text) == set(scope.write)


def test_external_absolute_path_in_target_files_stays_conservative():
    scope = pr._parse_write_scope("TARGET_FILES: /root/aads/AGENTS.md\n본문 app/x.py")
    assert scope.explicit is False and scope.error


def test_real_same_file_conflict_is_preserved():
    a = pr._extract_target_files("TARGET_FILES: src/app/chat/page.tsx\n설명")
    b = pr._extract_target_files("TARGET_FILES: src/app/chat/page.tsx, src/app/docs/page.tsx")
    assert pr._scope_overlap(a, b) == ["dashboard:src/app/chat/page.tsx"]


def test_basename_in_prose_does_not_overlap_other_job_target():
    docs = pr._extract_target_files("TARGET_FILES: src/app/docs/page.tsx")
    server = pr._extract_target_files("TARGET_FILES: app/x.py\nDESCRIPTION: page.tsx 를 읽는다")
    assert pr._scope_overlap(docs, server) == []


# ── 계획(dry-run) ────────────────────────────────────────────────────────


def _chain():
    return [
        _job("runner-2066618c", "TARGET_FILES: src/app/docs/page.tsx", status="awaiting_approval"),
        _job("runner-c1c59525", "TARGET_FILES: app/api/foo.py\nDESCRIPTION: page.tsx",
             depends_on="runner-2066618c", logs=_auto("runner-2066618c")),
        _job("runner-4fddd8cf", "TARGET_FILES: app/api/foo.py",
             depends_on="runner-c1c59525", logs=_auto("runner-c1c59525")),
        _job("runner-e266ec09", "TARGET_FILES: app/services/bar.py",
             depends_on="runner-4fddd8cf"),
    ]


def _verdicts(plan):
    return {(e["job_id"]): e["verdict"] for e in plan}


def test_plan_repairs_only_the_edge_without_real_overlap():
    plan = pr.plan_auto_dependency_repair(_chain())
    assert _verdicts(plan) == {
        "runner-c1c59525": "false_auto_edge",       # docs/page.tsx 와 겹치는 파일이 없다
        "runner-4fddd8cf": "real_conflict_keep",    # 같은 foo.py — 정당한 줄 세우기
        "runner-e266ec09": "explicit_keep",         # 사람이 건 명시 edge
    }
    edge = next(e for e in plan if e["job_id"] == "runner-c1c59525")
    assert edge["edge_kind"] == "auto" and edge["overlap"] == []
    assert edge["parent_files"] == ["dashboard:src/app/docs/page.tsx"]


def test_plan_never_touches_explicit_edge_even_without_overlap():
    rows = [
        _job("runner-aaaaaaaa", "TARGET_FILES: app/a.py", status="running"),
        _job("runner-bbbbbbbb", "TARGET_FILES: app/b.py", depends_on="runner-aaaaaaaa"),
    ]
    assert _verdicts(pr.plan_auto_dependency_repair(rows)) == {"runner-bbbbbbbb": "explicit_keep"}


def test_plan_keeps_edge_when_parent_already_terminal_or_child_running():
    rows = [
        _job("runner-aaaaaaaa", "TARGET_FILES: app/a.py", status="done"),
        _job("runner-bbbbbbbb", "TARGET_FILES: app/b.py", depends_on="runner-aaaaaaaa",
             logs=_auto("runner-aaaaaaaa")),
        _job("runner-cccccccc", "TARGET_FILES: app/c.py", status="running"),
        _job("runner-dddddddd", "TARGET_FILES: app/d.py", status="running",
             depends_on="runner-cccccccc", logs=_auto("runner-cccccccc")),
    ]
    verdicts = _verdicts(pr.plan_auto_dependency_repair(rows))
    assert verdicts["runner-bbbbbbbb"] == "parent_terminal_keep"
    assert verdicts["runner-dddddddd"] == "child_not_queued_keep"


def test_plan_reports_manual_when_child_still_conflicts_with_another_active_job():
    rows = [
        _job("runner-aaaaaaaa", "TARGET_FILES: app/a.py", status="running"),
        _job("runner-xxxxxxxx", "TARGET_FILES: app/shared.py", status="running"),
        _job("runner-bbbbbbbb", "TARGET_FILES: app/shared.py", depends_on="runner-aaaaaaaa",
             logs=_auto("runner-aaaaaaaa")),
    ]
    plan = pr.plan_auto_dependency_repair(rows)
    edge = next(e for e in plan if e["job_id"] == "runner-bbbbbbbb")
    assert edge["verdict"] == "other_conflict_manual"
    assert edge["other_conflicts"] == ["runner-xxxxxxxx"]


def test_plan_marks_cycle_and_still_only_releases_false_edges():
    rows = [
        _job("runner-aaaaaaaa", "TARGET_FILES: app/a.py", depends_on="runner-bbbbbbbb",
             logs=_auto("runner-bbbbbbbb")),
        _job("runner-bbbbbbbb", "TARGET_FILES: app/b.py", depends_on="runner-aaaaaaaa",
             logs=_auto("runner-aaaaaaaa")),
        _job("runner-cccccccc", "TARGET_FILES: app/c.py", depends_on="runner-cccccccc",
             logs=[]),
    ]
    plan = {e["job_id"]: e for e in pr.plan_auto_dependency_repair(rows)}
    assert plan["runner-aaaaaaaa"]["in_cycle"] and plan["runner-bbbbbbbb"]["in_cycle"]
    assert plan["runner-cccccccc"]["in_cycle"] and plan["runner-cccccccc"]["verdict"] == "explicit_keep"
    assert plan["runner-aaaaaaaa"]["verdict"] == "false_auto_edge"


def test_relinked_or_released_edges_are_not_treated_as_auto():
    parent = "runner-aaaaaaaa"
    relinked = _auto(parent) + [{"event": "superseded_dependency_relinked", "to_parent": parent}]
    assert pr._is_auto_dependency_edge(_auto(parent), parent) is True
    assert pr._is_auto_dependency_edge(relinked, parent) is False
    assert pr._is_auto_dependency_edge(_auto("runner-zzzzzzzz"), parent) is False
    assert pr._is_auto_dependency_edge('[{"event": "%s"}]' % AUTO, parent) is True
    restored = _auto(parent) + [{"event": "false_auto_dependency_released", "from_parent": parent},
                                {"event": "false_auto_dependency_restored", "restored_parent": parent}]
    assert pr._is_auto_dependency_edge(restored, parent) is True


def test_plan_hash_changes_with_repair_set_only():
    base = pr.plan_auto_dependency_repair(_chain())
    assert pr.auto_dependency_plan_hash(base) == pr.auto_dependency_plan_hash(
        pr.plan_auto_dependency_repair(list(reversed(_chain())))
    )
    other = _chain()
    other[1]["instruction"] = "TARGET_FILES: src/app/docs/page.tsx"
    assert pr.auto_dependency_plan_hash(pr.plan_auto_dependency_repair(other)) != pr.auto_dependency_plan_hash(base)


# ── 적용 / 롤백 / 엔드포인트 ─────────────────────────────────────────────


class _RecordingConn:
    def __init__(self, rows, *, update_hits=True, job_row=None):
        self.rows = rows
        self.update_hits = update_hits
        self.job_row = job_row
        self.updates: list[tuple[str, tuple]] = []
        self.notifies: list[str] = []

    def transaction(self):
        conn = self

        class _Tx:
            async def __aenter__(self):
                return conn

            async def __aexit__(self, *_a):
                return False

        return _Tx()

    async def fetch(self, query, *args):
        assert "FROM pipeline_jobs" in query
        return self.rows if "status = ANY" in query else []

    async def fetchrow(self, query, *args):
        if query.lstrip().startswith("UPDATE"):
            self.updates.append((query, args))
            return {"job_id": args[0]} if self.update_hits else None
        return self.job_row

    async def execute(self, query, *args):
        if "pg_notify" in query:
            self.notifies.append(args[0])


def _pool(conn):
    class _Acquire:
        async def __aenter__(self):
            return conn

        async def __aexit__(self, *_a):
            return False

    return type("P", (), {"acquire": lambda self: _Acquire()})()


def _context():
    return {"user": {"user_id": "ceo"}, "tenant": {"id": "33333333-3333-4333-8333-333333333333"},
            "membership": {"role": "admin"}}


@pytest.mark.asyncio
async def test_apply_requires_reviewed_plan_hash_and_reason(monkeypatch):
    import app.core.db_pool as db_pool
    from fastapi import HTTPException

    conn = _RecordingConn(_chain())
    monkeypatch.setattr(db_pool, "get_pool", lambda: _pool(conn))

    dry = await pr.repair_false_auto_dependencies(
        pr.DependencyRepairRequest(project="AADS"), _context())
    assert dry["action"] == "dry_run" and dry["summary"]["false_auto_edge"] == 1
    assert conn.updates == []  # dry-run 은 아무것도 바꾸지 않는다

    with pytest.raises(HTTPException) as no_reason:
        await pr.repair_false_auto_dependencies(
            pr.DependencyRepairRequest(project="AADS", action="apply", plan_hash=dry["plan_hash"]), _context())
    assert no_reason.value.status_code == 422

    with pytest.raises(HTTPException) as stale:
        await pr.repair_false_auto_dependencies(
            pr.DependencyRepairRequest(project="AADS", action="apply", plan_hash="0" * 16,
                                       reason="검토하지 않은 계획으로 적용 시도"), _context())
    assert stale.value.status_code == 409
    assert conn.updates == []

    applied = await pr.repair_false_auto_dependencies(
        pr.DependencyRepairRequest(project="AADS", action="apply", plan_hash=dry["plan_hash"],
                                   reason="파일명 오탐 자동 edge 해제 (dry-run 검토 완료)"), _context())
    assert applied["changes"] == [{
        "job_id": "runner-c1c59525",
        "before": {"status": "queued", "depends_on": "runner-2066618c"},
        "after": {"status": "queued", "depends_on": None},
        "applied": True,
    }]
    assert conn.notifies == ["runner-c1c59525"]
    query, args = conn.updates[0]
    # compare-and-set 이고 상태 전이(승인/취소)를 하지 않는다
    assert "status = 'queued' AND depends_on = $3" in query
    assert "SET depends_on = NULL" in query and "SET status" not in query
    assert args[0] == "runner-c1c59525" and args[2] == "runner-2066618c"


@pytest.mark.asyncio
async def test_apply_lost_race_reports_not_applied(monkeypatch):
    import app.core.db_pool as db_pool

    conn = _RecordingConn(_chain(), update_hits=False)
    monkeypatch.setattr(db_pool, "get_pool", lambda: _pool(conn))
    plan_hash = pr.auto_dependency_plan_hash(pr.plan_auto_dependency_repair(_chain()))

    result = await pr.repair_false_auto_dependencies(
        pr.DependencyRepairRequest(project="AADS", action="apply", plan_hash=plan_hash,
                                   reason="경합 상황 재현 테스트"), _context())

    assert result["changes"][0]["applied"] is False and result["changes"][0]["after"] is None
    assert conn.notifies == []


@pytest.mark.asyncio
async def test_rollback_restores_recorded_parent_only_when_still_valid(monkeypatch):
    import app.core.db_pool as db_pool

    released_logs = _auto("runner-2066618c") + [
        {"event": "false_auto_dependency_released", "from_parent": "runner-2066618c"}]
    conn = _RecordingConn([], job_row={"status": "queued", "depends_on": None, "logs": released_logs})
    monkeypatch.setattr(db_pool, "get_pool", lambda: _pool(conn))

    result = await pr.repair_false_auto_dependencies(
        pr.DependencyRepairRequest(project="AADS", action="rollback", job_ids=["runner-c1c59525"],
                                   reason="롤백 가능성 검증 테스트"), _context())
    assert result["results"][0]["restored"] is True
    assert result["results"][0]["after"] == {"depends_on": "runner-2066618c"}
    query, _ = conn.updates[0]
    assert "c.depends_on IS NULL" in query and "c.status = 'queued'" in query
    assert "p.status = ANY" in query  # 부모가 이미 종결이면 복원하지 않는다

    conn_no_event = _RecordingConn([], job_row={"status": "queued", "depends_on": None, "logs": []})
    monkeypatch.setattr(db_pool, "get_pool", lambda: _pool(conn_no_event))
    skipped = await pr.repair_false_auto_dependencies(
        pr.DependencyRepairRequest(project="AADS", action="rollback", job_ids=["runner-c1c59525"],
                                   reason="이벤트 없는 job 롤백"), _context())
    assert skipped["results"][0] == {"job_id": "runner-c1c59525", "restored": False,
                                     "reason": "no_release_event"}


def test_repair_endpoint_requires_admin_role_and_valid_ids():
    import inspect

    sig = inspect.signature(pr.repair_false_auto_dependencies)
    dependency = sig.parameters["context"].default.dependency
    assert dependency.__qualname__ == pr.require_tenant_admin.__qualname__
    with pytest.raises(ValueError):
        pr.DependencyRepairRequest(project="AADS", job_ids=["bad id; DROP"])


# ── 실측 체인: TARGET_FILES 없는 지시서의 basename 약한 교집합 ───────────────────


def _live_like():
    """runner-c1c59525 는 본문에 'page.tsx·extensions 는 건드리지 마라' 만 적었다(쓰기 아님)."""
    return [
        _job("runner-2066618c", "TARGET: /root/aads/aads-dashboard\n"
             "변경: src/app/docs/page.tsx, src/app/docs/CanonicalDocuments.tsx. page.tsx 의 탭 구성을 바꾼다.",
             status="awaiting_approval"),
        _job("runner-c1c59525", "TARGET: /root/aads/aads-dashboard\n"
             "4. 범위: src/app/chat/MarkdownRenderer.tsx 의 table 렌더러. page.tsx·확장(extensions/) 은 건드리지 마라.",
             depends_on="runner-2066618c", logs=_auto("runner-2066618c")),
        _job("runner-4fddd8cf", "TARGET: /root/aads/aads-dashboard\n"
             "변경: src/app/chat/MarkdownRenderer.tsx 의 코드블록 렌더러",
             depends_on="runner-c1c59525", logs=_auto("runner-c1c59525")),
        _job("runner-e266ec09", "TARGET_FILES: src/app/chat/page.tsx, src/hooks/useArtifactPanel.ts",
             depends_on="runner-4fddd8cf"),
    ]


def test_weak_basename_overlap_is_reported_not_released_by_default():
    plan = {e["job_id"]: e for e in pr.plan_auto_dependency_repair(_live_like())}
    edge = plan["runner-c1c59525"]
    assert edge["verdict"] == "weak_overlap_review" and edge["weak_overlap"] is True
    assert edge["overlap"] == ["server:page.tsx"]
    assert plan["runner-e266ec09"]["verdict"] == "explicit_keep"
    assert pr.auto_dependency_plan_hash(list(plan.values())) == pr.auto_dependency_plan_hash([])


def test_weak_overlap_flag_makes_edge_releasable_but_real_shared_path_stays():
    plan = {e["job_id"]: e for e in pr.plan_auto_dependency_repair(_live_like(), include_weak_overlap=True)}
    assert plan["runner-c1c59525"]["verdict"] == "false_auto_edge"
    # 4fddd8cf 와 c1c59525 는 MarkdownRenderer.tsx 를 실제로 같이 쓴다 — 약한 교집합이 아니다
    assert plan["runner-4fddd8cf"]["verdict"] == "real_conflict_keep"
    assert plan["runner-4fddd8cf"]["weak_overlap"] is False


def test_root_level_shared_file_is_a_strong_overlap():
    rows = [
        _job("runner-aaaaaaaa", "TARGET_FILES: package.json", status="running"),
        _job("runner-bbbbbbbb", "TARGET_FILES: package.json", depends_on="runner-aaaaaaaa",
             logs=_auto("runner-aaaaaaaa")),
    ]
    edge = pr.plan_auto_dependency_repair(rows, include_weak_overlap=True)[0]
    assert edge["verdict"] == "real_conflict_keep" and edge["weak_overlap"] is False
