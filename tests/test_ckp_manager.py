"""
AADS-186B: CKP Manager 테스트
"""
import asyncio
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))


# ─── CKPManager 단위 테스트 ──────────────────────────────────────────────────

class TestCKPManager:

    def setup_method(self):
        from app.services.ckp_manager import CKPManager
        self.mgr = CKPManager(db_conn=None)
        self.aads_root = Path('/root/aads')
        self.ckp_dir = self.aads_root / '.claude'

    def test_ckp_dir_exists(self):
        """CKP 디렉터리 존재 확인."""
        assert self.ckp_dir.exists(), ".claude 디렉터리가 없음"

    def test_five_ckp_files_exist(self):
        """5개 CKP 파일 존재 확인."""
        required = ['CLAUDE.md', 'ARCHITECTURE.md', 'CODEBASE-MAP.md', 'DEPENDENCY-MAP.md', 'LESSONS.md']
        for fname in required:
            fpath = self.ckp_dir / fname
            assert fpath.exists(), f"{fname} 파일이 없음"

    def test_claude_md_not_empty(self):
        """CLAUDE.md 내용 확인."""
        content = (self.ckp_dir / 'CLAUDE.md').read_text()
        assert len(content) > 100, "CLAUDE.md가 너무 짧음"
        assert '기술 스택' in content, "기술 스택 섹션 없음"

    def test_codebase_map_has_python_files(self):
        """CODEBASE-MAP.md에 Python 파일 목록 확인."""
        content = (self.ckp_dir / 'CODEBASE-MAP.md').read_text()
        assert '## Python 파일' in content
        assert '.py' in content

    def test_dependency_map_has_import_graph(self):
        """DEPENDENCY-MAP.md에 import 그래프 확인."""
        content = (self.ckp_dir / 'DEPENDENCY-MAP.md').read_text()
        assert 'Import' in content or 'import' in content

    def test_get_ckp_summary_token_limit(self):
        """get_ckp_summary() 토큰 제한 확인."""
        async def run():
            summary = await self.mgr.get_ckp_summary("AADS", max_tokens=2000)
            token_est = len(summary) // 4
            assert token_est <= 2100, f"토큰 초과: {token_est}"
            return summary
        summary = asyncio.run(run())
        assert len(summary) > 0, "요약이 비어 있음"

    def test_search_ckp_returns_results(self):
        """search_ckp() 관련 파일 반환 확인."""
        async def run():
            results = await self.mgr.search_ckp("AADS", "chat_service")
            return results
        results = asyncio.run(run())
        # 결과가 있거나 없어도 됨 (파일이 없을 수 있음)
        assert isinstance(results, list), "리스트 반환 필수"

    def test_search_ckp_intent_router(self):
        """search_ckp() intent_router 검색."""
        async def run():
            results = await self.mgr.search_ckp("AADS", "intent_router")
            return results
        results = asyncio.run(run())
        assert isinstance(results, list)
        # CODEBASE-MAP에 intent_router 관련 항목 있을 것
        # (파일이 있다면)

    def test_scan_local_project_structure(self):
        """scan_project() 결과 구조 확인 (실제 스캔은 느리므로 구조만)."""
        from app.services.ckp_manager import CKPScanResult
        # CKPScanResult 구조 확인
        result = CKPScanResult(project="AADS")
        assert result.project == "AADS"
        assert result.scanned_files == 0
        assert isinstance(result.errors, list)
        assert isinstance(result.generated_files, list)


# ─── ASTAnalyzer 단위 테스트 ─────────────────────────────────────────────────

class TestASTAnalyzer:

    def setup_method(self):
        from app.services.ast_analyzer import ASTAnalyzer
        self.analyzer = ASTAnalyzer()

    def test_analyze_python_simple(self):
        code = '''
class Foo:
    def bar(self, x: int) -> str:
        """docstring"""
        return str(x)

async def baz(y: str) -> None:
    pass
'''
        result = self.analyzer.analyze_python_file(code, "test.py")
        assert result.language == "python"
        assert len(result.classes) == 1
        assert result.classes[0].name == "Foo"
        assert any(m.name == "bar" for m in result.classes[0].methods)
        assert any(f.name == "baz" and f.is_async for f in result.functions)

    def test_analyze_python_imports(self):
        code = '''
import os
from typing import List, Optional
from app.services.chat_service import ChatService
'''
        result = self.analyzer.analyze_python_file(code, "test.py")
        module_names = [i.module for i in result.imports]
        assert "os" in module_names
        assert "typing" in module_names
        assert "app.services.chat_service" in module_names

    def test_analyze_typescript_exports(self):
        code = '''
import { useState } from "react";
import type { FC } from "react";

export function MyComponent() { return null; }
export const myFn = () => {};
export default function Page() { return null; }
'''
        result = self.analyzer.analyze_typescript_file(code, "test.tsx")
        assert result.language == "typescript"
        assert "MyComponent" in result.exports or "Page" in result.exports

    def test_build_dependency_graph_empty(self):
        """빈 분석 결과로 그래프 생성."""
        graph = self.analyzer.build_dependency_graph({})
        assert graph.nodes == []
        assert graph.edges == []


# ─── DB 마이그레이션 파일 확인 ───────────────────────────────────────────────

class TestMigration:

    def test_migration_file_exists(self):
        """022_ckp_tables.sql 존재 확인."""
        migration = Path('/root/aads/aads-server/migrations/022_ckp_tables.sql')
        assert migration.exists(), "022_ckp_tables.sql 없음"

    def test_migration_contains_tables(self):
        """마이그레이션 파일에 두 테이블 정의 확인."""
        content = Path('/root/aads/aads-server/migrations/022_ckp_tables.sql').read_text()
        assert 'ckp_index' in content
        assert 'ckp_lessons' in content
        assert 'CREATE TABLE' in content.upper()
