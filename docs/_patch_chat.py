import sys
path = "/root/aads/aads-server/app/services/chat_service.py"
with open(path, "r") as f:
    content = f.read()
old = '        logger.debug("bg_fact_extraction_launch_error", error=str(_bg_err))\n        # F8: CEO Pattern Tracking'
new = '        logger.debug("bg_fact_extraction_launch_error", error=str(_bg_err))\n        # F-LLMOps: Chat trace -> llmops_traces\n        try:\n            from app.services.llmops_chat_hook import record_chat_trace\n            _lv = locals()\n            _bg_asyncio.create_task(record_chat_trace(\n                session_id=session_id,\n                execution_id=_lv.get(\'_execution_id_str\'),\n                project=_normalized_project,\n                user_message=content or "",\n                ai_response=full_response or "",\n                model=_lv.get(\'model_used\'),\n                duration_sec=_duration_sec,\n                cost=_lv.get(\'cost\'),\n                tokens_in=_lv.get(\'tokens_in\', 0),\n                tokens_out=_lv.get(\'tokens_out\', 0),\n                tools_called=tools_called,\n                intent=_lv.get(\'intent\'),\n            ))\n        except Exception:\n            pass\n        # F8: CEO Pattern Tracking'
if old in content:
    content = content.replace(old, new, 1)
    with open(path, "w") as f:
        f.write(content)
    print("OK chat_service.py patched")
elif "llmops_chat_hook" in content:
    print("SKIP already patched")
else:
    print("ERR anchor not found")
