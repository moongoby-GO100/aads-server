# OVIS Smart Browser — 실행·학습 보강 설계 v1.1

기준: 2026-09-19 KST. 대상 목표: `169e5328-244e-444d-95c8-d20377192671`.
상태: 승인된 구현 요구사항. 이 문서는 구현·배포 완료 증거가 아니다.
기존 `AADS-SMART-BROWSER-PC-APP-PLAN-20260919.md`의 후속 보강 문서이며, 이전 현황 수치·예상 일정은 현재 운영 사실로 재사용하지 않는다. Mac은 보류한다. Windows 전용 업무만 PC 경로를 사용한다.

## 1. 사용자 흐름과 실행 순서

대상 사용자는 채팅에서 웹 검색·사내 업무·Windows 네이티브 업무를 수행하는 사용자다. 첫 화면은 채팅과 우측 브라우저 아티팩트이며 설치·연결·관리 설정은 보조 경로다.

`인증된 DirectiveEnvelope / 비신뢰 ObservationEnvelope → Channel Router(명령·관측 분리) → 실행 경로 선택 → 경로에 맞는 Exact/Vector/LLM 후보 검색 → 정책·스킬 계약 검증 → 실행 → 결과 검증 → 표시 직전 사실 재검증 → 화면 표시`

Channel Router의 우선 책임은 명령·관측 채널 분리다. 인증된 user_directive/approved_recipe/internal_control만 ActionIntent를 생성한다. source 문자열은 클라이언트 주장을 신뢰하지 않고 인증 컨텍스트로 확정한다. 페이지·DOM·ARIA·OCR·파일·RAG는 ObservationEnvelope로만 들어온다. 이후 Execution Router가 API/Browser/Windows PC/Human을 선택하며 각 단계에서 권한·origin·계정·위험도를 다시 검증한다. 입력 출처와 실행 경로는 별도 책임이다.

첫 방문은 허용 경로 선택, 현재 상태 안내, 검증 가능한 스킬 초안 생성까지다. 재방문은 버전·부분 구조를 대조하고 검증된 스킬을 재사용하되 가격·재고는 다시 조회한다. 로그인 만료는 Vault 또는 사용자 인증, DOM 변경은 재학습, PC 부재는 연결 안내, 최신성 실패는 재조회 버튼으로 복구한다. 조용히 다른 PC로 전환하거나 완료로 표시하지 않는다.

## 2. 여섯 가지 설계 계약

| 요구 | 구현 계약 | 거부·복구 | 검증 기준 |
|---|---|---|---|
| Channel Router 선행 | 인증된 사용자 의도, tenant, 사이트 정책, 실제 가용 능력으로 API/서버 Browser/Windows PC/Human을 결정한다. 허용된 공식 API가 업무를 충족하면 API, 일반 DOM은 서버 Playwright, 네이티브 창은 승인된 PC, OTP·인증·고위험 확정은 Human이다. route·reason·policy_version을 저장한다. 검색 이전 및 단계 전환 시 재검증한다. | 사용자 의도 불명확·권한 부족은 안내 또는 차단. 서버 오류·401만으로 PC 권한을 자동 부여하지 않는다. CAPTCHA·보안프로그램 우회 금지. | 허용 API·일반 DOM·네이티브·OTP·PC 부재·권한 부족·401 각각의 결정과 검색 이전 호출 순서 검증. |
| 페이지는 명령이 아닌 데이터 | 사용자 지시/control과 DOM·ARIA·OCR·검색·RAG/data를 타입·출처로 분리한다. LLM은 제한된 후보만 제안하고 서버가 allowlist, 스키마, origin, 권한을 재검증한다. 페이지가 tool 이름·코드·권한·시스템 프롬프트를 바꿀 수 없다. | 정규식 제거·untrusted 태그는 보조 수단이다. 스키마 탈출·도메인 이탈·승인 위조는 실행 전 차단한다. 정상 검색어·상품명은 제한된 data 인자로 허용한다. | DOM/ARIA/OCR/RAG 각각의 간접 주입, 태그 탈출, 도구 인자 오염, tenant 교차 및 정상 인자 회귀. 비밀번호·쿠키·OTP는 모델/벡터/로그에 없음. |
| 실행 가능한 Site Skill | 기존 Site Skill 정본을 skill_id+version의 서버 allowlist API/함수 참조로 관리한다. input/output JSON schema, capability, origin, permission, risk, timeout, retry, idempotency, pre/post-condition, evidence 계약을 갖는다. Recipe는 이 참조를 조합한다. | 자연어 설명만 있는 skill 실행 금지. DB 문자열 eval/exec·임의 import 금지. 중복 실행 위험 작업은 무조건 재시도하지 않는다. | 실제 등록 함수 호출, 잘못된 인자·미등록 함수·권한·버전 거부, 중복 요청 및 tenant 격리 검증. |
| ARIA 기반 부분 구조 시그니처 | 전체 DOM 해시 대신 업무 영역 landmark, role, 안정된 accessible name, 상호관계, 필수 입력·결과 영역을 정규화한다. selector 후보는 role/name/label 우선이다. | 가격·시각·광고·랜덤 ID·개인정보는 구조 시그니처에서 제외한다. ARIA 부재·중복 이름은 신뢰도 저하 후 DOM 대안 또는 재학습. 자동 성공 간주 금지. | 광고·가격 변경 시 재사용, 필수 검색창 삭제·role 변경 시 invalidation, ARIA 없는 페이지와 동명이인 요소 처리. |
| 표시 직전 사실 재검증 | 가격·재고·배송·판매자는 observation별 source, fetched_at, expires_at, evidence, entity/variant/account context를 가진다. 실행 완료와 표시 사이에도 변할 수 있으므로 최종 응답/아티팩트 내보내기 단계에서 다시 검사·필요시 조회한다. | 만료·불일치·조회 실패면 과거 값을 현재 사실로 표시하지 않는다. 미확인/조회 시각과 재시도 동작을 제시하고 권고 생성도 보류한다. SSE·세션 재연결·캐시 재생에도 같은 게이트 적용. | 수집 후 가격 변경, TTL 만료, 다른 옵션·계정, 전송 지연, 새로고침 실패, 재연결 후 오래된 프레임 차단. |
| 골든 태스크·회귀 기반 승격 | draft→candidate→shadow→active. 버전별 고정 입력·예상 결과·증거·환경을 가진 골든 태스크와 기존 회귀를 통과한 산출물만 승격한다. active 버전은 불변이며 previous_active 참조를 보존한다. | 단일 성공·LLM 자기평가·테스트 미실행은 승격 불가. 보안/권한/비밀/고위험/최신성 필수 케이스 하나라도 실패하면 차단. 테스트 인프라 오류는 보류다. | 허용된 데모 사이트에서 첫 학습·재방문·DOM 변경·주입·인증 만료·PC 없음·stale fact 시나리오 검증. 이전 버전 회귀 없음과 원자적 승격·롤백 확인. |

## 3. 마일스톤과 우선순위

기존 M1/M2 완료 판정은 유지한다. M3 검수 후 아래 순서로 진행하며 기존 작업을 중복 제출하지 않는다.

| 순서 | 마일스톤 | 우선순위 | 적용 방식 |
|---|---|---|---|
| 1 | G1 Channel Router | P0 | 명령/관측 출처 검증을 모든 실행·검색보다 선행 |
| 2 | G2 페이지 데이터 신뢰 경계 | P0 | G1 후; 복구·학습·검색 전 정책 경계 |
| 3 | G3 실행 가능한 Skill Registry | P0 | 계약·실행기 구현, M7에서 사이트 정본과 통합 |
| 4 | G4 ARIA 부분 구조 | P1 | 시그니처 계약 구현, M8에서 학습·재방문 결선 |
| 5 | G5 표시 직전 최신성 | P0 | 최종 표시 게이트 구현, M10/M11에 결선 |
| 6 | G6 골든 태스크 승격 | P0 | candidate→shadow→active 및 이전 버전 보존 |
| 7 | M4~M11 기존 구현 | 기존 유지 | G1~G6 계약 재사용, 실제 학습·재방문·UI·E2E·배포 연결 |

G1~G6는 등록된 정본 마일스톤이다. 실행 순서는 M3 검수 → G1~G6 → M4~M11이며 sequence_order와 Runner depends_on으로 강제한다. 공용 모듈 테스트만으로 실제 사용자 경로 결선 완료를 선언하지 않는다. 승인 대기·리뷰 인프라 실패는 통과로 바꾸지 않는다.

## 4. 검증·출시·롤백

단위 테스트는 정책 분기·격리·부정 사례를 검증하고, 통합 테스트는 실제 실행 경로가 새 정책과 스킬 registry를 통과하는지 확인한다. 골든 태스크는 허용된 읽기 전용 데모 사이트에서 실행하며 실제 구매·송금·본인확인을 자동 실행하지 않는다. 테스트 수·성공률·속도·토큰·비용은 실측 후 기록한다.

화면 검증은 채팅 오른쪽 브라우저 탭의 첫 방문·재방문·실패 복구를 캡처한다. 브라우저 불가 시 HTTP/API/프로세스 폴백 결과와 화면 미검증을 구분한다. Mac은 구현·테스트·배포 대상에서 제외한다.

구현 커밋은 정상 훅과 독립 검수를 거쳐 반영한다. 운영 배포는 기존 M11 릴리스에서 통합하고, clean release SHA 단일 빌드, candidate health, 짧은 nginx lock, DB owner fencing, 동일 digest standby, routed-health 실패 시 rollback, 5분 P0/P1 감시를 모두 만족해야 완료다.

마일스톤·큐 수정은 트랜잭션으로 수행하며 변경 전 row를 별도 운영 기록에 보관한다. 이후 작업이 실행됐다면 단순 되돌리기 대신 최신 상태를 대조한다. 스킬 rollback은 previous_active로 원자 전환하고 신규 학습 이력은 삭제하지 않는다. DB는 additive migration으로만 확장한다.
