from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.api.user_project_servers import ProjectServerCreate, _row_to_out, _safe_metadata, _validate_host


def test_project_server_create_accepts_jsw_contabo_host() -> None:
    body = ProjectServerCreate(
        label="ACCT dev server",
        host="5.104.85.244",
        ssh_user="partner",
        workspace_id="957b26ba-d59c-4904-84ae-b31b3e958699",
        project_key="acct",
        metadata={"secret_storage": "pc_agent_or_agent_vault"},
    )

    assert body.host == "5.104.85.244"
    assert body.ssh_user == "partner"
    assert body.project_key == "acct"


def test_project_server_rejects_secret_metadata_keys() -> None:
    with pytest.raises(ValidationError):
        ProjectServerCreate(host="5.104.85.244", metadata={"password": "do-not-store"})


def test_validate_host_rejects_shell_payloads() -> None:
    with pytest.raises(ValueError):
        _validate_host("5.104.85.244; curl bad")


def test_safe_metadata_blocks_api_key_alias() -> None:
    with pytest.raises(ValueError):
        _safe_metadata({"api_key": "do-not-store"})


def test_row_to_out_accepts_jsonb_string_metadata() -> None:
    class Row(dict):
        def __getitem__(self, key: str):
            return super().__getitem__(key)

    row = Row(
        id="85534b1b-c29f-460b-ab2d-8b5f0ec4ac1c",
        label="ACCT dev server",
        host="5.104.85.244",
        ssh_user="partner",
        ssh_port=22,
        auth_type="ssh_key",
        status="active",
        connection_state="reachable",
        workspace_id=None,
        project_key="ACCT",
        metadata='{"secret_storage":"pc_agent_or_agent_vault"}',
        last_checked_at=None,
        created_at=type("Dt", (), {"isoformat": lambda self: "2026-09-02T15:52:50+09:00"})(),
        updated_at=type("Dt", (), {"isoformat": lambda self: "2026-09-02T15:52:50+09:00"})(),
    )

    assert _row_to_out(row)["metadata"] == {"secret_storage": "pc_agent_or_agent_vault"}
