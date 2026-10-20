"""Server-side chat card for a mockup review (no database).

FakeConn answers the helper's three queries from memory. It proves the Python rules: which session is accepted,
that a retry updates instead of duplicating, that a failed write propagates (the caller's transaction rolls back).
The SQL itself runs in tests/integration/test_mockup_session_card_postgres.py (needs AADS_MOCKUP_TEST_DATABASE_URL).
"""
import asyncio
import json
from uuid import UUID, uuid4

import pytest
from fastapi import HTTPException

from app.services import mockup_review_service as svc

TENANT, OTHER_TENANT = uuid4(), uuid4()
SESSION, WORKSPACE = uuid4(), uuid4()
REVIEW, REV1, REV2 = uuid4(), uuid4(), uuid4()
PROJECT = "AADS"


class FakeConn:
    def __init__(self, session="default", fail_insert=False):
        self.session = {
            "tenant_id": TENANT, "user_id": "owner", "workspace_id": WORKSPACE, "workspace_tenant": TENANT,
            "project_key": PROJECT, "workspace_name": "AADS", "workspace_display_name": "AADS",
        } if session == "default" else session
        self.artifacts: list[dict] = []
        self.fail_insert = fail_insert
        self.queries: list[str] = []

    async def fetchrow(self, query, *args):
        self.queries.append(query)
        assert "FROM chat_sessions" in query
        return self.session

    async def fetch(self, query, *args):
        self.queries.append(query)
        assert query.startswith("UPDATE chat_artifacts")
        tenant, session, review, title, content, meta = args
        hit = [a for a in self.artifacts if a["tenant_id"] == tenant and a["session_id"] == session
               and a["metadata"].get("mockup_review_id") == review]
        for art in hit:
            art.update(title=title, content=content, metadata={**art["metadata"], **json.loads(meta)})
        return [{"id": a["id"]} for a in hit]

    async def fetchval(self, query, *args):
        self.queries.append(query)
        assert query.startswith("INSERT INTO chat_artifacts")
        if self.fail_insert:
            raise RuntimeError("insert failed")
        tenant, session, workspace, kind, title, content, meta = args
        art = {"id": uuid4(), "tenant_id": tenant, "session_id": session, "workspace_id": workspace, "type": kind,
               "title": title, "content": content, "metadata": json.loads(meta)}
        self.artifacts.append(art)
        return art["id"]


def head(**over):
    row = {"id": REVIEW, "tenant_id": TENANT, "project_key": PROJECT, "title": "로그인 화면", "session_id": SESSION}
    return {**row, **over}


def revision(rid, number):
    return {"id": rid, "revision": number}


def bind(conn, h=None, rev=None, actor="owner", elevated=False):
    return asyncio.run(svc._bind_session_card(conn, h or head(), rev or revision(REV1, 1), actor, elevated))


def test_submit_creates_one_card_with_the_three_binding_keys():
    conn = FakeConn()
    result = bind(conn)
    assert result["action"] == "created" and len(conn.artifacts) == 1
    art = conn.artifacts[0]
    assert art["session_id"] == SESSION and art["tenant_id"] == TENANT and art["workspace_id"] == WORKSPACE
    assert art["type"] == "report" and art["title"].startswith("목업 검토")
    assert art["metadata"] == {"subtype": "mockup_review", "mockup_review_id": str(REVIEW),
                               "mockup_project": PROJECT, "mockup_revision_id": str(REV1)}
    assert str(result["artifact_id"]) == str(art["id"])


def test_repeating_the_same_revision_does_not_duplicate():
    conn = FakeConn()
    first = bind(conn)
    second = bind(conn)
    assert len(conn.artifacts) == 1
    assert second["action"] == "updated" and second["artifact_id"] == first["artifact_id"]
    assert conn.artifacts[0]["metadata"]["mockup_revision_id"] == str(REV1)


def test_new_revision_moves_the_same_card_instead_of_adding_one():
    conn = FakeConn()
    bind(conn)
    bind(conn, rev=revision(REV2, 2))
    assert len(conn.artifacts) == 1
    assert conn.artifacts[0]["metadata"]["mockup_revision_id"] == str(REV2)
    assert "판: 2" in conn.artifacts[0]["content"]


def test_other_reviews_in_the_same_session_get_their_own_card():
    conn = FakeConn()
    bind(conn)
    bind(conn, h=head(id=uuid4()))
    assert len(conn.artifacts) == 2


def test_review_without_session_gets_no_card():
    conn = FakeConn()
    assert bind(conn, h=head(session_id=None)) == {"action": "skipped", "reason": "no_session"}
    assert conn.artifacts == [] and conn.queries == []


def test_deleted_session_is_skipped_not_fatal():
    conn = FakeConn(session=None)
    assert bind(conn) == {"action": "skipped", "reason": "session_not_found"}
    assert conn.artifacts == []


@pytest.mark.parametrize("override", [
    {"tenant_id": OTHER_TENANT},
    {"workspace_tenant": OTHER_TENANT},
    {"project_key": "KIS", "workspace_name": "KIS", "workspace_display_name": "KIS"},
])
def test_session_of_another_tenant_or_project_is_rejected(override):
    base = FakeConn().session
    conn = FakeConn(session={**base, **override})
    with pytest.raises(HTTPException) as exc:
        bind(conn)
    assert exc.value.status_code == 404 and exc.value.detail["code"] == "session_not_found"
    assert conn.artifacts == []


def test_private_session_of_another_user_needs_elevation():
    conn = FakeConn()
    with pytest.raises(HTTPException) as exc:
        bind(conn, actor="someone-else")
    assert exc.value.status_code == 403 and exc.value.detail["code"] == "session_access_denied"
    assert conn.artifacts == []
    assert bind(conn, actor="someone-else", elevated=True)["action"] == "created"


def test_ceo_integrated_workspace_serves_orchestrated_projects():
    base = FakeConn().session
    conn = FakeConn(session={**base, "project_key": "CEO", "workspace_name": svc.CEO_INTEGRATED_NAME})
    assert bind(conn)["action"] == "created"


def test_failed_write_propagates_so_the_caller_rolls_back():
    conn = FakeConn(fail_insert=True)
    with pytest.raises(RuntimeError):
        bind(conn)
    assert conn.artifacts == []


def test_card_keys_come_from_the_locked_head_not_the_caller():
    conn = FakeConn()
    bind(conn, h=head(project_key=PROJECT), rev=revision(REV1, 1))
    meta = conn.artifacts[0]["metadata"]
    assert UUID(meta["mockup_review_id"]) == REVIEW and meta["mockup_project"] == PROJECT


def test_submit_and_new_revision_both_bind_the_card_before_the_event():
    import inspect
    for fn in (svc.submit_review, svc.create_revision):
        source = inspect.getsource(fn)
        assert source.index("_bind_session_card") < source.rindex("_event(") and "elevated" in inspect.signature(fn).parameters
