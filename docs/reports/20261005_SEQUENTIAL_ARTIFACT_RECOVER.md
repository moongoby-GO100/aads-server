# AADS-SEQUENTIAL-ARTIFACT-RECOVER-20261005 — 순차 복구 2단계: 기존 목업 채팅 작업 재개와 문서 열람 검증

작성: 2026-10-05 KST · 러너 job `runner-a0be8138` · 코드 변경 없음(보고서 1개만 추가) · 커밋/푸시/빌드/배포/재시작/DB 쓰기 안 함

## 0. 결론 (먼저)

**이 단계는 "재개 가능 상태로 정리"까지만 했다. 승인·재개·화면 검증은 하지 않았고, 구현 성공으로 종료하지 않는다.**

| # | 항목 | 판정 | 근거 |
|---|---|---|---|
| 1 | 선행 A 의 수락조건 | **미충족(부분 완화)** | A 보고서 §0 에 FAIL 2건(러너 실행본에 PUSH_ONLY gate 미반영). 실행본 지금도 `fingerprint 56080140393b`, MainPID 4050729 그대로 |
| 2 | 4fddd8cf 코드 검수 | **PASS (재현됨)** | tsc 0 / vitest 68 passed / lint 29 = 기준선 29. 커밋·diff·금지파일 확인 (§2) |
| 3 | 4fddd8cf 화면 검증 | **미완료** | 로그인 화면 근거 없음. E2E Vault 계정 401, 브라우저 도구 없음 |
| 4 | 4fddd8cf push-only 승인 | **하지 않음 (보류)** | 1·3 이 미충족. 승인은 "화면 게이트 + PUSH_ONLY 보장" 이 둘 다 필요 (§3) |
| 5 | e266ec09 유지·재개 | **유지(queued), 재개 불가** | 지시서가 스스로 "ba8dab0 이 origin/main 에 없으면 page.tsx 수정 금지·blocked" 를 요구. 현재 origin/main 에 없음 (§4) |
| 6 | 기획/설계/PRD 본문·해시 열람 | **DB 수준 PASS, 화면 FAIL(미검증)** | 3종 DB 본문·SHA-256 확인. 로그인된 /chat 열람은 못 함 (§5) |
| 7 | 수정 목업 최신 revision | **존재하지 않음** | `mockup_review_heads/revisions/change_requests/events/notifications` 전부 0행 (§5) |
| 8 | 종결 후속 검토 예약 | **GAP** | 종결(done/error) 잡은 예약됨. 4fddd8cf 는 `awaiting_approval` 이라 예약 없음 (§6) |

선행 A 의 FAIL 이 해소되지 않았으므로, 지시서 규칙("선행 수락조건 미충족 시 후속 효과를 만들지 말고 차단 사유 보고")에 따라 승인·상태 변경·재큐잉 같은 효과는 만들지 않았다.

## 1. STEP 0 — 조회 대상 분류

| 대상 | 분류 | 비고 |
|---|---|---|
| `runner-4fddd8cf` (pipeline_jobs, 커밋 `34106ae0`) | 유지 | 읽기만. 상태·승인 불변 |
| `runner-e266ec09` (pipeline_jobs, depends_on 4fddd8cf) | 유지 | 읽기만. 중복 생성·의존 해제 없음 |
| 대시보드 `src/app/chat/page.tsx` 외 3개 | 유지(읽기만) | 이 조율 작업에서 수정 안 함 (지시) |
| `docs/reports/20261005_SEQUENTIAL_ARTIFACT_RECOVER.md` | 신규 | 이 보고서(TARGET_FILES) |
| 삭제 | 없음 | 호출처 영향·롤백 대상 없음 |

동일 파일 충돌: 이 보고서 경로를 건드리는 ledger 행·다른 워크트리 변경 없음. `chat_workspace_change_ledger` 에서 `chat/page.tsx`·`ChatArtifactPanel.tsx` 행은 모두 `deployed`/`reconciled_clean`(최신 2026-09-21) — 현재 `dirty` 로 걸린 행 없음. 대기 대상 파일 충돌(`page.tsx` 4fddd8cf→e266ec09)은 **정당한 직렬이라 보존**했다.

repo lock: 이 job 의 TARGET 은 aads-server 이고 4fddd8cf·e266ec09 는 aads-dashboard 라 lock 이 겹치지 않는다. 이 job 은 대상 job 이 끝나기를 기다리지 않고 종료하므로 C(`runner-4e443ba6`) 시작을 막지 않는다.

## 2. runner-4fddd8cf 재검수 (확인한 사실)

대상은 목업이 아니라 `AADS-CHAT-INTERRUPT-ACK-LIFECYCLE-UI-P0-20261003-R3-REBASE` — 추가 지시 수명주기 UI(ba8dab0)를 최신 origin/main 위로 리베이스한 것. e266ec09 의 `page.tsx` 선행이다.

| 항목 | 값 | 확인 방법 |
|---|---|---|
| 커밋 | `34106ae0755b564b2e4065de61ef353cb6f1e68b` (author root) | `git cat-file -t`, `git log` |
| 부모 | `238713249f0f` = 현재 `origin/main` (`2387132`) | `git show --format=%P`, `git log origin/main` |
| 변경 파일 | `src/app/chat/page.tsx`(+193 −18, 총합에서 역산), `interruptLifecycle.ts`(+452), `interruptLifecycle.test.ts`(+279) — 3개, 총 +924/−18 | `git show --stat`, DB `actual_changed_files` 와 일치 |
| 금지 파일 | GoalPanel 계열·`chatDeepLink.ts` 변경 0 | `git diff --name-only` 에 없음 |
| 충돌 마커 | `page.tsx` 에 `<<<<<<<`/`=======`/`>>>>>>>` 0건 | 커밋 본문 grep |
| origin/main 포함 | **아님** (push 전) | `merge-base --is-ancestor` → NOT |
| ref | 어떤 브랜치/태그에도 속하지 않음 (러너 워크트리 `/tmp/aads-wt-runner-4fddd8cf` 의 detached HEAD) | `git for-each-ref --contains` 빈 결과 |
| 리뷰 | `review_verdict=APPROVE`, score 0.845, `approval_requested_at` 2026-10-04 09:42, `approved_at` 없음 | DB |

### 재실행 결과 (격리 사본: `git archive 34106ae0` → `/tmp`, `node_modules` 는 심볼릭 링크)

| 검증 | 명령 | 결과 | 러너 보고와 |
|---|---|---|---|
| 타입 | `npx tsc --noEmit` | exit 0, 오류 0 | 동일 |
| 단위 | `npx vitest run src/features/chat src/lib/chatDeepLink.test.ts` | 5 files / **68 passed** | 동일 |
| lint | `npm run lint:chat` (`--max-warnings 23`) | 커밋 **29 warnings / 0 errors**, 부모(`23871324`) **29 / 0** | 동일. 게이트(23) 자체는 기준선부터 실패 중 — 이번 변경이 늘리지 않음 |

주의: 위 측정은 사본 기준이라 러너 워크트리의 상태와 바이트 단위로 같다는 뜻은 아니다. 커밋 객체에서 직접 뽑았으므로 **커밋 내용**에 대한 검증으로는 유효하다.

미검증: 화면. 러너 보고대로 E2E Vault 계정 `7e157c42` 가 401 이며, 빌드는 금지라 실행하지 않았다(승인 후 Runner 빌드 검증 대상).

## 3. push-only 승인을 하지 않은 이유 (보류 조건)

지시서: "검수 통과하고 PUSH_ONLY가 보장될 때 정식 `pipeline_runner_approve` 경로로 push-only 승인 가능".

- 검수(§2): 통과.
- **PUSH_ONLY 보장**: 러너 실행본은 여전히 옛 게이트(A 보고서 §3). 다만 이번 확인으로 한 가지 완화 사실을 얻었다 — 현재 DB 의 지시서 7건(A·B·C·D·E, 4fddd8cf, e266ec09)을 **옛 게이트 함수와 신규 함수 모두에 돌려 본 결과 7건 전부 FORBID** 다 (A 가 `done / push_only_by_directive` 로 끝나고 `deployed_at` 이 비어 있는 것과도 일치). A→B 호환 보강("배포 금지" 문구)이 옛 게이트도 막도록 만든 결과로 추정한다. 단 이는 **문구 의존적 완화**이며 실행본이 고쳐진 것이 아니다. 문구가 빠진 지시서는 여전히 배포로 새어 나간다.
- **화면 게이트**: `/pipeline/jobs/{id}/approve` 는 e2e 증거 게이트(`assert_screen_evidence_gate`)를 실행한다. 4fddd8cf 에는 화면 증거가 없다. 게이트 거절이 예상되며, 그 경우 bypass/defer 플래그나 DB 상태 조작은 하지 않는다(지시).
- 승인 호출은 인증이 필요한 상태 변경이다. 이 세션에는 승인 principal 이 없고(A 보고서: 자동승인 grant 0건), CEO 의 "순차 진행" 승인만으로 특정 커밋의 승인을 대신 누르지 않았다.

**승인 직전 체크리스트 (사람 또는 승인 권한 세션이 수행)**
1. `runner-4fddd8cf` 가 여전히 `awaiting_approval`, `commit_hash` 가 `34106ae0…` 인지 재조회.
2. 승인 화면에서 "push 만, 배포 없음" 을 확인(지시서에 `PUSH_ONLY`·`빌드·배포 금지` 포함 — 옛/신 게이트 모두 FORBID 확인됨).
3. 화면 증거: 테스트 preview 또는 로그인 /chat 의 추가 지시 접수 카드·상태 배지·스냅샷 복원. 없으면 게이트 거절을 그대로 받아들이고 증거 확보 작업을 연결.
4. 승인 감사 기록에 "CEO 순차 진행 승인(직전 5단계 우선순위 보고)" 을 근거로 남긴다.

## 4. runner-e266ec09 (목업 채팅 보완) 유지·재개

| 항목 | 실측 |
|---|---|
| 상태 | `queued / queued`, `started_at` 없음, `depends_on=runner-4fddd8cf` |
| 소유 | `REWORK_OF: runner-fe81943c`, TASK_ID `AADS-RDOC-MOCKUP-CHAT-REWORK-20261004`. 구현 소유는 이 job (지시) |
| 기반 산출물 | 브랜치 `backup/runner-fe81943c-ad2fa25` → `ad2fa252d71e…` 존재 확인(9파일 +2221/−4 로 지시서에 기재) |
| 선행 조건 | ba8dab0 이 origin/main 에 있어야 함. `git branch -r --contains ba8dab0` **결과 없음** → 지금 시작하면 지시서 규칙상 `blocked` 로 끝남 |
| 이력 | 선행이던 `runner-ba03a3c8` 가 rejected 되어 한 번 `blocked_dependency` 로 종결됐고, Runner Guard 가 승계 작업 4fddd8cf 에 재연결함 |
| 중복 생성 | 하지 않음. 새 목업 구현을 제출하지 않음 |

판단: e266ec09 는 **의존이 정확히 걸려 있어 그대로 두는 것이 맞다.** 풀면 선행 없이 `page.tsx` 에 올라타 같은 충돌(rejected 사유)을 반복한다. 의존을 임의로 해제하거나 취소하지 않았다.

기존 지시 완결에 필요한 보완(별도 write scope 제출은 하지 **않음** — 선행이 풀린 뒤에 판단):
- 지시서가 요구하는 실제 `/mockup-reviews` API 대상 Playwright(desktop 1280×800, mobile 390×844)는 §5 처럼 **운영 테이블이 비어 있어** 대상 데이터가 없다. e266ec09 가 시작되면 "운영 테이블이 없으면 blocked_evidence" 조항에 걸릴 수 있다 — 데이터가 아니라 테스트 데이터 시드가 선행 과제다.
- TARGET_FILES 의 `tests/rdoc-mockup-review.spec.ts`, `vitest.config.mts` 는 e266ec09 소유이므로 건드리지 않는다.

## 5. 문서·목업 열람 검증

### 5.1 DB 수준 (확인함)

세션 `8bf0405a-1f22-4ad9-bb09-6e0fce8c6339` 의 `chat_artifacts`:

| 문서 | artifact id | 길이 | SHA-256 (content UTF-8) |
|---|---|---|---|
| 기획서 `오비스 메인 채팅 능동 업무 운영 기획서` | `2ccff0af-e5c6-420f-b254-c1dbab904ca3` | 6243 | `d8bca56372a70a288063bd9ff45e636e7359820c1d293adbd2cb126427624fbe` |
| 설계서 `오비스 메인 채팅 능동 검토·실행 설계서` | `fc820fe2-ce7a-430e-8acd-1bafb3544be3` | 8787 | `8cfac4a8d48d54ac1e1cd44f794ddbc58b76019f1db6c63fd8210fdf033e8f85` |
| PRD `오비스 메인 채팅 능동 운영 PRD` | `842a973c-8b66-4970-911e-7717e76f7b35` | 6614 | `2d9466217acf533eba66717fc50cd582c90eca243640d0627b7c2d06f735eaeb` |
| 목업 수정 보고 `[수정 보고] R-DOC 목업 검토 v1→v2 채팅 중심 전환 (초안·미승인)` | `96a72e2d-851f-5bd6-90cb-90951ed7b62b` | 6018 | `091a16c16fcf364e5178d257d8aeaad77bc214129a2d66e0f4e11af069fc6264` |

세 문서는 모두 생성 2026-10-03 16:49 KST, 본문이 DB 에 있다. 기획·설계·PRD 는 사실상 "초안" 상태로 보고돼 있고 승인된 정본이 아니다(프로젝트 문서 조회에서도 "승인된 정본 문서 없음").

### 5.2 목업 revision (확인함: 없음)

| 테이블 | 행 수 |
|---|---|
| `mockup_review_heads` | 0 |
| `mockup_review_revisions` | 0 |
| `mockup_review_change_requests` | 0 |
| `mockup_review_events` | 0 |
| `mockup_review_task_bindings` | 0 |
| `ohvis_notifications` | 0 |

따라서 **"수정 목업 최신 revision 본문·해시"는 열람할 대상이 없다.** 위 4번째 문서는 목업 revision 이 아니라 수정 보고서이며 "초안·미승인" 이다. 수정요청 → 새 revision → 재승인 → 오비스 알림의 DB 저장은 **한 번도 일어난 적이 없다.** 이를 만들어 내려고 임의 revision 을 넣거나 승인하지 않았다(지시).

### 5.3 화면 (미완료 유지)

| 단계 | 결과 |
|---|---|
| 로그인된 브라우저 /chat | **수행 못 함** — 브라우저 도구 없음, E2E Vault 계정 401(러너 보고) |
| HTTP 폴백 `https://aads.newtalk.kr/chat` | 307 (로그인 리다이렉트, 정상 동작) |
| HTTP 폴백 `…/api/v1/mockup-reviews` | 401 (인증 필요, 라우트 존재) |
| API health `…/ops/health-check` | 200, `pipeline_healthy=true`, stalled 0 |
| 프로세스 | 러너 2개, relay 서버 기동 중 |

PC·모바일 화면, 기획/설계/PRD 열람 화면, 수정요청 흐름 화면은 **전부 미완료** 로 남긴다. 인증 우회·Vault 외 경로·새 디자인 승인은 하지 않았다.

## 6. 장기 대기 시 독립 후속 검토 예약 검증 (GAP)

- 종결 후속 검토 기능(`chat_deferred_reactions`, `runner_terminal:<job>:<status>:<sha>`)은 동작 중: A 는 `pending`(01:55:49), 51aca7ee 는 `claimed`. 이 세션 기준 completed 110 / failed 21 / skipped_stale 11.
- 그러나 이 기능은 **`done`/`error` 만** 예약한다 (`RUNNER_CONTINUATION_RECOVERY` §0). 4fddd8cf 는 `awaiting_approval` 로 약 16시간(2026-10-04 09:42~) 멈춰 있고 e266ec09·그 뒤 작업이 모두 이 한 곳에서 막혀 있지만, **승인 대기 장기화를 깨우는 예약은 없다.**
- 이 작업은 DB 쓰기·크론 생성이 범위 밖이라 예약을 만들지 않았다. 제안: 승인 대기 24시간 초과 시 오비스 내부 알림(외부 sender 금지)을 1회 보내는 점검, 또는 사람이 4fddd8cf 승인 여부를 정하는 시점까지 이 보고서가 유일한 기록임을 명시.

## 7. 배포 후보 SHA / 영향 / 롤백 (준비만, 실행 안 함)

- **후보**: 대시보드 `34106ae0755b564b2e4065de61ef353cb6f1e68b` (부모 = origin/main `2387132`). 단 현재 ref 가 없으므로 push 대상 브랜치는 Runner 가 승인 후 정한다.
- **영향**: 채팅 화면(`/chat`)의 추가 지시 수명주기 UI — 접수 카드, 상태 배지, 스냅샷 복원, public_reply, ack 지연 측정. `page.tsx` 변경이 크고(딥링크 헬퍼 6곳·`surface=panel` 6곳은 origin/main 과 동일하다고 러너가 보고) 화면 미검증이므로 운영 배포 전에 테스트 preview 검증이 필요하다. **운영 배포는 별도 승인 대기**이며 이 단계의 범위가 아니다.
- **롤백**: push 가 되면 `git revert 34106ae0`. 배포는 blue/green 이므로 문제 시 직전 슬롯 유지. 실행본 러너 게이트 미반영(A §5)이라 배포 승인 시점에는 반드시 지시서의 PUSH_ONLY 문구를 사람이 확인.

## 8. 실행한 검증 명령과 결과

| 명령 | 결과 |
|---|---|
| `git status` / `git log` (워크트리), 파일 조회 | clean, A 보고서 확인 |
| `pipeline_jobs` SELECT (7개 job), `chat_workspace_change_ledger` SELECT | §0·§1 |
| `/proc`·`systemctl show aads-pipeline-runner`, `sha256sum`, `grep -c read_job_instruction_strict` | 러너 PID 4050729 불변, fingerprint `56080140393b`, gate 코드 0건 |
| 옛/신 `instruction_forbids_deploy` 를 DB 지시서 7건에 실행(bash 함수만 추출) | 7건 모두 old=FORBID / new=FORBID |
| `git show --stat`, `git diff --name-only`, 충돌 마커 grep, `for-each-ref --contains`, `merge-base --is-ancestor` (대시보드 34106ae0) | §2 |
| `npx tsc --noEmit` (사본) | exit 0 |
| `npx vitest run src/features/chat src/lib/chatDeepLink.test.ts` (사본) | 5 files / 68 passed |
| `npm run lint:chat` (커밋 사본·부모 사본) | 둘 다 29 warnings / 0 errors |
| `mockup_review_*`, `ohvis_notifications`, `chat_artifacts`, `chat_deferred_reactions` SELECT | §5·§6 |
| `curl` health-check / chat / mockup-reviews | 200 / 307 / 401 |

**실행하지 않은 것**: 로그인 브라우저 검증, Playwright, `bash scripts/run_unit_tests.sh`(코드 변경 없음), ruff/compileall(코드 변경 없음), `npm run build`·배포·재시작·push·승인 호출, DB 쓰기, handover 기록. 빌드 검증은 승인 후 Runner 대상.

**부수 사실(투명성)**: 조사 중 대시보드 공유 체크아웃에서 `git fetch -q origin` 을 한 번 실행했다(원격 추적 ref 만 갱신, 작업 트리·인덱스 불변). 검증용 임시 사본은 `/tmp/verify_b` 에 만들었고 종료 전 삭제했다.

## 9. 커밋·URL·푸시·배포·남은 장애 (분리 보고)

- **실제 커밋**: 없음(이 job 은 커밋하지 않음, 승인 후 Runner 담당).
- **GitHub URL**: 없음(커밋 전).
- **푸시**: 하지 않음. 4fddd8cf `34106ae0` 도 push 되지 않았음.
- **배포·재시작**: 하지 않음. 자동실행 전역 enable 도 하지 않음.
- **외부 LLM 비용**: 외부 LLM 호출 없음, 추가 $0. 이 세션 자체의 토큰 비용은 측정하지 않음.
- **handover**: `project_handover_entries` 기록과 `docs/HANDOVER.md` 동기화 **하지 않았다.** TARGET_FILES 가 보고서 1개이고 DB 쓰기·타 파일 수정은 범위 밖이어서다. R-001 에 따라 그 전까지 이 단계는 완료가 아니다. 기록해야 할 요지는 §0 표.

### 남은 장애

1. **[P0] 실행 러너에 PUSH_ONLY gate 미반영** (A 에서 이월, 해소되지 않음). 현재는 지시서 문구("배포 금지")가 옛 게이트도 막고 있어 우연히 안전하다.
2. **[P1] 4fddd8cf 승인 대기 약 16시간 + 후속 예약 없음**, 이로 인해 e266ec09 및 목업 채팅 보완 전체가 정지. 화면 증거가 없어 승인 게이트를 통과하기 어렵다.
3. **[P1] 수정 목업 revision·알림이 운영 DB 에 0건.** 화면 검증을 하려면 먼저 테스트 데이터/preview 가 필요하다.
4. **[P2] 34106ae0 에 ref 가 없다.** 러너 워크트리가 정리되면 객체가 gc 대상이 될 수 있다 — 승인 전에 보존용 ref 가 필요한지 Runner 가 판단.

## 10. 다음 단계(C) 인계

- 이 단계는 승인·재개를 하지 않았다. C 는 4fddd8cf·e266ec09 의 상태가 §0 과 같음을 전제로 시작한다.
- 승인 권한 세션은 §3 체크리스트로 4fddd8cf 를 판단하고, 거절되면 필요한 화면 증거 확보를 기존 작업에 연결해야 한다.
- 승인되지 않은 특정 목업 revision 은 승인하지 않았다(대상 자체가 없음).
