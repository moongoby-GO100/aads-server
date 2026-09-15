"""244(진아) 서버의 LLM 계정을 공급사 API 로 직접 조회한다.

2026-09-15 대표님: "진아서버에 llm 계정들이 많이 추가되었다고 한다.
확인하고 리스트 요금제등 정리해서 보고하고 aads에 모두 반영해줘"

`/etc/biseo.env` 에 적혀 있다는 사실만으로는 **쓸 수 있는 계정인지 모른다.**
키가 폐기됐을 수도, 무료 티어가 소진됐을 수도 있다. 그래서 각 공급사의
계정/모델 엔드포인트를 실제로 호출해 살아 있는지와 요금제를 받아온다.

**키 값은 절대 출력하지 않는다.** 길이와 SHA-256 앞 12자리 지문만 찍는다.
지문은 AADS 에 이미 등록된 키와 같은 것인지 비교하는 데 쓴다.
"""

from __future__ import annotations

import hashlib
import json
import os
import sys
import urllib.error
import urllib.request

ENV_PATH = "/etc/biseo.env"
TIMEOUT = 20

# (env 키, 표시명, 종류, 확인 URL, 인증 방식)
#   인증 방식: bearer | xapi | query | none
PROBES = [
    ("BISEO_KEY_OPENROUTER", "OpenRouter", "LLM 라우터",
     "https://openrouter.ai/api/v1/key", "bearer"),
    ("BISEO_KEY_GROQ", "Groq", "LLM 추론",
     "https://api.groq.com/openai/v1/models", "bearer"),
    ("BISEO_KEY_CEREBRAS", "Cerebras", "LLM 추론",
     "https://api.cerebras.ai/v1/models", "bearer"),
    ("BISEO_KEY_TOGETHER", "Together AI", "LLM 추론",
     "https://api.together.xyz/v1/models", "bearer"),
    ("BISEO_KEY_MISTRAL", "Mistral", "LLM",
     "https://api.mistral.ai/v1/models", "bearer"),
    ("BISEO_KEY_DEEPSEEK", "DeepSeek", "LLM",
     "https://api.deepseek.com/user/balance", "bearer"),
    ("BISEO_KEY_NVIDIA", "NVIDIA NIM", "LLM 추론",
     "https://integrate.api.nvidia.com/v1/models", "bearer"),
    ("BISEO_KEY_SAMBANOVA", "SambaNova", "LLM 추론",
     "https://api.sambanova.ai/v1/models", "bearer"),
    ("BISEO_KEY_HUGGINGFACE", "HuggingFace", "모델 허브",
     "https://huggingface.co/api/whoami-v2", "bearer"),
    ("BISEO_KEY_GEMINI", "Google Gemini #1", "LLM",
     "https://generativelanguage.googleapis.com/v1beta/models", "query"),
    ("BISEO_KEY_GEMINI2", "Google Gemini #2", "LLM",
     "https://generativelanguage.googleapis.com/v1beta/models", "query"),
    ("BISEO_KEY_TAVILY", "Tavily", "웹검색 API",
     "https://api.tavily.com/usage", "bearer"),
]


def load_env(path: str) -> dict[str, str]:
    out: dict[str, str] = {}
    with open(path, "r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            out[k.strip()] = v.strip().strip('"').strip("'")
    return out


def fingerprint(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()[:12]


def call(url: str, key: str, auth: str) -> tuple[int, str]:
    if auth == "query":
        url = f"{url}?key={key}"
        req = urllib.request.Request(url)
    else:
        req = urllib.request.Request(url)
        req.add_header("Authorization", f"Bearer {key}")
    req.add_header("User-Agent", "aads-llm-account-probe/1.0")
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
            return resp.status, resp.read(200_000).decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read(4000).decode("utf-8", "replace")
    except Exception as exc:  # noqa: BLE001
        return 0, f"{type(exc).__name__}: {exc}"


def summarize(name: str, status: int, body: str) -> str:
    """응답에서 **요금제에 해당하는 사실만** 뽑는다. 없으면 없다고 적는다."""
    if status != 200:
        snippet = body.replace("\n", " ")[:120]
        return f"실패 — {snippet}"
    try:
        data = json.loads(body)
    except Exception:  # noqa: BLE001
        return f"OK (본문 {len(body):,}B, JSON 아님)"

    if name == "OpenRouter":
        d = data.get("data", {})
        limit = d.get("limit")
        usage = d.get("usage")
        free = d.get("is_free_tier")
        rl = d.get("rate_limit") or {}
        limit_s = "무제한(크레딧 한도 없음)" if limit is None else f"${limit}"
        return (f"티어={'무료' if free else '유료'} · 한도={limit_s} · "
                f"사용액=${usage} · rate={rl.get('requests')}/{rl.get('interval')}")
    if name == "DeepSeek":
        infos = data.get("balance_infos") or []
        if infos:
            b = infos[0]
            return (f"잔액 {b.get('total_balance')} {b.get('currency')} "
                    f"(충전 {b.get('granted_balance')}/유료 {b.get('topped_up_balance')}) · "
                    f"available={data.get('is_available')}")
        return f"available={data.get('is_available')} · 잔액정보 없음"
    if name == "HuggingFace":
        plan = data.get("plan") or (data.get("auth") or {}).get("accessToken", {}).get("role")
        return (f"계정={data.get('name')} · type={data.get('type')} · "
                f"plan={plan or '미표기(무료)'} · canPay={data.get('canPay')}")
    if name == "Tavily":
        return json.dumps(data, ensure_ascii=False)[:200]

    models = data.get("data") if isinstance(data.get("data"), list) else data.get("models")
    if isinstance(models, list):
        sample = []
        for m in models[:3]:
            if isinstance(m, dict):
                sample.append(str(m.get("id") or m.get("name") or "")[:40])
        return f"모델 {len(models)}개 접근 가능 · 예: {', '.join(x for x in sample if x)}"
    return f"OK (키 {list(data)[:5]})"


def main() -> int:
    if not os.path.isfile(ENV_PATH):
        print(f"ABORT: {ENV_PATH} 없음")
        return 1
    env = load_env(ENV_PATH)

    print(f"{'공급사':<18} {'종류':<10} {'상태':<6} {'지문':<14} 요금제/한도")
    print("-" * 110)
    rows = []
    for env_key, name, kind, url, auth in PROBES:
        val = env.get(env_key)
        if not val:
            print(f"{name:<18} {kind:<10} {'없음':<6} {'-':<14} {env_key} 미설정")
            continue
        status, body = call(url, val, auth)
        fp = fingerprint(val)
        mark = "OK" if status == 200 else str(status)
        info = summarize(name, status, body)
        print(f"{name:<18} {kind:<10} {mark:<6} {fp:<14} {info}")
        rows.append({
            "env_key": env_key, "name": name, "kind": kind,
            "status": status, "fingerprint": fp, "len": len(val),
            "info": info,
        })

    # Claude 구독 토큰은 위 표와 성격이 달라 따로 적는다.
    tok = env.get("CLAUDE_CODE_OAUTH_TOKEN")
    if tok:
        print("-" * 110)
        print(f"{'Claude Code':<18} {'구독 OAuth':<10} {'-':<6} {fingerprint(tok):<14} "
              f"sk-ant-oat (len={len(tok)}) — AADS 등록 여부는 지문으로 대조")

    print("\n@@JSON@@")
    print(json.dumps(rows, ensure_ascii=False))
    return 0


sys.exit(main())
