# R-DOC 목업 구현 연쇄 산출물 검증 및 운영 릴리스 준비 — blocked (B 미푸시 · C 없음)

- 작업: `AADS-RDOC-MOCKUP-RELEASE-EVIDENCE-20261003` · P1 · SIZE S · Runner `runner-5696989b` · 2026-10-03 KST
- 마일스톤 `3350deae-96eb-5548-a99c-adf68d984bfb` · 목표 `0361c451-cc03-4bd1-a423-76051b0546b2` · 원 세션 [`8bf0405a-1f22-4ad9-bb09-6e0fce8c6339`](https://aads.newtalk.kr/chat#8bf0405a-1f22-4ad9-bb09-6e0fce8c6339)
- 이전 보고 `20261003_RDOC_MOCKUP_RELEASE.md`(runner-7ba13423)는 기준이 `f20da86b` 였던 시점의 차단 보고다. 이후 A 가 origin/main 에 반영되어 **그 문서의 "A 미커밋" 항목은 더 이상 사실이 아니다**(§2). 이 문서가 최신 상태다.
- **결론: 완료 아님, 릴리스 가능 상태 아님, M9 completed 로 바꾸지 않았다.** 배포·운영 DB migration·재시작·commit/push 는 실행하지 않았다. "성공/배포완료" 주장 없음.

## 1. STEP 0 — 기존 구현 조사와 분류

| 대상 | 실측 상태 | 분류 |
|---|---|---|
| `app/api/mockup_reviews.py` (엔드포인트 12개: create/get review, revision create/get, asset, revising, submit, changes, change-report, approve, revoke, verify) | origin/main 에 존재, `app/main.py:3953` 에서 `/api/v1` 로 등록 | 유지 (변경 없음) |
| `app/services/mockup_review_service.py`, `app/models/mockup_review.py` | origin/main 에 존재 | 유지 |
| `migrations/20261003_mockup_reviews.sql` | origin/main 에 존재. `CREATE TABLE IF NOT EXISTS` 5개(heads/revisions/events/change_requests/task_bindings) + 인덱스·불변 트리거, 파괴적 DDL 없음. **rollback SQL 없음** | 유지 — rollback 부재는 §6 에 위험으로 기록 |
| `tests/unit/test_mockup_reviews.py`(50), `tests/integration/test_mockup_reviews_postgres.py`(15) | origin/main 에 존재 | 유지 |
| 러너/파이프라인의 `verify`·submit 게이트 소비자 | `git grep -i mockup_review -- app scripts` → A 파일 3개뿐. 파이프라인 러너·서비스에 호출처 **없음** | **신규 필요 (C 소관, 미구현)** |
| 오비스 내부 알림 생산자 (목업 검토 이벤트) | A 코드에 `notif/telegram/smtp/slack/email/sms/httpx` 0건. 기존 `push_notifications.py` 와 연결 없음 | **신규 필요 (C 소관, 미구현)** |
| 대시보드 채팅 UI | origin/main 에 없음. 로컬 백업 브랜치에만 존재 (§2 B) | 수정 대상 아님 — 이 작업이 대시보드를 바꾸지 않음 |
| `docs/reports/20261003_RDOC_MOCKUP_RELEASE_RECOVERY.md` | TARGET_FILES | 신규 |
| `HANDOVER.md` | TARGET_FILES | 수정 (최상단 항목 1개 추가, 기존 항목 보존) |
| 삭제 | 없음 | 해당 없음 |

지시서에 없는 파일 변경 없음. 다른 세션의 dirty 상태는 건드리지 않았다(이 워크트리 `git status` 깨끗, 변경은 위 두 파일뿐).

## 2. A→B→C 정확한 SHA 검증

| 단계 | runner | 기대 산출물 | 실측 | 판정 |
|---|---|---|---|---|
| A 백엔드 | `runner-aad81f4d` (runner-2f93e68d·7ba71286 대체) | mockup API·migration·테스트 | aads-server 커밋 [`1d19322f27b3de994723c4d09048b042906859c3`](https://github.com/moongoby-GO100/aads-server/commit/1d19322f27b3de994723c4d09048b042906859c3), 9개 파일 +3147. `git fetch` 후 `origin/main == 1d19322f`, 이 워크트리 HEAD 와 동일(조상 확인 통과) | **검증됨 · origin 반영** |
| B 채팅 UI | `runner-fe81943c` | 채팅 카드/패널/신뢰 영역 승인 | aads-dashboard 커밋 `ad2fa252d71efe18ba16c17b20bd6244902db219` 가 **로컬 브랜치 `backup/runner-fe81943c-ad2fa25` 에만** 있다. `git merge-base --is-ancestor ad2fa25 origin/main` rc=1, 로컬 HEAD 도 rc=1, `git ls-tree origin/main` 에 `MockupReview*`·`mockupReviewApi` 없음 | **blocked — origin 미반영 (책임 선행: runner-fe81943c, push 미완)** |
| C 게이트·오비스 알림 | `runner-48e5aafb` | submit/worker gate, 오비스 내부 알림 | 두 저장소 모두 `git log --all --grep=runner-48e5aafb` 0건, 대응 워크트리 없음, 코드 소비자 없음 | **blocked — 산출물 없음 (책임 선행: runner-48e5aafb)** |

B 의 자체 보고서(백업 커밋 안의 `docs/reports/20261003_RDOC_MOCKUP_CHAT.md`)도 "**부분 완료, `page.tsx` 연결 보류**"라고 적었다. 즉 푸시되더라도 `/chat` 페이지에서 카드가 열리는 연결은 `/tmp/mr-page-tsx.patch` 적용이 남아 있다. "done" 문자열·보고서 커밋만으로는 기능 완료로 보지 않았다.

### 요구 항목별 증거

| 요구 | A(origin) | B | C | 상태 |
|---|---|---|---|---|
| mockup API | 구현·`ruff F821,F811` 통과·`compileall` rc=0 | 클라이언트만(백업) | — | A 만 충족 |
| migration | additive 5테이블, rollback 없음 | — | — | 파일 검증만, **운영 미적용** |
| 채팅 실제 연동 | — | 백업 브랜치, 미푸시, page.tsx 미연결 | — | **미충족** |
| submit/worker gate | `POST .../verify` 엔드포인트만 있음 | — | 러너 호출처 없음 | **미충족** |
| 오비스 내부 알림 | 없음 | 딥링크 생성 함수만(`/chat#<session>` 재사용) | 생산자 없음 | **미충족, 검증 불가** |
| 테스트 | 단위 50 통과 (§3) | 백업 보고서 상 vitest 55 통과(내가 재실행하지 않음, stub 기반) | — | A 만 확인 |

## 3. 이 러너가 실제 실행한 검증

| 명령 | 결과 |
|---|---|
| `bash scripts/run_unit_tests.sh tests/unit/test_mockup_reviews.py` | **50 passed, rc=0** |
| `ruff check --select F821,F811` (A 의 app 3개·테스트 2개) | All checks passed |
| `python3 -m compileall -q` (A app 3개) | rc=0 |
| `tests/integration/test_mockup_reviews_postgres.py` (15) | **실행하지 않음** — 이 호스트에 `psql`·`asyncpg`·스크래치 Postgres 없음. 통과로 적지 않음. A 보고서의 "15 통과"는 이전 러너의 주장이며 재현하지 않았다 |
| 운영 컨테이너 읽기 전용 점검 | `aads-server`·`aads-server-green` 둘 다 이미지 `aads-server:f20da86b056c`. 두 슬롯 모두 `/app/app/api/mockup_reviews.py`, `/app/migrations/20261003_mockup_reviews.sql` **없음** → A 는 운영에 배포되지 않았다 |
| `GET /api/v1/health` | `status: ok` (운영 f20da86b 기준). 모든 `/api/v1/*` 가 비인증 401 이라 라우트 존재 판별에는 쓸 수 없었다 |
| 브라우저 E2E (`/chat` desktop/mobile, 권한, 세션 복구) | **미수행(미완료)**. B 가 미푸시이고 로그인 세션이 없다. HTTP/API/process 폴백으로 확인한 것은 위 표뿐이며 **시각 E2E 는 완료가 아니다** |
| 오비스 알림 표시·revision 정확 클릭·외부 sender 호출 없음 | 외부 sender 는 A·B 소스 grep 으로 **코드에 없음** 확인. 알림 표시·클릭 이동은 생산자(C)가 없어 **증거 수집 불가** |

Vault/bridge: 이 세션에는 Vault·bridge 도구가 없어 재시도 대상이 없었다. 비밀값·로그인 토큰은 사용하지 않았다.

## 4. 승인 상태 보고 (원 세션용)

- 목업 검토 기능에 대한 **기존 운영 배포 승인 기록은 확인하지 못했다.** 저장소·HANDOVER 에 승인 근거가 없고, DB 승인 이력은 `psql`·인증 토큰 부재로 조회하지 못했다. "승인이 있다/없다"를 단정하지 않는다 — **미확인**.
- 현재 배포 대상 범위(§5)에는 목업과 무관한 5개 커밋과 migration 2개가 함께 묶여 있어, 목업 승인만으로는 이 SHA 를 배포할 수 있는지 판단할 수 없다.
- 다음 정상 경로: 원 세션 CEO 가 아래 §5 의 SHA·migration·rollback 을 보고 **운영 배포 승인**을 명시 → Runner 가 `deploy.sh bluegreen` 수행. 이 보고는 승인 요청서이지 승인이 아니다. 문서 승인·목업 승인·배포 승인은 별개이며 `approved_revision_id` 는 바꾸지 않았다.

## 5. 릴리스 준비안 (실행하지 않음 — B/C 충족 후에만 유효)

**선행 조건 (모두 충족 전까지 릴리스 금지)**
1. B(`ad2fa25`)를 검수·push 하고 대시보드 origin/main SHA 를 새로 기록. `page.tsx` 연결 포함 여부 확인.
2. C(`runner-48e5aafb` 대응)가 submit/worker gate 와 오비스 내부 알림 생산자를 구현·테스트·push.
3. 그때의 API `origin/main` SHA 를 **릴리스 SHA 1개**로 확정(아래는 현재 기준 참고값).

**현재 기준 참고값**
- 운영 실행 SHA: `f20da86b056c07ba3a1853db387cd28697ffcb2b` (두 슬롯).
- 현재 origin/main: `1d19322f27b3de994723c4d09048b042906859c3`. 둘 사이 **6커밋**: `3b8952ea`(보고서), `f0be2bc9`(마일스톤 owner API), `6877fa2d`(채팅 interrupt 수명주기), `4fc96878`(chat send payload 검증), `dffe625c`(세션 effort), `1d19322f`(mockup A). 즉 mockup 만 배포되는 것이 아니라 **채팅 핵심 경로 변경 3건이 같이 나간다.** 기능 단위로 쪼개 배포할 수 없으므로 승인 때 이 범위를 명시해야 한다.
- additive migration 3개: `20261003_mockup_reviews.sql`, `20261003_chat_session_effort.sql`(rollback 있음), `20261003_chat_interrupt_states.sql`(rollback 없음). **마이그레이션이 코드보다 먼저.**

**승인 후 실행 순서 (AGENTS.md 계약 11조항 그대로)**
1. clean committed release worktree 에서 릴리스 SHA 확인 → `bash /root/aads/aads-server/deploy.sh bluegreen`. 릴리스 SHA 당 이미지 1회 빌드, 슬롯은 `--no-build`, 병행 빌드 금지.
2. candidate 직접 health 통과 **후에만** `/tmp/aads-nginx-upstream.lock` 획득 → 라우팅 전환 → 즉시 routed health → 락 해제. 빌드·drain·standby 동기화·QA 중에는 락을 잡지 않는다.
3. 채팅 실행은 DB `owner_instance`+`owner_epoch` lease 보유자만 변경(epoch fencing). 비활성 슬롯은 복구·자동 반응을 시작하지 않는다.
4. 이전 active 슬롯은 stream drain 후에만 재시작하고, **같은 digest** 이미지로 standby 동기화(재빌드 금지). 두 슬롯 digest 일치 확인.
5. routed health 실패 시 **즉시 routing rollback**.
6. QA(채팅 SSE, `mockup-reviews` 권한 401/403/200, 알림 표시·클릭, 세션 복구) 후 **5분 P0/P1 관찰**, 신규 오류 0 이어야 "release certified". 그 전에는 "cutover complete"로만 표기.

**rollback**
- 코드: 이전 슬롯(`f20da86b` 이미지)로 routing rollback — 슬롯이 drain 되기 전까지 유지.
- DB: 세 migration 모두 additive 이므로 **테이블은 유지**한다. mockup 테이블은 revisions/events 불변 트리거 + heads/bindings 삭제 차단이 있어 감사 기록 삭제가 설계상 막혀 있다. `mockup_reviews` 와 `chat_interrupt_states` 는 **down SQL 이 없다** — 필요하면 별도 작업으로 먼저 작성·승인해야 하며, 이 작업에서는 만들지 않았다.

## 6. 위험·미해결

1. **B 가 로컬 백업 브랜치에만 있다** — 정리·재생성 시 유실 가능. push 가 최우선.
2. mockup 연쇄가 이전에도 여러 번 끊겼다(runner-7ba13423 차단, 이번 C 부재). "done" 문자열을 신뢰한 연쇄 진행은 위험하다는 사례.
3. 통합 테스트 15개는 이 호스트에서 재현 불가. 릴리스 전 Postgres 가 있는 환경(Runner)에서 실행 필요.
4. 운영 DB·Vault·`handover_write` 접근 불가로 승인 이력·`goal_milestones` 상태는 조회하지 못했다.

## 7. 남은 일과 책임

| 항목 | 책임 | 상태 |
|---|---|---|
| B push (`ad2fa25`, page.tsx 연결 포함) | runner-fe81943c 후속 | blocked |
| C 구현 (gate + 오비스 알림) | runner-48e5aafb 후속 | blocked, 산출물 없음 |
| 통합 테스트 15 재실행 | Runner | 미실행 |
| 릴리스 승인 | 원 세션 CEO | 미확인 |
| M9 | — | **completed 로 바꾸지 않음** (release certified 필요) |

## 8. 열람 링크 (검증됨)

| 대상 | 링크 | 검증 |
|---|---|---|
| A 커밋 (origin/main) | <https://github.com/moongoby-GO100/aads-server/commit/1d19322f27b3de994723c4d09048b042906859c3> | `git fetch` 후 origin/main 과 일치 |
| A 백엔드 보고서 | <https://github.com/moongoby-GO100/aads-server/blob/1d19322f27b3de994723c4d09048b042906859c3/docs/reports/20261003_RDOC_MOCKUP_BACKEND.md> | 해당 SHA 에 파일 존재 확인 |
| 수정 보고서 | <https://github.com/moongoby-GO100/aads-server/blob/1d19322f27b3de994723c4d09048b042906859c3/docs/reports/20261003_RDOC_MOCKUP_CHANGE_REPORT.md> | 동일 |
| 원 채팅 세션 | <https://aads.newtalk.kr/chat#8bf0405a-1f22-4ad9-bb09-6e0fce8c6339> | 기존 보고서의 링크. 로그인 필요, 브라우저로 열어 확인하지 못함 |
| B 커밋 | **제시하지 않음** | origin 에 없는 SHA 는 GitHub 에서 열리지 않는다 |
| 오비스 알림 → 정확한 revision 문서 | **제시 불가** | C 부재 |

## 9. 비용

외부 LLM 호출·유료 API 호출 없음. 이 러너 세션의 토큰 비용 **$ 미측정**.
