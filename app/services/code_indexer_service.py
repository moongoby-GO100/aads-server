"""
AADS-188B: 코드베이스 인덱싱 서비스
벡터 기반 코드 청킹 + ChromaDB persistent 저장.
- Python: ast 모듈로 함수/클래스 단위 청킹
- TS/JS: regex로 함수/클래스 단위 청킹
- 원격 프로젝트: SSH로 파일 목록·내용 수집
- 임베딩: Google Gemini text-embedding-004 (GEMINI_API_KEY 없으면 hash dummy)
- 저장소: ChromaDB PersistentClient (/root/aads/data/chromadb/)
"""
from __future__ import annotations

import ast as python_ast
import asyncio
import hashlib
import logging
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

_CHROMADB_PATH = os.getenv("CHROMADB_PATH", "/root/aads/data/chromadb")
_COLLECTION_NAME = "code_chunks"
_MAX_CHUNK_SIZE = 1500       # 청크당 최대 문자 수
_EMBED_BATCH_SIZE = 50       # Gemini API 배치 크기
_MAX_FILES_PER_PROJECT = 300  # 프로젝트당 최대 파일 수

# 프로젝트 → 서버 정보 (중앙 설정에서 import)
from app.core.project_config import PROJECT_MAP as _PROJECT_MAP

_SUPPORTED_EXTS: Dict[str, List[str]] = {
    "python":     [".py"],
    "typescript": [".ts", ".tsx", ".js", ".jsx"],
    "php":        [".php"],
}

_SKIP_PATTERNS = (
    ".git", "__pycache__", ".venv", "node_modules", "venv", "dist",
    ".next", "build", "coverage", ".mypy_cache",
)


# ─── 데이터 클래스 ────────────────────────────────────────────────────────────

@dataclass
class CodeChunk:
    """코드 청크 — 인덱싱 단위."""
    project: str
    file_path: str          # 워크디렉토리 기준 상대 경로
    start_line: int
    end_line: int
    chunk_type: str         # function | class | module
    name: str
    code: str
    language: str = "python"

    @property
    def chunk_id(self) -> str:
        """고유 ID (project + file + type + name + line)."""
        safe = self.file_path.replace("/", "_").replace(".", "_").replace("-", "_")
        return f"{self.project}__{safe}__{self.chunk_type}__{self.name}__{self.start_line}"

    @property
    def text_for_embedding(self) -> str:
        """임베딩용 텍스트 (경로 + 이름 + 코드 앞 1200자)."""
        header = f"# {self.project}/{self.file_path} [{self.chunk_type}: {self.name}]"
        return f"{header}\n{self.code[:1200]}"


@dataclass
class IndexResult:
    """인덱싱 결과."""
    project: str
    files_scanned: int = 0
    chunks_created: int = 0
    chunks_stored: int = 0
    skipped_files: int = 0
    error: Optional[str] = None


# ─── 메인 서비스 ──────────────────────────────────────────────────────────────

class CodeIndexerService:
    """코드베이스 인덱싱 서비스 — ChromaDB + Gemini Embedding."""

    def __init__(self) -> None:
        self._chroma_client: Any = None
        self._collection: Any = None

    # ── ChromaDB 초기화 ──────────────────────────────────────────────────────

    def _ensure_chromadb(self) -> bool:
        """ChromaDB 클라이언트 초기화. 이미 초기화된 경우 스킵. 실패 시 False."""
        if self._chroma_client is not None:
            return True
        try:
            import chromadb  # type: ignore
            os.makedirs(_CHROMADB_PATH, exist_ok=True)
            self._chroma_client = chromadb.PersistentClient(path=_CHROMADB_PATH)
            self._collection = self._chroma_client.get_or_create_collection(
                name=_COLLECTION_NAME,
                metadata={"description": "AADS code chunks for semantic search — AADS-188B"},
            )
            logger.info(f"[CodeIndexer] ChromaDB 초기화 완료: {_CHROMADB_PATH}")
            return True
        except ImportError:
            logger.warning("[CodeIndexer] chromadb 미설치 — pip install chromadb")
            return False
        except Exception as e:
            logger.error(f"[CodeIndexer] ChromaDB 초기화 실패: {e}")
            return False

    # ── 임베딩 ──────────────────────────────────────────────────────────────

    async def _embed_texts(self, texts: List[str]) -> List[List[float]]:
        """
        Google Gemini gemini-embedding-001 (3072차원) 으로 텍스트 임베딩.
        GEMINI_API_KEY 없으면 hash 기반 dummy 임베딩 반환.
        """
        api_key = os.getenv("GOOGLE_API_KEY") or os.getenv("GEMINI_API_KEY")
        if not api_key:
            logger.debug("[CodeIndexer] GEMINI_API_KEY 없음 — dummy 임베딩 사용")
            return [self._dummy_embedding(t) for t in texts]

        try:
            from google import genai as google_genai  # type: ignore
            client = google_genai.Client(api_key=api_key)
            loop = asyncio.get_event_loop()
            all_embeddings: List[List[float]] = []

            for i in range(0, len(texts), _EMBED_BATCH_SIZE):
                batch = texts[i: i + _EMBED_BATCH_SIZE]

                def _call(b: List[str] = batch) -> Any:
                    return client.models.embed_content(
                        model="models/gemini-embedding-001",
                        contents=b,
                    )

                result = await loop.run_in_executor(None, _call)
                for emb in result.embeddings:
                    all_embeddings.append(list(emb.values))

            return all_embeddings
        except Exception as e:
            logger.warning(f"[CodeIndexer] Gemini 임베딩 실패: {e} — dummy 사용")
            return [self._dummy_embedding(t) for t in texts]

    def _dummy_embedding(self, text: str, dim: int = 3072) -> List[float]:
        """테스트/폴백용 hash 기반 dummy 임베딩 (재현 가능)."""
        h = hashlib.sha256(text.encode()).digest()
        # 32바이트 → 8개 float, 반복하여 768차원 채우기
        base: List[float] = []
        for i in range(0, 32, 4):
            val = int.from_bytes(h[i: i + 4], "big")
            base.append((val / 2**32) * 2.0 - 1.0)
        result = (base * (dim // len(base) + 1))[:dim]
        return result

    # ── 파일 목록 ────────────────────────────────────────────────────────────

    async def _list_files(self, project: str) -> List[str]:
        """프로젝트 파일 목록 반환 (로컬 or SSH)."""
        info = _PROJECT_MAP.get(project)
        if not info:
            return []
        host = info["server"]
        workdir = info["workdir"]
        lang = info.get("lang", "python")
        exts = _SUPPORTED_EXTS.get(lang, [".py"])

        if host == "localhost":
            files: List[str] = []
            base = Path(workdir)
            for ext in exts:
                for p in base.rglob(f"*{ext}"):
                    rel = str(p.relative_to(base))
                    if any(skip in rel for skip in _SKIP_PATTERNS):
                        continue
                    files.append(str(p))
            return files[:_MAX_FILES_PER_PROJECT]
        else:
            ext_filter = " -o ".join(f"-name '*{e}'" for e in exts)
            cmd = (
                f"find {workdir} \\( {ext_filter} \\)"
                f" -not -path '*/.git/*' -not -path '*/__pycache__/*'"
                f" 2>/dev/null | head -{_MAX_FILES_PER_PROJECT}"
            )
            from app.services.code_explorer_service import _ssh_run
            out = await _ssh_run(host, cmd, timeout=30.0)
            return [line.strip() for line in out.strip().split("\n") if line.strip()]

    # ── 파일 읽기 ────────────────────────────────────────────────────────────

    async def _read_file(self, host: str, path: str) -> str:
        """파일 읽기 (로컬 or SSH). 최대 50KB."""
        if host == "localhost":
            try:
                with open(path, "r", errors="replace") as f:
                    return f.read(50000)
            except Exception:
                return ""
        from app.services.code_explorer_service import _ssh_run
        return await _ssh_run(host, f"cat '{path}' 2>/dev/null | head -c 50000", timeout=15.0)

    # ── 청킹 ─────────────────────────────────────────────────────────────────

    def _chunk_python(self, content: str, file_path: str, project: str) -> List[CodeChunk]:
        """Python: ast 모듈로 함수/클래스/메서드 단위 청킹."""
        lines = content.split("\n")
        chunks: List[CodeChunk] = []

        try:
            tree = python_ast.parse(content)
        except SyntaxError:
            # 파싱 불가: 파일 전체를 module 청크로
            chunks.append(CodeChunk(
                project=project, file_path=file_path,
                start_line=1, end_line=len(lines),
                chunk_type="module", name=Path(file_path).stem,
                code=content[:_MAX_CHUNK_SIZE], language="python",
            ))
            return chunks

        def _node_lines(node: Any) -> Tuple[int, int]:
            return getattr(node, "lineno", 1), getattr(node, "end_lineno", 1)

        def _code_slice(start: int, end: int) -> str:
            return "\n".join(lines[start - 1: end])[:_MAX_CHUNK_SIZE]

        for node in python_ast.iter_child_nodes(tree):
            if isinstance(node, (python_ast.FunctionDef, python_ast.AsyncFunctionDef)):
                s, e = _node_lines(node)
                chunks.append(CodeChunk(
                    project=project, file_path=file_path,
                    start_line=s, end_line=e,
                    chunk_type="function", name=node.name,
                    code=_code_slice(s, e), language="python",
                ))
            elif isinstance(node, python_ast.ClassDef):
                s, e = _node_lines(node)
                chunks.append(CodeChunk(
                    project=project, file_path=file_path,
                    start_line=s, end_line=e,
                    chunk_type="class", name=node.name,
                    code=_code_slice(s, e), language="python",
                ))
                # 메서드도 별도 청킹
                for child in python_ast.iter_child_nodes(node):
                    if isinstance(child, (python_ast.FunctionDef, python_ast.AsyncFunctionDef)):
                        ms, me = _node_lines(child)
                        chunks.append(CodeChunk(
                            project=project, file_path=file_path,
                            start_line=ms, end_line=me,
                            chunk_type="function",
                            name=f"{node.name}.{child.name}",
                            code=_code_slice(ms, me), language="python",
                        ))

        if not chunks:
            chunks.append(CodeChunk(
                project=project, file_path=file_path,
                start_line=1, end_line=len(lines),
                chunk_type="module", name=Path(file_path).stem,
                code=content[:_MAX_CHUNK_SIZE], language="python",
            ))
        return chunks

    def _chunk_typescript(self, content: str, file_path: str, project: str) -> List[CodeChunk]:
        """TypeScript/JS: regex 기반 함수/클래스/화살표함수 청킹."""
        lines = content.split("\n")
        chunks: List[CodeChunk] = []

        patterns: List[Tuple[re.Pattern[str], str]] = [
            (re.compile(r'^(?:export\s+)?(?:async\s+)?function\s+(\w+)\s*\(', re.MULTILINE), "function"),
            (re.compile(r'^(?:export\s+)?(?:default\s+)?class\s+(\w+)', re.MULTILINE),       "class"),
            (re.compile(r'^(?:export\s+)?(?:const|let|var)\s+(\w+)\s*=\s*(?:async\s+)?\(', re.MULTILINE), "function"),
        ]

        for pat, ctype in patterns:
            for m in pat.finditer(content):
                name = m.group(1)
                start_line = content[: m.start()].count("\n") + 1
                end_line = min(start_line + 80, len(lines))
                code = "\n".join(lines[start_line - 1: end_line])[:_MAX_CHUNK_SIZE]
                chunks.append(CodeChunk(
                    project=project, file_path=file_path,
                    start_line=start_line, end_line=end_line,
                    chunk_type=ctype, name=name,
                    code=code, language="typescript",
                ))

        if not chunks:
            chunks.append(CodeChunk(
                project=project, file_path=file_path,
                start_line=1, end_line=len(lines),
                chunk_type="module", name=Path(file_path).stem,
                code=content[:_MAX_CHUNK_SIZE], language="typescript",
            ))
        return chunks

    def chunk_file(
        self,
        content: str,
        file_path: str,
        project: str,
        language: str = "python",
    ) -> List[CodeChunk]:
        """파일 내용을 언어에 맞게 청킹. 외부에서 직접 호출 가능."""
        ext = Path(file_path).suffix.lower()
        if ext == ".py":
            return self._chunk_python(content, file_path, project)
        elif ext in (".ts", ".tsx", ".js", ".jsx"):
            return self._chunk_typescript(content, file_path, project)
        else:
            lines = content.split("\n")
            return [CodeChunk(
                project=project, file_path=file_path,
                start_line=1, end_line=len(lines),
                chunk_type="module", name=Path(file_path).stem,
                code=content[:_MAX_CHUNK_SIZE], language=language,
            )]

    # ── ChromaDB 저장 ────────────────────────────────────────────────────────

    async def _store_chunks(
        self, chunks: List[CodeChunk], embeddings: List[List[float]]
    ) -> int:
        """ChromaDB에 청크 upsert. 저장된 수 반환."""
        if not chunks or not embeddings or len(chunks) != len(embeddings):
            return 0
        loop = asyncio.get_event_loop()

        def _upsert() -> int:
            self._collection.upsert(
                ids=[c.chunk_id for c in chunks],
                embeddings=embeddings,
                documents=[c.text_for_embedding for c in chunks],
                metadatas=[
                    {
                        "project": c.project,
                        "file": c.file_path,
                        "start_line": c.start_line,
                        "end_line": c.end_line,
                        "type": c.chunk_type,
                        "name": c.name,
                        "language": c.language,
                    }
                    for c in chunks
                ],
            )
            return len(chunks)

        return await loop.run_in_executor(None, _upsert)

    def _delete_by_file(self, project: str, file_path: str) -> None:
        """특정 파일의 기존 청크 삭제 (재인덱싱 전처리)."""
        try:
            res = self._collection.get(where={"$and": [{"project": project}, {"file": file_path}]})
            if res and res.get("ids"):
                self._collection.delete(ids=res["ids"])
        except Exception as e:
            logger.debug(f"[CodeIndexer] 파일 청크 삭제 실패: {e}")

    # ── 메인 인덱싱 ──────────────────────────────────────────────────────────

    async def index_project(self, project: str) -> IndexResult:
        """
        프로젝트 전체 인덱싱.

        Args:
            project: 프로젝트명 (AADS, KIS, GO100, SF, NTV2, NAS)

        Returns:
            IndexResult
        """
        proj = project.upper()
        result = IndexResult(project=proj)

        if not self._ensure_chromadb():
            result.error = "ChromaDB 초기화 실패"
            return result

        info = _PROJECT_MAP.get(proj)
        if not info:
            result.error = f"미지원 프로젝트: {project}. 지원: {list(_PROJECT_MAP)}"
            return result

        host = info["server"]
        workdir = info["workdir"]
        lang = info.get("lang", "python")

        # 1. 파일 목록
        files = await self._list_files(proj)
        result.files_scanned = len(files)
        if not files:
            result.error = "파일 없음 또는 SSH 접근 실패"
            return result

        logger.info(f"[CodeIndexer] {proj} 파일 {len(files)}개 인덱싱 시작")

        # 2. 병렬 파일 읽기
        contents = await asyncio.gather(
            *[self._read_file(host, f) for f in files],
            return_exceptions=True,
        )

        # 3. 청킹
        all_chunks: List[CodeChunk] = []
        for file_path, content in zip(files, contents):
            if isinstance(content, Exception) or not content:
                result.skipped_files += 1
                continue
            rel = file_path.replace(workdir, "").lstrip("/")
            try:
                all_chunks.extend(self.chunk_file(content, rel, proj, lang))
            except Exception as e:
                logger.debug(f"[CodeIndexer] 청킹 실패 {file_path}: {e}")
                result.skipped_files += 1

        result.chunks_created = len(all_chunks)
        if not all_chunks:
            result.error = "청킹된 코드 없음"
            return result

        # 4. 임베딩 + 저장 (배치)
        stored = 0
        for i in range(0, len(all_chunks), _EMBED_BATCH_SIZE):
            batch = all_chunks[i: i + _EMBED_BATCH_SIZE]
            embeddings = await self._embed_texts([c.text_for_embedding for c in batch])
            if len(embeddings) == len(batch):
                stored += await self._store_chunks(batch, embeddings)

        result.chunks_stored = stored
        logger.info(f"[CodeIndexer] {proj} 완료: {stored}/{len(all_chunks)} 청크 저장")
        return result

    async def update_index(self, project: str, changed_files: List[str]) -> IndexResult:
        """
        변경된 파일만 재인덱싱.

        Args:
            project: 프로젝트명
            changed_files: 변경 파일 목록 (절대/상대 경로)
        """
        proj = project.upper()
        result = IndexResult(project=proj)

        if not self._ensure_chromadb():
            result.error = "ChromaDB 초기화 실패"
            return result

        info = _PROJECT_MAP.get(proj)
        if not info:
            result.error = f"미지원 프로젝트: {project}"
            return result

        host = info["server"]
        workdir = info["workdir"]
        lang = info.get("lang", "python")
        result.files_scanned = len(changed_files)

        all_chunks: List[CodeChunk] = []
        loop = asyncio.get_event_loop()

        for fp in changed_files:
            abs_path = fp if fp.startswith("/") else f"{workdir}/{fp}"
            content = await self._read_file(host, abs_path)
            if not content:
                result.skipped_files += 1
                continue
            rel = abs_path.replace(workdir, "").lstrip("/")
            # 기존 청크 삭제
            await loop.run_in_executor(None, self._delete_by_file, proj, rel)
            try:
                all_chunks.extend(self.chunk_file(content, rel, proj, lang))
            except Exception as e:
                logger.debug(f"[CodeIndexer] 청킹 실패 {fp}: {e}")

        result.chunks_created = len(all_chunks)
        stored = 0
        for i in range(0, len(all_chunks), _EMBED_BATCH_SIZE):
            batch = all_chunks[i: i + _EMBED_BATCH_SIZE]
            embeddings = await self._embed_texts([c.text_for_embedding for c in batch])
            if len(embeddings) == len(batch):
                stored += await self._store_chunks(batch, embeddings)

        result.chunks_stored = stored
        return result

    # ── 통계 ─────────────────────────────────────────────────────────────────

    def get_collection_stats(self, project: Optional[str] = None) -> Dict[str, Any]:
        """ChromaDB 통계. project 지정 시 해당 프로젝트 청크 수만 반환."""
        if not self._ensure_chromadb():
            return {"error": "ChromaDB 초기화 실패"}
        try:
            if project:
                res = self._collection.get(where={"project": project.upper()})
                return {
                    "project": project.upper(),
                    "chunk_count": len(res.get("ids", [])),
                    "db_path": _CHROMADB_PATH,
                }
            total = self._collection.count()
            return {"total_chunks": total, "db_path": _CHROMADB_PATH}
        except Exception as e:
            return {"error": str(e)}
