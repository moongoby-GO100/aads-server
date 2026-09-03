from app.services.workspace_change_tracker import (
    _derive_change_owner,
    _is_ignored_change_path,
    _parse_porcelain_entries,
)


def test_aads_runtime_state_files_are_ignored_change_paths():
    assert _is_ignored_change_path("AADS", "aads-server", ".active_container")
    assert _is_ignored_change_path("AADS", "aads-server", "/root/aads/aads-server/.active_port")


def test_non_runtime_files_are_not_ignored_change_paths():
    assert not _is_ignored_change_path("AADS", "aads-server", "app/services/chat_service.py")
    assert not _is_ignored_change_path("AADS", "aads-dashboard", ".active_port")
    assert not _is_ignored_change_path("NTV2", "newtalk-v2", ".active_port")


def test_generated_runtime_files_are_ignored_change_paths():
    assert _is_ignored_change_path("AADS", "aads-server", "app/data/foo/events.jsonl")
    assert _is_ignored_change_path("AADS", "aads-dashboard", "tsconfig.tsbuildinfo")
    assert _is_ignored_change_path("AADS", "aads-server", "litellm-config.yaml.bak")


def test_parse_porcelain_entries_keeps_branch_status_and_rename_target():
    text = "\n".join([
        "## main...origin/main",
        " M app/services/workspace_change_tracker.py",
        "?? scripts/sync_workspace_change_ledger.py",
        "R  old.py -> new.py",
    ])

    entries = _parse_porcelain_entries(text)

    assert entries == [
        {
            "path": "app/services/workspace_change_tracker.py",
            "git_status": " M",
            "git_branch": "main",
        },
        {
            "path": "scripts/sync_workspace_change_ledger.py",
            "git_status": "??",
            "git_branch": "main",
        },
        {"path": "new.py", "git_status": "R ", "git_branch": "main"},
    ]


def test_derive_change_owner_prefers_explicit_then_session_then_tool():
    assert _derive_change_owner("session-1234567890", "tool", "owner-x") == "owner-x"
    assert _derive_change_owner("session-1234567890", "tool") == "chat:session-1234"
    assert _derive_change_owner("", "write_remote_file") == "tool:write_remote_file"
