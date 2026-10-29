---
name: design-contrast-audit
description: "모바일·접근성·운영 화면 어긋남에서 계산 근거로 판정한다."
allowed-tools: Read, Grep, Glob
risk_tier: read
status: candidate
executor: model-instructions
---
# 대비와 조작 영역 점검

문서 키: `design-contrast-audit`. 이 스킬은 검토·질문 지침이며 외부 쓰기 권한을 부여하지 않는다.

## 입력·산출물과 진행 규칙

입력: {rendered_styles, bounding_boxes, viewport, scroll_dimensions, calculation_results}.
산출물: {checks: [{element, metric, value, threshold, verdict, evidence}], questions}.
대비·터치(44px)·넘침은 모델 판단이 아니라 코드 계산값으로 판정한다. 계산 못 한 항목은 '평가불가'.
sRGB 채널을 선형화(c<=0.04045이면 c/12.92, 아니면 ((c+0.055)/1.055)^2.4)하고 L=0.2126R+0.7152G+0.0722B를 구한다.
대비율은 (L밝음+0.05)/(L어두움+0.05). 일반 텍스트 4.5:1, 큰 글자(24px 또는 굵은 18.66px 이상) 3:1을 비교한다.
계산 코드·입력·출력을 증거로 남긴다. 반투명·그라디언트 배경 합성을 계산 못 하면 평가불가다.
터치는 실제 조작 영역 bounding box의 폭과 높이가 모두 44px 이상인지 계산한다.
넘침은 같은 viewport에서 scrollWidth>clientWidth 및 요소 경계를 계산하고 의도된 내부 스크롤과 문서 넘침을 구분한다.
실패·질문 조건: 스타일·치수 누락이면 평가불가와 측정 요청. 스크린샷 인상으로 통과시키지 않는다.

## 검증 fixture

입력: {calculation_results: {contrast_black_white: 21, button_width: 44, button_height: 43, scrollWidth: 390, clientWidth: 375}}
기대: 대비 통과(일반 4.5), 터치 실패(높이 43<44), 문서 넘침 15px 실패, 계산 증거 기록.
입력: {rendered_styles: null, bounding_boxes: null}
기대: 모든 미측정 항목 '평가불가', 통과 없음.

위 fixture는 기대 동작 명세다. 실제 모델 수행 검증·사용 증거와 사람 승인 전에는 active로 승격하지 않는다.
