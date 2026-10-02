"""진아서버(jinah244) 신규 사용 중단(2026-10-02 CEO 결정) 회귀 테스트.

배포·동기화 기본 대상에서는 빠지고, 원장에는 '사용 중단' 으로 남으며,
운영 의존(ACCT DB 터널·OBYS 8210 터널)은 그대로여야 한다.
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


def test_registry_marks_jinah244_decommissioning_but_keeps_ledger():
    entry = SERVER_REGISTRY["jinah244"]
    assert entry["status"] == "decommissioning"
    assert entry["status_label"] == "사용 중단(이관 대기)"
    assert "사용 중단" in entry["display_name"]
    assert get_server_status("jinah244") == "decommissioning"
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
    tools_db = (ROOT / "app/api/ceo_chat_tools_db.py").read_text(encoding="utf-8")
    assert "get_server_host" in tools_db and "jinah244" in tools_db
