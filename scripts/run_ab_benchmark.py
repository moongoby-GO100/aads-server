#!/usr/bin/env python3
"""
AADS-207: A/B 벤치마크 실행 스크립트
25문제 x 5모델 동시 비교 테스트

실행: docker exec aads-server python3 scripts/run_ab_benchmark.py
"""
import asyncio
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.services.ab_test_problems import PROBLEMS
from app.services.ab_test_runner import run_ab_comparison, blind_judge

MODELS = [
    "claude-opus",
    "claude-sonnet",
    "qwen3-coder-next",
    "qwen3.6-plus",
    "deepseek-v3.2",
]


async def main():
    print("=== A/B 벤치마크 시작: {}문제 x {}모델 ===".format(len(PROBLEMS), len(MODELS)))
    batch_ids = []

    for i, prob in enumerate(PROBLEMS):
        print("[{}/{}] Lv{}: {}".format(i + 1, len(PROBLEMS), prob["level"], prob["title"]))
        records = await run_ab_comparison(
            prompt=prob["prompt"],
            models=MODELS,
            test_tier="P{}".format(min(prob["level"], 3)),
            difficulty=prob["level"],
            test_type="coding_bench",
        )
        if records:
            batch_ids.append(records[0]["batch_id"])
            for r in records:
                status = "OK" if not r.get("error") else "ERR: {}".format(str(r["error"])[:50])
                print("  {:30s} {:6d}ms  {}".format(r["model_name"], r.get("latency_ms", 0), status))

        await asyncio.sleep(2)  # rate limit 방지

    print("\n=== Blind Judge 채점 시작 ({} 배치) ===".format(len(batch_ids)))
    for bid in batch_ids:
        await blind_judge(bid)
        await asyncio.sleep(1)

    print("\n=== 완료 ===")
    print("DB 리포트 조회:")
    print("  SELECT model_name, COUNT(*) AS cnt, ROUND(AVG(judge_score),2) AS avg_score,")
    print("         ROUND(AVG(latency_ms)) AS avg_ms")
    print("  FROM ab_test_log GROUP BY model_name ORDER BY avg_score DESC;")


if __name__ == "__main__":
    asyncio.run(main())
