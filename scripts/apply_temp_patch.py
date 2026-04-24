"""Governance v2.1: temperature 배선 패치 — model_selector.py"""
import re

TARGET = "/root/aads/aads-server/app/services/model_selector.py"

with open(TARGET, "r") as f:
    code = f.read()

changes = 0

# Patch 1: contextvars import + _ctx_temperature
old1 = "import time as _time_mod\nfrom datetime import datetime, timezone"
new1 = "import time as _time_mod\nimport contextvars\nfrom datetime import datetime, timezone\n\n_ctx_temperature: contextvars.ContextVar[float] = contextvars.ContextVar('_ctx_temperature', default=0.2)"
if old1 in code and "_ctx_temperature" not in code:
    code = code.replace(old1, new1, 1)
    changes += 1
    print("Patch 1: contextvars import OK")

# Patch 2: resolve temperature in call_stream()
old2 = "    runtime_available_models = await get_available_model_ids()\n    if runtime_available_models and model not in runtime_available_models:"
new2 = "    from app.services.intent_router import resolve_intent_temperature as _rit\n    _ctx_temperature.set(await _rit(_intent))\n\n    runtime_available_models = await get_available_model_ids()\n    if runtime_available_models and model not in runtime_available_models:"
if old2 in code and "_rit" not in code:
    code = code.replace(old2, new2, 1)
    changes += 1
    print("Patch 2: call_stream temperature resolve OK")

# Patch 3: _stream_litellm_anthropic req_body
old3 = '''                req_body: Dict[str, Any] = {
                    "model": litellm_model,
                    "system": _cached_system,
                    "messages": current_msgs,
                    "max_tokens": _MAX_TOKENS_CLAUDE,
                    "stream": True,
                }'''
new3 = '''                req_body: Dict[str, Any] = {
                    "model": litellm_model,
                    "system": _cached_system,
                    "messages": current_msgs,
                    "max_tokens": _MAX_TOKENS_CLAUDE,
                    "stream": True,
                    "temperature": _ctx_temperature.get(0.2),
                }'''
if old3 in code:
    code = code.replace(old3, new3, 1)
    changes += 1
    print("Patch 3: litellm_anthropic temperature OK")

# Patch 4: _stream_litellm_openai req_body
old4 = '''                req_body: Dict[str, Any] = {
                    "model": model,
                    "messages": loop_msgs,
                    "max_tokens": max_tokens,
                    "stream": True,
                    **extra_params,
                }'''
new4 = '''                req_body: Dict[str, Any] = {
                    "model": model,
                    "messages": loop_msgs,
                    "max_tokens": max_tokens,
                    "stream": True,
                    "temperature": _ctx_temperature.get(0.2),
                    **extra_params,
                }'''
if old4 in code:
    code = code.replace(old4, new4, 1)
    changes += 1
    print("Patch 4: litellm_openai temperature OK")

# Patch 5: _stream_cli_relay req_body
old5 = '''    req_body: Dict[str, Any] = {
        "model": model,
        "system_prompt": system_prompt,
        "session_id": session_id or "",
    }'''
new5 = '''    req_body: Dict[str, Any] = {
        "model": model,
        "system_prompt": system_prompt,
        "session_id": session_id or "",
        "temperature": _ctx_temperature.get(0.2),
    }'''
if old5 in code:
    code = code.replace(old5, new5, 1)
    changes += 1
    print("Patch 5: cli_relay temperature OK")

if changes > 0:
    with open(TARGET, "w") as f:
        f.write(code)
    print(f"\nTotal: {changes} patches applied to {TARGET}")
else:
    print("No patches needed (already applied or pattern mismatch)")
