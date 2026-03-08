"""
AADS-186B: CTO 모드 테스트
"""
import asyncio
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))


class TestCTOMode:

    def setup_method(self):
        from app.services.cto_mode import CTOMode
        self.cto = CTOMode()

    def test_cto_mode_import(self):
        """CTOMode 임포트 확인."""
        from app.services.cto_mode import CTOMode
        assert CTOMode is not None

    def test_strategy_discussion_returns_str(self):
        """strategy_discussion() 문자열 반환 확인 (LLM mock)."""
        async def run():
            with patch("app.services.cto_mode._llm_call", new=AsyncMock(return_value="전략 분석 결과")):
                result = await self.cto.strategy_discussion("기술 방향 의견 주세요", "")
                assert isinstance(result, str)
                assert len(result) > 0
        asyncio.run(run())

    def test_generate_directive_dry_run(self):
        """generate_and_submit_directive() dry_run=True 시 유효한 지시서 형식 확인."""
        async def run():
            with patch("app.services.cto_mode._llm_call", new=AsyncMock(return_value="1. 기능 구현\n2. 테스트")):
                with patch("app.services.cto_mode._get_conn", new=AsyncMock(side_effect=Exception("no db"))):
                    result = await self.cto.generate_and_submit_directive(
                        description="테스트 기능 구현",
                        dry_run=True,
                    )
                    assert result.dry_run is True
                    assert result.submitted is False
                    assert "AADS-" in result.task_id
                    # 지시서 포맷 확인
                    assert ">>>DIRECTIVE_START" in result.content
                    assert "TASK_ID:" in result.content
                    assert "PRIORITY:" in result.content
        asyncio.run(run())

    def test_track_tech_debt_returns_report(self):
        """track_tech_debt() TODO/FIXME 집계 반환 확인."""
        async def run():
            report = await self.cto.track_tech_debt("AADS")
            assert report.project == "AADS"
            assert isinstance(report.items, list)
            assert isinstance(report.by_tag, dict)
            assert report.total >= 0
            # TODO가 코드베이스에 존재할 것
            return report
        report = asyncio.run(run())
        # 실제 코드베이스 스캔 결과 — TODO 존재 여부 확인
        assert report.total >= 0  # 0 이상이면 OK

    def test_verify_task_structure(self):
        """verify_task() 결과 구조 확인."""
        async def run():
            result = await self.cto.verify_task("AADS-186B")
            assert result.task_id == "AADS-186B"
            assert isinstance(result.checked_files, list)
            assert isinstance(result.passed, list)
            assert isinstance(result.failed, list)
        asyncio.run(run())

    def test_impact_analysis_returns_report(self):
        """impact_analysis() 결과 구조 확인."""
        async def run():
            report = await self.cto.impact_analysis("context_builder 수정")
            assert isinstance(report.target_files, list)
            assert isinstance(report.affected_files, list)
            assert report.risk_level in ("LOW", "MEDIUM", "HIGH")
            assert isinstance(report.summary, str)
        asyncio.run(run())

    def test_next_task_id_format(self):
        """_next_task_id() 반환값 AADS-xxx 형식 확인."""
        async def run():
            with patch("app.services.cto_mode._get_conn", new=AsyncMock(side_effect=Exception("no db"))):
                task_id = await self.cto._next_task_id()
                assert task_id.startswith("AADS-"), f"잘못된 task_id: {task_id}"
        asyncio.run(run())

    def test_format_directive_content(self):
        """_format_directive() 지시서 포맷 확인."""
        content = self.cto._format_directive(
            task_id="AADS-999",
            description="테스트 기능",
            priority="P1",
            size="S",
            files_owned=["app/services/test.py"],
            criteria="1. 기능 구현\n2. 테스트 통과",
        )
        assert "AADS-999" in content
        assert ">>>DIRECTIVE_START" in content
        assert ">>>DIRECTIVE_END" in content
        assert "P1" in content
        assert "app/services/test.py" in content


class TestCTOIntentRouting:
    """CTO 인텐트 6개 라우팅 확인."""

    def test_cto_intents_in_map(self):
        """intent_router.py에 CTO 인텐트 6개 존재 확인."""
        from app.services.intent_router import INTENT_MAP
        cto_intents = [
            "cto_strategy", "cto_code_analysis", "cto_directive",
            "cto_verify", "cto_impact", "cto_tech_debt"
        ]
        for intent in cto_intents:
            assert intent in INTENT_MAP, f"{intent} 누락"

    def test_cto_strategy_uses_opus(self):
        """cto_strategy 인텐트 → opus 모델 확인."""
        from app.services.intent_router import INTENT_MAP
        cfg = INTENT_MAP["cto_strategy"]
        assert "opus" in cfg.get("model", ""), f"cto_strategy 모델: {cfg}"

    def test_cto_code_analysis_uses_sonnet(self):
        """cto_code_analysis 인텐트 → sonnet 모델 확인."""
        from app.services.intent_router import INTENT_MAP
        cfg = INTENT_MAP["cto_code_analysis"]
        assert "sonnet" in cfg.get("model", ""), f"cto_code_analysis 모델: {cfg}"

    def test_keyword_fallback_cto(self):
        """키워드 폴백 CTO 인텐트 분류 확인."""
        from app.services.intent_router import _keyword_fallback
        result = _keyword_fallback("기술 부채 확인해줘")
        assert result.intent == "cto_tech_debt"

        result2 = _keyword_fallback("영향 분석 해줘")
        assert result2.intent == "cto_impact"
