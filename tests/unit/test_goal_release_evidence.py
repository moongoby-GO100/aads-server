"""릴리스 증거 다리 — **동작** 검증 (소스 문자열 검사가 아니다).

거부 사유 3번("기존 테스트가 소스 문자열만 확인하고 DB 재조정/멱등/자동전진을
행동으로 증명하지 못한다")에 대한 응답이다. 여기서는

* 인메모리 가짜 DB(`_FakeDB`)로 `reconcile_release_links` 를 **실제로 실행**해
  링크 승격 / 멱등 / 인증 배포 제외 / 접두사 거부를 행 상태 변화로 확인하고,
* 같은 가짜 DB 위에서 `GoalStateMachine.check_milestone_completion` 이 마일스톤을
  완료시키고 다음 마일스톤을 자동 개시하며 목표 진행률을 올리는지 확인하고,
* 배포 시점 훅(`scripts/record-release-provenance.sh`)은 **진짜 임시 git 저장소**를
  만들어 실행해 exact/ancestor/거부 판정을 검증한다.

DB 도, 컨테이너도, 프로덕션 데이터도 건드리지 않는다.
"""
from __future__ import annotations

import asyncio
import re
import subprocess
import uuid
from pathlib import Path

import pytest

from app.services import goal_manager as gm
from app.services import release_evidence as re_mod
from app.services.goal_manager import GoalStateMachine, goal_advance_gate
from app.services.release_evidence import (
    build_evidence_index,
    is_certified_deploy_row,
    normalize_full_sha,
    plan_release_completions,
    reconcile_release_links,
    select_best_provenance,
)

ROOT = Path(__file__).resolve().parents[2]
HOOK = ROOT / "scripts" / "record-release-provenance.sh"
CONTRACT = ROOT / "scripts" / "verify-bluegreen-release-contract.sh"

GOAL_ID = "1a00d8d3-1126-4a6e-8ed2-8a43f5502250"
CHILD_GOAL_ID = "380c796f-2524-4bfc-b9d8-a382a20096e9"
MS1 = "8519fd43-7070-49b0-8d85-1d053046cc1d"
MS2 = "9629fd43-7070-49b0-8d85-1d053046cc2e"

SHA_RELEASE = "a" * 40
SHA_TASK_ANCESTOR = "b" * 40
SHA_UNRELATED = "c" * 40


# ─── 순수 함수: SHA 정규화 / 인증 게이트 ────────────────────────────────────
@pytest.mark.parametrize(
    "value",
    [
        "0e15f47f",                       # 8자 축약
        "0e15f47f1234",                   # 12자 축약 (deploy_runs.release_sha 의 실제 형태)
        "0e15f47f" + "0" * 31,            # 39자
        "0e15f47f" + "0" * 33,            # 41자
        "g" * 40,                         # hex 아님
        "  ",
        "",
        None,
        12345,
        "a" * 39 + "Z",
    ],
)
def test_ambiguous_or_invalid_sha_is_rejected(value):
    """축약 접두사는 절대 full SHA 로 승격되지 않는다 (fail closed)."""
    assert normalize_full_sha(value) is None


def test_full_sha_is_normalized_to_lowercase():
    assert normalize_full_sha("A" * 40) == "a" * 40
    assert normalize_full_sha("  " + "f" * 40 + "\n") == "f" * 40


@pytest.mark.parametrize(
    "row,expected",
    [
        ({"status": "success", "phase": "completed", "image_digest": "sha256:x", "standby_digest": "sha256:x"}, True),
        # 실패/미완료 배포는 증거가 아니다
        ({"status": "failed", "phase": "completed", "image_digest": "sha256:x", "standby_digest": "sha256:x"}, False),
        ({"status": "running", "phase": "completed", "image_digest": "sha256:x", "standby_digest": "sha256:x"}, False),
        ({"status": "success", "phase": "p0p1_monitoring", "image_digest": "sha256:x", "standby_digest": "sha256:x"}, False),
        # 두 슬롯 이미지가 다르면 릴리스가 굳지 않은 것이다
        ({"status": "success", "phase": "completed", "image_digest": "sha256:x", "standby_digest": "sha256:y"}, False),
        ({"status": "success", "phase": "completed", "image_digest": None, "standby_digest": "sha256:x"}, False),
        ({"status": "success", "phase": "completed", "image_digest": "sha256:x", "standby_digest": None}, False),
        (None, False),
    ],
)
def test_certified_deploy_gate(row, expected):
    assert is_certified_deploy_row(row) is expected


def test_forged_exact_relationship_is_dropped():
    """relationship='exact' 인데 두 SHA 가 다르면 증거로 쓰지 않는다."""
    prov = [{"task_sha": SHA_TASK_ANCESTOR, "release_sha": SHA_RELEASE,
             "relationship": "exact", "deploy_run_id": 7}]
    deploys = [{"id": 7, "status": "success", "phase": "completed",
                "image_digest": "d", "standby_digest": "d"}]
    assert build_evidence_index(prov, deploys) == {}


def test_exact_beats_ancestor_and_newer_beats_older():
    rows = [
        {"relationship": "ancestor", "deploy_run_id": 99},
        {"relationship": "exact", "deploy_run_id": 3},
    ]
    assert select_best_provenance(rows)["relationship"] == "exact"
    assert select_best_provenance([
        {"relationship": "ancestor", "deploy_run_id": 3},
        {"relationship": "ancestor", "deploy_run_id": 9},
    ])["deploy_run_id"] == 9
    assert select_best_provenance([{"relationship": "bogus", "deploy_run_id": 1}]) is None


def test_plan_skips_failed_job_even_with_release_evidence():
    links = [{
        "link_id": "L1", "task_id": "job-1", "goal_id": GOAL_ID, "milestone_id": MS1,
        "link_status": "failed", "release_deploy_run_id": None,
        "commit_hash": SHA_TASK_ANCESTOR, "job_status": "error", "job_phase": "terminated",
    }]
    evidence = {SHA_TASK_ANCESTOR: {"release_sha": SHA_RELEASE, "relationship": "ancestor", "deploy_run_id": 7}}
    planned = plan_release_completions(links, evidence)
    assert planned[0]["action"] == "skip"
    assert planned[0]["reason"] == "job_failed"


# ─── 인메모리 가짜 DB ───────────────────────────────────────────────────────
class _FakeDB:
    """release_evidence + GoalStateMachine 이 실제로 쓰는 쿼리만 구현한 가짜 DB.

    행을 진짜로 갱신하므로 "두 번째 실행은 0건"(멱등)이나 "다음 마일스톤이
    in_progress 가 된다"(자동전진) 같은 성질을 상태 변화로 증명할 수 있다.
    """

    def __init__(self, *, link_columns=True):
        self.goals: list[dict] = []
        self.milestones: list[dict] = []
        self.links: list[dict] = []
        self.jobs: list[dict] = []
        self.deploy_runs: list[dict] = []
        self.provenance: list[dict] = []
        self.link_columns = link_columns
        self.executed: list[str] = []

    # -- 커넥션/풀 프로토콜 -------------------------------------------------
    def acquire(self):
        db = self

        class _Ctx:
            async def __aenter__(self):
                return db

            async def __aexit__(self, *exc):
                return False

        return _Ctx()

    # -- 조회 ---------------------------------------------------------------
    async def fetch(self, query, *args):
        q = " ".join(query.split())
        if "information_schema.columns" in q and "goal_task_links" in q:
            names = [
                "bind_source", "bound_by", "link_state", "detach_reason",
                "superseded_by", "superseded_at", "last_job_status",
                "reconciled_at", "updated_at",
            ]
            if self.link_columns:
                names += ["release_deploy_run_id", "release_sha",
                          "release_relationship", "release_verified_at"]
            return [{"column_name": n} for n in names]

        if "FROM goal_task_links l" in q and "JOIN pipeline_jobs j" in q:
            project, limit = args
            out = []
            for link in self.links:
                if (link.get("link_state") or "active") != "active" or link.get("superseded_by"):
                    continue
                job = self._job(link["task_id"])
                if job is None or not job.get("commit_hash"):
                    continue
                goal = self._goal(link.get("goal_id"))
                goal_project = goal["project"] if goal else None
                if project and goal_project != project and job.get("project") != project:
                    continue
                out.append({
                    "link_id": link["id"], "goal_id": link.get("goal_id"),
                    "milestone_id": link.get("milestone_id"), "task_id": link["task_id"],
                    "link_status": link.get("status"),
                    "release_deploy_run_id": link.get("release_deploy_run_id"),
                    "commit_hash": job.get("commit_hash"),
                    "job_status": job.get("status"), "job_phase": job.get("phase"),
                    "project": goal_project or job.get("project"),
                })
            return out[:limit]

        if "FROM deploy_release_provenance p" in q:
            shas, project = args
            out = []
            for prov in self.provenance:
                if prov["task_sha"] not in shas:
                    continue
                if project and prov.get("project") != project:
                    continue
                run = self._deploy_run(prov["deploy_run_id"])
                if run is None:
                    continue
                out.append({**prov, "status": run.get("status"), "phase": run.get("phase"),
                            "image_digest": run.get("image_digest"),
                            "standby_digest": run.get("standby_digest")})
            return sorted(out, key=lambda r: -r["deploy_run_id"])

        if "SELECT task_type, task_id, status FROM goal_task_links" in q:
            milestone_id = args[0]
            return [
                {"task_type": link["task_type"], "task_id": link["task_id"], "status": link.get("status")}
                for link in self.links
                if link.get("milestone_id") == milestone_id
                and (link.get("link_state") or "active") == "active"
                and not link.get("superseded_by")
            ]

        if "SET superseded_by" in q:  # _mark_superseded_failures
            return []

        raise AssertionError(f"unhandled fetch query: {q[:160]}")

    async def fetchrow(self, query, *args):
        q = " ".join(query.split())

        if "LEFT JOIN goals p ON p.id = g.parent_goal_id" in q:
            goal = self._goal(args[0])
            if goal is None:
                return None
            parent = self._goal(goal.get("parent_goal_id"))
            return {
                "id": goal["id"], "project": goal["project"], "title": goal["title"],
                "status": goal["status"], "parent_goal_id": goal.get("parent_goal_id"),
                "parent_status": parent["status"] if parent else None,
                "milestone_count": sum(1 for m in self.milestones if m["goal_id"] == goal["id"]),
            }

        if "SELECT status, phase FROM pipeline_jobs" in q:
            return self._job(args[0])

        if "SELECT goal_id FROM milestones WHERE id" in q:
            ms = self._milestone(args[0])
            return {"goal_id": ms["goal_id"]} if ms else None

        if "SELECT sequence_order FROM milestones WHERE id" in q:
            ms = self._milestone(args[0])
            return {"sequence_order": ms["sequence_order"]} if ms else None

        if "SELECT id, auto_advance FROM milestones" in q:
            goal_id, seq = args
            nxt = sorted(
                (m for m in self.milestones
                 if m["goal_id"] == goal_id and m["sequence_order"] > seq and m["status"] == "pending"),
                key=lambda m: m["sequence_order"],
            )
            return {"id": nxt[0]["id"], "auto_advance": nxt[0]["auto_advance"]} if nxt else None

        if "COUNT(*) AS total" in q:
            goal_id = args[0]
            mine = [m for m in self.milestones if m["goal_id"] == goal_id]
            return {"total": len(mine), "completed": sum(1 for m in mine if m["status"] == "completed")}

        if "SELECT project FROM goals WHERE id" in q:
            goal = self._goal(args[0])
            return {"project": goal["project"]} if goal else None

        if "status = 'draft'" in q and "FROM goals" in q:
            drafts = [g for g in self.goals if g["project"] == args[0] and g["status"] == "draft"]
            return {"id": drafts[0]["id"]} if drafts else None

        if "milestone_status" in q and "is_next_open" in q:  # _recover_stale_milestone_block
            ms = self._milestone(args[0])
            if ms is None:
                return None
            goal = self._goal(ms["goal_id"])
            earlier_open = any(
                m["goal_id"] == ms["goal_id"] and m["sequence_order"] < ms["sequence_order"]
                and m["status"] != "completed" for m in self.milestones
            )
            return {"goal_id": ms["goal_id"], "milestone_status": ms["status"],
                    "goal_status": goal["status"], "is_next_open": not earlier_open}

        if "FROM milestones WHERE goal_id" in q and "status = 'in_progress'" in q:
            found = sorted(
                (m for m in self.milestones if m["goal_id"] == args[0] and m["status"] == "in_progress"),
                key=lambda m: m["sequence_order"],
            )
            return {"id": found[0]["id"]} if found else None

        if "FROM milestones WHERE goal_id" in q and "status = 'pending'" in q:
            found = sorted(
                (m for m in self.milestones if m["goal_id"] == args[0] and m["status"] == "pending"),
                key=lambda m: m["sequence_order"],
            )
            return {"id": found[0]["id"]} if found else None

        raise AssertionError(f"unhandled fetchrow query: {q[:160]}")

    async def fetchval(self, query, *args):
        q = " ".join(query.split())
        if "SELECT status FROM goals" in q:
            goal = self._goal(args[0])
            return goal["status"] if goal else None
        raise AssertionError(f"unhandled fetchval query: {q[:160]}")

    # -- 갱신 ---------------------------------------------------------------
    async def execute(self, query, *args):
        q = " ".join(query.split())
        self.executed.append(q)

        if "UPDATE goal_task_links" in q and "release_deploy_run_id = $2::bigint" in q:
            link_id, run_id, release_sha, relationship = args
            link = self._link(link_id)
            if link is None or link.get("release_deploy_run_id") == run_id:
                return "UPDATE 0"
            link.update({
                "status": "completed", "last_job_status": "completed",
                "release_deploy_run_id": run_id, "release_sha": release_sha,
                "release_relationship": relationship,
            })
            return "UPDATE 1"

        if "UPDATE goal_task_links SET status = 'completed'" in q:
            milestone_id, task_id = args
            for link in self.links:
                if link.get("milestone_id") == milestone_id and link["task_id"] == task_id:
                    link["status"] = "completed"
            return "UPDATE 1"

        if "UPDATE milestones SET status = 'completed'" in q:
            ms = self._milestone(args[0])
            if ms and ms["status"] != "completed":
                ms["status"] = "completed"
                return "UPDATE 1"
            return "UPDATE 0"

        if "UPDATE milestones SET status = 'in_progress'" in q:
            ms = self._milestone(args[0])
            if ms:
                ms["status"] = "in_progress"
            return "UPDATE 1"

        if "UPDATE goals SET status = 'completed'" in q:
            goal = self._goal(args[0])
            if goal and goal["status"] != "completed":
                goal.update({"status": "completed", "progress": 1.0})
                return "UPDATE 1"
            return "UPDATE 0"

        if "UPDATE goals SET progress = $2" in q:
            goal = self._goal(args[0])
            if goal:
                goal["progress"] = args[1]
            return "UPDATE 1"

        if "UPDATE goals SET status = 'active'" in q:
            goal = self._goal(str(args[0]))
            if goal:
                goal["status"] = "active"
            return "UPDATE 1"

        if "UPDATE milestones" in q or "UPDATE goals" in q:
            return "UPDATE 0"

        raise AssertionError(f"unhandled execute query: {q[:160]}")

    # -- 조회 헬퍼 ----------------------------------------------------------
    def _goal(self, goal_id):
        return next((g for g in self.goals if g["id"] == goal_id), None) if goal_id else None

    def _milestone(self, milestone_id):
        return next((m for m in self.milestones if m["id"] == milestone_id), None)

    def _link(self, link_id):
        return next((link for link in self.links if link["id"] == link_id), None)

    def _job(self, job_id):
        return next((j for j in self.jobs if j["job_id"] == job_id), None)

    def _deploy_run(self, run_id):
        return next((d for d in self.deploy_runs if d["id"] == run_id), None)


@pytest.fixture
def db(monkeypatch):
    """가짜 DB 를 풀로 주입하고, 모듈 수준 스키마 캐시를 매 테스트 초기화한다."""
    fake = _FakeDB()
    monkeypatch.setattr("app.core.db_pool.get_pool", lambda: fake)
    gm._link_columns_cache = None
    re_mod.reset_schema_cache()
    return fake


@pytest.fixture
def traces(monkeypatch):
    captured: list[dict] = []

    async def _record(**kwargs):
        captured.append(kwargs)
        return True

    monkeypatch.setattr("app.services.ohvis_harness_trace.record_trace", _record)
    return captured


def _seed_release_scenario(fake, *, relationship="ancestor", certified=True, task_sha=SHA_TASK_ANCESTOR):
    fake.goals.append({"id": GOAL_ID, "project": "AADS", "title": "채팅 안정화",
                       "status": "active", "progress": 0.0, "parent_goal_id": None})
    fake.milestones += [
        {"id": MS1, "goal_id": GOAL_ID, "sequence_order": 1, "status": "in_progress", "auto_advance": True},
        {"id": MS2, "goal_id": GOAL_ID, "sequence_order": 2, "status": "pending", "auto_advance": True},
    ]
    fake.jobs.append({"job_id": "job-1", "project": "AADS", "status": "running",
                      "phase": "claude_code_work", "commit_hash": task_sha})
    fake.links.append({"id": str(uuid.uuid4()), "goal_id": GOAL_ID, "milestone_id": MS1,
                       "task_type": "pipeline_job", "task_id": "job-1", "status": "running",
                       "link_state": "active", "superseded_by": None,
                       "release_deploy_run_id": None})
    fake.deploy_runs.append({
        "id": 42, "project": "AADS", "status": "success" if certified else "failed",
        "phase": "completed", "image_digest": "sha256:abc", "standby_digest": "sha256:abc",
    })
    fake.provenance.append({"deploy_run_id": 42, "project": "AADS", "task_sha": task_sha,
                            "release_sha": SHA_RELEASE if relationship == "ancestor" else task_sha,
                            "relationship": relationship})
    return fake


# ─── 동작 검증: 재조정 ──────────────────────────────────────────────────────
def test_ancestor_provenance_completes_link_and_advances_milestone(db, traces):
    _seed_release_scenario(db, relationship="ancestor")

    result = asyncio.run(reconcile_release_links("AADS", dry_run=False))

    assert result["completed"] == 1
    link = db.links[0]
    assert link["status"] == "completed"
    assert link["release_deploy_run_id"] == 42
    assert link["release_sha"] == SHA_RELEASE
    assert link["release_relationship"] == "ancestor"

    # GoalStateMachine 이 마일스톤을 완료시키고 다음 단계를 자동 개시했다.
    assert db._milestone(MS1)["status"] == "completed"
    assert db._milestone(MS2)["status"] == "in_progress"
    assert db._goal(GOAL_ID)["progress"] == 0.5

    # 상관 ID 가 붙은 goal_release_evidence trace 가 남는다.
    evidence_traces = [t for t in traces if t.get("run_type") == "goal_release_evidence"]
    assert evidence_traces, "goal_release_evidence trace missing"
    meta = evidence_traces[-1]["metadata"]
    assert re.fullmatch(r"[0-9a-f]{16}", meta["correlation_id"])
    assert meta["deploy_run_ids"] == [42]
    assert meta["release_shas"] == [SHA_RELEASE]


def test_exact_full_sha_completes_link(db):
    _seed_release_scenario(db, relationship="exact", task_sha=SHA_RELEASE)

    result = asyncio.run(reconcile_release_links("AADS", dry_run=False))

    assert result["completed"] == 1
    assert db.links[0]["release_relationship"] == "exact"
    assert db.links[0]["release_sha"] == SHA_RELEASE


def test_second_pass_is_idempotent(db):
    _seed_release_scenario(db)

    first = asyncio.run(reconcile_release_links("AADS", dry_run=False))
    db.executed.clear()
    second = asyncio.run(reconcile_release_links("AADS", dry_run=False))

    assert first["completed"] == 1
    assert second["completed"] == 0
    assert second["counts"].get("skip:already_release_certified") == 1
    assert not [q for q in db.executed if "release_deploy_run_id = $2::bigint" in q]
    # 두 번째 실행이 마일스톤/목표를 다시 건드리지 않는다.
    assert second["milestones_recomputed"] == 0
    assert db._milestone(MS2)["status"] == "in_progress"


def test_dry_run_writes_nothing(db):
    _seed_release_scenario(db)

    result = asyncio.run(reconcile_release_links("AADS", dry_run=True))

    assert result["planned"] == 1
    assert result["completed"] == 0
    assert db.links[0]["status"] == "running"
    assert db.links[0]["release_deploy_run_id"] is None
    assert db._milestone(MS1)["status"] == "in_progress"


@pytest.mark.parametrize(
    "mutation",
    [
        {"status": "failed"},                       # 실패한 배포
        {"status": "running"},                      # 아직 진행 중
        {"phase": "p0p1_monitoring"},               # 인증 전 (5분 모니터링 중)
        {"standby_digest": "sha256:different"},     # 두 슬롯 이미지 불일치
        {"standby_digest": None},                   # standby 동기화 안 됨
        {"image_digest": None},
    ],
)
def test_uncertified_deploy_never_produces_evidence(db, mutation):
    _seed_release_scenario(db)
    db.deploy_runs[0].update(mutation)

    result = asyncio.run(reconcile_release_links("AADS", dry_run=False))

    assert result["completed"] == 0
    assert result["evidence_shas"] == 0
    assert result["counts"].get("skip:no_certified_release") == 1
    assert db.links[0]["status"] == "running"
    assert db._milestone(MS1)["status"] == "in_progress"


@pytest.mark.parametrize("bad_sha", ["0e15f47f1234", "0e15f47f", "z" * 40, "a" * 39])
def test_ambiguous_commit_hash_is_never_matched(db, bad_sha):
    """축약/불량 커밋 해시는 계보가 있어도 완료로 승격되지 않는다."""
    _seed_release_scenario(db, task_sha=bad_sha)

    result = asyncio.run(reconcile_release_links("AADS", dry_run=False))

    assert result["completed"] == 0
    assert result["resolvable_shas"] == 0
    assert result["counts"].get("skip:unresolvable_task_sha") == 1
    assert db.links[0]["status"] == "running"


def test_missing_migration_170_degrades_to_readonly_report(monkeypatch):
    fake = _FakeDB(link_columns=False)
    monkeypatch.setattr("app.core.db_pool.get_pool", lambda: fake)
    gm._link_columns_cache = None
    re_mod.reset_schema_cache()
    _seed_release_scenario(fake)

    result = asyncio.run(reconcile_release_links("AADS", dry_run=False))

    assert result["error"] == "migration_170_required"
    assert result["completed"] == 0
    assert fake.links[0]["status"] == "running"
    assert not fake.executed


def test_release_evidence_never_marks_goals_or_milestones_directly(db):
    _seed_release_scenario(db)
    asyncio.run(reconcile_release_links("AADS", dry_run=False))

    # 이 모듈이 직접 실행한 문장은 goal_task_links 갱신뿐이어야 하고,
    # milestones/goals 변경은 전부 GoalStateMachine 경로에서 나와야 한다.
    direct = [q for q in db.executed if "release_deploy_run_id = $2::bigint" in q]
    assert len(direct) == 1
    assert all("UPDATE goals" not in q for q in direct)
    assert all("UPDATE milestones" not in q for q in direct)


# ─── 동작 검증: 제로 마일스톤 / 부모 게이트 ─────────────────────────────────
@pytest.mark.parametrize(
    "milestone_count,parent_id,parent_status,expected",
    [
        (0, None, None, "no_milestones"),
        (0, GOAL_ID, "completed", "no_milestones"),
        (4, GOAL_ID, "active", "parent_incomplete"),
        (4, GOAL_ID, "draft", "parent_incomplete"),
        (4, GOAL_ID, "paused", "parent_incomplete"),
        (4, GOAL_ID, "completed", None),
        (4, GOAL_ID, "cancelled", None),
        (4, None, None, None),
    ],
)
def test_goal_advance_gate(milestone_count, parent_id, parent_status, expected):
    assert goal_advance_gate(
        milestone_count=milestone_count,
        parent_goal_id=parent_id,
        parent_status=parent_status,
    ) == expected


def test_zero_milestone_goal_is_skipped_without_emitting_a_trace(db, traces):
    """마일스톤 0개 active 목표가 사이클마다 trace 를 찍던 루프가 사라진다."""
    db.goals.append({"id": CHILD_GOAL_ID, "project": "AADS",
                     "title": "오비스 자율 오케스트레이션 완성", "status": "active",
                     "progress": 0.0, "parent_goal_id": None})

    result = asyncio.run(GoalStateMachine().advance_goal(CHILD_GOAL_ID))

    assert result["advanced"] is False
    assert result["gated"] == "no_milestones"
    assert db._goal(CHILD_GOAL_ID)["status"] == "active"  # 상태를 바꾸지 않는다
    assert [t for t in traces if t.get("run_type") == "goal_advance"] == []
    assert not db.executed


def test_child_goal_does_not_start_while_parent_is_open(db, traces):
    db.goals += [
        {"id": GOAL_ID, "project": "AADS", "title": "부모", "status": "active",
         "progress": 0.0, "parent_goal_id": None},
        {"id": CHILD_GOAL_ID, "project": "AADS", "title": "오비스 자율 오케스트레이션 완성",
         "status": "draft", "progress": 0.0, "parent_goal_id": GOAL_ID},
    ]
    db.milestones.append({"id": MS2, "goal_id": CHILD_GOAL_ID, "sequence_order": 1,
                          "status": "pending", "auto_advance": True})

    result = asyncio.run(GoalStateMachine().advance_goal(CHILD_GOAL_ID))

    assert result["gated"] == "parent_incomplete"
    assert db._milestone(MS2)["status"] == "pending"   # 선행 목표 전에 시작하지 않는다
    assert db._goal(CHILD_GOAL_ID)["status"] == "draft"


def test_child_goal_starts_once_parent_completes(db):
    db.goals += [
        {"id": GOAL_ID, "project": "AADS", "title": "부모", "status": "completed",
         "progress": 1.0, "parent_goal_id": None},
        {"id": CHILD_GOAL_ID, "project": "AADS", "title": "오비스 자율 오케스트레이션 완성",
         "status": "draft", "progress": 0.0, "parent_goal_id": GOAL_ID},
    ]
    db.milestones.append({"id": MS2, "goal_id": CHILD_GOAL_ID, "sequence_order": 1,
                          "status": "pending", "auto_advance": True})

    result = asyncio.run(GoalStateMachine().advance_goal(CHILD_GOAL_ID))

    assert result["advanced"] is True
    assert db._milestone(MS2)["status"] == "in_progress"
    assert db._goal(CHILD_GOAL_ID)["status"] == "active"


def test_advance_sweep_reports_gated_goals_in_one_summary_trace(db, traces):
    db.goals.append({"id": CHILD_GOAL_ID, "project": "AADS", "title": "제로 마일스톤",
                     "status": "active", "progress": 0.0, "parent_goal_id": None})

    async def _fetch(query, *args):
        return [{"id": CHILD_GOAL_ID}]

    db.fetch = _fetch  # advance_active_goals 의 목표 목록 조회만 대체
    result = asyncio.run(GoalStateMachine().advance_active_goals("AADS"))

    assert result["gated"] == {"no_milestones": 1}
    sweeps = [t for t in traces if t.get("run_type") == "goals_advance_sweep"]
    assert len(sweeps) == 1
    assert sweeps[0]["metadata"]["gated"] == {"no_milestones": 1}


# ─── 동작 검증: 배포 시점 계보 훅 (진짜 git 저장소) ─────────────────────────
def _git(repo, *args):
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True, capture_output=True, text=True,
    ).stdout.strip()


@pytest.fixture
def release_repo(tmp_path):
    """release HEAD / 조상 / 무관한 분기 커밋을 가진 진짜 git 저장소."""
    repo = tmp_path / "release"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "config", "user.email", "test@example.com")
    _git(repo, "config", "user.name", "test")

    (repo / "a.txt").write_text("1")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "ancestor")
    ancestor = _git(repo, "rev-parse", "HEAD")

    (repo / "a.txt").write_text("2")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "release head")
    head = _git(repo, "rev-parse", "HEAD")

    # release HEAD 에 포함되지 않는 다른 분기
    _git(repo, "checkout", "-q", "-b", "side", ancestor)
    (repo / "b.txt").write_text("3")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "unrelated")
    unrelated = _git(repo, "rev-parse", "HEAD")
    _git(repo, "checkout", "-q", "main")

    return {"path": repo, "ancestor": ancestor, "head": head, "unrelated": unrelated}


def _run_hook(repo, candidates, tmp_path, *, deploy_run_id="42", release_ref="HEAD"):
    cand_file = tmp_path / f"candidates-{uuid.uuid4().hex}.txt"
    cand_file.write_text("\n".join(candidates) + "\n")
    return subprocess.run(
        [str(HOOK), "--repo", str(repo), "--deploy-run-id", deploy_run_id,
         "--project", "AADS", "--release-ref", release_ref,
         "--candidates-file", str(cand_file), "--emit-sql-only"],
        capture_output=True, text=True,
    )


def test_hook_classifies_exact_and_ancestor_and_rejects_the_rest(release_repo, tmp_path):
    r = release_repo
    proc = _run_hook(
        r["path"],
        [r["head"], r["ancestor"], r["unrelated"], r["head"][:12], "not-a-sha", "f" * 40],
        tmp_path,
    )
    assert proc.returncode == 0, proc.stderr
    sql = proc.stdout

    # 릴리스 HEAD 자신 = exact, 조상 = ancestor, 둘 다 40자 full SHA 로 기록된다.
    assert f"'{r['head']}', '{r['head']}', 'exact'" in sql
    assert f"'{r['ancestor']}', '{r['head']}', 'ancestor'" in sql

    # 릴리스에 없는 커밋 / 축약 접두사 / 문법 오류 / 존재하지 않는 객체는 전부 제외.
    assert r["unrelated"] not in sql
    assert "not-a-sha" not in sql
    assert "f" * 40 not in sql
    assert r["head"][:12] not in sql.replace(r["head"], "")

    # 축약 접두사 + 문법 오류 문자열 = 2건이 40자 검사에서 걸러진다.
    assert "rejected_ambiguous=2" in proc.stderr
    assert "rejected_unknown=1" in proc.stderr
    assert "rejected_not_contained=1" in proc.stderr
    assert "exact=1" in proc.stderr and "ancestor=1" in proc.stderr


def test_hook_sql_carries_the_certified_deploy_gate_and_is_idempotent(release_repo, tmp_path):
    proc = _run_hook(release_repo["path"], [release_repo["ancestor"]], tmp_path)
    sql = proc.stdout

    assert "INSERT INTO deploy_release_provenance" in sql
    assert "d.status = 'success'" in sql
    assert "d.phase = 'completed'" in sql
    assert "d.image_digest = d.standby_digest" in sql
    assert "d.image_digest IS NOT NULL" in sql and "d.standby_digest IS NOT NULL" in sql
    assert "ON CONFLICT (deploy_run_id, task_sha) DO NOTHING" in sql
    # 인증 조건은 EXISTS 가드로 INSERT 안에 있다 — 인증 전 실행해도 0행이다.
    assert sql.index("WHERE EXISTS") < sql.index("ON CONFLICT")


def test_hook_fails_closed_on_unresolvable_release_ref(release_repo, tmp_path):
    proc = _run_hook(release_repo["path"], [release_repo["ancestor"]], tmp_path,
                     release_ref="refs/heads/does-not-exist")
    assert proc.returncode == 4
    assert proc.stdout.strip() == ""
    assert "full 40-char SHA" in proc.stderr


def test_hook_refuses_non_numeric_deploy_run_id(release_repo, tmp_path):
    proc = _run_hook(release_repo["path"], [release_repo["ancestor"]], tmp_path,
                     deploy_run_id="42; DROP TABLE deploy_runs")
    assert proc.returncode == 3
    assert proc.stdout.strip() == ""


def test_hook_emits_nothing_when_no_candidate_is_contained(release_repo, tmp_path):
    proc = _run_hook(release_repo["path"], [release_repo["unrelated"]], tmp_path)
    assert proc.returncode == 0
    assert "INSERT INTO" not in proc.stdout


# ─── 릴리스 계약 ────────────────────────────────────────────────────────────
def test_release_contract_accepts_the_wired_repository():
    """계약 검증 스크립트가 이 워크트리를 통과해야 한다 (배포 preflight 게이트)."""
    proc = subprocess.run([str(CONTRACT), str(ROOT)], capture_output=True, text=True)
    assert proc.returncode == 0, proc.stderr


def test_release_contract_rejects_a_deploy_without_the_provenance_hook(tmp_path):
    """훅 호출이 사라지면 배포가 계약 검증에서 멈춰야 한다 (조용한 회귀 방지)."""
    fake_root = tmp_path / "root"
    fake_root.mkdir()
    for name in ("docker-compose.prod.yml", ".dockerignore"):
        (fake_root / name).write_text((ROOT / name).read_text())
    (fake_root / "scripts").mkdir()
    for script in ("verify-bluegreen-release-contract.sh", "record-release-provenance.sh"):
        target = fake_root / "scripts" / script
        target.write_text((ROOT / "scripts" / script).read_text())
        target.chmod(0o755)
    stripped = (ROOT / "deploy.sh").read_text().replace(
        "scripts/record-release-provenance.sh", "scripts/removed-hook.sh",
    )
    (fake_root / "deploy.sh").write_text(stripped)

    proc = subprocess.run(
        [str(fake_root / "scripts" / "verify-bluegreen-release-contract.sh"), str(fake_root)],
        capture_output=True, text=True,
    )
    assert proc.returncode != 0
    assert "provenance hook is not wired" in proc.stderr
