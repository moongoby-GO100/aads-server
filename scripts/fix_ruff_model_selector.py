#!/usr/bin/env python3
"""Fix ruff E402/E701/F841 in model_selector.py — string-match based, safe."""
fpath = "/root/aads/aads-server/app/services/model_selector.py"

with open(fpath) as f:
    content = f.read()

original = content  # for diff count

# ─── E402: noqa on imports that follow module-level statements ───────────────
# Lines 23-31: imports after _ctx_temperature ContextVar assignment
for imp in [
    "import asyncpg\n",
    "import httpx\n",
    "from anthropic import AsyncAnthropic, APIStatusError, APIConnectionError, RateLimitError\n",
    "from app.config import Settings\n",
    "from app.core.llm_key_provider import get_api_key as _get_db_key, get_provider_keys as _get_provider_keys\n",
    "from app.services.model_registry import get_executable_model_ids as _get_registry_executable_model_ids\n",
    "from app.services.model_registry import list_registered_models as _list_registered_models\n",
    "from app.services.model_registry import normalize_provider as _normalize_registry_provider\n",
    "from app.services.intent_router import IntentResult\n",
    "import re as _re_mod\n",  # line 133
]:
    content = content.replace(imp, imp.rstrip("\n") + "  # noqa: E402\n", 1)

# Line 216: multi-line import after _is_slot_available — first line only
content = content.replace(
    "from app.core.auth_provider import (\n",
    "from app.core.auth_provider import (  # noqa: E402\n",
    1,
)
# Line 222
content = content.replace(
    "from app.core.llm_key_provider import mark_key_rate_limited as _mark_key_rate_limited\n",
    "from app.core.llm_key_provider import mark_key_rate_limited as _mark_key_rate_limited  # noqa: E402\n",
    1,
)
# Line 223
content = content.replace(
    "from app.services.oauth_usage_tracker import log_usage as _log_oauth_usage\n",
    "from app.services.oauth_usage_tracker import log_usage as _log_oauth_usage  # noqa: E402\n",
    1,
)

# ─── E701: expand inline try/except (lines 187-192) ─────────────────────────
content = content.replace(
    "        try: return _time_mod.time() + float(ra)\n"
    "        except (ValueError, TypeError): pass\n",
    "        try:\n"
    "            return _time_mod.time() + float(ra)\n"
    "        except (ValueError, TypeError):\n"
    "            pass\n",
)
content = content.replace(
    "        try: return float(rr)\n"
    "        except (ValueError, TypeError): pass\n",
    "        try:\n"
    "            return float(rr)\n"
    "        except (ValueError, TypeError):\n"
    "            pass\n",
)

# ─── F841: sdk_model unused only in _stream_cli_relay (NOT in _once) ─────────
# Unique context: the retry-wrapper function has retry_messages right after sdk_model
content = content.replace(
    ") -> AsyncGenerator[Dict[str, Any], None]:\n"
    "    sdk_model = _ANTHROPIC_MODEL_ID.get(model, model)\n"
    "    retry_messages = messages\n",
    ") -> AsyncGenerator[Dict[str, Any], None]:\n"
    "    retry_messages = messages\n",
    1,  # only first match — _stream_cli_relay
)

# ─── F841: result_text unused (intentionally skipped per comment) ─────────────
content = content.replace(
    '        result_text = event.get("result", "")\n',
    "",
    1,
)

assert content != original, "No changes made — check patterns"

with open(fpath, "w") as f:
    f.write(content)

print("Done.")
