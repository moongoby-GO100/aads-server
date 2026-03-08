"""
AADS-186B: CKP(Codebase Knowledge Package) 데이터 모델
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Dict, List, Optional


# ─── DB 레코드 모델 ──────────────────────────────────────────────────────────

@dataclass
class CKPIndexRecord:
    """ckp_index 테이블 레코드."""
    project: str
    file_path: str
    file_type: str  # 'claude_md', 'architecture', 'codebase_map', 'dependency_map', 'lessons'
    token_count: int = 0
    last_scanned_at: Optional[datetime] = None
    last_commit_sha: Optional[str] = None
    id: Optional[int] = None
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None


@dataclass
class CKPLessonRecord:
    """ckp_lessons 테이블 레코드."""
    project: str
    title: str
    description: str
    category: str = "pattern"  # 'bug_fix', 'architecture_decision', 'performance', 'security', 'pattern'
    related_files: List[str] = field(default_factory=list)
    source_task_id: Optional[str] = None
    id: Optional[int] = None
    created_at: Optional[datetime] = None


# ─── 분석 결과 모델 ──────────────────────────────────────────────────────────

@dataclass
class FunctionInfo:
    """Python 함수/메서드 정보."""
    name: str
    signature: str
    docstring: Optional[str] = None
    decorators: List[str] = field(default_factory=list)
    is_async: bool = False
    line_number: int = 0


@dataclass
class ClassInfo:
    """Python 클래스 정보."""
    name: str
    bases: List[str] = field(default_factory=list)
    methods: List[FunctionInfo] = field(default_factory=list)
    docstring: Optional[str] = None
    line_number: int = 0


@dataclass
class ImportInfo:
    """import 정보."""
    module: str
    names: List[str] = field(default_factory=list)
    alias: Optional[str] = None
    is_from: bool = False


@dataclass
class FileAnalysis:
    """단일 파일 분석 결과."""
    file_path: str
    language: str  # 'python', 'typescript', 'other'
    classes: List[ClassInfo] = field(default_factory=list)
    functions: List[FunctionInfo] = field(default_factory=list)
    imports: List[ImportInfo] = field(default_factory=list)
    exports: List[str] = field(default_factory=list)  # TS exports
    token_estimate: int = 0
    error: Optional[str] = None


@dataclass
class DependencyEdge:
    """파일 간 의존성 엣지."""
    source: str  # 파일 경로
    target: str  # 파일 경로
    import_names: List[str] = field(default_factory=list)


@dataclass
class DependencyGraph:
    """프로젝트 전체 의존성 그래프."""
    nodes: List[str] = field(default_factory=list)  # 파일 경로 목록
    edges: List[DependencyEdge] = field(default_factory=list)
    circular_deps: List[List[str]] = field(default_factory=list)  # 순환 의존성
    external_packages: List[str] = field(default_factory=list)


# ─── 스캔 결과 모델 ──────────────────────────────────────────────────────────

@dataclass
class CKPScanResult:
    """CKP 스캔 완료 결과."""
    project: str
    scanned_files: int = 0
    total_tokens: int = 0
    generated_files: List[str] = field(default_factory=list)
    errors: List[str] = field(default_factory=list)
    duration_seconds: float = 0.0
    commit_sha: Optional[str] = None


@dataclass
class CKPSearchResult:
    """CKP 검색 결과 아이템."""
    file_path: str
    match_type: str  # 'function', 'class', 'import', 'file'
    match_text: str
    context: str = ""
    relevance: float = 1.0


# ─── CTO 모드 결과 모델 ──────────────────────────────────────────────────────

@dataclass
class DirectiveResult:
    """자동 생성된 지시서 결과."""
    task_id: str
    title: str
    content: str
    submitted: bool = False
    dry_run: bool = False
    error: Optional[str] = None


@dataclass
class VerificationResult:
    """작업 결과 검증 보고서."""
    task_id: str
    commit_sha: Optional[str] = None
    checked_files: List[str] = field(default_factory=list)
    passed: List[str] = field(default_factory=list)
    failed: List[str] = field(default_factory=list)
    missing: List[str] = field(default_factory=list)
    summary: str = ""


@dataclass
class ImpactReport:
    """변경 사전 영향 분석 보고서."""
    target_files: List[str] = field(default_factory=list)
    affected_files: List[str] = field(default_factory=list)
    affected_services: List[str] = field(default_factory=list)
    risk_level: str = "LOW"  # 'LOW', 'MEDIUM', 'HIGH'
    summary: str = ""


@dataclass
class TechDebtItem:
    """기술 부채 항목."""
    file_path: str
    line_number: int
    tag: str  # 'TODO', 'FIXME', 'HACK', 'XXX', 'DEPRECATED'
    content: str
    priority: str = "MEDIUM"


@dataclass
class TechDebtReport:
    """기술 부채 전체 보고서."""
    project: str
    items: List[TechDebtItem] = field(default_factory=list)
    by_tag: Dict[str, int] = field(default_factory=dict)
    by_file: Dict[str, int] = field(default_factory=dict)
    total: int = 0
    summary: str = ""
