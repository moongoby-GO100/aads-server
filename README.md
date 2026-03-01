# AADS Server

**Autonomous AI Development System** — Phase 1 Week 1

## 빠른 시작

```bash
# 1. 의존성 설치
pip install -e ".[dev]"

# 2. 환경변수 설정
cp .env.example .env
# .env 편집: ANTHROPIC_API_KEY, E2B_API_KEY, SUPABASE_DIRECT_URL 필수

# 3. 서버 기동
uvicorn app.main:app --reload

# 4. 헬스 체크
curl http://localhost:8000/api/v1/health
```

## API

| Method | Path | 설명 |
|--------|------|------|
| GET | `/api/v1/health` | 서버 상태 |
| POST | `/api/v1/projects` | 프로젝트 생성 |
| GET | `/api/v1/projects/{id}` | 프로젝트 상태 조회 |
| POST | `/api/v1/projects/{id}/checkpoint` | 체크포인트 승인/수정 |

## 아키텍처

```
START → PM Agent → [interrupt: 요구사항 승인] → Supervisor → Developer → END
```

- **PM Agent**: 사용자 요청 → TaskSpec JSON 생성 + 사용자 승인 요청
- **Supervisor**: TaskSpec 검증 + 에이전트 라우팅
- **Developer**: 코드 생성 (Claude Sonnet 4.6) + E2B 샌드박스 실행

## 환경변수

```
ANTHROPIC_API_KEY=         # Claude API (필수)
E2B_API_KEY=               # E2B 샌드박스 (필수)
SUPABASE_DIRECT_URL=       # postgresql://postgres:PW@db.REF.supabase.co:5432/postgres (권장)
OPENAI_API_KEY=            # 폴백 모델 (선택)
GOOGLE_API_KEY=            # 폴백 모델 (선택)
```

## 테스트

```bash
# 단위 테스트
pytest tests/unit/ -v

# E2E 테스트 (서버 기동 후)
AADS_TEST_URL=http://localhost:8000/api/v1 pytest tests/e2e/ -v -s
```
