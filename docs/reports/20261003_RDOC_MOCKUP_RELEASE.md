# R-DOC 목업 검토 기능 — 운영 큐 배포·채팅 실사용 검증 (D) — 선행 미충족으로 차단

- 작업: `AADS-RDOC-MOCKUP-OPERATIONS-20261003` · P1 · SIZE S · Runner `runner-7ba13423` · 2026-10-03 KST
- 마일스톤: `3350deae-96eb-5548-a99c-adf68d984bfb` · 상위 목표 `0361c451-cc03-4bd1-a423-76051b0546b2` · 원 세션 `8bf0405a-1f22-4ad9-bb09-6e0fce8c6339`
- 기준 SHA: `f20da86b056c07ba3a1853db387cd28697ffcb2b` (= 조회 시점 origin/main, fetch 직후 확인)
- **결론: 이 작업은 완료가 아니다.** 배포할 A/B/C 산출물이 origin/main 에 없다. 큐 등록·deploy_run_id·두 repo SHA·5분 관찰·채팅 실사용 검증은 **하나도 수행하지 않았고** 통과로 적지 않는다. 승인 상태·`approved_revision_id`·알림 설정·다른 프로젝트 설정은 건드리지 않았다.

## 1. STEP 0 — 기존 구현 조사와 분류

| 대상 | 현재 상태(실측) | 분류 |
|---|---|---|
| origin/main 의 목업 검토 API/서비스/모델 (`app/api/mockup_reviews.py` 등) | **없음** (`git grep mockup origin/main -- app migrations` 는 obys 정적 목업 HTML 만 매칭) | 신규(타 작업 소관) — 이 작업에서 만들지 않음 |
| 마이그레이션 `migrations/20261003_mockup_reviews.sql` | origin/main 에 없음. **운영 DB 에 `mockup%` 테이블 0개** (information_schema 읽기 전용 조회) | 신규(타 작업 소관) |
| A(백엔드, runner-2f93e68d) | 워크트리 `/tmp/aads-wt-runner-2f93e68d` 에 **스테이징만 된 미커밋 상태**(app/api·models·services, 마이그레이션, 단위 50·통합 15 테스트, 보고서 2개, `app/main.py` 수정). HEAD 는 f20da86b 그대로 | 유지(손대지 않음) |
| B(채팅 UI, 대시보드) | `/root/aads/aads-dashboard` HEAD `6d0cfb6`, `git grep -i "mockup\|change_request" -- src` 에 해당 기능 없음. 어느 `/tmp/aads-wt-*` 에도 목업 검토 관련 변경 없음 | 없음 |
| C(게이트·오비스 알림 연결) | 어느 워크트리에도 해당 변경 없음. A 의 서비스·라우터에서 `telegram/email/sms/slack/notif/알림` 검색 결과 0건 → 외부 sender 는 없지만 **오비스 내부 알림 연결도 없다** | 없음 |
| 기존 오비스 내부 알림 경로 | `app/api/notifications.py`(웹푸시 구독·테스트), `app/services/push_notifications.py`(`notify_chat_response_complete` 등, best-effort). 목업 검토 이벤트 연결 없음 | 유지 — 재사용 후보 |
| 배포 큐 | `POST /ops/deploy/requests`(app/api/ops.py:649) → `enqueue_deploy_request`, 상태 `/ops/deploy/status`. 최근 `deploy_runs` #5573 success(생성 14:05 KST). 이번 건으로 만든 run 없음 | 유지 — 사용하지 않음 |
| `docs/reports/20261003_RDOC_MOCKUP_RELEASE.md`, `HANDOVER.md` | 이 작업의 TARGET_FILES | 신규 / 수정(HANDOVER 에 항목 1개 추가) |
| 삭제 | 없음 | 해당 없음 |

지시서에 없는 파일 변경 없음. 기존 v1/v2 산출물과 공용 dirty 상태 보존.

## 2. 차단 사유와 판정

지시서 조건: "검수된 A/B/C push 및 migration/backward compatibility·승인 gate scope 검증 후 실행". 확인 결과:

1. A 는 코드 완료이나 **미커밋·미푸시·미검수**. 운영 DB 에도 적용 전. 큐에 등록하면 존재하지 않는 SHA 를 배포하거나 A 없는 SHA 를 "기능 배포"로 오인시킨다.
2. B(채팅 카드/패널/신뢰 영역 승인)와 C(게이트 + 오비스 알림 연결)는 산출물이 없다. 채팅 실사용 검증(열기→수정→새 미승인 버전→재승인→저장/재접속→모바일 캡처)의 대상 기능이 존재하지 않는다.
3. CEO_OHVIS_ONLY 합격 조건("실제 테스트 이벤트의 오비스 알림 표시, 정확한 목업/수정문서 열기, 중복 방지, 외부 sender 호출 없음")은 C 연결이 없어 **검증 불가 = 미달**. 지시서 규정에 따라 미달 시 완료/릴리스 인증 금지.
4. 이 러너는 commit/push/docker/배포 명령이 금지되어 있어, 선행이 충족되어도 큐 등록과 5분 관찰은 승인 후 Runner 단계에서만 가능하다.

`deploy_run_id`: **없음**. 두 repo 배포 SHA: **없음**(API 기준 f20da86b·dashboard 6d0cfb6 는 이번 기능을 포함하지 않는 현재 HEAD 이며 배포 대상 SHA 가 아니다).

## 3. 합격 조건 점검표 (모두 미충족, 완료로 표시하지 않음)

| 항목 | 상태 | 근거 |
|---|---|---|
| A/B/C 가 origin/main 에 push 됨 | 미충족 | origin/main == f20da86b, A 는 타 워크트리 미커밋 |
| migration 적용·backward compatibility 확인 | 미충족 | 운영 DB 에 mockup 테이블 없음. 파일은 `CREATE TABLE IF NOT EXISTS` 5개로 additive 로 보이나(읽기만 함) 적용·롤백 리허설 안 함 |
| 승인 gate scope 검증 | 미충족 | 게이트(C) 없음. canonical_gate 는 shadow/fail-open 그대로 |
| 오비스 알림만 사용·외부 채널 없음 | **검증 불가** | A 코드에 외부 sender 없음은 확인했으나, 알림 연결 자체가 없음 |
| 알림 클릭 시 정확한 목업/수정문서 열기·읽음·재접속·중복 억제 | 미충족 | B/C 없음 |
| 큐 등록(deploy_run_id, 상태조회 경로, 두 repo SHA) | 미수행 | 위 1~2 |
| 5분 P0/P1 관찰 | 미수행 | 배포 없음 |
| /chat 원세션 실사용·모바일 캡처 | 미수행 | 기능 없음, 로그인 세션·비밀값 미사용 |
| M6~M9 | **미달성** | 증거 없음 |

## 4. 재개 절차 (선행이 채워진 뒤 D 를 다시 실행)

1. A(runner-2f93e68d)·B·C 가 검수 후 commit/push 되고 origin/main 에 포함되었는지 `git log origin/main` 으로 확인. C 는 `grep -rn -i "telegram\|smtp\|slack\|sendgrid\|twilio"` 로 외부 sender 가 새로 생기지 않았는지, 알림이 기존 `push_notifications`/오비스 알림 경로를 쓰는지 대조.
2. **대상**: API(aads-server) bluegreen + dashboard `deploy.sh`. **영향**: 신규 테이블 5개(additive)·신규 라우터(`/api/v1/projects/{project}/mockup-reviews`)·채팅 카드/패널. 기존 `canonical_documents` 계약과 `approved_revision_id` 불변. **rollback**: 라우터 제거 SHA 로 routing rollback(DB 는 additive 이므로 테이블 유지, 감사 기록 삭제 금지), 게이트 mode 는 이전 값 복구 + UI 영향 작업 보류.
3. 마이그레이션은 코드보다 먼저, 큐 등록은 `POST /ops/deploy/requests` 로 동일 SHA 1회 빌드. 다른 릴리스와 lock 이 충돌하면 순차화. 큐 등록 후에는 **등록 상태만 보고**하고 5분 관찰이 끝나기 전에는 완료로 쓰지 않는다.
4. 실사용 검증은 권한 있는 테스트 fixture 승인과 실제 사용자 승인 경로를 분리하고, 운영 사용자 명의의 임의 승인은 만들지 않는다. 시각 증거가 불가하면 HTTP/API/process fallback 과 시각 미완료를 분리해 적는다.
5. 보고에는 GitHub 브라우저 커밋 링크와 목업·수정문서 직접 링크를 넣는다(현재는 커밋이 없어 제시 불가).

## 5. 실행한 검증 (실측)

- `git fetch origin` 후 `git rev-parse HEAD origin/main` → 둘 다 `f20da86b…`.
- `git grep -i mockup origin/main -- app migrations` → obys 정적 목업뿐, 목업 검토 기능 없음.
- 읽기 전용 DB 조회: `information_schema.tables like 'mockup%'` → 0행. `deploy_runs` 최신 #5573 success (이번 작업과 무관).
- 형제 워크트리 `git status` 조사: 목업 검토 변경은 runner-2f93e68d 에만 존재(스테이징·미커밋).
- 코드 테스트·빌드·배포는 실행하지 않았다(변경한 코드 없음). 비용: 외부 LLM 호출 없음, 별도 측정 비용 없음.

## 6. 남은 일

A/B/C 완료·푸시 후 D 재실행. 이 보고서는 승인·구현 명령이 아니며 canonical `approved_revision_id` 를 변경하지 않았다. 문서 승인·목업 승인·배포 승인은 별개 범위다.
