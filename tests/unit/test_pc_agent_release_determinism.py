"""Cross-platform ZIP determinism without requiring a Windows runner."""
import hashlib
import io
import json
import zipfile
from pathlib import Path

from pc_agent import release_archive as release


def test_source_names_sort_identically_with_windows_native_path_order(monkeypatch, tmp_path):
    names = ["agent.py", "VERSION", "_main.py", "Zhelper.py"]
    for name in names:
        (tmp_path / name).write_bytes(name.encode())
    expected = sorted(names)
    assert release._source_files(tmp_path) == expected

    class WindowsSortedPath:
        """Emulate PureWindowsPath's case-insensitive comparison on Linux."""
        def __init__(self, path):
            self.path = path

        def __getattr__(self, name):
            return getattr(self.path, name)

        def __lt__(self, other):
            return str(self.path).casefold() < str(other.path).casefold()

    paths = [WindowsSortedPath(tmp_path / name) for name in names]
    assert [path.name for path in sorted(paths)] != expected
    monkeypatch.setattr(Path, "rglob", lambda self, pattern: iter(paths))
    assert release._source_files(tmp_path) == expected


def test_zip_digest_and_ticket_metadata_ignore_platform_zipinfo_defaults(monkeypatch, tmp_path):
    (tmp_path / "VERSION").write_bytes(b"1.0.2\n")
    (tmp_path / "agent.py").write_bytes(b"# release\n")
    archive = release.build_agent_zip(tmp_path)
    ticketed = release.ticketed_agent_zip(tmp_path, "test-ticket")
    assert release.build_agent_zip(tmp_path, "test-ticket") == ticketed
    original_zip_info = zipfile.ZipInfo

    class WindowsZipInfo(original_zip_info):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            self.create_system = 0  # CPython Windows default.

    monkeypatch.setattr(zipfile, "ZipInfo", WindowsZipInfo)
    assert release.build_agent_zip(tmp_path) == archive
    assert release.build_agent_zip(tmp_path, "test-ticket") == ticketed
    assert release.ticketed_agent_zip(tmp_path, "test-ticket") == ticketed
    with zipfile.ZipFile(io.BytesIO(ticketed)) as zipped:
        assert all(info.create_system == 3 for info in zipped.infolist())
        assert all(info.external_attr == 0o644 << 16 for info in zipped.infolist())


def test_manifest_written_on_unix_verifies_with_windows_zipinfo(monkeypatch, tmp_path):
    (tmp_path / "VERSION").write_bytes(b"1.0.2\n")
    (tmp_path / "agent.py").write_bytes(b"# release\n")
    digest = hashlib.sha256(release.build_agent_zip(tmp_path)).hexdigest()
    (tmp_path / release.MANIFEST_NAME).write_text(json.dumps({
        "version": "1.0.2", "files": release._source_files(tmp_path), "sha256": digest,
    }), encoding="utf-8")
    original_zip_info = zipfile.ZipInfo

    class WindowsZipInfo(original_zip_info):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            self.create_system = 0

    monkeypatch.setattr(zipfile, "ZipInfo", WindowsZipInfo)
    assert release.release_sha256(tmp_path) == digest
