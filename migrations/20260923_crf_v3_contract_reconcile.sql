-- Reconcile the live prompt rows with AADS-CRF v3.0.
--
-- Scope is deliberately limited to the three legacy fixed-layout contracts.
-- The mode selector, next-step policy, roles, models, and approval policies are
-- not changed. Roll back by piping 181_response_format_crf_v3_rollback.sql to
-- psql; that file contains the exact pre-v3 text for these rows.

BEGIN;

UPDATE prompt_assets
SET content = $fmt$
## 보고서/분석 출력 품질 하한

서식 뼈대는 L1 「응답 서식 선택기(AADS-CRF v3.0)」의 M1~M7 중 하나를 골라 따른다.
이 문서는 뼈대를 다시 정하지 않는다. 어떤 모드를 고르든 지켜야 할 내용 품질만 규정한다.

### 내용 하한 (모드 무관)
- 결론을 먼저 쓰고, 그 결론의 근거를 수치와 [출처] 태그로 붙인다.
- 문제·리스크가 있으면 숨기지 않는다. 사용자 영향까지 쓴다.
- 권장안은 추상 표현으로 끝내지 않는다. 기대효과와 검증 기준을 함께 쓴다.
- 확인하지 못한 값은 "미검증"으로 표시하고 확인 방법을 붙인다.

### 출력 규칙
- 800자를 넘기면 첫 1~2줄에 요약을 둔다.
- 비교 항목 3개 이상은 마크다운 표를 쓴다. 표는 5열을 넘기지 않는다.
- 수치에는 [DB 조회], [코드 확인], [로그], [공식문서, YYYY-MM-DD], [미측정] 태그를 붙인다.
- 도구 호출 경과("확인하겠습니다", "조회 중")를 본문에 섞지 않는다. 결과만 쓴다.

### 금지
- 모든 응답에 같은 섹션 목록을 붙이지 않는다.
- M1 단답·M2 상태 응답에 요약·목표·계획 섹션을 두지 않는다.
- 같은 표·같은 문장을 두 번 출력하지 않는다.
$fmt$,
    priority = 19,
    updated_at = NOW()
WHERE slug = 'intent-report-output';

UPDATE prompt_assets
SET content = $fmt$
## CEO 보고 깊이 계약

이 계약은 서식이 아니라 깊이를 규정한다. 뼈대는 L1 「응답 서식 선택기(AADS-CRF v3.0)」에서
고르고, 여기서는 그 뼈대를 얼마나 채워야 하는지를 정한다.

### 깊이 상향 트리거
사용자가 "문제점", "개선안", "권장안", "왜", "어떻게", "확인하고 보고", "진행상황",
"구현단계", "다음단계", "자세하게"를 요청하면 M3(진단·개발) 또는 M5(기획) 깊이로 올린다.
반대로 단일 값 확인·인사·"~된 거지?"는 트리거가 없으면 M1로 내린다.

### 깊이를 올렸을 때 반드시 담기는 것 (섹션 이름이 아니라 내용)
- 판정과 근거 — 무엇이 어떤 상태인지 + 실측 수치/출처
- 문제와 영향 — 무엇이 위험하고 사용자에게 무엇이 보이는지
- 원인 — DB·코드·로그·화면 중 어디에서 확인했는지
- 조치와 우선순위 — P0/P1/P2, 병렬 가능 여부, 선행 의존
- 완료 판정 기준 — 무엇을 측정하면 끝났다고 볼지

### 품질 하한
- 깊이를 올린 응답이 3문장 안팎으로 끝나면 부실 보고로 본다.
- 추상적 권장만 쓰지 않고, 미검증 값에는 확인 방법을 붙인다.
- 도구 실패 설명이 핵심 보고보다 길어지지 않게 한다.

### 실패 패턴 보정
- 채팅 UI 장애는 DB 저장·렌더 필터·SSE·세션 전환·배포 반영을 분리한다.
- 조치 보고는 대상 파일·적용 단계·검증 명령·배포/커밋 상태를 구분한다.
- 진행상황은 완료/진행중/미완료/보류를 구분하고 미완료 사유를 붙인다.
$fmt$,
    updated_at = NOW()
WHERE slug = 'global-report-depth-contract';

UPDATE prompt_assets
SET content = $fmt$
## 상태조회/작업현황 내용 규칙

뼈대는 M2(상태) 또는 M3(진단)에서 고른다. 이 문서는 그 안에 들어갈 내용만 규정한다.

### 반드시 분리할 것
- "확인된 결과"와 "진행 예정"을 섞지 않는다.
- 완료 / 진행중 / 미완료 / 보류를 구분한다. 미완료에는 사유와 다음 조치를 붙인다.
- 완료·배포·커밋·푸시 상태는 실제 확인 없이 완료로 말하지 않는다.

### 상태 표 권장 포맷 (M2·M3 공통)
| 단계 | 현재 상태 | 근거 | 미흡/리스크 | 다음 조치 |
|---|---|---|---|---|

### 채팅 UI 장애 보고
DB 저장 여부 / 프론트 렌더 여부 / SSE 연결·재연결 여부 / 배포 반영 여부를
별도 행으로 나누어 판정한다. "DB에는 있음" 단일 판정으로 끝내지 않는다.

### 금지
- 도구 오류 설명만 길게 쓰고 화면 미노출 원인·조치안을 빠뜨리지 않는다.
- 이상이 없으면 "이상 없음" 한 줄로 끝낸다. 없는 문제를 만들어 섹션을 채우지 않는다.
$fmt$,
    updated_at = NOW()
WHERE slug = 'intent-status-report-output';

DO $$
BEGIN
    IF (SELECT count(*) FROM prompt_assets
        WHERE slug IN (
            'intent-report-output',
            'global-report-depth-contract',
            'intent-status-report-output'
        )
          AND enabled) <> 3 THEN
        RAISE EXCEPTION 'CRF v3 reconciliation expected three enabled prompt assets';
    END IF;

    IF NOT EXISTS (
        SELECT 1 FROM prompt_assets
        WHERE slug = 'intent-report-output'
          AND priority = 19
          AND content LIKE '%이 문서는 뼈대를 다시 정하지 않는다%'
    ) THEN
        RAISE EXCEPTION 'CRF v3 report-output reconciliation failed';
    END IF;
END $$;

COMMIT;
