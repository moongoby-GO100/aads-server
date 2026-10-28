---
name: mcp-server-design
description: "MCP 서버나 AADS 도구(tool_registry.py)를 새로 설계하거나 기존 도구 정의를 검토할 때 쓴다. 도구 이름·설명·입력 스키마·오류 메시지·평가 질문을 점검 항목 순서대로 확인한다. 새 도구 추가, 모델이 도구를 못 고르거나 잘못 호출하는 문제, 외부 MCP 서버 연동 검토가 나오면 먼저 읽는다."
allowed-tools: Read, Grep, Glob
risk_tier: read
status: candidate
executor: model-instructions
source: "https://github.com/anthropics/skills/tree/main/skills/mcp-builder"
license: Apache-2.0
adapted: 2026-10-28
---
# MCP 서버·도구 설계 검토

이 스킬은 **설계와 검토 방법**만 다룬다. 파일 수정·셸 실행 권한을 주지 않는다.
실제 코드 수정과 검증 명령은 이 스킬을 읽은 세션(채팅·러너)이 이미 가진 권한 안에서 한다.

## 언제 쓰나
- `app/services/tool_registry.py` 의 `_TOOLS` 에 도구를 추가하거나 고치기 전.
- 모델이 비슷한 도구 사이에서 계속 엉뚱한 것을 고를 때.
- 외부 MCP 서버(또는 `langchain_mcp_adapters` 경유 도구)를 붙이기 전 품질 검토.

쓰지 않는 경우: 도구 내부 비즈니스 로직 버그 추적은 `systematic-debugging` 을 쓴다.

## 먼저 정해야 할 것
1. **이 도구가 해결할 사용자 과업 한 문장.** 과업이 없으면 API 엔드포인트를 그대로 옮긴 도구가 된다.
2. **API 1:1 도구냐, 과업 단위 도구냐.** 한 과업에 호출이 3번 이상 필요하면 과업 단위로 묶는 것을 먼저 검토한다.
   반대로 조합 가능성이 중요한 저수준 조작은 1:1 로 남긴다. 둘을 섞을 때는 이름에서 구분되게 한다.
3. **읽기/쓰기/되돌릴 수 없음** 중 어디인가. 쓰기·삭제 도구는 설명 첫 줄에 그 사실을 적는다.

## 도구 정의 점검표 (`_TOOLS[name]`)

### 이름
- [ ] 동사_대상 형태이고 기존 이름과 접두어 규칙이 같다 (`read_remote_file`, `list_remote_dir` 처럼).
- [ ] 다른 도구와 이름만 보고 구분된다. 헷갈리는 쌍이 있으면 설명에 "X 가 아니라 Y 를 쓸 때" 를 넣는다.
- [ ] `_TOOLS` 의 키와 `"name"` 값이 같다.

### 설명 (`description`)
- [ ] 첫 문장이 "무엇을 하는가", 다음이 "언제 쓰는가 / 쓰면 안 되는가".
- [ ] 반환값의 모양(필드, 단위, 최대 개수)을 적었다. 모델은 반환 구조를 모르면 후속 호출을 못 만든다.
- [ ] 다른 도구가 필요한 선행 조건(예: 마일스톤 id 를 먼저 얻는 도구)을 이름으로 가리킨다.
- [ ] 길이는 필요한 만큼만. 지연 로딩(`_DEFER_LOADING`)이 아닌 상시 도구는 매 요청 토큰을 먹는다.

### 입력 스키마 (`input_schema`)
- [ ] 필수는 `required` 에 있고, 선택 인자의 기본값은 설명에 쓰여 있다.
- [ ] 열거형은 `enum`, 숫자는 `minimum`/`maximum`, 문자열 형식은 설명에 예시를 넣는다.
- [ ] 자유 문자열 하나에 여러 의미를 싣지 않는다. 조건이 둘이면 인자를 둘로 나눈다.
- [ ] 목록을 돌려주는 도구는 `limit`/`offset` 또는 커서를 받는다. 무제한 반환은 컨텍스트를 터뜨린다.
- [ ] `input_examples` 에 정상 호출 2건 이상(최소 호출 1건 + 선택 인자 다 쓴 호출 1건).

### 출력
- [ ] 성공 응답이 JSON 이면 키 이름이 도구 간에 일관된다 (`id`, `status`, `items` 등).
- [ ] 큰 결과는 요약+식별자를 주고 상세는 별도 조회로 뺀다.
- [ ] 한 호출에서 사람이 읽을 문장과 기계가 읽을 필드를 섞는다면 필드가 우선이다.

### 오류 메시지
- [ ] "실패" 로 끝나지 않고 **왜 + 다음에 무엇을 하면 되는지**를 적는다.
  나쁜 예: `invalid input`. 좋은 예: `project 는 AADS/KIS/GO100/SF/NTV2/NAS 중 하나여야 합니다. 받은 값: aads2`.
- [ ] 오류에 `error_code` 같은 안정적인 식별자가 있다 (`tool_executor.py` 의 timeout 응답이 그 방식이다).
- [ ] 비밀값·내부 경로·토큰이 오류 문자열에 섞이지 않는다 (R-KEY, R-AUTH).

## AADS 등록 정합성 (놓치기 쉬운 곳)
도구 하나는 **세 곳**에 같이 들어가야 한다. 한 곳만 고치면 `scripts/check_tool_consistency.py` 가 pre-commit 에서 막는다.
1. `app/services/tool_registry.py` — `_TOOLS` 정의, 필요하면 `_GROUPS`·`_CORE_TOOLS`·`_INTENT_TOOL_MAP`·`_DEFER_LOADING`.
2. `app/services/tool_executor.py` — `_dispatch` 에 실제 실행 분기.
3. `app/api/ceo_chat_tools.py` — `TOOL_DEFINITIONS` 와 `execute_tool`. (CEO 채팅 전용 도구는 executor 에 없어도 되는 예외 목록이 검사기에 있다.)

검토 순서: `Grep` 으로 새 이름이 세 곳에 모두 있는지 → 그룹/인텐트에 들어갔는지 →
`_DEFER_LOADING` 값이 의도와 맞는지(기본이 지연 로딩이라 빠뜨리면 상시 로드되지 않는다) 확인한다.
`check_tool_consistency --fix` 로 자동 생성된 코드는 들여쓰기·클래스 소속이 틀릴 수 있으니
`bash scripts/run_unit_tests.sh tests/unit/test_tools_and_pipeline.py` 를 반드시 돌린다 (R-QUALITY).

## 외부 MCP 서버 도입 시 추가 확인
- 전송 방식: 원격이면 상태 없는 HTTP 가 운영·확장에 유리하고, 로컬 프로세스면 stdio 다.
- 도구에 읽기전용/파괴적/멱등/외부세계 접근 힌트(annotations)가 있는지. 없으면 우리 쪽 위험 등급을 보수적으로 매긴다.
- 인증은 `.env` 경유 주입만 허용한다. 소스·yaml 에 키를 쓰지 않는다 (R-KEY).
- LLM 호출이 필요한 도구라면 `call_llm_with_fallback()` 을 거친다 (R-AUTH).

## 평가 질문 작성 (도구를 만든 뒤)
도구가 쓸 만한지는 정의를 읽어서가 아니라 **질문을 풀게 해 봐야** 안다. 10개 안팎의 질문을 만든다.
- 읽기 전용 호출만으로 답이 나온다 (쓰기 부작용 없음).
- 도구를 여러 번, 서로 다른 도구를 섞어 써야 풀린다 — 한 번에 풀리면 변별력이 없다.
- 실제 사용자가 할 법한 과업이다 (장난감 질문 금지).
- 정답이 하나의 검증 가능한 값이다 (문자열 비교로 채점 가능).
- 시간이 지나도 답이 변하지 않는다 ("현재 개수" 같은 질문은 피한다).
질문마다 `질문 / 정답 / 필요한 도구 순서` 를 적어 두고, 모델이 못 풀면 **도구 정의를 먼저 의심**한다 (이름, 설명, 반환 구조 순).

## 산출물 형식
검토 결과는 항목별 `통과 / 수정 필요 / 해당 없음` 표와, 수정 필요 항목의 구체적 변경안(어느 파일 몇 번째 줄을 어떻게)으로 보고한다.

## 승격 조건
현재 `status: candidate`. 아래를 모두 만족해야 `active` 로 올린다.
1. 이 스킬을 실제로 따라 수행한 **사용 증거 2건** — 세션 id 또는 러너 job_id 를 각각 기록한다.
2. 두 건 모두 점검표 결과(표)가 남아 있고, 그 결과로 실제 도구 정의가 개선되었거나 "문제 없음" 이 근거와 함께 확인되었다.
3. 위험 등급이 `read` 로 유지된다 (`infer_risk_tier` 결과 확인). 쓰기·셸 도구를 추가하려면 별도 스킬로 나눈다.
승격은 증거 확인 후 사람이 승인한다. 이 문서는 스스로 승격하지 않는다.

## 출처 및 변경 고지
- 출처: anthropics/skills 저장소의 `skills/mcp-builder` (https://github.com/anthropics/skills/tree/main/skills/mcp-builder).
- 라이선스: Apache License 2.0. 원 저작물의 라이선스 전문은 위 저장소의 `LICENSE.txt` 에 있다.
- 변경 사실: 원문을 옮기지 않고 방법론(설계 원칙·점검 항목·평가 질문 기준)을 이해한 뒤 한국어로 새로 썼다.
  AADS 도구 레지스트리·정합성 검사·사내 규칙(R-KEY, R-AUTH, R-QUALITY) 기준으로 항목을 바꾸었고,
  언어별 SDK 안내와 원본의 부속 자료는 포함하지 않았다. 2026-10-28 각색.
