"""파일 스킬 본문 로더·model-instructions 실행기·skill_load 도구 계약 (AADS-SKILL-INSTRUCTION-LOADER-20261028)."""
from __future__ import annotations

import asyncio
import hashlib
import inspect
import json
import logging
import os
from pathlib import Path

import pytest

os.environ.setdefault("JWT_SECRET_KEY", "unit-test-secret")

from app.services import ohvis_harness as harness  # noqa: E402
from app.services import skill_chat_tools as sct  # noqa: E402
from app.services import skill_instruction_loader as loader  # noqa: E402
from app.services.skill_file_import import sha256_file  # noqa: E402

_REPO = Path(__file__).resolve().parents[2]
TENANT = "00000000-0000-0000-0000-000000000001"
SKILL_ID = "11111111-1111-1111-1111-111111111111"
SESSION = "22222222-2222-2222-2222-222222222222"


def run(coro):
    return asyncio.run(coro)


def _write_skill(root: Path, slug: str, body: str, front: str = "name: x\nallowed-tools: Read\nstatus: candidate") -> Path:
    path = root / slug / "SKILL.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"---\n{front}\n---\n{body}", encoding="utf-8")
    return path


@pytest.fixture
def skill_root(tmp_path):
    root = tmp_path / ".claude" / "skills"
    root.mkdir(parents=True)
    return root


# ---------------------------------------------------------------- 로더


def test_loads_real_repo_skill_without_frontmatter():
    result = loader.load_skill_instructions("systematic-debugging")
    assert "error" not in result
    assert result["slug"] == "systematic-debugging"
    assert result["title"] == "체계적 디버깅"
    assert result["status"] == "candidate"
    assert result["risk_tier"] == "read"
    assert result["source"] == ".claude/skills/systematic-debugging/SKILL.md"
    assert result["allowed_tools"] == ["Read", "Grep", "Glob"]
    assert result["truncated"] is False
    assert not result["instructions"].startswith("---")
    assert "executor: model-instructions" not in result["instructions"]
    path = _REPO / ".claude/skills/systematic-debugging/SKILL.md"
    assert result["content_sha256"] == sha256_file(path)


def test_loaded_skill_matches_scan_candidate_fields():
    scanned = {c["slug"]: c for c in harness.scan_repository_skills()}["systematic-debugging"]
    loaded = loader.load_skill_instructions("systematic-debugging")
    assert loaded["risk_tier"] == scanned["risk_tier"]
    assert loaded["source"] == scanned["source"]
    assert "skill_load(slug)" in scanned["validation"][0]


@pytest.mark.parametrize("bad", ["../etc/passwd", "a/b", "a\\b", "Systematic-Debugging", "", " ", "-lead", "a" * 65,
                                 "ok\n", ".hidden", "a..b"])
def test_invalid_slugs_are_rejected(skill_root, bad):
    result = loader.load_skill_instructions(bad, roots=[skill_root])
    assert result["error"] == "invalid_slug"


def test_non_string_slug_is_rejected(skill_root):
    assert loader.load_skill_instructions(None, roots=[skill_root])["error"] == "invalid_slug"  # type: ignore[arg-type]


def test_symlinked_skill_dir_outside_root_is_rejected(tmp_path, skill_root):
    outside = tmp_path / "outside"
    _write_skill(outside, "evil", "비밀 본문")
    (skill_root / "evil").symlink_to(outside / "evil", target_is_directory=True)
    result = loader.load_skill_instructions("evil", roots=[skill_root])
    assert result["error"] == "skill_path_escape"
    assert "비밀" not in json.dumps(result, ensure_ascii=False)


def test_symlinked_skill_file_outside_root_is_rejected(tmp_path, skill_root):
    secret = tmp_path / "secret.md"
    secret.write_text("---\nname: x\n---\n비밀 본문", encoding="utf-8")
    (skill_root / "leak").mkdir()
    (skill_root / "leak" / "SKILL.md").symlink_to(secret)
    assert loader.load_skill_instructions("leak", roots=[skill_root])["error"] == "skill_path_escape"


def test_symlink_inside_root_is_allowed(skill_root):
    _write_skill(skill_root, "real-one", "# 진짜\n본문")
    (skill_root / "alias").symlink_to(skill_root / "real-one", target_is_directory=True)
    assert loader.load_skill_instructions("alias", roots=[skill_root])["title"] == "진짜"


def test_body_over_limit_is_truncated(skill_root):
    _write_skill(skill_root, "big", "# 큰 스킬\n" + "가" * (loader.MAX_INSTRUCTION_CHARS + 500))
    result = loader.load_skill_instructions("big", roots=[skill_root])
    assert result["truncated"] is True
    assert len(result["instructions"]) == loader.MAX_INSTRUCTION_CHARS


def test_body_at_limit_is_not_truncated(skill_root):
    _write_skill(skill_root, "edge", "x" * loader.MAX_INSTRUCTION_CHARS)
    result = loader.load_skill_instructions("edge", roots=[skill_root])
    assert result["truncated"] is False


def test_sha256_covers_full_file_even_when_truncated(skill_root):
    path = _write_skill(skill_root, "big", "y" * (loader.MAX_INSTRUCTION_CHARS + 10))
    result = loader.load_skill_instructions("big", roots=[skill_root])
    assert result["content_sha256"] == hashlib.sha256(path.read_bytes()).hexdigest()


def test_missing_slug_returns_not_found_with_up_to_five_candidates(skill_root):
    for name in ("systematic-debugging", "systematic-review", "debugging-guide", "test-driven", "writing-plans",
                 "debug-helper", "zzz"):
        _write_skill(skill_root, name, "본문")
    result = loader.load_skill_instructions("systematic-debug", roots=[skill_root])
    assert result["error"] == "skill_not_found"
    assert 1 <= len(result["candidates"]) <= 5
    assert "systematic-debugging" in result["candidates"]


def test_missing_slug_with_no_similar_skill_has_empty_candidates(skill_root):
    _write_skill(skill_root, "alpha", "본문")
    result = loader.load_skill_instructions("qqqqqqqq", roots=[skill_root])
    assert result == {"error": "skill_not_found", "message": result["message"], "candidates": []}


def test_second_root_is_searched(tmp_path, skill_root):
    codex = tmp_path / ".codex" / "skills"
    _write_skill(codex, "codex-only", "# 코덱스\n본문")
    result = loader.load_skill_instructions("codex-only", roots=[skill_root, codex])
    assert result["source"] == ".codex/skills/codex-only/SKILL.md"


def test_loader_has_no_execution_or_write_primitives():
    source = inspect.getsource(loader)
    for token in ("subprocess", "os.system", "exec(", "eval(", "write_text", "write_bytes", "open("):
        assert token not in source


# ---------------------------------------------------------------- model-instructions 실행기


def test_model_instructions_executor_is_registered_and_not_noop():
    assert harness._SKILL_EXECUTORS["model-instructions"] is harness._model_instructions_executor
    assert harness.skill_executability("model-instructions") == (True, None)
    assert "model-instructions" not in harness.NOOP_SKILL_EXECUTORS


def test_executor_returns_loader_result():
    out = harness._model_instructions_executor({"slug": "systematic-debugging"})
    assert out["slug"] == "systematic-debugging" and out["instructions"]


def test_executor_raises_on_unknown_slug():
    with pytest.raises(harness.SkillRegistryError) as exc:
        harness._model_instructions_executor({"slug": "no-such-skill-xyz"})
    assert exc.value.code == "skill_not_found"


def test_bind_registry_slug_injects_and_rejects_mismatch():
    assert harness.bind_registry_slug("model-instructions", "alpha", {}) == {"slug": "alpha"}
    assert harness.bind_registry_slug("model-instructions", "alpha", {"slug": "alpha"}) == {"slug": "alpha"}
    with pytest.raises(harness.SkillRegistryError) as exc:
        harness.bind_registry_slug("model-instructions", "alpha", {"slug": "beta"})
    assert exc.value.code == "skill_slug_mismatch"


def test_bind_registry_slug_does_not_touch_contract_echo():
    payload = {"slug": "anything", "x": 1}
    assert harness.bind_registry_slug("ohvis.contract-echo", "alpha", payload) == payload
    assert harness._contract_echo_executor(payload) == {"input": payload}


class _Conn:
    def __init__(self, row):
        self.row = row
        self.calls: list[str] = []

    def transaction(self):
        class _Tx:
            async def __aenter__(self_inner):
                return None

            async def __aexit__(self_inner, *exc):
                return False

        return _Tx()

    async def fetchrow(self, sql, *args):
        self.calls.append(sql)
        if "FROM ops_skill_library l JOIN ops_skill_versions v" in sql:
            return self.row
        if "INSERT INTO ops_skill_runs" in sql:
            return {"id": "run-1"}
        if "UPDATE ops_skill_runs" in sql:
            return {"id": "run-1", "status": "succeeded", "output": {}}
        return None

    async def execute(self, sql, *args):
        self.calls.append(sql)


def _model_instructions_row(slug="systematic-debugging"):
    schema = {"$schema": "https://json-schema.org/draft/2020-12/schema", "type": "object"}
    manifest = harness.validate_skill_manifest({
        "skill_id": SKILL_ID, "version": "1.0.0", "input_schema": schema, "output_schema": schema,
        "executor": "model-instructions", "allowed_tools": ["Read"], "timeout_seconds": 10,
        "retry": {"max_attempts": 1, "backoff_seconds": 0}, "idempotency": {"mode": "optional"},
        "preconditions": ["authenticated"], "postconditions": ["recorded"], "evidence": ["result_hash"],
        "risk_tier": "read", "status": "active", "provenance": {"source": "unit", "project": "AADS"},
    })
    return {"slug": slug, "risk_tier": "read", "version_id": "ver-1", "status": "active",
            "manifest": manifest, "content": harness._canonical_json(manifest),
            "content_sha256": harness._sha256(manifest)}


def _execute(monkeypatch, input_data):
    import app.core.db_pool as db_pool

    conn = _Conn(_model_instructions_row())

    class _Pool:
        def acquire(self):
            class _A:
                async def __aenter__(self_inner):
                    return conn

                async def __aexit__(self_inner, *exc):
                    return False

            return _A()

    monkeypatch.setattr(db_pool, "get_pool", lambda: _Pool())
    intent = sct._build_intent(TENANT, SESSION, {"skill_id": SKILL_ID, "version": "1.0.0", "input": input_data}, "c-1")
    result = run(harness.execute_skill(
        tenant_id=TENANT, skill_id=SKILL_ID, version="1.0.0", input_data=input_data,
        idempotency_key=None, action_intent=intent))
    return result, conn


def test_execute_skill_rejects_input_slug_that_differs_from_registry_row(monkeypatch):
    seen: list[str] = []
    monkeypatch.setattr(loader, "load_skill_instructions", lambda slug, **kw: seen.append(slug) or {"slug": slug})
    with pytest.raises(harness.SkillRegistryError) as exc:
        _execute(monkeypatch, {"slug": "writing-plans"})
    assert exc.value.code == "skill_slug_mismatch"
    assert seen == []


def test_execute_skill_runs_executor_with_registry_slug(monkeypatch):
    seen: list[str] = []
    monkeypatch.setattr(loader, "load_skill_instructions", lambda slug, **kw: seen.append(slug) or {"slug": slug})
    result, conn = _execute(monkeypatch, {})
    assert result["status"] == "succeeded"
    assert seen == ["systematic-debugging"]
    assert any("INSERT INTO ops_skill_runs" in sql for sql in conn.calls)


# ---------------------------------------------------------------- skill_load 도구


def test_skill_load_tool_returns_loader_result_and_usage_note(caplog):
    with caplog.at_level(logging.INFO, logger="app.services.skill_chat_tools"):
        result = run(sct.skill_load(inp={"slug": "systematic-debugging"}, session_id=SESSION))
    assert result["slug"] == "systematic-debugging" and result["instructions"]
    assert "R-AUTH" in result["usage_note"] and "R-DOCKER" in result["usage_note"]
    record = next(r for r in caplog.records if r.getMessage().startswith("skill_load_used"))
    payload = json.loads(record.getMessage().split(" ", 1)[1])
    assert payload == {"skill_load_used": True, "slug": "systematic-debugging",
                       "content_sha256": result["content_sha256"], "session_id": SESSION}


def test_skill_load_tool_errors_are_returned_without_usage_log(caplog):
    with caplog.at_level(logging.INFO, logger="app.services.skill_chat_tools"):
        assert run(sct.skill_load(inp={}))["error"] == "invalid_input"
        assert run(sct.skill_load(inp={"slug": "../x"}))["error"] == "invalid_slug"
        missing = run(sct.skill_load(inp={"slug": "no-such-skill-xyz"}))
    assert missing["error"] == "skill_not_found" and isinstance(missing["candidates"], list)
    assert not [r for r in caplog.records if "skill_load_used" in r.getMessage()]


def test_skill_load_is_registered_dispatched_and_read_only():
    from app.services import plan_mode_guard, tool_executor, tool_registry

    assert "skill_load" in tool_registry._TOOLS
    assert "skill_load" in tool_registry._GROUPS["action"]
    assert "skill_load" in tool_executor._DATABASE_TOOLS
    assert hasattr(tool_executor.ToolExecutor, "_skill_load")
    assert '"skill_load":' in inspect.getsource(tool_executor.ToolExecutor._dispatch)
    assert "skill_load" in plan_mode_guard._READ_ONLY_TOOLS
    assert tool_registry._TOOLS["skill_load"]["input_schema"]["required"] == ["slug"]


def test_skill_load_handler_works_without_tenant_binding():
    from app.services import tool_executor as te

    result = run(te.ToolExecutor()._skill_load({"slug": "systematic-debugging"}))
    assert result["slug"] == "systematic-debugging"
