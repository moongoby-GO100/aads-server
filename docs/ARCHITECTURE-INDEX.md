# AADS 아키텍처 문서 인덱스 (단일 진입점)

_작성: 2026-08-21 11:00 KST | 검증 기준: contabo116(5.104.86.116) 실측_

> 이 문서는 AADS 아키텍처/운영 문서의 **정본 진입점**이다.
> 신규 세션·러너·에이전트는 이 인덱스를 먼저 읽고, 필요한 계층 문서로 내려간다.
> 각 문서에는 **최신성 등급**이 붙는다. `현행` 외 등급은 내용 인용 전 코드/DB 재확인이 필요하다.

## 최신성 등급 정의

| 등급 | 의미 | 사용 규칙 |
|------|------|-----------|
| 현행 | 최근 30일 내 갱신 + 실측 일치 | 근거로 인용 가능 |
| 부분현행 | 구조는 유효하나 수치·목록이 낡음 | 구조만 인용, 수치는 재측정 |
| 노후 | 90일 이상 미갱신, 구조 변경 반영 안 됨 | 이력 참고용, 근거 인용 금지 |
| 이력 | 완료된 작업 기록 | 감사/회고용 |

---

## 1. 최상위 (반드시 먼저)

| 문서 | 경로 | 등급 | 갱신일 |
|------|------|------|--------|
| CEO 절대 지시 | `aads-docs/CEO-DIRECTIVES.md` (GitHub raw) | 현행 | 2026-04-21 |
| 프로젝트 규칙 | `CLAUDE.md` (repo 루트) | 현행 | 상시 |
| 작업 인수인계 원장 | `docs/HANDOVER.md` (287줄) | 현행 | 2026-08-20 |
| 아키텍처 지도 | `docs/knowledge/CTO-SYSTEM-MAP.md` | 부분현행 | 2026-05-03 |
| 3단계 온보딩 인덱스 | `docs/knowledge/AADS-3STEP-SYSTEM-INDEX.md` | 부분현행 | 2026-07-15 |

## 2. 인프라 · 배포

| 문서 | 경로 | 등급 | 갱신일 |
|------|------|------|--------|
| Blue-Green 무중단 배포 명세 | `docs/BLUEGREEN_DEPLOY_SPEC.md` (278줄) | 현행 | 2026-08-21 |
| 직접작업 의존성 정책 v1.0 | `docs/knowledge/DIRECT-WORK-DEPENDENCY-POLICY-v1.0.md` | 현행 | 2026-07-31 |
| 개발 플로우 v1.1 / 체크리스트 | `docs/knowledge/DEV-FLOW-v1.1.md`, `DEV-FLOW-CHECKLIST-v1.0.md` | 현행 | 2026-07-31 |
| 백업 보존 정책 | `docs/AADS-BACKUP-RETENTION-POLICY.md` | 부분현행 | 2026-07-15 |
| 인프라 이관 계획(68→contabo5) | `docs/plans/AADS-INFRA-MIGRATION-68-TO-CONTABO5.md` | 노후 | 2026-07-15 |

### 실측 컨테이너 구성 (2026-08-21 11:00 KST, `docker ps`)

| 컨테이너 | 포트 | 상태 | 역할 |
|----------|------|------|------|
| aads-server | 127.0.0.1:8100→8080 | healthy | FastAPI 백엔드 (Blue 슬롯) |
| aads-server-green | 127.0.0.1:8102→8080 | healthy | Blue-Green 대상 (Green 슬롯) |
| aads-dashboard | 127.0.0.1:3100 | healthy | Next.js 대시보드 (Blue) |
| aads-dashboard-green | 127.0.0.1:3101 | healthy | Next.js 대시보드 (Green) |
| aads-postgres | 5433→5432 | healthy | PostgreSQL 15 + pgvector |
| aads-redis | 내부 6379 | healthy | 캐시 + 락 |
| aads-litellm | 4000 | healthy | LLM 프록시 |
| aads-nginx | — | up | 리버스 프록시 / upstream 전환 |
| aads-searxng | 8888→8080 | healthy | 메타검색 |
| aads-socket-proxy | 내부 2375 | up | 보안 Docker 소켓 |
| yeoljeong-finance | 127.0.0.1:8110→8080 | healthy | 매장비서(FOOD) 사이드카 |

> `CTO-SYSTEM-MAP.md`의 컨테이너 표에는 `aads-dashboard-green`, `aads-nginx`, `yeoljeong-finance`가 누락돼 있다. 위 표가 정본이다.

## 3. 채팅 시스템 (핵심 도메인)

| 문서 | 경로 | 등급 | 갱신일 |
|------|------|------|--------|
| 채팅 시스템 개요 | `docs/chat/CHAT-SYSTEM-OVERVIEW.md` | 부분현행 | 2026-07-15 |
| 백엔드 스펙 | `docs/chat/CHAT-BACKEND-SPEC.md` | 부분현행 | 2026-07-15 |
| 스트리밍 스펙 | `docs/chat/CHAT-STREAMING-SPEC.md` | 부분현행 | 2026-07-15 |
| 프론트엔드 스펙 | `docs/chat/CHAT-FRONTEND-SPEC.md` | 부분현행 | 2026-07-15 |
| SSE 스트리밍 아키텍처 | `docs/knowledge/SSE-STREAMING-ARCHITECTURE.md` | 부분현행 | 2026-07-15 |
| 채팅 변경 이력 | `docs/chat/CHAT-CHANGELOG.md` | 이력 | 2026-07-15 |

### 실측 코드 규모 (2026-08-21, `wc -l`)

| 파일 | 실제 줄 수 | 문서 기재값 | 비고 |
|------|-----------|-------------|------|
| `app/routers/chat.py` | 3,525 | 1,157 (CTO-SYSTEM-MAP) | 3.0배 증가 |
| `app/services/chat_service.py` | 11,703 | 4,146 (CTO-SYSTEM-MAP) | 2.8배 증가 |
| `app/services/context_builder.py` | 591 | 552 | 소폭 증가 |
| `app/services/workspace_preloader.py` | 285 | 194 | 소폭 증가 |
| `app/core/project_config.py` | 165 | 미기재 | 별칭 레이어 도입 후 신규 |
| `deploy.sh` | 1,079 | 871 (구 기록) | deploy_history INSERT 추가 |

## 4. 프롬프트 · 메모리 거버넌스

| 문서 | 경로 | 등급 | 갱신일 |
|------|------|------|--------|
| 시스템 프롬프트 아키텍처 | `docs/SYSTEM_PROMPT_ARCHITECTURE.md` | 노후 | 2026-03-31 |
| 프롬프트 최적화 리포트 | `docs/SYSTEM_PROMPT_OPTIMIZATION_REPORT.md` | 이력 | 2026-03-31 |
| 메모리 진화 아키텍처 | `docs/MEMORY_EVOLUTION_ARCHITECTURE.md` | 노후 | 2026-03-29 |
| 세션 거버넌스 v2 (최종/부록) | `reports/20260423_session_governance_architecture_v2_final.md`, `..._v2_1_addendum.md` | 부분현행 | 2026-04-23 |
| AADS 전용 지식 | `docs/knowledge/AADS-KNOWLEDGE.md` | 노후 | 2026-04-24 |

> 현재 운영 중인 5-Layer 프롬프트(L1~L5 + `prompt_assets` + `compiled_prompt_provenance`) 구조는 위 문서 어디에도 정본 기술서가 없다. **문서 공백 P0**.

## 5. Pipeline Runner

| 문서 | 경로 | 등급 | 갱신일 |
|------|------|------|--------|
| 러너 아키텍처 | `docs/pipeline-runner/PIPELINE-RUNNER-ARCHITECTURE.md` | 부분현행 | 2026-07-15 |
| 러너 API 레퍼런스 | `docs/pipeline-runner/PIPELINE-RUNNER-API-REFERENCE.md` | 부분현행 | 2026-07-15 |
| 러너 감사·개선 | `docs/reports/20260506_RUNNER_AUDIT_REMEDIATION.md` | 이력 | 2026-05-06 |

> 2026-07-31 이후 추가된 `parallel_group` 스코프 락, `actual_changed_files`, deploy preflight 완화는 러너 아키텍처 문서에 미반영이다. 근거는 `docs/HANDOVER.md` 2026-07-31 / 2026-08-20 항목.

## 6. OHVIS · 자율 루프 (기획 계열)

| 문서 | 경로 | 등급 | 갱신일 |
|------|------|------|--------|
| 자율 루프 에이전트 LAYOUT-002 | `docs/AADS-LAYOUT-002_AUTONOMOUS-LOOP-AGENT.md` | 부분현행 | 2026-08-02 |
| OHVIS 루프 시스템 LAYOUT-001 | `docs/AADS-LAYOUT-001_OHVIS-LOOP-SYSTEM.md` | 부분현행 | 2026-07-27 |
| OHVIS 실시간 응답 오케스트레이션 | `docs/reports/20260802_OHVIS_REALTIME_RESPONSE_ORCHESTRATION_PLAN.md` | 부분현행 | 2026-08-02 |
| OHVIS 완전응답 청사진 | `docs/reports/20260802_OHVIS_PERFECT_RESPONSE_SYSTEM_BLUEPRINT.md` | 부분현행 | 2026-08-02 |
| OHVIS 브라우저 에이전트 아키텍처 | `docs/plans/20260819_OHVIS_ASIDE_BROWSER_AGENT_ARCHITECTURE.md` | 현행 | 2026-08-19 |
| Agent Vault 계정등록 UI 계획 | `docs/plans/20260820_OHVIS_AGENT_VAULT_ACCOUNT_REGISTRATION_UI_PLAN.md` | 현행 | 2026-08-20 |

## 7. 장애 · WRAP (최근)

| 문서 | 경로 | 갱신일 |
|------|------|--------|
| 로그인 API 502 포스트모템 | `docs/reports/INCIDENT-20260821-AADS-LOGIN-API-502.md` | 2026-08-21 |
| FOOD 브라우저 P0 WRAP | `docs/AADS-WRAP-FOOD-BROWSER-P0_20260820.md` | 2026-08-20 |
| 메시지 소실 P0 WRAP | `docs/AADS-WRAP-MSG-VANISH-P0_20260818.md` | 2026-08-18 |

## 8. 변경 이력 원장 (자동 기록, 대용량 — 통독 금지)

| 파일 | 크기 | 용도 |
|------|------|------|
| `docs/CHANGELOG-go100-direct.md` | 1,041 KB | GO100 직접수정 자동 로그 |
| `docs/CHANGELOG-direct-edit.md` | 267 KB | AADS 직접수정 자동 로그 |
| `docs/CHANGELOG-dashboard-direct.md` | 68 KB | 대시보드 직접수정 자동 로그 |

> 위 3개는 배포 preflight의 dirty 원인 1순위다. 조회 시 `tail`/`grep`만 사용한다.

---

## 알려진 문서 공백 (P0/P1)

| 우선순위 | 공백 | 영향 |
|---------|------|------|
| P0 | 5-Layer 프롬프트 거버넌스 정본 기술서 없음 | L3 누락·provenance 판정 시 매번 코드/DB 역추적 |
| P0 | `CTO-SYSTEM-MAP.md` 수치 노후(코드 3배 증가 미반영) | 신규 러너가 잘못된 규모 전제로 설계 |
| P1 | 프로젝트 별칭 레이어(`project_config.resolve_project`) 문서 없음 | 프로젝트 라벨 정규화 규칙이 코드에만 존재 |
| P1 | 러너 동시성(`parallel_group`/worktree/락) 문서 미반영 | 동시 배포 정책이 HANDOVER 산문에만 존재 |
| P2 | `aads-docs` repo 아키텍처 문서 3월 고착 | 외부 참조 시 구조 오해 |

## 폐기·정리 대상

- `docs/*.bak_aads` 3건 (`HANDOVER.md.bak_aads` 85KB 등) — 백업 잔여물
- `docs/hook_sed_test.md`, `docs/appium.service`, `docs/ProductController_AADS.php` — 문서 디렉터리 오배치
- `docs/AADS-CHAT-SYSTEM-TECHNICAL-DOC*.html` 5개 버전 병존 — 최신 1개만 유지 권장
- `aads-docs/architecture/*`, `aads-docs/design/aads-architecture-v1*.md` — 2026-03월 고착, `노후` 표기 필요
