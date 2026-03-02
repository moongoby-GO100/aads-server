# AADS Server

Autonomous AI Development System — FastAPI 기반 멀티에이전트 서버.

## 에이전트 체인 (Phase 1 Week 2)

```
PM → Supervisor → Developer → QA → Judge
                     ↑________________|
                    (Judge fail, 최대 3회 재작업)
```

| 에이전트 | 역할 | 모델 |
|---------|------|------|
| PM | 요구사항 구조화 (TaskSpec 생성) | claude-sonnet-4-6 |
| Supervisor | 오케스트레이션, 태스크 분배 | claude-opus-4-6 |
| Developer | 코드 생성 + E2B 실행 | claude-sonnet-4-6 |
| QA | 테스트 코드 생성 + E2B 실행 | claude-sonnet-4-6 |
| Judge | 독립 출력 검증, 합격 판정 | claude-sonnet-4-6 |

## MCP 서버 (Phase 1 Week 2)

| 타입 | 서버 | 포트 | 에이전트 |
|-----|------|------|---------|
| 상시 가동 | Filesystem | 8765 | Developer, QA |
| 상시 가동 | Git | 8766 | Developer |
| 상시 가동 | Memory | 8767 | Supervisor |
| 상시 가동 | PostgreSQL | 8768 | 전체 |
| 온디맨드 | GitHub | 8769 | Developer |
| 온디맨드 | Brave Search | 8770 | Researcher |
| 온디맨드 | Fetch | 8771 | Researcher |

## 설치 및 실행

```bash
# 의존성 설치
pip install -e ".[dev]"

# .env 파일 설정
cp .env.example .env
# .env 편집 (API 키 입력)

# 서버 실행
uvicorn app.main:app --reload

# 테스트 실행
pytest tests/unit/ -v
```

## 환경변수 (.env)

```
ANTHROPIC_API_KEY=...
OPENAI_API_KEY=...
GOOGLE_API_KEY=...
E2B_API_KEY=...
SUPABASE_DIRECT_URL=postgresql://postgres:PW@db.REF.supabase.co:5432/postgres
GITHUB_TOKEN=...
BRAVE_API_KEY=...
UPSTASH_REDIS_URL=...
```

## 절대 규칙 (CEO-DIRECTIVES)

- R-010: `langgraph-supervisor` 라이브러리 사용 금지
- R-011: Supabase port 5432 직접 연결 (Supavisor 금지)
- R-012: LLM 호출 최대 15회/태스크

## 현재 상태

- Phase 1 Week 1: ✅ 완료 (3-agent chain, 6/6 테스트)
- Phase 1 Week 2: 🟡 진행중
  - ✅ W2-001: QA/Judge 에이전트 추가 (23/23 테스트)
  - ✅ W2-002: MCP 서버 연결 설정
  - ⏳ W2-003: Fly.io 배포
  - ⏳ W2-004: E2B 실제 API 키 연동
