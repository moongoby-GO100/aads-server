"""DB execution authorization and catalog-independent relay protocol regression tests."""
import json
from unittest.mock import AsyncMock, Mock

import pytest

from app.services import model_registry as registry
from scripts import claude_model_contract as contract


@pytest.fixture
def catalog(monkeypatch):
    monkeypatch.setattr(registry, "_claude_execution_models", None)
    monkeypatch.setattr(registry, "_claude_execution_expires", 0.0)
    conn = Mock()
    conn.fetch = AsyncMock(return_value=[])
    pool = Mock()
    pool.acquire.return_value.__aenter__ = AsyncMock(return_value=conn)
    pool.acquire.return_value.__aexit__ = AsyncMock(return_value=False)
    monkeypatch.setattr(registry, "get_pool", lambda: pool)
    return conn.fetch


def row(model="claude-test-9-9", execution=None):
    return {"model_id": model, "execution_model_id": execution, "metadata": {}}


@pytest.mark.asyncio
async def test_db_only_model_execution_and_empty_catalog_blocks_everything(catalog):
    catalog.return_value = [row()]
    assert await registry.resolve_executable_claude_model("claude-test-9-9") == "claude-test-9-9"
    assert "provider = 'anthropic' AND is_executable = TRUE" in catalog.call_args.args[0]
    await registry.resolve_executable_claude_model("claude-test-9-9")
    assert catalog.await_count == 1
    # DB success with 0 rows is authoritative: previous snapshot is NOT resurrected.
    catalog.return_value = []
    registry.invalidate_registry_cache()
    with pytest.raises(ValueError):
        await registry.resolve_executable_claude_model("claude-test-9-9")
    # A non-empty snapshot without the model denies it (is_executable=false row excluded by SQL).
    catalog.return_value = [row("claude-other-1-1")]
    registry.invalidate_registry_cache()
    with pytest.raises(ValueError):
        await registry.resolve_executable_claude_model("claude-test-9-9")


@pytest.mark.asyncio
async def test_last_model_disabled_blocks_within_ttl_after_invalidation_and_by_ttl(catalog, monkeypatch):
    now = [1000.0]
    monkeypatch.setattr(registry.time, "monotonic", lambda: now[0])
    catalog.return_value = [row()]
    assert await registry.resolve_executable_claude_model("claude-test-9-9")
    catalog.return_value = []  # last executable model switched off
    now[0] += 119  # still inside TTL: cached allow
    assert await registry.resolve_executable_claude_model("claude-test-9-9") == "claude-test-9-9"
    registry.invalidate_registry_cache()  # sync invalidation: immediate block
    with pytest.raises(ValueError):
        await registry.resolve_executable_claude_model("claude-test-9-9")
    catalog.return_value = [row()]
    registry.invalidate_registry_cache()
    assert await registry.resolve_executable_claude_model("claude-test-9-9")
    catalog.return_value = []
    now[0] += 120  # TTL expiry alone also blocks
    with pytest.raises(ValueError):
        await registry.resolve_executable_claude_model("claude-test-9-9")


@pytest.mark.asyncio
async def test_zero_rows_without_snapshot_blocks_code_list_models_too(catalog, caplog):
    catalog.return_value = []
    with caplog.at_level("ERROR"):
        with pytest.raises(ValueError):
            await registry.resolve_executable_claude_model("opus")
    assert "claude_execution_zero_rows" in caplog.text


@pytest.mark.asyncio
async def test_execution_id_and_legacy_aliases(catalog):
    catalog.return_value = [row("claude-test-9-9", "claude-test-9-9-20261111"), row("claude-opus")]
    assert await registry.resolve_executable_claude_model("claude-test-9-9") == "claude-test-9-9-20261111"
    assert await registry.resolve_executable_claude_model("claude-test-9-9-20261111") == "claude-test-9-9-20261111"
    assert await registry.resolve_executable_claude_model("opus") == contract.resolve_model("opus")


@pytest.mark.asyncio
async def test_error_retains_last_success_and_offline_fallback(catalog, monkeypatch):
    catalog.side_effect = RuntimeError("DB unavailable")
    assert await registry.resolve_executable_claude_model("opus") == contract.resolve_model("opus")
    with pytest.raises(ValueError):
        await registry.resolve_executable_claude_model("claude-test-9-9")
    assert catalog.await_count == 1
    catalog.side_effect = None
    catalog.return_value = [row()]
    registry.invalidate_registry_cache()
    await registry.resolve_executable_claude_model("claude-test-9-9")
    catalog.side_effect = RuntimeError("DB unavailable")
    registry.invalidate_registry_cache()
    assert await registry.resolve_executable_claude_model("claude-test-9-9") == "claude-test-9-9"
    with pytest.raises(ValueError):
        await registry.resolve_executable_claude_model("opus")


@pytest.mark.asyncio
async def test_ttl_refresh(catalog, monkeypatch):
    now = [1000.0]
    monkeypatch.setattr(registry.time, "monotonic", lambda: now[0])
    catalog.return_value = [row()]
    await registry.resolve_executable_claude_model("claude-test-9-9")
    catalog.return_value = [row("claude-other-1-1")]
    now[0] += 120
    with pytest.raises(ValueError):
        await registry.resolve_executable_claude_model("claude-test-9-9")


@pytest.mark.parametrize("model", ["claude-../x", "claude-test-9-9 ", " claude-test-9-9", "CLAUDE-test-9-9", "gpt-9", "claude-test-9\nx"])
def test_relay_rejects_unsafe_format(model):
    with pytest.raises(ValueError):
        contract.resolve_relay_model(model)


def test_pattern_aliases_and_protocol_independent_of_catalog(monkeypatch):
    assert contract.resolve_relay_model("claude-test-9-9") == "claude-test-9-9"
    for alias in contract.ALIASES:
        assert contract.resolve_relay_model(alias) == contract.resolve_model(alias)
    monkeypatch.setattr(contract, "EXACT_MODEL_IDS", frozenset())
    assert contract.CONTRACT_VERSION == 1  # Already-shipped API/relay protocol.
    assert contract.resolve_relay_model("claude-test-9-9") == "claude-test-9-9"
    assert contract.session_key("s", "1", "claude-test-9-9") == "s@1@claude-test-9-9"


@pytest.mark.asyncio
@pytest.mark.parametrize("version", [None, 1])
@pytest.mark.parametrize("model", ["claude-opus", "claude-test-9-9"])
async def test_old_and_new_clients_pass_relay_version_gate(monkeypatch, version, model):
    from scripts import claude_relay_server as relay

    class PastVersionGate(Exception):
        pass

    def pick_auth(**kwargs):
        raise PastVersionGate

    monkeypatch.setattr(relay, "_DIRECT_OAUTH_ENABLED", True)
    monkeypatch.setattr(relay, "_pick_auth", pick_auth)
    request = AsyncMock()
    request.json.return_value = {"model": model, "model_contract_version": version, "messages_text": "test"}
    with pytest.raises(PastVersionGate):
        await relay.handle_stream(request)


@pytest.mark.asyncio
async def test_api_blocks_unverified_but_probe_reaches_relay(monkeypatch, catalog):
    from app.services import model_selector as selector

    client = AsyncMock()
    client.get.return_value = Mock(status_code=503)
    factory = Mock()
    factory.return_value.__aenter__ = AsyncMock(return_value=client)
    factory.return_value.__aexit__ = AsyncMock(return_value=False)
    monkeypatch.setattr(selector.httpx, "AsyncClient", factory)
    events = [event async for event in selector._stream_cli_relay_once("claude-test-9-9", "", [])]
    assert events[-1]["error_type"] == "unsupported_claude_model"
    client.get.assert_not_called()
    events = [event async for event in selector._stream_cli_relay_once("claude-test-9-9", "", [], activation_probe=True)]
    assert "503" in events[-1]["content"]
    client.get.assert_awaited_once()


@pytest.mark.asyncio
async def test_activation_probe_explicitly_uses_probe_path(monkeypatch):
    from app.services import model_selector as selector
    calls = []

    async def stream(model, *args, **kwargs):
        calls.append((model, kwargs))
        yield {"type": "error", "content": "test rejection"}

    monkeypatch.setattr(selector, "_stream_cli_relay_once", stream)
    await registry._probe_claude_cli_relay("claude-test-9-9")
    assert calls[0][1]["activation_probe"] is True


@pytest.mark.asyncio
async def test_successful_sync_immediately_invalidates_snapshot(catalog, monkeypatch):
    catalog.return_value = [row()]
    await registry.resolve_executable_claude_model("claude-test-9-9")
    catalog.return_value = [row("claude-other-1-1")]
    pool = registry.get_pool()
    conn = await pool.acquire().__aenter__()
    conn.fetchval = AsyncMock(return_value=True)
    conn.execute = AsyncMock()
    monkeypatch.setattr(registry, "_sync_model_registry_locked", AsyncMock(return_value={"ok": True}))
    assert (await registry.sync_model_registry())["ok"] is True
    with pytest.raises(ValueError):
        await registry.resolve_executable_claude_model("claude-test-9-9")


def test_shell_runner_snapshot_authorization():
    assert contract.resolve_authorized_model("claude-test-9-9", [row()]) == "claude-test-9-9"
    # None = DB unavailable and no last success -> offline code fallback.
    assert contract.resolve_authorized_model("opus", None) == contract.resolve_model("opus")
    with pytest.raises(ValueError):
        contract.resolve_authorized_model("claude-test-9-9", None)
    # Successful EMPTY snapshot is authoritative: everything blocked, code list included.
    for model in ("opus", "claude-test-9-9"):
        with pytest.raises(ValueError):
            contract.resolve_authorized_model(model, [])
    with pytest.raises(ValueError):
        contract.resolve_authorized_model("claude-test-9-9", [row("claude-other-1-1")])


def _authorize_cli(model, stdin):
    import subprocess, sys
    return subprocess.run([sys.executable, contract.__file__, "--authorize", model],
                          input=stdin, capture_output=True, text=True)


def test_authorize_cli_tolerates_psql_noise_and_bad_json():
    ok = '[{"model_id": "claude-test-9-9", "execution_model_id": "claude-test-9-9"}]'
    for stdin in (ok, "SET\n" + ok + "\n"):
        r = _authorize_cli("claude-test-9-9", stdin)
        assert (r.returncode, r.stdout.strip()) == (0, "claude-test-9-9")
    r = _authorize_cli("opus", "garbage{")  # unparseable -> offline fallback, not blanket rejection
    assert r.returncode == 0 and r.stdout.strip() == contract.resolve_model("opus")
    assert _authorize_cli("opus", "").returncode == 0  # no snapshot -> fallback
    assert _authorize_cli("opus", "[]\n").returncode == 2  # empty success -> blocked
    assert _authorize_cli("claude-test-9-9", ok.replace("test-9-9", "other-1-1")).returncode == 2


@pytest.mark.asyncio
async def test_runner_empty_model_not_an_error(catalog):
    from app.services import pipeline_runner_service as runner

    # 1. DB lookup itself failed with no snapshot: follows offline code fallback rule.
    catalog.side_effect = RuntimeError("DB unavailable")
    assert await runner._resolve_cli_model_allowed("") == "claude-sonnet-5-5"
    assert await runner._resolve_cli_model_allowed("   ") == "claude-sonnet-5-5"

    # 2. DB has executable models: empty model resolves to DB default id explicitly.
    catalog.side_effect = None
    catalog.return_value = [row()]
    registry.invalidate_registry_cache()
    assert await runner._resolve_cli_model_allowed("") == "claude-test-9-9"
    assert await runner._resolve_cli_model_allowed("   ") == "claude-test-9-9"

    # If standard sonnet is available, it is preferred as default.
    catalog.return_value = [row("claude-other-1-1"), row("claude-sonnet-5-5")]
    registry.invalidate_registry_cache()
    assert await runner._resolve_cli_model_allowed("") == "claude-sonnet-5-5"

    # 3. DB lookup succeeded but 0 rows: empty model fails with error (fail-closed, no bypass).
    catalog.return_value = []
    registry.invalidate_registry_cache()
    with pytest.raises(ValueError):
        await runner._resolve_cli_model_allowed("")
    with pytest.raises(ValueError):
        await runner._resolve_cli_model_allowed("   ")

    # DB failure after successful 0 rows retains 0-row block (not resurrecting code fallback).
    catalog.side_effect = RuntimeError("DB unavailable")
    registry.invalidate_registry_cache()
    with pytest.raises(ValueError):
        await runner._resolve_cli_model_allowed("")


def test_litellm_sdk_paths_keep_legacy_model_mapping():
    import inspect
    from app.services import model_selector as selector
    assert "resolve_executable_claude_model" not in inspect.getsource(selector._stream_anthropic)
    assert "resolve_executable_claude_model" not in inspect.getsource(selector._stream_litellm_anthropic)
    assert "startswith" not in inspect.getsource(selector._stream_litellm).split("_is_claude =")[1].split("\n")[0]


@pytest.mark.asyncio
async def test_api_accepts_shipped_v1_relay_without_catalog_comparison(monkeypatch, catalog):
    from app.services import model_selector as selector

    catalog.return_value = [row()]
    client = AsyncMock()
    # Old relay health has no source field and a different model catalog.
    client.get.return_value = Mock(status_code=200, json=lambda: {
        "claude_model_contract": {"version": 1, "models": ["claude-opus-5-5"]},
    })
    client.stream = Mock(side_effect=RuntimeError("past health gate"))
    factory = Mock()
    factory.return_value.__aenter__ = AsyncMock(return_value=client)
    factory.return_value.__aexit__ = AsyncMock(return_value=False)
    monkeypatch.setattr(selector.httpx, "AsyncClient", factory)
    events = [event async for event in selector._stream_cli_relay_once("claude-test-9-9", "", [])]
    assert client.stream.call_count == 1
    assert not any("model_contract_version_mismatch" in event.get("content", "") for event in events)


@pytest.mark.asyncio
async def test_python_runner_rejects_before_launch(catalog, monkeypatch):
    from app.services import pipeline_runner_service as runner

    job = object.__new__(runner.PipelineCJob)
    job.model = "claude-test-9-9"
    job._log = Mock()
    launch = AsyncMock(return_value={"output": "ok"})
    monkeypatch.setattr(job, "_run_claude_code", launch)
    catalog.return_value = [row("claude-other-1-1")]
    result = await job._run_model_candidate("test", "claude-test-9-9")
    assert "unsupported_claude_model" in result["error"]
    launch.assert_not_called()
    catalog.return_value = [row()]
    registry.invalidate_registry_cache()
    result = await job._run_model_candidate("test", "claude-test-9-9")
    assert result["output"] == "ok"
    launch.assert_awaited_once()


# --- sync guard -----------------------------------------------------------

def _mrow(provider, model, executable, **kw):
    return {"provider": provider, "model_id": model, "is_executable": executable, **kw}


def test_guard_blocks_only_when_last_executable_anthropic_would_go():
    cur = [{"model_id": "a", "activation_source": "db"}, {"model_id": "b", "activation_source": "db"}]
    guard = registry._would_zero_anthropic_executable
    assert guard(cur, [_mrow("anthropic", "a", False), _mrow("anthropic", "b", False)]) is True
    assert guard(cur, [_mrow("anthropic", "a", False)]) is True  # b retired by absence
    assert guard(cur, [_mrow("anthropic", "a", False), _mrow("anthropic", "b", True)]) is False  # partial
    assert guard(cur, [_mrow("anthropic", "a", False), _mrow("anthropic", "c", True)]) is False  # new on
    assert guard([], [_mrow("anthropic", "a", False)]) is False
    assert guard(cur, [_mrow("codex", "x", True)]) is True
    manual = [{"model_id": "m", "activation_source": "manual"}]
    assert guard(manual, []) is False  # manual rows are not retired by sync


@pytest.fixture
def sync_env(monkeypatch):
    conn = Mock()
    conn.fetch = AsyncMock(return_value=[])
    conn.execute = AsyncMock()
    tx = Mock()
    tx.__aenter__ = AsyncMock(return_value=None)
    tx.__aexit__ = AsyncMock(return_value=False)
    conn.transaction = Mock(return_value=tx)
    pool = Mock()
    pool.acquire.return_value.__aenter__ = AsyncMock(return_value=conn)
    pool.acquire.return_value.__aexit__ = AsyncMock(return_value=False)
    monkeypatch.setattr(registry, "get_pool", lambda: pool)
    for name, val in (
        ("_fetch_key_rows", []), ("_fetch_last_discovery_success", {}), ("_fetch_activation_state", {}),
    ):
        monkeypatch.setattr(registry, name, AsyncMock(return_value=val))
    monkeypatch.setattr(registry.cli_model_autoreg, "fetch_verified_codex_ids", AsyncMock(return_value=set()))
    monkeypatch.setattr(registry.cli_model_autoreg, "autoregister_after_sync", AsyncMock(return_value={}))
    monkeypatch.setattr(registry, "refresh_model_freshness_cache", AsyncMock())
    return conn


def _full_row(model, executable):
    return {
        "provider": "anthropic", "model_id": model, "display_name": model, "family": "x", "category": "c",
        "supports_tools": True, "supports_thinking": False, "supports_vision": False, "supports_coding": True,
        "input_cost": 0, "output_cost": 0, "is_active": executable, "activation_source": "db",
        "linked_key_name": "k", "metadata": {}, "is_executable": executable,
    }


async def _run_sync(conn, monkeypatch, rows, current, initial_table=None):
    table = {k: dict(v) for k, v in initial_table.items()} if initial_table is not None else None

    async def fetch(sql, *a):
        if "activation_source FROM llm_models WHERE provider = 'anthropic'" in sql:
            if isinstance(current, Exception):
                raise current
            return [{"model_id": m, "activation_source": "db"} for m in current]
        return []
    conn.fetch = AsyncMock(side_effect=fetch)

    async def execute_side_effect(sql, *args):
        if table is not None:
            if "INSERT INTO llm_models" in sql:
                prov = args[0]
                mid = args[1]
                meta = json.loads(args[14]) if isinstance(args[14], str) else args[14]
                is_exec = args[22]
                lookup_failed = args[25]
                key = (prov, mid)
                if key in table:
                    table[key]["sync_token"] = meta.get("sync_token")
                    if lookup_failed and prov == "anthropic":
                        pass  # preserved from existing DB row
                    else:
                        table[key]["is_executable"] = is_exec
                else:
                    table[key] = {
                        "provider": prov,
                        "model_id": mid,
                        "is_executable": is_exec,
                        "sync_token": meta.get("sync_token"),
                    }
            elif "retired\": true" in sql:
                sync_token = args[0]
                lookup_failed = args[2]
                guard_anthropic = args[3]
                for (p, m), rec in list(table.items()):
                    if rec.get("sync_token") == sync_token:
                        continue
                    spared = (
                        guard_anthropic
                        and p == "anthropic"
                        and (lookup_failed or rec.get("is_executable") is True)
                    )
                    if not spared:
                        rec["is_executable"] = False
                        rec["retired"] = True
        return "OK"

    conn.execute = AsyncMock(side_effect=execute_side_effect)
    monkeypatch.setattr(registry, "build_registry_snapshots", lambda k: ([], []))
    monkeypatch.setattr(registry, "discover_provider_model_rows", AsyncMock(return_value=([], [])))
    monkeypatch.setattr(registry, "_merge_model_rows", lambda *a, **k: rows)
    monkeypatch.setattr(registry, "apply_activation_probes", AsyncMock(return_value={}))
    monkeypatch.setattr(registry.cli_model_autoreg, "apply_verified_codex", lambda *a, **k: None)
    await registry._sync_model_registry_locked()
    upserts = {c.args[2]: c.args[23] for c in conn.execute.await_args_list if "INSERT INTO llm_models" in c.args[0]}
    retire = [c.args for c in conn.execute.await_args_list if "retired\": true" in c.args[0]]
    if initial_table is not None:
        return upserts, retire, table
    return upserts, retire


@pytest.mark.asyncio
async def test_sync_guard_keeps_last_executable_and_logs_error(sync_env, monkeypatch, caplog):
    rows = [_full_row("claude-a-1-1", False)]
    with caplog.at_level("ERROR"):
        upserts, retire = await _run_sync(sync_env, monkeypatch, rows, ["claude-a-1-1"])
    assert upserts["claude-a-1-1"] is True  # downgrade not applied
    assert retire[0][-1] is True  # retire SQL spared executable anthropic rows
    assert "sync_would_zero_anthropic_executable" in caplog.text


@pytest.mark.asyncio
async def test_sync_partial_disable_applies_normally(sync_env, monkeypatch, caplog):
    rows = [_full_row("claude-a-1-1", False), _full_row("claude-b-1-1", True)]
    with caplog.at_level("ERROR"):
        upserts, retire = await _run_sync(sync_env, monkeypatch, rows, ["claude-a-1-1", "claude-b-1-1"])
    assert upserts == {"claude-a-1-1": False, "claude-b-1-1": True}
    assert retire[0][-1] is False
    assert "sync_would_zero" not in caplog.text


@pytest.mark.asyncio
async def test_sync_guard_lookup_exception_fails_closed(sync_env, monkeypatch, caplog):
    rows = [_full_row("claude-a-1-1", False), _full_row("gpt-b-1-1", False)]
    rows[1]["provider"] = "openai"
    with caplog.at_level("ERROR"):
        upserts, retire = await _run_sync(
            sync_env, monkeypatch, rows, RuntimeError("DB connection dropped")
        )
    assert upserts["claude-a-1-1"] is False  # R5: does not promote to True
    assert upserts["gpt-b-1-1"] is False  # other provider sync continues normally
    assert retire[0][-1] is True  # retire SQL spared executable anthropic rows
    assert "anthropic_guard_lookup_failed" in caplog.text


@pytest.mark.asyncio
async def test_sync_guard_lookup_failure_preserves_false_in_db(sync_env, monkeypatch, caplog):
    # 1. lookup 예외 주입 + DB 에 is_executable=false 인 anthropic 행이 있고 sync 결과도 false → 실행 후에도 false (승격 안 됨)
    db = {("anthropic", "claude-a-1-1"): {"is_executable": False}}
    rows = [_full_row("claude-a-1-1", False)]
    with caplog.at_level("ERROR"):
        upserts, retire, final_db = await _run_sync(
            sync_env, monkeypatch, rows, RuntimeError("DB connection dropped"), initial_table=db
        )
    assert final_db[("anthropic", "claude-a-1-1")]["is_executable"] is False
    assert upserts["claude-a-1-1"] is False
    insert_call = next(c for c in sync_env.execute.await_args_list if "INSERT INTO llm_models" in c.args[0])
    assert "is_executable = CASE" in insert_call.args[0]
    assert insert_call.args[26] is True


@pytest.mark.asyncio
async def test_sync_guard_lookup_failure_preserves_true_in_db(sync_env, monkeypatch, caplog):
    # 2. lookup 예외 주입 + DB 에 is_executable=true 인 anthropic 행, sync 결과 false → 실행 후에도 true (하향 안 됨)
    db = {("anthropic", "claude-a-1-1"): {"is_executable": True}}
    rows = [_full_row("claude-a-1-1", False)]
    with caplog.at_level("ERROR"):
        upserts, retire, final_db = await _run_sync(
            sync_env, monkeypatch, rows, RuntimeError("DB connection dropped"), initial_table=db
        )
    assert final_db[("anthropic", "claude-a-1-1")]["is_executable"] is True
    assert upserts["claude-a-1-1"] is False


@pytest.mark.asyncio
async def test_sync_guard_lookup_failure_updates_other_providers_normally(sync_env, monkeypatch, caplog):
    # 3. lookup 예외 주입 + 다른 provider 행은 정상 갱신됨
    db = {
        ("openai", "gpt-b-1-1"): {"is_executable": False},
        ("anthropic", "claude-a-1-1"): {"is_executable": True},
    }
    rows = [_full_row("claude-a-1-1", False), _full_row("gpt-b-1-1", True)]
    rows[1]["provider"] = "openai"
    with caplog.at_level("ERROR"):
        upserts, retire, final_db = await _run_sync(
            sync_env, monkeypatch, rows, RuntimeError("DB connection dropped"), initial_table=db
        )
    assert final_db[("openai", "gpt-b-1-1")]["is_executable"] is True
    assert final_db[("anthropic", "claude-a-1-1")]["is_executable"] is True
    assert upserts["gpt-b-1-1"] is True
    assert upserts["claude-a-1-1"] is False


@pytest.mark.asyncio
async def test_sync_guard_lookup_failure_new_anthropic_inserted_as_false(sync_env, monkeypatch, caplog):
    # 신규 anthropic 행이 INSERT 되는 경우에는 is_executable=false 로 넣는다(검증 전 승격 금지)
    db = {}
    rows = [_full_row("claude-new-1-1", True)]
    with caplog.at_level("ERROR"):
        upserts, retire, final_db = await _run_sync(
            sync_env, monkeypatch, rows, RuntimeError("DB connection dropped"), initial_table=db
        )
    assert final_db[("anthropic", "claude-new-1-1")]["is_executable"] is False
    assert upserts["claude-new-1-1"] is False


@pytest.mark.asyncio
async def test_sync_guard_lookup_failure_spares_all_anthropic_from_retirement(sync_env, monkeypatch, caplog):
    # 은퇴 UPDATE 는 lookup_failed 일 때 anthropic 행 전체를 제외한다
    db = {
        ("anthropic", "claude-non-exec"): {"is_executable": False},
        ("anthropic", "claude-exec"): {"is_executable": True},
        ("openai", "gpt-old"): {"is_executable": True},
    }
    rows = [_full_row("claude-current", False)]
    with caplog.at_level("ERROR"):
        upserts, retire, final_db = await _run_sync(
            sync_env, monkeypatch, rows, RuntimeError("DB connection dropped"), initial_table=db
        )
    assert final_db[("anthropic", "claude-non-exec")].get("retired") is not True
    assert final_db[("anthropic", "claude-exec")].get("retired") is not True
    assert final_db[("openai", "gpt-old")].get("retired") is True


def _sh(tmp_path, snapshot_file, db_body, model, preseed=None):
    """Run the REAL shell functions with db_exec redefined; returns (rc_refresh, authorize_rc, out, file)."""
    import subprocess
    from pathlib import Path
    root = Path(contract.__file__).resolve().parent
    if preseed is not None:
        snapshot_file.write_text(preseed)
    script = (
        'CLAUDE_MODEL_CONTRACT=%s\nCLAUDE_EXEC_SNAPSHOT_FILE=%s\n'
        % (root / "claude_model_contract.py", snapshot_file)
        + "eval \"$(sed -n '/^refresh_claude_execution_snapshot() {/,/^}/p;/^authorize_claude_model() {/,/^}/p' %s)\"\n"
        % (root / "pipeline-runner.sh")
        + "db_exec() { %s; }\n" % db_body
        + 'refresh_claude_execution_snapshot; echo "refresh=$?"\n'
        + 'authorize_claude_model %s; echo "auth=$?"\n' % model
    )
    r = subprocess.run(["bash", "-c", script.replace("\\n", "\n")], capture_output=True, text=True)
    return r.stdout, (snapshot_file.read_text() if snapshot_file.exists() else None)


_OK = '[{"model_id":"claude-test-9-9","execution_model_id":"claude-test-9-9"}]'


def test_shell_snapshot_normal_json(tmp_path):
    out, f = _sh(tmp_path, tmp_path / "snap", "echo SET; echo '%s'" % _OK, "claude-test-9-9")
    assert "refresh=0" in out and "claude-test-9-9\nauth=0" in out
    assert "claude-test-9-9" in f


def test_shell_snapshot_db_failure_keeps_previous(tmp_path):
    snap = tmp_path / "snap"
    out, f = _sh(tmp_path, snap, "echo 'conn refused'; return 1", "claude-test-9-9", preseed=_OK + "\n")
    assert "refresh=1" in out and "auth=0" in out and f == _OK + "\n"


def test_shell_snapshot_unparseable_keeps_previous(tmp_path):
    snap = tmp_path / "snap"
    out, f = _sh(tmp_path, snap, "echo 'ERROR: garbage{'", "claude-test-9-9", preseed=_OK + "\n")
    assert "refresh=1" in out and "auth=0" in out and f == _OK + "\n"
    out, f = _sh(tmp_path, snap, "echo '{\"a\":1}'", "claude-test-9-9")  # not an array
    assert "refresh=1" in out and f == _OK + "\n"


def test_shell_snapshot_empty_array_blocks(tmp_path):
    snap = tmp_path / "snap"
    out, f = _sh(tmp_path, snap, "echo '[]'", "opus", preseed=_OK + "\n")
    assert "refresh=0" in out and "auth=2" in out
    assert f.strip() == "[]"


def test_shell_no_snapshot_and_failure_uses_code_fallback(tmp_path):
    snap = tmp_path / "snap"
    out, f = _sh(tmp_path, snap, "return 1", "opus")
    assert "refresh=1" in out and "auth=0" in out and contract.resolve_model("opus") in out
    assert f is None
