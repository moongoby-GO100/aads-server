#!/usr/bin/env python3
"""ChatArtifactPanel.tsx 패치: model 필드 추가 + 렌더링 개선"""

path = "/root/aads/aads-dashboard/src/app/chat/ChatArtifactPanel.tsx"

with open(path, "r", encoding="utf-8") as f:
    content = f.read()

# 백업
with open(path + ".bak_aads", "w", encoding="utf-8") as f:
    f.write(content)

# Patch 1: RunnerJob 인터페이스에 model, size 필드 추가
old1 = "  worker_model?: string;\n  actual_model?: string;"
new1 = "  model?: string;\n  worker_model?: string;\n  actual_model?: string;\n  size?: string;"
assert old1 in content, "Patch 1: old string not found"
content = content.replace(old1, new1, 1)

# Patch 2: 렌더링 — actual_model 중복 제거 + model fallback 배지 추가
old2 = '                                  {job.actual_model && <span style={{ fontSize: "9px", background: "rgba(34,197,94,0.2)", color: "#4ade80", borderRadius: "3px", padding: "1px 4px", whiteSpace: "nowrap" }}>🤖 {job.actual_model}</span>}'
new2 = (
    '                                  {job.actual_model && job.actual_model !== job.worker_model && '
    '<span style={{ fontSize: "9px", background: "rgba(34,197,94,0.2)", color: "#4ade80", borderRadius: "3px", padding: "1px 4px", whiteSpace: "nowrap" }}>🤖 {job.actual_model}</span>}\n'
    '                                  {!job.worker_model && !job.actual_model && job.model && '
    '<span style={{ fontSize: "9px", background: "rgba(156,163,175,0.2)", color: "#9ca3af", borderRadius: "3px", padding: "1px 4px", whiteSpace: "nowrap" }}>📍 {job.model}</span>}'
)
assert old2 in content, "Patch 2: old string not found"
content = content.replace(old2, new2, 1)

with open(path, "w", encoding="utf-8") as f:
    f.write(content)

print("패치 완료!")
print(f"  - RunnerJob 인터페이스: model?, size? 필드 추가")
print(f"  - 렌더링: actual_model 중복 제거 + model fallback 배지 (회색)")
