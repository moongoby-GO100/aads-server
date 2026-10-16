"""Mockup review list API: filter arguments, limit clamp, tenant/project isolation, link format (no DB)."""
import asyncio
from datetime import datetime, timezone
from uuid import uuid4

from app.api import mockup_reviews as api
from app.services import mockup_review_service as svc

TENANT = str(uuid4())
NOW = datetime(2026, 10, 16, 1, 0, tzinfo=timezone.utc)


def _head(project, **extra):
    row = {"id": uuid4(), "project_key": project, "tenant_id": TENANT, "title": f"{project} mock",
           "status": "approved", "change_type": "new", "goal_id": None, "session_id": None,
           "latest_revision_id": uuid4(), "approved_revision_id": None, "created_at": NOW, "updated_at": NOW,
           "latest_revision": 2, "asset_count": 3}
    row.update(extra)
    return row


class _Conn:
    """Applies the tenant/project filter the SQL expresses so isolation is checked on the bound arguments."""

    def __init__(self, heads):
        self.heads = heads
        self.fetch_calls = []
        self.fetchval_calls = []

    def _match(self, tenant, project, status, session_id, goal_id):
        return [h for h in self.heads if h["tenant_id"] == tenant and h["project_key"] == project
                and (status is None or h["status"] == status)
                and (session_id is None or h["session_id"] == session_id)
                and (goal_id is None or h["goal_id"] == goal_id)]

    async def fetch(self, sql, *args):
        self.fetch_calls.append((sql, args))
        return self._match(*args[:5])[args[6]:args[6] + args[5]]

    async def fetchval(self, sql, *args):
        self.fetchval_calls.append((sql, args))
        return len(self._match(*args[:5]))


def run(conn, project="AADS", **kwargs):
    return asyncio.run(svc.list_reviews(conn, TENANT, project, **kwargs))


def test_filter_arguments_and_sql_shape():
    sid, gid = uuid4(), uuid4()
    conn = _Conn([_head("AADS", session_id=sid, goal_id=gid)])
    out = run(conn, status="approved", session_id=sid, goal_id=gid, limit=10, offset=5)
    sql, args = conn.fetch_calls[0]
    assert args == (TENANT, "AADS", "approved", sid, gid, 10, 5)
    assert "h.tenant_id=$1::uuid AND h.project_key=$2" in sql
    assert "ORDER BY h.updated_at DESC,h.id DESC" in sql
    assert conn.fetchval_calls[0][1] == (TENANT, "AADS", "approved", sid, gid)
    assert out["total"] == 1 and out["items"] == []


def test_null_filters_are_passed_as_none():
    conn = _Conn([_head("AADS")])
    out = run(conn)
    assert conn.fetch_calls[0][1] == (TENANT, "AADS", None, None, None, 50, 0)
    assert len(out["items"]) == 1 and out["total"] == 1


def test_limit_is_clamped():
    conn = _Conn([])
    run(conn, limit=0)
    run(conn, limit=10_000)
    run(conn, limit=-5, offset=-3)
    assert [c[1][5] for c in conn.fetch_calls] == [1, 200, 1]
    assert conn.fetch_calls[2][1][6] == 0


def test_other_project_and_tenant_rows_are_excluded():
    other_tenant = _head("AADS", tenant_id=str(uuid4()))
    conn = _Conn([_head("AADS"), _head("KIS"), other_tenant])
    out = run(conn, "AADS")
    assert out["total"] == 1
    assert [i["title"] for i in out["items"]] == ["AADS mock"]


def test_link_and_item_fields():
    head = _head("AADS", goal_id=uuid4(), session_id=uuid4())
    item = run(_Conn([head]))["items"][0]
    assert item["link"] == f"/chat?mockup_review={head['id']}&mockup_project=AADS&mockup_revision={head['latest_revision_id']}"
    assert item["review_id"] == str(head["id"])
    assert item["session_id"] == str(head["session_id"]) and item["goal_id"] == str(head["goal_id"])
    assert item["latest_revision"] == 2 and item["asset_count"] == 3
    assert item["created_at"] == NOW.isoformat() and item["approved_revision_id"] is None


def test_link_without_revision_omits_revision_key():
    item = run(_Conn([_head("AADS", latest_revision_id=None, latest_revision=None, asset_count=0)]))["items"][0]
    assert "mockup_revision" not in item["link"] and item["latest_revision_id"] is None


def test_list_route_does_not_shadow_other_routes():
    routes = {(m, r.path): r for r in api.router.routes for m in getattr(r, "methods", ())}
    base = "/projects/{project_key}/mockup-reviews"
    assert ("GET", base) in routes
    assert routes[("GET", base)].endpoint is api.list_reviews
    assert ("GET", base + "/notifications") in routes
    assert ("GET", base + "/{review_id}") in routes
    paths = [r.path for r in api.router.routes if "GET" in getattr(r, "methods", ())]
    assert paths.index(base + "/notifications") < paths.index(base + "/{review_id}")
