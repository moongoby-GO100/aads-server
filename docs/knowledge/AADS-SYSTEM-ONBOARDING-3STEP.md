# AADS 3-Step System Onboarding

작성 시각: 2026-06-18 10:01 KST

## 목적

신규 러너, 서브에이전트, 운영 AI가 AADS를 수정하기 전에 반드시 같은 순서로 시스템을 파악하게 한다.
이 문서는 코드 수정 전 읽을 최소 지도이며, 실제 운영 판단은 현재 코드, DB, 헬스체크 결과를 우선한다.

## 1단계: 운영 경계 확인

먼저 대상 프로젝트가 AADS인지 확인한다. AADS 기준 주요 경로는 다음과 같다.

| 영역 | 경로/대상 | 확인 기준 |
|---|---|---|
| Backend | `/root/aads/aads-server` | FastAPI, API 라우터, DB 접근, Runner, prompt compiler |
| Dashboard | `/root/aads/aads-dashboard` | Next.js 화면, middleware, admin/chat UX |
| DB | PostgreSQL `aads-postgres` | tenant, chat, prompt, provenance, usage, runner 상태 |
| 배포 | `deploy.sh`, dashboard `deploy.sh` | blue/green, active slot, health |
| 문서 | `HANDOVER.md`, `docs/` | 변경 기록과 운영 정책 |

금지 사항:

- 기존 사용자 변경을 되돌리지 않는다.
- `.env`, API key, secret을 출력하거나 커밋하지 않는다.
- DROP/TRUNCATE, hard reset, force push, 무단 재시작을 하지 않는다.
- 빌드/배포/docker/restart는 지시 범위와 승인 정책을 따른다.

## 2단계: 핵심 축별 파일 읽기

작업 전 최소로 읽을 파일은 다음과 같다.

| 축 | 우선 파일 |
|---|---|
| 인증/SaaS | `app/auth.py`, `app/api/auth.py`, `docs/SAAS_USER_ACCESS_AND_BRIEFING_POLICY.md` |
| 채팅 | `app/routers/chat.py`, `app/services/chat_service.py`, `src/app/chat/page.tsx` |
| 브리핑/아젠다 | `app/api/briefing.py`, `app/api/agenda.py`, `app/services/agenda_service.py` |
| 아티팩트 | `app/api/artifacts.py`, `app/services/db_recorder.py` |
| 관리자 화면 | `app/api/admin.py`, `app/api/admin_users.py`, `src/app/admin/*`, `src/components/Sidebar.tsx` |
| Prompt governance | `app/services/prompt_compiler.py`, `docs/SYSTEM_PROMPT_ARCHITECTURE.md` |
| Runner | `app/api/pipeline_runner.py`, `docs/pipeline-runner/PIPELINE-RUNNER-ARCHITECTURE.md` |
| 배포 | `deploy.sh`, `/root/aads/aads-dashboard/deploy.sh`, `docs/BLUEGREEN_DEPLOY_SPEC.md` |

## 3단계: 완료 판정

변경 완료 보고에는 아래 항목을 분리한다.

| 항목 | 완료 기준 |
|---|---|
| 코드 | 변경 파일 diff와 영향 범위 확인 |
| 검증 | `py_compile`, pytest, eslint, tsc, curl/API smoke 중 해당 범위 실행 |
| SaaS 격리 | CEO/internal 토큰과 customer 토큰의 접근 차이 확인 |
| DB | 스키마/row count/샘플은 실제 조회값으로 보고 |
| 배포 | active slot health와 외부 URL/API 응답 확인 |
| 문서 | `HANDOVER.md` 또는 `docs/` 기록 |

## 개인 비서 모드 P0 기준

- CEO/internal admin은 기존 홈, 운영, 프로젝트, 관리자 메뉴를 유지한다.
- customer 일반 사용자는 `/chat` 중심으로 진입하고 AADS 내부 운영 프로젝트를 기본 안내받지 않는다.
- customer 브리핑, 아젠다, 아티팩트는 tenant/session scope로만 노출한다.
- 고위험 실행은 승인 정책을 통과해야 한다.
