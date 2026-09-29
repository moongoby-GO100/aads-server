"""PC Agent v1.0.56 release guard regressions."""

from __future__ import annotations

import importlib.util
import hashlib
import io
import json
import zipfile
from pathlib import Path
from types import SimpleNamespace

import pytest


ROOT = Path(__file__).resolve().parents[2]


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_launcher_updater_blocks_server_downgrade() -> None:
    updater = _load("launcher_updater_test", ROOT / "pc_agent" / "updater.py")

    assert updater._is_remote_newer("1.0.55", "1.0.52") is True
    assert updater._is_remote_newer("1.0.50", "1.0.52") is False
    assert updater._is_remote_newer("1.0.52", "1.0.52") is False
    assert updater._is_remote_newer("unknown", "1.0.52") is False


def _update_fixture(monkeypatch, tmp_path):
    updater = _load("launcher_update_install_test", ROOT / "pc_agent" / "updater.py")
    install = tmp_path / "install"
    agent = install / "agent"
    agent.mkdir(parents=True)
    (agent / "agent.py").write_text("current", encoding="utf-8")
    monkeypatch.setattr(updater, "INSTALL_DIR", install)
    monkeypatch.setattr(updater, "AGENT_DIR", agent)
    return updater, install, agent


def _zip_payload() -> bytes:
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w") as archive:
        archive.writestr("agent.py", "new")
        archive.writestr("VERSION", "1.0.2")
    return output.getvalue()


def test_preinstall_validation_failure_preserves_current_agent_with_stale_backup(monkeypatch, tmp_path):
    updater, install, agent = _update_fixture(monkeypatch, tmp_path)
    stale = install / "agent_backup"
    stale.mkdir()
    (stale / "agent.py").write_text("stale", encoding="utf-8")
    digest = hashlib.sha256(b"not-a-zip").hexdigest()
    monkeypatch.setattr(updater, "_api_get", lambda path, token="": (
        json.dumps({"version": "1.0.2", "zip_sha256": digest}).encode()
        if path.endswith("/version") else b"not-a-zip"))

    with pytest.raises(ValueError, match="ZIP이 아닌"):
        updater.download_update({}, "1.0.2")
    assert (agent / "agent.py").read_text() == "current"
    assert (stale / "agent.py").read_text() == "stale"


def test_hash_mismatch_stops_install(monkeypatch, tmp_path):
    updater, _install, agent = _update_fixture(monkeypatch, tmp_path)
    monkeypatch.setattr(updater, "_api_get", lambda path, token="": (
        json.dumps({"version": "1.0.2", "zip_sha256": "0" * 64}).encode()
        if path.endswith("/version") else _zip_payload()))

    with pytest.raises(ValueError, match="SHA256"):
        updater.download_update({}, "1.0.2")
    assert (agent / "agent.py").read_text() == "current"


def test_only_backup_created_by_this_attempt_is_restored(monkeypatch, tmp_path):
    updater, install, agent = _update_fixture(monkeypatch, tmp_path)
    stale = install / "agent_backup"
    stale.mkdir()
    (stale / "agent.py").write_text("stale", encoding="utf-8")
    payload = _zip_payload()
    digest = hashlib.sha256(payload).hexdigest()
    monkeypatch.setattr(updater, "_api_get", lambda path, token="": (
        json.dumps({"version": "1.0.2", "zip_sha256": digest}).encode()
        if path.endswith("/version") else payload))

    def partial_copy(_source, target):
        target.mkdir()
        (target / "agent.py").write_text("partial", encoding="utf-8")
        raise OSError("copy failed")

    monkeypatch.setattr(updater.shutil, "copytree", partial_copy)
    with pytest.raises(OSError, match="copy failed"):
        updater.download_update({}, "1.0.2")
    assert (agent / "agent.py").read_text() == "current"
    assert not stale.exists()


def test_release_manifest_matches_checkout_and_rejects_source_drift(tmp_path):
    from pc_agent.release_archive import _source_files, build_agent_zip, release_sha256

    assert len(release_sha256(ROOT / "pc_agent")) == 64
    (tmp_path / "VERSION").write_text("1.0.2", encoding="utf-8")
    (tmp_path / "agent.py").write_text("original", encoding="utf-8")
    digest = hashlib.sha256(build_agent_zip(tmp_path)).hexdigest()
    (tmp_path / "RELEASE_ZIP_SHA256.json").write_text(
        json.dumps({"version": "1.0.2", "files": _source_files(tmp_path), "sha256": digest}), encoding="utf-8"
    )
    assert release_sha256(tmp_path) == digest
    (tmp_path / "agent.py").write_text("changed", encoding="utf-8")
    with pytest.raises(ValueError, match="differs"):
        release_sha256(tmp_path)


def test_release_archive_ignores_unregistered_runtime_files_and_caches(tmp_path):
    from pc_agent.release_archive import cached_agent_zip, ticketed_agent_zip

    (tmp_path / "VERSION").write_text("1.0.2", encoding="utf-8")
    (tmp_path / "agent.py").write_text("release", encoding="utf-8")
    (tmp_path / "RELEASE_ZIP_SHA256.json").write_text(json.dumps({
        "version": "1.0.2", "files": ["VERSION", "agent.py"], "sha256": "0" * 64
    }), encoding="utf-8")
    first, digest = cached_agent_zip(tmp_path)
    (tmp_path / "runtime.log").write_text("runtime", encoding="utf-8")
    assert cached_agent_zip(tmp_path)[0] is first
    assert hashlib.sha256(first).hexdigest() == digest
    assert ticketed_agent_zip(tmp_path, "ticket") != first
    with zipfile.ZipFile(io.BytesIO(first)) as archive:
        assert "runtime.log" not in archive.namelist()
        assert all(item.compress_type == zipfile.ZIP_STORED for item in archive.infolist())


def test_legacy_server_without_digest_still_updates(monkeypatch, tmp_path):
    updater, _install, agent = _update_fixture(monkeypatch, tmp_path)
    payload = _zip_payload()
    monkeypatch.setattr(updater, "_api_get", lambda path, token="": (
        json.dumps({"version": "1.0.2"}).encode() if path.endswith("/version") else payload))
    updater.download_update({}, "1.0.2")
    assert (agent / "agent.py").read_text() == "new"


def test_agent_updater_blocks_server_downgrade() -> None:
    updater = _load(
        "agent_updater_test", ROOT / "pc_agent" / "commands" / "updater.py"
    )

    assert updater._is_remote_newer("1.0.55", "1.0.52") is True
    assert updater._is_remote_newer("1.0.50", "1.0.52") is False


def test_full_exit_confirmation_fails_closed(monkeypatch) -> None:
    tray = _load("tray_test", ROOT / "pc_agent" / "tray.py")
    monkeypatch.setattr(tray.sys, "platform", "win32")

    class BrokenWindll:
        @property
        def user32(self):
            raise RuntimeError("dialog unavailable")

    import ctypes

    monkeypatch.setattr(ctypes, "windll", BrokenWindll(), raising=False)
    assert tray.confirm_full_exit() is False


def test_full_exit_confirmation_handles_yes_and_no(monkeypatch) -> None:
    tray = _load("tray_yes_no_test", ROOT / "pc_agent" / "tray.py")
    monkeypatch.setattr(tray.sys, "platform", "win32")

    import ctypes

    yes_box = SimpleNamespace(MessageBoxW=lambda *_args: 6)
    monkeypatch.setattr(
        ctypes, "windll", SimpleNamespace(user32=yes_box), raising=False
    )
    assert tray.confirm_full_exit() is True

    no_box = SimpleNamespace(MessageBoxW=lambda *_args: 7)
    monkeypatch.setattr(
        ctypes, "windll", SimpleNamespace(user32=no_box), raising=False
    )
    assert tray.confirm_full_exit() is False


def test_tray_masks_agent_token() -> None:
    tray = _load("tray_mask_test", ROOT / "pc_agent" / "tray.py")

    assert tray._mask_secret("") == "미등록"
    assert tray._mask_secret("short") == tray._MASKED
    assert tray._mask_secret("abcd1234wxyz") == "abcd…wxyz"


def test_tray_has_safe_settings_and_manual_reconnect_menu() -> None:
    source = (ROOT / "pc_agent" / "tray.py").read_text(encoding="utf-8")

    assert "Item(\"재연결 시도\", request_reconnect)" in source
    assert "target=open_settings_window" in source
    assert "os.startfile(config_path)" not in source


def test_launcher_wires_manual_reconnect_callback() -> None:
    source = (ROOT / "pc_agent" / "launcher.py").read_text(encoding="utf-8")

    assert "reconnect_requested = threading.Event()" in source
    assert "def on_reconnect()" in source
    assert "manual-reconnect run_agent()" in source


def test_launcher_has_single_redownload_guard_definition() -> None:
    source = (ROOT / "pc_agent" / "launcher.py").read_text(encoding="utf-8")
    assert source.count("def _can_redownload()") == 1
    assert source.count("def _record_redownload()") == 1
    assert "disable_watchdog_for_user_exit()" in source


def test_release_publish_is_main_only() -> None:
    workflow_path = ROOT / ".github" / "workflows" / "build-pc-agent.yml"
    if not workflow_path.exists():
        # `.github` is excluded by .dockerignore, so this guard only runs on
        # the host / CI checkout where the workflow file is present.
        pytest.skip("workflow file not present in this checkout (.dockerignore)")

    workflow = workflow_path.read_text(encoding="utf-8")
    assert "python -m pc_agent.release_archive check" in workflow
    assert (
        "- name: Create/Update Release\n"
        "        if: github.ref == 'refs/heads/main'\n"
    ) in workflow
