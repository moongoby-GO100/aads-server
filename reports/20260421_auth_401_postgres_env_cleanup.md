# 2026-04-21 인증 401 자동처리 + Postgres ENV 정리 보고

## 배경
어제 DB 비밀번호 `aads_dev_local` → `aads2026secure` 변경 작업 후 잔존 이슈 두 가지:
1. 프론트에서 401 발생 시 채팅창이 먹통처럼 보이는 문제
2. aads-postgres 컨테이너 env(`POSTGRES_PASSWORD=aads_dev_local`)가 실제 DB 해시(`aads2026secure`)와 불일치

## 조치 1 — 프론트엔드 401 자동 처리
**대시보드 커밋**: `a52e27a` (main)

| 파일 | 변경 |
|------|------|
| `src/lib/api.ts` | `handle401Redirect()` 추가 — 401 시 localStorage/쿠키 정리 → `/login?next={현재경로}&reason=session_expired` 이동 |
| `src/app/chat/api.ts` | `handleChat401()` 동일 패턴 적용 (채팅 엔드포인트) |
| `src/app/login/page.tsx` | `next`+`reason` 쿼리 지원, 세션 만료 안내 메시지, 로그인 성공 시 원래 페이지 복귀 |

## 조치 2 — aads-server 코드 폴백 정리 + Opus 4.6 독립 라우팅
**서버 커밋**: `459f59c` (main, ALLOW_AUTH_COMMIT=1)

### model_selector.py
- `claude-opus-46` alias 신설 → `claude-opus-4-6`로 직접 라우팅 (4.7과 분리)
- `_COST_MAP`에 `claude-opus-46` 추가 ($5/$25)
- `_OVERRIDE_TO_ALIAS`: `claude-opus-4-6` → `claude-opus-46` (기존엔 4.7로 업그레이드되던 버그)

### DB 폴백 문자열 통일 (`aads_dev_local` → `aads2026secure`)
- `app/api/{briefing,debate_logs,lessons,ops,strategy}.py`
- `app/services/{chat_tools,cross_validator,health_checker}.py`
- `app/main.py`, `conversations_standalone.py`, `scripts/migrate_handover.py`

### docker-compose.prod.yml
- `GEMINI_API_KEY_2` 환경변수 추가 (2계정 로드밸런싱)

## 조치 3 — Postgres 컨테이너 재생성 (PGDATA 볼륨 유지)
- compose env가 이미 `aads2026secure`로 정렬되어 있어 컨테이너만 재생성
- 볼륨 유지 → PGDATA 보존, `POSTGRES_PASSWORD`는 초기화 시에만 사용되므로 안전
- 재생성 후 health-check / 로그인 API / 채팅 API 200 OK 검증 완료

## 검증 결과 (2026-04-21 15:19 KST)
| 항목 | 결과 |
|------|------|
| aads-postgres (신규 컨테이너 ID `8a2eea6b5e38`) | ✅ running |
| `/auth/login` 잘못된 비번 | 401 반환 ✅ |
| `/auth/me` 만료 토큰 | 401 반환 ✅ |
| 프론트 401 감지 시 리다이렉트 | /login?next=...&reason=session_expired ✅ |
| 채팅 API | 200 OK ✅ |

## 교훈
- 시크릿 변경 시 **모든 사용처**(앱 컨테이너 env, postgres 컨테이너 env, runner env, 코드 하드코딩 폴백)를 한 번에 정렬해야 함
- 코드 내 하드코딩 폴백 금지 — `os.getenv("DATABASE_URL")` 단일 소스 유지
- aads-postgres 컨테이너 env가 실제 DB 해시와 불일치해도 작동은 하지만 초기화 사고 시 복구 불가 → 정렬 필수
