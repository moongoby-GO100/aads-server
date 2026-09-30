"""오비서 신규 가입 500 두 건 회귀 테스트 (2026-10-01).

1. obys.register_tenant_slug_collision — slug 중복 검사가 삭제된 조직을 빼고 봐서
   UNIQUE 제약(삭제 포함)과 어긋났다. 한글 매장명은 "tenant" 로 바뀌어 삭제된
   tenant-N 과 부딪혔다.
2. obys.login_chat_workspaces_missing — 오비서 단독 DB 에는 chat_workspaces 가 없어
   로그인 때 기본 작업공간 생성이 500 을 냈다.
"""
import os

os.environ.setdefault("JWT_SECRET_KEY", "unit-test-secret")

import asyncio

import app.auth as auth_module


class _Tx:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


class _Acquire:
    def __init__(self, conn):
        self.conn = conn

    async def __aenter__(self):
        return self.conn

    async def __aexit__(self, *exc):
        return False


class _Pool:
    def __init__(self, conn):
        self.conn = conn

    def acquire(self):
        return _Acquire(self.conn)


class SlugConn:
    """tenants 에 삭제된 행까지 포함한 slug 집합을 들고 있는 가짜 연결."""

    def __init__(self, taken_slugs, has_chat_workspaces=True):
        self.taken = set(taken_slugs)
        self.has_chat_workspaces = has_chat_workspaces
        self.slug_queries = []
        self.inserted_slug = None
        self.workspace_insert = False

    def transaction(self):
        return _Tx()

    async def fetchval(self, query, *args):
        if "FROM saas_users" in query:
            return True
        if "FROM tenants WHERE slug" in query:
            self.slug_queries.append(query)
            return args[0] in self.taken
        if "to_regclass('public.chat_workspaces')" in query:
            return self.has_chat_workspaces
        raise AssertionError(f"unexpected fetchval: {query}")

    async def fetchrow(self, query, *args):
        if "INSERT INTO tenants" in query:
            self.inserted_slug = args[0]
            return {"tenant_id": "11111111-1111-1111-1111-111111111111", "slug": args[0],
                    "name": args[1], "kind": "customer", "status": "active",
                    "metadata": {}, "created_at": None}
        if "INSERT INTO tenant_memberships" in query:
            return {"membership_id": "m1", "role": "owner", "status": "active"}
        if "FROM tenants" in query:
            return {"tenant_id": args[0], "name": "매장", "kind": "customer"}
        if "FROM chat_workspaces" in query:
            if not self.has_chat_workspaces:
                raise AssertionError("chat_workspaces queried although table is missing")
            return None
        if "INSERT INTO chat_workspaces" in query:
            self.workspace_insert = True
            return {"id": "w1", "name": args[1]}
        raise AssertionError(f"unexpected fetchrow: {query}")

    async def execute(self, query, *args):
        return "OK"


def _run(conn, monkeypatch, name):
    async def _ready():
        return None

    async def _pool():
        return _Pool(conn)

    monkeypatch.setattr(auth_module, "require_saas_schema_ready", _ready)
    monkeypatch.setattr(auth_module, "_ensure_pool", _pool)
    return asyncio.run(auth_module.create_tenant_for_user(user_id="u1", name=name))


def test_slug_check_includes_deleted_tenants(monkeypatch):
    # "store" 는 삭제된 조직이 쓰고 있다 — 검사가 삭제 행까지 봐야 store-2 로 비껴간다.
    conn = SlugConn({"store"})
    out = _run(conn, monkeypatch, "Store")
    assert out["slug"] == "store-2"
    assert all("deleted_at" not in q for q in conn.slug_queries)


def test_hangul_name_gets_random_suffix_not_bare_tenant(monkeypatch):
    taken = {"tenant"} | {f"tenant-{i}" for i in range(2, 60)}
    conn = SlugConn(taken)
    out = _run(conn, monkeypatch, "열정매장")
    assert out["slug"].startswith("tenant-")
    assert out["slug"] not in taken
    assert len(conn.slug_queries) == 1


def test_signup_without_chat_workspaces_table_skips_workspace(monkeypatch):
    conn = SlugConn(set(), has_chat_workspaces=False)
    out = _run(conn, monkeypatch, "열정매장")
    assert out["workspace"] is None
    assert conn.workspace_insert is False


def test_workspace_still_created_when_table_exists(monkeypatch):
    conn = SlugConn(set(), has_chat_workspaces=True)
    out = _run(conn, monkeypatch, "Store")
    assert out["workspace"] == {"id": "w1", "name": "[WORK] Store"}
    assert conn.workspace_insert is True
