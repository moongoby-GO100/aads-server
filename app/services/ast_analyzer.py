"""
AADS-186B: AST 분석기
Python: ast 모듈 기반 함수/클래스/import 추출
TypeScript/JS: regex 기반 export/import 추출
"""
from __future__ import annotations

import ast
import logging
import re
from typing import Dict, List, Optional

from app.models.ckp import (
    ClassInfo,
    DependencyEdge,
    DependencyGraph,
    FileAnalysis,
    FunctionInfo,
    ImportInfo,
)

logger = logging.getLogger(__name__)


class ASTAnalyzer:
    """Python/TypeScript 소스 코드 정적 분석기."""

    # ─── Python 분석 ─────────────────────────────────────────────────────────

    def analyze_python_file(self, content: str, file_path: str = "") -> FileAnalysis:
        """Python AST로 함수·클래스·import 추출."""
        analysis = FileAnalysis(file_path=file_path, language="python")
        try:
            tree = ast.parse(content)
        except SyntaxError as e:
            analysis.error = f"SyntaxError: {e}"
            analysis.token_estimate = len(content) // 4
            return analysis

        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    analysis.imports.append(ImportInfo(
                        module=alias.name,
                        alias=alias.asname,
                        is_from=False,
                    ))
            elif isinstance(node, ast.ImportFrom):
                module = node.module or ""
                names = [a.name for a in node.names]
                analysis.imports.append(ImportInfo(
                    module=module,
                    names=names,
                    is_from=True,
                ))

        # 최상위 클래스·함수
        for node in ast.iter_child_nodes(tree):
            if isinstance(node, ast.ClassDef):
                analysis.classes.append(self._parse_class(node))
            elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                analysis.functions.append(self._parse_function(node))

        analysis.token_estimate = len(content) // 4
        return analysis

    def _parse_function(
        self,
        node: ast.FunctionDef | ast.AsyncFunctionDef,
    ) -> FunctionInfo:
        """함수 노드 파싱."""
        decorators = []
        for d in node.decorator_list:
            if isinstance(d, ast.Attribute):
                decorators.append(f"@{ast.unparse(d)}")
            elif isinstance(d, ast.Name):
                decorators.append(f"@{d.id}")
            else:
                decorators.append(f"@{ast.unparse(d)}")

        # 시그니처 (파라미터)
        try:
            sig = ast.unparse(node.args)
        except Exception:
            sig = "..."

        docstring = ast.get_docstring(node)
        return FunctionInfo(
            name=node.name,
            signature=f"({sig})",
            docstring=docstring,
            decorators=decorators,
            is_async=isinstance(node, ast.AsyncFunctionDef),
            line_number=node.lineno,
        )

    def _parse_class(self, node: ast.ClassDef) -> ClassInfo:
        """클래스 노드 파싱."""
        bases = []
        for b in node.bases:
            try:
                bases.append(ast.unparse(b))
            except Exception:
                pass

        methods = []
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                methods.append(self._parse_function(child))

        docstring = ast.get_docstring(node)
        return ClassInfo(
            name=node.name,
            bases=bases,
            methods=methods,
            docstring=docstring,
            line_number=node.lineno,
        )

    # ─── TypeScript 분석 ──────────────────────────────────────────────────────

    def analyze_typescript_file(self, content: str, file_path: str = "") -> FileAnalysis:
        """Regex 기반 TS/TSX 분석."""
        analysis = FileAnalysis(file_path=file_path, language="typescript")

        # import 추출
        import_pattern = re.compile(
            r'import\s+(?:type\s+)?(?:\{([^}]+)\}|(\w+))\s+from\s+[\'"]([^\'"]+)[\'"]',
            re.MULTILINE,
        )
        for m in import_pattern.finditer(content):
            named = m.group(1)
            default = m.group(2)
            module = m.group(3)
            names = []
            if named:
                names = [n.strip().split(" as ")[0].strip() for n in named.split(",") if n.strip()]
            if default:
                names = [default]
            analysis.imports.append(ImportInfo(module=module, names=names, is_from=True))

        # export function/const/class 추출
        export_fn_pattern = re.compile(
            r'export\s+(?:default\s+)?(?:async\s+)?function\s+(\w+)',
            re.MULTILINE,
        )
        export_const_pattern = re.compile(
            r'export\s+(?:const|let|var)\s+(\w+)',
            re.MULTILINE,
        )
        export_class_pattern = re.compile(
            r'export\s+(?:default\s+)?class\s+(\w+)',
            re.MULTILINE,
        )
        export_default_pattern = re.compile(
            r'export\s+default\s+function\s+(\w+)',
            re.MULTILINE,
        )

        exports: List[str] = []
        for p in (export_fn_pattern, export_const_pattern, export_class_pattern, export_default_pattern):
            for m in p.finditer(content):
                name = m.group(1)
                if name not in exports:
                    exports.append(name)

        analysis.exports = exports

        # React 컴포넌트: export default function / export function Comp
        component_pattern = re.compile(
            r'(?:export\s+(?:default\s+)?function|export\s+const)\s+([A-Z]\w*)',
            re.MULTILINE,
        )
        for m in component_pattern.finditer(content):
            func = FunctionInfo(
                name=m.group(1),
                signature="(props)",
                is_async=False,
            )
            if func.name not in [f.name for f in analysis.functions]:
                analysis.functions.append(func)

        analysis.token_estimate = len(content) // 4
        return analysis

    # ─── 의존성 그래프 ────────────────────────────────────────────────────────

    def build_dependency_graph(
        self,
        analyses: Dict[str, FileAnalysis],
    ) -> DependencyGraph:
        """파일 간 import 관계 그래프 생성."""
        graph = DependencyGraph(nodes=list(analyses.keys()))
        external: set[str] = set()

        for src_path, analysis in analyses.items():
            for imp in analysis.imports:
                module = imp.module
                if not module:
                    continue

                # 내부 모듈 매핑 시도
                target = self._resolve_internal(module, src_path, analyses)
                if target:
                    graph.edges.append(DependencyEdge(
                        source=src_path,
                        target=target,
                        import_names=imp.names,
                    ))
                else:
                    # 외부 패키지 (최상위 모듈명만)
                    top = module.split(".")[0]
                    external.add(top)

        graph.external_packages = sorted(external)
        graph.circular_deps = self._find_circular_deps(graph)
        return graph

    def _resolve_internal(
        self,
        module: str,
        src_path: str,
        analyses: Dict[str, FileAnalysis],
    ) -> Optional[str]:
        """모듈명 → 내부 파일 경로 해결."""
        # app.services.xxx → app/services/xxx.py
        candidate = module.replace(".", "/") + ".py"
        for path in analyses:
            if path.endswith(candidate):
                return path
            if path.endswith(module.replace(".", "/") + "/") or \
               path.endswith("/" + module.split(".")[-1] + ".py"):
                return path
        return None

    def _find_circular_deps(self, graph: DependencyGraph) -> List[List[str]]:
        """DFS 기반 순환 의존성 탐지."""
        adj: Dict[str, List[str]] = {n: [] for n in graph.nodes}
        for edge in graph.edges:
            if edge.source in adj:
                adj[edge.source].append(edge.target)

        visited: set[str] = set()
        rec_stack: set[str] = set()
        cycles: List[List[str]] = []

        def dfs(node: str, path: List[str]) -> None:
            visited.add(node)
            rec_stack.add(node)
            for neighbor in adj.get(node, []):
                if neighbor not in visited:
                    dfs(neighbor, path + [neighbor])
                elif neighbor in rec_stack:
                    # 순환 탐지
                    cycle_start = path.index(neighbor) if neighbor in path else 0
                    cycle = path[cycle_start:] + [neighbor]
                    if cycle not in cycles:
                        cycles.append(cycle)
            rec_stack.discard(node)

        for node in graph.nodes:
            if node not in visited:
                dfs(node, [node])

        return cycles[:10]  # 최대 10개

    def get_impact_files(
        self,
        changed_file: str,
        graph: DependencyGraph,
    ) -> List[str]:
        """특정 파일 변경 시 영향받는 파일 목록 (역방향 탐색)."""
        # 역방향 인접 리스트
        reverse_adj: Dict[str, List[str]] = {}
        for edge in graph.edges:
            if edge.target not in reverse_adj:
                reverse_adj[edge.target] = []
            reverse_adj[edge.target].append(edge.source)

        affected: set[str] = set()
        queue = [changed_file]
        while queue:
            current = queue.pop(0)
            for dependent in reverse_adj.get(current, []):
                if dependent not in affected:
                    affected.add(dependent)
                    queue.append(dependent)

        return sorted(affected)
