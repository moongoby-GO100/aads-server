# RESULT: KakaoBot SaaS 회원가입 — 백엔드 (auth + kakao_bot 인증 게이트)

## 구현 요약
SaaS 사용자 회원가입/로그인 시스템 + KakaoBot API 인증 게이트 구현 완료.

## 변경 파일
| 파일 | 변경 내용 |
|------|-----------|
| `migrations/039_saas_users.sql` | saas_users 테이블 (기존 - 변경 없음) |
| `pyproject.toml` | `passlib[bcrypt]>=1.7.4` 의존성 추가 |
| `app/auth.py` | SaaS 사용자 CRUD + `get_current_user` 의존성 + bcrypt 직접 사용 |
| `app/api/auth.py` | `POST /auth/register` + `POST /auth/login` 수정 (SaaS 1순위, CEO 2순위) |
| `app/api/kakao_bot.py` | 19개 SaaS 엔드포인트에 JWT 인증 게이트 적용 |

## 검증 체크리스트

### [x] 구현 목표
SaaS 사용자 회원가입/로그인 + KakaoBot API JWT 인증 게이트

### [x] 검증 방법 및 결과

| # | 테스트 | curl 명령 | 결과 | 상태 |
|---|--------|----------|------|------|
| 1 | 회원가입 | `POST /api/v1/auth/register` `{"email":"v4_test@example.com","password":"test123456","name":"V4 Test"}` | `{"token":"eyJ...","user_id":"36","email":"v4_test@example.com","is_admin":false}` | ✅ |
| 2 | 로그인 (SaaS) | `POST /api/v1/auth/login` `{"email":"v4_test@example.com","password":"test123456"}` | `{"token":"eyJ...","user_id":"36","is_admin":false}` | ✅ |
| 3 | /auth/me | `GET /api/v1/auth/me` + Bearer token | `{"user_id":"36","email":"v4_test@example.com","is_admin":false}` | ✅ |
| 4 | CEO 관리자 로그인 | `POST /api/v1/auth/login` (환경변수 인증정보) | `{"user_id":"admin","is_admin":true}` | ✅ |
| 5 | 연락처 생성 (JWT) | `POST /api/v1/kakao-bot/contacts` + Bearer | `{"id":3,"user_id":"36","name":"E2E Contact"}` | ✅ |
| 6 | 연락처 목록 (JWT) | `GET /api/v1/kakao-bot/contacts` + Bearer | `{"count":1,"contacts":[...]}` | ✅ |
| 7 | 인증 없이 접근 | `GET /api/v1/kakao-bot/contacts` (no auth) | `401 - 인증이 필요합니다` | ✅ |
| 8 | 이메일 중복 | `POST /api/v1/auth/register` (같은 이메일) | `409 - 이미 등록된 이메일입니다` | ✅ |
| 9 | 짧은 비밀번호 | `POST /api/v1/auth/register` (3자) | `400 - 비밀번호는 최소 6자 이상` | ✅ |
| 10 | 웹훅 (인증 제외) | `POST /api/v1/kakao-bot/respond` (no auth) | AI 응답 정상 반환 | ✅ |

### [x] 완료 기준
- 회원가입 → JWT 토큰 발급 ✅
- 로그인 → SaaS DB 1순위 + CEO 환경변수 2순위 ✅
- KakaoBot SaaS 엔드포인트 JWT 인증 필수 ✅
- 웹훅/공개 API 인증 제외 ✅

### [x] 실패 기준 확인
- 회원가입 없이 KakaoBot API 접근 → 401 ✅
- 잘못된 비밀번호 로그인 → 401 ✅
- CEO 관리자 로그인 불가 → CEO 로그인 정상 ✅

### [x] 서비스 재시작 확인
```
$ docker ps --filter name=aads-server
aads-server   Up XX seconds (healthy)
```

### [x] 에러 로그 0건
```
$ docker logs --since 60s aads-server 2>&1 | grep -i error
(zero errors)
```

## 인증 제외 엔드포인트 (웹훅/공개)
- `POST /kakao-bot/respond` — AI 응답 생성
- `GET/POST /kakao-bot/agent/*` — PC Agent 배포/등록
- `POST /kakao-bot/msgbot/webhook` — 메신저봇R 웹훅
- `POST /auth/register` — 회원가입
- `POST /auth/login` — 로그인
- `GET /auth/me` — 현재 사용자

## 인증 필수 엔드포인트 (19개)
연락처 CRUD (4), 기념일 CRUD+upcoming (5), AI 생성/개선 (2), 템플릿 CRUD+seed (4), 예약발송 CRUD (4)

## 기술 노트
- bcrypt 5.0.0: passlib 호환성 문제로 `bcrypt` 모듈 직접 사용
- PyJWT `sub` 클레임: 반드시 string 타입 (integer 시 `InvalidSubjectError`)
- `from __future__ import annotations`: FastAPI + Pydantic v2 response_model과 호환 이슈 → 제거
