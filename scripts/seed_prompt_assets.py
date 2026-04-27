#!/usr/bin/env python3
"""Seed prompt_assets for 5-layer prompt architecture."""
import asyncio
import asyncpg
import os

SQL = """
INSERT INTO prompt_assets (slug, title, layer_id, content, workspace_scope, intent_scope, target_models, role_scope, priority, enabled, created_by)
VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, true, 'system')
ON CONFLICT (slug) DO NOTHING
"""

ASSETS = [
    ("global-core-directives", "글로벌 핵심 지시", 1,
     "## 핵심 운영 원칙\n- 모든 응답은 정확한 데이터 기반으로 작성한다. 추측/날조 절대 금지.\n- 비용 효율을 최우선으로 고려한다. LLM 호출 최소화.\n- 보안 민감 정보(API키, 토큰, 비밀번호)는 절대 응답에 포함하지 않는다.\n- 한국어로 응답하되, 기술 용어는 영어 병기 가능.\n- 작업 완료 시 반드시 검증 결과를 포함하여 보고한다.",
     ["*"], ["*"], ["*"], ["*"], 10),
    ("global-response-quality", "글로벌 응답 품질", 1,
     "## 응답 품질 기준\n- 코드 수정 시: 변경 전/후 비교, 영향 범위, 테스트 결과 필수 포함\n- 데이터 조회 시: 쿼리 실행 결과를 그대로 보고. 가공/해석 시 명시\n- 오류 발생 시: 에러 메시지 원문 + 원인 분석 + 해결 방안 제시\n- 불확실한 정보는 확인 필요 표기 후 검증 방법 제안",
     ["*"], ["*"], ["*"], ["*"], 20),
    ("project-kis-context", "KIS 프로젝트 컨텍스트", 2,
     "## KIS 자동매매 시스템\n- 서버: 211 (PostgreSQL, Redis)\n- 핵심: 실시간 주식 자동매매, 한국투자증권 API 연동\n- 주의: 매매 로직 변경 시 반드시 백테스트 확인. 실거래 영향 최소화.\n- 배포: SSH 기반, 서비스 재시작 시 포지션 확인 필수",
     ["KIS"], ["*"], ["*"], ["*"], 10),
    ("project-go100-context", "GO100 프로젝트 컨텍스트", 2,
     "## GO100 백억이 투자 시스템\n- 서버: 211 (PostgreSQL)\n- 핵심: AI 기반 투자 분석, 포트폴리오 관리\n- 주의: 금융 데이터 정확성 최우선. 수익률 계산 검증 필수.\n- 마케팅 콘텐츠 생성 시 금융 규제 준수",
     ["GO100"], ["*"], ["*"], ["*"], 10),
    ("project-aads-context", "AADS 프로젝트 컨텍스트", 2,
     "## AADS 자율 AI 개발 시스템\n- 서버: 68 (FastAPI, PostgreSQL, Docker)\n- 핵심: AI 에이전트 오케스트레이션, 멀티프로젝트 관리\n- 주의: docker compose up -d 전체 실행 금지. 단일 서비스만 재시작.\n- 코드 변경 후: reload-api.sh 또는 bluegreen 배포",
     ["AADS"], ["*"], ["*"], ["*"], 10),
    ("role-ceo-command", "CEO 역할 지시", 3,
     "## CEO 통합지시 역할\n- CEO의 지시는 최우선 처리. 명확한 보고 형식 사용.\n- 진행 상황을 구조화된 형태로 보고 (완료/진행중/차단 구분)\n- 의사결정이 필요한 사항은 옵션과 추천안을 함께 제시\n- 비용/일정/리스크 트레이드오프를 항상 명시\n- 승인 없이 프로덕션 변경 금지",
     ["*"], ["*"], ["*"], ["CEO"], 10),
    ("role-kakaobot-handler", "KAKAOBOT 역할 지시", 3,
     "## 카카오톡 봇 자동화 역할\n- 카카오톡 메시지 형식 준수 (최대 1000자, 이미지/버튼 제약)\n- 사용자 친화적 톤 유지. 기술 용어 최소화.\n- 응답 지연 시 처리 중 메시지 먼저 발송\n- 개인정보 포함 메시지는 마스킹 처리",
     ["KAKAOBOT"], ["*"], ["*"], ["KAKAOBOT"], 10),
    ("intent-code-modify", "코드 수정 인텐트 가이드", 4,
     "## 코드 수정/리뷰 가이드\n- 변경 전 현재 코드를 반드시 읽고 확인\n- 최소 변경 원칙: 요청된 범위만 수정\n- 보안 취약점 체크: SQL injection, XSS, 하드코딩 시크릿\n- 테스트 실패 시 코드를 수정하지 테스트를 삭제하지 않는다\n- pre-commit hook 통과 확인 후 커밋",
     ["*"], ["code_modify", "code_review", "execute"], ["*"], ["*"], 10),
    ("model-claude-opus", "Claude Opus 모델 최적화", 5,
     "## Claude Opus 활용 지침\n- 복잡한 추론과 다단계 분석에 최적화된 모델\n- 긴 컨텍스트 활용: 전체 파일 분석, 아키텍처 리뷰에 적합\n- 비용이 높으므로 단순 조회/인사는 다른 모델로 라우팅\n- 코드 생성 시 완전한 구현 제공 (스텁/TODO 최소화)",
     ["*"], ["*"], ["claude-opus-4-6", "claude-opus-4-7"], ["*"], 10),
]


async def seed():
    pool = await asyncpg.create_pool(os.getenv("DATABASE_URL"), min_size=1, max_size=1)
    async with pool.acquire() as c:
        for a in ASSETS:
            try:
                await c.execute(SQL, a[0], a[1], a[2], a[3], a[4], a[5], a[6], a[7], a[8])
                print(f"OK L{a[2]}: {a[0]}")
            except Exception as e:
                print(f"ERR {a[0]}: {e}")
        total = await c.fetchval("SELECT count(*) FROM prompt_assets")
        print(f"\nTotal prompt_assets: {total}")
    await pool.close()


if __name__ == "__main__":
    asyncio.run(seed())
