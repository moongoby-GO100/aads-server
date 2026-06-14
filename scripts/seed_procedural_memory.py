"""P1: procedural_memory 초기 시드 데이터."""
import asyncio
import json
import os
import asyncpg


async def seed():
    pool = await asyncpg.create_pool(
        os.getenv("DATABASE_URL", "postgresql://aads:aads@postgres:5432/aads")
    )
    async with pool.acquire() as conn:
        procedures = [
            (
                "AADS 무중단 배포",
                ["코드 수정 완료", "구문 검사 python3 -m py_compile", "reload-api.sh 실행", "health-check 확인", "git commit+push"],
                0.92, 47, "deployment", "AADS",
            ),
            (
                "파이프라인 러너 운영",
                ["pipeline_runner_submit 제출", "status 확인(1-3분 대기)", "awaiting_approval 시 diff 확인", "approve/reject 판단", "배포 검증"],
                0.85, 32, "pipeline", "AADS",
            ),
            (
                "DB 마이그레이션 안전",
                ["백업 테이블 생성", "ALTER/INSERT 실행", "검증 쿼리 실행", "롤백 스크립트 준비", "서비스 영향 확인"],
                0.88, 15, "database", "AADS",
            ),
            (
                "버그 디버깅 플로우",
                ["에러 로그 확인(docker logs/search_logs)", "관련 코드 read_remote_file", "원인 특정", "patch_remote_file 수정", "테스트 실행", "배포"],
                0.78, 28, "debugging", "AADS",
            ),
            (
                "KIS 전략 백테스트",
                ["전략 파라미터 정의", "과거 데이터 로드", "시뮬레이션 실행", "수익률/MDD/승률 계산", "결과 리포트"],
                0.82, 12, "analysis", "KIS",
            ),
            (
                "CEO 보고서 작성",
                ["DB/도구로 실측 데이터 수집", "표/차트 구성", "결론 선행 배치", "다음 단계 제시", "출처 태그 부착"],
                0.90, 55, "reporting", "AADS",
            ),
            (
                "NTV2 프론트엔드 수정",
                ["소스 코드 확인(read_remote_file)", "컴포넌트 구조 파악", "patch_remote_file 수정", "빌드(npm run build)", "배포(docker compose up -d)", "브라우저 검증"],
                0.75, 18, "frontend", "NTV2",
            ),
        ]
        inserted = 0
        for name, steps, rate, count, ptype, agent in procedures:
            result = await conn.execute(
                """
                INSERT INTO procedural_memory (procedure_name, steps, success_rate, execution_count, procedure_type, agent_name, content)
                VALUES ($1, $2::jsonb, $3, $4, $5, $6, '{}'::jsonb)
                ON CONFLICT DO NOTHING
                """,
                name, json.dumps(steps, ensure_ascii=False), rate, count, ptype, agent,
            )
            if "INSERT" in result:
                inserted += 1
        cnt = await conn.fetchval("SELECT count(*) FROM procedural_memory")
        print(f"Seeded {inserted} new procedures. Total: {cnt}")
    await pool.close()


if __name__ == "__main__":
    asyncio.run(seed())
