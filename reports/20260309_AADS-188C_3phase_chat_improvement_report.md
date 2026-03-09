# AADS-188C: 3-Phase Chat Improvement 보고서 (2026-03-09)

## 문제 진단

CEO Chat에서 "확인하겠습니다", "알겠습니다" 등 **빈 약속 응답** 반복 발생.
5개 레이어 근본 원인 분석 결과:

| Layer | 문제 | 원인 |
|-------|------|------|
| ① 시스템 프롬프트 | 행동 규칙 없음 | 도구 호출 의무 미명시 |
| ② 인텐트 라우팅 | 상태 조회 → 도구 미연결 | task_query/status_check 인텐트 누락 |
| ③ tool_choice | 항상 auto | Claude가 도구 안 쓰고 텍스트만 생성 |
| ④ 출력 검증 | 없음 | 빈 약속 응답 그대로 전달 |
| ⑤ 능력 경계 | 불명확 | "할 수 없는 것" 정의 부재 |

## 해결 — 3-Phase 구현

### Phase 1: 시스템 프롬프트 구조 개선

**파일**: `app/core/prompts/system_prompt_v2.py`

- `LAYER1_BEHAVIOR` — 행동 원칙 4개 절대 규칙 최상단 배치
  1. 빈 약속 금지
  2. 행동 우선 (도구 즉시 호출)
  3. 불가능 명시 (이유 + 대안)
  4. 응답 최소 기준 (도구 결과 | 거절 사유 | 명확화 질문)
- `LAYER1_CEO_GUIDE` — CEO 비격식 표현 → 도구 매핑 ("다른 친구" → 에이전트, "됐나?" → task_history)
- `LAYER1_ROLE` — Orchestrator 역할 명시 (직접 도구 vs 위임 판단)

### Phase 2: 메타 도구 + Orchestrator 기초

**파일**: `tool_executor.py`, `tool_registry.py`, `intent_router.py`

#### 메타 도구 3개 추가

| 도구 | 기능 | 내부 호출 |
|------|------|----------|
| `check_directive_status` | 작업 이력 + 서비스 상태 통합 | task_history + get_all_service_status |
| `delegate_to_agent` | Agent SDK 자율 실행 위임 | agent_sdk_service 가용성 확인 |
| `delegate_to_research` | Deep Research 위임 | deep_research 내부 호출 |

#### 인텐트 매핑

| 인텐트 | 모델 | 도구 그룹 | 트리거 예시 |
|--------|------|----------|-------------|
| `task_query` | claude-sonnet | meta | "시킨거 됐나?", "진행 확인" |
| `status_check` | claude-sonnet | meta | "전체 상태 보고", "시스템 체크" |

- `INTENT_REQUIRED_TOOLS` 매핑 추가 (task_query → check_directive_status)
- 키워드 fallback: 2개 이상 키워드 매칭으로 task_query 정확도 향상

### Phase 3: Output Validator + 동적 tool_choice

**파일**: `output_validator.py` (신규), `chat_service.py`, `model_selector.py`

#### Output Validator (`output_validator.py`)

3종 탐지 유형:

| 유형 | 조건 | 대응 |
|------|------|------|
| `EMPTY_PROMISE` | <100자 + "하겠습니다" 패턴 + 도구 미호출 | 재시도 프롬프트 |
| `NO_TOOL_FOR_ACTION` | <200자 + 행동 동사 + "겠" + 도구 미호출 | 재시도 프롬프트 |
| `TOO_SHORT` | <30자 + 도구 미호출 (greeting/casual 제외) | 재시도 프롬프트 |

- `chat_service.py` 인라인 검증 코드 → `output_validator` 모듈로 교체
- 탐지 시 자동 재시도: 빈 약속 + 시스템 경고를 대화에 추가 후 재호출

#### 동적 tool_choice (`model_selector.py`)

| 인텐트 유형 | tool_choice | 동작 |
|------------|-------------|------|
| status_check, dashboard, task_query, health_check 등 | `any` | 반드시 도구 호출 |
| greeting, casual | 생략 (auto) | 도구 호출 안 해도 됨 |
| 나머지 (use_tools=true) | `any` (첫 턴) | 도구 호출 우선 |

## Agent SDK 활성화

**파일**: `agent_sdk_service.py`, `agent_hooks.py`

| 항목 | 설정 |
|------|------|
| 허용 도구 | Read, Glob, Grep, Write, Edit, Bash + AADS 도구 21개 |
| permission_mode | `default` (훅에서 자동 승인) |
| 위험 차단 | rm -rf, DROP TABLE, shutdown, fork bomb 등 12개 패턴 |
| 민감 경로 차단 | .env, .ssh/, id_rsa, /etc/shadow 등 13개 경로 |
| 인텐트 라우팅 | execute, code_modify → claude-opus + group=all |

## 수정 파일 목록 (9개)

| 파일 | Phase | 변경 내용 |
|------|-------|----------|
| `app/core/prompts/system_prompt_v2.py` | 1 | 행동 원칙, CEO 화법, Orchestrator |
| `app/services/agent_hooks.py` | SDK | PreToolUse/PostToolUse 훅 dict 반환 |
| `app/services/agent_sdk_service.py` | SDK | Write/Edit/Bash 허용, 21 도구 |
| `app/services/chat_service.py` | 3 | output_validator 모듈 통합 |
| `app/services/intent_router.py` | 2 | task_query/status_check 인텐트 |
| `app/services/model_selector.py` | 3 | 동적 tool_choice |
| `app/services/tool_executor.py` | 2 | 3 메타 도구 구현 |
| `app/services/tool_registry.py` | 2 | 3 메타 도구 스키마 + INTENT_REQUIRED_TOOLS |
| `app/services/output_validator.py` | 3 | **신규** — 빈 약속 탐지 모듈 |

## 배포 상태

| 항목 | 값 |
|------|-----|
| 커밋 | `cd17a32` |
| 브랜치 | `main` |
| Push | origin/main ✅ |
| Docker 빌드 | ✅ 성공 (60s) |
| 컨테이너 | aads-server Up, healthy |
| 헬스체크 | ✅ 응답 정상 |
| 문법 검증 | 9개 파일 모두 ✅ |

## 테스트 시나리오

### Phase 1 테스트
- "다른 친구에게 시킨거 진행 확인해줘" → `task_query` 인텐트 → `check_directive_status` 호출 기대

### Phase 2 테스트
- "전체 상태 보고해" → `status_check` → `check_directive_status` + `get_all_service_status`
- "KIS 작업 현황" → `task_query` → `check_directive_status(project="KIS")`

### Phase 3 테스트
- 빈 약속 응답 시 자동 재시도 확인
- greeting("안녕") → tool_choice 생략, 텍스트 응답
- health_check → tool_choice=any, 반드시 도구 호출
