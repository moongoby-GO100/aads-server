"""ops._resolve_build_hash — 배포 버전 기준 고정 해시."""
import os
import time

import pytest

from app.api import ops


@pytest.fixture
def clean(monkeypatch, tmp_path):
    monkeypatch.delenv("AADS_RELEASE_SHA", raising=False)
    monkeypatch.setattr(ops, "_RELEASE_SHA_FILE", str(tmp_path / "none"))
    return tmp_path


def test_env_first12(clean, monkeypatch):
    monkeypatch.setenv("AADS_RELEASE_SHA", "abcdef0123456789")
    assert ops._resolve_build_hash() == ("abcdef012345", "env")


@pytest.mark.parametrize("v", ["unknown", "local", "", "  "])
def test_env_skipped(clean, monkeypatch, v):
    monkeypatch.setenv("AADS_RELEASE_SHA", v)
    f = clean / "sha"
    f.write_text("1234567890abcdef\n")
    monkeypatch.setattr(ops, "_RELEASE_SHA_FILE", str(f))
    assert ops._resolve_build_hash() == ("1234567890ab", "file")


def test_stable_across_pid_and_time(clean, monkeypatch):
    first = ops._resolve_build_hash()
    monkeypatch.setattr(os, "getpid", lambda: 999999)
    monkeypatch.setattr(time, "time", lambda: 1.0)
    assert ops._resolve_build_hash() == first


def test_content_fallback_deterministic(clean, monkeypatch, tmp_path):
    app = tmp_path / "app"
    app.mkdir()
    (app / "a.py").write_text("x=1")
    monkeypatch.setattr(ops, "_APP_DIR", app)
    monkeypatch.setattr(ops, "_REPO_ROOT", tmp_path / "nogit")  # cwd 없음 → git 실패
    a = ops._resolve_build_hash()
    assert a[1] == "content" and len(a[0]) == 12
    assert ops._resolve_build_hash() == a


def test_all_fail_unknown(clean, monkeypatch, tmp_path):
    monkeypatch.setattr(ops, "_APP_DIR", tmp_path / "missing")
    monkeypatch.setattr(ops, "_REPO_ROOT", tmp_path / "nogit")
    assert ops._resolve_build_hash() == ("unknown", "unknown")


def test_file_source_from_tmp_path(clean, monkeypatch):
    f = clean / "release_sha"
    f.write_text("0123456789abcdef0123\n")
    monkeypatch.setattr(ops, "_RELEASE_SHA_FILE", str(f))
    assert ops._resolve_build_hash() == ("0123456789ab", "file")


def test_file_unknown_content_ignored(clean, monkeypatch):
    f = clean / "release_sha"
    f.write_text("unknown\n")
    monkeypatch.setattr(ops, "_RELEASE_SHA_FILE", str(f))
    assert ops._resolve_build_hash()[1] != "file"


def test_version_handler_returns_file_hash(clean, monkeypatch):
    import asyncio

    f = clean / "release_sha"
    f.write_text("fedcba9876543210\n")
    monkeypatch.setattr(ops, "_RELEASE_SHA_FILE", str(f))
    h, src = ops._resolve_build_hash()
    monkeypatch.setattr(ops, "_BUILD_HASH", h)
    monkeypatch.setattr(ops, "_BUILD_SOURCE", src)
    monkeypatch.setattr(os, "getpid", lambda: 4242)
    monkeypatch.setattr(time, "time", lambda: 2.0)
    body = asyncio.run(ops.get_version())
    assert body["build_hash"] == "fedcba987654"
    assert body["build_source"] == "file"
    assert {"git_sha", "timestamp"} <= set(body)
