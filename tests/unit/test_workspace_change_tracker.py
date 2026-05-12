from app.services.workspace_change_tracker import _is_ignored_change_path


def test_aads_runtime_state_files_are_ignored_change_paths():
    assert _is_ignored_change_path("AADS", "aads-server", ".active_container")
    assert _is_ignored_change_path("AADS", "aads-server", "/root/aads/aads-server/.active_port")


def test_non_runtime_files_are_not_ignored_change_paths():
    assert not _is_ignored_change_path("AADS", "aads-server", "app/services/chat_service.py")
    assert not _is_ignored_change_path("AADS", "aads-dashboard", ".active_port")
    assert not _is_ignored_change_path("NTV2", "newtalk-v2", ".active_port")
