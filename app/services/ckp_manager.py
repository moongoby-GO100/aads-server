"""
AADS-186B: CKP(Codebase Knowledge Package) 매니저
프로젝트 소스 스캔 → CKP 파일 5종 생성 + DB 메타데이터 기록
"""
from __future__ import annotations

import logging
import os
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from app.models.ckp import (
    CKPIndexRecord,
    CKPScanResult,
    CKPSearchResult,
    FileAnalysis,
)
from app.services.ast_analyzer import ASTAnalyzer

logger = logging.getLogger(__name__)

# ─── 상수 ─────────────────────────────────────────────────────────────────────

AADS_ROOT = Path("/root/aads")
CKP_DIR = AADS_ROOT / ".claude"

# 스캔 대상 확장자
SCAN_EXTENSIONS = {".py", ".ts", ".tsx", ".sql", ".md", ".yml", ".yaml", ".env"}

# 제외 디렉토리
EXCLUDE_DIRS = {
    "node_modules", "__pycache__", ".git", ".next", ".venv", "venv",
    "dist", "build", ".mypy_cache", ".pytest_cache", "htmlcov",
    "backups", "logs", "pids",
}

# 토큰 읽기 제한
MAX_TOKENS_PER_SCAN = 50_000
MAX_FILE_TOKENS = 5_000  # 파일 1개당 최대

# CKP 파일 타입
CKP_FILE_TYPES = {
    "CLAUDE.md":        "claude_md",
    "ARCHITECTURE.md":  "architecture",
    "CODEBASE-MAP.md":  "codebase_map",
    "DEPENDENCY-MAP.md": "dependency_map",
    "LESSONS.md":       "lessons",
}


class CKPManager:
    """Codebase Knowledge Package 관리자."""

    def __init__(self, db_conn=None):
        self.db = db_conn
        self.analyzer = ASTAnalyzer()

    # ─── 로컬 프로젝트 스캔 ──────────────────────────────────────────────────

    async def scan_local_project(self) -> CKPScanResult:
        """AADS 프로젝트(서버 68 /root/aads) 스캔 → CKP 파일 5종 생성."""
        return await self.scan_project("AADS", str(AADS_ROOT))

    async def scan_project(self, project: str, root_path: str) -> CKPScanResult:
        """프로젝트 소스 스캔 → CKP 파일 5종 생성."""
        start = time.monotonic()
        result = CKPScanResult(project=project)
        root = Path(root_path)

        logger.info(f"[CKP] 스캔 시작: {project} @ {root_path}")

        # 파일 수집
        py_files: Dict[str, str] = {}
        ts_files: Dict[str, str] = {}
        other_files: Dict[str, str] = {}
        total_tokens = 0

        for dirpath, dirnames, filenames in os.walk(root):
            # 제외 디렉토리 필터
            dirnames[:] = [
                d for d in dirnames
                if d not in EXCLUDE_DIRS and not d.startswith(".")
            ]

            for fname in filenames:
                fpath = Path(dirpath) / fname
                ext = fpath.suffix.lower()
                if ext not in SCAN_EXTENSIONS:
                    continue

                rel_path = str(fpath.relative_to(root))
                try:
                    content = fpath.read_text(encoding="utf-8", errors="ignore")
                except Exception:
                    continue

                from app.core.token_utils import estimate_tokens as _est_tokens, CHARS_PER_TOKEN
                tokens = _est_tokens(content)
                if tokens > MAX_FILE_TOKENS:
                    content = content[:MAX_FILE_TOKENS * CHARS_PER_TOKEN]
                    tokens = MAX_FILE_TOKENS

                if total_tokens + tokens > MAX_TOKENS_PER_SCAN:
                    logger.debug(f"[CKP] 토큰 한도 도달, 스캔 중단 @ {rel_path}")
                    break

                total_tokens += tokens
                result.scanned_files += 1

                if ext == ".py":
                    py_files[rel_path] = content
                elif ext in (".ts", ".tsx"):
                    ts_files[rel_path] = content
                else:
                    other_files[rel_path] = content

        result.total_tokens = total_tokens

        # AST 분석
        py_analyses: Dict[str, FileAnalysis] = {}
        for rel_path, content in py_files.items():
            analysis = self.analyzer.analyze_python_file(content, rel_path)
            py_analyses[rel_path] = analysis

        ts_analyses: Dict[str, FileAnalysis] = {}
        for rel_path, content in ts_files.items():
            analysis = self.analyzer.analyze_typescript_file(content, rel_path)
            ts_analyses[rel_path] = analysis

        # 의존성 그래프
        all_analyses = {**py_analyses, **ts_analyses}
        dep_graph = self.analyzer.build_dependency_graph(py_analyses)

        # CKP 디렉토리 준비
        ckp_dir = root / ".claude"
        ckp_dir.mkdir(exist_ok=True)

        # CKP 파일 5종 생성
        now_str = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

        files_generated = []

        # a) CODEBASE-MAP.md
        codebase_map = self._build_codebase_map(py_analyses, ts_analyses, now_str)
        (ckp_dir / "CODEBASE-MAP.md").write_text(codebase_map, encoding="utf-8")
        files_generated.append(".claude/CODEBASE-MAP.md")

        # b) DEPENDENCY-MAP.md
        dep_map = self._build_dependency_map(dep_graph, py_files, now_str)
        (ckp_dir / "DEPENDENCY-MAP.md").write_text(dep_map, encoding="utf-8")
        files_generated.append(".claude/DEPENDENCY-MAP.md")

        # c) ARCHITECTURE.md (기존 파일 없을 때만 생성)
        arch_path = ckp_dir / "ARCHITECTURE.md"
        if not arch_path.exists():
            arch_md = self._build_architecture_md(project, py_analyses, now_str)
            arch_path.write_text(arch_md, encoding="utf-8")
            files_generated.append(".claude/ARCHITECTURE.md")

        # d) LESSONS.md (기존 파일 없을 때만 생성)
        lessons_path = ckp_dir / "LESSONS.md"
        if not lessons_path.exists():
            lessons_md = self._build_lessons_md(project, now_str)
            lessons_path.write_text(lessons_md, encoding="utf-8")
            files_generated.append(".claude/LESSONS.md")

        # e) CLAUDE.md (기존 파일 없을 때만 생성)
        claude_path = ckp_dir / "CLAUDE.md"
        if not claude_path.exists():
            claude_md = self._build_claude_md(project, py_analyses, ts_analyses, now_str)
            claude_path.write_text(claude_md, encoding="utf-8")
            files_generated.append(".claude/CLAUDE.md")

        result.generated_files = files_generated
        result.duration_seconds = time.monotonic() - start

        # DB 메타데이터 기록
        if self.db:
            await self._upsert_ckp_index(project, ckp_dir, result)

        logger.info(
            f"[CKP] 스캔 완료: {project}, {result.scanned_files}파일, "
            f"{result.total_tokens}토큰, {result.duration_seconds:.1f}s"
        )
        return result

    # ─── 원격 프로젝트 스캔 ──────────────────────────────────────────────────

    async def scan_remote_project(self, project: str) -> CKPScanResult:
        """원격 프로젝트(SSH 경유) 스캔.
        SSH 접근 불가(claudebot 키 없음) → .claude/projects/{project}/ CKP 파일 사용.
        staged HANDOVER로 보완하여 DB 메타데이터 등록.
        """
        import time
        start = time.monotonic()
        result = CKPScanResult(project=project)

        # .claude/projects/{project}/ CKP 디렉토리 확인
        projects_ckp_dir = AADS_ROOT / ".claude" / "projects" / project
        if projects_ckp_dir.exists():
            generated = []
            scanned = 0
            for fname in ["CLAUDE.md", "ARCHITECTURE.md", "CODEBASE-MAP.md",
                          "DEPENDENCY-MAP.md", "LESSONS.md"]:
                fpath = projects_ckp_dir / fname
                if fpath.exists():
                    generated.append(str(fpath.relative_to(AADS_ROOT)))
                    scanned += 1
            result.scanned_files = scanned
            result.generated_files = generated
            from app.core.token_utils import estimate_tokens as _est_tokens
            result.total_tokens = sum(
                _est_tokens((projects_ckp_dir / f).read_text(encoding="utf-8", errors="ignore"))
                for f in ["CLAUDE.md", "ARCHITECTURE.md", "CODEBASE-MAP.md",
                          "DEPENDENCY-MAP.md", "LESSONS.md"]
                if (projects_ckp_dir / f).exists()
            )
            logger.info(
                f"[CKP] 원격 프로젝트 {project}: .claude/projects/ CKP {scanned}파일 사용"
            )
        else:
            # 폴백: staged HANDOVER.md
            handover_path = AADS_ROOT / "aads-docs" / f"{project}-HANDOVER.md"
            if handover_path.exists():
                result.scanned_files = 1
                result.generated_files = [str(handover_path)]
                logger.info(f"[CKP] 원격 프로젝트 {project}: staged HANDOVER 사용")
            else:
                result.errors.append(
                    f"SSH 접근 불가, CKP 디렉토리·staged HANDOVER 없음: {project}"
                )

        result.duration_seconds = time.monotonic() - start

        # DB 메타데이터 기록
        if self.db and result.scanned_files > 0:
            await self._upsert_ckp_index(project, projects_ckp_dir, result)

        return result

    # ─── 증분 업데이트 ────────────────────────────────────────────────────────

    async def update_on_diff(self, project: str, changed_files: List[str]) -> None:
        """Git diff 기반 CKP 증분 업데이트."""
        logger.info(f"[CKP] 증분 업데이트: {project}, {len(changed_files)}개 파일")

        root = AADS_ROOT if project == "AADS" else AADS_ROOT / project.lower()
        ckp_dir = root / ".claude"
        ckp_dir.mkdir(exist_ok=True)

        # 변경된 .py / .ts 파일만 재스캔
        py_analyses: Dict[str, FileAnalysis] = {}
        ts_analyses: Dict[str, FileAnalysis] = {}

        for rel_path in changed_files:
            fpath = root / rel_path
            if not fpath.exists():
                continue
            try:
                content = fpath.read_text(encoding="utf-8", errors="ignore")
            except Exception:
                continue

            if rel_path.endswith(".py"):
                py_analyses[rel_path] = self.analyzer.analyze_python_file(content, rel_path)
            elif rel_path.endswith((".ts", ".tsx")):
                ts_analyses[rel_path] = self.analyzer.analyze_typescript_file(content, rel_path)

        # CODEBASE-MAP 갱신 (전체 재빌드 대신 변경 파일 섹션만 업데이트)
        if py_analyses or ts_analyses:
            now_str = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
            diff_note = f"\n\n## 최근 변경 ({now_str})\n변경된 파일: {', '.join(changed_files[:10])}\n"

            lessons_path = ckp_dir / "LESSONS.md"
            if lessons_path.exists():
                existing = lessons_path.read_text(encoding="utf-8")
                lessons_path.write_text(existing + diff_note, encoding="utf-8")

    # ─── CKP 요약 생성 ────────────────────────────────────────────────────────

    async def get_ckp_summary(self, project: str, max_tokens: int = 2000) -> str:
        """Context Builder에 주입할 CKP 요약 생성.
        AADS: .claude/ 직접 사용.
        원격 프로젝트: .claude/projects/{project}/ 우선, 없으면 AADS_ROOT/.claude/ fallback.
        """
        if project == "AADS":
            ckp_dir = AADS_ROOT / ".claude"
        else:
            projects_dir = AADS_ROOT / ".claude" / "projects" / project
            ckp_dir = projects_dir if projects_dir.exists() else AADS_ROOT / ".claude"

        parts = []
        used_tokens = 0

        # CLAUDE.md 전문
        claude_path = ckp_dir / "CLAUDE.md"
        if claude_path.exists():
            content = claude_path.read_text(encoding="utf-8")
            from app.core.token_utils import estimate_tokens as _est_tokens
            tokens = _est_tokens(content)
            if used_tokens + tokens <= max_tokens:
                parts.append(content)
                used_tokens += tokens

        # ARCHITECTURE.md 요약 (첫 50줄)
        arch_path = ckp_dir / "ARCHITECTURE.md"
        if arch_path.exists():
            lines = arch_path.read_text(encoding="utf-8").splitlines()[:50]
            snippet = "\n".join(lines)
            tokens = _est_tokens(snippet)
            if used_tokens + tokens <= max_tokens:
                parts.append(f"\n## Architecture (요약)\n{snippet}")
                used_tokens += tokens

        # 최근 LESSONS 5건
        lessons_path = ckp_dir / "LESSONS.md"
        if lessons_path.exists() and self.db:
            try:
                rows = await self.db.fetch(
                    """
                    SELECT title, description, source_task_id
                    FROM ckp_lessons
                    WHERE project = $1
                    ORDER BY created_at DESC
                    LIMIT 5
                    """,
                    project,
                )
                if rows:
                    lesson_txt = "\n## 최근 교훈\n"
                    for r in rows:
                        lesson_txt += f"- [{r['source_task_id']}] {r['title']}: {r['description'][:100]}\n"
                    tokens = _est_tokens(lesson_txt)
                    if used_tokens + tokens <= max_tokens:
                        parts.append(lesson_txt)
            except Exception as e:
                logger.debug(f"[CKP] lessons DB 조회 실패: {e}")

        return "\n".join(parts)

    # ─── CKP 검색 ─────────────────────────────────────────────────────────────

    async def search_ckp(self, project: str, query: str) -> List[CKPSearchResult]:
        """CKP 내에서 키워드 검색."""
        root = AADS_ROOT if project == "AADS" else AADS_ROOT / project.lower()
        ckp_dir = root / ".claude"
        results: List[CKPSearchResult] = []
        query_lower = query.lower()

        for ckp_file in ["CODEBASE-MAP.md", "DEPENDENCY-MAP.md"]:
            fpath = ckp_dir / ckp_file
            if not fpath.exists():
                continue
            content = fpath.read_text(encoding="utf-8")
            for i, line in enumerate(content.splitlines()):
                if query_lower in line.lower():
                    match_type = "function" if "def " in line or "function " in line else "file"
                    results.append(CKPSearchResult(
                        file_path=f".claude/{ckp_file}",
                        match_type=match_type,
                        match_text=line.strip(),
                        context=f"line {i + 1}",
                        relevance=1.0 if query_lower in line.lower()[:50] else 0.7,
                    ))

        # 관련성 높은 순 정렬
        results.sort(key=lambda x: x.relevance, reverse=True)
        return results[:20]

    # ─── CKP 파일 빌더 ───────────────────────────────────────────────────────

    def _build_codebase_map(
        self,
        py_analyses: Dict[str, FileAnalysis],
        ts_analyses: Dict[str, FileAnalysis],
        now_str: str,
    ) -> str:
        lines = [
            "# CODEBASE-MAP",
            f"_자동 생성: {now_str}_",
            "",
            "## Python 파일",
            "",
        ]

        # 크기순 정렬
        sorted_py = sorted(py_analyses.items(), key=lambda x: x[1].token_estimate, reverse=True)
        for rel_path, analysis in sorted_py[:80]:
            lines.append(f"### `{rel_path}` ({analysis.token_estimate}t)")
            for cls in analysis.classes:
                method_names = ", ".join(m.name for m in cls.methods[:5])
                lines.append(f"  - **class** `{cls.name}` [{method_names}]")
            for fn in analysis.functions[:5]:
                prefix = "async " if fn.is_async else ""
                deco = " ".join(fn.decorators[:2])
                lines.append(f"  - **{prefix}def** `{fn.name}{fn.signature}` {deco}")
            lines.append("")

        lines += ["", "## TypeScript/TSX 파일", ""]
        sorted_ts = sorted(ts_analyses.items(), key=lambda x: x[1].token_estimate, reverse=True)
        for rel_path, analysis in sorted_ts[:50]:
            exports_str = ", ".join(f"`{e}`" for e in analysis.exports[:5])
            lines.append(f"### `{rel_path}` → {exports_str}")

        return "\n".join(lines)

    def _build_dependency_map(
        self,
        dep_graph,
        py_files: Dict[str, str],
        now_str: str,
    ) -> str:
        lines = [
            "# DEPENDENCY-MAP",
            f"_자동 생성: {now_str}_",
            "",
            "## Python Import 그래프",
            "",
        ]

        # 그룹별 정리 (서비스가 어떤 서비스를 import하는지)
        by_source: Dict[str, List[str]] = {}
        for edge in dep_graph.edges:
            src = edge.source
            if src not in by_source:
                by_source[src] = []
            by_source[src].append(edge.target)

        for src, targets in sorted(by_source.items()):
            lines.append(f"- `{src}` → {', '.join(f'`{t}`' for t in targets[:5])}")

        if dep_graph.circular_deps:
            lines += ["", "## ⚠️ 순환 의존성", ""]
            for cycle in dep_graph.circular_deps:
                lines.append(f"- {' → '.join(cycle)}")

        lines += ["", "## 외부 패키지", ""]
        for pkg in dep_graph.external_packages[:30]:
            lines.append(f"- `{pkg}`")

        return "\n".join(lines)

    def _build_architecture_md(
        self,
        project: str,
        py_analyses: Dict[str, FileAnalysis],
        now_str: str,
    ) -> str:
        return f"""# ARCHITECTURE — {project}
_자동 생성: {now_str}_

## 시스템 다이어그램

```
CEO Chat → Intent Router → Model Selector → LLM
         ↓                                    ↓
    Tool Executor  ←─────────────────  Tool Loop
         ↓
    SSE Stream → 대시보드
```

## 데이터 흐름

```
Request
  → chat_service (세션 조회/저장)
  → context_builder (3계층 컨텍스트 조립)
  → intent_router (Gemini Flash-Lite 분류, ~200ms)
  → model_selector (LiteLLM 라우팅)
  → llm_call (Anthropic/OpenAI/Gemini)
  → tool_loop (최대 5회 반복)
  → SSE response (delta 스트리밍)
```

## DB 스키마 요약

| 테이블 | 설명 |
|--------|------|
| directive_lifecycle | 지시서 상태 추적 |
| chat_workspaces | 워크스페이스 |
| chat_sessions | 채팅 세션 |
| chat_messages | 메시지 이력 |
| ceo_facts | 컨텍스트 사실 |
| ckp_index | CKP 파일 메타데이터 |
| ckp_lessons | CKP 교훈 레코드 |

## 외부 의존성

- **LiteLLM Proxy** (http://litellm:4000) — 모델 통합 게이트웨이
- **Anthropic API** — Claude Opus/Sonnet/Haiku
- **Google Gemini API** — 인텐트 분류(Flash-Lite) + Deep Research
- **Brave Search API** — 웹 검색
- **PostgreSQL 15** (aads-postgres:5432) — 메인 DB
- **Redis** — 캐시 + Pub/Sub

## 기술 스택

- FastAPI 0.115 + Uvicorn + Python 3.11
- LangGraph 1.0.10 (멀티에이전트 오케스트레이션)
- Next.js 16 (대시보드)
- Docker Compose (프로덕션)
"""

    def _build_lessons_md(self, project: str, now_str: str) -> str:
        return f"""# LESSONS — {project}
_자동 생성: {now_str}_

## AADS-170~186 주요 이슈 및 해결

### AADS-170: Chat-First 시스템 구축
- **이슈**: asyncpg JSONB 배열 파싱 — `'['` 시작 문자열도 json.loads 필요
- **해결**: `_row_to_dict()`에서 `isinstance(v, str) and v.startswith('[')` 체크 추가
- **적용**: 모든 JSONB 컬럼 파싱 시 동일 패턴 사용

### AADS-182: SSE 타입 오류
- **이슈**: 프론트엔드에서 `"token"` 이벤트 수신 → 백엔드는 `"delta"` 전송
- **해결**: 프론트 SSEChunk 타입을 `delta/done/error`로 통일
- **교훈**: SSE 이벤트 타입은 백엔드-프론트 간 반드시 계약 문서화

### AADS-148: /proc grep 블로킹 장애
- **이슈**: `grep -r /proc` 실행으로 서버 211 3일 장애 (PID 20812)
- **해결**: `pgrep`, `ps`, `lsof` 전용 사용, `/proc grep -r` 절대 금지
- **교훈**: L-010 등록 — 좀비 프로세스 탐색은 PGID kill 체인 필수

### AADS-178: Preflight Check
- **이슈**: 중복 지시서 pending 발행으로 작업 충돌
- **해결**: `GET /api/v1/directives/preflight` API + pending/running 교차 검증

## 알려진 제약사항

- 서버 211 SSH 불안정 → bridge.py 경유 간접 접근
- Gemini Flash 직접 호출 금지 (LiteLLM proxy 경유 필수)
- claudebot → /root/.genspark/ 쓰기 불가 (root 소유)
- /proc grep -r 절대 금지 (pgrep/ps/lsof 사용)
- DB 호스트명: `aads-postgres` (Docker 내부), 외부: `localhost:5432`

## 향후 개선 사항

- CKP 원격 스캔 (SSH key 확보 후 서버 211/114 직접 스캔)
- CKP 벡터 검색 (pgvector 활용)
- 자동 지시서 생성 품질 개선 (CKP 기반 ACCEPTANCE_CRITERIA 자동 작성)
"""

    def _build_claude_md(
        self,
        project: str,
        py_analyses: Dict[str, FileAnalysis],
        ts_analyses: Dict[str, FileAnalysis],
        now_str: str,
    ) -> str:
        py_count = len(py_analyses)
        ts_count = len(ts_analyses)
        return f"""# AADS — Codebase Knowledge Package
_자동 생성: {now_str}_

## 프로젝트 개요
AADS(Autonomous AI Development System): 6개 서비스 자율 AI 개발/운영 시스템
CEO moongoby 전용, 서버 68(68.183.183.11)에서 실행

## 기술 스택
- **백엔드**: FastAPI 0.115, Python 3.11, LangGraph 1.0.10, asyncpg
- **프론트엔드**: Next.js 16, TypeScript, Tailwind CSS
- **DB**: PostgreSQL 15 (aads-postgres:5432)
- **인프라**: Docker Compose, Redis, LiteLLM Proxy
- **AI**: Anthropic (Claude Opus/Sonnet/Haiku), Google Gemini, OpenAI GPT

## 핵심 디렉토리 구조
```
aads-server/
  app/
    api/          # 34+ 엔드포인트 모듈
    services/     # 비즈니스 로직 (context_builder, intent_router, ckp_manager 등)
    models/       # Pydantic/dataclass 모델
    routers/      # chat v2 라우터
    agents/       # LangGraph 에이전트
    graphs/       # LangGraph 실행 체인
    mcp/          # MCP 클라이언트
    memory/       # 메모리 관리
  migrations/     # SQL 마이그레이션 (001~022)
  tests/          # 테스트

aads-dashboard/
  src/app/        # Next.js 앱 라우터
    chat/         # CEO Chat UI
    ops/          # Ops 대시보드
    managers/     # 매니저 페이지

.claude/          # CKP 파일 (현재 파일)
scripts/          # 파이프라인 스크립트
```

## 코딩 규칙
- **async/await** 필수 (I/O 작업 전체)
- **Pydantic v2** 모델 (BaseModel + field validators)
- **한국어 주석** (docstring 한국어)
- DB 연결: `asyncpg` pool, host=`aads-postgres`
- 에러 로깅: `logger = logging.getLogger(__name__)`

## 주요 파일 카운트
- Python: {py_count}개 파일
- TypeScript/TSX: {ts_count}개 파일

## 테스트 방법
```bash
cd /root/aads/aads-server
python -m pytest tests/ -v
```

## 배포 절차
```bash
docker compose -f docker-compose.prod.yml up -d --build aads-server
curl -s https://aads.newtalk.kr/api/v1/ops/health-check | python3 -m json.tool
```

## 주요 환경 변수
| 변수 | 설명 |
|------|------|
| DATABASE_URL | PostgreSQL 연결 (asyncpg) |
| ANTHROPIC_API_KEY | Claude API |
| LITELLM_BASE_URL | LiteLLM proxy (http://litellm:4000) |
| LITELLM_MASTER_KEY | LiteLLM 마스터 키 |
| BRAVE_API_KEY | Brave 검색 API |
| TELEGRAM_BOT_TOKEN | 텔레그램 알림 |
| TELEGRAM_CHAT_ID | CEO 채팅 ID |
"""

    # ─── DB 기록 ──────────────────────────────────────────────────────────────

    async def _upsert_ckp_index(
        self,
        project: str,
        ckp_dir: Path,
        scan_result: CKPScanResult,
    ) -> None:
        """ckp_index 테이블에 스캔 결과 UPSERT."""
        if not self.db:
            return
        try:
            now = datetime.now(timezone.utc)
            for fname, ftype in CKP_FILE_TYPES.items():
                fpath = ckp_dir / fname
                if not fpath.exists():
                    continue
                content = fpath.read_text(encoding="utf-8")
                from app.core.token_utils import estimate_tokens as _est_tokens
                token_count = _est_tokens(content)
                rel_path = f".claude/{fname}"
                await self.db.execute(
                    """
                    INSERT INTO ckp_index (project, file_path, file_type, token_count, last_scanned_at, updated_at)
                    VALUES ($1, $2, $3, $4, $5, $5)
                    ON CONFLICT (project, file_path)
                    DO UPDATE SET
                        file_type = EXCLUDED.file_type,
                        token_count = EXCLUDED.token_count,
                        last_scanned_at = EXCLUDED.last_scanned_at,
                        updated_at = EXCLUDED.updated_at
                    """,
                    project, rel_path, ftype, token_count, now,
                )
            logger.info(f"[CKP] ckp_index 업데이트 완료: {project}")
        except Exception as e:
            logger.warning(f"[CKP] ckp_index DB 기록 실패: {e}")
