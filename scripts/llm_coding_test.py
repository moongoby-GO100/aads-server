#!/usr/bin/env python3
"""Gemini 2.5 Pro vs MiniMax M2.7 코딩 테스트 — AADS 실제 코드 기반
   (Sonnet 4.6 Anthropic 429 rate-limit → Gemini 2.5 Pro 대체)
"""
import json, time, os
import urllib.request

LITELLM_URL = os.environ.get("LITELLM_BASE_URL", "http://aads-litellm:4000") + "/v1/chat/completions"
LITELLM_KEY = os.environ.get("LITELLM_MASTER_KEY", "sk-litellm")
MODELS = ["gemini-2.5-pro", "minimax-m2.7"]

PROBLEMS = [
    {
        "id": "T1",
        "title": "bare except 수정 (Easy)",
        "prompt": """다음 Python 코드에서 bare `except: pass`를 올바르게 수정하세요.
SystemExit, KeyboardInterrupt 등이 무시되지 않도록 적절한 예외 타입을 지정하세요.
수정된 함수 전체를 반환하세요. 설명 없이 코드만.

```python
def _parse_rl_reset_ms(headers) -> float | None:
    if not headers:
        return None
    ra = headers.get("retry-after") or headers.get("Retry-After")
    if ra:
        try: return _time_mod.time() + float(ra)
        except: pass
    rr = headers.get("x-ratelimit-reset") or headers.get("X-RateLimit-Reset")
    if rr:
        try: return float(rr)
        except: pass
    return None
```"""
    },
    {
        "id": "T2",
        "title": "_check_resolved() 구현 (Medium)",
        "prompt": """다음은 AADS 에스컬레이션 엔진의 미구현 함수입니다.
이 함수가 실제로 이슈 해결 여부를 확인하도록 구현하세요.

사용 가능한 리소스:
- `asyncpg` DB 풀: `from app.core.database import get_pool`
- DB 테이블: `directive_lifecycle` (컬럼: directive_id, status, updated_at)
- DB 테이블: `pipeline_jobs` (컬럼: job_id, status, project)
- `issue_data`에는 `directive_id`, `job_id`, `service_name` 등이 포함될 수 있음
- `issue_type`은 "service_down", "job_stuck", "directive_failed" 중 하나

수정된 함수 전체를 반환하세요. 설명 없이 코드만.

```python
async def _check_resolved(issue_type: str, issue_data: dict) -> bool:
    # TODO: Implement actual resolution checking. Currently always returns False.
    # Possible: check if PID is gone, verify service health,
    # query directive_lifecycle for status changes, etc.
    return False
```"""
    },
    {
        "id": "T3",
        "title": "circuit_breaker 풀 리팩터링 (Medium)",
        "prompt": """다음 코드는 매 호출마다 `asyncpg.connect()`로 새 DB 연결을 생성합니다.
이를 커넥션 풀을 사용하도록 리팩터링하세요.

사용 가능:
- `from app.core.database import get_pool` — 앱 전역 asyncpg 풀 반환 (Pool | None)
- 풀이 None이면 기존 connect 방식 폴백

수정된 함수 전체를 반환하세요. 설명 없이 코드만.

```python
async def check_circuit(server: str) -> bool:
    db_url = _db_url()
    if not db_url:
        return True
    try:
        import asyncpg
        conn = await asyncpg.connect(db_url, timeout=5)
        try:
            row = await conn.fetchrow(
                "SELECT state, failure_count, cooldown_until FROM circuit_breaker_state WHERE server=$1",
                server,
            )
            if not row:
                return True
            state = row["state"]
            cooldown_until = row["cooldown_until"]
            now = datetime.now()
            if state == "closed":
                return True
            if state == "open":
                if cooldown_until and now > cooldown_until:
                    await conn.execute(
                        "UPDATE circuit_breaker_state SET state='half_open', updated_at=NOW() WHERE server=$1",
                        server,
                    )
                    return True
                return False
            if state == "half_open":
                return False
            return True
        finally:
            await conn.close()
    except Exception as e:
        logger.warning("check_circuit_error", server=server, error=str(e))
        return True
```"""
    },
]


def call_model(model, prompt, timeout=90):
    payload = json.dumps({
        "model": model,
        "messages": [
            {"role": "system", "content": "You are an expert Python developer. Return only code, no explanations."},
            {"role": "user", "content": prompt}
        ],
        "max_tokens": 2000,
        "temperature": 0.0,
    }).encode()
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {LITELLM_KEY}",
    }
    req = urllib.request.Request(LITELLM_URL, data=payload, headers=headers, method="POST")
    t0 = time.time()
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read())
        elapsed = round(time.time() - t0, 2)
        content = data["choices"][0]["message"].get("content") or ""
        # Gemini thinking model: content가 None이면 parts에서 추출
        if not content and "parts" in data["choices"][0]["message"]:
            parts = data["choices"][0]["message"]["parts"]
            content = "\n".join(p.get("text", "") for p in parts if p.get("text"))
        usage = data.get("usage", {})
        return {
            "ok": True, "content": content, "sec": elapsed,
            "tok_in": usage.get("prompt_tokens", 0),
            "tok_out": usage.get("completion_tokens", 0),
        }
    except Exception as e:
        return {"ok": False, "content": str(e)[:300], "sec": round(time.time() - t0, 2), "tok_in": 0, "tok_out": 0}


def extract_code(text):
    """마크다운 코드블록에서 코드 추출"""
    import re
    m = re.search(r"```(?:python)?\s*\n(.*?)```", text, re.DOTALL)
    return m.group(1).strip() if m else text.strip()


def score_t1(code):
    """T1 채점: bare except 제거, ValueError/TypeError 지정"""
    s = 0
    if "except:" not in code.replace("except (", "").replace("except V", "").replace("except T", ""):
        s += 30  # bare except 제거
    if "ValueError" in code:
        s += 25
    if "TypeError" in code:
        s += 25
    if "_parse_rl_reset_ms" in code and "return None" in code:
        s += 20  # 함수 구조 유지
    return min(s, 100)


def score_t2(code):
    """T2 채점: 분기 구현, DB 조회, 에러 처리"""
    s = 0
    if "service_down" in code:
        s += 15
    if "job_stuck" in code:
        s += 15
    if "directive_failed" in code:
        s += 15
    if "get_pool" in code or "asyncpg" in code:
        s += 20  # DB 연결
    if "fetchrow" in code or "fetchval" in code or "fetch" in code:
        s += 15  # 실제 쿼리
    if "try" in code and "except" in code:
        s += 10  # 에러 처리
    if "async def" in code:
        s += 10  # async 유지
    return min(s, 100)


def score_t3(code):
    """T3 채점: 풀 사용, 폴백, 리소스 정리"""
    s = 0
    if "get_pool" in code:
        s += 25  # 풀 사용
    if "pool.acquire" in code or "async with" in code:
        s += 25  # context manager
    if "asyncpg.connect" in code:
        s += 15  # 폴백 유지
    if "finally" in code or "async with" in code:
        s += 15  # 리소스 정리
    if "check_circuit" in code and "server" in code:
        s += 10  # 함수 시그니처 유지
    if "half_open" in code:
        s += 10  # 로직 유지
    return min(s, 100)


SCORERS = {"T1": score_t1, "T2": score_t2, "T3": score_t3}


def main():
    results = {}
    for model in MODELS:
        results[model] = {}
        for prob in PROBLEMS:
            print(f"\n{'='*60}")
            print(f"[{prob['id']}] {prob['title']} — {model}")
            print(f"{'='*60}")
            r = call_model(model, prob["prompt"])
            if r["ok"]:
                code = extract_code(r["content"])
                score = SCORERS[prob["id"]](code)
                r["score"] = score
                r["code"] = code
                print(f"  Time: {r['sec']}s | Tokens: {r['tok_in']}→{r['tok_out']} | Score: {score}/100")
                print(f"  Code ({len(code)} chars):\n{code[:600]}")
            else:
                r["score"] = 0
                r["code"] = ""
                print(f"  FAILED ({r['sec']}s): {r['content'][:200]}")
            results[model][prob["id"]] = r

    # 최종 비교표
    print(f"\n\n{'='*80}")
    print("FINAL SCORECARD — Gemini 2.5 Pro vs MiniMax M2.7")
    print(f"{'='*80}")
    print(f"{'Problem':<12} {'Model':<18} {'Score':>6} {'Time':>7} {'Tokens':>14} {'Chars':>7}")
    print("-" * 80)
    totals = {m: 0 for m in MODELS}
    total_time = {m: 0.0 for m in MODELS}
    for prob in PROBLEMS:
        for model in MODELS:
            r = results[model][prob["id"]]
            tok = f"{r['tok_in']}→{r['tok_out']}" if r["ok"] else "-"
            chars = len(r.get("code", ""))
            score = r.get("score", 0)
            totals[model] += score
            total_time[model] += r["sec"]
            print(f"{prob['id']:<12} {model:<18} {score:>5}/100 {r['sec']:>6}s {tok:>14} {chars:>6}c")
    
    print("-" * 80)
    for model in MODELS:
        print(f"{'TOTAL':<12} {model:<18} {totals[model]:>5}/300 {total_time[model]:>6.1f}s")
    
    print(f"\nWinner: {max(totals, key=totals.get)} ({totals[max(totals, key=totals.get)]}/300)")
    
    with open("/tmp/llm_test_results.json", "w") as f:
        json.dump(results, f, ensure_ascii=False, indent=2)
    print("Saved: /tmp/llm_test_results.json")


if __name__ == "__main__":
    main()
