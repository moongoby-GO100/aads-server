"""작업 레시피(Work Recipe) — 스마트 브라우저 P0.

한 번 성공한 브라우저 작업을 레시피로 굳혀서 다음부터는 LLM 없이 재생한다.

- :mod:`schema`  레시피 YAML 파서/직렬화기 + `{{var}}` 치환기
- :mod:`store`   work_recipes 조회/저장(버전 증가) + 실행 기록 recorder
- :mod:`player`  레시피 순차 재생 엔진 (정상 경로 LLM 호출 0회)

이 패키지는 기존 `app/browser_bridge/`, `app/services/browser_recipe_registry.py`
와 별개의 신규 모듈이다. 기존 모듈은 읽기만 하고 수정하지 않는다.
"""
from __future__ import annotations

from app.services.work_recipe.schema import (
    ALLOWED_ACTIONS,
    RISK_LEVELS,
    RecipeInput,
    RecipeStep,
    WorkRecipe,
    parse_recipe,
    render_template,
    risk_rank,
)
from app.services.work_recipe.player import (
    DEFAULT_MAX_RISK,
    DEFAULT_RETRIES,
    DEFAULT_STEP_TIMEOUT,
    RecipePlayer,
    RunResult,
    StepResult,
    play_recipe,
)

__all__ = [
    "ALLOWED_ACTIONS",
    "RISK_LEVELS",
    "RecipeInput",
    "RecipeStep",
    "WorkRecipe",
    "parse_recipe",
    "render_template",
    "risk_rank",
    "DEFAULT_MAX_RISK",
    "DEFAULT_RETRIES",
    "DEFAULT_STEP_TIMEOUT",
    "RecipePlayer",
    "RunResult",
    "StepResult",
    "play_recipe",
]
