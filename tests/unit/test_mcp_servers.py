"""
mcp_servers/ 단위 테스트 — Filesystem/Git/Memory 서버 로직 검증.
FastMCP 서버 기동 없이 개별 도구 함수 직접 테스트.
"""
import pytest
import sys
import os
import tempfile
import shutil
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "../.."))


# ── Filesystem 서버 ───────────────────────────────────────────────
class TestFilesystemServer:
    @pytest.fixture(autouse=True)
    def setup_sandbox(self, tmp_path):
        """임시 sandbox root 설정."""
        import mcp_servers.filesystem_server as fs_module
        self._orig_root = fs_module.SANDBOXED_ROOT
        fs_module.SANDBOXED_ROOT = tmp_path
        yield
        fs_module.SANDBOXED_ROOT = self._orig_root

    def test_write_and_read_file(self, tmp_path):
        import mcp_servers.filesystem_server as fs_module
        content = "Hello AADS!"
        result = fs_module.write_file("test.txt", content)
        assert "저장 완료" in result

        read_back = fs_module.read_file("test.txt")
        assert read_back == content

    def test_read_nonexistent_raises(self):
        import mcp_servers.filesystem_server as fs_module
        with pytest.raises(FileNotFoundError):
            fs_module.read_file("nonexistent.txt")

    def test_list_directory_empty(self):
        import mcp_servers.filesystem_server as fs_module
        entries = fs_module.list_directory(".")
        assert isinstance(entries, list)

    def test_list_directory_with_files(self, tmp_path):
        import mcp_servers.filesystem_server as fs_module
        fs_module.write_file("a.txt", "A")
        fs_module.write_file("b.txt", "B")
        entries = fs_module.list_directory(".")
        assert "a.txt" in entries
        assert "b.txt" in entries

    def test_create_directory(self):
        import mcp_servers.filesystem_server as fs_module
        result = fs_module.create_directory("subdir/nested")
        assert "생성 완료" in result

    def test_delete_file(self, tmp_path):
        import mcp_servers.filesystem_server as fs_module
        fs_module.write_file("to_delete.txt", "data")
        result = fs_module.delete_file("to_delete.txt")
        assert "삭제 완료" in result
        with pytest.raises(FileNotFoundError):
            fs_module.read_file("to_delete.txt")

    def test_path_traversal_blocked(self):
        import mcp_servers.filesystem_server as fs_module
        with pytest.raises(ValueError, match="경로 이탈"):
            fs_module._safe_path("../../etc/passwd")

    def test_file_info(self, tmp_path):
        import mcp_servers.filesystem_server as fs_module
        fs_module.write_file("info_test.txt", "content here")
        info = fs_module.file_info("info_test.txt")
        assert info["is_file"] is True
        assert info["size"] > 0
        assert "modified_at" in info


# ── Memory 서버 ───────────────────────────────────────────────────
class TestMemoryServer:
    @pytest.fixture(autouse=True)
    def clear_store(self):
        """각 테스트 전 메모리 초기화."""
        import mcp_servers.memory_server as mem
        mem._store.clear()
        yield
        mem._store.clear()

    def test_store_and_retrieve(self):
        import mcp_servers.memory_server as mem
        mem.memory_store("key1", "value1")
        assert mem.memory_retrieve("key1") == "value1"

    def test_retrieve_nonexistent(self):
        import mcp_servers.memory_server as mem
        result = mem.memory_retrieve("missing_key")
        assert "[없음]" in result

    def test_store_with_namespace(self):
        import mcp_servers.memory_server as mem
        mem.memory_store("key", "val_a", namespace="ns_a")
        mem.memory_store("key", "val_b", namespace="ns_b")
        assert mem.memory_retrieve("key", namespace="ns_a") == "val_a"
        assert mem.memory_retrieve("key", namespace="ns_b") == "val_b"

    def test_delete_key(self):
        import mcp_servers.memory_server as mem
        mem.memory_store("del_key", "will be deleted")
        mem.memory_delete("del_key")
        assert "[없음]" in mem.memory_retrieve("del_key")

    def test_list_keys(self):
        import mcp_servers.memory_server as mem
        mem.memory_store("alpha", "1")
        mem.memory_store("beta", "2")
        keys = mem.memory_list()
        assert "alpha" in keys
        assert "beta" in keys

    def test_search_by_value(self):
        import mcp_servers.memory_server as mem
        mem.memory_store("task1", "python fastapi development")
        mem.memory_store("task2", "react frontend work")
        results = mem.memory_search("fastapi")
        assert len(results) == 1
        assert results[0]["key"] == "task1"

    def test_search_by_key(self):
        import mcp_servers.memory_server as mem
        mem.memory_store("context_main", "main context data")
        results = mem.memory_search("context")
        assert any(r["key"] == "context_main" for r in results)

    def test_memory_clear(self):
        import mcp_servers.memory_server as mem
        mem.memory_store("a", "1")
        mem.memory_store("b", "2")
        mem.memory_clear()
        assert mem.memory_list() == []

    def test_namespaces_list(self):
        import mcp_servers.memory_server as mem
        mem.memory_store("k", "v", namespace="ns1")
        mem.memory_store("k", "v", namespace="ns2")
        namespaces = mem.memory_namespaces()
        assert "ns1" in namespaces
        assert "ns2" in namespaces

    def test_store_with_tags(self):
        import mcp_servers.memory_server as mem
        mem.memory_store("tagged", "data", tags=["python", "backend"])
        results = mem.memory_search("backend")
        assert len(results) == 1
        assert "backend" in results[0]["tags"]


# ── Git 서버 ──────────────────────────────────────────────────────
class TestGitServer:
    def test_run_git_invalid_path_blocked(self, tmp_path):
        """허용되지 않은 경로 접근 시 ValueError 발생."""
        import mcp_servers.git_server as git_mod
        self._orig_root = git_mod.ALLOWED_REPO_ROOT
        git_mod.ALLOWED_REPO_ROOT = tmp_path
        try:
            with pytest.raises(ValueError, match="허용되지 않은"):
                git_mod._run_git(["status"], cwd="/etc")
        finally:
            git_mod.ALLOWED_REPO_ROOT = self._orig_root

    def test_git_status_no_repo(self, tmp_path):
        """git repo 없는 경로에서 status 실행 — 에러 응답."""
        import mcp_servers.git_server as git_mod
        self._orig_root = git_mod.ALLOWED_REPO_ROOT
        git_mod.ALLOWED_REPO_ROOT = tmp_path
        try:
            result = git_mod.git_status(".")
            # 오류 메시지이거나 빈 문자열
            assert isinstance(result, str)
        finally:
            git_mod.ALLOWED_REPO_ROOT = self._orig_root

    def test_git_branches_no_repo(self, tmp_path):
        """git repo 없는 경로 → 빈 목록."""
        import mcp_servers.git_server as git_mod
        self._orig_root = git_mod.ALLOWED_REPO_ROOT
        git_mod.ALLOWED_REPO_ROOT = tmp_path
        try:
            result = git_mod.git_branches(".")
            assert isinstance(result, list)
        finally:
            git_mod.ALLOWED_REPO_ROOT = self._orig_root
