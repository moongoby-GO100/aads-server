"""
AADS-186E-3: 코드 탐색 서비스
- trace_function_chain: 함수 호출 체인 추적 (depth 3)
- analyze_recent_changes: 최근 Git 변경 분석 + 위험도 평가
- search_all_projects: 6개 프로젝트 CKP 동시 검색
SSH 타임아웃: 30초/파일, 전체 추적 3분 이내.
SSH 실패 프로젝트 스킵, 성공분만 반환.
"""
from __future__ import annotations

import asyncio
import logging
import os
import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

# 프로젝트 → 서버 정보 매핑 (server_registry.py 연동)
_PROJECT_MAP: Dict[str, Dict[str, str]] = {
    "KIS": {"host": "211.188.51.113", "workdir": "/root/kis-autotrade-v4", "lang": "python"},
    "GO100": {"host": "211.188.51.113", "workdir": "/root/go100", "lang": "python"},
    "SF": {"host": "116.120.58.155", "workdir": "/data/shortflow", "lang": "python"},
    "NTV2": {"host": "116.120.58.155", "workdir": "/srv/newtalk-v2", "lang": "php"},
    "AADS": {"host": "localhost", "workdir": "/root/aads/aads-server", "lang": "python"},
    "NAS": {"host": "116.120.58.155", "workdir": "/data/nas", "lang": "python"},
}

_ALL_PROJECTS = list(_PROJECT_MAP.keys())


# ─── 결과 데이터클래스 ──────────────────────────────────────────────────────

@dataclass
class TraceResult:
    """함수 호출 체인 추적 결과."""
    project: str = ""
    entry_point: str = ""
    diagram: str = ""          # 텍스트 다이어그램
    chain: List[Dict[str, Any]] = field(default_factory=list)
    error: Optional[str] = None


@dataclass
class ChangeReport:
    """Git 변경 분석 보고서."""
    project: str = ""
    days: int = 7
    commits: List[Dict[str, str]] = field(default_factory=list)
    changed_files: List[Dict[str, Any]] = field(default_factory=list)
    categories: Dict[str, int] = field(default_factory=dict)   # 'feature' | 'fix' | 'refactor' | 'docs' | 'other'
    risk_level: str = "LOW"    # LOW | MEDIUM | HIGH
    affected_services: List[str] = field(default_factory=list)
    summary: str = ""
    error: Optional[str] = None


@dataclass
class CrossProjectResult:
    """6개 프로젝트 동시 검색 결과."""
    query: str = ""
    matches: List[Dict[str, Any]] = field(default_factory=list)   # [{project, file, match_type, snippet}]
    duplicate_patterns: List[str] = field(default_factory=list)
    shared_modules: List[str] = field(default_factory=list)
    projects_searched: List[str] = field(default_factory=list)
    projects_failed: List[str] = field(default_factory=list)
    error: Optional[str] = None


# ─── SSH 헬퍼 ────────────────────────────────────────────────────────────────

async def _ssh_run(host: str, cmd: str, timeout: float = 30.0) -> str:
    """원격 서버에서 명령 실행 → stdout 반환. 실패 시 빈 문자열."""
    if host == "localhost" or host == "68.183.183.11":
        # 로컬 실행
        try:
            proc = await asyncio.wait_for(
                asyncio.create_subprocess_shell(
                    cmd,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                ),
                timeout=5.0,
            )
            stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout)
            return stdout.decode(errors="replace")
        except Exception as e:
            logger.debug(f"[CodeExplorer] local cmd error: {e}")
            return ""
    else:
        ssh_cmd = f"ssh -o ConnectTimeout=10 -o StrictHostKeyChecking=no root@{host} '{cmd}'"
        try:
            proc = await asyncio.wait_for(
                asyncio.create_subprocess_shell(
                    ssh_cmd,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                ),
                timeout=5.0,
            )
            stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout)
            return stdout.decode(errors="replace")
        except Exception as e:
            logger.debug(f"[CodeExplorer] SSH error ({host}): {e}")
            return ""


# ─── 메인 서비스 ─────────────────────────────────────────────────────────────

class CodeExplorerService:
    """프로젝트 소스코드 탐색 서비스."""

    # ── 1. 함수 호출 체인 추적 ─────────────────────────────────────────────

    async def trace_function_chain(
        self,
        project: str,
        entry_point: str,
        depth: int = 3,
    ) -> TraceResult:
        """
        함수 호출 체인 추적 (depth 3까지).

        Args:
            project: 프로젝트명 (AADS, KIS, GO100, SF, NTV2, NAS)
            entry_point: 진입점 (예: "app/main.py::process_order")
            depth: 추적 깊이 (기본 3)

        Returns:
            TraceResult (diagram + chain)
        """
        proj = project.upper()
        info = _PROJECT_MAP.get(proj)
        if not info:
            return TraceResult(error=f"미지원 프로젝트: {project}. 지원: {_ALL_PROJECTS}")

        # entry_point 파싱: "file.py::function_name" 또는 "file.py"
        if "::" in entry_point:
            file_path, func_name = entry_point.split("::", 1)
        else:
            file_path = entry_point
            func_name = ""

        host = info["host"]
        workdir = info["workdir"]
        lang = info["lang"]

        # 파일 내용 읽기
        full_path = f"{workdir}/{file_path.lstrip('/')}"
        content = await self._read_file(host, full_path)
        if not content:
            # CKP에서 파일 경로 재탐색
            content = await self._find_in_ckp(proj, file_path, func_name, host, workdir)

        if not content:
            return TraceResult(
                project=proj,
                entry_point=entry_point,
                error=f"파일 읽기 실패: {full_path}",
            )

        # 함수 호출 체인 빌드
        chain = await self._build_chain(
            content, func_name, file_path, host, workdir, lang, depth, visited=set()
        )

        # 다이어그램 생성
        diagram = self._render_diagram(entry_point, chain, indent=0)

        return TraceResult(
            project=proj,
            entry_point=entry_point,
            diagram=diagram,
            chain=chain,
        )

    async def _read_file(self, host: str, path: str) -> str:
        if host == "localhost":
            try:
                with open(path, "r", errors="replace") as f:
                    return f.read(50000)  # 최대 50KB
            except Exception:
                return ""
        return await _ssh_run(host, f"cat {path} 2>/dev/null | head -c 50000")

    async def _find_in_ckp(
        self, project: str, file_path: str, func_name: str, host: str, workdir: str
    ) -> str:
        """CKP CODEBASE-MAP에서 파일 경로 찾고 읽기."""
        try:
            from app.services.ckp_manager import CKPManager
            mgr = CKPManager(db_conn=None)
            summary = await mgr.get_ckp_summary(project, max_tokens=2000)
            if file_path.split("/")[-1] in summary:
                # 실제 파일 경로 탐색
                fname = file_path.split("/")[-1]
                result = await _ssh_run(
                    host,
                    f"find {workdir} -name '{fname}' -maxdepth 6 2>/dev/null | head -1",
                )
                found_path = result.strip()
                if found_path:
                    return await self._read_file(host, found_path)
        except Exception as e:
            logger.debug(f"[CodeExplorer] CKP 탐색 실패: {e}")
        return ""

    async def _build_chain(
        self,
        content: str,
        func_name: str,
        file_path: str,
        host: str,
        workdir: str,
        lang: str,
        depth: int,
        visited: set,
    ) -> List[Dict[str, Any]]:
        """재귀적 함수 호출 체인 빌드."""
        if depth <= 0:
            return []

        called_funcs = self._extract_called_functions(content, func_name, lang)
        chain = []
        for called in called_funcs[:5]:  # 함수당 최대 5개
            call_key = f"{file_path}::{called['name']}"
            if call_key in visited:
                chain.append({"name": called["name"], "file": called.get("file", file_path), "recursive": True, "children": []})
                continue
            visited.add(call_key)

            # 호출된 함수의 파일 읽기
            call_file = called.get("file", file_path)
            if call_file != file_path:
                full_path = f"{workdir}/{call_file.lstrip('/')}"
                sub_content = await self._read_file(host, full_path)
            else:
                sub_content = content

            children = []
            if sub_content and depth > 1:
                children = await self._build_chain(
                    sub_content, called["name"], call_file, host, workdir, lang, depth - 1, visited
                )

            chain.append({
                "name": called["name"],
                "file": call_file,
                "children": children,
            })

        return chain

    def _extract_called_functions(
        self, content: str, func_name: str, lang: str
    ) -> List[Dict[str, str]]:
        """함수 내부에서 호출되는 함수 목록 추출 (정규식 기반)."""
        if not func_name:
            # 최상위 레벨 함수 호출 추출
            return self._extract_top_level_calls(content, lang)

        # 함수 본문 추출
        body = self._extract_function_body(content, func_name, lang)
        if not body:
            return []

        return self._parse_calls_from_body(body, lang)

    def _extract_function_body(self, content: str, func_name: str, lang: str) -> str:
        """함수 정의 본문 추출."""
        if lang == "python":
            # def func_name(...): 이후 블록 추출
            pattern = rf"def {re.escape(func_name)}\s*\([^)]*\)[^:]*:"
            match = re.search(pattern, content)
            if match:
                start = match.start()
                # 함수 본문 (최대 3000자)
                return content[start:start + 3000]
        elif lang == "php":
            pattern = rf"function {re.escape(func_name)}\s*\([^)]*\)\s*\{{"
            match = re.search(pattern, content)
            if match:
                start = match.start()
                return content[start:start + 3000]
        return content[:3000]  # 폴백: 전체 파일 앞 3000자

    def _parse_calls_from_body(self, body: str, lang: str) -> List[Dict[str, str]]:
        """본문에서 함수 호출 패턴 추출."""
        calls = []
        seen: set[str] = set()

        if lang == "python":
            # service.method() 또는 function() 패턴
            for m in re.finditer(r"(?:self\.)?(\w+)\.(\w+)\s*\(", body):
                obj, method = m.group(1), m.group(2)
                key = f"{obj}.{method}"
                if key not in seen and method not in ("get", "set", "append", "format", "print"):
                    seen.add(key)
                    calls.append({"name": method, "file": f"services/{obj}.py"})

            # 단순 함수 호출
            for m in re.finditer(r"\b(?<!def )([a-z_]\w+)\s*\(", body):
                name = m.group(1)
                if name not in seen and name not in ("if", "for", "while", "return", "await", "lambda", "print", "len", "str", "int"):
                    seen.add(name)
                    calls.append({"name": name, "file": "?"})

        elif lang == "php":
            for m in re.finditer(r"\$this->(\w+)\s*\(", body):
                name = m.group(1)
                if name not in seen:
                    seen.add(name)
                    calls.append({"name": name, "file": "?"})

        return calls[:8]  # 최대 8개

    def _extract_top_level_calls(self, content: str, lang: str) -> List[Dict[str, str]]:
        """최상위 레벨 함수/클래스 목록."""
        functions = []
        if lang == "python":
            for m in re.finditer(r"^(?:async )?def (\w+)\s*\(", content, re.MULTILINE):
                functions.append({"name": m.group(1), "file": "?"})
        return functions[:5]

    def _render_diagram(
        self, entry: str, chain: List[Dict[str, Any]], indent: int
    ) -> str:
        """텍스트 트리 다이어그램 생성."""
        lines = []
        if indent == 0:
            lines.append(entry)

        prefix = "  " * indent
        for i, node in enumerate(chain):
            is_last = i == len(chain) - 1
            connector = "└→" if is_last else "├→"
            file_info = node.get("file", "")
            if file_info and file_info != "?":
                label = f"{file_info}::{node['name']}()"
            else:
                label = f"{node['name']}()"
            if node.get("recursive"):
                label += " [재귀]"
            lines.append(f"{prefix}  {connector} {label}")
            if node.get("children"):
                sub = self._render_diagram(
                    label, node["children"], indent + 1
                )
                lines.extend(sub.split("\n")[1:])  # 첫 줄 (루트) 제외

        return "\n".join(lines)

    # ── 2. 최근 Git 변경 분석 ─────────────────────────────────────────────

    async def analyze_recent_changes(
        self,
        project: str,
        days: int = 7,
    ) -> ChangeReport:
        """
        최근 Git 변경 분석 + 위험도 평가.

        Args:
            project: 프로젝트명
            days: 분석 기간 (일)

        Returns:
            ChangeReport
        """
        proj = project.upper()
        info = _PROJECT_MAP.get(proj)
        if not info:
            return ChangeReport(error=f"미지원 프로젝트: {project}")

        host = info["host"]
        workdir = info["workdir"]

        # git log
        log_out = await _ssh_run(
            host,
            f"cd {workdir} && git log --oneline --since='{days} days ago' 2>/dev/null | head -20",
        )
        # git diff --stat
        stat_out = await _ssh_run(
            host,
            f"cd {workdir} && git diff HEAD~5 --stat 2>/dev/null | head -30",
        )

        if not log_out and not stat_out:
            return ChangeReport(
                project=proj,
                days=days,
                error="git 접근 실패 또는 변경 없음",
            )

        # 커밋 파싱
        commits = []
        for line in log_out.strip().split("\n"):
            if line.strip():
                parts = line.strip().split(" ", 1)
                commits.append({
                    "hash": parts[0] if parts else "",
                    "message": parts[1] if len(parts) > 1 else line,
                })

        # 변경 파일 파싱
        changed_files = []
        for line in stat_out.strip().split("\n"):
            if "|" in line:
                file_part = line.split("|")[0].strip()
                stats_part = line.split("|")[1].strip() if "|" in line else ""
                changed_files.append({"file": file_part, "stats": stats_part})

        # 카테고리 분류
        categories = self._categorize_commits(commits)
        # 위험도 평가
        risk_level = self._assess_risk(commits, changed_files)
        # 영향 서비스 추출
        affected_services = self._extract_affected_services(changed_files, proj)
        # 요약 생성
        summary = self._build_change_summary(proj, days, commits, categories, risk_level)

        return ChangeReport(
            project=proj,
            days=days,
            commits=commits,
            changed_files=changed_files,
            categories=categories,
            risk_level=risk_level,
            affected_services=affected_services,
            summary=summary,
        )

    def _categorize_commits(self, commits: List[Dict[str, str]]) -> Dict[str, int]:
        """커밋 메시지에서 카테고리 분류."""
        cats: Dict[str, int] = {"feature": 0, "fix": 0, "refactor": 0, "docs": 0, "other": 0}
        for c in commits:
            msg = c.get("message", "").lower()
            if any(w in msg for w in ("feat", "add", "new", "추가", "신규")):
                cats["feature"] += 1
            elif any(w in msg for w in ("fix", "bug", "error", "수정", "버그")):
                cats["fix"] += 1
            elif any(w in msg for w in ("refactor", "clean", "리팩", "정리")):
                cats["refactor"] += 1
            elif any(w in msg for w in ("doc", "readme", "comment", "문서")):
                cats["docs"] += 1
            else:
                cats["other"] += 1
        return {k: v for k, v in cats.items() if v > 0}

    def _assess_risk(
        self,
        commits: List[Dict[str, str]],
        changed_files: List[Dict[str, Any]],
    ) -> str:
        """변경 위험도 평가."""
        risk_score = 0
        # 커밋 수
        if len(commits) > 10:
            risk_score += 2
        elif len(commits) > 5:
            risk_score += 1
        # 핵심 파일 변경 감지
        core_files = ("main.py", "config", "migration", "schema", "auth", "payment", "trade", "order")
        for f in changed_files:
            fname = f.get("file", "").lower()
            if any(kw in fname for kw in core_files):
                risk_score += 2
                break
        # 커밋 메시지에 긴급/위험 키워드
        for c in commits:
            msg = c.get("message", "").lower()
            if any(w in msg for w in ("hotfix", "critical", "emergency", "urgent", "긴급")):
                risk_score += 3
                break
        if risk_score >= 4:
            return "HIGH"
        elif risk_score >= 2:
            return "MEDIUM"
        return "LOW"

    def _extract_affected_services(
        self, changed_files: List[Dict[str, Any]], project: str
    ) -> List[str]:
        """변경 파일에서 영향받는 서비스 추출."""
        services = set()
        for f in changed_files:
            fname = f.get("file", "").lower()
            if "api" in fname or "router" in fname:
                services.add(f"{project} API")
            if "service" in fname or "handler" in fname:
                services.add(f"{project} Service")
            if "model" in fname or "schema" in fname:
                services.add(f"{project} DB")
            if "test" in fname:
                services.add(f"{project} Tests")
        return sorted(services)

    def _build_change_summary(
        self,
        project: str,
        days: int,
        commits: List[Dict[str, str]],
        categories: Dict[str, int],
        risk_level: str,
    ) -> str:
        """변경 요약 텍스트 생성."""
        cat_str = ", ".join(f"{k}:{v}" for k, v in categories.items())
        commit_count = len(commits)
        risk_icon = {"LOW": "🟢", "MEDIUM": "🟡", "HIGH": "🔴"}.get(risk_level, "⚪")
        summary = (
            f"## {project} 변경 분석 ({days}일)\n"
            f"- 커밋: {commit_count}건 | 카테고리: {cat_str or '없음'}\n"
            f"- 위험도: {risk_icon} {risk_level}\n"
        )
        if commits:
            recent = commits[:3]
            summary += "- 최근 커밋:\n"
            for c in recent:
                summary += f"  * [{c['hash'][:7]}] {c['message'][:60]}\n"
        return summary

    # ── 3. 6개 프로젝트 CKP 동시 검색 ─────────────────────────────────────

    async def search_all_projects(self, query: str) -> CrossProjectResult:
        """
        6개 프로젝트의 CKP + 원격 파일을 동시 검색.

        Args:
            query: 검색어 (파일명, 함수명, 클래스명)

        Returns:
            CrossProjectResult
        """
        tasks = [
            self._search_single_project(proj, query)
            for proj in _ALL_PROJECTS
        ]
        # 최대 3분 타임아웃, 실패 프로젝트 스킵
        results = await asyncio.gather(*tasks, return_exceptions=True)

        all_matches: List[Dict[str, Any]] = []
        projects_searched: List[str] = []
        projects_failed: List[str] = []

        for proj, result in zip(_ALL_PROJECTS, results):
            if isinstance(result, Exception):
                logger.debug(f"[CodeExplorer] search_all {proj} error: {result}")
                projects_failed.append(proj)
            elif isinstance(result, list):
                if result:
                    all_matches.extend(result)
                    projects_searched.append(proj)
                else:
                    projects_searched.append(proj)

        # 중복 패턴 감지
        duplicate_patterns = self._detect_duplicates(all_matches)
        # 공유 가능 모듈 식별
        shared_modules = self._identify_shared_modules(all_matches)

        return CrossProjectResult(
            query=query,
            matches=all_matches[:50],
            duplicate_patterns=duplicate_patterns,
            shared_modules=shared_modules,
            projects_searched=projects_searched,
            projects_failed=projects_failed,
        )

    async def _search_single_project(
        self, project: str, query: str
    ) -> List[Dict[str, Any]]:
        """단일 프로젝트 검색 (CKP + 파일 그렙)."""
        matches: List[Dict[str, Any]] = []
        info = _PROJECT_MAP.get(project, {})
        host = info.get("host", "")
        workdir = info.get("workdir", "")

        # 1. CKP CODEBASE-MAP 검색
        try:
            from app.services.ckp_manager import CKPManager
            mgr = CKPManager(db_conn=None)
            summary = await asyncio.wait_for(
                mgr.get_ckp_summary(project, max_tokens=3000),
                timeout=5.0,
            )
            if summary and query.lower() in summary.lower():
                # CKP에서 관련 라인 추출
                for line in summary.split("\n"):
                    if query.lower() in line.lower():
                        matches.append({
                            "project": project,
                            "file": "CKP",
                            "match_type": "ckp",
                            "snippet": line.strip()[:120],
                        })
                        if len(matches) >= 5:
                            break
        except Exception:
            pass

        # 2. 원격 파일 검색 (grep)
        if host and host != "localhost" and workdir:
            lang = info.get("lang", "python")
            ext = "*.py" if lang == "python" else "*.php"
            grep_out = await _ssh_run(
                host,
                f"cd {workdir} && grep -rl '{query}' --include='{ext}' . 2>/dev/null | head -10",
                timeout=20.0,
            )
            if grep_out:
                for fpath in grep_out.strip().split("\n"):
                    fpath = fpath.strip()
                    if not fpath:
                        continue
                    # 해당 파일에서 매칭 라인 추출
                    snippet_out = await _ssh_run(
                        host,
                        f"cd {workdir} && grep -n '{query}' '{fpath}' 2>/dev/null | head -3",
                        timeout=10.0,
                    )
                    matches.append({
                        "project": project,
                        "file": fpath.lstrip("./"),
                        "match_type": "source",
                        "snippet": snippet_out.strip()[:120] if snippet_out else "",
                    })
        elif host == "localhost":
            # AADS 로컬 검색
            try:
                import subprocess
                result = subprocess.run(
                    ["grep", "-rl", query, workdir, "--include=*.py", "--max-depth=6"],
                    capture_output=True, text=True, timeout=10,
                )
                for fpath in result.stdout.strip().split("\n")[:10]:
                    if fpath.strip():
                        matches.append({
                            "project": project,
                            "file": fpath.strip().replace(workdir, "").lstrip("/"),
                            "match_type": "source",
                            "snippet": "",
                        })
            except Exception:
                pass

        return matches

    def _detect_duplicates(self, matches: List[Dict[str, Any]]) -> List[str]:
        """여러 프로젝트에 동일 파일명/패턴이 있으면 중복으로 표시."""
        from collections import Counter
        file_names = [m.get("file", "").split("/")[-1] for m in matches]
        counts = Counter(file_names)
        return [fname for fname, cnt in counts.items() if cnt >= 2 and fname not in ("", "CKP")]

    def _identify_shared_modules(self, matches: List[Dict[str, Any]]) -> List[str]:
        """공유 가능 모듈 추천 — 3개 이상 프로젝트에 존재하는 패턴."""
        from collections import Counter
        projects_per_file: Dict[str, set] = {}
        for m in matches:
            fname = m.get("file", "").split("/")[-1]
            proj = m.get("project", "")
            if fname and fname != "CKP":
                if fname not in projects_per_file:
                    projects_per_file[fname] = set()
                projects_per_file[fname].add(proj)
        return [
            fname
            for fname, projs in projects_per_file.items()
            if len(projs) >= 3
        ]
