# OVIS Smart Browser PRD 보강: 채널·신뢰 경계·스킬 승격

버전: 1.1 추가 설계 / 2026-09-19 KST
목표: 169e5328-244e-444d-95c8-d20377192671
기존 문서: [실행 아키텍처 기획서](AADS-SMART-BROWSER-PC-APP-PLAN-20260919.md)
이 문서는 설계 요구사항이며 구현·운영 검증 완료를 뜻하지 않는다. 기존 문서의 Mac 출시·PC Agent 완전 대체·ANTHROPIC 토큰을 앱 로그인에 사용하는 제안은 채택하지 않는다. Windows만 범위에 포함하고 AADS 사용자 인증과 Vault를 사용한다.

## 검토 결론과 실제 근거

요청한 여섯 항목을 모두 채택한다. 중복 시스템을 만들지 않고 기존 work_recipe 실행기, browser task 정책, site profile을 확장한다.
`app/services/work_recipe/orchestrator.py`는 이름/도메인 매칭 후 BrowserRecipeExecutor를 직접 만든다. `executor.py`는 page_texts 검사와 ARIA snapshot 기반이 있으나, 이것만으로 전 경로 신뢰 경계나 부분 구조 변경감지가 입증되지는 않는다.
운영 마일스톤 M4의 기존 기준인 "복구 1회 성공 + 버전 자동 +1"은 후보 생성으로 제한한다. active 승격은 골든 태스크·회귀 검증 뒤에만 허용한다.

## 사용자 흐름

첫 진입은 채팅과 우측 브라우저 탭이다. 자연어 지시 후 실행 채널·이유·현재 단계·다음 행동을 보여준다. 일반 웹은 서버 Playwright로 즉시 처리한다.
재방문은 검증된 사이트 스킬을 변수만 바꿔 호출한다. 가격·재고·검색 결과는 저장된 결과를 그대로 답하지 않는다.
로그인 만료는 재로그인, 네이티브 창은 Windows 연결, OTP·본인확인·고위험 쓰기는 Human Gateway로 연결한다. 재개 시 계정·tenant·origin·승인 범위와 단계를 재검증한다. 401을 이유로 PC에 조용히 넘기지 않는다. Mac은 보류한다.

## 실행 순서

인증된 사용자 지시 → 정책/권한 precheck → Channel Router → 해당 채널의 Exact/Vector/LLM 스킬 선택 → typed 인자 검증 → 실행 직전 정책/승인 재검사 → API/Browser/PC/Human 실행 → 단계 성공 검증 → 표시 직전 사실 재조회 → 근거와 결과 표시.
Channel Router는 검색 전에 가능한 채널을 제한하고 각 단계 직전에 다시 평가한다. 명시적으로 등록·허용된 공식 API/함수, 서버 DOM, 로컬 인증 브라우저, Windows 네이티브, 사용자 개입을 구분한다. API는 UI와 동등한 권한·승인 게이트를 거친다. 페이지가 API 주소나 실행 채널을 지시할 수 없다.
모호하거나 지원하지 않는 채널은 사용자에게 필요한 행동을 안내하고 중단한다. 재시도 횟수·시간 예산, 결정 사유, 채널 전환, 계정 바인딩을 감사한다. LLM 에스컬레이션도 권한을 높이지 않는다.

## 여섯 계약과 검증 기준

| 우선순위 / 연결 | 계약 | 필수 검증 |
|---|---|---|
| P0 / G1 | Channel Router 선행, 실행 채널 allowlist 및 단계별 재판정. 본인확인/외부 쓰기는 Human Gateway 우선 | DOM→서버, 네이티브→Windows, OTP→Human; PC 미연결 안내; API도 동일 승인; route 이유 기록 |
| P0 / G2 | 사용자 의도·정책과 페이지 관찰을 타입/출처로 분리. DOM·ARIA·OCR·RAG·도구 결과는 untrusted | 지시 무시, 비밀 유출, 외부 URL, ARIA 위장, OCR/RAG 공격이 tool/권한/scope를 바꾸지 못함; 정상 상품 추출은 성공 |
| P0 / G3·M7 | 스킬을 registry의 versioned API/함수로 관리 | skill_id/version/허용 handler/input/output schema/tenant/account/origin/pre/postcondition/risk/timeout/idempotency/증거 계약; 임의 eval·import·shell 불가; 권한·중복 실행·알 수 없는 스킬 차단 |
| P1 / G4·M8 | 페이지 전체 hash 대신 ARIA 기반 영역별 구조 시그니처 | role/name/landmark/부모·자식/레이블 관계를 정규화; 가격·광고·시간 변화는 허용, 핵심 검색/결제 컨트롤 변화는 해당 스킬만 무효화; ARIA 누락·중복 시 DOM 보조 또는 재학습 |
| P0 / G5·M10 | TTL 이내여도 표시 직전 실시간 사실 재조회 | source/entity/options/currency/account/observed_at/version 결합; 중간 변경·TTL 만료·다른 상품 옵션 혼합 차단; 조회 실패는 미확인 표시; 예전 값을 현재 사실로 단정 금지 |
| P0 / G6·M4·M8·M11 | draft/candidate→shadow→active 승격 | 골든 태스크 성공 + 영향 범위 회귀 + 보안·tenant·권한 음성 테스트 모두 통과; 실패 시 이전 active 유지/롤백; 복구 1회로 자동 승격 불가 |

### 신뢰 경계 상세

페이지 데이터에서 상품명·링크·가격을 추출하는 것은 허용한다. 단, 데이터가 도구 이름·handler·권한·승인·목적지 정책을 정하게 해서는 안 된다. 추출한 링크는 URL 정규화, origin allowlist, 리다이렉트/SSRF 제한을 통과해야 한다. 인자는 타입·길이·열거값 검사를 거친다. LLM 출력은 실행 명령이 아닌 검증 대기 제안이다.
페이지, ARIA accessible name, OCR, 과거 경험 메모리 모두 출처를 보존한다. 신뢰도 점수나 인젝션 키워드 필터만으로 신뢰된 지시로 승격하지 않는다. 비밀은 Vault 참조로만 넘기고 DOM·증거·로그·Vector DB에 남기지 않는다.

### ARIA 부분 구조 상세

시그니처는 tenant/site/template/version/region을 키로 관리한다. ARIA 이름 자체도 공격자 통제 가능하므로 정규화·길이 제한을 적용한다. 추출 데이터와 동적 요소를 제외하되 action의 대상·위험도를 바꾸는 구조는 제외하지 않는다. 로그인 상태·locale·AB 변형은 별도 템플릿 조건으로 저장한다. 유사도 기준은 골든 fixture로 교정하고 실측 없이 임의 성공률을 약속하지 않는다.

### 사실 재검증 상세

결과 생성 직전 상품 식별자·옵션·판매자·통화·지역·계정 조건을 고정해 읽기 전용 재조회한다. 브라우저 표시/스트리밍 직전에 freshness token과 observed_at을 다시 확인하고 만료되면 재조회 또는 미확인 상태로 내린다. 결과와 근거가 같은 관찰 버전을 참조해야 한다. 조회 이후 사이트 변경 가능성은 관찰 시각으로 드러내며 원자적 최신성을 보장한다고 표현하지 않는다. 결제 같은 최종 실행은 별도 승인과 실행 직전 재검증이 필요하다.

### 승격과 골든 태스크 상세

로컬 결정적 fixture: 첫 방문 검색, 재방문 변수 변경, 구조 일부 변경, 가격 변경, 품절, 로그인 만료, PC 미연결, OTP 대기/재개, 페이지 인젝션, tenant 교차 접근, timeout/중복 재시도.
외부 읽기 전용 사이트 E2E는 별도 실행해 실측 근거를 남긴다. 네트워크 장애를 성공으로 처리하지 않으며 fixture 통과만으로 외부 E2E를 대체했다고 말하지 않는다. 승인된 skill version과 test suite digest·실행 결과·증거를 묶고, 코드/정책/템플릿 버전 변경 시 재검증한다. shadow 검증은 소규모 읽기 전용으로 제한하고 자동 승격 조건·실패 롤백을 명시한다.

## 구현 순서와 출시 조건

등록된 실행 순서: G1 채널 라우터 → G2 신뢰 경계 → G3 스킬 함수 → G4 ARIA → G5 사실 재검증 → G6 골든 승격 → M4~M11 기존 통합·E2E·배포. M3 인증 검수는 기반 작업과 병행한다. M4와 최종 출시 지시에는 M3 승인·main 반영 SHA·인증 회귀 증거를 별도로 확인하고, 미충족 시 차단하도록 명시한다. 현재 단일 depends_on만으로 G6와 M3 두 선행 조건이 모두 보장된다고 표현하지 않는다. G3~G6은 먼저 공용 계약을 구현하고 후속 M7~M11이 이를 실제 사이트 학습·검색·화면에 연결한다. 독립 모듈 테스트만 통과한 상태는 최종 결선 완료가 아니다.
마일스톤 번호는 기존 참조를 유지하며 sequence_order와 Runner depends_on으로 실행 순서를 지정한다. M9가 G1 라우터를 다시 만들지 않도록 기존 계약을 재사용한다.
검증 결과, git SHA, 실제 운영 image digest, `/chat` 브라우저 아티팩트 캡처, API 결과, 비용/LLM 호출 수를 기록한다. 미실행은 미검증으로 남긴다. API는 deploy.sh bluegreen 단일 SHA 이미지·후보 health·짧은 nginx lock·DB owner lease·동일 digest standby·실패 rollback·5분 P0/P1 감시를 통과해야 출시 완료다.
운영 계획 변경은 대상 goal/milestone/queued job만 트랜잭션으로 수정하고 이전 값 snapshot을 보존한다. 코드 rollback은 이전 검증 버전/라우팅 복원이며 기존 데이터 삭제는 사용하지 않는다.
