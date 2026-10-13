"""구독계정 수동 켜기/끄기 — CEO 가 끈 계정은 자동 복구가 다시 켜지 않는다.

16:23 에 끈 id=2 가 34초 뒤 되살아났다. rate_limit_recovery 가 "is_active=false 이고
rate_limited_until 이 비어 있는" 키를 전부 되켜기 때문이다. 이 파일은 그 경로를
실제 main.py 의 SQL 로 굴려 본다.
"""
import ast
import asyncio
import sys
import types
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from fastapi import HTTPException

from app.api import llm_keys
from scripts import claude_relay_server as relay

ROOT = Path(__file__).resolve().parents[2]
FAR = datetime(2100, 1, 1, tzinfo=timezone.utc)


class _Tx:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *_):
        return False


class FakeDb:
    """llm_api_keys 한 테이블을 메모리로 흉내 낸다. 질의 문구를 보고 동작하므로
    SQL 에서 manual_disabled 조건을 빼면 이 가짜도 같은 방식으로 되켠다."""

    def __init__(self, rows):
        self.rows = {r["id"]: dict(r) for r in rows}
        self.audit = []

    def transaction(self):
        return _Tx()

    def acquire(self):
        outer = self

        class _Acq:
            async def __aenter__(self):
                return outer

            async def __aexit__(self, *_):
                return False

        return _Acq()

    async def fetchval(self, *_a):
        return 0

    async def fetchrow(self, query, *args):
        q = " ".join(query.split())
        if q.startswith("SELECT id, provider, key_name, priority, is_active, manual_disabled"):
            return self.rows.get(args[0])
        if "FROM llm_api_keys WHERE provider = $1 AND priority = $2" in q:
            for r in self.rows.values():
                if (r["provider"] == args[0] and r["priority"] == args[1]
                        and r["is_active"] and r["id"] != args[2]):
                    return r
            return None
        if q.startswith("UPDATE llm_api_keys SET is_active = TRUE, manual_disabled = FALSE"):
            r = self.rows[args[0]]
            r.update(is_active=True, manual_disabled=False, manual_disabled_at=None,
                     manual_disabled_by=None, rate_limited_until=None)
            return r
        if q.startswith("UPDATE llm_api_keys SET is_active = FALSE, manual_disabled = TRUE"):
            r = self.rows[args[0]]
            r.update(is_active=False, manual_disabled=True,
                     manual_disabled_at=datetime.now(timezone.utc), manual_disabled_by=args[1])
            return r
        raise AssertionError("unexpected fetchrow: " + q[:120])

    async def fetch(self, query, *_args):
        q = " ".join(query.split())
        out = []
        now = datetime.now(timezone.utc)
        if q.startswith("UPDATE llm_api_keys SET rate_limited_until = NULL"):
            for r in self.rows.values():
                if r["rate_limited_until"] and r["rate_limited_until"] <= now:
                    r["rate_limited_until"] = None
                    out.append(r)
        elif q.startswith("UPDATE llm_api_keys SET is_active = TRUE"):
            guard = "manual_disabled = FALSE" in q
            for r in self.rows.values():
                if r["is_active"]:
                    continue
                if guard and r["manual_disabled"]:
                    continue
                if r["rate_limited_until"] and r["rate_limited_until"] > now:
                    continue
                r["is_active"] = True
                out.append(r)
        else:
            raise AssertionError("unexpected fetch: " + q[:120])
        return out


def _rows():
    return [
        {"id": 1, "provider": "anthropic", "key_name": "ANTHROPIC_AUTH_TOKEN", "priority": 1,
         "is_active": True, "manual_disabled": False, "manual_disabled_at": None,
         "manual_disabled_by": None, "rate_limited_until": None, "encrypted_value": "x"},
        {"id": 2, "provider": "anthropic", "key_name": "ANTHROPIC_AUTH_TOKEN_2", "priority": 2,
         "is_active": True, "manual_disabled": False, "manual_disabled_at": None,
         "manual_disabled_by": None, "rate_limited_until": None, "encrypted_value": "x"},
    ]


@pytest.fixture
def db(monkeypatch):
    fake = FakeDb(_rows())

    async def audit(conn, **kw):
        fake.audit.append(kw)

    async def sync(_reason):
        return {"ok": True}

    monkeypatch.setattr(llm_keys, "get_pool", lambda: fake)
    monkeypatch.setattr(llm_keys, "append_key_audit_log", audit)
    monkeypatch.setattr(llm_keys, "_run_registry_sync", sync)
    monkeypatch.setattr(llm_keys, "invalidate_key_cache", lambda *_: None)
    return fake


def _load_recovery(monkeypatch, fake):
    """main.py 안쪽에 정의된 실제 _run_rate_limit_recovery 를 꺼내 가짜 풀로 한 번 돌린다."""
    tree = ast.parse((ROOT / "app" / "main.py").read_text(encoding="utf-8"))
    node = next(n for n in ast.walk(tree)
                if isinstance(n, ast.AsyncFunctionDef) and n.name == "_run_rate_limit_recovery")
    module = ast.Module(body=[node], type_ignores=[])
    ns = {"logger": types.SimpleNamespace(info=lambda *a, **k: None, warning=lambda *a, **k: None)}
    exec(compile(module, "app/main.py", "exec"), ns)

    db_pool = types.ModuleType("app.core.db_pool")
    db_pool.get_pool = lambda: fake
    monkeypatch.setitem(sys.modules, "app.core.db_pool", db_pool)
    return ns["_run_rate_limit_recovery"]


def test_manual_off_survives_recovery_loop(db, monkeypatch):
    result = asyncio.run(llm_keys.set_llm_key_enabled(2, llm_keys.LlmKeyEnabled(enabled=False)))
    assert result["is_active"] is False and result["manual_disabled"] is True
    assert db.rows[2]["manual_disabled_by"] == "ceo-settings"
    assert db.audit[-1]["event_type"] == "manual_disable"

    recovery = _load_recovery(monkeypatch, db)
    asyncio.run(recovery())
    assert db.rows[2]["is_active"] is False
    assert db.rows[1]["is_active"] is True


def test_recovery_still_revives_non_manual_inactive_keys(db, monkeypatch):
    db.rows[2]["is_active"] = False
    recovery = _load_recovery(monkeypatch, db)
    asyncio.run(recovery())
    assert db.rows[2]["is_active"] is True


def test_manual_on_clears_hold_and_flag(db):
    asyncio.run(llm_keys.set_llm_key_enabled(2, llm_keys.LlmKeyEnabled(enabled=False)))
    db.rows[2]["rate_limited_until"] = FAR

    result = asyncio.run(llm_keys.set_llm_key_enabled(
        2, llm_keys.LlmKeyEnabled(enabled=True, reason="복구")))
    assert result["is_active"] is True
    assert result["manual_disabled"] is False
    assert db.rows[2]["rate_limited_until"] is None
    assert db.audit[-1]["event_type"] == "manual_enable"
    assert db.audit[-1]["details"]["reason"] == "복구"


def test_manual_on_priority_conflict_is_409(db):
    db.rows[2]["priority"] = 1
    db.rows[2]["is_active"] = False
    db.rows[2]["manual_disabled"] = True
    with pytest.raises(HTTPException) as exc:
        asyncio.run(llm_keys.set_llm_key_enabled(2, llm_keys.LlmKeyEnabled(enabled=True)))
    assert exc.value.status_code == 409
    assert db.rows[2]["manual_disabled"] is True


def test_unknown_key_is_404(db):
    with pytest.raises(HTTPException) as exc:
        asyncio.run(llm_keys.set_llm_key_enabled(99, llm_keys.LlmKeyEnabled(enabled=False)))
    assert exc.value.status_code == 404


def test_put_and_activate_cannot_override_manual_off(monkeypatch):
    class Conn(FakeDb):
        async def fetchrow(self, query, *args):
            return {"id": 2, "provider": "anthropic", "key_name": "K", "label": "", "priority": 2,
                    "is_active": False, "notes": "", "manual_disabled": True}

    conn = Conn([])
    monkeypatch.setattr(llm_keys, "get_pool", lambda: conn)
    with pytest.raises(HTTPException) as put_exc:
        asyncio.run(llm_keys.update_llm_key(2, llm_keys.LlmKeyUpdate(is_active=True)))
    assert put_exc.value.status_code == 409
    with pytest.raises(HTTPException) as act_exc:
        asyncio.run(llm_keys.activate_llm_key(2))
    assert act_exc.value.status_code == 409


def test_automatic_activation_paths_respect_manual_disabled():
    keys_src = (ROOT / "app" / "api" / "llm_keys.py").read_text(encoding="utf-8")
    assert "SET is_active = NOT manual_disabled, rate_limited_until = NULL" in keys_src
    provider_src = (ROOT / "app" / "core" / "llm_key_provider.py").read_text(encoding="utf-8")
    assert "is_active = NOT llm_api_keys.manual_disabled" in provider_src
    assert "is_active = TRUE," not in provider_src.split("async def store_api_key")[1]


def test_migration_is_additive_and_idempotent():
    sql = (ROOT / "migrations" / "20261012_llm_api_keys_manual_disabled.sql").read_text(encoding="utf-8")
    code = "\n".join(l for l in sql.splitlines() if not l.strip().startswith("--")).upper()
    assert "ADD COLUMN IF NOT EXISTS MANUAL_DISABLED BOOLEAN NOT NULL DEFAULT FALSE" in code
    assert "ADD COLUMN IF NOT EXISTS MANUAL_DISABLED_AT TIMESTAMPTZ" in code
    assert "ADD COLUMN IF NOT EXISTS MANUAL_DISABLED_BY TEXT" in code
    for forbidden in ("UPDATE ", "DELETE", "DROP", "TRUNCATE"):
        assert forbidden not in code


def test_overview_exposes_manual_disabled(monkeypatch):
    now = datetime.now(timezone.utc)

    def row(**kw):
        base = {"id": 1, "provider": "anthropic", "key_name": "ANTHROPIC_AUTH_TOKEN",
                "encrypted_value": "x", "label": "a", "priority": 1, "is_active": True,
                "rate_limited_until": None, "notes": "", "manual_disabled": False,
                "manual_disabled_at": None, "used_percent": None, "resets_at": None,
                "snapshot_at": None, "ok_72h": 0, "limit_72h": 0, "collected_at": None}
        base.update(kw)
        return base

    stamp = now - timedelta(minutes=5)
    rows = [
        row(),
        row(id=2, key_name="ANTHROPIC_AUTH_TOKEN_2", priority=2, is_active=False,
            manual_disabled=True, manual_disabled_at=stamp, rate_limited_until=FAR),
    ]

    class Conn:
        async def fetch(self, *_a):
            return rows

    class Pool:
        def acquire(self):
            class A:
                async def __aenter__(s):
                    return Conn()

                async def __aexit__(s, *_):
                    return False

            return A()

    async def no_relay(*_a, **_k):
        raise HTTPException(status_code=502, detail="relay down")

    async def no_clear():
        return {}

    import app.services.model_registry as registry

    monkeypatch.setattr(llm_keys, "get_pool", lambda: Pool())
    monkeypatch.setattr(llm_keys, "_relay_call", no_relay)
    monkeypatch.setattr(llm_keys, "decrypt_value", lambda _v: "sk-ant-oat01-abcdef")
    monkeypatch.setattr(registry, "clear_expired_rate_limits", no_clear)

    overview = asyncio.run(llm_keys.llm_overview())
    by_name = {a["key_name"]: a for a in overview["accounts"]}
    on, off = by_name["ANTHROPIC_AUTH_TOKEN"], by_name["ANTHROPIC_AUTH_TOKEN_2"]
    assert on["manual_disabled"] is False and on["manual_disabled_at"] is None
    assert off["manual_disabled"] is True
    assert off["manual_disabled_at"] == stamp.isoformat()
    assert off["state"] == "inactive"
    assert off["is_active"] is False


def _relay_with_credentials(monkeypatch, disabled):
    monkeypatch.setattr(
        relay, "_read_oauth_tokens", lambda: ("tok-1", "tok-2", "1", "acct1", "acct2"))
    relay._EXTRA_SLOT_AUTH.clear()
    monkeypatch.setattr(relay, "_last_429_slot", 0, raising=False)
    monkeypatch.setattr(relay, "_DB_DISABLED_SLOTS", set(disabled))
    monkeypatch.setattr(relay, "_slot_auth_status", lambda slot, env_fallback_available=False: {
        "status": "ok", "refresh_capable": True})


def test_relay_skips_db_disabled_slot_even_with_credentials(monkeypatch):
    _relay_with_credentials(monkeypatch, {"1"})
    assert relay._pick_auth()["slot"] == "2"
    assert relay._pick_auth(preferred_slot="1")["slot"] in ("2", "0")


def test_relay_fallback_never_returns_disabled_slot(monkeypatch):
    _relay_with_credentials(monkeypatch, {"2"})
    monkeypatch.setattr(relay, "_last_429_slot", 1, raising=False)
    assert relay._pick_auth()["slot"] == "1"


def test_relay_returns_none_when_every_slot_disabled(monkeypatch):
    _relay_with_credentials(monkeypatch, {"1", "2"})
    auth = relay._pick_auth()
    assert auth["slot"] == "0" and auth["source"] == "none"


def test_relay_without_disabled_info_behaves_as_before(monkeypatch):
    _relay_with_credentials(monkeypatch, set())
    assert relay._pick_auth()["slot"] == "1"


def test_relay_parses_disabled_slots_payload():
    assert relay._parse_disabled_slots({"disabled_slots": ["2", 4, ""]}) == {"2", "4"}
    assert relay._parse_disabled_slots({"disabled_slots": []}) == set()
    assert relay._parse_disabled_slots({"keys": []}) is None
    assert relay._parse_disabled_slots(None) is None


def test_disabled_slots_lookup_ignores_slot_that_still_has_active_key(monkeypatch):
    from app.core import auth_provider

    class Conn:
        async def fetch(self, *_a):
            return [
                {"key_name": "ANTHROPIC_AUTH_TOKEN", "is_active": True},
                {"key_name": "env_oauth_1", "is_active": False},
                {"key_name": "ANTHROPIC_AUTH_TOKEN_2", "is_active": False},
                {"key_name": "unmapped", "is_active": False},
            ]

    class Pool:
        def acquire(self):
            class A:
                async def __aenter__(s):
                    return Conn()

                async def __aexit__(s, *_):
                    return False

            return A()

    db_pool = types.ModuleType("app.core.db_pool")
    db_pool.get_pool = lambda: Pool()
    monkeypatch.setitem(sys.modules, "app.core.db_pool", db_pool)
    assert asyncio.run(auth_provider.get_disabled_anthropic_slots()) == ["2"]


def test_model_selector_drops_disabled_slot_from_candidates(monkeypatch):
    from app.services import model_selector

    async def records(include_rate_limited=True):
        return [
            {"key_name": "ANTHROPIC_AUTH_TOKEN", "slot": "1", "priority": 1},
            {"key_name": "ANTHROPIC_AUTH_TOKEN_2", "slot": "2", "priority": 2},
        ]

    async def disabled():
        return ["2"]

    monkeypatch.setattr(model_selector, "_ap_get_key_records_async", records)
    monkeypatch.setattr(model_selector, "_ap_get_disabled_slots", disabled)
    slots = asyncio.run(model_selector._get_claude_slot_records())
    assert set(slots) == {"1"}


# ── 재작업 1: 리뷰 반려 지적 4건 ───────────────────────────────────────────────


def _fake_urlopen(payload):
    class Resp:
        def __enter__(self):
            return self

        def __exit__(self, *_):
            return False

        def read(self):
            import json
            return json.dumps(payload).encode()

    return lambda *_a, **_k: Resp()


@pytest.fixture
def relay_state(monkeypatch, tmp_path):
    monkeypatch.setattr(relay, "_OAUTH_STATE_DISK_CACHE", tmp_path / "oauth-state.json")
    monkeypatch.setattr(relay, "_DB_OAUTH_CACHE", {"rows": None, "ts": 0})
    monkeypatch.setattr(relay, "_DB_DISABLED_SLOTS", set())
    monkeypatch.setattr(relay, "_oauth_state_urls", lambda: ["http://x/oauth-state"])
    monkeypatch.setattr(relay, "_load_relay_secret", lambda: "")
    return relay


def test_relay_accepts_empty_keys_when_every_account_is_disabled(relay_state, monkeypatch):
    """지적 1: keys=[] 이어도 disabled_slots 가 있으면 정상 응답이다."""
    monkeypatch.setattr(relay.urllib_request, "urlopen",
                        _fake_urlopen({"keys": [], "disabled_slots": ["1", "2"]}))
    assert relay._read_db_oauth_rows() == []
    assert relay._DB_DISABLED_SLOTS == {"1", "2"}


def test_relay_disk_cache_keeps_disabled_slots_when_keys_empty(relay_state, monkeypatch):
    monkeypatch.setattr(relay.urllib_request, "urlopen",
                        _fake_urlopen({"keys": [], "disabled_slots": ["1", "2"]}))
    relay._read_db_oauth_rows()

    relay._DB_DISABLED_SLOTS.clear()
    relay._DB_OAUTH_CACHE.update(rows=None, ts=0)

    def boom(*_a, **_k):
        raise relay.urllib_error.URLError("down")

    monkeypatch.setattr(relay.urllib_request, "urlopen", boom)
    assert relay._read_db_oauth_rows() == []
    assert relay._DB_DISABLED_SLOTS == {"1", "2"}


def test_relay_empty_keys_without_block_list_is_still_a_failure(relay_state, monkeypatch):
    monkeypatch.setattr(relay.urllib_request, "urlopen",
                        _fake_urlopen({"keys": [], "disabled_slots": []}))
    assert relay._read_db_oauth_rows() is None


def test_relay_keeps_block_list_when_response_has_no_field(relay_state, monkeypatch):
    """지적 4: API 가 DB 조회 실패로 disabled_slots 를 생략하면 기존 차단을 지우지 않는다."""
    relay._DB_DISABLED_SLOTS.update({"2"})
    rows = [{"slot": "1", "value": "v", "label": "a", "priority": 1}]
    monkeypatch.setattr(relay.urllib_request, "urlopen", _fake_urlopen({"keys": rows}))
    assert relay._read_db_oauth_rows() == rows
    assert relay._DB_DISABLED_SLOTS == {"2"}


def test_relay_pick_auth_with_all_accounts_off_uses_no_local_credentials(relay_state, monkeypatch):
    monkeypatch.setattr(relay.urllib_request, "urlopen",
                        _fake_urlopen({"keys": [], "disabled_slots": ["1", "2"]}))
    monkeypatch.setattr(relay, "_ENV_OAUTH_FILE", Path("/nonexistent/env-oauth"))
    monkeypatch.setattr(relay, "_last_429_slot", 0, raising=False)
    monkeypatch.setattr(relay, "_slot_auth_status", lambda slot, env_fallback_available=False: {
        "status": "ok", "refresh_capable": True})
    assert relay._pick_auth()["source"] == "none"


def _auth_provider_with(monkeypatch, *, active_rows, all_rows, db_ok=True):
    from app.core import auth_provider

    class Conn:
        async def fetch(self, *_a):
            if not db_ok:
                raise RuntimeError("db down")
            return all_rows

    class Pool:
        def acquire(self):
            class A:
                async def __aenter__(s):
                    return Conn()

                async def __aexit__(s, *_):
                    return False

            return A()

    db_pool = types.ModuleType("app.core.db_pool")
    db_pool.get_pool = lambda: Pool()
    monkeypatch.setitem(sys.modules, "app.core.db_pool", db_pool)

    import app.core.llm_key_provider as provider

    async def records(_provider, include_rate_limited=True):
        return list(active_rows)

    monkeypatch.setattr(provider, "get_provider_key_records", records)
    monkeypatch.setattr(auth_provider, "_TOKEN_PRIMARY", "env-token-1")
    monkeypatch.setattr(auth_provider, "_TOKEN_FALLBACK", "env-token-2")
    monkeypatch.setattr(auth_provider, "_LAST_KNOWN_DISABLED_SLOTS", set())
    return auth_provider


def test_env_tokens_not_returned_for_slots_turned_off_in_db(monkeypatch):
    """지적 2: 활성 DB 계정이 0개여도 꺼진 슬롯의 env 토큰은 나가지 않는다."""
    ap = _auth_provider_with(monkeypatch, active_rows=[], all_rows=[
        {"key_name": "ANTHROPIC_AUTH_TOKEN", "is_active": False},
        {"key_name": "ANTHROPIC_AUTH_TOKEN_2", "is_active": False},
    ])
    assert asyncio.run(ap.get_oauth_tokens_async()) == []
    assert asyncio.run(ap.get_oauth_key_records_async()) == []


def test_env_tokens_only_for_slots_still_on(monkeypatch):
    ap = _auth_provider_with(monkeypatch, active_rows=[], all_rows=[
        {"key_name": "ANTHROPIC_AUTH_TOKEN_2", "is_active": False},
    ])
    slots = [r["slot"] for r in asyncio.run(ap.get_oauth_key_records_async())]
    assert slots == ["1"]


def test_env_fallback_unchanged_when_nothing_is_disabled(monkeypatch):
    ap = _auth_provider_with(monkeypatch, active_rows=[], all_rows=[])
    assert len(asyncio.run(ap.get_oauth_tokens_async())) == 2


def test_db_failure_reports_unknown_and_reuses_last_known_block(monkeypatch):
    """지적 4: 조회 실패는 None(알 수 없음). 앞서 읽은 차단 목록을 잊지 않는다."""
    ap = _auth_provider_with(monkeypatch, active_rows=[], all_rows=[
        {"key_name": "ANTHROPIC_AUTH_TOKEN_2", "is_active": False},
    ])
    assert asyncio.run(ap.query_disabled_anthropic_slots()) == ["2"]

    ap = _auth_provider_with(monkeypatch, active_rows=[], all_rows=[], db_ok=False)
    ap._LAST_KNOWN_DISABLED_SLOTS = {"2"}
    assert asyncio.run(ap.query_disabled_anthropic_slots()) is None
    assert asyncio.run(ap.get_disabled_anthropic_slots()) == ["2"]
    assert [r["slot"] for r in asyncio.run(ap.get_oauth_key_records_async())] == ["1"]


def test_oauth_state_endpoint_omits_field_when_lookup_failed(monkeypatch):
    from app.api import health
    from app.core import auth_provider

    async def records(include_rate_limited=True):
        return []

    async def unknown():
        return None

    monkeypatch.setattr(health, "_require_relay_secret", lambda _r: None)
    monkeypatch.setattr(auth_provider, "get_oauth_key_records_async", records)
    monkeypatch.setattr(auth_provider, "query_disabled_anthropic_slots", unknown)
    assert "disabled_slots" not in asyncio.run(health.claude_relay_oauth_state(None))

    async def known():
        return ["1"]

    monkeypatch.setattr(auth_provider, "query_disabled_anthropic_slots", known)
    assert asyncio.run(health.claude_relay_oauth_state(None))["disabled_slots"] == ["1"]


def test_put_and_activate_lock_the_row_before_checking_manual_disabled(monkeypatch):
    """지적 3: 검사와 UPDATE 사이에 /enabled 가 끼어들 수 없도록 행을 잠근다."""
    seen = []

    class Conn(FakeDb):
        async def fetchrow(self, query, *args):
            seen.append(" ".join(query.split()))
            return {"id": 2, "provider": "anthropic", "key_name": "K", "label": "", "priority": 2,
                    "is_active": False, "notes": "", "manual_disabled": True}

    monkeypatch.setattr(llm_keys, "get_pool", lambda: Conn([]))
    for call in (
        lambda: llm_keys.update_llm_key(2, llm_keys.LlmKeyUpdate(is_active=True)),
        lambda: llm_keys.activate_llm_key(2),
    ):
        seen.clear()
        with pytest.raises(HTTPException):
            asyncio.run(call())
        assert seen and seen[0].endswith("FOR UPDATE"), seen[0]


# ── 재작업 2: 리뷰 반려 지적 2건 ───────────────────────────────────────────────


class _AuthDb(FakeDb):
    """llm_keys 엔드포인트와 auth_provider 조회가 같은 행을 보게 하는 가짜."""

    async def fetch(self, query, *args):
        q = " ".join(query.split())
        if q.startswith("SELECT key_name, is_active FROM llm_api_keys"):
            return [{"key_name": r["key_name"], "is_active": r["is_active"]} for r in self.rows.values()]
        return await super().fetch(query, *args)


def _auth_state_with_db(monkeypatch, db):
    from app.core import auth_provider
    import app.core.llm_key_provider as provider

    db_pool = types.ModuleType("app.core.db_pool")
    db_pool.get_pool = lambda: db
    monkeypatch.setitem(sys.modules, "app.core.db_pool", db_pool)

    async def records(_provider, include_rate_limited=True):
        return [
            {"key_name": r["key_name"], "label": r["key_name"], "priority": r["priority"],
             "value": "tok-%d" % r["id"], "source": "db"}
            for r in sorted(db.rows.values(), key=lambda r: r["priority"]) if r["is_active"]
        ]

    monkeypatch.setattr(provider, "get_provider_key_records", records)
    monkeypatch.setattr(auth_provider, "_LAST_KNOWN_DISABLED_SLOTS", set())
    # 끄기 전에 이미 한 번 채워진 정렬 토큰 캐시를 재현한다.
    asyncio.run(auth_provider.get_oauth_key_records_async())
    assert auth_provider.get_oauth_tokens() == ["tok-1", "tok-2"]
    return auth_provider


def test_toggle_refreshes_auth_provider_token_cache_immediately(monkeypatch):
    """지적 2: 끄기/켜기 직후 sync get_oauth_tokens 결과에서 꺼진 슬롯이 빠지고 되돌아온다."""
    fake = _AuthDb(_rows())

    async def audit(conn, **kw):
        pass

    async def sync(_reason):
        return {"ok": True}

    monkeypatch.setattr(llm_keys, "get_pool", lambda: fake)
    monkeypatch.setattr(llm_keys, "append_key_audit_log", audit)
    monkeypatch.setattr(llm_keys, "_run_registry_sync", sync)
    monkeypatch.setattr(llm_keys, "invalidate_key_cache", lambda *_: None)
    ap = _auth_state_with_db(monkeypatch, fake)

    asyncio.run(llm_keys.set_llm_key_enabled(1, llm_keys.LlmKeyEnabled(enabled=False)))
    assert ap.get_oauth_tokens() == ["tok-2"]
    assert ap.get_primary_token() == "tok-2"
    assert ap.get_available_tokens() == ["tok-2"]

    asyncio.run(llm_keys.set_llm_key_enabled(1, llm_keys.LlmKeyEnabled(enabled=True)))
    assert ap.get_oauth_tokens() == ["tok-1", "tok-2"]


def test_sync_getters_filter_disabled_slot_even_if_cache_is_stale(monkeypatch):
    """갱신이 실패해도(마지막으로 안 꺼진 슬롯 기준) sync getter 는 꺼진 슬롯을 내주지 않는다."""
    fake = _AuthDb(_rows())
    ap = _auth_state_with_db(monkeypatch, fake)
    monkeypatch.setattr(ap, "_LAST_KNOWN_DISABLED_SLOTS", {"1"})
    assert ap.get_oauth_tokens() == ["tok-2"]
    assert ap.get_fallback_token() == ""
    assert ap.has_valid_token() is True
    monkeypatch.setattr(ap, "_LAST_KNOWN_DISABLED_SLOTS", {"1", "2"})
    assert ap.get_oauth_tokens() == [] and ap.has_valid_token() is False


def test_toggle_survives_token_cache_refresh_failure(monkeypatch):
    fake = _AuthDb(_rows())

    async def audit(conn, **kw):
        pass

    async def sync(_reason):
        return {"ok": True}

    async def boom():
        raise RuntimeError("refresh failed")

    monkeypatch.setattr(llm_keys, "get_pool", lambda: fake)
    monkeypatch.setattr(llm_keys, "append_key_audit_log", audit)
    monkeypatch.setattr(llm_keys, "_run_registry_sync", sync)
    monkeypatch.setattr(llm_keys, "invalidate_key_cache", lambda *_: None)
    from app.core import auth_provider
    monkeypatch.setattr(auth_provider, "refresh_oauth_token_state", boom)
    result = asyncio.run(llm_keys.set_llm_key_enabled(1, llm_keys.LlmKeyEnabled(enabled=False)))
    assert result["is_active"] is False


def _sdk_fallback_env(monkeypatch, disabled):
    from app.core import auth_provider
    from app.services import model_selector

    async def get_disabled():
        return list(disabled)

    monkeypatch.setattr(auth_provider, "get_disabled_anthropic_slots", get_disabled)
    monkeypatch.setattr(model_selector, "_ap_get_disabled_slots", get_disabled)
    return model_selector


def test_sdk_fallback_blocked_when_slot1_off(monkeypatch):
    """지적 1: 슬롯 1 이 꺼져 있으면 SDK 폴백은 컨테이너 env 토큰(슬롯 1)으로 돌지 않는다."""
    model_selector = _sdk_fallback_env(monkeypatch, ["1"])
    ran = []

    async def sdk(*a, **k):
        ran.append(1)
        yield {"type": "delta", "content": "slot1 token used"}

    monkeypatch.setattr(model_selector, "_stream_agent_sdk", sdk)

    async def collect():
        return [e async for e in model_selector._iter_agent_sdk_fallback("claude-opus", "s", [], None)]

    events = asyncio.run(collect())
    assert ran == []
    assert len(events) == 1 and events[0]["type"] == "error"
    assert "sdk_fallback_skipped" in events[0]["content"]


def test_sdk_fallback_still_runs_when_slot1_on(monkeypatch):
    model_selector = _sdk_fallback_env(monkeypatch, ["2"])

    async def sdk(*a, **k):
        yield {"type": "delta", "content": "ok"}

    monkeypatch.setattr(model_selector, "_stream_agent_sdk", sdk)

    async def collect():
        return [e async for e in model_selector._iter_agent_sdk_fallback("claude-opus", "s", [], None)]

    assert [e["content"] for e in asyncio.run(collect())] == ["ok"]


def test_call_stream_slot1_off_relay_failure_never_reaches_sdk(monkeypatch):
    """슬롯 1 꺼짐 + 슬롯 2 릴레이 실패: SDK 는 호출되지 않고 오류로 다음 단계로 넘어간다."""
    from app.services.intent_router import IntentResult

    model_selector = _sdk_fallback_env(monkeypatch, ["1"])

    async def no_db_key(*a, **k):
        return ""

    async def available_models():
        return {"claude-opus", "claude-sonnet"}

    async def no_registry_row(*a, **k):
        return None

    async def slot_records(**k):
        return {"2": {"priority": 2, "key_name": "ANTHROPIC_AUTH_TOKEN_2"}}

    calls = []

    async def failing_relay(model, *a, oauth_slot=None, **k):
        calls.append(("relay", oauth_slot))
        yield {"type": "error", "content": "CLI Relay 500: boom"}

    async def sdk(*a, **k):
        calls.append(("sdk", None))
        yield {"type": "delta", "content": "must not run"}

    monkeypatch.setattr(model_selector, "_get_db_key", no_db_key)
    monkeypatch.setattr(model_selector, "get_available_model_ids", available_models)
    monkeypatch.setattr(model_selector, "_get_registered_model_row", no_registry_row)
    monkeypatch.setattr(model_selector, "_get_claude_slot_records", slot_records)
    monkeypatch.setattr(model_selector, "_stream_cli_relay", failing_relay)
    monkeypatch.setattr(model_selector, "_stream_agent_sdk", sdk)

    async def run():
        out = []
        try:
            async for e in model_selector.call_stream(
                IntentResult(intent="cto_strategy", model="claude-opus", use_tools=True, tool_group="all"),
                "system", [{"role": "user", "content": "hi"}],
            ):
                out.append(e)
        except Exception:
            pass
        return out

    events = asyncio.run(asyncio.wait_for(run(), timeout=30))
    assert ("sdk", None) not in calls
    assert all(slot == "2" for kind, slot in calls if kind == "relay")
    assert not any(e.get("content") == "must not run" for e in events)


def test_agent_sdk_service_refuses_when_slot1_off(monkeypatch):
    from app.services import agent_sdk_service

    _sdk_fallback_env(monkeypatch, ["1"])
    svc = agent_sdk_service.AgentSDKService()
    monkeypatch.setattr(agent_sdk_service, "_SDK_AVAILABLE", True)
    monkeypatch.setattr(agent_sdk_service, "AGENT_SDK_ENABLED", True)

    async def run():
        async for _ in svc.execute_stream("hi"):
            pass

    with pytest.raises(RuntimeError, match="슬롯 1"):
        asyncio.run(run())
