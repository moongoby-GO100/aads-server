#!/usr/bin/env python3
"""obys-v4 상위 정본 3문서를 goal_documents 대장에 등록한다.

배경. `doc_path ILIKE '%obys-v4%'` 99건은 전부 슬라이스 산출물(33x3)이었고,
소유권을 규율하는 상위 문서(PRD·로드맵·결정표)는 대장 밖에 있었다.
근거 문서가 대장 밖이면 정본 게이트가 표류·중복·세대교체를 감지하지 못한다.

등록 규칙은 POST /goals/{goal_id}/documents(add_goal_document)와 같다.
경로 정규화와 document_key/version 산출을 그 라우터의 헬퍼로 하므로 기존
99건과 키 체계가 갈라지지 않는다.

멱등하다. 같은 goal 에서 같은 document_key 가 is_latest 로 이미 있으면 건너뛴다.

사용:
    docker exec -i aads-server python3 - < scripts/register_obys_v4_core_docs.py
    docker exec -i aads-server python3 - --dry-run < scripts/register_obys_v4_core_docs.py
"""
from __future__ import annotations

import asyncio
import os
import sys

sys.path.insert(0, "/app")

import asyncpg  # noqa: E402

from app.routers.goals import (  # noqa: E402
    GoalDocRequest,
    _goal_document_identity,
    _normalize_goal_document_path,
)

GOAL_ID = "6c789b57-957d-4dfc-9c29-0baae4bd3741"

_SPEC_DIR = "docs/specs/obys-v4"

CORE_DOCS = [
    {
        "kind": "prd",
        "doc_path": "docs/PRD-OBYS-MOCKUP-V3-DETAIL-PAGES.md",
        "title": "오비서 V3 상세페이지 업무별 전용화 PRD",
        "note": "6절이 메뉴 소유권 정본. 로드맵 상태 판정의 근거 문서",
    },
    {
        "kind": "plan",
        "doc_path": f"{_SPEC_DIR}/roadmap.md",
        "title": "오비서 V4 기획 로드맵 — Spec of Specs",
        "note": "scripts/gen_obys_specs.py 가 생성하는 33슬라이스 상태 대장. 수기 편집 금지",
    },
    {
        "kind": "contract",
        "doc_path": f"{_SPEC_DIR}/owner-decision-20260922.md",
        "title": "obys-v4 슬라이스 소유권 결정표 (2026-09-22)",
        "note": (
            "CEO 승인 대기 상태의 상정안이다. 승인 전이므로 이 표의 권고를 "
            "확정 소유권으로 인용하지 마라"
        ),
    },
]


async def register(dry_run: bool) -> int:
    dsn = os.getenv("DATABASE_URL", "")
    if not dsn:
        print("FAIL: DATABASE_URL 미설정. 컨테이너 안에서 실행했는지 확인하라.")
        return 2

    conn = await asyncpg.connect(dsn)
    try:
        tenant_id = await conn.fetchval(
            "SELECT tenant_id FROM goals WHERE id = $1::uuid", GOAL_ID
        )
        if tenant_id is None:
            print(f"FAIL: goal {GOAL_ID} 없음")
            return 2

        for spec in CORE_DOCS:
            req = GoalDocRequest(**spec, set_latest=True)
            path = _normalize_goal_document_path(req.doc_path)
            document_key, version = _goal_document_identity(req, path)

            async with conn.transaction():
                await conn.fetchval(
                    "SELECT pg_advisory_xact_lock(hashtextextended($1, 0))",
                    f"goal-document:{GOAL_ID}:{document_key}",
                )
                existing = await conn.fetchrow(
                    "SELECT id, doc_path FROM goal_documents "
                    "WHERE goal_id = $1::uuid AND document_key = $2 AND is_latest",
                    GOAL_ID, document_key,
                )
                if existing:
                    print(f"SKIP  {req.kind:<8} {path} (key={document_key} 이미 is_latest: "
                          f"{existing['doc_path']})")
                    continue
                if dry_run:
                    print(f"WOULD {req.kind:<8} {path} key={document_key} v{version}")
                    continue
                row = await conn.fetchrow(
                    """
                    INSERT INTO goal_documents
                        (goal_id, kind, doc_path, title, note, created_by,
                         document_key, version, status, is_latest,
                         change_summary, supersedes_id, updated_at)
                    VALUES ($1::uuid, $2, $3, $4, $5, 'ceo',
                            $6, $7, 'active', true, NULL, NULL, NOW())
                    ON CONFLICT (goal_id, doc_path) DO NOTHING
                    RETURNING id
                    """,
                    GOAL_ID, req.kind, path, req.title, req.note,
                    document_key, version,
                )
                if row is None:
                    print(f"SKIP  {req.kind:<8} {path} (같은 doc_path 행이 이미 있음)")
                else:
                    print(f"ADD   {req.kind:<8} {path} key={document_key} v{version}")
    finally:
        await conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(register("--dry-run" in sys.argv[1:])))
