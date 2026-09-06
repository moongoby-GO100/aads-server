"""오케스트레이터 워크스페이스 preload 검증 스크립트.

docker exec aads-server python3 /app/scripts/verify_orchestrator_preload.py
"""
import asyncio
import sys

sys.path.insert(0, "/app")


async def main() -> int:
    from app.core.db_pool import init_pool, get_pool  # noqa: F401
    try:
        await init_pool()
    except Exception as e:  # pool may already be initialised
        print(f"[warn] init_pool: {e}")

    from app.services.workspace_preloader import (
        _scope_projects,
        build_workspace_preload,
        is_orchestrator_workspace,
    )

    print("is_orchestrator_workspace(CEO) =", is_orchestrator_workspace("CEO"))
    print("is_orchestrator_workspace(GO100) =", is_orchestrator_workspace("GO100"))
    print("scope(CEO) =", _scope_projects("CEO"))
    print("scope(GO100) =", _scope_projects("GO100"))

    ok = True
    for proj in ("CEO", "GO100"):
        block = await build_workspace_preload(project=proj, session_id=None)
        print(f"\n===== preload({proj}) chars={len(block)} =====")
        print(block[:2500])
        if proj == "CEO":
            hit = [p for p in ("[AADS]", "[GO100]", "[KIS]", "[SF]", "[NTV2]") if p in block]
            print("\n[CEO] cross-project tags found:", hit)
            ok = ok and len(hit) >= 2
        else:
            # 일반 프로젝트 모드 누수 검사: fact/경고 라인의 프로젝트 태그 위치만 검사
            # ("예상 관심사항" 같은 전역 블록의 [KIS] 표기는 누수가 아님)
            leak = []
            for ln in block.splitlines():
                for p in ("[AADS]", "[KIS]", "[SF]", "[NTV2]", "[CEO]"):
                    if (ln.startswith("  - [") and ln[11:].startswith(p)) or ln.startswith(f"  ⚠️ {p}"):
                        leak.append(p)
            leak = sorted(set(leak))
            print("\n[GO100] leak tags (should be empty):", leak)
            ok = ok and not leak
    print("\nRESULT:", "PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
