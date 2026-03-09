"""
AADS-188B: 코드 인덱서 + 시맨틱 검색 테스트
- CodeIndexerService 청킹/임베딩/저장 단위 테스트
- SemanticCodeSearch 검색 단위 테스트 (ChromaDB mock)
"""
from __future__ import annotations

import asyncio
import sys
import os
import pytest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

# 프로젝트 루트 경로 설정
sys.path.insert(0, str(Path(__file__).parent.parent))

from app.services.code_indexer_service import CodeChunk, CodeIndexerService, IndexResult


# ─── 테스트 픽스처 ────────────────────────────────────────────────────────────

SAMPLE_PYTHON = """
import os
import logging

logger = logging.getLogger(__name__)


class HealthChecker:
    \"\"\"AADS 헬스체크 서비스.\"\"\"

    def __init__(self):
        self.endpoint = os.getenv("HEALTH_URL", "http://localhost:8080")

    async def check_health(self, server: str = "all") -> dict:
        \"\"\"서버 헬스체크 실행.\"\"\"
        if server == "all":
            return await self._check_all()
        return {"server": server, "status": "ok"}

    async def _check_all(self) -> dict:
        return {"status": "ok", "servers": ["68", "211", "114"]}


def classify_intent(message: str) -> str:
    \"\"\"인텐트 분류 함수.\"\"\"
    if "헬스" in message or "health" in message.lower():
        return "health_check"
    return "general"
"""

SAMPLE_TYPESCRIPT = """
import { useState } from 'react';

export interface ChatMessage {
  id: string;
  content: string;
}

export function useChatSession(workspaceId: string) {
  const [messages, setMessages] = useState<ChatMessage[]>([]);

  const sendMessage = async (content: string) => {
    const msg: ChatMessage = { id: Date.now().toString(), content };
    setMessages(prev => [...prev, msg]);
  };

  return { messages, sendMessage };
}

export class ChatService {
  private baseUrl: string;

  constructor(baseUrl: string) {
    this.baseUrl = baseUrl;
  }
}

const fetchMessages = async (sessionId: string) => {
  return fetch(`/api/messages/${sessionId}`);
};
"""

SAMPLE_BROKEN_PYTHON = "def foo(:\n    pass"


# ─── CodeChunk 테스트 ────────────────────────────────────────────────────────

class TestCodeChunk:
    def test_chunk_id_format(self):
        """chunk_id가 project__file__type__name__line 포맷인지 확인."""
        chunk = CodeChunk(
            project="AADS",
            file_path="app/services/health_checker.py",
            start_line=10,
            end_line=30,
            chunk_type="function",
            name="check_health",
            code="async def check_health(): pass",
        )
        cid = chunk.chunk_id
        assert "AADS" in cid
        assert "function" in cid
        assert "check_health" in cid
        assert "10" in cid

    def test_chunk_id_unique_for_different_lines(self):
        """같은 함수명이라도 라인 다르면 chunk_id 달라야 함."""
        c1 = CodeChunk(project="AADS", file_path="a.py", start_line=1, end_line=5,
                       chunk_type="function", name="foo", code="")
        c2 = CodeChunk(project="AADS", file_path="a.py", start_line=10, end_line=20,
                       chunk_type="function", name="foo", code="")
        assert c1.chunk_id != c2.chunk_id

    def test_text_for_embedding_contains_metadata(self):
        """text_for_embedding에 프로젝트, 파일, 함수명이 포함되어야 함."""
        chunk = CodeChunk(
            project="AADS",
            file_path="app/services/health_checker.py",
            start_line=10,
            end_line=30,
            chunk_type="function",
            name="check_health",
            code="async def check_health(): pass",
        )
        text = chunk.text_for_embedding
        assert "AADS" in text
        assert "check_health" in text
        assert "function" in text


# ─── CodeIndexerService 청킹 테스트 ─────────────────────────────────────────

class TestCodeIndexerChunking:
    def setup_method(self):
        self.svc = CodeIndexerService()

    def test_chunk_python_extracts_functions(self):
        """Python 파일에서 함수 청크 추출 확인."""
        chunks = self.svc.chunk_file(SAMPLE_PYTHON, "health_checker.py", "AADS")
        function_names = [c.name for c in chunks if c.chunk_type == "function"]
        # check_health, _check_all, classify_intent, HealthChecker.check_health, HealthChecker._check_all
        assert "check_health" in function_names or any("check_health" in n for n in function_names)
        assert "classify_intent" in function_names

    def test_chunk_python_extracts_class(self):
        """Python 파일에서 클래스 청크 추출 확인."""
        chunks = self.svc.chunk_file(SAMPLE_PYTHON, "health_checker.py", "AADS")
        class_names = [c.name for c in chunks if c.chunk_type == "class"]
        assert "HealthChecker" in class_names

    def test_chunk_python_methods_included(self):
        """클래스 메서드도 별도 청크로 추출되는지 확인."""
        chunks = self.svc.chunk_file(SAMPLE_PYTHON, "health_checker.py", "AADS")
        method_names = [c.name for c in chunks if c.chunk_type == "function"]
        # HealthChecker.check_health 또는 check_health
        assert any("check_health" in n for n in method_names)

    def test_chunk_python_syntax_error_fallback(self):
        """SyntaxError 파일 → module 청크로 폴백."""
        chunks = self.svc.chunk_file(SAMPLE_BROKEN_PYTHON, "broken.py", "AADS")
        assert len(chunks) == 1
        assert chunks[0].chunk_type == "module"

    def test_chunk_typescript_extracts_functions(self):
        """TypeScript 파일에서 함수/클래스 청크 추출 확인."""
        chunks = self.svc.chunk_file(SAMPLE_TYPESCRIPT, "chat.tsx", "AADS", language="typescript")
        names = [c.name for c in chunks]
        assert "useChatSession" in names
        assert "ChatService" in names or any("ChatService" in n for n in names)

    def test_chunk_typescript_arrow_function(self):
        """TypeScript 화살표 함수 청킹 확인."""
        chunks = self.svc.chunk_file(SAMPLE_TYPESCRIPT, "chat.tsx", "AADS", language="typescript")
        names = [c.name for c in chunks]
        assert "fetchMessages" in names

    def test_chunk_unknown_ext_module_fallback(self):
        """미지원 확장자 → module 청크 폴백."""
        chunks = self.svc.chunk_file("SELECT * FROM users;", "schema.sql", "AADS", language="sql")
        assert len(chunks) == 1
        assert chunks[0].chunk_type == "module"

    def test_chunk_code_length_limit(self):
        """청크 코드가 MAX_CHUNK_SIZE(1500자) 초과하지 않는지 확인."""
        long_code = "x = 1\n" * 500  # 매우 긴 코드
        chunks = self.svc.chunk_file(long_code, "long.py", "AADS")
        for c in chunks:
            assert len(c.code) <= 1500


# ─── 임베딩 테스트 ──────────────────────────────────────────────────────────

class TestDummyEmbedding:
    def setup_method(self):
        self.svc = CodeIndexerService()

    def test_dummy_embedding_dimension(self):
        """dummy 임베딩이 768차원인지 확인."""
        emb = self.svc._dummy_embedding("test text")
        assert len(emb) == 768

    def test_dummy_embedding_reproducible(self):
        """같은 입력에 동일한 dummy 임베딩 반환."""
        e1 = self.svc._dummy_embedding("health_checker")
        e2 = self.svc._dummy_embedding("health_checker")
        assert e1 == e2

    def test_dummy_embedding_different_for_different_texts(self):
        """다른 텍스트에는 다른 임베딩 반환."""
        e1 = self.svc._dummy_embedding("health_check function")
        e2 = self.svc._dummy_embedding("intent_router classify")
        assert e1 != e2

    @pytest.mark.asyncio
    async def test_embed_texts_without_api_key(self):
        """GEMINI_API_KEY 없을 때 dummy 임베딩 반환 확인."""
        import os
        original = os.environ.pop("GEMINI_API_KEY", None)
        os.environ.pop("GOOGLE_API_KEY", None)
        try:
            embeddings = await self.svc._embed_texts(["hello", "world"])
            assert len(embeddings) == 2
            assert len(embeddings[0]) == 768
        finally:
            if original:
                os.environ["GEMINI_API_KEY"] = original


# ─── IndexResult 구조 테스트 ─────────────────────────────────────────────────

class TestIndexResult:
    def test_index_result_defaults(self):
        """IndexResult 기본값 확인."""
        r = IndexResult(project="AADS")
        assert r.project == "AADS"
        assert r.files_scanned == 0
        assert r.chunks_created == 0
        assert r.chunks_stored == 0
        assert r.error is None

    def test_index_result_with_error(self):
        """에러 필드 설정 확인."""
        r = IndexResult(project="AADS", error="ChromaDB 초기화 실패")
        assert r.error is not None
        assert "ChromaDB" in r.error


# ─── ChromaDB mock 기반 인덱싱 테스트 ────────────────────────────────────────

class TestCodeIndexerWithMock:
    @pytest.mark.asyncio
    async def test_index_project_unknown_project(self):
        """알 수 없는 프로젝트 → error 반환."""
        svc = CodeIndexerService()
        # ChromaDB 초기화 mock
        svc._chroma_client = MagicMock()
        svc._collection = MagicMock()
        result = await svc.index_project("UNKNOWN_XYZ")
        assert result.error is not None

    @pytest.mark.asyncio
    async def test_index_project_chromadb_failure(self):
        """ChromaDB 초기화 실패 시 error 반환."""
        svc = CodeIndexerService()
        with patch.object(svc, "_ensure_chromadb", return_value=False):
            result = await svc.index_project("AADS")
        assert result.error == "ChromaDB 초기화 실패"

    @pytest.mark.asyncio
    async def test_store_chunks_mismatch_returns_zero(self):
        """청크 수와 임베딩 수 불일치 → 0 반환."""
        svc = CodeIndexerService()
        svc._collection = MagicMock()
        chunks = [
            CodeChunk(project="AADS", file_path="a.py", start_line=1, end_line=5,
                      chunk_type="function", name="foo", code="def foo(): pass"),
        ]
        embeddings = [[0.1] * 768, [0.2] * 768]  # 개수 불일치
        result = await svc._store_chunks(chunks, embeddings)
        assert result == 0

    @pytest.mark.asyncio
    async def test_store_chunks_calls_upsert(self):
        """정상 입력 시 ChromaDB upsert 호출 확인."""
        svc = CodeIndexerService()
        mock_coll = MagicMock()
        mock_coll.upsert = MagicMock()
        svc._collection = mock_coll

        chunks = [
            CodeChunk(project="AADS", file_path="health_checker.py",
                      start_line=1, end_line=10,
                      chunk_type="function", name="check_health",
                      code="async def check_health(): return 'ok'"),
        ]
        embeddings = [[0.1] * 768]
        stored = await svc._store_chunks(chunks, embeddings)
        assert stored == 1
        mock_coll.upsert.assert_called_once()

    @pytest.mark.asyncio
    async def test_update_index_skips_empty_files(self):
        """비어있는 파일은 skipped_files로 카운트."""
        svc = CodeIndexerService()
        svc._chroma_client = MagicMock()
        svc._collection = MagicMock()
        svc._collection.get = MagicMock(return_value={"ids": []})

        with patch.object(svc, "_ensure_chromadb", return_value=True):
            with patch.object(svc, "_read_file", new=AsyncMock(return_value="")):
                result = await svc.update_index("AADS", ["nonexistent.py"])
        assert result.skipped_files == 1
        assert result.chunks_created == 0


# ─── SemanticCodeSearch mock 테스트 ─────────────────────────────────────────

class TestSemanticCodeSearch:
    @pytest.mark.asyncio
    async def test_search_unavailable_returns_error(self):
        """ChromaDB 없으면 error 포함 결과 반환."""
        from app.services.semantic_code_search import SemanticCodeSearch
        svc = SemanticCodeSearch()
        with patch.object(svc, "_is_available", return_value=False):
            results = await svc.search("헬스체크 로직")
        assert len(results) == 1
        assert "error" in results[0]

    @pytest.mark.asyncio
    async def test_search_returns_results_with_required_fields(self):
        """검색 결과에 필수 필드가 포함되는지 확인."""
        from app.services.semantic_code_search import SemanticCodeSearch
        svc = SemanticCodeSearch()

        # ChromaDB mock
        mock_collection = MagicMock()
        mock_collection.query.return_value = {
            "ids": [["AADS__health_checker_py__function__check_health__10"]],
            "distances": [[0.15]],
            "metadatas": [[{
                "project": "AADS",
                "file": "app/services/health_checker.py",
                "start_line": 10,
                "end_line": 30,
                "type": "function",
                "name": "check_health",
                "language": "python",
            }]],
            "documents": [["# AADS/health_checker.py [function: check_health]\nasync def check_health(): ..."]],
        }
        svc._indexer._chroma_client = MagicMock()
        svc._indexer._collection = mock_collection

        with patch.object(svc, "_is_available", return_value=True):
            with patch.object(svc._indexer, "_embed_texts",
                              new=AsyncMock(return_value=[[0.1] * 768])):
                results = await svc.search("헬스체크 로직", project="AADS", top_k=5)

        assert len(results) >= 1
        r = results[0]
        assert "project" in r
        assert "file" in r
        assert "similarity_score" in r
        assert "code_snippet" in r
        assert r["project"] == "AADS"
        assert r["file"] == "app/services/health_checker.py"
        assert r["similarity_score"] == pytest.approx(0.85, abs=0.01)  # 1 - 0.15

    @pytest.mark.asyncio
    async def test_build_code_context_empty_when_no_results(self):
        """결과 없으면 빈 문자열 반환."""
        from app.services.semantic_code_search import SemanticCodeSearch
        svc = SemanticCodeSearch()
        with patch.object(svc, "_is_available", return_value=False):
            ctx = await svc.build_code_context("테스트 쿼리")
        assert ctx == ""

    @pytest.mark.asyncio
    async def test_build_code_context_format(self):
        """build_code_context 결과가 XML 태그로 감싸지는지 확인."""
        from app.services.semantic_code_search import SemanticCodeSearch
        svc = SemanticCodeSearch()

        mock_results = [{
            "project": "AADS",
            "file": "app/services/health_checker.py",
            "start_line": 10,
            "end_line": 30,
            "type": "function",
            "name": "check_health",
            "language": "python",
            "code_snippet": "async def check_health(): return {'status': 'ok'}",
            "similarity_score": 0.85,
        }]

        with patch.object(svc, "_is_available", return_value=True):
            with patch.object(svc, "search", new=AsyncMock(return_value=mock_results)):
                ctx = await svc.build_code_context("헬스체크 로직")

        assert "<semantic_code_context>" in ctx
        assert "check_health" in ctx


# ─── 통합: AADS 로컬 파일 목록 확인 ─────────────────────────────────────────

class TestLocalFileList:
    @pytest.mark.asyncio
    async def test_list_aads_files_returns_python_files(self):
        """AADS 로컬 파일 목록에 .py 파일이 포함되는지 확인."""
        svc = CodeIndexerService()
        files = await svc._list_files("AADS")
        # AADS 서버 경로가 존재하는 경우에만 검증
        if files:
            py_files = [f for f in files if f.endswith(".py")]
            assert len(py_files) > 0, "Python 파일이 없음"
            assert len(files) >= 10, f"파일 수 부족: {len(files)}"
        else:
            # 경로 없으면 스킵 (CI 환경)
            pytest.skip("AADS 서버 경로 없음 (CI 환경)")

    @pytest.mark.asyncio
    async def test_list_unknown_project_returns_empty(self):
        """미지원 프로젝트 → 빈 리스트."""
        svc = CodeIndexerService()
        files = await svc._list_files("UNKNOWN_PROJECT")
        assert files == []


# ─── 청킹 통합: 실제 파일 청킹 ──────────────────────────────────────────────

class TestRealFileChunking:
    def test_chunk_real_health_checker(self):
        """실제 health_checker.py 파일(있으면) 청킹 확인."""
        path = Path("/root/aads/aads-server/app/services/health_checker.py")
        if not path.exists():
            pytest.skip("health_checker.py 없음")
        content = path.read_text(errors="replace")
        svc = CodeIndexerService()
        chunks = svc.chunk_file(content, "app/services/health_checker.py", "AADS")
        assert len(chunks) >= 1
        # 모든 청크에 project 설정 확인
        for c in chunks:
            assert c.project == "AADS"
            assert c.language == "python"

    def test_chunk_real_intent_router(self):
        """실제 intent_router.py 파일(있으면) 청킹 확인."""
        path = Path("/root/aads/aads-server/app/services/intent_router.py")
        if not path.exists():
            pytest.skip("intent_router.py 없음")
        content = path.read_text(errors="replace")
        svc = CodeIndexerService()
        chunks = svc.chunk_file(content, "app/services/intent_router.py", "AADS")
        # intent_router는 분류 로직이 있으므로 함수 청크가 있어야 함
        function_chunks = [c for c in chunks if c.chunk_type == "function"]
        assert len(function_chunks) >= 1, "함수 청크 없음"
