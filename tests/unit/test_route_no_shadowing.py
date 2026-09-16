"""라우트 그림자(ROUTE_SHADOWED) 회귀 방지.

배경: `app/api/ops.py` 에 `@router.get("/ops/codex-usage")` 가 두 번 있었다.
FastAPI 는 **먼저 등록된 라우트를 쓰므로** 뒤엣것은 절대 실행되지 않는다.
예외가 나지 않아 HTTP 로는 보이지 않았고, 2026-05-18 부터 2026-09-16 까지
넉 달을 죽은 채로 남아 있었다. AAG 정적 스캔이 P0 로 잡아냈다.

이 테스트는 같은 일이 다시 생기면 커밋 단계에서 걸리게 한다.
"""

from collections import Counter

import pytest

from app.main import app


def _mounted_routes():
    """(method, path) 쌍 목록. HEAD/OPTIONS 자동 생성분은 제외한다."""
    pairs = []
    for route in app.routes:
        path = getattr(route, "path", None)
        methods = getattr(route, "methods", None)
        if not path or not methods:
            continue
        for method in methods:
            if method in ("HEAD", "OPTIONS"):
                continue
            pairs.append((method, path))
    return pairs


def test_codex_usage_is_registered_exactly_once():
    """이 테스트가 존재하는 이유 — 실제로 두 번 등록돼 있었다."""
    pairs = _mounted_routes()
    hits = [p for p in pairs if p == ("GET", "/api/v1/ops/codex-usage")]
    assert len(hits) == 1, f"/api/v1/ops/codex-usage 등록 {len(hits)}회 — 뒤엣것은 도달 불가"


def test_no_duplicate_method_path_registrations():
    """같은 METHOD+경로가 두 번 등록되면 뒤엣것은 죽은 코드다."""
    counts = Counter(_mounted_routes())
    dupes = {k: v for k, v in counts.items() if v > 1}
    assert not dupes, (
        "도달 불가 라우트가 생겼다 — 먼저 등록된 것만 살고 나머지는 죽는다: "
        + ", ".join(f"{m} {p} x{n}" for (m, p), n in sorted(dupes.items()))
    )


def test_dead_codex_helpers_are_gone():
    """구 핸들러 전용 헬퍼가 되살아나면 그림자도 같이 돌아온 것이다."""
    import app.api.ops as ops

    for dead in ("_CODEX_USAGE_PROXY_CACHE", "_CODEX_USAGE_PROXY_TTL",
                 "_build_codex_usage_fallback", "get_codex_usage"):
        assert not hasattr(ops, dead), f"{dead} 이 되살아났다"

    # 살아 있는 경로는 그대로여야 한다
    for alive in ("get_codex_cli_usage", "_normalize_codex_usage_payload",
                  "_codex_usage_fallback"):
        assert hasattr(ops, alive), f"{alive} 이 사라졌다 — 살아 있는 경로다"


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-q"]))
