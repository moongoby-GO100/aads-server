from __future__ import annotations

import json

import pytest

from app.services import prompt_compiler


class _AcquireCtx:
    def __init__(self, conn):
        self._conn = conn

    async def __aenter__(self):
        return self._conn

    async def __aexit__(self, exc_type, exc, tb):
        return False


class _Pool:
    def __init__(self, conn):
        self._conn = conn

    def acquire(self):
        return _AcquireCtx(self._conn)


class _CompilerConn:
    def __init__(self):
        self.execute_calls: list[tuple[str, tuple[object, ...]]] = []

    async def fetchval(self, query, *args):
        if "to_regclass" in query:
            table_name = str(args[0]).split(".")[-1]
            if table_name in {"prompt_assets", "session_blueprints", "compiled_prompt_provenance"}:
                return f"public.{table_name}"
            return None
        return None

    async def fetch(self, query, *args):
        if "FROM prompt_assets" in query:
            return [
                {
                    "slug": "system.intent_classifier",
                    "layer_id": 1,
                    "content": "BASE ASSET",
                    "model_variants": {
                        "gpt-5.4": {"content": "MODEL VARIANT CONTENT"},
                    },
                    "priority": 10,
                }
            ]
        return []

    async def fetchrow(self, query, *args):
        if "FROM session_blueprints" in query:
            return {
                "slug": "default.standard",
                "lite_mode": False,
                "skip_sections": ["legacy"],
                "extra_sections": ["system.intent_classifier"],
                "extra_skip_sections": [],
            }
        return None

    async def execute(self, query, *args):
        self.execute_calls.append((query, args))


@pytest.mark.asyncio
async def test_prompt_compiler_uses_base_prompt_when_governance_disabled(monkeypatch):
    async def _disabled(default=True):
        return False

    monkeypatch.setattr(prompt_compiler, "governance_enabled", _disabled)

    compiled = await prompt_compiler.PromptCompiler().compile(
        workspace_name="CEO",
        intent="report",
        model="gpt-5.4",
        session_id="00000000-0000-0000-0000-000000000001",
        base_system_prompt="BASE PROMPT",
    )

    assert compiled.system_prompt == "BASE PROMPT"
    assert compiled.provenance["fallback_used"] is True
    assert compiled.provenance["governance_enabled"] is False


@pytest.mark.asyncio
async def test_prompt_compiler_applies_assets_and_blueprint(monkeypatch):
    conn = _CompilerConn()

    async def _enabled(default=True):
        return True

    monkeypatch.setattr(prompt_compiler, "governance_enabled", _enabled)
    monkeypatch.setattr(prompt_compiler, "get_pool", lambda: _Pool(conn))

    compiled = await prompt_compiler.PromptCompiler().compile(
        workspace_name="CEO",
        intent="report",
        model="gpt-5.4",
        session_id="00000000-0000-0000-0000-000000000002",
        base_system_prompt="BASE PROMPT",
    )

    assert compiled.system_prompt.startswith("BASE PROMPT")
    assert "MODEL VARIANT CONTENT" in compiled.system_prompt
    assert compiled.provenance["governance_enabled"] is True
    assert compiled.provenance["applied_assets"][0]["slug"] == "system.intent_classifier"
    assert compiled.provenance["blueprint"]["slug"] == "default.standard"


@pytest.mark.asyncio
async def test_record_prompt_provenance_inserts_json_payload():
    conn = _CompilerConn()
    compiled = prompt_compiler.CompiledPrompt(
        system_prompt="compiled prompt body",
        provenance={
            "system_prompt_hash": "a" * 64,
            "system_prompt_chars": 20,
            "workspace": "CEO",
        },
    )

    await prompt_compiler.record_prompt_provenance(
        conn=conn,
        session_id="00000000-0000-0000-0000-000000000010",
        execution_id="00000000-0000-0000-0000-000000000011",
        intent="report",
        model="gpt-5.4",
        compiled_prompt=compiled,
    )

    assert conn.execute_calls
    _, args = conn.execute_calls[0]
    assert args[0] == "00000000-0000-0000-0000-000000000010"
    assert args[1] == "00000000-0000-0000-0000-000000000011"
    assert args[2] == "report"
    assert args[3] == "gpt-5.4"
    assert json.loads(args[6])["workspace"] == "CEO"
