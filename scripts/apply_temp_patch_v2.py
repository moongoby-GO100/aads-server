"""Governance v2.1: temperature 배선 패치 v2 — 컨테이너/호스트 자동 감지"""
import os

# 경로 자동 감지: 컨테이너 내부 또는 호스트
if os.path.exists("/app/app/services/model_selector.py"):
    TARGET = "/app/app/services/model_selector.py"
elif os.path.exists("/root/aads/aads-server/app/services/model_selector.py"):
    TARGET = "/root/aads/aads-server/app/services/model_selector.py"
else:
    raise FileNotFoundError("model_selector.py not found")

print(f"TARGET: {TARGET}")

with open(TARGET, "r") as f:
    code = f.read()

original_len = len(code)
changes = 0

# Patch 1: contextvars import + _ctx_temperature
if "_ctx_temperature" not in code:
    old1 = "import time as _time_mod\nfrom datetime import datetime, timezone"
    new1 = "import time as _time_mod\nimport contextvars\nfrom datetime import datetime, timezone\n\n_ctx_temperature: contextvars.ContextVar[float] = contextvars.ContextVar('_ctx_temperature', default=0.2)"
    if old1 in code:
        code = code.replace(old1, new1, 1)
        changes += 1
        print("Patch 1: contextvars import OK")
    else:
        print("Patch 1: SKIP — pattern not found")
else:
    print("Patch 1: SKIP — already applied")

# Patch 2: resolve temperature in call_stream()
if "_rit" not in code and "_ctx_temperature" in code:
    old2 = "    runtime_available_models = await get_available_model_ids()\n    if runtime_available_models and model not in runtime_available_models:"
    new2 = "    from app.services.intent_router import resolve_intent_temperature as _rit\n    _ctx_temperature.set(await _rit(_intent))\n\n    runtime_available_models = await get_available_model_ids()\n    if runtime_available_models and model not in runtime_available_models:"
    if old2 in code:
        code = code.replace(old2, new2, 1)
        changes += 1
        print("Patch 2: call_stream temperature resolve OK")
    else:
        print("Patch 2: SKIP — pattern not found")

# Patch 3: _stream_litellm_anthropic req_body
old3 = '                req_body: Dict[str, Any] = {\n                    "model": litellm_model,\n                    "system": _cached_system,\n                    "messages": current_msgs,\n                    "max_tokens": _MAX_TOKENS_CLAUDE,\n                    "stream": True,\n                }'
new3 = '                req_body: Dict[str, Any] = {\n                    "model": litellm_model,\n                    "system": _cached_system,\n                    "messages": current_msgs,\n                    "max_tokens": _MAX_TOKENS_CLAUDE,\n                    "stream": True,\n                    "temperature": _ctx_temperature.get(0.2),\n                }'
if old3 in code:
    code = code.replace(old3, new3, 1)
    changes += 1
    print("Patch 3: litellm_anthropic temperature OK")

# Patch 4: _stream_litellm_openai req_body
old4 = '                req_body: Dict[str, Any] = {\n                    "model": model,\n                    "messages": loop_msgs,\n                    "max_tokens": max_tokens,\n                    "stream": True,\n                    **extra_params,\n                }'
new4 = '                req_body: Dict[str, Any] = {\n                    "model": model,\n                    "messages": loop_msgs,\n                    "max_tokens": max_tokens,\n                    "stream": True,\n                    "temperature": _ctx_temperature.get(0.2),\n                    **extra_params,\n                }'
if old4 in code:
    code = code.replace(old4, new4, 1)
    changes += 1
    print("Patch 4: litellm_openai temperature OK")

# Patch 5: _stream_cli_relay req_body
old5 = '    req_body: Dict[str, Any] = {\n        "model": model,\n        "system_prompt": system_prompt,\n        "session_id": session_id or "",\n    }'
new5 = '    req_body: Dict[str, Any] = {\n        "model": model,\n        "system_prompt": system_prompt,\n        "session_id": session_id or "",\n        "temperature": _ctx_temperature.get(0.2),\n    }'
if old5 in code:
    code = code.replace(old5, new5, 1)
    changes += 1
    print("Patch 5: cli_relay temperature OK")

if changes > 0:
    with open(TARGET, "w") as f:
        f.write(code)
    new_len = len(code)
    print(f"\nTotal: {changes} patches applied ({original_len} -> {new_len} bytes)")
else:
    print("No patches needed")
