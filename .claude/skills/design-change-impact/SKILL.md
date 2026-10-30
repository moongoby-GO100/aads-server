---
name: design-change-impact
description: "화면 수정·운영 어긋남에서 변경 범위와 회귀 위험을 정한다."
allowed-tools: Read, Grep, Glob
risk_tier: read
status: candidate
version: 0.3.0
executor: model-instructions
---
# 디자인 변경 영향 분석

문서 키: `design-change-impact`. 이 스킬은 검토·질문 지침이며 외부 쓰기 권한을 부여하지 않는다.

## 입력·산출물과 진행 규칙

입력: {project_key, screen, request, existing_patterns, constraints, intake_answers, brief_confirmed, baseline_revision, baseline_hash, before_hash, after_hash, approved}.
산출물: {impact_level, request_type, scope_answers, alternatives: [{intensity, before, after, affected, risks}], regression_checklist, baseline_revision, baseline_hash, before_hash, after_hash, allowed_scope, forbidden_scope, acceptance_criteria}.
영향 범위는 국소(한 요소·화면), 패턴(반복 컴포넌트), 시스템(토큰·전체 흐름) 3단계로 분류한다.
기존 design_modification_requests request_type 14종 중 하나만 사용한다:
spacing, spacing_density, visual_hierarchy, color, color_brand, typography, component, component_consistency, responsive, interaction, content_clarity, workflow_layout, flow, other.
여러 유형이 겹치면 주 유형과 이유를 기록하고 범위를 확장하기 전에 질문한다.
질문은 소유하지 않는다. design-intake-interview 답변 기록(범위 질문 모드)을 참조한다. 적용 범위·보존 조건·완료 판단이 없으면 intake로 반환한다.
브리프 확인 후 보수/중간/적극 전후 3안을 모두 제시하고 각 안의 영향·위험을 비교한다. 답변 전 전체 시스템 변경을 확정하지 않는다.
산출물은 기준 승인본 revision/hash(baseline_revision/baseline_hash), 전후 판본 hash(before_hash/after_hash)를 기록하고 각 변경을 allowed_scope/forbidden_scope/acceptance_criteria와 연결한다. 기준 승인본 또는 hash 누락·불일치면 중단하고 기준을 확인한다. 브리프 확인 전 시안 생성 금지. 승인 전 인계 금지: approved=true 및 승인 대상 hash 일치 전 구현 담당자에게 넘기지 않는다.
회귀 점검표에 기존 화면, 공유 컴포넌트, 반응형, 키보드·포커스, 데이터 상태, 토큰 영향과 확인 방법을 포함한다.
실패·질문 조건: 범위 답변 누락·보존 조건 충돌은 질문하고 미확정으로 남긴다. API나 승인 상태를 변경하지 않는다.

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

실행 단계: S3의 대상 특정→현재 상태→유형/영향 분류→범위 질문→전후 3안→회귀→승인/인계→배포 후 대조를 추적한다. 기존 14종 request_type 중 근거로 분류하고 영향은 국소/패턴/시스템으로 구분한다. 변경 전후 화면·6개 상태·역할·공용 컴포넌트/토큰·관련 화면·회귀·복구를 나열한다. 같은 콘텐츠/viewport의 보수·중간·적극 3안, allowed_scope·forbidden_scope·acceptance_criteria, 승인본 새 판본/hash가 필요하다. 배포 후 차이는 S9로 연결한다.
반려·복구 조건: 파일 diff만 보고 영향 없음 판정, 공용 토큰의 타 화면 영향 누락, 승인본 덮어쓰기, S3와 S8 혼동, 범위 질문 생략, 빈 범위 필드로 review, 승인 전 구현 러너 인계, 캡처/회귀 미확인을 완료로 표기하면 반려한다.

| 단계 | 에이전트가 하는 일 | 산출물 | 통과 조건 |
|---|---|---|---|
| 1 접수·대상 특정 | 대상 화면을 design_screens·route·캡처에서 찾는다. 못 찾으면 실재 후보 화면 3개 이상을 제시해 고르게 한다. 실재 후보가 3개 미만이면 그 사실을 밝힌다 | screen_id 또는 route | 대상 1개 이상 확정 |
| 2 현재 상태 수집 | PC·모바일 캡처, 승인본 판본·hash, 사용 컴포넌트·토큰 | 현재 화면 증거, 승인본 대조표 | 캡처 실패 시 HTTP·API 폴백 기록, 시각 검수는 미완료 |
| 3 유형·영향 분류 | request_type 14종 분류, 영향 3단계: 국소/패턴/시스템 | 유형, 영향 화면 목록 | 분류 근거 기록 |
| 4 범위 질문 | design-intake-interview를 범위 질문 모드로 실행. ① 적용 범위 ② 바꾸지 말 것 ③ 완료 판단 기준. 문항마다 선택 예시 3개 이상·권장·출처 | 답변 기록 | 필수 3문항 확정. change-impact는 같은 답변 기록을 참조만 하고 다시 묻지 않는다 |
| 5 전후 비교 시안 | 같은 콘텐츠·viewport로 전후 비교, 강도별 3안(보수·중간·적극), 바뀐 부분 표시 | 전후 비교 3안 | 상태 6종 유지 확인 |
| 6 회귀 확인 | 영향 화면 전체에서 깨지는 곳 확인. 대비·터치·넘침은 코드로 계산 | 회귀 점검표 | 차단 결함 0건, 계산 못 한 항목은 평가불가 |
| 7 승인·인계 | allowed_scope·forbidden_scope·acceptance_criteria를 채워 review로 올림(D1.6 게이트 3이 강제). CEO 승인 후 승인본 새 판본·hash 생성 후 러너 인계 | 승인된 수정 요청, 새 판본·hash | 승인 전 러너 인계 금지 |
| 8 배포 후 대조 | 배포 화면을 승인된 "후" 시안과 비교 | 대조 결과 | 어긋나면 S9. 배포가 없으면 이 단계는 미실행으로 표시 |


## 검증 fixture

출처: design-agent-d15-ux-acceptance-rev4-57980cb2 revision 2. 정적 계약이며 실제 모델·화면 실행은 미검증이다.

### 정상 fixture

ID: UX10-N
입력: 공용 버튼 간격 수정 요청을 패턴 영향으로 분류하고 관련 화면/권한/6개 상태를 나열한다. 범위 질문 확인 후 같은 콘텐츠/viewport의 3안과 회귀 증거를 준비한다.
기대: 합격 가능: 범위·금지·인수 기준을 채워 review까지 진행; 실제 CEO 승인 후 새 승인판본/hash로 인계한다.
관측: 배포가 없다면 8단계는 미실행이며, 전체 S3 완료로 보고하지 않는다.

### 누락 fixture

ID: UX10-M
입력: 대상 화면이 불명확하거나 forbidden_scope/acceptance_criteria가 비어 있다.
기대: 질문 필요·review 차단: 실재 후보 3개 이상이 있으면 제시해 선택받고 없으면 후보 부족을 밝힌다. 범위를 확인한다.
관측: 없는 화면을 만들어 후보 수를 채우거나 빈 필드를 허위 기본값으로 채워 통과시키면 반려한다.

### 충돌 fixture

ID: UX10-C
입력: 요청은 한 화면 수정인데 공용 토큰 변경으로 여러 화면이 바뀌며 금지 범위와 겹친다.
기대: 진행 차단·범위 재확인: 국소 대안과 시스템 변경 영향을 비교하고 승인 범위를 재확정한다.
관측: 원래 국소 승인으로 공용 변경을 인계하면 반려한다.

### 실패 fixture

ID: UX10-F
입력: 회귀에서 다른 화면 넘침이 발생하거나 배포 후 승인된 후 시안과 다르다.
기대: 회귀 결함이면 인계 차단; 배포 후 차이면 S9 수정 초안으로 연결하고 복구 판단에 필요한 근거를 제공한다.
관측: 관찰만으로 임의 재배포/롤백하지 않으며 실패를 완료로 표시하지 않는다.

## 추가 회귀 사례 (supplemental)

정본 40건에 합산하지 않는다.


입력: {screen: 주문, request: 버튼 간격 확대, scope_answers: null}
기대: design-intake-interview 범위 질문 모드로 반환, next_action=ask, 시안 0개, 인계 중단.
입력: {request: 전체 브랜드 색 교체, constraints: 브랜드 색 유지}
기대: 시스템 영향·color_brand, 충돌 질문, 변경 확정 없음.

위 fixture는 기대 동작 명세다. 실제 모델 수행 검증·사용 증거와 사람 승인 전에는 active로 승격하지 않는다.

### supplemental 정상 fixture

입력: {scope_answers: 확인됨, brief_confirmed: true, baseline_revision: r1, baseline_hash: h1, before_hash: h1, after_hash: h2, approved: false}
기대: 3안과 범위·회귀 연결, 승인 전 인계 금지.

### supplemental 누락 fixture

입력: {scope_answers: null}
기대: intake 범위 질문 모드 반환, 시안 0개.

### supplemental 충돌 fixture

입력: {allowed_scope: 색 변경, forbidden_scope: 브랜드 색 유지}
기대: 충돌 질문, 시안·인계 중단.

### supplemental 실패 fixture

입력: {baseline_hash: 승인본과 불일치}
기대: 기준 판본 확인 요청, 인계 중단.
