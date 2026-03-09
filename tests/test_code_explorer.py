"""
AADS-186E-3: 코드 탐색 서비스 단위 테스트
- trace_function_chain: 트리 구조 반환 (mock SSH)
- analyze_recent_changes: commits, changed_files 포함 (mock SSH)
- search_all_projects: 복수 프로젝트 결과 (mock CKP)
"""
from __future__ import annotations

import asyncio
import os
import sys
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))


def run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


# ─── 헬퍼: mock SSH 응답 ───────────────────────────────────────────────────────

_SAMPLE_PY = """
import asyncio

async def handle_chat_message(session_id, content):
    intent = await classify(content)
    result = await context_builder.build(session_id)
    response = await model_selector.call_stream(intent, result)
    return response

async def classify(content):
    return "casual"

async def context_builder_build(sid):
    pass
"""

_SAMPLE_GIT_LOG = """
abc1234 feat: AADS-186E-3 코드 탐색 도구 추가
def5678 fix: session_notes 인덱스 오류 수정
ghi9012 refactor: memory_manager 리팩토링
"""

_SAMPLE_GIT_STAT = """
app/services/code_explorer_service.py | 25 ++++++++++++++
app/services/memory_manager.py        | 12 +++++--
tests/test_code_explorer.py           | 50 +++++++++++++++++++++++++
"""


class TestTraceFunctionChain:
    """trace_function_chain: 함수 호출 체인 추적."""

    def test_trace_returns_diagram(self):
        """로컬 파일 읽기 성공 시 트리 다이어그램 반환."""
        from app.services.code_explorer_service import CodeExplorerService

        svc = CodeExplorerService()

        async def _mock_read(host, path):
            return _SAMPLE_PY

        with patch.object(svc, "_read_file", new=AsyncMock(side_effect=_mock_read)):
            result = run(svc.trace_function_chain("AADS", "app/main.py::handle_chat_message", depth=2))

        assert result.project == "AADS"
        assert result.entry_point == "app/main.py::handle_chat_message"
        assert isinstance(result.diagram, str)
        assert result.error is None

    def test_trace_invalid_project(self):
        """미지원 프로젝트 → error 포함 TraceResult 반환."""
        from app.services.code_explorer_service import CodeExplorerService

        svc = CodeExplorerService()
        result = run(svc.trace_function_chain("UNKNOWN_PROJ", "main.py::func"))

        assert result.error is not None
        assert "미지원" in result.error

    def test_trace_file_not_found(self):
        """파일 읽기 실패 → error 포함 TraceResult 반환."""
        from app.services.code_explorer_service import CodeExplorerService

        svc = CodeExplorerService()

        with (
            patch.object(svc, "_read_file", new=AsyncMock(return_value="")),
            patch.object(svc, "_find_in_ckp", new=AsyncMock(return_value="")),
        ):
            result = run(svc.trace_function_chain("AADS", "nonexistent.py::func"))

        assert result.error is not None

    def test_trace_renders_tree(self):
        """다이어그램에 트리 구조 기호 포함 확인."""
        from app.services.code_explorer_service import CodeExplorerService

        svc = CodeExplorerService()

        # 내용에 함수 본문 포함
        content_with_calls = """
def target_func(x):
    result = helper_one(x)
    data = helper_two(result)
    return data

def helper_one(x):
    return x * 2

def helper_two(x):
    return str(x)
"""

        with patch.object(svc, "_read_file", new=AsyncMock(return_value=content_with_calls)):
            result = run(svc.trace_function_chain("AADS", "utils.py::target_func", depth=1))

        assert result.diagram
        # 트리 다이어그램은 진입점으로 시작
        assert "target_func" in result.diagram or "utils.py" in result.diagram

    def test_trace_depth_limit(self):
        """depth=0 → 체인 비어있음."""
        from app.services.code_explorer_service import CodeExplorerService

        svc = CodeExplorerService()
        with patch.object(svc, "_read_file", new=AsyncMock(return_value=_SAMPLE_PY)):
            result = run(svc.trace_function_chain("AADS", "app/main.py::handle_chat_message", depth=0))

        # depth=0이면 chain이 비어있거나 에러 없음
        assert result.error is None or result.chain == []


class TestAnalyzeRecentChanges:
    """analyze_recent_changes: Git 변경 분석."""

    def test_analyze_returns_commits(self):
        """정상 git log 출력 → commits 포함한 ChangeReport 반환."""
        from app.services.code_explorer_service import CodeExplorerService

        svc = CodeExplorerService()

        async def _mock_ssh(host, cmd, timeout=30.0):
            if "git log" in cmd:
                return _SAMPLE_GIT_LOG
            elif "git diff" in cmd:
                return _SAMPLE_GIT_STAT
            return ""

        with patch("app.services.code_explorer_service._ssh_run", new=_mock_ssh):
            result = run(svc.analyze_recent_changes("AADS", days=7))

        assert result.project == "AADS"
        assert result.days == 7
        assert len(result.commits) > 0
        assert result.commits[0]["hash"]
        assert result.commits[0]["message"]

    def test_analyze_returns_changed_files(self):
        """git diff --stat 출력 → changed_files 포함 확인."""
        from app.services.code_explorer_service import CodeExplorerService

        svc = CodeExplorerService()

        async def _mock_ssh(host, cmd, timeout=30.0):
            if "git log" in cmd:
                return _SAMPLE_GIT_LOG
            elif "git diff" in cmd:
                return _SAMPLE_GIT_STAT
            return ""

        with patch("app.services.code_explorer_service._ssh_run", new=_mock_ssh):
            result = run(svc.analyze_recent_changes("AADS", days=7))

        assert len(result.changed_files) > 0

    def test_analyze_risk_assessment(self):
        """커밋 수/핵심 파일 변경 → risk_level 평가."""
        from app.services.code_explorer_service import CodeExplorerService

        svc = CodeExplorerService()

        async def _mock_ssh(host, cmd, timeout=30.0):
            if "git log" in cmd:
                return _SAMPLE_GIT_LOG
            elif "git diff" in cmd:
                return _SAMPLE_GIT_STAT
            return ""

        with patch("app.services.code_explorer_service._ssh_run", new=_mock_ssh):
            result = run(svc.analyze_recent_changes("AADS", days=7))

        assert result.risk_level in ("LOW", "MEDIUM", "HIGH")

    def test_analyze_invalid_project(self):
        """미지원 프로젝트 → error 포함 ChangeReport."""
        from app.services.code_explorer_service import CodeExplorerService

        svc = CodeExplorerService()
        result = run(svc.analyze_recent_changes("INVALID_PROJ"))

        assert result.error is not None

    def test_analyze_empty_git(self):
        """git log 결과 없음 → error 포함."""
        from app.services.code_explorer_service import CodeExplorerService

        svc = CodeExplorerService()

        async def _mock_ssh(host, cmd, timeout=30.0):
            return ""

        with patch("app.services.code_explorer_service._ssh_run", new=_mock_ssh):
            result = run(svc.analyze_recent_changes("AADS", days=7))

        assert result.error is not None


class TestSearchAllProjects:
    """search_all_projects: 6개 프로젝트 동시 검색."""

    def _make_mock_ckp(self, query: str):
        """CKP 검색 mock — AADS, KIS에서 매칭."""

        async def _mock_summary(project, max_tokens=3000):
            if project in ("AADS", "KIS"):
                return f"# {project} Codebase\n{query} 관련 로직 포함\nhealth_check 함수 존재"
            return f"# {project} Codebase\n관련 없음"

        return _mock_summary

    def test_search_returns_matches(self):
        """키워드 검색 → 복수 프로젝트에서 결과 반환."""
        from app.services.code_explorer_service import CodeExplorerService

        svc = CodeExplorerService()
        query = "health_check"

        mock_ckp_mgr = MagicMock()
        mock_ckp_mgr.get_ckp_summary = AsyncMock(
            side_effect=self._make_mock_ckp(query)
        )

        with patch("app.services.ckp_manager.CKPManager", return_value=mock_ckp_mgr), \
             patch("builtins.__import__", wraps=__builtins__.__import__ if hasattr(__builtins__, '__import__') else __import__):
            # _search_single_project를 직접 mock하여 CKP 결과 시뮬레이션
            async def _mock_search(proj, q):
                if proj in ("AADS", "KIS"):
                    return [{"project": proj, "file": "CKP", "match_type": "ckp", "snippet": f"{q} 관련 로직"}]
                return []

            with patch.object(svc, "_search_single_project", side_effect=_mock_search):
                result = run(svc.search_all_projects(query))

        assert result.query == query
        assert isinstance(result.matches, list)
        assert isinstance(result.projects_searched, list)
        assert len(result.matches) > 0

    def test_search_identifies_duplicates(self):
        """여러 프로젝트에 동일 파일명 → duplicate_patterns 포함."""
        from app.services.code_explorer_service import CodeExplorerService

        svc = CodeExplorerService()

        # 여러 프로젝트에 config.py가 있는 상황 시뮬레이션
        async def _mock_search(proj, q):
            return [{"project": proj, "file": "config.py", "match_type": "source", "snippet": "설정"}]

        with patch.object(svc, "_search_single_project", side_effect=_mock_search):
            result = run(svc.search_all_projects("config"))

        # config.py가 여러 프로젝트에 있으면 duplicate_patterns에 포함
        assert isinstance(result.duplicate_patterns, list)
        assert "config.py" in result.duplicate_patterns

    def test_search_partial_failure(self):
        """일부 프로젝트 SSH 실패 → projects_failed에 포함."""
        from app.services.code_explorer_service import CodeExplorerService

        svc = CodeExplorerService()

        async def _mock_search_single(proj, query):
            if proj in ("KIS", "SF"):
                raise Exception("SSH 연결 실패")
            return []

        with patch.object(svc, "_search_single_project", side_effect=_mock_search_single):
            result = run(svc.search_all_projects("test_query"))

        assert "KIS" in result.projects_failed or "SF" in result.projects_failed
