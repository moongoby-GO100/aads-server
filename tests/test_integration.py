"""
AADS-186D: 통합 테스트 — 7개 시나리오
186A(도구 고도화) + 186B(CKP+CTO) + 186C(Observability+MCP+Telegram) + 186D(통합+원격CKP+ToolSearch+Caching)
각 시나리오는 실제 외부 API 없이 단위 검증 가능하도록 구성.
"""
from __future__ import annotations

import pytest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch


AADS_ROOT = Path("/root/aads")
CKP_PROJECTS_DIR = AADS_ROOT / ".claude" / "projects"


# ─── 시나리오 1: 역량 설명 (Tool Category Guide) ─────────────────────────────

class TestScenario1CapabilityDescription:
    """시나리오 1: '너 뭘 할 수 있어?' → 6개 카테고리 역량 설명."""

    def test_tool_category_guide_exists(self):
        """TOOL_CATEGORY_GUIDE가 tool_registry에 정의되어 있어야 한다."""
        from app.services.tool_registry import TOOL_CATEGORY_GUIDE
        assert TOOL_CATEGORY_GUIDE
        assert "상시 로드 도구" in TOOL_CATEGORY_GUIDE
        assert "온디맨드 도구" in TOOL_CATEGORY_GUIDE

    def test_tool_category_guide_has_all_sections(self):
        """도구 카테고리 안내에 핵심 도구가 포함되어야 한다."""
        from app.services.tool_registry import TOOL_CATEGORY_GUIDE
        for tool_name in ["health_check", "directive_create", "get_all_service_status", "generate_directive"]:
            assert tool_name in TOOL_CATEGORY_GUIDE, f"{tool_name} not in TOOL_CATEGORY_GUIDE"

    def test_eager_tools_include_core_tools(self):
        """상시 로드 도구에 핵심 4개가 포함되어야 한다."""
        from app.services.tool_registry import ToolRegistry
        registry = ToolRegistry()
        eager = registry.get_eager_tools()
        names = {t["name"] for t in eager}
        assert "health_check" in names
        assert "directive_create" in names
        assert "get_all_service_status" in names
        assert "generate_directive" in names

    def test_deferred_tools_do_not_include_eager(self):
        """온디맨드 도구에 상시 로드 도구가 포함되면 안 된다."""
        from app.services.tool_registry import ToolRegistry
        registry = ToolRegistry()
        deferred_names = {t["name"] for t in registry.get_deferred_tools()}
        eager_names = {t["name"] for t in registry.get_eager_tools()}
        overlap = eager_names & deferred_names
        assert not overlap, f"도구 중복: {overlap}"

    def test_tool_guide_injected_in_context_builder(self):
        """context_builder._build_tool_guide_layer()가 TOOL_CATEGORY_GUIDE를 반환해야 한다."""
        from app.services.context_builder import _build_tool_guide_layer
        guide = _build_tool_guide_layer()
        assert "상시 로드 도구" in guide


# ─── 시나리오 2: KIS CKP 참조 ──────────────────────────────────────────────────

class TestScenario2KISCodeAnalysis:
    """시나리오 2: 'KIS 주문 로직 분석해' → CKP 참조 + 코드 흐름 분석."""

    def test_kis_ckp_directory_exists(self):
        """KIS CKP 디렉토리가 존재해야 한다."""
        kis_dir = CKP_PROJECTS_DIR / "KIS"
        assert kis_dir.exists(), f"KIS CKP 디렉토리 없음: {kis_dir}"

    def test_kis_ckp_files_complete(self):
        """KIS CKP 5개 파일이 모두 존재해야 한다."""
        kis_dir = CKP_PROJECTS_DIR / "KIS"
        required = ["CLAUDE.md", "ARCHITECTURE.md", "CODEBASE-MAP.md",
                    "DEPENDENCY-MAP.md", "LESSONS.md"]
        for fname in required:
            fpath = kis_dir / fname
            assert fpath.exists(), f"KIS CKP 파일 없음: {fname}"
            content = fpath.read_text(encoding="utf-8")
            assert len(content) > 100, f"KIS CKP 파일 내용 부족: {fname} ({len(content)}자)"

    def test_kis_claude_md_has_project_info(self):
        """KIS CLAUDE.md에 서버·WORKDIR 정보가 포함되어야 한다."""
        content = (CKP_PROJECTS_DIR / "KIS" / "CLAUDE.md").read_text(encoding="utf-8")
        assert "211" in content  # 서버 211
        assert "kis-autotrade" in content.lower()

    @pytest.mark.asyncio
    async def test_ckp_manager_get_summary_kis(self):
        """CKPManager.get_ckp_summary('KIS')가 비어있지 않은 요약을 반환해야 한다."""
        from app.services.ckp_manager import CKPManager
        mgr = CKPManager(db_conn=None)
        summary = await mgr.get_ckp_summary("KIS", max_tokens=500)
        assert summary, "KIS CKP 요약이 비어있음"
        assert len(summary) > 50

    @pytest.mark.asyncio
    async def test_ckp_builder_injects_for_kis_workspace(self):
        """_build_ckp_layer('KIS')가 <codebase_knowledge> 블록을 반환해야 한다."""
        from app.services.context_builder import _build_ckp_layer
        result = await _build_ckp_layer("KIS")
        assert "<codebase_knowledge>" in result


# ─── 시나리오 3: 웹 검색 도구 정의 확인 ──────────────────────────────────────

class TestScenario3WebSearch:
    """시나리오 3: 'AI 에이전트 트렌드 검색해' → web_search_brave 도구 정의."""

    def test_web_search_brave_tool_defined(self):
        """web_search_brave 도구가 tool_registry에 정의되어야 한다."""
        from app.services.tool_registry import ToolRegistry
        registry = ToolRegistry()
        tool = registry.get_tool("web_search_brave")
        assert tool
        assert tool["name"] == "web_search_brave"
        assert "input_schema" in tool

    def test_web_search_is_deferred(self):
        """web_search_brave는 온디맨드(deferred) 도구여야 한다."""
        from app.services.tool_registry import ToolRegistry
        registry = ToolRegistry()
        assert registry.is_deferred("web_search_brave")

    def test_web_search_in_deferred_list(self):
        """web_search_brave가 get_deferred_tools() 목록에 있어야 한다."""
        from app.services.tool_registry import ToolRegistry
        registry = ToolRegistry()
        names = {t["name"] for t in registry.get_deferred_tools()}
        assert "web_search_brave" in names


# ─── 시나리오 4: 전체 서비스 상태 ─────────────────────────────────────────────

class TestScenario4AllServiceStatus:
    """시나리오 4: '전체 서비스 상태' → get_all_service_status 도구."""

    def test_get_all_service_status_tool_defined(self):
        """get_all_service_status 도구가 정의되어야 한다."""
        from app.services.tool_registry import ToolRegistry
        registry = ToolRegistry()
        tool = registry.get_tool("get_all_service_status")
        assert tool
        assert "6개 서비스" in tool["description"] or "서비스" in tool["description"]

    def test_get_all_service_status_is_eager(self):
        """get_all_service_status는 상시 로드(eager) 도구여야 한다."""
        from app.services.tool_registry import ToolRegistry
        registry = ToolRegistry()
        assert not registry.is_deferred("get_all_service_status")

    def test_all_6_services_in_tool_category_guide(self):
        """도구 카테고리 안내에 get_all_service_status가 언급되어야 한다."""
        from app.services.tool_registry import TOOL_CATEGORY_GUIDE
        assert "get_all_service_status" in TOOL_CATEGORY_GUIDE


# ─── 시나리오 5: 지시서 자동 생성 ────────────────────────────────────────────

class TestScenario5DirectiveGeneration:
    """시나리오 5: 'NTV2 에러 수정 지시서 만들어' → generate_directive 도구."""

    def test_generate_directive_tool_defined(self):
        """generate_directive 도구가 정의되어야 한다."""
        from app.services.tool_registry import ToolRegistry
        registry = ToolRegistry()
        tool = registry.get_tool("generate_directive")
        assert tool
        assert "description" in tool["input_schema"]["properties"]

    def test_generate_directive_is_eager(self):
        """generate_directive는 상시 로드 도구여야 한다."""
        from app.services.tool_registry import ToolRegistry
        registry = ToolRegistry()
        assert not registry.is_deferred("generate_directive")

    def test_ntv2_ckp_exists(self):
        """NTV2 CKP 디렉토리가 존재해야 한다."""
        ntv2_dir = CKP_PROJECTS_DIR / "NTV2"
        assert ntv2_dir.exists()
        assert (ntv2_dir / "CLAUDE.md").exists()


# ─── 시나리오 6: Prompt Caching 설정 확인 ───────────────────────────────────

class TestScenario6PromptCaching:
    """시나리오 6: Prompt Caching 모듈 동작 확인."""

    def test_cache_config_module_exists(self):
        """cache_config.py 모듈이 임포트되어야 한다."""
        from app.core.cache_config import (
            make_cacheable_block,
            build_cached_system_blocks,
            build_cached_tools,
            estimate_cache_savings,
        )
        assert callable(make_cacheable_block)
        assert callable(build_cached_system_blocks)
        assert callable(build_cached_tools)
        assert callable(estimate_cache_savings)

    def test_make_cacheable_block_with_long_text(self):
        """1024토큰 이상 텍스트는 cache_control이 추가되어야 한다."""
        from app.core.cache_config import make_cacheable_block
        long_text = "a" * 4200  # ~1050 토큰
        block = make_cacheable_block(long_text)
        assert "cache_control" in block
        assert block["cache_control"] == {"type": "ephemeral"}

    def test_make_cacheable_block_short_text_no_cache(self):
        """짧은 텍스트는 cache_control이 없어야 한다 (force=False)."""
        from app.core.cache_config import make_cacheable_block
        short_text = "짧은 텍스트"
        block = make_cacheable_block(short_text, force=False)
        assert "cache_control" not in block

    def test_make_cacheable_block_force_always_cached(self):
        """force=True면 짧은 텍스트도 cache_control이 추가되어야 한다."""
        from app.core.cache_config import make_cacheable_block
        block = make_cacheable_block("짧음", force=True)
        assert "cache_control" in block

    def test_build_cached_system_blocks_layer1_always_cached(self):
        """build_cached_system_blocks: Layer1은 항상 캐시되어야 한다."""
        from app.core.cache_config import build_cached_system_blocks
        blocks = build_cached_system_blocks(
            layer1_text="정적 시스템 프롬프트 " * 200,
            layer2_text="동적 정보",
        )
        assert len(blocks) >= 1
        assert "cache_control" in blocks[0]

    def test_build_cached_tools_last_tool_has_cache(self):
        """build_cached_tools: 전체 토큰 합계 >=1024 시 마지막 도구에 cache_control.
        각 도구 description 500자 × 10개 = 5000자 ÷ 4 ≈ 1250토큰 → MIN_CACHE_TOKENS(1024) 초과.
        """
        from app.core.cache_config import build_cached_tools
        tools = [
            {"name": f"tool_{i}", "description": "x" * 500, "input_schema": {"type": "object"}}
            for i in range(10)
        ]
        cached = build_cached_tools(tools)
        assert "cache_control" in cached[-1]

    def test_estimate_cache_savings_positive(self):
        """캐시 절감 추정 결과가 양수여야 한다."""
        from app.core.cache_config import estimate_cache_savings
        result = estimate_cache_savings(5000, 1000, cache_hit_rate=0.8)
        assert result["savings_pct"] > 0
        assert result["cached_cost_ratio"] < 1.0

    def test_context_builder_uses_cache_config(self):
        """context_builder.build()가 cache_config를 사용한 system_blocks를 반환해야 한다."""
        import asyncio
        from app.services.context_builder import build

        async def _run():
            result = await build(workspace_name="CEO", session_id="test-session")
            return result

        result = asyncio.get_event_loop().run_until_complete(_run())
        assert result.system_blocks
        # Layer 1 블록에 cache_control이 있어야 함
        assert any("cache_control" in b for b in result.system_blocks)


# ─── 시나리오 7: 주간 브리핑 스케줄 등록 확인 ──────────────────────────────

class TestScenario7WeeklyBriefingSchedule:
    """시나리오 7: 주간 브리핑 APScheduler 등록 확인."""

    def test_main_py_has_weekly_briefing_job(self):
        """main.py에 weekly_briefing 스케줄러 코드가 있어야 한다."""
        main_path = Path("/root/aads/aads-server/app/main.py")
        assert main_path.exists()
        content = main_path.read_text(encoding="utf-8")
        assert "weekly_briefing" in content
        assert "_run_weekly_briefing" in content
        assert "day_of_week" in content

    def test_weekly_briefing_is_monday_kst(self):
        """주간 브리핑이 월요일 09:00 KST (UTC 00:00)에 설정되어야 한다."""
        main_path = Path("/root/aads/aads-server/app/main.py")
        content = main_path.read_text(encoding="utf-8")
        # CronTrigger(day_of_week="mon", hour=0, minute=0, timezone="UTC")
        assert '"mon"' in content or "'mon'" in content
        assert "hour=0" in content

    def test_all_remote_ckp_directories_exist(self):
        """5개 원격 프로젝트 CKP 디렉토리 모두 존재해야 한다."""
        for project in ["KIS", "GO100", "SF", "NTV2", "NAS"]:
            proj_dir = CKP_PROJECTS_DIR / project
            assert proj_dir.exists(), f"{project} CKP 디렉토리 없음"
            # 5개 파일 모두 존재 확인
            for fname in ["CLAUDE.md", "ARCHITECTURE.md", "CODEBASE-MAP.md",
                          "DEPENDENCY-MAP.md", "LESSONS.md"]:
                assert (proj_dir / fname).exists(), f"{project}/{fname} 없음"

    @pytest.mark.asyncio
    async def test_ckp_manager_scan_remote_project(self):
        """CKPManager.scan_remote_project()가 CKP 파일 경로를 반환해야 한다."""
        from app.services.ckp_manager import CKPManager
        mgr = CKPManager(db_conn=None)
        for project in ["KIS", "GO100", "SF", "NTV2", "NAS"]:
            result = await mgr.scan_remote_project(project)
            assert result.project == project
            assert result.scanned_files > 0, f"{project} scanned_files=0"
            assert result.generated_files, f"{project} generated_files 비어있음"
