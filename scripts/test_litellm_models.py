import urllib.request, json, os

key = os.environ.get('LITELLM_MASTER_KEY', '')
base = 'http://aads-litellm:4000/v1/chat/completions'

models = [
    # Pipeline runner core (priority)
    'kimi-k2.5',
    'minimax-m2.7',
    'gemini-2.5-flash',
    'deepseek-chat',
    'qwen3-235b',
    # Gemini family
    'gemini-2.5-flash-lite',
    'gemini-2.5-pro',
    'gemini-3-pro-preview',
    'gemini-3-flash-preview',
    'gemini-3.1-pro-preview',
    'gemini-3.1-flash-lite-preview',
    'gemini-flash-lite',
    'gemini-flash',
    'gemini-pro',
    'gemma-3-27b-it',
    # DeepSeek
    'deepseek-chat',
    'deepseek-reasoner',
    # Groq
    'groq-llama-70b',
    'groq-llama-8b',
    'groq-llama4-maverick',
    'groq-llama4-scout',
    'groq-qwen3-32b',
    'groq-kimi-k2',
    'groq-gpt-oss-120b',
    'groq-compound',
    # Claude
    'claude-sonnet',
    'claude-haiku',
    'claude-opus',
    'claude-sonnet-4-6',
    'claude-haiku-4-5',
    # Qwen
    'qwen-turbo',
    'qwen-plus',
    'qwen-max',
    'qwen-flash',
    'qwen3-8b',
    'qwen3-32b',
    'qwen3-235b-instruct',
    'qwen3-max',
    'qwq-plus',
    'dashscope-deepseek-v3.2',
    # OpenRouter
    'openrouter-grok-4-fast',
    'openrouter-deepseek-v3',
    'openrouter-mistral-small',
    'openrouter-nemotron-free',
    'openrouter-minimax-m2',
    # Kimi
    'kimi-k2',
    'kimi-latest',
    'kimi-128k',
    'kimi-8k',
    # MiniMax
    'minimax-m2.5',
]

# deduplicate while preserving order
seen = set()
unique_models = []
for m in models:
    if m not in seen:
        seen.add(m)
        unique_models.append(m)

results = []
for m in unique_models:
    body = json.dumps({
        'model': m,
        'messages': [{'role': 'user', 'content': '1+1=?'}],
        'max_tokens': 15
    }).encode()
    req = urllib.request.Request(
        base, data=body,
        headers={'Content-Type': 'application/json', 'Authorization': f'Bearer {key}'}
    )
    try:
        with urllib.request.urlopen(req, timeout=45) as r:
            d = json.loads(r.read())
            choices = d.get('choices', [])
            if choices:
                msg = choices[0].get('message', {})
                content = msg.get('content') or msg.get('reasoning_content', '')
                finish = choices[0].get('finish_reason', '')
                snippet = str(content).strip()[:100]
                results.append(('OK', m, f"finish={finish} | {snippet}"))
            else:
                results.append(('OK?', m, f"no choices: {json.dumps(d)[:150]}"))
    except urllib.error.HTTPError as e:
        err = e.read().decode()[:200]
        results.append(('ERROR', m, f"HTTP {e.code}: {err}"))
    except Exception as e:
        results.append(('ERROR', m, str(e)[:200]))

print(f"\n{'MODEL':<40} {'STATUS':<8} DETAIL")
print('-' * 130)
for status, model, detail in results:
    print(f"{model:<40} {status:<8} {detail}")

ok = sum(1 for s, _, _ in results if s == 'OK')
err = sum(1 for s, _, _ in results if s == 'ERROR')
print(f"\nSUMMARY: {ok} OK / {err} ERROR / {len(results)} total")
