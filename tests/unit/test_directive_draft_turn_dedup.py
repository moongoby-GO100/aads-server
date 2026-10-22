"""같은 턴의 지시 초안 요청은 새 행이 아니라 기존 초안의 새 revision 이 된다."""
from __future__ import annotations

import asyncio
import json
import re
import uuid
from contextlib import asynccontextmanager
from pathlib import Path

import pytest

from app.services import directive_draft_service as service
from app.services.directive_draft_service import DraftSource, build_fallback_directive

ROOT = Path(__file__).resolve().parents[2]
MIGRATION = ROOT / "migrations" / "20261022_directive_draft_turn_dedup.sql"
ROLLBACK = ROOT / "migrations" / "rollback" / "20261022_directive_draft_turn_dedup.down.sql"

TENANT_A = str(uuid.uuid4())
TENANT_B = str(uuid.uuid4())


class UniqueViolation(Exception):
    pass


class FakeConn:
    """directive_draft_service.create_draft 가 쓰는 쿼리만 흉내 내는 메모리 DB."""

    def __init__(self):
        self.drafts: list[dict] = []
        self.artifacts: list[dict] = []
        self.revisions: list[dict] = []
        self.events: list[dict] = []

    @asynccontextmanager
    async def transaction(self):
        yield self

    def _active(self, row):
        return row["classification"].get("superseded_by") is None

    async def fetchrow(self, query, *args):
        if "FROM directive_drafts" in query and "selected_assistant_message_id' = $2" in query:
            tenant, mid = args
            hits = [
                d for d in self.drafts
                if d["tenant_id"] == tenant and self._active(d)
                and d["classification"].get("source_mode") == "selected_response"
                and d["classification"].get("selected_assistant_message_id") == mid
            ]
            return hits[-1] if hits else None
        if "FROM directive_drafts" in query and "turn_key' = $3" in query:
            tenant, session, key = args
            hits = [
                d for d in self.drafts
                if d["tenant_id"] == tenant and d["session_id"] == session and self._active(d)
                and d["classification"].get("source_mode") == "recent_context"
                and d["classification"].get("turn_key") == key
            ]
            return hits[-1] if hits else None
        if "INSERT INTO directive_drafts" in query:
            tenant, session, actor, project, title, content, risk, conf, ids, cls, model = args
            classification = json.loads(cls)
            mid = classification.get("selected_assistant_message_id")
            if classification["source_mode"] == "selected_response" and any(
                d["tenant_id"] == tenant and self._active(d)
                and d["classification"].get("source_mode") == "selected_response"
                and d["classification"].get("selected_assistant_message_id") == mid
                for d in self.drafts
            ):
                raise UniqueViolation("ux_directive_drafts_selected_response_turn")
            row = {
                "id": uuid.uuid4(), "tenant_id": tenant, "session_id": session, "artifact_id": None,
                "created_by": actor, "project_key": project, "title": title, "content": content,
                "status": "draft", "risk_level": risk, "confidence": conf, "current_revision": 1,
                "source_message_ids": list(ids), "classification": classification, "model_used": model,
            }
            self.drafts.append(row)
            return row
        if "UPDATE directive_drafts" in query and "SET title = $3" in query:
            did, tenant, title, content, risk, conf, ids, cls, model, revision = args
            row = next(d for d in self.drafts if d["id"] == did and d["tenant_id"] == tenant)
            row.update(
                title=title, content=content, risk_level=risk, confidence=conf,
                source_message_ids=list(ids), model_used=model, current_revision=revision,
            )
            row["classification"] = {**row["classification"], **json.loads(cls)}
            return row
        if "INSERT INTO chat_artifacts" in query:
            tenant, session, workspace, title, content, metadata = args
            row = {
                "id": uuid.uuid4(), "tenant_id": tenant, "session_id": session, "workspace_id": workspace,
                "type": "report", "title": title, "content": content, "metadata": json.loads(metadata),
            }
            self.artifacts.append(row)
            return row
        if "UPDATE chat_artifacts" in query:
            aid, tenant, title, content, revision = args
            row = next(a for a in self.artifacts if a["id"] == aid and a["tenant_id"] == tenant)
            row.update(title=title, content=content)
            row["metadata"] = {**row["metadata"], "revision": revision}
            return row
        if "FROM chat_artifacts" in query:
            aid, tenant = args
            return next((a for a in self.artifacts if a["id"] == aid and a["tenant_id"] == tenant), None)
        raise AssertionError(query)

    async def fetchval(self, query, *args):
        if "SELECT change_source FROM directive_draft_revisions" in query:
            rows = [r for r in self.revisions if r["draft_id"] == args[0]]
            return max(rows, key=lambda r: r["revision"])["change_source"] if rows else None
        raise AssertionError(query)

    async def execute(self, query, *args):
        if "pg_advisory_xact_lock" in query:
            return
        if "UPDATE directive_drafts SET artifact_id" in query:
            artifact_id, draft_id = args
            next(d for d in self.drafts if d["id"] == draft_id)["artifact_id"] = artifact_id
        elif "INSERT INTO directive_draft_revisions" in query:
            tenant, draft_id, revision, title, content, change_source, editor, metadata = args
            if any(r["draft_id"] == draft_id and r["revision"] == revision for r in self.revisions):
                raise UniqueViolation("directive_draft_revisions (draft_id, revision)")
            self.revisions.append({
                "draft_id": draft_id, "revision": revision, "title": title, "content": content,
                "change_source": change_source, "metadata": json.loads(metadata),
            })
        elif "INSERT INTO directive_draft_events" in query:
            tenant, draft_id, revision, action, actor, metadata = args
            self.events.append({"draft_id": draft_id, "revision": revision, "action": action})
        else:
            raise AssertionError(query)


class FakePool:
    def __init__(self, conn):
        self.conn = conn

    @asynccontextmanager
    async def acquire(self):
        yield self.conn


class Harness:
    def __init__(self, monkeypatch):
        self.conn = FakeConn()
        self.llm_calls = 0
        self.body = "첫 생성"
        self.source: DraftSource | None = None
        monkeypatch.setattr(service, "get_pool", lambda: FakePool(self.conn))
        monkeypatch.setattr(service, "_load_source", self._load_source)
        monkeypatch.setattr(service, "generate_directive_content", self._generate)

    async def _load_source(self, **_kwargs):
        return self.source

    async def _generate(self, source, risk_level, **_kwargs):
        self.llm_calls += 1
        content = build_fallback_directive(source, risk_level).replace("DESCRIPTION:", f"DESCRIPTION:\n{self.body}", 1)
        return content, "generated", "test-model"

    def create(self, tenant=TENANT_A):
        return asyncio.run(service.create_draft(tenant_id=tenant, user_id=None, session_id=str(uuid.uuid4())))


def _selected_source(assistant_id=None, session_id=None) -> DraftSource:
    assistant_id = assistant_id or uuid.uuid4()
    return DraftSource(
        session_id=session_id or uuid.uuid4(), workspace_id=uuid.uuid4(), session_title="세션",
        workspace_name="워크스페이스", project_key="AADS",
        messages=[
            {"id": uuid.uuid4(), "role": "user", "content": "파동엔진 정본을 통일해줘"},
            {"id": assistant_id, "role": "assistant", "content": "→ 다음 단계:\n1. 통일"},
        ],
        selected_assistant_message_id=assistant_id,
    )


def _context_source(session_id=None, extra=0) -> DraftSource:
    messages = [
        {"id": uuid.uuid4(), "role": "user", "content": "상태를 확인해줘"},
        {"id": uuid.uuid4(), "role": "assistant", "content": "정상입니다."},
    ]
    for index in range(extra):
        messages.append({"id": uuid.uuid4(), "role": "user", "content": f"추가 {index}"})
    return DraftSource(
        session_id=session_id or uuid.uuid4(), workspace_id=uuid.uuid4(), session_title="세션",
        workspace_name="워크스페이스", project_key="AADS", messages=messages,
    )


@pytest.fixture
def h(monkeypatch):
    return Harness(monkeypatch)


def test_same_turn_twice_keeps_one_row_and_adds_a_revision(h):
    h.source = _selected_source()
    first = h.create()
    h.body = "다시 생성한 본문"
    second = h.create()

    assert first["turn_dedup"] == "created" and second["turn_dedup"] == "revised"
    assert len(h.conn.drafts) == 1 and first["id"] == second["id"]
    assert second["current_revision"] == 2 and h.conn.drafts[0]["current_revision"] == 2
    assert [r["revision"] for r in h.conn.revisions] == [1, 2]
    assert [r["change_source"] for r in h.conn.revisions] == ["generated", "regenerated"]
    assert [e["action"] for e in h.conn.events] == ["created", "edited"]
    assert len(h.conn.artifacts) == 1
    assert h.conn.artifacts[0]["metadata"]["revision"] == 2
    assert "다시 생성한 본문" in h.conn.artifacts[0]["content"]
    assert second["artifact_id"] == first["artifact_id"] == str(h.conn.artifacts[0]["id"])


def test_identical_regeneration_adds_no_revision(h):
    h.source = _selected_source()
    h.create()
    again = h.create()
    assert again["turn_dedup"] == "existing"
    assert len(h.conn.drafts) == 1 and len(h.conn.revisions) == 1 and len(h.conn.events) == 1


def test_same_message_id_in_another_tenant_is_a_separate_draft(h):
    h.source = _selected_source()
    a = h.create(TENANT_A)
    b = h.create(TENANT_B)
    assert a["id"] != b["id"]
    assert {d["tenant_id"] for d in h.conn.drafts} == {uuid.UUID(TENANT_A), uuid.UUID(TENANT_B)}
    assert a["turn_dedup"] == b["turn_dedup"] == "created"


def test_different_assistant_message_is_a_separate_draft(h):
    h.source = _selected_source()
    first = h.create()
    h.source = _selected_source(session_id=h.source.session_id)
    second = h.create()
    assert first["id"] != second["id"] and len(h.conn.drafts) == 2


@pytest.mark.parametrize("frozen", ["user_edit", "artifact_edit"])
def test_human_edited_draft_is_returned_without_calling_the_llm(h, frozen):
    h.source = _selected_source()
    first = h.create()
    h.conn.revisions.append({
        "draft_id": uuid.UUID(first["id"]), "revision": 2, "title": "t", "content": "사람이 고친 본문",
        "change_source": frozen, "metadata": {},
    })
    h.conn.drafts[0].update(current_revision=2, content="사람이 고친 본문")
    calls = h.llm_calls
    h.body = "덮어쓰면 안 되는 본문"
    again = h.create()
    assert again["turn_dedup"] == "existing" and again["id"] == first["id"]
    assert h.llm_calls == calls
    assert h.conn.drafts[0]["content"] == "사람이 고친 본문" and len(h.conn.drafts) == 1


@pytest.mark.parametrize("status", ["approved", "sent", "rejected", "archived"])
def test_reviewed_draft_is_not_regenerated(h, status):
    h.source = _selected_source()
    first = h.create()
    h.conn.drafts[0]["status"] = status
    calls = h.llm_calls
    again = h.create()
    assert again["turn_dedup"] == "existing" and again["id"] == first["id"]
    assert h.llm_calls == calls and len(h.conn.drafts) == len(h.conn.revisions) == 1


def test_recent_context_same_window_is_one_row_regardless_of_message_order(h):
    h.source = _context_source()
    first = h.create()
    h.source = DraftSource(**{**h.source.__dict__, "messages": list(reversed(h.source.messages))})
    h.body = "컨텍스트 재생성"
    second = h.create()
    assert len(h.conn.drafts) == 1 and first["id"] == second["id"]
    assert second["turn_dedup"] == "revised" and second["current_revision"] == 2


def test_recent_context_new_message_or_session_is_a_new_turn(h):
    session = uuid.uuid4()
    h.source = _context_source(session_id=session)
    first = h.create()
    h.source = _context_source(session_id=session, extra=1)
    grown = h.create()
    h.source = DraftSource(**{**h.source.__dict__, "session_id": uuid.uuid4()})
    other_session = h.create()
    assert len({first["id"], grown["id"], other_session["id"]}) == 3
    assert h.conn.drafts[0]["classification"]["turn_key"].startswith("ctx:")


def test_other_modes_and_superseded_rows_are_never_matched(h):
    h.source = _selected_source()
    message_id = str(h.source.selected_assistant_message_id)
    milestone_goal, milestone = str(uuid.uuid4()), str(uuid.uuid4())
    base = {
        "id": uuid.uuid4(), "tenant_id": uuid.UUID(TENANT_A), "session_id": h.source.session_id,
        "artifact_id": None, "status": "draft", "current_revision": 1, "title": "t", "content": "c",
        "project_key": "AADS",
    }
    h.conn.drafts.append({**base, "classification": {
        "source_mode": "milestone_auto_advance", "goal_id": milestone_goal, "milestone_id": milestone,
        "selected_assistant_message_id": message_id,
    }})
    h.conn.drafts.append({**base, "id": uuid.uuid4(), "classification": {
        "source_mode": "selected_response", "selected_assistant_message_id": message_id,
        "superseded_by": str(uuid.uuid4()),
    }})
    snapshot = [dict(d) for d in h.conn.drafts]

    created = h.create()
    assert created["turn_dedup"] == "created" and len(h.conn.drafts) == 3
    assert h.conn.drafts[:2] == snapshot
    assert h.conn.drafts[0]["classification"]["goal_id"] == milestone_goal
    assert "goal_id" not in h.conn.drafts[2]["classification"]
    assert "milestone_id" not in h.conn.drafts[2]["classification"]
    assert h.create()["id"] == created["id"]


def test_turn_key_depends_on_tenant_session_and_ids_not_order():
    source = _context_source()
    reordered = DraftSource(**{**source.__dict__, "messages": list(reversed(source.messages))})
    assert service._turn_key(TENANT_A, source) == service._turn_key(TENANT_A, reordered)
    assert service._turn_key(TENANT_A, source) != service._turn_key(TENANT_B, source)
    selected = _selected_source()
    assert service._turn_key(TENANT_A, selected) == f"msg:{selected.selected_assistant_message_id}"


def _sql(path: Path) -> str:
    return re.sub(r"--[^\n]*", "", path.read_text(encoding="utf-8"))


def test_migration_adds_partial_unique_index_and_never_deletes_rows():
    sql = _sql(MIGRATION)
    assert "CREATE UNIQUE INDEX IF NOT EXISTS ux_directive_drafts_selected_response_turn" in sql
    assert "(tenant_id, (classification->>'selected_assistant_message_id'))" in sql
    assert "classification->>'superseded_by' IS NULL" in sql
    assert not re.search(r"\b(DELETE|TRUNCATE|DROP)\b", sql, re.IGNORECASE)
    assert "superseded_by" in sql and "superseded_prev_status" in sql


def test_migration_only_assigns_a_status_the_check_constraint_allows():
    base = (ROOT / "migrations" / "172_directive_draft_copilot.sql").read_text(encoding="utf-8")
    allowed = set(re.search(r"status IN \(([^)]*)\)", base).group(1).replace("'", "").replace(" ", "").split(","))
    assigned = set(re.findall(r"THEN '([a-z]+)'", _sql(MIGRATION)))
    assert assigned and assigned <= allowed
    assert "superseded" not in allowed  # 허용 값에 없으므로 status 로는 쓰지 않고 classification 에만 기록한다.


def test_rollback_drops_index_and_restores_previous_status():
    sql = _sql(ROLLBACK)
    assert "DROP INDEX IF EXISTS ux_directive_drafts_selected_response_turn" in sql
    assert "superseded_prev_status" in sql
    assert not re.search(r"\b(DELETE|TRUNCATE)\b", sql, re.IGNORECASE)
