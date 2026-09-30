#!/usr/bin/env python3
"""aads_ohvis_public_screen_check v2(verify 절 포함) 등록 + 1회 재생.

v1 행은 건드리지 않는다. v2 는 save_recipe 가 새 버전으로 INSERT 한다.
대상은 https://aads.newtalk.kr/ohvis 공개 GET 뿐이며 로그인·입력·쓰기가 없다.

    python3 scripts/work_recipe_aads_ohvis_verify_v2.py register
    python3 scripts/work_recipe_aads_ohvis_verify_v2.py run
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys

from app.core.db_pool import close_pool, get_pool, init_pool
from app.services.work_recipe.audit import GuardedRunRecorder
from app.services.work_recipe.executor import BrowserRecipeExecutor
from app.services.work_recipe.orchestrator import GenericVerificationExecutor
from app.services.work_recipe.player import play_recipe
from app.services.work_recipe.schema import parse_recipe
from app.services.work_recipe.store import save_recipe

NAME = "aads_ohvis_public_screen_check"
DOMAIN = "aads.newtalk.kr"
TASK_ID = "AADS-WORKRECIPE-GENERIC-VERIFY-EVIDENCE-20260930"
LOGIN_URL = "https://aads.newtalk.kr/login?redirect=%2Fohvis"
VERIFY = [
    {"action": "snapshot", "assertion": "url_equals", "expected": LOGIN_URL, "risk": "READ",
     "description": "/ohvis 가 공개 로그인 화면으로 이동했는가"},
    {"action": "snapshot", "selector": "h1", "assertion": "element_visible", "risk": "READ",
     "description": "제목 요소가 보이는가"},
    {"action": "snapshot", "selector": "h1", "assertion": "text_contains", "expected": "OHVIS",
     "risk": "READ", "description": "제목에 OHVIS 문구가 있는가"},
]


async def _latest_row(conn):
    return await conn.fetchrow(
        "SELECT id, tenant_id, version, spec FROM work_recipes WHERE name=$1 AND domain=$2 "
        "ORDER BY version DESC LIMIT 1", NAME, DOMAIN)


def _spec(row) -> dict:
    spec = row["spec"]
    return json.loads(spec) if isinstance(spec, str) else dict(spec)


async def register() -> None:
    async with get_pool().acquire() as conn:
        row = await _latest_row(conn)
    if row is None:
        sys.exit("v1 레시피가 없습니다")
    spec = _spec(row)
    if spec.get("verify"):
        print(f"이미 verify 절이 있는 버전이 있습니다: v{row['version']} (재등록 안 함)")
        return
    recipe = parse_recipe({**spec, "verify": VERIFY, "version": int(row["version"]) + 1})
    saved = await save_recipe(recipe, tenant_id=row["tenant_id"], created_by=f"task:{TASK_ID}")
    print("registered", saved["name"], "v", saved["version"], "max_risk", saved["max_risk"])


async def run() -> None:
    async with get_pool().acquire() as conn:
        row = await _latest_row(conn)
    spec = _spec(row)
    if not spec.get("verify"):
        sys.exit("verify 절이 있는 버전이 없습니다 — register 먼저")
    recipe = parse_recipe(spec)
    executor = BrowserRecipeExecutor()
    result = await play_recipe(
        recipe,
        GenericVerificationExecutor(executor, recipe),
        recorder=GuardedRunRecorder(
            domain=recipe.domain, tenant_id=row["tenant_id"],
            triggered_by=f"task:{TASK_ID}", task_id=TASK_ID, requested_by=f"task:{TASK_ID}"),
        max_risk="READ",
        context={"domain": recipe.domain},
    )
    print("run_id", result.run_id, "status", result.status, "llm_calls", result.llm_calls,
          "error", result.error)
    for step in result.steps:
        print(" ", step.phase, step.seq, step.action, step.status, step.error)


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("mode", choices=("register", "run"))
    args = parser.parse_args()
    await init_pool()
    try:
        await (register() if args.mode == "register" else run())
    finally:
        await close_pool()


if __name__ == "__main__":
    asyncio.run(main())
