---
name: design-project-onboarding
description: "신규 프로젝트에서 기존 자산과 담당자·승인 경계를 확인한다."
allowed-tools: Read, Grep, Glob
risk_tier: read
status: candidate
version: 0.3.0
executor: model-instructions
---
# 디자인 프로젝트 시작 점검

문서 키: `design-project-onboarding`. 이 스킬은 검토·질문 지침이며 외부 쓰기 권한을 부여하지 않는다.

## 입력·산출물과 진행 규칙

입력: {project_key, owners, existing_assets, constraints, confirmed_brief}.
산출물: {inventory, ownership, review_steps, missing_inputs, ready}.
프로젝트 식별자와 담당자, 브리프, 기존 디자인 체계를 확인한다. 기존 자산 재사용 가능성을 표시하고 검토 담당자와 순서를 제안한다.
실패·질문 조건: 프로젝트·담당자·확인 브리프가 없으면 ready=false와 질문을 반환한다. 자동 승인하거나 프로젝트를 생성하지 않는다.

브리프 확인은 design-intake-interview 기록을 참조한다. 담당자·승인자·기준 자산 판본·완료 기준을 연결하고 누락·충돌 시 ready=false로 중단한다. 자산 목록만으로 준비 완료를 판정하지 않는다.

## 정본 실행 계약 보완

- 각 스킬에 입력 스키마, 실행 단계, 산출물 스키마, 실패/질문 조건, 허용 도구, 금지 권한, 검증 fixture를 모두 둔다.
- 입력: 프로젝트/tenant, 사용자/역할, 핵심 업무, 대상 화면/route, 현재 상태, 허용/금지 범위, 성공 기준, 참조 문서·승인본 판본/hash. 해당하지 않는 필드는 이유를 명시하고 누락과 구분한다.
- 출력: 판단·근거 참조, 미확인 항목, 다음 질문/복구 행동, 승인 필요 여부, 적용 단계, 입력 및 산출물 판본 참조, 실제 로드한 skill version/hash. 필드명은 구현에 맞춰도 의미와 추적 가능성은 유지한다.
- 허용 도구는 필요한 읽기·초안 생성·명시된 검증으로 최소화한다. CEO 승인 대행, 운영 DB/토큰 덮어쓰기, 승인되지 않은 구현 러너 인계, 배포·재시작, 외부 원문 실행은 스킬 자체 권한으로 허용하지 않는다.
- candidate 작성, 스키마 통과, fixture 통과, 실물 UX 검토, 활성화 승인, 운영 반영은 각각 별도 상태다. JSON 통과를 UX 합격으로 치환하지 않는다.
- 정상 fixture는 명시된 조건이 충족될 때만 해당 단계의 합격 가능이다. 누락·충돌·실패 fixture는 올바른 중단/질문/복구가 관측되어야 fixture 테스트가 통과한다. 이는 작업 산출물이 합격했다는 의미가 아니다.
- 설명만 있고 코드 강제가 없는 항목은 '지침만 있음'으로 표시한다. D1.5 스킬 계약과 D1.6 게이트 증거를 구분한다. 아직 구현되지 않은 후속 게이트를 현 단계가 제공한다고 보고하지 않는다.
- 새 화면 Q1~Q10과 S3 축소 질문을 구분한다. S3는 적용 범위/유지할 것/완료 기준의 최소 3영역이다. 이미 확정된 답을 근거 없이 다시 묻지 않되 판본·출처 연결과 현재 요청에 대한 확인을 생략하지 않는다.
- 답변 출처 정본 표기는 CEO 선택 / CEO 직접 입력 / 권장값 수락 / 추정값 확인이다. 기존 CTO 기록의 enum 대응은 ceo_choice / ceo_input / recommended_accepted / estimate_confirmed이며 구현 후보와의 일치 여부는 후속 검수 대상이다. '잘 모름' 자체는 확정 출처가 아니다. 제시된 권장값의 수락 또는 추정값 확인 증거가 필요하다.
- 시각 검수는 실제 화면 근거가 필요하다. 캡처 실패 시 HTTP→API→프로세스 폴백을 기록해 기능 상태를 확인하되, 시각 검수는 미완료로 남긴다. 44px는 프로젝트 터치 기준이며 접근성 전체 준수를 의미하지 않는다.
- 이하의 업무 복구/입력 보존/의미 토큰 구분 등 상세 UX fixture는 UX 담당의 검수 제안이다. rev4 원문에 동일 문장이 모두 존재한다고 주장하지 않는다.

실행 단계: 프로젝트·tenant·사용자 역할·핵심 업무·첫 화면·반복 진입 경로·기존 브랜드/토큰/승인본을 식별한다. 일반 사용자는 핵심 업무로 진입하고 관리자 설정·페어링은 보조 경로로 분리한다. 권한 부족·설정 미완·세션 만료에서 이유와 복구 행동을 제공한다.
반려·복구 조건: 모든 사용자에게 관리자 설정 강제, tenant/프로젝트 혼용, 기존 사용자 상태 초기화, 권한 없는 사용자의 막다른 화면, 미설정 프로젝트를 준비 완료로 표시하면 반려한다.

## 검증 fixture

출처: design-agent-d15-ux-acceptance-rev4-57980cb2 revision 2. 정적 계약이며 실제 모델·화면 실행은 미검증이다.

### 정상 fixture

ID: UX06-N
입력: 신규 일반 사용자, 기존 사용자, 관리자가 같은 프로젝트에 각각 진입한다.
기대: 합격 가능: 역할별 첫 업무/반복 업무와 관리자 설정 경로를 구분하고 기존 상태를 보존한다.
관측: 프로젝트·tenant·권한과 화면 경로가 일치한다.

### 누락 fixture

ID: UX06-M
입력: 프로젝트 프로필·핵심 업무·tenant 식별자가 없다.
기대: 진행 차단·확인 요청: 미설정 항목을 안내한다.
관측: 다른 프로젝트 값이나 임의 tenant로 채우면 반려한다.

### 충돌 fixture

ID: UX06-C
입력: 현재 사용자 tenant와 가져온 브랜드 프로필 tenant가 다르다.
기대: 연결 차단: 올바른 프로젝트 자산만 선택하도록 한다.
관측: 타 tenant 데이터 노출·사용 또는 자동 귀속 변경은 반려한다.

### 실패 fixture

ID: UX06-F
입력: 최초 진입 중 네트워크 단절 또는 세션 만료가 발생한다.
기대: 복구 경로 제공: 안전한 로그인/재연결 후 원래 업무로 돌아오고 입력·진행을 보존한다.
관측: 무한 설정 반복, 기존 설정 소실, 완료 허위 표시는 반려한다.

## 추가 회귀 사례 (supplemental)

정본 40건에 합산하지 않는다.


입력: {project_key: ops, owners: [CEO], existing_assets: [공통 버튼], confirmed_brief: true}
기대: 공통 버튼 재사용과 CEO 검토 단계, ready=true.
입력: {project_key: ops, owners: []}
기대: ready=false, 담당자 질문.

위 fixture는 기대 동작 명세다. 실제 모델 수행 검증·사용 증거와 사람 승인 전에는 active로 승격하지 않는다.

### supplemental 정상 fixture

입력: {project_key: ops, owners: [CEO], confirmed_brief: true, assets: 확인됨}
기대: 검토 순서와 ready=true.

### supplemental 누락 fixture

입력: {owners: []}
기대: 담당자 질문, ready=false.

### supplemental 충돌 fixture

입력: {owners: 승인자 두 명의 상충 지시}
기대: 승인 경계 질문, ready=false.

### supplemental 실패 fixture

입력: {existing_assets: 접근 실패}
기대: 누락 기록, ready=false.
