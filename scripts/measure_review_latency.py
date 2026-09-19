"""리뷰 LLM 시도당 상한(_REVIEW_LLM_TIMEOUT_SEC)을 고치기 전에 실제 지연을 잰다.

code_reviewer.py 주석의 경고 그대로다 — "상한을 줄일 때는 반드시 실제 리뷰
프롬프트로 지연을 먼저 재라." 늘릴 때도 같다. 감으로 올리면 왜 그 값인지
다음 사람이 알 수 없다.

지정한 잡의 실제 diff 로 등록된 리뷰 모델 각각의 응답 시간을 잰다.
DB 에는 아무것도 쓰지 않는다 — diff 를 읽고 _call_review_model 만 부른다.

    docker exec aads-server python3 /app/scripts/measure_review_latency.py <job_id>
"""
import asyncio
import os
import sys
import time

sys.path.insert(0, "/app")


async def main() -> int:
    job_id = sys.argv[1] if len(sys.argv) > 1 else "runner-1b282742"
    from app.core.db_pool import close_pool, get_pool, init_pool
    from app.services import code_reviewer as cr

    # 풀을 먼저 띄운다. 안 띄우면 _get_review_models() 가 조용히 실패해
    # 기본값 하나(qwen-turbo)만 돌려주고, 정작 재려던 모델을 재지 못한다.
    await init_pool()
    try:
        pool = get_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT git_diff, instruction FROM pipeline_jobs WHERE job_id=$1", job_id
            )
        if not row or not row["git_diff"]:
            print(f"no diff for {job_id}")
            return 1

        diff = row["git_diff"]
        truncated, was_cut = cr._truncate_diff_for_review(diff)
        models = await cr._get_review_models()
        print(f"job={job_id} diff_bytes={len(diff)} "
              f"prompt_diff_bytes={len(truncated)} truncated={was_cut}")
        print(f"models={models}")
        print(f"limits: per_attempt={cr._REVIEW_LLM_TIMEOUT_SEC}s "
              f"sync_deadline={cr._REVIEW_TOTAL_DEADLINE_SEC}s "
              f"async_deadline={cr._REVIEW_ASYNC_DEADLINE_SEC}s")

        prompt = (
            "다음 git diff 를 검토하고 JSON 으로 판정하세요.\n"
            '{"verdict":"APPROVE|REQUEST_CHANGES|FLAG","score":0.0,"reasons":[]}\n\n'
            f"[지시서]\n{(row['instruction'] or '')[:4000]}\n\n[diff]\n{truncated}"
        )

        # 상한을 넉넉히 풀고 잰다. 45초에 잘리는 것이 '무응답'인지 '느린 것'인지
        # 구분하려면 잘리지 않게 두고 끝까지 기다려 봐야 한다.
        probe_timeout = float(os.environ.get("PROBE_TIMEOUT_SEC", "180"))
        for model in models:
            started = time.monotonic()
            try:
                text = await asyncio.wait_for(
                    cr._call_review_model(
                        model=model,
                        prompt=prompt,
                        system="너는 엄격한 코드 리뷰어다. JSON 만 출력한다.",
                        max_tokens=2000,
                    ),
                    timeout=probe_timeout,
                )
                elapsed = time.monotonic() - started
                size = len(text or "")
                print(f"  {model:32s} {elapsed:7.1f}s chars={size} {'OK' if size else 'EMPTY'}")
            except asyncio.TimeoutError:
                print(f"  {model:32s} {probe_timeout:7.1f}s TIMEOUT(probe limit)")
            except Exception as exc:  # noqa: BLE001 - 측정 스크립트, 원문 그대로 보여준다
                elapsed = time.monotonic() - started
                print(f"  {model:32s} {elapsed:7.1f}s ERROR {str(exc)[:140]}")
        return 0
    finally:
        await close_pool()


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
