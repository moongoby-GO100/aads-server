#!/usr/bin/env python3
"""pipeline_runner.py 리스트 API에 model, size 필드 추가"""

path = "/root/aads/aads-server/app/api/pipeline_runner.py"

with open(path, "r", encoding="utf-8") as f:
    content = f.read()

# 백업
with open(path + ".bak_model", "w", encoding="utf-8") as f:
    f.write(content)

# Patch 1: SELECT에 model, size 추가
old1 = "                   started_at, depends_on, chat_session_id, worker_model, actual_model\n            FROM pipeline_jobs"
new1 = "                   started_at, depends_on, chat_session_id, model, worker_model, actual_model, size\n            FROM pipeline_jobs"

if old1 not in content:
    print("ERROR: Patch 1 old_string not found")
    exit(1)
content = content.replace(old1, new1, 1)

# Patch 2: response dict에 model, size 추가
old2 = '            "depends_on": r.get("depends_on"),\n            "worker_model": r.get("worker_model") or "",\n            "actual_model": r.get("actual_model") or "",\n        }\n        for r in rows\n    ]'
new2 = '            "depends_on": r.get("depends_on"),\n            "model": r.get("model") or "",\n            "worker_model": r.get("worker_model") or "",\n            "actual_model": r.get("actual_model") or "",\n            "size": r.get("size") or "M",\n        }\n        for r in rows\n    ]'

if old2 not in content:
    print("ERROR: Patch 2 old_string not found")
    exit(1)
content = content.replace(old2, new2, 1)

with open(path, "w", encoding="utf-8") as f:
    f.write(content)

print("패치 완료!")
print("  - SELECT: model, size 추가")
print("  - response dict: model, size 추가")
