#!/usr/bin/env python3
"""kimi-k2.5 thinking 비활성화 패치 (litellm-config.yaml)"""

path = "/root/aads/aads-server/litellm-config.yaml"

with open(path, "r", encoding="utf-8") as f:
    content = f.read()

# 백업
with open(path + ".bak_thinking", "w", encoding="utf-8") as f:
    f.write(content)

old = """# P0: kimi-k2.5 moonshot 프로바이더로 통일 (openai/ 경유 시 인증 에러)
- model_name: kimi-k2.5
  litellm_params:
    model: moonshot/kimi-k2.5
    api_key: os.environ/KIMI_API_KEY"""

new = """# P0: kimi-k2.5 moonshot 프로바이더로 통일 (openai/ 경유 시 인증 에러)
# P1: thinking 비활성화 — tool call 시 reasoning_content 누락 에러 방지
- model_name: kimi-k2.5
  litellm_params:
    model: moonshot/kimi-k2.5
    api_key: os.environ/KIMI_API_KEY
    extra_body:
      thinking:
        type: disabled"""

assert old in content, "old string not found"
content = content.replace(old, new, 1)

with open(path, "w", encoding="utf-8") as f:
    f.write(content)

print("패치 완료: kimi-k2.5 thinking 비활성화")
