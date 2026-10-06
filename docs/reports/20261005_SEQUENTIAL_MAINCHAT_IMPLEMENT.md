# 순차 복구 4단계 — 오비스 메인 채팅 승인 범위 구현

- TASK_ID: AADS-SEQUENTIAL-MAINCHAT-IMPLEMENT-20261005 (P1/M, DEPLOY false, PUSH_ONLY)
- 기준 문서(읽기 전용): `docs/prd/20261003_AADS_OHVIS_MAIN_CHAT_PRD.md` 및 PLAN/SPEC (다른 세션의 미추적 파일, 수정하지 않음)
- 상태: **구현 코드와 테스트는 워킹트리에 있다. 커밋·push·빌드·배포·마운트·마이그레이션 적용은 하나도 하지 않았다.**

## 1. 결론

최소 유효 단위를 **언마운트된 백엔드**로 구현했다. 보고 수신(durable inbox) → 근거 검토 → 승인(grant) → 효과 outbox → 일시 중단/재개 → 오비스 내부 알림 → 카드 조회까지 한 줄로 이어지고, 서비스·SQL 수준 테스트 48건이 통과했다.

**끝나지 않은 것(핵심):**

1. 실제 근거 조회기(commit/artifact/test 재조회)가 없다. 기본값은 "근거 없음 → waiting_evidence / unverifiable" 이라 어떤 구현 보고도 자동 검증되지 않는다.
2. 실제 러너 adapter가 없다. 공유 `submit_job` 연결이 필요하고 운영 DB 쓰기 없이는 테스트할 수 없어 만들지 않았다. 기본 `NullAdapter`는 모든 dispatch를 거부한다. 따라서 **이 단위로는 실제 후속 job이 한 건도 제출되지 않는다.**
3. UI/목업 산출물을 만들지 않았다. 승인된 목업 revision이 없고, 미승인 revision을 승인 처리하지 않는다는 제약을 지켰다.
4. 라우터는 `app/main.py`에 마운트하지 않았다(§6 의존성).

## 2. 선행 승인 상태와 처리

| 선행 | 상태 | 이 작업에서의 처리 |
|---|---|---|
| NODEPLOY-COMPAT-GUARD (A) | 러너 PUSH_ONLY 게이트 FAIL, 지시문 문구로만 완화 | 효과를 만들지 않는 additive 코드만 작성 |
| CONTROL-VERIFY (C) | 승인 0건, 목업 revision 0건 | 어떤 승인도 만들거나 대체하지 않음. 미확정 메인 채팅 goal을 R-DOC goal(0361c451-…)로 치환하지 않음 |
| ARTIFACT-RECOVER (B) / CONTINUATION-E2E | 조건부 통과 | 위와 동일 |

선행 수용 기준이 완전히 충족되지 않았으므로 **후속 효과(job 제출, 알림 발송, 마운트, 플래그 ON)는 만들지 않았다.** 자동 효과 플래그 `OHVIS_MAIN_CHAT_AUTO_EFFECT`는 기본 OFF이고 전역 자동 실행을 켜지 않았다.

## 3. STEP 0 — 기존 요소 분류

| 대상 | 분류 | 비고 |
|---|---|---|
| `chat_service._is_local_active_api_slot` | [유지] | 서비스가 지연 import로 호출만 한다 |
| `chat_service` deferred-reaction busy-session SQL | [유지] | 같은 조건(`chat_turn_executions` running/retrying + lease)을 `claim_next`에 복제 |
| `canonical_documents` (`SECRET`, `_authorize`, `_scope`, `_project`, `VIEW`, `WRITE`) | [유지] | import 해서 사용 |
| `app/auth.py` (`TenantRole`, `require_tenant_role`) | [유지] | `VIEW`/`WRITE` 경유 |
| `app/core/db_pool.get_pool` | [유지] | |
| `pipeline_runner.submit_job` / `notify_completion` / `_enqueue_terminal_followup` | [유지] | 호출하지 않음. 연결은 §6 의존성 |
| `ohvis_notifications`, `mockup_review_service`, `mockup_reviews` API | [유지] | kind CHECK가 목업 소관이라 건드리지 않고 별도 notices 테이블 사용 |
| `chat_sessions`, `chat_turn_executions`, `pipeline_jobs`, `chat_workspace_change_ledger` | [유지] | 읽기 전용(앞 둘만 claim SQL에서 SELECT) |
| `app/main.py` 라우터 등록 | [유지] | 이 단위는 수정하지 않음. 마운트는 §6 |
| `app/api/ohvis_main_chat.py` (router, 9 endpoint) | [신규] | 미마운트 |
| `app/services/ohvis_main_chat_service.py` | [신규] | |
| `app/models/ohvis_main_chat.py` | [신규] | |
| `tests/integration/test_ohvis_main_chat.py` | [신규] | |
| 테이블 8개 (`ohvis_main_chat_routes/control/audit/notices`, `ohvis_report_inbox/reviews`, `ohvis_action_grants/outbox`), 함수 `ohvis_main_chat_audit_append_only` | [신규] | 어느 DB에도 적용하지 않음 |
| 환경 플래그 4개 | [신규] | 전부 기본 OFF/미설정(미설정 cap = fail-closed) |
| 삭제 | 없음 | 삭제한 함수·엔드포인트·테이블 없음 |

**TARGET_FILES 밖 파일 2개를 만들었다.** `migrations/20261005_ohvis_main_chat.sql`, `migrations/rollback/20261005_ohvis_main_chat.down.sql`. 사유: durable inbox/outbox는 스키마 없이 존재할 수 없다. 둘 다 신규·additive·멱등(DROP 없음, 기존 테이블 무변경)이고 rollback 짝이 있다. 소유자는 이 작업이며, **적용은 Runner 승인 후 `scripts/apply_release_migrations.sh` 경유**다.

## 4. MC-F / MC-N 매트릭스

범례: 구현=코드 존재 / 검증=이번 세션에 실행해 통과 / 미완=남은 것.
"검증"은 서비스 함수와 임시 PostgreSQL 15에 대한 테스트다. HTTP 계층(인증·권한 Depends)은 라우터가 마운트되지 않아 **실행 검증하지 못했다.**

### MC-F (20)

| ID | 구현 | 검증 | 미완 |
|---|---|---|---|
| F01 메인 채팅 지정 | O route upsert/list, revision CAS, 타 tenant 세션 거부 | O DB | 미지정 시 `waiting_route` 보존은 검증. HTTP 권한 미검증 |
| F02 tenant/project/goal 라우팅 | O 요청에 tenant/session 필드 금지(extra=forbid), goal 정확 매칭 후 opt-in 프로젝트 route만 폴백 | O DB (교차 tenant 분리, goal 폴백 규칙) | goal_id의 실재 여부는 goal 테이블에 조회하지 않는다 → **위조 goal 거부는 부분** |
| F03 구조화 보고 | O 필수 필드·schema_version 검증 | O pure | |
| F04 source event dedupe | O UNIQUE + payload SHA-256, 불일치 시 quarantine 감사 | O DB 순차 재전송·충돌 | **동시 수신 경합은 테스트하지 않음**(UNIQUE+ON CONFLICT에 의존) |
| F05 도착≠완료 | O 카드 라벨에 "완료" 없음, 배지 `report_received`/`code_verified`/`tests_rechecked=false` | O | 마일스톤 생성 경로를 건드리지 않음 |
| F06 실제 근거 검토 | 부분 | O 판정 규칙 | **근거 조회기 미구현**, 테스트 재실행 미구현. 기본은 근거 없음 → 확정 불가 |
| F07 선행 납품 확인 | 부분 | O 게이트가 `verified+code_verified` 아니면 차단 | 근거 조회기가 없어 실제 납품 확인은 불가 |
| F08 승인 범위 내 후속 러너 | 부분 | O 가짜 adapter로 1건 제출·중복 없음 | **실제 러너 adapter 없음 → 실 job 0건** |
| F09 승인 유형 분리 | O design_approval/code_review 생성 불가, push grant는 effect 미생성, push/deploy action은 adapter 없음 | O pure+DB | |
| F10 승인 재검증 | O 만료·취소·revision·scope·hash·commit/generation 재검사 | O pure(26 케이스)+DB | |
| F11 수정본 디자인 재승인 | **미구현** | — | 목업 승인 도메인 소관. 이 단위는 어떤 승인 상태도 만들지 않는다 |
| F12 동일 효과 단일 실행 | O unknown → adapter 조회(found/not_found/unavailable) | O 가짜 adapter, 같은 key로 재전송 확인 | 실제 원격 조회 미검증 |
| F13 busy 세션 대기 | O `waiting_session`, claim/model 카운터 불변 | O DB (최소 스키마 테이블 기준) | 실제 `chat_*` 스키마 대상 아님 |
| F14 보고방 변경·삭제 | 부분 | O 변경 revision | **삭제·내부 보관 미구현** |
| F15 오비스 전용 알림 | O in-app notices, 내부 deep link만, 외부 sender 없음 | O | deep link 목적지 화면 없음 |
| F16 일시 중단·재개 | O pause 시 claim·dispatch 차단, resume 시 게이트 재평가 | O DB | |
| F17 실제 자료 열람 | **미구현** | — | |
| F18 업무 이력·핸드오버 | 부분 | O audit append-only, 카드에 source→review→effect→job 링크 | `project_handover_entries`/HANDOVER.md 미기록(§8) |
| F19 보고 최신성 | O 낮은 revision은 `obsolete`, 최신 상태를 되돌리지 않음 | O DB | |
| F20 이해 가능한 상태 | 부분 | O 카드 API | **첫 화면 UI 없음** |

### MC-N (12)

| ID | 구현 | 검증 | 미완 |
|---|---|---|---|
| N01 durable inbox/outbox | O | O 만료 lease 재청구, unknown/dispatching 복구 | 실제 프로세스 재기동 시험은 안 함 |
| N02 lease/epoch fencing | O | O 구 epoch 결과 저장 거부 | dispatch의 epoch 비교는 pure 검증만 |
| N03 active slot | O claim·dispatch·recover가 standby에서 중단 | O (슬롯 판정은 주입 함수) | 실제 블루/그린 파일 기반 판정은 미검증 |
| N04 retry 회계 분리 | O 4개 카운터 분리, 모델 호출 예약만 증가 | O DB | |
| N05 비용·루프 제한 | 부분 | O 모델 호출 cap(미설정=거부), 루트당 effect 상한, `ohvis_main_chat` source 금지 | 세대 상한·자가 반응 탐지 미구현 |
| N06 권한 fail-closed | O | O pure | DB 장애 주입은 안 함 |
| N07 비밀값·입력 지시 방어 | 부분 | O 비밀 패턴 보고·grant 거부, 보고 본문은 어떤 지시로도 해석되지 않음 | 패턴 기반이라 우회 가능 |
| N08 관측과 감사 | 부분 | O append-only 감사 | correlation별 실제 비용 기록 없음 |
| N09 릴리스 안전 계약 | — | — | **승인 후 Runner 빌드 검증 대상** |
| N10 모바일·세션 복구 | **미구현** | — | UI 없음 |
| N11 원격 idempotency 한계 | O 조회 불가 adapter는 blocked, 재전송 안 함 | O | |
| N12 성능·비용 정직성 | O 개선율 주장 없음 | — | 이 코드는 LLM을 호출하지 않는다(외부 LLM 호출 0). 세션 자체의 모델 비용은 **측정하지 못했다** |

## 5. 검증 명령과 결과

| 명령 | 결과 |
|---|---|
| `ruff check --select F821,F811,F401,F841` (신규 4개 py) | 통과 |
| `python3 -m compileall -q` (신규 4개 py) | 통과 |
| `python3 scripts/dup_guard.py --paths <신규 4개 py>` | 출력 없음(위반 없음). 스테이징 상태 검사가 아님 |
| `bash scripts/run_unit_tests.sh tests/integration/test_ohvis_main_chat.py` | 37 passed, 11 skipped (DB 테스트는 URL 없어 skip) |
| 임시 `pgvector/pgvector:pg15` 컨테이너(`docker run --rm`)에 `OHVIS_MAIN_CHAT_TEST_DATABASE_URL`을 주고 pytest | **48 passed** (DB 11건 포함) |

- 첫 DB 실행에서 1건 실패했고 실제 결함이었다: payload 충돌 감사 행을 예외를 던질 트랜잭션 안에서 기록해 롤백됐다. 트랜잭션 커밋 후 예외를 던지도록 고쳤고 재실행해 통과했다.
- 운영 `aads-postgres` 등 기존 DB에는 쓰지 않았다. 임시 컨테이너는 종료돼 남아 있지 않다.
- 마이그레이션 up 2회 적용·down·재적용을 임시 DB에서 확인했다. 운영 DB에는 적용하지 않았다.
- 실행하지 않은 것: 전체 `tests/unit` 회귀, HTTP 엔드포인트 호출, 실제 `chat_*` 스키마 대상 claim, pre-commit hook(커밋하지 않음).

## 6. Runner 의존성 (공유 파일은 몰래 고치지 않았다)

모두 승인 후 별도 단계이며 범위·소유자를 명시한다.

1. `app/main.py` — 라우터 1줄 `include_router(...)`. 소유: 공유 앱 진입점.
2. 마이그레이션 적용 — `scripts/apply_release_migrations.sh`(up 파일에 DROP 없음; 게이트 통과 여부는 이 세션에서 실행해 확인하지 않음).
3. 러너 adapter — `pipeline_runner.submit_job`를 `effect_key` 멱등 조회와 함께 감싸는 구현. 소유: pipeline_runner. 없으면 자동 효과는 계속 0건이다.
4. 근거 조회기 — commit/diff/artifact 재조회 함수를 `EvidenceProvider`로 주입.
5. 보고 생산자 연결 — `notify_completion`/`_enqueue_terminal_followup`이 `POST /projects/{p}/main-chat/reports`로 보내도록.
6. 워커 스케줄 — `run_once`를 주기 호출하는 곳이 없다(의도적).
7. UI — 목업 revision 제출·승인 게이트 통과 후에만 가능.
8. 플래그 — `OHVIS_MAIN_CHAT_ENABLED` 등은 점검 창구에서 CEO 승인 후.

## 7. 커밋·push·배포

- 커밋: **없음.** push: **없음.** GitHub URL: **없음.** (이 작업 지시가 git add/commit/push를 금지한다. 커밋·push는 CEO 승인 후 Runner가 수행한다.)
- 배포·빌드·재시작: **하지 않았다.** 대상이 아니다.
- 워킹트리 변경은 신규(미추적) 파일 6개뿐이며 다른 세션의 dirty/staged 파일은 건드리지 않았다.

## 8. 핸드오버·비용

- `project_handover_entries` 기록: **하지 않았다.** HANDOVER.md 동기화: **하지 않았다.** R-001에 따라 이 작업은 "완료"가 아니라 "구현 단위 작성, 승인 대기"로 보고한다.
- 외부 LLM 비용: 이 코드가 LLM을 호출하지 않고 테스트도 호출하지 않아 **코드 기준 $0**. 에이전트 세션 자체의 토큰 비용은 측정 수단이 없어 **미측정**이다($5 상한 준수 여부를 입증할 수 없다).

## 9. 남은 결함·위험

- 근거 조회기와 러너 adapter가 없어 end-to-end 자동 경로는 열려 있지 않다(의도된 fail-closed).
- `waiting_evidence` 상태는 운영자가 `/evidence/requeue`로 되돌려야 한다. 자동 재시도 없음.
- 위조 goal_id 검증, route 삭제·보관, 세대 상한, 자가 반응 탐지, 비용 기록은 미구현.
- 비밀 탐지는 `canonical_documents.SECRET` 패턴에 의존한다.
- 동일 이벤트 동시 수신 경합과 HTTP 권한 경로는 실행 검증하지 못했다.
- AAG 착수 브리프의 기존 항목(P0 `/ops/ai-response-errors` ROUTE_MISSING, P1 `api.ts:821` PATH_DRIFT, `/` 네임스페이스 소유, 동적 SQL 테이블)은 이 단위 범위가 아니라 건드리지 않았다.
- 오류 사전: 새로 확정한 오류 원인이 없어 등록하지 않았다(트랜잭션 내 감사 롤백은 이번 구현 중 발견·수정한 것이며 재발 방지는 테스트 `test_inbox_is_idempotent_conflict_quarantined_and_tenant_scoped`가 막는다).
