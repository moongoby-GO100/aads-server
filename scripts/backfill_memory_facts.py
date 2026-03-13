"""
과거 대화에서 memory_facts 백필.
user+assistant 쌍을 Haiku로 분석하여 핵심사실 추출.

실행: docker exec aads-server python3 /app/scripts/backfill_memory_facts.py
"""
import asyncio
import json
import os
import sys
import uuid
from datetime import datetime

# 앱 경로 설정
sys.path.insert(0, "/app")

MAX_FACTS_PER_TURN = 5
BATCH_SIZE = 10  # 동시 처리 수
DELAY_BETWEEN_BATCHES = 0.5  # 초
USE_GEMINI = True  # Gemini Flash 사용 (LiteLLM 경유)

EXTRACTION_PROMPT = """다음 대화 턴에서 핵심 사실을 최대 {max_facts}건 추출하세요.

카테고리:
- decision: CEO가 내린 결정/지시
- file_change: 코드/파일 변경 사항
- config_change: 설정/환경 변경
- error_resolution: 에러 해결 방법
- ceo_instruction: CEO의 운영 지침
- error_pattern: 실패→성공 패턴
- timeline_event: 프로젝트 마일스톤/이벤트

JSON 배열로 반환. 각 항목:
{{"category": "...", "subject": "50자 이내 요약", "detail": "상세 설명 100자 이내", "tags": ["태그1"]}}

워크스페이스: {workspace}
시각: {timestamp}

대화 턴:
사용자: {user_msg}
AI: {ai_msg}

중요하지 않은 인사/잡담/단순확인은 건너뛰고 빈 배열 [] 반환.
JSON만 반환하세요. 마크다운 코드블록 없이."""


async def main():
    import asyncpg
    import httpx

    dsn = os.getenv("DATABASE_URL", "postgresql://aads:aads@aads-postgres:5432/aads")
    pool = await asyncpg.create_pool(dsn, min_size=2, max_size=5)

    # Gemini Flash 직접 호출
    api_key = os.getenv("GOOGLE_API_KEY") or os.getenv("GEMINI_API_KEY", "")
    client = httpx.AsyncClient(timeout=30.0)
    client._gemini_key = api_key

    # 대화 쌍 추출: user → 바로 다음 assistant
    async with pool.acquire() as conn:
        pairs = await conn.fetch("""
            WITH numbered AS (
                SELECT id, session_id, role, content, created_at,
                       ROW_NUMBER() OVER (PARTITION BY session_id ORDER BY created_at) AS rn
                FROM chat_messages
                WHERE role IN ('user', 'assistant')
                  AND content IS NOT NULL
                  AND LENGTH(content) > 30
                ORDER BY session_id, created_at
            )
            SELECT
                u.session_id,
                u.content AS user_content,
                a.content AS ai_content,
                u.created_at AS turn_time,
                w.name AS workspace_name
            FROM numbered u
            JOIN numbered a ON a.session_id = u.session_id AND a.rn = u.rn + 1 AND a.role = 'assistant'
            JOIN chat_sessions s ON s.id = u.session_id
            JOIN chat_workspaces w ON w.id = s.workspace_id
            WHERE u.role = 'user'
              AND LENGTH(u.content) > 20
              AND LENGTH(a.content) > 50
            ORDER BY u.created_at ASC
        """)

    total = len(pairs)
    print(f"총 {total}개 대화 쌍 발견. 백필 시작...")

    saved_total = 0
    skipped = 0
    errors = 0

    for batch_start in range(0, total, BATCH_SIZE):
        batch = pairs[batch_start:batch_start + BATCH_SIZE]
        tasks = []

        for pair in batch:
            tasks.append(extract_and_save(
                client, pool, pair,
            ))

        results = await asyncio.gather(*tasks, return_exceptions=True)

        for r in results:
            if isinstance(r, Exception):
                errors += 1
            elif isinstance(r, int):
                if r == 0:
                    skipped += 1
                else:
                    saved_total += r

        progress = min(batch_start + BATCH_SIZE, total)
        print(f"  [{progress}/{total}] 저장: {saved_total} | 스킵: {skipped} | 에러: {errors}")

        if batch_start + BATCH_SIZE < total:
            await asyncio.sleep(DELAY_BETWEEN_BATCHES)

    print(f"\n완료! 총 {saved_total}건 memory_facts 저장. 스킵: {skipped}, 에러: {errors}")

    # 임베딩 생성
    print("임베딩 생성 중...")
    await generate_embeddings(pool)

    await pool.close()


async def extract_and_save(client, pool, pair) -> int:
    """단일 대화 쌍에서 사실 추출 + 저장. Gemini Flash via LiteLLM."""
    user_msg = pair["user_content"][:500]
    ai_msg = pair["ai_content"][:2000]
    workspace = pair["workspace_name"] or "CEO"
    timestamp = pair["turn_time"].strftime("%Y-%m-%d %H:%M") if pair["turn_time"] else ""
    session_id = pair["session_id"]

    # 프로젝트 추출
    project = ""
    ws_upper = workspace.upper()
    for key in ("KIS", "AADS", "GO100", "SF", "NTV2", "NAS", "CEO"):
        if key in ws_upper:
            project = key
            break

    prompt = EXTRACTION_PROMPT.format(
        max_facts=MAX_FACTS_PER_TURN,
        user_msg=user_msg,
        ai_msg=ai_msg,
        workspace=workspace,
        timestamp=timestamp,
    )

    try:
        api_key = client._gemini_key
        resp = await client.post(
            f"https://generativelanguage.googleapis.com/v1beta/models/gemini-3-flash-preview:generateContent?key={api_key}",
            json={
                "contents": [{"parts": [{"text": prompt}]}],
                "generationConfig": {"temperature": 0.1, "maxOutputTokens": 1024},
            },
        )
        resp.raise_for_status()
        data = resp.json()
        text = data["candidates"][0]["content"]["parts"][0]["text"].strip()

        # Clean markdown
        if text.startswith("```"):
            text = text.split("\n", 1)[1] if "\n" in text else text[3:]
            if text.endswith("```"):
                text = text[:-3]
            text = text.strip()

        facts = json.loads(text)
        if not isinstance(facts, list) or not facts:
            return 0

        saved = 0
        async with pool.acquire() as conn:
            for fact in facts[:MAX_FACTS_PER_TURN]:
                category = fact.get("category", "")
                subject = fact.get("subject", "")
                detail = fact.get("detail", "")
                tags = fact.get("tags", [])

                if not category or not subject or not detail:
                    continue

                try:
                    # 중복 체크
                    existing = await conn.fetchval(
                        "SELECT 1 FROM memory_facts WHERE subject = $1 AND category = $2 AND project = $3 LIMIT 1",
                        subject[:300], category, project or None,
                    )
                    if existing:
                        continue

                    await conn.execute(
                        """
                        INSERT INTO memory_facts
                            (session_id, project, category, subject, detail, tags, confidence, created_at)
                        VALUES ($1, $2, $3, $4, $5, $6, 0.7, $7)
                        """,
                        session_id,
                        project or None,
                        category,
                        subject[:300],
                        detail,
                        tags,
                        pair["turn_time"] or datetime.utcnow(),
                    )
                    saved += 1
                except Exception as e:
                    pass  # 중복 등 무시

        return saved

    except json.JSONDecodeError:
        return 0
    except Exception as e:
        print(f"  에러: {e}")
        raise


async def generate_embeddings(pool):
    """저장된 memory_facts에 임베딩 생성."""
    try:
        from app.services.chat_embedding_service import embed_texts

        async with pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT id, category, subject FROM memory_facts WHERE embedding IS NULL LIMIT 200"
            )

        if not rows:
            print("임베딩 대상 없음")
            return

        print(f"  {len(rows)}건 임베딩 생성 중...")

        # 배치 처리
        batch_size = 20
        embedded = 0
        for i in range(0, len(rows), batch_size):
            batch = rows[i:i + batch_size]
            texts = [f"{r['category']}: {r['subject']}" for r in batch]

            try:
                embeddings = await embed_texts(texts)

                async with pool.acquire() as conn:
                    for row, emb in zip(batch, embeddings):
                        if emb:
                            await conn.execute(
                                "UPDATE memory_facts SET embedding = $1 WHERE id = $2",
                                str(emb), row["id"],
                            )
                            embedded += 1
            except Exception as e:
                print(f"  임베딩 배치 에러: {e}")

            await asyncio.sleep(0.5)

        print(f"  임베딩 완료: {embedded}/{len(rows)}건")

    except Exception as e:
        print(f"임베딩 생성 실패: {e}")


if __name__ == "__main__":
    asyncio.run(main())
