"""
AADS-188B: 시맨틱 코드 검색 서비스
- ChromaDB 벡터 유사도 검색
- code_explorer(AST 정밀 검색)와 하이브리드 검색 지원
- Context Builder 연동: 쿼리 관련 코드 청크 최대 5개, 3000토큰 이하
"""
from __future__ import annotations

import asyncio
import logging
import os
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

_MAX_SNIPPET_LEN = 600   # 결과 코드 스니펫 최대 길이
_CONTEXT_MAX_TOKENS = 3000
_CONTEXT_MAX_CHUNKS = 5


class SemanticCodeSearch:
    """ChromaDB 기반 시맨틱 코드 검색."""

    def __init__(self) -> None:
        from app.services.code_indexer_service import CodeIndexerService
        self._indexer = CodeIndexerService()

    def _is_available(self) -> bool:
        """ChromaDB 사용 가능 여부."""
        return self._indexer._ensure_chromadb()

    async def search(
        self,
        query: str,
        project: Optional[str] = None,
        top_k: int = 5,
    ) -> List[Dict[str, Any]]:
        """
        시맨틱 코드 검색.

        Args:
            query: 자연어 검색어 (예: "인증 로직", "헬스체크 함수")
            project: 프로젝트 필터 (None이면 전체)
            top_k: 반환할 결과 수

        Returns:
            [{project, file, start_line, end_line, type, name, code_snippet, similarity_score}]
        """
        if not self._is_available():
            return [{"error": "ChromaDB 미초기화 — index_project 먼저 실행 필요"}]

        # 쿼리 임베딩
        embeddings = await self._indexer._embed_texts([query])
        if not embeddings:
            return [{"error": "임베딩 실패"}]
        query_embedding = embeddings[0]

        # ChromaDB 검색
        loop = asyncio.get_event_loop()

        def _query() -> Any:
            where_filter: Optional[Dict[str, Any]] = None
            if project:
                where_filter = {"project": project.upper()}
            return self._indexer._collection.query(
                query_embeddings=[query_embedding],
                n_results=min(top_k, 20),
                where=where_filter,
                include=["documents", "metadatas", "distances"],
            )

        try:
            raw = await loop.run_in_executor(None, _query)
        except Exception as e:
            logger.error(f"[SemanticSearch] ChromaDB 쿼리 실패: {e}")
            return [{"error": str(e)}]

        results: List[Dict[str, Any]] = []
        ids_list = raw.get("ids", [[]])[0]
        distances = raw.get("distances", [[]])[0]
        metadatas = raw.get("metadatas", [[]])[0]
        documents = raw.get("documents", [[]])[0]

        for i, (dist, meta, doc) in enumerate(zip(distances, metadatas, documents)):
            # ChromaDB distance → similarity (cosine distance 기준: similarity = 1 - distance)
            similarity = round(1.0 - float(dist), 4)
            snippet = doc[:_MAX_SNIPPET_LEN] if doc else ""
            results.append({
                "project":          meta.get("project", ""),
                "file":             meta.get("file", ""),
                "start_line":       meta.get("start_line", 0),
                "end_line":         meta.get("end_line", 0),
                "type":             meta.get("type", ""),
                "name":             meta.get("name", ""),
                "language":         meta.get("language", ""),
                "code_snippet":     snippet,
                "similarity_score": similarity,
            })

        # similarity 내림차순 정렬
        results.sort(key=lambda x: x.get("similarity_score", 0), reverse=True)
        return results[:top_k]

    async def hybrid_search(
        self,
        query: str,
        project: Optional[str] = None,
        top_k: int = 5,
    ) -> Dict[str, Any]:
        """
        하이브리드 검색: 시맨틱(벡터) + CKP(키워드) 결합.
        시맨틱 결과를 주로 하고, CKP 키워드 매칭을 보조로 추가.

        Returns:
            {semantic: [...], ckp: [...], combined: [...]}
        """
        semantic_results = await self.search(query, project=project, top_k=top_k)

        ckp_results: List[Dict[str, Any]] = []
        try:
            from app.services.code_explorer_service import CodeExplorerService
            svc = CodeExplorerService()
            cross = await asyncio.wait_for(
                svc.search_all_projects(query),
                timeout=30.0,
            )
            # project 필터 적용
            for m in cross.matches[:10]:
                if project and m.get("project", "").upper() != project.upper():
                    continue
                ckp_results.append({
                    "project":  m.get("project", ""),
                    "file":     m.get("file", ""),
                    "snippet":  m.get("snippet", ""),
                    "source":   m.get("match_type", "ckp"),
                })
        except Exception as e:
            logger.debug(f"[SemanticSearch] CKP 검색 실패: {e}")

        # 중복 제거 후 병합 (semantic 우선)
        seen_files: set[str] = {r.get("file", "") for r in semantic_results}
        for r in ckp_results:
            if r.get("file", "") not in seen_files:
                seen_files.add(r["file"])
                semantic_results.append({
                    "project":          r["project"],
                    "file":             r["file"],
                    "start_line":       0,
                    "end_line":         0,
                    "type":             "source",
                    "name":             r["file"].split("/")[-1],
                    "language":         "",
                    "code_snippet":     r["snippet"],
                    "similarity_score": 0.0,
                })

        return {
            "query":    query,
            "semantic": semantic_results[:top_k],
            "ckp":      ckp_results[:5],
            "combined": semantic_results[:top_k],
        }

    async def build_code_context(
        self,
        query: str,
        project: Optional[str] = None,
    ) -> str:
        """
        Context Builder 연동 — CEO 질의 관련 코드 청크를 XML 태그로 반환.
        최대 5개 청크, 약 3000 토큰 이하.

        Returns:
            <semantic_code_context>...</semantic_code_context> 또는 빈 문자열
        """
        if not self._is_available():
            return ""

        try:
            results = await asyncio.wait_for(
                self.search(query, project=project, top_k=_CONTEXT_MAX_CHUNKS),
                timeout=10.0,
            )
        except asyncio.TimeoutError:
            return ""
        except Exception as e:
            logger.debug(f"[SemanticSearch] build_code_context 실패: {e}")
            return ""

        # error 결과만 있는 경우 스킵
        valid = [r for r in results if "error" not in r and r.get("similarity_score", 0) > 0.3]
        if not valid:
            return ""

        lines: List[str] = []
        total_chars = 0
        char_limit = _CONTEXT_MAX_TOKENS * 4  # 토큰당 ~4자 근사

        for r in valid:
            snippet = r.get("code_snippet", "")[:400]
            entry = (
                f"[{r['project']}/{r['file']}:{r['start_line']}-{r['end_line']}]"
                f" {r['type']}:{r['name']} (score={r['similarity_score']:.2f})\n"
                f"```\n{snippet}\n```"
            )
            if total_chars + len(entry) > char_limit:
                break
            lines.append(entry)
            total_chars += len(entry)

        if not lines:
            return ""

        return (
            "\n<semantic_code_context>\n"
            + "\n\n".join(lines)
            + "\n</semantic_code_context>"
        )

    def get_stats(self) -> Dict[str, Any]:
        """ChromaDB 컬렉션 통계."""
        return self._indexer.get_collection_stats()
