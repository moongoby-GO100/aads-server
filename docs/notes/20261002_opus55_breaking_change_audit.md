# Opus 5.5 breaking change 점검 (AADS-OPUS55-RESIDUAL-DEFAULTS-20261002)

수정하지 않고 목록만 남긴다. 점검 키워드: `tool_choice`, `"type": "disabled"`, `_drop_tool_choice_for_thinking`.

## claude-opus-5-5 로 갈 때 영향 가능성이 있는 곳 (app/services/model_selector.py)
- ~5905행: turn 0 에서 `_force_tool_intents`(status_check, dashboard, search, browser 등)이면 `tool_choice={"type":"any"}` 를 강제한다.
- ~6069행: 빈 응답 재시도 시 `tool_choice={"type":"any"}` 를 다시 강제한다.
- `_drop_tool_choice_for_thinking`(1177행)은 `thinking_config` 가 있을 때만 tool_choice 를 지운다. thinking 이 꺼진 Opus 5.5 호출에서는 강제 tool_choice 가 그대로 나간다. 공식 문서의 breaking change(강제 tool_choice any/tool) 해당 여부를 배포 후 확인할 것.
- 3519행 `{"thinking": {"type": "disabled"}}` 는 deepseek 계열 전용이라 Opus 5.5 와 무관.
- `computer_20251124` 도구 사용처는 이번 범위에서 확인하지 않음.

## SDK 경로 라벨/실행 불일치 (수정함)
- agent_sdk_service._build_options 가 `model="claude-opus-4-6"` 으로 고정이었고 chat_service 의 DIRECT_EXECUTION 라벨은 `claude-opus-5` 였다. 라벨과 실제 실행 모델이 달랐다.
- `execute_stream/_build_options` 에 `model` 인자를 추가하고 chat_service 가 `_DEFAULT_PREMIUM_CLAUDE` 를 넘긴다. 인자 없이 호출하면 기존 `claude-opus-4-6` 유지.
