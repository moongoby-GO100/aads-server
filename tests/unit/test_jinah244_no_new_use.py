"""진아서버(jinah244) 신규 사용 중단(2026-10-02 CEO 결정) 회귀 테스트.

배포·동기화 기본 대상에서는 빠지고, 원장에는 '사용 중단' 으로 남으며,
폐기 상태에서도 기존 호스트 조회 호환성을 보존한다.
"""
from __future__ import annotations

import subprocess
from pathlib import Path

from app.services.server_registry import (
    CANONICAL_SERVER_IDS,
    LEDGER_SERVER_IDS,
    SERVER_REGISTRY,
    get_server_host,
    get_server_status,
    is_server_decommissioning,
    list_ledger_servers,
)

ROOT = Path(__file__).resolve().parents[2]
JINAH_IP = "5.104.85.244"


def _function_body(text: str, name: str) -> str:
    lines = text.splitlines()
    start = next(i for i, ln in enumerate(lines) if ln.startswith(f"{name}()"))
    end = next(i for i in range(start + 1, len(lines)) if lines[i] == "}")
    return "\n".join(lines[start : end + 1])


def test_lease_clients_default_targets_exclude_jinah244():
    text = (ROOT / "scripts/deploy_lease_clients.sh").read_text(encoding="utf-8")
    line = next(ln for ln in text.splitlines() if ln.startswith("TARGETS="))
    assert JINAH_IP not in line
    assert "5.104.86.14" in line and "114.207.244.86" in line


def test_runner_sync_default_targets_exclude_jinah244():
    text = (ROOT / "scripts/sync_pipeline_runner_remote.sh").read_text(encoding="utf-8")
    func = _function_body(text, "default_targets")
    out = subprocess.run(
        ["bash", "-c", f'SCRIPT_DIR=/s\n{func}\ndefault_targets'],
        capture_output=True, text=True, check=True,
    ).stdout
    names = [ln.split("|", 1)[0] for ln in out.splitlines() if ln.strip()]
    assert names == ["contabo14", "cafe24_114"]
    assert "jinah244" not in out


def test_runner_sync_leaves_restore_hint():
    text = (ROOT / "scripts/sync_pipeline_runner_remote.sh").read_text(encoding="utf-8")
    assert "2026-10-02" in text
    assert "AADS_RUNNER_SYNC_TARGETS" in _function_body(text, "default_targets")


def test_registry_marks_jinah244_retired_but_keeps_ledger():
    entry = SERVER_REGISTRY["jinah244"]
    assert entry["status"] == "retired"
    assert entry["status_label"] == "사용 중단(폐기)"
    assert "사용 중단" in entry["display_name"]
    assert get_server_status("jinah244") == "retired"
    assert is_server_decommissioning("244")
    assert not is_server_decommissioning("contabo116")
    assert get_server_status("contabo14") == "active"


def test_jinah244_stays_in_ledger_and_host_lookup():
    assert "jinah244" in LEDGER_SERVER_IDS
    assert "jinah244" in [s["id"] for s in list_ledger_servers()]
    assert get_server_host("jinah244") == JINAH_IP
    assert "jinah244" not in CANONICAL_SERVER_IDS


def test_codex_oauth_docstring_no_longer_claims_jinah_server_is_source():
    import app.core.codex_oauth as mod

    doc = mod.__doc__ or ""
    assert "정본이고 이쪽은 사본" not in doc
    assert "계정 이름" in doc
    assert "사용 중단" in doc


def test_codex_auth_sync_marks_jinah244_decommissioned():
    text = (ROOT / "scripts/codex_auth_sync.sh").read_text(encoding="utf-8")
    assert "사용 중단" in text


def test_live_paths_untouched():
    nginx = ROOT / "nginx-aads-upstream.conf"
    if nginx.exists():
        assert "8210" in nginx.read_text(encoding="utf-8")
    from app.api import ceo_chat_tools_db as db

    assert callable(db._ensure_ssh_tunnel)
    assert callable(db.validate_query)
    assert db.validate_query("DELETE FROM journal_entry")
    assert db.validate_query("SELECT 1") is None


def test_acct_without_explicit_host_never_starts_tunnel(monkeypatch):
    import pytest
    from app.api import ceo_chat_tools_db as db

    monkeypatch.setitem(db._SSH_TUNNEL_PROJECTS, "ACCT", {**db._SSH_TUNNEL_PROJECTS["ACCT"], "ssh_host": ""})
    def unexpected(*args, **kwargs):
        pytest.fail("unconfigured ACCT must not create or reuse a tunnel")
    monkeypatch.setattr(db.subprocess, "Popen", unexpected)
    monkeypatch.setattr(db, "_cleanup_idle_ssh_tunnels", unexpected)
    with pytest.raises(ValueError, match="ACCT_SSH_HOST"):
        db._ensure_ssh_tunnel("ACCT")


def test_acct_config_defaults_and_override_precedence(monkeypatch):
    import ast
    from app.api import ceo_chat_tools_db as db

    # Evaluate the actual import-time configuration with isolated environment values.
    tree = ast.parse((ROOT / "app/api/ceo_chat_tools_db.py").read_text())
    node = next(n for n in tree.body if isinstance(n, ast.AnnAssign)
                and getattr(n.target, "id", "") == "_SSH_TUNNEL_PROJECTS")
    def config():
        return eval(compile(ast.Expression(node.value), "tunnel-config", "eval"), vars(db))
    for key in list(db.os.environ):
        if key.startswith(("ACCT_", "SF_", "NTV2_", "SSH_")):
            monkeypatch.delenv(key)
    defaults = config()
    assert defaults["ACCT"]["ssh_host"] == ""
    assert defaults["SF"]["ssh_host"] == defaults["NTV2"]["ssh_host"] == get_server_host("cafe24_114")
    assert defaults["SF"]["remote_port"] == 3306
    assert defaults["NTV2"]["remote_port"] == 3307
    monkeypatch.setenv("SSH_HOST", "global.example")
    monkeypatch.setenv("SSH_ACCT_HOST", "legacy.example")
    monkeypatch.setenv("ACCT_SSH_HOST", "explicit.example")
    monkeypatch.setenv("ACCT_SSH_PORT", "2222")
    monkeypatch.setenv("ACCT_DB_HOST", "db.internal")
    monkeypatch.setenv("ACCT_DB_PORT", "15432")
    acct = config()["ACCT"]
    assert (acct["ssh_host"], acct["ssh_port"], acct["remote_host"], acct["remote_port"]) == ("explicit.example", 2222, "db.internal", 15432)
    monkeypatch.delenv("ACCT_SSH_HOST")
    assert config()["ACCT"]["ssh_host"] == "legacy.example"
    monkeypatch.delenv("SSH_ACCT_HOST")
    assert config()["ACCT"]["ssh_host"] == "global.example"


def test_current_tool_descriptions_use_cafe24_acct():
    text = (ROOT / "app/api/ceo_chat_tools.py").read_text()
    assert "ACCT→cafe24_114" in text
    assert "jinah244" not in text
