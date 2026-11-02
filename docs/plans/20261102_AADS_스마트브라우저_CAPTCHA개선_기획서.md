# 스마트브라우저 캡챠 개선 기획서

- document_key: smart-browser-captcha-plan
- 상태: 초안 / 작성일: 2026-11-02

## 제품 방향과 사용자 여정
주 사용자는 사이트에 로그인하고 반복업무를 수행하는 사람이다. 첫 실행은 업무 요청 → 지정 계정·사이트·행위 범위 확인 → 필요한 인증 → 바로 같은 업무 계속으로 이어진다. 반복 실행은 기존 인증을 실제 사이트에서 검증하고 재사용한다. 실패하면 새 업무를 만들지 않고 동일 work_key로 인증을 재개한다. 관리자 설정은 보조 경로다.

PC와 모바일 모두 현재 상태·마지막 오류·다음 행동·증거·취소/재개를 같은 업무 문맥에 제공한다. 사람 인증은 해당 작업에 바인딩된 서버/조건부 PC 브라우저에서 수행하며 모바일에 비밀번호나 OTP를 전송하지 않는다. 모바일에서 직접 조작할 수 없으면 해당 바인딩 브라우저로 이동하는 안내와 대기 상태를 제공한다.

## 단계와 의존성
C0 문서·계약·업무추적 → C1 관측 보안·물리적 바인딩 → C2 공통 처리·검증 및 C3 사용자 직접인증·복구 → C4 땡겨요 실제 인수 순서다. 모든 단계 auto_advance=false. C4는 C2/C3의 실제 검증을 기다린다. 기존 B1 runner-964c89b4 awaiting_approval은 완료·운영 적용으로 간주하지 않는다.

## 기존 목표 보강
제공된 M1~M13 실제 ID·원래 criteria·상태·담당·증거를 manifest existing_milestones.snapshots에 그대로 보존했다. 각각 epic/story/task 최소1개, 총13/13/13개를 legacy_work_items로 계획했다. M1/M6 completed에는 근거 재검증 task를 planned로 추가하고 기존 완료를 새로 주장하지 않는다. 모든 등록은 원세션 현재 상태 재조회 후 별도 수행한다. M8 원문에 남은 HANDOVER 파일 언급은 보존 자료이며 실제 이행은 R-001의 DB 핸드오버만 따른다.

## 측정과 수용
로그인 성공/시도, 인증 재사용/적격 반복, 사용자 직접 해결 전환 사유, 재개 성공/적격 재개, 중복 제출, 처리 지연 분포, 실제 비용을 분모·환경·시간·증거와 함께 기록한다. 목표 수치는 기준선 측정 후 검토한다. C4 로그인과 레시피 등록은 실제 증거가 나올 때만 완료로 보고한다.

## 기준과 출처
목표 ID `1a463229-5bbe-4c7e-b020-71b157acc241`, title=스마트브라우져 진화 및 최적화, status=blocked, owner_session_id `7fb5f50a-9fe7-4a29-9333-9c023125aa6e`, tenant_id `2d701a8c-9596-4757-8588-faa4f7837112` 보존. 작성 출처 source_task_id=runner-6fbe30cd. 정본 미등록(미완료), 열람 링크 미제공.

- `smart-browser-work-engine-contract`: revision 2, ID `4d1db0d0-313b-4417-a00b-c7dc1a810e19`, SHA256 `30d7f8250975d90cba68bf685dbfa985f4c5e6503cb56c9aeec68c492b264aae`, generation=2, status=draft, approved=false.
- `smart-browser-work-engine-design`: revision 2, ID `2b7ffa5f-c050-4f11-b5b5-705dd52466b2`, SHA256 `34629dfa3bd0cfa6f25913b03b67191d2c9e4c1521ff66613c5e002431d62c75`, generation=4, status=review, approved=false.
- `smart-browser-work-engine-prd`: revision 2, ID `3487a522-51d5-40fe-abf3-420cdce5c4f4`, SHA256 `2ad915051e20180f97d069a9303d053b468b66842e4c37def866c9a383bc81c6`, generation=4, status=review, approved=false.
- `smart-browser-work-engine-plan`: revision 2, ID `9fb95a2b-91d0-41aa-baba-fa1e1c4e1a3c`, SHA256 `7ddf4e84f7af024b0f110bf14f364f178f8692738d3082dafa9248e132ddbc30`, generation=4, status=review, approved=false.

원세션 제공 query_database 스냅샷 4종 원문 UTF-8 SHA256를 모두 재검증했다. 조회 당시 최신 rev2이며 현재 운영 조회/승인 증거가 아니다. contract 본문의 rev1 미러 역사·None 계약 참조는 역사로만 남기며 끝부분 PM-C1~C3 rev2 후속 보완을 현재 설계 입력으로 사용한다. 원작업의 rev1/None 참조는 manifest historical_source_references에만 보존한다. v4 핸드오버 smart-browser-v4-review-20261102 revision1 원문도 제공본에서 읽었다. 무중단 세션이관/1초 성능/실제PG 완료는 증거 없음이다.

## 요구사항에서 업무·시험까지
| 요구사항 | 내용 | 마일스톤 | 인수 시험 ID |
|---|---|---|---|
| R01 | 첫 로그인 후 같은 업무 계속·반복 인증 재사용 | C3 | T-HUMAN,T-RESUME,T-REUSE |
| R02 | 관측 및 승인과 물리적 화면 바인딩 | C1 | T-SCOPE,T-BIND,T-STALE |
| R03 | 승인된 단순 CAPTCHA 공통 처리 | C2 | T-APPROVAL,T-ACTION |
| R04 | 판독과 사이트 로그인 후조건 구분 | C2 | T-VERIFY,T-LOGIN |
| R05 | PC/모바일 안내·취소·복구·중복 방지 | C3 | T-UX,T-IDEM |
| R06 | 중앙 LLM·마스킹·비밀정보 비반환 | C2 | T-MASK,T-LLM |
| R07 | 땡겨요 로그인·분할 레시피·다른 세션 재사용 | C4 | T-SITE,T-LOGIN,T-RECIPE,T-REUSE |
| R08 | 정본·업무계층·배치 출처 추적 | C0 | T-DOC,T-REG,T-TREE,T-LINK |

상세 epic/story/task와 각 test_id·담당·파일·선행·completion_criteria·acceptance_criteria(JSON 배열)·고정 idempotency_key는 `docs/specs/smart-browser-captcha/업무계층_등록계획.json`에 있다. C0~C4 각 1 epic/2 story/4 task, 총 5/10/20개를 계획한다. 참조 parent_ref는 서버 UUID가 아니며 등록 응답을 재조회한 실제 ID로 치환해야 한다. 모든 실제 시험은 미실행이다. 정적 검증 통과가 제품 시험 통과는 아니다.

## 승인·안전·완료 경계
단순 CAPTCHA의 정식 화면 조작만 승인 범위 내 지원하는 설계다. 토큰 위조, anti-bot stealth, Access Denied 우회, 프록시 회전은 금지한다. OTP/ARS/보안프로그램·지원 불가 이미지격자는 사용자 직접 해결 후 동일 work_key로 재개한다. click/drag capability 미지원이면 unsupported/waiting_auth로 알리고 구현 완료라고 쓰지 않는다.

판독 recognized와 site_verified는 별개다. CAPTCHA 해제 + 실제 업무화면 + 지정 계정 일치 + 오류/Access Denied 부재를 모두 확인해야 로그인 성공이다. 숫자 판독만으로 captcha_solved 기록 금지. digits/쿠키/비밀번호/토큰 원문은 반환·저장·로그 금지. 증거는 마스킹된 artifact_ref만 사용한다.

작업당 30분 상한, 비용 미측정은 null이며 0달러가 아니다. $5 초과 예상/계측 시 중간보고 후 중단한다. 성공률·1초 무중단·100% 무결점을 실측 없이 보장하지 않는다. 문서 승인·운영 설정 변경·레시피 강제승인은 이 작업 범위에 없다.

## 서버 우선과 실행 바인딩
일반 사이트는 서버 Playwright가 기본이다. PC는 기존 로컬 로그인 세션·인증서·보안프로그램 필요성이 실제 증거로 확인된 경우에만 선택한다. 서버 제한만으로 PC 로그인 성공이나 PC 필수로 승격하지 않는다. 같은 work_key의 tenant/project/account_ref/lane/page/frame/navigation/device/owner_epoch를 바인딩하고 실행 중 레인 자동변경을 금지한다. 새 장치는 재인증한다. 레인 변경 필요시 기존 실행을 멈추고 명시 승인·재인증·새 관측으로 같은 업무를 재개한다.

서버 E2E와 PC E2E는 각각 환경·run·마스킹 screenshot/artifact·성공 또는 not_run 사유를 기록한다. 조건부 PC 미사용은 PC 성공이 아니다. 현재 두 레인 모두 not_run이다. 실제 사이트 로그인 후 계정일치 + 업무화면 + CAPTCHA/오류/AccessDenied 부재가 모두 필수다. HTTP200은 로그인 성공이 아니며 서버/PC E2E 실패를 HTTP/API/process fallback 성공으로 대체하지 못한다. 정상 인증 유지가 확인되면 제출을 반복하지 않는다. OTP/지원불가는 사람이 해결한 뒤 같은 work_key와 체크포인트로 재개한다.

## 로그인·목적별 레시피 조각 계약
먼저 smart_browser list로 기존 승인본을 탐색하여 정확한 name/version/hash·tenant/project/계정·origin을 비교한다. 재사용 가능하면 새 레시피를 중복 생성하지 않는다. 조각은 (a)로그인 화면 진입 (b)Vault 자격입력 (c)제출·성공확인 (d)조회 (e)다운로드 (f)집계로 분리한다. manifest recipe_contract.pieces의 각 입력/행동/시작·성공/검증/실패복귀를 따른다.

각 조각은 exact origin/domain(승인 profile 조회 전 null·실행금지), lane/work_key/account/page 결속, starts_when/succeeds_when, verify assertion, 선행 recipe name/version/hash, 마스킹 screenshot_ref/artifact_ref/run_ref, failure_return, credential_scope와 secret 변수 참조를 갖춘다. null은 등록 완료 증거가 아니라 실행 전 해소할 값이다. 비밀번호는 filled/empty만 검사하며 값·길이·text 판독 금지. verify_assertion은 기존 GenericVerificationExecutor가 지원하는 assertion/locator로 매핑한 뒤 검증하며 미지원이면 등록 중단한다.

진입은 exact login_url과 로그인폼·오류부재, Vault는 scope/ID 일치와 filled/empty, 제출은 인증미유지일 때 승인된 1회 제출 후 계정/업무/오류부재를 검증한다. 조회는 계정·매장·기간과 결과/검증된 빈결과, 다운로드는 완료파일·행수·content hash, 집계는 타입·Decimal·통화·시각 보존과 원본 행수/합계/가능한 잔액을 대조한다. 실패는 마지막 승인 조각으로 복귀하고 ACK불명은 읽기 대사한다. 인증유지 시 submit 조각을 건너뛰되 기존 인증의 계정/업무 후조건과 checkpoint를 검증한다.

계획 planned → 실제 E2E 성공 e2e_verified → 정상 register_e2e 등록대기 registration_pending → 별도 사람 승인 approved → 다른 허용 세션 실제 재생 replayed_verified를 구별한다. 사이트 오류/AccessDenied/계정불일치/fallback만 성공한 실행은 성공 register_e2e로 올리지 않는다. name/version/hash와 run·화면 증거는 실제 응답으로 채우며 강제승인하지 않는다. T-SITE/T-LOGIN/T-RECIPE/T-REUSE의 상세 입력·행동·기대·증거·복구는 manifest test_contracts에 있고 현재 모두 not_run이다.

## 상위 공통 원장과 PM 후속 계약
공통 실행 원장 sb_executions/sb_step_journal과 owner_instance/owner_epoch/lease fencing을 재사용한다. 새 CAPTCHA 원장/API/schema를 만들지 않는다. PM-C1: collection run/schedule·혼합 recipe dataset write는 dataset 소유 project만, read 공유나 일반 execute grant로 확대하지 않는다. 인증 tenant/project/principal을 cache보다 먼저 검증하고 같은 tenant 무권한403/다른 tenant404, dispatch·공개commit 직전 모든 dataset 재검사, 빈 allowlist 거부다.

PM-C2: 기존 money/number/url 명시 타입·값·복합 record key 보존, 자동 dataset key 재작성 금지. 기존 records 2000/2001·gap 계약 보존; 새 changes/snapshot만 기본500/상한1000, 1001 거부·만료410 cursor_expired·교차 endpoint cursor 거부다. 목표계약과 현재 구현 차이를 완료로 위장하지 않는다.

PM-C3: 24h 응답 cache와 최소 비민감 scope/principal/key/request digest·effect state·안전참조 tombstone을 분리한다. 초기 tombstone 자동삭제 없음, receipt/artifact/result GC 연쇄삭제 금지. 현재 권한 → hash충돌409 idempotency_conflict → unknown409 needs_reconciliation → 보존결과 기존 ticket → 확정결과 소실410 idempotency_result_expired 순서다. 동일 key는 execution/outbox/command/dispatch 증가0이어야 하며 다른 key의 의미중복 완전차단은 주장하지 않는다. PM-C1-T1、PM-C1-T2、PM-C2-T1、PM-C2-T2、PM-C3-T1、PM-C3-T2 여섯 시험은 manifest pm_tests에 별도 추적, 실제PG/GC 시험 not_run이다.

상위의 CAPTCHA 자동대행 제외는 유지한다. 최신 CEO 지시의 제한된 단순 화면조작(click/drag/readText)만 승인·계정·origin·레인·관측 바인딩과 후조건을 충족할 때 확장 범위다. OTP/ARS/지원불가·토큰위조·stealth·AccessDenied 우회는 포함하지 않는다. 문서 구현계획·코드 구현·교차검수·사람승인·운영 적용은 각각 별도 판정이다.
