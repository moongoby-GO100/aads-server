# AADS Server

Autonomous AI Development System — FastAPI 기반 멀티에이전트 서버.

[![AADS CI](https://github.com/moongoby-GO100/aads-server/actions/workflows/ci.yml/badge.svg)](https://github.com/moongoby-GO100/aads-server/actions/workflows/ci.yml)

## 에이전트 체인 (Phase 1.5 — 8-agent)

```
PM → Supervisor → Researcher (온디맨드)
              ↓
           Architect → Developer → QA → Judge → DevOps → END
                          ↑________________|
                         (Judge fail, 최대 3회 재작업)
```

| 에이전트 | 역할 | 모델 | 비용 |
|---------|------|------|------|
| PM | 요구사항 구조화 (TaskSpec 12필드) | claude-sonnet-4-6 | $3/$15 |
| Supervisor | 오케스트레이션, 태스크 분배 | claude-opus-4-6 | $5/$25 |
| Architect | 시스템 설계, DB스키마, API설계 | claude-sonnet-4-6 | $3/$15 |
| Developer | 코드 생성 + E2B 실행 | claude-sonnet-4-6 | $3/$15 |
| QA | 테스트 코드 생성 + E2B 실행 | claude-sonnet-4-6 | $3/$15 |
| Judge | 독립 출력 검증, 합격 판정 | claude-sonnet-4-6 | $3/$15 |
| DevOps | 배포 스크립트, health check | gpt-4o-mini | $0.15/$0.6 |
| Researcher | 기술 조사, Brave Search | gemini-2.5-flash | $0.3/$2.5 |

## MCP 서버

| 타입 | 서버 | 포트 | 에이전트 |
|-----|------|------|---------|
| 상시 가동 | Filesystem | 8765 | Developer, QA |
| 상시 가동 | Git | 8766 | Developer, DevOps |
| 상시 가동 | Memory | 8767 | Supervisor, Architect |
| 상시 가동 | PostgreSQL | 8768 | 전체 (체크포인트) |
| 온디맨드 | GitHub | 8769 | Developer |
| 온디맨드 | Brave Search | 8770 | Researcher |
| 온디맨드 | Fetch | 8771 | Researcher |

## API 엔드포인트

| 메서드 | 경로 | 설명 |
|-------|------|------|
| GET | /api/v1/health | 서버 상태 확인 |
| POST | /api/v1/projects | 프로젝트 생성 (8-agent 체인 실행) |
| GET | /api/v1/projects/{id} | 프로젝트 상태 조회 |
| GET | /api/v1/projects/{id}/costs | 에이전트별 비용 조회 |
| POST | /api/v1/projects/{id}/checkpoint | HITL 체크포인트 승인/수정 |

## 설치 및 실행

```bash
# 의존성 설치
pip install -e ".[dev]"

# .env 파일 설정
cp .env.example .env
# .env 파일에 API 키 입력

# 서버 실행
uvicorn app.main:app --reload

# 테스트 실행
pytest tests/unit/ -v
pytest tests/e2e/test_real_pipeline.py -v
```

## Docker 배포

```bash
docker compose up -d --build
curl https://aads.newtalk.kr/api/v1/health
```

## 비용 한도 (R-012)

- 작업당 LLM 호출: 최대 15회
- 작업당 비용: 최대 $10
- 월 비용: 최대 $500

## 제약사항

- R-003: .env 키 커밋 금지
- R-010: langgraph-supervisor 사용 금지 (Native StateGraph 사용)
- R-012: 작업당 LLM 호출 15회 이내
- T-008: TaskSpec 12필드 필수
