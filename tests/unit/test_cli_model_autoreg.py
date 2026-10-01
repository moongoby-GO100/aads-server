"""AADS-CLI-MODEL-AUTOREG-20260930 — codex CLI 신규 모델 자동등록.

발견(models_cache) → 후보(discovered) → 실호출 검증(probe) → runner_llm 비활성 후보.
발견과 실행가능 판정을 분리하는지, 기존 라우팅 행을 건드리지 않는지 본다.
"""
from __future__ import annotations

import importlib.util
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from app.services import cli_model_autoreg, model_registry

REPO_ROOT = Path(__file__).resolve().parents[2]


def _load_probe():
    spec = importlib.util.spec_from_file_location("codex_model_probe", REPO_ROOT / "scripts" / "codex_model_probe.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


probe = _load_probe()


# ---------------------------------------------------------------------------
# fixtures / fakes
# ---------------------------------------------------------------------------

CACHE_PAYLOAD = {
    "fetched_at": "2026-09-30T04:14:00Z",
    "etag": "secret-etag",
    "client_version": "0.159.2",
    "identity": {"account": "someone@example.com"},
    "models": [
        {"slug": "gpt-6.1-sol", "display_name": "GPT-6.1 Sol", "visibility": "list", "supported_in_api": True},
        {"slug": "gpt-6-sol", "display_name": "GPT-6 Sol", "visibility": "list", "supported_in_api": True},
        {"slug": "gpt-reserve", "display_name": "reserve", "visibility": "hide", "supported_in_api": True},
    ],
}


@pytest.fixture
def cache_file(tmp_path, monkeypatch):
    path = tmp_path / "models_cache.json"
    path.write_text(json.dumps(CACHE_PAYLOAD), encoding="utf-8")
    monkeypatch.setenv(cli_model_autoreg.CODEX_CACHE_ENV, str(path))
    monkeypatch.setenv(cli_model_autoreg.CODEX_CACHE_MIRROR_ENV, str(tmp_path / "no-mirror.json"))
    return path


@pytest.fixture
def no_cache(tmp_path, monkeypatch):
    monkeypatch.setenv(cli_model_autoreg.CODEX_CACHE_ENV, str(tmp_path / "missing.json"))
    monkeypatch.setenv(cli_model_autoreg.CODEX_CACHE_MIRROR_ENV, str(tmp_path / "missing-mirror.json"))


def _skip_fetch(**extra):
    return AsyncMock(return_value=([], {"status": "skipped", "error": "test", **extra}))


@pytest.fixture
def api_fetchers_off(monkeypatch):
    for name in ("_fetch_openai_models", "_fetch_anthropic_models", "_fetch_gemini_models",
                 "_fetch_litellm_models", "_fetch_kimi_models"):
        monkeypatch.setattr(model_registry, name, _skip_fetch())


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


class DbConn:
    """sync 가 쓰는 SQL 만 흉내 낸다. 후보·라우팅 테이블은 PK 충돌 시 DO NOTHING."""

    def __init__(self, *, existing=(), verified=(), candidates=None, routing=None):
        self.existing = set(existing)
        self.verified = set(verified)
        self.candidates = dict(candidates or {})
        self.routing = dict(routing or {})
        self.llm_models: dict[tuple[str, str], dict] = {}
        self.execute_calls: list[tuple[str, tuple]] = []
        self.discovery_runs: list[tuple] = []

    def transaction(self):
        return _Tx()

    async def fetch(self, query, *args):
        if "activation_source = 'manual'" in query:
            return []
        if query.strip() == "SELECT provider, model_id FROM llm_models":
            return [{"provider": p, "model_id": m} for p, m in self.existing]
        if "FROM llm_model_candidates" in query and "status = 'verified'" in query:
            return [{"model_id": m} for m in self.verified]
        if "INSERT INTO llm_model_candidates" in query:
            assert "DO NOTHING" in query and "'discovered'" in query
            key = ("codex", args[0])
            if key in self.candidates:
                return []
            self.candidates[key] = {"status": "discovered", "product_name": args[1], "source": args[2]}
            return [{"model_id": args[0]}]
        if "SELECT provider, model_id, execution_model_id" in query:
            return []  # 채팅 LLM 후보 반영(register_chat_llm_candidates)은 별도 테스트에서 다룬다.
        if "FROM model_routing_preferences WHERE route_key" in query:
            return [{"provider": k[1], "model_id": k[2]} for k in self.routing if k[0] == args[0]]
        if "INSERT INTO model_routing_preferences" in query:
            assert "DO NOTHING" in query and "VALUES ($1, $2, $3, $4, FALSE, FALSE" in query
            key = (args[0], args[1], args[2])
            if key in self.routing:
                return []
            self.routing[key] = {"is_enabled": False, "updated_by": args[5]}
            return [{"provider": args[1], "model_id": args[2]}]
        raise AssertionError(f"unexpected fetch: {query[:80]}")

    async def execute(self, query, *args):
        self.execute_calls.append((query, args))
        if "INSERT INTO llm_models" in query:
            self.llm_models[(args[0], args[1])] = {
                "is_active": args[11], "discovery_source": args[16],
                "verification_status": args[17], "is_executable": args[22],
            }
        if "INSERT INTO llm_model_discovery_runs" in query:
            self.discovery_runs.append(args)
        return "OK"


async def _sync(monkeypatch, conn, key_rows=()):
    monkeypatch.setattr(model_registry, "get_pool", lambda: _Pool(conn))
    monkeypatch.setattr(model_registry, "_fetch_key_rows", AsyncMock(return_value=list(key_rows)))
    monkeypatch.setattr(model_registry, "append_key_audit_log", AsyncMock())
    notify = AsyncMock(return_value=True)
    monkeypatch.setattr("app.services.ohvis_alert.notify", notify)
    result = await model_registry.sync_model_registry(triggered_by="test", reason="unit")
    return result, notify


# ---------------------------------------------------------------------------
# A/B — 발견
# ---------------------------------------------------------------------------

def test_parse_cache_keeps_only_list_visibility():
    rows, hidden = cli_model_autoreg.parse_codex_models_cache(CACHE_PAYLOAD)
    assert [r["model_id"] for r in rows] == ["gpt-6.1-sol", "gpt-6-sol"]
    assert hidden == 1
    # 계정 정보는 raw 로 옮기지 않는다.
    assert all("identity" not in r["raw"] and "etag" not in r["raw"] for r in rows)


@pytest.mark.asyncio
async def test_discovery_adds_codex_cli_source_hide_excluded(cache_file, api_fetchers_off):
    rows, runs = await model_registry.discover_provider_model_rows([], enabled=True)
    codex_rows = [r for r in rows if r["provider"] == "codex"]
    assert {r["model_id"] for r in codex_rows} == {"gpt-6.1-sol", "gpt-6-sol"}
    assert all(r["discovery_source"] == "codex_cli_cache" for r in codex_rows)
    # 목록에 있다고 실행 가능한 것이 아니다.
    assert all(r["is_active"] is False and r["is_executable"] is False for r in codex_rows)
    codex_run = next(run for run in runs if run["provider"] == "codex")
    assert codex_run["status"] == "ok" and codex_run["count"] == 2 and codex_run["hidden_count"] == 1


@pytest.mark.asyncio
async def test_sync_registers_new_codex_slug_inactive_and_candidate(monkeypatch, cache_file, api_fetchers_off):
    conn = DbConn(existing={("codex", "gpt-6-sol")})
    result, notify = await _sync(monkeypatch, conn)

    assert result["ok"] is True
    new_row = conn.llm_models[("codex", "gpt-6.1-sol")]
    assert new_row["is_active"] is False and new_row["is_executable"] is False
    assert new_row["discovery_source"] == "codex_cli_cache"
    # 신규 1건만 후보로. 템플릿 slug(gpt-6-sol)·hide(gpt-reserve)는 후보를 만들지 않는다.
    assert set(conn.candidates) == {("codex", "gpt-6.1-sol")}
    assert conn.candidates[("codex", "gpt-6.1-sol")]["source"] == "codex_cli_cache"
    assert ("codex", "gpt-reserve") not in conn.llm_models
    # 발견만으로는 runner_llm 에 넣지 않는다(검증 전).
    assert not any(k[1] == "codex" for k in conn.routing)
    notify.assert_not_awaited()
    # 템플릿 하드코딩은 늘리지 않는다.
    assert "gpt-6.1-sol" not in model_registry._PROVIDER_MODELS["codex"]


@pytest.mark.asyncio
async def test_template_slug_and_existing_row_not_duplicated(monkeypatch, cache_file, api_fetchers_off):
    conn = DbConn(existing={("codex", "gpt-6-sol"), ("codex", "gpt-6.1-sol")})
    await _sync(monkeypatch, conn)
    assert conn.candidates == {}
    # 템플릿 행은 템플릿 출처로 남는다.
    assert conn.llm_models[("codex", "gpt-6-sol")]["discovery_source"] == "template"


@pytest.mark.asyncio
async def test_missing_cache_records_unavailable_run(monkeypatch, no_cache, api_fetchers_off):
    rows, runs = await model_registry.discover_provider_model_rows([], enabled=True)
    codex_run = next(run for run in runs if run["provider"] == "codex")
    assert codex_run["status"] == "unavailable"
    assert "not_found:" in codex_run["error"] and "missing-mirror.json" in codex_run["error"]
    assert not [r for r in rows if r["provider"] == "codex"]

    conn = DbConn()
    await _sync(monkeypatch, conn)
    recorded = [args for args in conn.discovery_runs if args[0] == "codex"]
    assert recorded and recorded[0][1] == "unavailable"
    # 캐시를 못 읽은 회차에는 codex_cli_cache 행을 은퇴시키지 않는다.
    retire = next(args for q, args in conn.execute_calls if "retired_at = COALESCE" in q)
    assert retire[1] is True


@pytest.mark.asyncio
async def test_sync_keeps_probe_verified_codex_row_executable(monkeypatch, cache_file, api_fetchers_off):
    conn = DbConn(existing={("codex", "gpt-6-sol"), ("codex", "gpt-6.1-sol")}, verified={"gpt-6.1-sol"})
    _, notify = await _sync(monkeypatch, conn)
    row = conn.llm_models[("codex", "gpt-6.1-sol")]
    assert row["is_executable"] is True and row["verification_status"] == "verified"
    # verified → runner_llm 비활성 후보 + 알림 1건
    assert conn.routing[("runner_llm", "codex", "gpt-6.1-sol")] == {"is_enabled": False, "updated_by": "auto_discovery"}
    notify.assert_awaited_once()
    assert notify.await_args.args[0] == cli_model_autoreg.VERIFIED_ALERT_TITLE

    # 두 번째 sync: 행이 이미 있으니 알림도 다시 안 간다.
    _, notify2 = await _sync(monkeypatch, conn)
    notify2.assert_not_awaited()


# ---------------------------------------------------------------------------
# C — probe 상태 전이
# ---------------------------------------------------------------------------

def test_classify_probe_three_outcomes():
    assert probe.classify_probe(0, "OK\n", "") == ("verified", "codex exec 응답 OK")
    status, note = probe.classify_probe(
        1, "",
        "ERROR: unexpected status 400 Bad Request: {\"detail\":\"The 'gpt-6.1-sol' model is not supported "
        "when using Codex with a ChatGPT account.\"}\nmore noise",
    )
    assert status == "blocked_account"
    assert "not supported when using Codex with a ChatGPT account" in note and "\n" not in note
    assert probe.classify_probe(1, "", "error: stream disconnected")[0] == "probe_failed"
    assert probe.classify_probe(124, "", "", timed_out=True)[0] == "probe_failed"


class FakeStore:
    def __init__(self, rows):
        self.rows = rows
        self.recorded: list[tuple[str, str, str]] = []

    def candidates(self):
        return [dict(r) for r in self.rows]

    def record(self, model_id, status, note):
        self.recorded.append((model_id, status, note))
        for r in self.rows:
            if r["model_id"] == model_id:
                r["status"] = status
        return {"runner_candidate_inserted": status == "verified"}


def test_probe_transitions_and_daily_cap():
    now = datetime(2026, 9, 30, 12, 0, tzinfo=timezone.utc)
    store = FakeStore([
        {"model_id": "m-ok", "status": "discovered", "last_probe_at": None},
        {"model_id": "m-blocked", "status": "discovered", "last_probe_at": None},
        {"model_id": "m-fail", "status": "discovered", "last_probe_at": None},
        # 24시간이 안 지난 blocked_account 는 건너뛴다.
        {"model_id": "m-recent", "status": "blocked_account", "last_probe_at": now - timedelta(hours=3)},
        # 24시간 지난 blocked_account 는 재시도.
        {"model_id": "m-retry", "status": "blocked_account", "last_probe_at": now - timedelta(hours=25)},
    ])
    outcomes = {
        "m-ok": ("verified", "ok"),
        "m-blocked": ("blocked_account", "not supported when using Codex with a ChatGPT account"),
        "m-fail": ("probe_failed", "rc=1"),
        "m-retry": ("blocked_account", "still"),
    }
    calls = []

    def fake_probe(model_id, *, deadline):
        calls.append(model_id)
        return outcomes[model_id]

    results = probe.probe_candidates(store, probe=fake_probe, now=now, deadline=1e12, max_models=10)
    assert calls == ["m-ok", "m-blocked", "m-fail", "m-retry"]
    assert [(r["model_id"], r["status"]) for r in results] == [
        ("m-ok", "verified"), ("m-blocked", "blocked_account"),
        ("m-fail", "probe_failed"), ("m-retry", "blocked_account"),
    ]
    # verified 는 다시 치지 않는다.
    assert probe.is_due({"status": "verified", "last_probe_at": None}, now) is False


class _Cursor:
    def __init__(self, conn):
        self.conn = conn

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, sql, params=()):
        self.conn.sql.append((sql, params))

    def fetchone(self):
        return ("x",) if self.conn.insert_result else None


class _PgConn:
    def __init__(self, insert_result=True):
        self.sql = []
        self.insert_result = insert_result

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def cursor(self):
        return _Cursor(self)


def test_probe_store_record_sql():
    blocked = _PgConn()
    probe.ProbeStore(blocked).record("gpt-6.1-sol", "blocked_account", "400 not supported ...")
    assert len(blocked.sql) == 1 and "UPDATE llm_model_candidates" in blocked.sql[0][0]
    assert blocked.sql[0][1][0] == "blocked_account"

    ok = _PgConn(insert_result=True)
    out = probe.ProbeStore(ok).record("gpt-7", "verified", "ok")
    joined = "\n".join(s for s, _ in ok.sql)
    assert "SET is_executable = TRUE" in joined and "provider = 'codex'" in joined
    assert "is_enabled" not in joined.split("INSERT INTO model_routing_preferences")[0]
    assert "FALSE, FALSE" in joined and "DO NOTHING" in joined
    assert "INSERT INTO alert_history" in joined and out["runner_candidate_inserted"] is True

    again = _PgConn(insert_result=False)
    probe.ProbeStore(again).record("gpt-7", "verified", "ok")
    assert not any("alert_history" in s for s, _ in again.sql)


def test_build_mirror_strips_account_fields():
    mirror = probe.build_mirror(CACHE_PAYLOAD, "/root/.codex/models_cache.json")
    assert "identity" not in mirror and "etag" not in mirror
    assert [m["slug"] for m in mirror["models"]] == ["gpt-6.1-sol", "gpt-6-sol", "gpt-reserve"]
    rows, hidden = cli_model_autoreg.parse_codex_models_cache(mirror)
    assert len(rows) == 2 and hidden == 1


# ---------------------------------------------------------------------------
# D — runner_llm 후보 자동등록
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_runner_candidates_idempotent_and_existing_rows_untouched():
    existing = {
        ("runner_llm", "openai", "gpt-6.1-sol"): {"is_enabled": False, "updated_by": "CEO-chat:gpt61sol-candidate"},
        ("runner_llm", "codex", "gpt-6-sol"): {"is_enabled": True, "updated_by": "CEO:9102c970-runner-tail"},
    }
    conn = DbConn(routing={k: dict(v) for k, v in existing.items()})
    wanted = [("openai", "gpt-6.1-sol"), ("codex", "gpt-6-sol"), ("codex", "gpt-7")]

    first = await cli_model_autoreg.register_runner_llm_candidates(conn, wanted)
    second = await cli_model_autoreg.register_runner_llm_candidates(conn, wanted)

    assert first == [("codex", "gpt-7")]
    assert second == []
    for key, value in existing.items():
        assert conn.routing[key] == value
    assert conn.routing[("runner_llm", "codex", "gpt-7")] == {"is_enabled": False, "updated_by": "auto_discovery"}
    assert not any("UPDATE model_routing_preferences" in q for q, _ in conn.execute_calls)


@pytest.mark.asyncio
async def test_claude_api_discovery_gets_runner_candidate(monkeypatch, no_cache, api_fetchers_off):
    """F: Claude 는 별도 CLI 카탈로그가 없다 — anthropic_api discovery 가 D 경로를 그대로 탄다."""
    monkeypatch.setattr(model_registry, "_fetch_anthropic_models", AsyncMock(return_value=(
        [{"model_id": "claude-nova-6", "display_name": "Claude Nova 6", "raw": {}}], {"status": "ok"},
    )))
    now = datetime.now(timezone.utc)
    key_rows = [{
        "id": 1, "provider": "anthropic", "key_name": "ANTHROPIC_AUTH_TOKEN", "priority": 1,
        "is_active": True, "rate_limited_until": None, "last_used_at": now, "last_verified_at": now,
    }]
    conn = DbConn(routing={("runner_llm", "anthropic", "claude-sonnet-5-5"): {"is_enabled": True, "updated_by": "x"}})
    await _sync(monkeypatch, conn, key_rows=key_rows)
    assert conn.routing[("runner_llm", "anthropic", "claude-nova-6")] == {"is_enabled": False, "updated_by": "auto_discovery"}
    assert conn.routing[("runner_llm", "anthropic", "claude-sonnet-5-5")] == {"is_enabled": True, "updated_by": "x"}
    # 템플릿 모델은 "새로 발견" 이 아니다.
    assert ("runner_llm", "anthropic", "claude-opus-5-5") not in conn.routing

    # 이미 llm_models 에 있던 모델은 다시 후보로 올리지 않는다.
    conn2 = DbConn(existing={("anthropic", "claude-nova-6")})
    await _sync(monkeypatch, conn2, key_rows=key_rows)
    assert conn2.routing == {}


# ---------------------------------------------------------------------------
# E — 러너 안전장치
# ---------------------------------------------------------------------------

class _RunnerConn:
    def __init__(self, rows, codex_exec):
        self.rows = rows
        self.codex_exec = codex_exec

    async def fetch(self, query, *args):
        if "FROM llm_models WHERE provider = 'codex' AND is_executable = TRUE" in query:
            return [{"model_id": m} for m in self.codex_exec]
        return self.rows


@pytest.mark.asyncio
async def test_routing_guard_drops_openai_models_not_executable_on_codex(monkeypatch):
    from app.services import pipeline_runner_service as prs

    rows = [
        {"group_order": 2, "model": "claude-sonnet-5-5", "priority": 1, "provider": "anthropic"},
        {"group_order": 2, "model": "gpt-6.1-sol", "priority": 2, "provider": "openai"},
        {"group_order": 2, "model": "gpt-5.5", "priority": 3, "provider": "openai"},
        {"group_order": 2, "model": "gpt-6-sol", "priority": 4, "provider": "codex"},
    ]
    conn = _RunnerConn(rows, codex_exec={"gpt-5.5", "gpt-6-sol"})
    monkeypatch.setattr("app.core.db_pool.get_pool", lambda: _Pool(conn))
    monkeypatch.setattr(model_registry, "filter_executable_models", AsyncMock(side_effect=lambda m: list(m)))

    models = await prs._get_db_model_config("M")
    assert models == ["claude-sonnet-5-5", "codex:gpt-5.5", "codex:gpt-6-sol"]
    assert "codex:gpt-6.1-sol" not in models


def test_auth_error_is_not_a_model_verdict():
    """인증 실패(무효 토큰 401)는 기록하지 않고 회차를 멈춘다 — 모델의 24h 상한을 소모하지 않는다."""
    status, note = probe.classify_probe(
        1, "", "ERROR codex: unexpected status 401 Unauthorized: Encountered invalidated oauth token for user"
    )
    assert status == "auth_error" and "401" in note

    store = FakeStore([
        {"model_id": "m1", "status": "discovered", "last_probe_at": None},
        {"model_id": "m2", "status": "discovered", "last_probe_at": None},
    ])
    results = probe.probe_candidates(
        store, probe=lambda model_id, *, deadline: ("auth_error", "401"), deadline=1e12, max_models=10
    )
    assert store.recorded == []
    assert [r["status"] for r in results] == ["auth_error"]


def test_probe_uses_catalog_account_home():
    # 카탈로그를 받은 계정(/root/.codex)으로 친다 — 러너 셸의 CODEX_HOME 을 따라가지 않는다.
    assert probe.PROBE_CODEX_HOME == str(Path(probe.CODEX_CACHE).parent)
