# AADS — 자율 AI 개발 시스템 (서버 68)

## 기술 스택
FastAPI 0.115, PostgreSQL 15+pgvector, LangGraph 1.0.10, Docker, Python 3.11, Next.js 16

## CEO 절대 규칙
핵심: HANDOVER 필수 업데이트, push+HTTP200, 시크릿 커밋 금지, 직접DB편집 금지
비용 효율 최우선, max 15 LLM/task, no Supavisor, no langgraph-supervisor
상세: https://raw.githubusercontent.com/moongoby-GO100/aads-docs/main/CEO-DIRECTIVES.md

## 공유 교훈
docs/shared-lessons/INDEX.md 또는 https://raw.githubusercontent.com/moongoby-GO100/aads-docs/main/shared/lessons/INDEX.md

## AADS 전용 지식
docs/knowledge/AADS-KNOWLEDGE.md

## FLOW 규칙
.claude/rules/flow-rules.md
모든 작업: Find→Layout→Operate→Wrap up. Wrap up 미완료 시 다음 작업 차단(P0/P1).

## 현재 상태
- Phase: Phase 2 운영, FLOW 문서화 체계 도입중
- 최근: AADS-120(FLOW Phase 1-B), AADS-119(HANDOVER v6.0), AADS-118(교차검증 9종)
- 긴급: 없음

## 빌드/배포
docker compose -f docker-compose.prod.yml up -d --build aads-server
curl -s https://aads.newtalk.kr/api/v1/ops/health-check | python3 -m json.tool
